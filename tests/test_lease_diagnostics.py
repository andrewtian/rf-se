"""Lease/lock acquisition attribution (task #43): classify WHY an acquire failed and WHO holds the
device -- bridge-down vs adapter-wedged vs leased-by-<holder> vs board-busy. Pure + a tiny TCP probe;
fully hardware-free (the probes are injected).

Run:  uv run python -m pytest rf-se/se299/tests/test_lease_diagnostics.py -q
"""
from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lease_diagnostics as ld


# ---- classify (pure decision table) ---------------------------------------------------------

def test_classify_bridge_down_when_tcp_refused():
    d = ld.classify("RX", tcp_reachable=False, bridge_responsive=None)
    assert d.reason == ld.BRIDGE_DOWN and "start" in d.action.lower() and d.holder == ""


def test_classify_adapter_wedged_when_bridge_unresponsive():
    d = ld.classify("RX", tcp_reachable=True, bridge_responsive=False)
    assert d.reason == ld.ADAPTER_WEDGED and "replug" in d.action.lower()


def test_classify_adapter_wedged_when_txn_never_completed():
    # TCP open but the handshake/transaction never finished (construction timed out) -> board not answering
    d = ld.classify("RX", tcp_reachable=True, bridge_responsive=None)
    assert d.reason == ld.ADAPTER_WEDGED


def test_classify_leased_names_the_holder():
    d = ld.classify("RX", tcp_reachable=True, bridge_responsive=True, holder="session 3 pad 18")
    assert d.reason == ld.LEASED and d.holder == "session 3 pad 18" and "wait" in d.action.lower()


def test_classify_board_busy_when_up_and_unleased_but_no_answer():
    d = ld.classify("RX", tcp_reachable=True, bridge_responsive=True, holder="")
    assert d.reason == ld.BOARD_BUSY


def test_str_is_human_readable_and_shows_holder():
    d = ld.classify("8565EC (RX)", tcp_reachable=True, bridge_responsive=True, holder="session 9")
    s = str(d)
    assert "LEASED" in s and "session 9" in s and "->" in s


# ---- tcp_open (real sockets) ----------------------------------------------------------------

def test_tcp_open_true_on_listening_false_on_closed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    assert ld.tcp_open(host, port, 1.0) is True
    srv.close()
    assert ld.tcp_open("127.0.0.1", port, 0.5) is False    # nothing listening now -> refused


# ---- diagnose (composes the probes) ---------------------------------------------------------

def test_diagnose_bridge_down_when_tcp_refused():
    d = ld.diagnose("h", 1, "RX", probe_tcp=lambda h, p: False)
    assert d.reason == ld.BRIDGE_DOWN


def test_diagnose_adapter_wedged_when_lease_report_raises():
    def boom():
        raise TimeoutError("timed out")
    d = ld.diagnose("h", 1, "RX", lease_report_fn=boom, probe_tcp=lambda h, p: True)
    assert d.reason == ld.ADAPTER_WEDGED           # TCP open but the bridge txn hung -> wedged


def test_diagnose_leased_uses_lease_table_as_holder():
    d = ld.diagnose("h", 1, "RX", lease_report_fn=lambda: "pad 18 -> session 7 scope device",
                    probe_tcp=lambda h, p: True)
    assert d.reason == ld.LEASED and "session 7" in d.holder


def test_diagnose_board_busy_when_empty_lease_table():
    d = ld.diagnose("h", 1, "RX", lease_report_fn=lambda: "(empty lease table)",
                    probe_tcp=lambda h, p: True)
    assert d.reason == ld.BOARD_BUSY               # up + responsive + nobody holds it


def test_diagnose_adapter_wedged_when_tcp_open_but_no_report_probe():
    d = ld.diagnose("h", 1, "RX", lease_report_fn=None, probe_tcp=lambda h, p: True)
    assert d.reason == ld.ADAPTER_WEDGED           # construction-failure path (no lease probe available)


# ---- integration: lease_exclusive carries the classified attribution ------------------------

def test_lease_exclusive_conflict_carries_classified_attribution():
    import pytest
    import drivers
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()

    class _RealishT:
        def __init__(self):
            self.host, self.port = host, port          # real host/port -> diagnose runs (TCP reachable)

        def lease(self, scope="device", ttl_s=30.0):
            raise IOError("pad 18 leased by another controller")

        def lease_report(self):
            return "pad 18 -> session 7 u=other"

    try:
        with pytest.raises(drivers.SingleConsumerConflict) as ei:
            drivers.lease_exclusive(_RealishT(), "8565EC analyzer (RX)", ttl_s=100)
        # the operator now gets WHY (leased) + WHO (session 7) + the raw table -- not a bare conflict
        assert "LEASED" in ei.value.report and "session 7" in ei.value.report and "u=other" in ei.value.report
    finally:
        srv.close()
