"""Unit tests for InstrumentHub with FAKE links (test doubles): lease-on-demand, two-phase
all-or-nothing acquire, arbiter handoff, and shutdown. No hardware, no Qt.

Run:  uv run python -m pytest rf-se/se299/tests/test_instrument_hub.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from instrument_hub import InstrumentHub


class _FakeTransport:
    def __init__(self, lease_ok=True, holder="session 9 scope 18 ttl 30.0s"):
        self.lease_ok, self.holder = lease_ok, holder
        self.leased = False
        self.released = 0

    def lease(self, scope="device", ttl_s=30.0):
        if not self.lease_ok:
            raise IOError(f"gpib bridge error: locked by {self.holder}")
        self.leased = True
        return "scope ok"

    def release_lease(self):
        self.released += 1
        self.leased = False

    def lease_report(self):
        return self.holder if not self.leased else "no active leases"

    def sessions_report(self):
        return "session 9 client coordinator|host=h|pid=1|u=aaaa peer 1:2 role coordinator pad 18 lease 18"


class _FakeLink:
    def __init__(self, transport):
        self.analyzer = type("D", (), {"t": transport})()

    def ensure(self):
        return True   # transport at link.analyzer.t


class _FakeCoord:
    def __init__(self, rx_t, tx_t):
        self.rx = _FakeLink(rx_t)
        self.tx = _FakeLink(tx_t)
        self.analyzer = "RX-DRIVER"
        self.source = "TX-DRIVER"

    def ensure_ready(self):
        return True


class _Eng:
    def __init__(self, name):
        self.name, self.suspended, self.resumed = name, 0, 0

    def suspend(self): self.suspended += 1

    def resume(self): self.resumed += 1


def _hub(rx_ok=True, tx_ok=True):
    rx_t, tx_t = _FakeTransport(rx_ok), _FakeTransport(tx_ok)
    return InstrumentHub(_FakeCoord(rx_t, tx_t)), rx_t, tx_t


def test_acquire_leases_on_demand_once():
    hub, rx_t, _ = _hub()
    sa = _Eng("sa")
    ok, who = hub.acquire("rx", sa)
    assert ok and who is None and rx_t.leased is True
    hub.acquire("rx", sa)                       # second acquire does not re-lease
    assert rx_t.leased is True


def test_acquire_external_conflict_reports_holder():
    hub, rx_t, _ = _hub(rx_ok=False)
    ok, who = hub.acquire("rx", _Eng("sa"))
    assert ok is False and "session 9" in who and rx_t.leased is False


def test_acquire_both_all_or_nothing_rolls_back():
    hub, rx_t, tx_t = _hub(rx_ok=True, tx_ok=False)
    ok, who = hub.acquire_both(_Eng("se"))
    assert ok is False                           # tx conflict -> rx must roll back
    assert rx_t.leased is False and rx_t.released == 1
    assert "session 9" in who


def test_acquire_both_success_and_handoff():
    hub, rx_t, tx_t = _hub()
    sa, sg, se = _Eng("sa"), _Eng("sg"), _Eng("se")
    hub.acquire("rx", sa)
    hub.acquire("tx", sg)
    ok, who = hub.acquire_both(se)               # SE preempts both
    assert ok and rx_t.leased and tx_t.leased
    assert sa.suspended == 1 and sg.suspended == 1
    hub.release("rx", se); hub.release("tx", se)
    assert sa.resumed == 1 and sg.resumed == 1


def test_acquire_ensure_failure_reports_absent_and_does_not_lease():
    rx_t, tx_t = _FakeTransport(), _FakeTransport()
    coord = _FakeCoord(rx_t, tx_t)
    coord.rx.ensure = lambda: False
    hub = InstrumentHub(coord)
    ok, who = hub.acquire("rx", _Eng("sa"))
    assert ok is False and "not ready" in who
    assert rx_t.leased is False


def test_shutdown_releases_all_leases():
    hub, rx_t, tx_t = _hub()
    hub.acquire("rx", _Eng("sa"))
    hub.acquire("tx", _Eng("sg"))
    hub.shutdown()
    assert rx_t.leased is False and tx_t.leased is False


def test_sessions_report_reads_over_transport():
    hub, _, _ = _hub()
    rep = hub.sessions_report("rx")
    assert "u=aaaa" in rep
