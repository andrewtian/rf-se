"""Hardware-free tests for the CLIENT-side reliability fixes of the networked GPIB bridge.

Covers a 3-agent audit's client-side findings, one section per fix:
  1  RF safety: a mid-point analyzer drop must NOT leave the source radiating (loop try/finally
     + a guaranteed rf_off backstop in Coordinator.release_control).
  2  A wedged adapter must not report READY (a liveness probe gates READY) and must escalate to a
     terminal FAULT after K consecutive failures; ensure() never auto-clears FAULT. reconnects
     counts only real drops, not every READY transition.
  3  set_timeout() must ALSO raise the CLIENT socket read deadline above the bridge timeout.
  4  reconnect() must re-assert an active lease (re-send L) so auto-reconnect keeps exclusivity.
  5  ENOL handling: one re-address + retry, then a typed AdapterNotAnswering (feeds the FAULT
     classifier); the shared `Z ping` / `Z recover` recovery-verb contract.
  6  A small default ensure() backoff; a bounded keepalive-join ceiling; reconnect swaps the socket
     under the txn lock (RLock, reentrant).

All fakes are FakeT / fake-bridge; no hardware, no pyvisa.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
        rf-se/se299/tests/test_client_reliability.py -q
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import connection as conn
import control_plane
import coordinator
import discover as disc
import drivers
from gpib_bridge import ni_gpib_server


def _start_fake_server(token=None):
    """A fake-backend bridge on an ephemeral port; returns the port (daemon thread)."""
    srv = ni_gpib_server.listen("127.0.0.1", 0)
    port = srv.getsockname()[1]
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "token": token}, daemon=True).start()
    return port


def _cfg():
    # one short 2-point low band (< 2.9 GHz -> no preselector path) keeps the loop fakes minimal
    band = cfg_mod.BandPlan("t", 1e9, 2e9, 2, 14.0, 12.0, -150.0, target_se_db=100.0)
    return cfg_mod.Campaign(bands=(band,), label="reliability-test")


# ============================================================ fix 1: RF-off on a mid-point drop

class _RfSource:
    """A source that records its RF state + on/off counts; enough surface for the loop primitives."""

    def __init__(self):
        self.rf = False
        self.on_calls = 0
        self.off_calls = 0

    def prepare(self):
        self.rf = False

    def set_power(self, p_dbm):
        pass

    def set_freq(self, f_hz):
        pass

    def rf_on(self):
        self.rf = True
        self.on_calls += 1

    def rf_off(self):
        self.rf = False
        self.off_calls += 1

    def await_settled(self, settle_s=0.05, use_opc=False):
        pass

    def settle(self, settle_s=0.05):
        pass


class _GoodAnalyzer:
    """Floor + tone reads both succeed (used to build a reference for the wall-pass test)."""

    def __init__(self):
        self.detector = "POS"

    def prepare(self):
        pass

    def configure(self, *a):
        pass

    def measure_floor(self, f_hz, s):
        return (f_hz, -120.0)

    def measure_peak(self, f_hz, s):
        return (f_hz, -40.0)


class _DropAnalyzer(_GoodAnalyzer):
    """The source-off floor read succeeds, but the RF-ON tone read drops mid-point."""

    def measure_peak(self, f_hz, s):
        raise IOError("simulated link drop mid-read")


def test_acquire_reference_leaves_rf_off_on_mid_point_drop():
    import loop
    src = _RfSource()
    with pytest.raises(IOError):
        loop.acquire_reference(_cfg(), src, _DropAnalyzer())
    assert src.rf is False                     # RF NOT left radiating after the drop
    assert src.off_calls >= 1                  # the try/finally fired rf_off


def test_measure_wall_leaves_rf_off_on_mid_point_drop():
    import loop
    cfg = _cfg()
    reference = loop.acquire_reference(cfg, _RfSource(), _GoodAnalyzer())   # a clean reference first
    src = _RfSource()
    with pytest.raises(IOError):
        loop.measure_wall(cfg, src, _DropAnalyzer(), reference)
    assert src.rf is False
    assert src.off_calls >= 1


def test_stepped_cw_sweep_leaves_rf_off_on_mid_point_drop():
    import loop
    src = _RfSource()
    with pytest.raises(IOError):
        loop.stepped_cw_sweep(_cfg(), src, _DropAnalyzer(), [1.5e9], bench=None)
    assert src.rf is False
    assert src.off_calls >= 1


def test_localize_leaves_rf_off_on_mid_point_drop():
    import loop
    src = _RfSource()
    with pytest.raises(IOError):
        loop.localize(_cfg(), src, _DropAnalyzer(), 1.5e9, positions=[0.0, 0.1])
    assert src.rf is False
    assert src.off_calls >= 1


def test_coordinator_release_control_forces_rf_off():
    # a guaranteed RF-off backstop: even if a primitive was bypassed and left RF on, releasing
    # control turns the source off (BEFORE dropping the lease) so it cannot keep radiating.
    cp = control_plane.simulated(cfg_mod.default())
    coord = cp.make_coordinator()
    assert coord.ensure_ready()
    coord.take_control()
    cp.bench.src_rf_on = True                   # pretend a bypassed path left the source ON
    coord.release_control()
    assert cp.bench.src_rf_on is False          # release_control forced it off


# ============================================================ fix 2: FAULT + liveness probe

def _dev(model="Anritsu 68369A/NV", pad=5):
    return disc.DiscoveredDevice("net", f"net:h:1:{pad}", model, "", (), "", model)


def test_persistently_silent_adapter_escalates_to_terminal_fault():
    # a link whose open (liveness) ALWAYS fails reaches FAULT after K, ensure() returns False,
    # and a subsequent ensure() stays FAULT WITHOUT re-attempting (terminal, never auto-cleared).
    dev = _dev()
    opens = {"n": 0}

    def open_always_fails(d):
        opens["n"] += 1
        raise IOError("gpib bridge ENOL: no listeners currently addressed")

    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=lambda: [dev], open_fn=open_always_fails,
                           retries=5, backoff_s=0.0, fault_after=3)
    assert link.ensure() is False
    assert link.state == conn.FAULT
    assert opens["n"] == 3                       # escalated after exactly K consecutive failures
    r = link.reason.lower()
    assert "power-cycle" in r and "tx" in r and "pad 5" in r   # actionable, names role + pad

    opens["n"] = 0
    assert link.ensure() is False               # still terminal
    assert link.state == conn.FAULT
    assert opens["n"] == 0                       # ensure() short-circuited -- NO re-open attempt


def test_adapter_wedged_verdict_faults_immediately():
    dev = _dev()

    def open_wedged(d):
        raise drivers.AdapterNotAnswering("adapter wedged", verdict="ADAPTER_WEDGED")

    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=lambda: [dev], open_fn=open_wedged,
                           retries=5, backoff_s=0.0, fault_after=3)
    assert link.ensure() is False
    assert link.state == conn.FAULT             # terminal on the FIRST failure (wedged verdict)
    assert "ADAPTER_WEDGED" in link.reason


# ---- 44.2 soft-recover hook at the FAULT threshold (opt-in; default None = prior behavior) ----

def test_soft_recover_hook_averts_fault_on_success():
    # at the FAULT threshold, a recover_fn that reports a successful QMP virtual-replug must AVERT the
    # terminal FAULT: the streak clears and the link drops to DISCONNECTED (revalidate next ensure).
    calls = {"n": 0}

    def recover(exc):
        calls["n"] += 1
        return True                                  # a successful soft recovery

    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=lambda: [], open_fn=lambda d: None,
                           retries=5, backoff_s=0.0, fault_after=1, recover_fn=recover)
    link._bump_failure(IOError("boom"))              # fault_after=1 -> would FAULT; recovery averts it
    assert calls["n"] == 1                            # recovery attempted at the threshold
    assert link.state == conn.DISCONNECTED           # averted -> revalidate, NOT terminal FAULT
    assert "soft-recovered" in link.reason.lower()
    assert link._consec_fail == 0                     # the failure streak was cleared


def test_soft_recover_hook_falls_through_to_fault_on_failure():
    # a recover_fn that reports FAILURE (a HARD FX2 wedge a QMP reset cannot clear) must fall through to
    # the terminal FAULT with the actionable physical-replug message (44.3).
    dev = _dev()

    def open_always_fails(d):
        raise IOError("gpib bridge ENOL: no listeners currently addressed")

    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=lambda: [dev], open_fn=open_always_fails,
                           retries=5, backoff_s=0.0, fault_after=3,
                           recover_fn=lambda exc: False)
    assert link.ensure() is False
    assert link.state == conn.FAULT
    assert "power-cycle" in link.reason.lower()      # HARD: physical remedy, not a silent spin


def test_soft_recover_is_not_reentrant():
    # a bus op that faults INSIDE recover_fn must NOT recurse into recovery (the _recovering guard).
    dev = _dev()

    def open_always_fails(d):
        raise IOError("ENOL")

    depth = {"cur": 0, "max": 0}
    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=lambda: [dev], open_fn=open_always_fails,
                           retries=2, backoff_s=0.0, fault_after=1)

    def recover(exc):
        depth["cur"] += 1
        depth["max"] = max(depth["max"], depth["cur"])
        link._on_drop(IOError("a bus op inside recovery"))   # re-enters _bump_failure; must NOT recurse
        depth["cur"] -= 1
        return False

    link._recover_fn = recover
    link.ensure()
    assert depth["max"] == 1                          # recovery never re-entered itself
    assert link.state == conn.FAULT                  # inner fault + failed recovery -> terminal


def test_absent_device_is_not_a_fault():
    # ABSENT (nothing on the bus) must stay ABSENT, never escalate to FAULT -- distinct condition.
    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=lambda: [], open_fn=lambda d: None,
                           retries=5, backoff_s=0.0, fault_after=3)
    assert link.ensure() is False
    assert link.state == conn.ABSENT


def test_liveness_probe_rejects_a_silent_device():
    # control_plane wraps a network open_fn with a liveness probe: a device that does not answer
    # IDN fails the open (and the half-open driver is closed), so the link never reaches READY.
    class _Silent:
        def __init__(self):
            self.closed = False
            self.t = None

        def idn(self):
            raise IOError("gpib bridge ENOL: no listeners currently addressed")

        def close(self):
            self.closed = True

    made = {}

    def open_fn(dev):
        made["drv"] = _Silent()
        return made["drv"]

    probed = control_plane._open_with_probe(open_fn)
    with pytest.raises(IOError):
        probed(None)
    assert made["drv"].closed is True           # the failed half-open driver was closed


def test_liveness_probe_prefers_ping_then_falls_back_to_idn():
    class _T:
        def __init__(self, ping_ok):
            self.ping_ok = ping_ok
            self.pinged = False
            self.timeout_ms = 10000
            self.timeouts = []

        def set_timeout(self, ms):
            self.timeouts.append(ms)

        def ping(self):
            self.pinged = True
            if not self.ping_ok:
                raise IOError("gpib bridge error: unknown verb 'Z'")
            return "pong"

    class _D:
        def __init__(self, ping_ok):
            self.t = _T(ping_ok)
            self.idn_read = False

        def idn(self):
            self.idn_read = True
            return "HP8565E"

    d1 = _D(ping_ok=True)
    control_plane._probe_liveness(d1)
    assert d1.t.pinged and not d1.idn_read      # a working ping suffices -- no IDN fallback

    d2 = _D(ping_ok=False)
    control_plane._probe_liveness(d2)
    assert d2.t.pinged and d2.idn_read          # old bridge (no Z) -> IDN-readback fallback
    # the probe bounds the deadline, then RESTORES the campaign read timeout
    assert d2.t.timeouts[0] == control_plane._PROBE_TIMEOUT_MS
    assert d2.t.timeouts[-1] == 10000


def test_reconnects_counts_real_drops_not_repeated_connects():
    # a repeated successful connect() (no prior drop) must NOT inflate reconnects; only a recovery
    # from an actual mid-op DROP counts.
    bench = drivers.SimBench()
    link = conn.SourceLink(expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
                           discover_fn=disc.sim_inventory,
                           open_fn=lambda d: drivers.SimSignalGenerator(bench), backoff_s=0.0)
    assert link.connect().state == "READY"
    assert link.reconnects == 0
    link.connect()                              # a second clean connect -- NOT a reconnect
    assert link.reconnects == 0
    link.set_freq(1e9)                          # a genuine bus op
    link._on_drop(IOError("boom"))              # simulate a mid-op drop -> DISCONNECTED
    assert link.reconnects == 0
    assert link.ensure() is True                # recover
    assert link.reconnects == 1                 # NOW it is a real reconnect


# ============================================================ fix 3: set_timeout socket deadline

def test_set_timeout_raises_socket_deadline_above_bridge_timeout():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    t.set_timeout(30000)                        # a slow op raises the bridge timeout to 30 s
    assert t._sock.gettimeout() > 30.0          # ... the CLIENT socket now outlives it (bridge wins)
    assert drivers.NetworkTransport.CONNECT_TIMEOUT_S <= 5.0   # a SEPARATE short connect deadline
    t.close()


# ============================================================ fix 4: reconnect re-asserts the lease

def test_reconnect_reasserts_active_lease_resends_L():
    port = _start_fake_server()
    ni_gpib_server._LEASES.reset()
    try:
        t = drivers.NetworkTransport("127.0.0.1", port, 18)
        t.lease(scope="device", ttl_s=30)
        sent = []
        orig = t._send_lease

        def spy(scope, ttl_s):
            sent.append((scope, ttl_s))
            return orig(scope, ttl_s)

        t._send_lease = spy
        t.reconnect()                           # an auto-reconnect must re-assert the held lease
        assert sent and sent[0][0] == "device"  # an L was re-sent on the fresh socket
        assert t._lease_scope == "device"       # still remembered for any FURTHER reconnect
        t.close()
    finally:
        ni_gpib_server._LEASES.reset()


def test_release_lease_forgets_it_so_reconnect_does_not_reassert():
    port = _start_fake_server()
    ni_gpib_server._LEASES.reset()
    try:
        t = drivers.NetworkTransport("127.0.0.1", port, 18)
        t.lease(scope="device", ttl_s=30)
        t.release_lease()
        assert t._lease_scope is None           # released -> no phantom re-assert on a later drop
        sent = []
        orig = t._send_lease
        t._send_lease = lambda s, ttl: (sent.append(s), orig(s, ttl))[1]
        t.reconnect()
        assert sent == []                       # nothing re-asserted (no active lease)
        t.close()
    finally:
        ni_gpib_server._LEASES.reset()


# ============================================================ fix 5: ENOL + Z recovery contract

def test_enol_readdresses_once_then_raises_typed_error():
    # a wedged/absent device ENOLs; the transport RE-ADDRESSES once and retries, and on a second
    # ENOL raises a TYPED AdapterNotAnswering (not a bare IOError string) that feeds the classifier.
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    calls = []

    def fake_txn(token, payload=b""):
        calls.append(token)
        if token in ("W", "Q"):
            raise IOError("gpib bridge error: no listeners currently addressed")
        if token == "Z":
            raise IOError("gpib bridge error: unknown verb 'Z'")   # old bridge: recover unsupported
        return b""                                                 # A (re-address) etc. ack

    t._txn = fake_txn
    with pytest.raises(drivers.AdapterNotAnswering) as ei:
        t.query("ID?")
    assert calls == ["Q", "A", "Q", "Z"]        # op, ONE re-address, retry-op, then recover probe
    assert ei.value.verdict == "DEVICE_SILENT"  # recover unsupported -> default verdict
    assert isinstance(ei.value, IOError)        # subclass -> caught by every existing IOError handler
    t.close()


def test_enol_recovers_silently_after_one_readdress():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    state = {"first": True}

    def fake_txn(token, payload=b""):
        if token == "Q":
            if state["first"]:
                state["first"] = False
                raise IOError("gpib bridge error: no listeners currently addressed")
            return b"HP8565E"
        return b""                              # A ack

    t._txn = fake_txn
    assert t.query("ID?") == "HP8565E"          # the re-address rescued the op; no error surfaced
    t.close()


def test_adapter_not_answering_carries_wedged_flag():
    e = drivers.AdapterNotAnswering("x", verdict="ADAPTER_WEDGED")
    assert e.adapter_wedged is True and isinstance(e, IOError)
    assert drivers.AdapterNotAnswering("y").adapter_wedged is False


def test_recover_parses_verdict_and_detail():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    t._txn = lambda token, payload=b"": b"ADAPTER_WEDGED usb reset failed"
    assert t.recover() == ("ADAPTER_WEDGED", "usb reset failed")
    t.close()


def test_ping_and_recover_roundtrip_against_the_bridge():
    # the shared recovery-verb contract end-to-end over TCP: `Z ping` -> '= OK <sb>' and
    # `Z recover` -> '= <VERDICT> <detail>', parsed by the client.
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    assert t.ping().startswith("OK")            # '= OK 0' -> liveness confirmed
    verdict, detail = t.recover()
    assert verdict in drivers.NetworkTransport.RECOVER_VERDICTS and verdict == "OK"
    assert detail                               # the bridge always returns a human detail
    t.close()


def test_ping_recover_unsupported_on_an_old_bridge_is_treated_as_ioerror():
    # an OLD bridge (predating the Z verb) replies '! unknown verb' -> IOError, which callers catch
    # and treat as "recovery unsupported" (falling back to the IDN-readback liveness probe).
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    t._txn = lambda token, payload=b"": (_ for _ in ()).throw(
        IOError("gpib bridge error: unknown verb 'Z'"))
    with pytest.raises(IOError):
        t.ping()
    with pytest.raises(IOError):
        t.recover()
    t.close()


# ============================================================ fix 6: backoff / join ceiling / RLock

def test_analyzer_link_has_a_small_default_backoff():
    link = conn.AnalyzerLink(conn.DEFAULT_8565EC, (1e9, 6e9),
                             discover_fn=lambda: [], open_fn=lambda d: None)
    assert link.backoff_s > 0                   # don't hammer a hard-down bridge between retries


def test_reconnect_swaps_socket_under_a_reentrant_txn_lock():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    old = t._sock
    t.reconnect()
    assert t._sock is not old                   # a fresh socket was swapped in
    assert "8565E" in t.query("ID?")            # ... and it works
    # the swap runs under the txn lock, which is REENTRANT (RLock) so _connect's own H/A/T/L
    # transactions can re-enter it on the same thread without deadlock.
    assert t._txn_lock.acquire(blocking=False)
    assert t._txn_lock.acquire(blocking=False)  # second acquire on the same thread -> RLock
    t._txn_lock.release()
    t._txn_lock.release()
    t.close()
