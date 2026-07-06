"""Networking tests for the multi-instance control plane (R6). Grows per increment.

Increment 1: one multiplexing gateway serves BOTH instruments (per bound pad -> 68369A
source / 8565EC analyzer), and MULTIPLE instances connect + operate CONCURRENTLY
(thread-per-connection; the old sequential accept loop blocked the second in the backlog).

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_networked.py -q
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import connection as conn
import control_plane
import coordinator
import discover as disc
import discovery
import drivers
import roles
import telemetry
from gpib_bridge import ni_gpib_server


@pytest.fixture(autouse=True)
def _reset_leases():
    # the lease table is a process-wide module global shared by every server thread; clear
    # it around each test so an arbitration test can't leak a lease into the next test.
    ni_gpib_server._LEASES.reset()
    yield
    ni_gpib_server._LEASES.reset()


def _start_server(signal="flat", token=None):
    srv = ni_gpib_server.listen("127.0.0.1", 0)
    port = srv.getsockname()[1]
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "signal": signal, "token": token},
                     daemon=True).start()
    return port


def test_one_gateway_serves_source_and_analyzer_per_pad():
    port = _start_server()
    src = drivers.NetworkTransport("127.0.0.1", port, 5)      # pad 5  -> 68369A source
    ana = drivers.NetworkTransport("127.0.0.1", port, 18)     # pad 18 -> 8565EC analyzer
    assert "68369" in src.query("*IDN?")
    assert "8565" in ana.query("ID?")
    src.close()
    ana.close()


def test_two_instances_connect_and_operate_concurrently():
    # both transports are OPEN at once; the old one-client-at-a-time loop would block t2.
    port = _start_server()
    t1 = drivers.NetworkTransport("127.0.0.1", port, 5)
    t2 = drivers.NetworkTransport("127.0.0.1", port, 18)      # opens while t1 is still open
    for _ in range(5):                                        # interleave on both, live
        assert "68369" in t1.query("*IDN?")
        assert "8565" in t2.query("ID?")
    t1.close()
    t2.close()


def test_concurrent_hammer_no_cross_talk():
    # two threads pound the two logical instruments on one gateway; per-pad answers stay
    # correct (each session has its own bound pad; no interleaving corruption).
    port = _start_server()
    t_src = drivers.NetworkTransport("127.0.0.1", port, 5)
    t_ana = drivers.NetworkTransport("127.0.0.1", port, 18)
    errs = []

    def hammer(t, expect, n):
        for _ in range(n):
            try:
                if expect not in t.query("*IDN?"):
                    errs.append(("wrong-id", expect))
            except Exception as e:                           # pragma: no cover
                errs.append(e)

    ths = [threading.Thread(target=hammer, args=(t_src, "68369", 25)),
           threading.Thread(target=hammer, args=(t_ana, "8565", 25))]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert errs == []
    t_src.close()
    t_ana.close()


def test_analyzer_pad_still_reads_a_trace_over_the_gateway():
    # the analyzer pad keeps its full command surface (a 601-pt trace) alongside a source pad
    port = _start_server(signal="moving")
    ana = drivers.Agilent856xEC(drivers.NetworkTransport("127.0.0.1", port, 18))
    freqs, levels = ana.read_trace("A")
    assert len(levels) == 601 and "8565" in ana.idn()
    ana.close()


# ============================================================ Increment 2: SourceLink
# The TX source (68369A) gets the SAME self-managing lifecycle as the RX analyzer -- a
# symmetric RX/TX control plane (R9). SourceLink mirrors AnalyzerLink: discover -> validate
# -> READY -> auto-reconnect, driven by writes instead of trace reads.

def _sim_source_link(span=(1e9, 6e9), retries=3, discover_fn=None, open_fn=None):
    bench = drivers.SimBench()
    return conn.SourceLink(
        expected=conn.DEFAULT_68369A, span=span,
        discover_fn=discover_fn or disc.sim_inventory,
        open_fn=open_fn or (lambda dev: drivers.SimSignalGenerator(bench)),
        retries=retries), bench


def test_sourcelink_discovers_and_validates_the_68369():
    link, _ = _sim_source_link()
    st = link.connect()
    assert st.state == "READY" and st.valid is True
    assert "68369" in st.model and "68369" in link.idn()


def test_sourcelink_absent_when_only_the_analyzer_is_present():
    # discover only the 8565EC -> the source token/family match nothing -> ABSENT (honest)
    only_ana = [disc.sim_inventory()[0]]
    link, _ = _sim_source_link(discover_fn=lambda: only_ana)
    st = link.connect()
    assert st.state == "ABSENT" and st.detected is False and st.valid is False


def test_sourcelink_invalid_when_wrong_683xx_unit_present():
    # a 68347C is the same 683xx family but the wrong unit -> DETECTED but INVALID, not ABSENT
    wrong = disc.DiscoveredDevice("visa", "GPIB0::5::INSTR", "Anritsu 68347C", "S1", (), "", "68347C")
    link, _ = _sim_source_link(discover_fn=lambda: [wrong], open_fn=lambda dev: None)
    st = link.connect()
    assert st.state == "INVALID" and st.detected is True and st.valid is False


def test_sourcelink_write_ops_drive_the_source_state():
    link, bench = _sim_source_link()
    link.ensure()
    link.set_freq(2.4e9)
    link.set_power(-5.0)
    link.rf_on()
    assert bench.src_freq_hz == 2.4e9 and bench.src_power_dbm == -5.0 and bench.src_rf_on is True
    link.rf_off()
    assert bench.src_rf_on is False


def test_sourcelink_rejects_read_sweep_and_read_point():
    link, _ = _sim_source_link()
    link.ensure()
    with pytest.raises(conn.LinkNotReady):
        link.read_sweep(101)
    with pytest.raises(conn.LinkNotReady):
        link.read_point(1e9)


def test_sourcelink_auto_reconnects_after_a_dropped_write():
    # the first opened source raises on its first set_freq (bus drop); the link drops to
    # DISCONNECTED, ensure() re-runs discover->open->validate with a fresh source, retry ok.
    bench = drivers.SimBench()

    class _FlakySource(drivers.SimSignalGenerator):
        def set_freq(self, f_hz):
            raise IOError("simulated bus drop")

    opens = {"n": 0}

    def open_fn(dev):
        opens["n"] += 1
        return _FlakySource(bench) if opens["n"] == 1 else drivers.SimSignalGenerator(bench)

    link = conn.SourceLink(
        expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
        discover_fn=disc.sim_inventory, open_fn=open_fn, retries=3)
    assert link.connect().state == "READY"
    with pytest.raises(conn.LinkDropped):
        link.set_freq(2e9)                       # flaky source drops the link
    assert link.ensure() is True                 # reconnect with a fresh, working source
    link.set_freq(2e9)
    assert bench.src_freq_hz == 2e9 and link.reconnects == 1


def test_control_plane_pair_rx_and_tx_both_ready_from_one_inventory():
    # the coordinator's premise: one discovery inventory yields BOTH a READY RX and a READY
    # TX, resolved by capability -- the modular (TX,RX) pair the SE loop runs over (R9).
    bench = drivers.SimBench()
    rx = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSpectrumAnalyzer(nf_model=drivers.demo_nearfield_spectrum()))
    tx = conn.SourceLink(
        expected=conn.DEFAULT_68369A, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSignalGenerator(bench))
    assert rx.ensure() is True and tx.ensure() is True
    assert "8565" in rx.status().model and "68369" in tx.status().model


# ==================================================== Increment 3: lease/lock arbitration
# One shared bus, many role instances -> a VXI-11-style lease gives ONE controller exclusive
# bus access to a scope (a device pad or the whole BUS); everyone else is an observer.

# -- registry logic (pure, deterministic; `now` injected -- no wall-clock sleeps) ----------

def test_lease_registry_exclusive_per_pad_and_expiry():
    reg = ni_gpib_server.LeaseRegistry()
    assert reg.acquire(5, holder=1, ttl=10, now=0)[0] is True
    assert reg.check(5, holder=2, now=1)[0] is False        # observer blocked on the held pad
    assert reg.check(18, holder=2, now=1)[0] is True         # a different pad is free
    assert reg.check(5, holder=1, now=1)[0] is True          # the holder itself is allowed
    assert reg.acquire(5, holder=2, ttl=10, now=1)[0] is False   # cannot steal a live lease
    assert reg.acquire(5, holder=2, ttl=10, now=11)[0] is True   # after TTL lapse, free to take


def test_lease_registry_bus_scope_blocks_every_other_pad():
    reg = ni_gpib_server.LeaseRegistry()
    reg.acquire("BUS", holder=1, ttl=10, now=0)
    assert reg.check(5, holder=2, now=1)[0] is False
    assert reg.check(18, holder=2, now=1)[0] is False
    assert reg.check(5, holder=1, now=1)[0] is True          # the BUS holder may touch any pad


def test_lease_registry_renew_and_release():
    reg = ni_gpib_server.LeaseRegistry()
    reg.acquire(5, 1, 10, now=0)
    assert reg.renew(1, 10, now=9)[0] is True                # extend before it lapses (->19)
    assert reg.check(5, 2, now=15)[0] is False               # still held thanks to the renew
    reg.release(1)
    assert reg.check(5, 2, now=15)[0] is True                # released -> free
    assert reg.renew(1, 10, now=15)[0] is False              # nothing to renew now


# -- end to end over TCP -------------------------------------------------------------------

def test_lease_blocks_a_second_controller_then_frees_on_release():
    port = _start_server()
    c1 = drivers.NetworkTransport("127.0.0.1", port, 18)
    c2 = drivers.NetworkTransport("127.0.0.1", port, 18)     # opens fine (A/T aren't arbitrated)
    c1.lease(scope="device", ttl_s=30)
    assert "8565" in c1.query("ID?")                         # the controller works
    with pytest.raises(IOError):
        c2.query("ID?")                                      # the observer is refused
    assert "session" in c2.lease_report()                    # but can still SEE the lease (R)
    c1.release_lease()
    assert "8565" in c2.query("ID?")                         # released -> c2 may use the bus
    c1.close()
    c2.close()


def test_bus_lease_blocks_a_different_pad_over_tcp():
    port = _start_server()
    ctrl = drivers.NetworkTransport("127.0.0.1", port, 18)
    other = drivers.NetworkTransport("127.0.0.1", port, 5)   # a DIFFERENT device pad
    ctrl.lease(scope="BUS", ttl_s=30)
    with pytest.raises(IOError):
        other.query("*IDN?")                                 # BUS lease locks even another pad
    ctrl.release_lease()
    assert "68369" in other.query("*IDN?")
    ctrl.close()
    other.close()


def test_two_device_leases_on_different_pads_coexist():
    # the coordinator holds BOTH instruments at once: an analyzer-pad lease and a source-pad
    # lease never conflict, so it can drive the RX and the TX concurrently.
    port = _start_server()
    ana = drivers.NetworkTransport("127.0.0.1", port, 18)
    src = drivers.NetworkTransport("127.0.0.1", port, 5)
    ana.lease(scope="device", ttl_s=30)
    src.lease(scope="device", ttl_s=30)                      # granted -- different pad
    assert "8565" in ana.query("ID?") and "68369" in src.query("*IDN?")
    ana.close()
    src.close()


def test_lease_expires_by_ttl_over_tcp():
    port = _start_server()
    c1 = drivers.NetworkTransport("127.0.0.1", port, 18)
    c2 = drivers.NetworkTransport("127.0.0.1", port, 18)
    c1.lease(scope="device", ttl_s=0.1)                      # 100 ms lease
    with pytest.raises(IOError):
        c2.query("ID?")
    time.sleep(0.15)                                         # let it lapse
    assert "8565" in c2.query("ID?")                         # auto-freed by TTL
    c1.close()
    c2.close()


def test_lease_released_on_hard_disconnect():
    port = _start_server()
    c1 = drivers.NetworkTransport("127.0.0.1", port, 5)
    c1.lease(scope="device", ttl_s=30)
    c2 = drivers.NetworkTransport("127.0.0.1", port, 5)
    with pytest.raises(IOError):
        c2.query("*IDN?")
    c1._sock.shutdown(socket.SHUT_RDWR)                      # hard drop (no clean C verb):
    c1._sock.close()                                         # shutdown forces the FIN past the makefile
    ok = False
    for _ in range(50):                                      # poll until the server's finally runs
        try:
            if "68369" in c2.query("*IDN?"):
                ok = True
                break
        except IOError:
            time.sleep(0.02)
    assert ok
    c2.close()


# ================================================ Increment 4: Coordinator (owns RX+TX, R8)
# The SE-coordinator role instance holds BOTH links, drives them in lockstep for the
# substitution measurement, and streams a live worst-case SE figure during operation.

def _sim_coordinator(cfg=None):
    """A Coordinator wired to the simulator: the RX analyzer and TX source SHARE one
    SimBench (the source sets the tone, the analyzer reads the same physics)."""
    cfg = cfg or cfg_mod.default()
    bench = drivers.SimBench(separation_m=cfg.geometry.separation_m)
    drivers.install_bench_models(bench, cfg)
    rx = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSpectrumAnalyzer(bench))
    tx = conn.SourceLink(
        expected=conn.DEFAULT_68369A, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSignalGenerator(bench))
    return coordinator.Coordinator(cfg, rx, tx), bench


def test_coordinator_runs_a_campaign_with_a_live_se_figure():
    coord, bench = _sim_coordinator()
    n = len(coord.cfg.frequencies())
    updates = []
    result = coord.run_campaign(bench=bench,
                                on_se_update=lambda fig, row: updates.append(fig["se_db"]))
    assert len(updates) == n                                 # one live SE update per wall point
    assert result["se_figure"]["points"] == n
    # the live figure is a running WORST case -> monotonically non-increasing, ending at min
    assert updates == sorted(updates, reverse=True)
    wall_min = min(r["se_reported_db"] for r in result["wall"].values())
    assert result["se_figure"]["se_db"] == pytest.approx(wall_min)
    assert "campaign_pass" in result["summary"]


def test_coordinator_raises_when_a_link_is_absent():
    cfg = cfg_mod.default()
    bench = drivers.SimBench()
    rx = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSpectrumAnalyzer(bench))
    tx = conn.SourceLink(                                    # no source on the bus
        expected=conn.DEFAULT_68369A, span=(1e9, 6e9),
        discover_fn=lambda: [], open_fn=lambda dev: None, retries=1)
    coord = coordinator.Coordinator(cfg, rx, tx)
    assert coord.ensure_ready() is False
    with pytest.raises(coordinator.CoordinatorNotReady):
        coord.run_campaign(bench=bench)


def test_coordinator_take_control_leases_both_instruments_over_tcp():
    # networked: take_control takes an exclusive lease on BOTH pads, so a concurrent observer
    # is refused the bus on either instrument until the coordinator releases.
    port = _start_server()
    cfg = cfg_mod.default()
    rx = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.Agilent856xEC(drivers.NetworkTransport("127.0.0.1", port, 18)))
    tx = conn.SourceLink(
        expected=conn.DEFAULT_68369A, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.Anritsu68369(drivers.NetworkTransport("127.0.0.1", port, 5)))
    coord = coordinator.Coordinator(cfg, rx, tx)
    assert coord.ensure_ready() is True
    coord.take_control()
    obs_ana = drivers.NetworkTransport("127.0.0.1", port, 18)
    obs_src = drivers.NetworkTransport("127.0.0.1", port, 5)
    with pytest.raises(IOError):
        obs_ana.query("ID?")                                 # analyzer pad locked
    with pytest.raises(IOError):
        obs_src.query("*IDN?")                               # source pad locked
    coord.release_control()
    assert "8565" in obs_ana.query("ID?") and "68369" in obs_src.query("*IDN?")
    obs_ana.close()
    obs_src.close()
    rx.close()
    tx.close()


# ==================================================== Increment 4b: lease keepalive
# A campaign can run longer than the lease TTL. take_control must keep BOTH leases alive
# (renewed from the same sessions) for as long as control is held, and release_control must
# stop that keepalive completely -- a lingering renewer would steal control back from the
# next controller.

def _tcp_coordinator(port, lease_ttl_s):
    cfg = cfg_mod.default()
    rx = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.Agilent856xEC(drivers.NetworkTransport("127.0.0.1", port, 18)))
    tx = conn.SourceLink(
        expected=conn.DEFAULT_68369A, span=(1e9, 6e9), discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.Anritsu68369(drivers.NetworkTransport("127.0.0.1", port, 5)))
    coord = coordinator.Coordinator(cfg, rx, tx, lease_ttl_s=lease_ttl_s)
    assert coord.ensure_ready() is True
    return coord, rx, tx


def test_take_control_keepalive_renews_lease_past_ttl():
    # exclusivity must survive past the original TTL while control is held: a rival
    # controller stays refused on both pads long after an unrenewed lease would have lapsed.
    port = _start_server()
    coord, rx, tx = _tcp_coordinator(port, lease_ttl_s=0.5)
    coord.take_control()
    rival = drivers.NetworkTransport("127.0.0.1", port, 18)
    try:
        time.sleep(1.3)                                      # > 2x TTL: unrenewed leases lapse
        with pytest.raises(IOError):
            rival.lease(scope="device", ttl_s=30)            # still held -> refused
        with pytest.raises(IOError):
            rival.query("ID?")                               # and the bus stays locked
    finally:
        coord.release_control()
    assert "scope" in rival.lease(scope="device", ttl_s=30)  # released -> rival may control
    rival.close()
    rx.close()
    tx.close()


def test_release_control_stops_keepalive_no_lease_stealback():
    # after release_control the keepalive is fully stopped: the next controller takes the
    # lease and KEEPS it (a lingering renewer re-acquiring would steal control back).
    port = _start_server()
    coord, rx, tx = _tcp_coordinator(port, lease_ttl_s=0.5)
    coord.take_control()
    coord.release_control()
    rival = drivers.NetworkTransport("127.0.0.1", port, 18)
    rival.lease(scope="device", ttl_s=30)
    time.sleep(0.6)                                          # > the keepalive interval
    assert "8565" in rival.query("ID?")                      # rival is still the controller
    assert rival.lease_report().count("scope") == 1          # exactly one live lease: the rival's
    rival.close()
    rx.close()
    tx.close()


def test_transport_serializes_concurrent_renew_and_query():
    # the keepalive renews on the SAME socket the campaign transacts on: per-transport
    # serialization is required or the request/reply framing desyncs across threads.
    port = _start_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    t.lease(scope="device", ttl_s=30)
    errors = []

    def hammer_query():
        try:
            for _ in range(200):
                assert "8565" in t.query("ID?")
        except Exception as e:                               # noqa: BLE001 -- collected for assert
            errors.append(e)

    def hammer_renew():
        try:
            for _ in range(200):
                t.renew_lease(30)
        except Exception as e:                               # noqa: BLE001 -- collected for assert
            errors.append(e)

    th_q, th_r = threading.Thread(target=hammer_query), threading.Thread(target=hammer_renew)
    th_q.start()
    th_r.start()
    th_q.join()
    th_r.join()
    assert errors == []
    t.close()


# ==================================================== Increment 5: UDP discovery beacon
# Role instances find bridges on the network with no hand-configured address: a bridge runs a
# Beacon; discover() broadcasts a probe and collects the replies. Loopback + ephemeral ports
# keep it xdist-safe (no fixed well-known port to collide on).

def _fake_beacon_info(host="127.0.0.1", port=5555):
    return discovery.BeaconInfo(
        host=host, port=port,
        instruments=({"pad": 5, "model": "Anritsu 68369A/NV", "kind": "tx"},
                     {"pad": 18, "model": "HP8565EC", "kind": "rx"}))


def test_beacon_encode_decode_roundtrip():
    info = _fake_beacon_info("10.0.0.5", 5555)
    back = discovery.decode_beacon(discovery.encode_beacon(info))
    assert back.host == "10.0.0.5" and back.port == 5555
    assert back.instruments[0]["pad"] == 5 and back.instruments[1]["kind"] == "rx"


def test_decode_rejects_foreign_udp():
    assert discovery.decode_beacon(b"random junk") is None                 # not JSON
    assert discovery.decode_beacon(json.dumps({"service": "other"}).encode()) is None  # wrong tag


def test_discover_finds_a_local_beacon_over_loopback():
    beacon = discovery.Beacon(_fake_beacon_info(), host="127.0.0.1", port=0).start()
    try:
        found = discovery.discover(port=beacon.port, timeout_s=1.0, broadcast_host="127.0.0.1")
    finally:
        beacon.stop()
    assert len(found) == 1
    assert found[0].host == "127.0.0.1" and found[0].port == 5555
    kinds = {i["kind"] for i in found[0].instruments}
    assert kinds == {"rx", "tx"}                              # both roles advertised


def test_discover_returns_empty_when_no_beacon():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    assert discovery.discover(port=free_port, timeout_s=0.3, broadcast_host="127.0.0.1") == []


# =================================================== Increment 6: control plane (R9)
# Discover, type-by-capability, resolve ANY registered TX/RX. The Coordinator and GUIs speak
# only the abstract rx/tx contract, so a new instrument model plugs in via the registry.

@pytest.fixture
def registry_guard():
    saved = list(control_plane._REGISTRY)
    yield
    control_plane._REGISTRY[:] = saved                       # restore -> no cross-test leakage


def test_registry_resolves_rx_and_tx_by_model():
    assert control_plane.resolve_driver("HP8565EC").kind == "rx"
    assert control_plane.resolve_driver("Anritsu 68369A/NV").kind == "tx"
    assert control_plane.resolve_driver("Fluke 9999") is None    # unknown -> honest None


def test_registering_a_new_model_plugs_in_without_touching_the_coordinator(registry_guard):
    class R3131(drivers.SpectrumAnalyzer):                   # a different analyzer model
        pass
    control_plane.register_driver("R3131", "rx", R3131, 9e3, 3.6e9, "Advantest R3131")
    spec = control_plane.resolve_driver("ADVANTEST R3131A")
    assert spec is not None and spec.kind == "rx" and spec.driver is R3131


def test_control_plane_roster_and_capability_resolve_from_sim():
    cp = control_plane.simulated(cfg_mod.default())
    kinds = {u["kind"] for u in cp.roster()}
    assert kinds == {"rx", "tx"}
    assert len(cp.available_rx()) == 1 and len(cp.available_tx()) == 1
    rx, tx = cp.resolve(kind="rx"), cp.resolve(kind="tx")
    assert rx.ensure() and tx.ensure()
    assert "8565" in rx.status().model and "68369" in tx.status().model
    # capabilities are role-derived (abstract contract), not model-specific
    assert "sweep" in cp.available_rx()[0].capabilities
    assert "cw" in cp.available_tx()[0].capabilities


def test_control_plane_make_coordinator_runs_a_campaign():
    cp = control_plane.simulated(cfg_mod.default())
    coord = cp.make_coordinator()
    result = coord.run_campaign(bench=cp.bench)
    assert "campaign_pass" in result["summary"]
    assert result["se_figure"]["points"] == len(cp.cfg.frequencies())


def test_control_plane_make_coordinator_raises_without_a_pair():
    cp = control_plane.ControlPlane(cfg_mod.default())       # empty roster
    with pytest.raises(control_plane.ControlPlaneError):
        cp.make_coordinator()


def test_control_plane_from_beacons_skips_unknown_models():
    beacon = discovery.BeaconInfo(host="127.0.0.1", port=1234,
                                  instruments=({"pad": 9, "model": "Unknownotron 5", "kind": "rx"},))
    cp = control_plane.from_beacons(cfg_mod.default(), [beacon])
    assert cp.roster() == []                                 # unregistered model -> no unit


def test_control_plane_from_beacons_builds_networked_pair_over_tcp():
    port = _start_server()
    beacon = _fake_beacon_info(host="127.0.0.1", port=port)
    cp = control_plane.from_beacons(cfg_mod.default(), [beacon])
    assert len(cp.available_rx()) == 1 and len(cp.available_tx()) == 1
    coord = cp.make_coordinator()
    assert coord.ensure_ready() is True                      # opens real drivers over the bridge
    coord.take_control()                                     # leases both pads
    obs = drivers.NetworkTransport("127.0.0.1", port, 18)
    with pytest.raises(IOError):
        obs.query("ID?")                                     # analyzer pad locked by the plane
    coord.release_control()
    assert "8565" in obs.query("ID?")
    obs.close()
    cp.resolve(kind="rx").close()
    cp.resolve(kind="tx").close()


# ============================================= Increment 7: role instances + pub/sub (R8)
# Multiple instances, one role each: a CoordinatorRole owns the bus and publishes the live SE
# figure; DashboardRole observers subscribe and render without touching the hardware.

def test_telemetry_pubsub_delivers_a_message():
    hub = telemetry.TelemetryHub().start()
    try:
        sub = telemetry.TelemetrySubscriber("127.0.0.1", hub.port)
        assert hub.wait_subscribers(1)
        hub.publish("se", {"se_db": 82.5, "lower_bound": False})
        msg = sub.recv_one(1.0)
        assert msg["topic"] == "se" and msg["data"]["se_db"] == 82.5
        sub.close()
    finally:
        hub.stop()


def test_telemetry_fans_out_to_multiple_subscribers():
    hub = telemetry.TelemetryHub().start()
    try:
        subs = [telemetry.TelemetrySubscriber("127.0.0.1", hub.port) for _ in range(3)]
        assert hub.wait_subscribers(3)
        hub.publish("roster", [{"kind": "rx"}])
        for s in subs:
            m = s.recv_one(1.0)
            assert m["topic"] == "roster" and m["data"][0]["kind"] == "rx"
            s.close()
    finally:
        hub.stop()


def test_coordinator_role_threads_telemetry_bind_host_to_hub():
    # R8/R9: a dashboard on ANOTHER host must be able to subscribe. If the CoordinatorRole always
    # binds the telemetry listener to loopback, that observer half is broken. A caller-supplied
    # bind host must reach the TelemetryHub's actual bind, not silently drop to 127.0.0.1.
    cp = control_plane.simulated(cfg_mod.default())
    role = roles.CoordinatorRole(cp, telemetry_host="0.0.0.0")
    try:
        assert role.telemetry_host == "0.0.0.0"              # role reports the real bind interface
        assert role.hub.host == "0.0.0.0"                    # and it actually reached the hub socket
    finally:
        role.stop()
    # default stays loopback (no behavior change unless asked)
    role2 = roles.CoordinatorRole(control_plane.simulated(cfg_mod.default()))
    try:
        assert role2.telemetry_host == "127.0.0.1"
    finally:
        role2.stop()


def test_coordinator_publishes_live_se_to_a_dashboard_observer():
    # the capstone: two instances operate concurrently -- a CoordinatorRole runs the campaign
    # and a DashboardRole observer KNOWS the SE figure during operation (R8), over telemetry,
    # without ever touching the bus.
    cp = control_plane.simulated(cfg_mod.default())
    role = roles.CoordinatorRole(cp).start()
    try:
        dash = roles.DashboardRole().connect("127.0.0.1", role.telemetry_port)
        assert role.hub.wait_subscribers(1)
        result = role.run_campaign()
        n = result["se_figure"]["points"]
        deadline = time.monotonic() + 3.0                    # let the async reader drain
        while time.monotonic() < deadline and (dash.summary is None or len(dash.se_history) < n):
            time.sleep(0.02)
        dash.close()
    finally:
        role.stop()
    assert dash.roster and {u["kind"] for u in dash.roster} == {"rx", "tx"}
    assert dash.latest_se["se_db"] == pytest.approx(result["se_figure"]["se_db"])
    assert dash.summary is not None and "campaign_pass" in dash.summary
    hist = [h for h in dash.se_history if h is not None]
    assert hist == sorted(hist, reverse=True)                # running worst-case, monotone down
    assert "SE" in dash.se_text()


def test_control_plane_from_addresses_over_tcp():
    # explicit net: addresses (no discovery beacon) -> a working coordinator that leases both
    port = _start_server()
    cp = control_plane.from_addresses(
        cfg_mod.default(), rx_addr=f"net:127.0.0.1:{port}:18", tx_addr=f"net:127.0.0.1:{port}:5")
    coord = cp.make_coordinator()
    assert coord.ensure_ready() is True
    coord.take_control()
    obs = drivers.NetworkTransport("127.0.0.1", port, 18)
    with pytest.raises(IOError):
        obs.query("ID?")
    coord.release_control()
    obs.close()
    cp.resolve(kind="rx").close()
    cp.resolve(kind="tx").close()


def test_cli_coordinator_runs_a_sim_campaign(capsys):
    import cli
    rc = cli.main(["coordinator", "--source", "sim", "--analyzer", "sim",
                   "--telemetry-port", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CAMPAIGN PASS" in out and "roster" in out and "live worst SE" in out


def test_cli_coordinator_vm_wires_both_units_from_two_bridges(capsys):
    # the qemu goal, hardware-free: TWO fake bridges (one per NI board behind the VM) stand in
    # for the two boards -- the 8565EC on the analyzer port (pad 18), the 68369A on the source
    # port (pad 5). ensure_bridge(require_both=True) finds BOTH units up and wires both net:
    # addresses -- no qemu, no fake in the runtime path.
    import cli
    aport, sport = _start_server(), _start_server()
    rc = cli.main(["coordinator", "--vm", "--vm-mode", "both", "--vm-port", str(aport),
                   "--vm-source-port", str(sport), "--telemetry-port", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bridge already up" in out                        # ensure_bridge saw BOTH units answer
    assert "8565EC" in out and "68369A" in out               # roster: both units
    assert f"net:127.0.0.1:{aport}:18" in out and f"net:127.0.0.1:{sport}:5" in out
    assert "CAMPAIGN PASS" in out


def _start_server_on(port):
    srv = ni_gpib_server.listen("127.0.0.1", port)
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "signal": "flat"}, daemon=True).start()


def _two_consecutive_free_ports():
    import socket as _s
    for _ in range(50):
        a = _s.socket(); a.bind(("127.0.0.1", 0)); p = a.getsockname()[1]; a.close()
        b = _s.socket()
        try:
            b.bind(("127.0.0.1", p + 1)); b.close(); return p
        except OSError:
            b.close()
    raise RuntimeError("no consecutive free ports")


def test_cli_coordinator_vm_golden_two_instances(capsys):
    # the GOLDEN, hardware-free: TWO separate instances (two fakes stand in for two qemus, one
    # per instrument) on consecutive ports -- analyzer VM (pad 18) on base_port, source VM (pad
    # 5) on base_port+1. --vm-mode golden ensures BOTH instances, then runs the campaign over
    # the two networked bridges. 1 instance == 1 qemu.
    import cli
    base = _two_consecutive_free_ports()
    _start_server_on(base)                                   # analyzer VM stand-in (pad 18)
    _start_server_on(base + 1)                               # source VM stand-in (pad 5)
    rc = cli.main(["coordinator", "--vm", "--vm-mode", "golden", "--vm-port", str(base),
                   "--telemetry-port", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "golden pair" in out                              # two-instance path taken
    assert f"net:127.0.0.1:{base}:18" in out                 # analyzer VM (RX)
    assert f"net:127.0.0.1:{base + 1}:5" in out              # source VM (TX)
    assert "CAMPAIGN PASS" in out


def test_cli_coordinator_vm_singleton_reuses_running_bridge(capsys):
    # SINGLETON (the single-machine DEFAULT), hardware-free: ONE VM serving BOTH boards. Two fakes
    # (one per board behind the singleton VM) stand in on the analyzer + source ports. ensure_singleton
    # sees BOTH answer -> REUSE (no boot, no attach) and wires both net: addresses.
    import cli
    aport, sport = _start_server(), _start_server()
    rc = cli.main(["coordinator", "--vm", "--vm-mode", "singleton", "--vm-port", str(aport),
                   "--vm-source-port", str(sport), "--telemetry-port", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "singleton" in out and "already up" in out        # REUSE path (both boards answer)
    assert f"net:127.0.0.1:{aport}:18" in out and f"net:127.0.0.1:{sport}:5" in out
    assert "CAMPAIGN PASS" in out


def test_cli_dashboard_reports_no_coordinator(capsys):
    import cli
    # nothing serving on this ephemeral port -> honest failure, not a hang
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    rc = cli.main(["dashboard", "--telemetry", f"127.0.0.1:{free_port}"])
    assert rc == 1
    assert "cannot reach coordinator telemetry" in capsys.readouterr().out


# ======================================= Increment 8: shield-insertion hook (C4)
# The IEEE-299 substitution method requires a PHYSICAL SHIELD inserted between the reference
# pass and the wall pass -- otherwise "wall" is measured over the same open geometry as
# "reference". run_campaign's optional on_shield_prompt() fires exactly once, strictly between
# the last reference point and the first wall point, INSIDE the exclusive-control hold so a
# raising callback still releases control cleanly.

def test_shield_prompt_fires_once_strictly_between_reference_and_wall_passes():
    cp = control_plane.simulated(cfg_mod.default())
    coord = cp.make_coordinator()
    log = []                                                  # one shared ordered log proves it
    result = coord.run_campaign(
        bench=cp.bench,
        on_reference_point=lambda i, row: log.append(("ref", i)),
        on_se_update=lambda fig, row: log.append(("wall", row["f_hz"])),
        on_shield_prompt=lambda: log.append(("shield",)))
    shield_idxs = [i for i, e in enumerate(log) if e == ("shield",)]
    assert len(shield_idxs) == 1                              # fires EXACTLY once
    ref_idxs = [i for i, e in enumerate(log) if e[0] == "ref"]
    wall_idxs = [i for i, e in enumerate(log) if e[0] == "wall"]
    assert ref_idxs and wall_idxs                              # both passes actually ran
    assert max(ref_idxs) < shield_idxs[0] < min(wall_idxs)     # after last ref, before first wall
    assert "campaign_pass" in result["summary"]


def test_shield_prompt_omitted_preserves_prior_no_shield_behavior():
    # default None -> the campaign runs exactly as before this context (back-compat)
    cp = control_plane.simulated(cfg_mod.default())
    coord = cp.make_coordinator()
    result = coord.run_campaign(bench=cp.bench)
    assert "campaign_pass" in result["summary"]
    assert result["se_figure"]["points"] == len(coord.cfg.frequencies())


def test_shield_prompt_raise_aborts_campaign_and_still_releases_control():
    cp = control_plane.simulated(cfg_mod.default())
    coord = cp.make_coordinator()

    def boom():
        raise RuntimeError("insert the shield")

    with pytest.raises(RuntimeError, match="insert the shield"):
        coord.run_campaign(bench=cp.bench, on_shield_prompt=boom)
    # the raise happened INSIDE the take_control/finally -- release_control still ran, so
    # the lease is free and a subsequent take (a later campaign, or a rival) succeeds.
    coord.take_control()
    coord.release_control()
