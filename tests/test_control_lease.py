"""Tests for ControlLease -- the single client-side lease+keepalive+release primitive (Phase 3).

Headline coverage: the silent 120 s lease-lapse REGRESSION (the hub now holds control past the
TTL because it renews), and TWO-OWNER PARITY (Coordinator and InstrumentHub emit IDENTICAL L/K/U
wire traffic). Plus the primitive's contract: renew at ttl/3 (driven directly -- no real sleep),
re-acquire after a simulated lapse, release stops+joins the keepalive BEFORE U, sim link = full
no-op with no thread, and (ok, who) on a conflict.

Hardware-free: recording transports + the fake ni_gpib_server for the live-bridge lapse test.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
        rf-se/se299/tests/test_control_lease.py -q
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import connection as conn
import control_lease
import coordinator
import discover as disc
import drivers
import instrument_hub
from gpib_bridge import ni_gpib_server


# ----------------------------------------------------------------- recording doubles

class _RecLease:
    """A transport double that records ONLY the arbitration verbs L / K / U (the lease wire
    traffic) -- write/query (e.g. an rf_off 'RF0') are NOT lease ops and never appear here."""

    def __init__(self, lease_ok=True, holder="session 7 scope 18 ttl 30.0s"):
        self.verbs = []
        self.lease_ok = lease_ok
        self.holder = holder
        self.leased = False

    def lease(self, scope="device", ttl_s=30.0):
        self.verbs.append("L")
        if not self.lease_ok:
            raise IOError(f"gpib bridge error: locked by {self.holder}")
        self.leased = True
        return "scope ok"

    def renew_lease(self, ttl_s=30.0):
        self.verbs.append("K")

    def release_lease(self):
        self.verbs.append("U")
        self.leased = False

    def lease_report(self):
        return self.holder if not self.leased else "no active leases"


class _RecDriver:
    def __init__(self, t):
        self.t = t

    def rf_off(self):
        pass                                    # a W over the bus, not an L/K/U lease op

    # a HEALTHY analyzer for the coordinator health gate (these tests exercise lease mechanics,
    # not the reference/LO wedge): no error codes, sweep live.
    def query_errors(self):
        return []

    def _sweep_is_live(self):
        return True


class _RecLink:
    def __init__(self, t):
        self.analyzer = _RecDriver(t)

    def ensure(self):
        return True

    def status(self):
        return type("S", (), {"state": "READY", "reason": "ok"})()


class _SimLink:
    """A link with a driver but NO transport (sim / local-VISA) -> ControlLease is a full no-op."""

    def __init__(self):
        self.analyzer = object()                # getattr(driver, "t", None) is None

    def ensure(self):
        return True


class _Eng:
    def __init__(self, name):
        self.name, self.suspended, self.resumed = name, 0, 0

    def suspend(self):
        self.suspended += 1

    def resume(self):
        self.resumed += 1


# ----------------------------------------------------------------- acquire / keepalive / release

def test_acquire_leases_starts_keepalive_and_reports_held():
    t = _RecLease()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=30.0)
    ok, who = lease.acquire()
    assert ok and who is None and lease.held() is True
    assert t.verbs == ["L"]                     # an L was sent
    assert lease._thread is not None and lease._thread.is_alive()   # keepalive running
    lease.release()
    assert lease._thread is None and lease.held() is False          # stopped + released


def test_acquire_is_idempotent():
    t = _RecLease()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=1e9)
    lease.acquire()
    lease.acquire()                             # a second acquire while held is a no-op
    assert t.verbs == ["L"]                     # NOT re-leased
    lease.release()


def test_renew_interval_is_ttl_over_three_and_renew_once_sends_K():
    # injected-clock semantics: drive the tick DIRECTLY (no real sleep). The renew cadence is ttl/3.
    t = _RecLease()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=120.0)
    assert lease._interval == pytest.approx(40.0)          # 120 / 3
    lease._renew_once()
    assert t.verbs == ["K"]                                # one tick -> one K (no L, no thread needed)


def test_renew_once_reacquires_after_a_simulated_lapse():
    # a lapsed lease: renew_lease raises -> the tick RE-ACQUIRES (L), never silently loses control.
    class _Lapsed(_RecLease):
        def renew_lease(self, ttl_s=30.0):
            self.verbs.append("K?")                        # attempted...
            raise IOError("gpib bridge error: no lease to renew")

    t = _Lapsed()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=30.0)
    lease._renew_once()
    assert t.verbs == ["K?", "L"]                          # renew failed -> re-acquired


def test_release_stops_and_joins_keepalive_before_U():
    class _RecLeaseEvt(_RecLease):
        def __init__(self):
            super().__init__()
            self.renewed = threading.Event()

        def renew_lease(self, ttl_s=30.0):
            super().renew_lease(ttl_s)
            self.renewed.set()

    t = _RecLeaseEvt()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=0.15)   # interval 0.05 -> renews quickly
    lease.acquire()
    assert t.renewed.wait(2.0)                             # at least one K fired
    lease.release()
    assert lease._thread is None                           # keepalive stopped + joined
    assert t.verbs[-1] == "U"                              # U is the LAST verb...
    assert "K" not in t.verbs[t.verbs.index("U") + 1:]     # ... no renew after the release


def test_renew_racing_release_does_not_resteal_the_lease():
    # THE steal regression: a keepalive tick is mid-renew when release() begins. Without the
    # _releasing guard, the failed renew's except-path would re-issue lease() (L) AFTER release
    # sent U -- re-grabbing the bus and blocking the next taker for the full TTL. The guard suppresses it.
    holder = {}

    class _RaceLapsed(_RecLease):
        def renew_lease(self, ttl_s=30.0):
            self.verbs.append("K?")                        # attempted...
            holder["lease"]._releasing = True              # ...release() has begun WHILE we renew
            raise IOError("gpib bridge error: no lease to renew")

    t = _RaceLapsed()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=30.0)
    holder["lease"] = lease
    lease.acquire()                                        # L (keepalive; ttl 30 -> no auto-fire)
    lease._renew_once()                                    # renew fails; release raced in mid-renew
    assert t.verbs == ["L", "K?"]                          # NO trailing "L": the guard blocked the re-steal
    lease.release()                                        # stops the keepalive cleanly


def test_release_is_idempotent():
    t = _RecLease()
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=1e9)
    lease.acquire()
    lease.release()
    lease.release()                                        # second release is a no-op
    assert t.verbs == ["L", "U"]


def test_sim_link_is_a_full_noop_no_thread():
    lease = control_lease.ControlLease(_SimLink(), ttl_s=30.0)
    ok, who = lease.acquire()
    assert ok and who is None and lease.held() is True     # control "held" (nothing to lease)
    assert lease._thread is None                           # NO keepalive thread on a sim link
    lease.release()                                        # no-op, must not raise


def test_acquire_conflict_returns_ok_false_and_who():
    t = _RecLease(lease_ok=False, holder="session 9 scope 18 ttl 30.0s")
    lease = control_lease.ControlLease(_RecLink(t), ttl_s=30.0)
    ok, who = lease.acquire()
    assert ok is False and "session 9" in who              # who holds it, from the lease table
    assert lease.held() is False and lease._thread is None  # not held, no keepalive


# ----------------------------------------------------------------- two-owner parity (L/K/U)

def _fake_coord(rx_t, tx_t):
    class _C:
        def __init__(self):
            self.rx = _RecLink(rx_t)
            self.tx = _RecLink(tx_t)
            self.analyzer = self.rx.analyzer
            self.source = self.tx.analyzer

        def ensure_ready(self):
            return True

    return _C()


def test_take_control_all_or_nothing_rolls_back_rx_and_raises_naming_tx_holder():
    # A rival holds the TX source. take_control acquires RX first (L), then hits the TX conflict; it
    # must RELEASE the RX lease it just took (L,U) and raise ControlConflict naming TX + the holder,
    # BEFORE any bus op -- never leaving RX stranded (mirrors instrument_hub.acquire_both rollback).
    rx_t = _RecLease()                                        # RX free
    tx_t = _RecLease(lease_ok=False, holder="session 9 scope 5 ttl 30.0s")  # TX held by a rival
    coord = coordinator.Coordinator(cfg_mod.default(), _RecLink(rx_t), _RecLink(tx_t),
                                    lease_ttl_s=1e9)
    with pytest.raises(coordinator.ControlConflict) as ei:
        coord.take_control()
    assert ei.value.instrument == "TX" and "session 9" in ei.value.who   # names TX + who
    assert coord._rx_lease.held() is False                   # RX rolled back -- NOT stranded
    assert rx_t.verbs == ["L", "U"]                          # RX was taken then released
    assert tx_t.verbs == ["L"]                               # TX was only attempted (conflict)


def test_coordinator_and_hub_emit_identical_LKU_wire_traffic():
    # the two control owners must speak the SAME arbitration language. ttl huge -> the keepalive
    # never auto-fires in the test window; one manual tick each makes the K deterministic.
    def owner_traffic(build):
        rx_t, tx_t = _RecLease(), _RecLease()
        build(rx_t, tx_t)
        return rx_t.verbs, tx_t.verbs

    def via_coordinator(rx_t, tx_t):
        coord = coordinator.Coordinator(cfg_mod.default(), _RecLink(rx_t), _RecLink(tx_t),
                                        lease_ttl_s=1e9)
        coord.take_control()                              # L, L
        coord._rx_lease._renew_once()                     # K
        coord._tx_lease._renew_once()                     # K
        coord.release_control()                           # rf_off (no L/K/U) then U, U

    def via_hub(rx_t, tx_t):
        hub = instrument_hub.InstrumentHub(_fake_coord(rx_t, tx_t), lease_ttl_s=1e9)
        eng = _Eng("sa")
        hub.acquire("rx", eng)                            # L
        hub.acquire("tx", eng)                            # L
        hub._lease["rx"]._renew_once()                    # K
        hub._lease["tx"]._renew_once()                    # K
        hub.shutdown()                                    # U, U

    coord_rx, coord_tx = owner_traffic(via_coordinator)
    hub_rx, hub_tx = owner_traffic(via_hub)
    assert coord_rx == hub_rx == ["L", "K", "U"]
    assert coord_tx == hub_tx == ["L", "K", "U"]


# ----------------------------------------------------------------- LAPSE REGRESSION (live bridge)

def _start_server():
    srv = ni_gpib_server.listen("127.0.0.1", 0)
    port = srv.getsockname()[1]
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True}, daemon=True).start()
    return port


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


def test_hub_holds_control_past_ttl_the_lapse_regression():
    # THE headline regression: the InstrumentHub must keep control PAST the lease TTL because its
    # ControlLease renews (the OLD hub leased once and never renewed -> silent 120 s lapse). A rival
    # stays refused long after an unrenewed 0.5 s lease would have lapsed.
    port = _start_server()
    ni_gpib_server._LEASES.reset()
    try:
        coord, rx, tx = _tcp_coordinator(port, lease_ttl_s=0.5)
        hub = instrument_hub.InstrumentHub(coord, lease_ttl_s=0.5)
        assert hub.acquire("rx", _Eng("sa")) == (True, None)
        rival = drivers.NetworkTransport("127.0.0.1", port, 18)
        try:
            time.sleep(1.3)                                # > 2x TTL: an unrenewed lease would lapse
            with pytest.raises(IOError):
                rival.lease(scope="device", ttl_s=30)      # still held -> renew fired
        finally:
            hub.shutdown()
        assert "scope" in rival.lease(scope="device", ttl_s=30)   # released -> rival may control now
        rival.close()
        rx.close()
        tx.close()
    finally:
        ni_gpib_server._LEASES.reset()


def test_hub_shutdown_releases_all_over_the_bridge():
    port = _start_server()
    ni_gpib_server._LEASES.reset()
    try:
        coord, rx, tx = _tcp_coordinator(port, lease_ttl_s=30.0)
        hub = instrument_hub.InstrumentHub(coord, lease_ttl_s=30.0)
        hub.acquire("rx", _Eng("sa"))
        hub.acquire("tx", _Eng("sg"))
        hub.shutdown()
        rival = drivers.NetworkTransport("127.0.0.1", port, 18)
        assert "scope" in rival.lease(scope="device", ttl_s=30)   # both leases were released
        rival.close()
        rx.close()
        tx.close()
    finally:
        ni_gpib_server._LEASES.reset()
