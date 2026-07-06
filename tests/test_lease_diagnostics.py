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


# ---- classify_recovery (#44 pure recovery-tier decision table) -------------------------------

def _rv(**over):
    kw = dict(host_present=True, host_settled=True, boards_online=True, instrument_answering=True,
              grace_elapsed=True, qmp_attempts_spent=False, is_b_role=False, fxloaded=True,
              shared_controller=False)
    kw.update(over)
    return ld.classify_recovery("RX", **kw)


def test_classify_recovery_decision_table():
    cases = [
        # (overrides, expected_tier, expected_soft_recoverable)
        (dict(host_present=False), ld.BRIDGE_DOWN, False),
        (dict(host_settled=False), ld.SETTLING, False),
        (dict(is_b_role=True, fxloaded=False), ld.PRE_FIRMWARE, False),
        (dict(boards_online=False, instrument_answering=False), ld.BOOTING_GRACE, False),
        # R6 guard: boards not enumerated -> NOT a wedge EVEN past the attempt budget
        (dict(boards_online=False, instrument_answering=False, qmp_attempts_spent=True),
         ld.BOOTING_GRACE, False),
        (dict(instrument_answering=True), ld.ANSWERING, False),
        (dict(instrument_answering=False, grace_elapsed=False), ld.SETTLING, False),
        (dict(instrument_answering=False, grace_elapsed=True, qmp_attempts_spent=False),
         ld.INSTRUMENT_SILENT, True),
        (dict(instrument_answering=False, grace_elapsed=True, qmp_attempts_spent=True),
         ld.INSTRUMENT_SILENT, False),
    ]
    for over, tier, soft in cases:
        v = _rv(**over)
        assert v.tier == tier, (over, v.tier)
        assert v.soft_recoverable is soft, (over, v.soft_recoverable)


def test_classify_recovery_boot_grace_guards_r6_false_positive():
    # the reshaped R6 fix: boards not enumerated yet must NOT declare a wedge, even after the QMP budget.
    v = _rv(boards_online=False, instrument_answering=False, grace_elapsed=True, qmp_attempts_spent=True)
    assert v.tier == ld.BOOTING_GRACE and v.soft_recoverable is False


def test_classify_recovery_soft_then_hard_on_budget_exhaustion():
    soft = _rv(instrument_answering=False, grace_elapsed=True, qmp_attempts_spent=False)
    hard = _rv(instrument_answering=False, grace_elapsed=True, qmp_attempts_spent=True)
    assert soft.tier == ld.INSTRUMENT_SILENT and soft.soft_recoverable is True     # try QMP replug
    assert hard.tier == ld.INSTRUMENT_SILENT and hard.soft_recoverable is False    # HARD: physical replug
    assert "QMP" in soft.action and "physically" in hard.action.lower()


def test_classify_recovery_shared_controller_rides_and_renders():
    v = _rv(instrument_answering=False, grace_elapsed=True, qmp_attempts_spent=True, shared_controller=True)
    assert v.shared_controller is True
    assert "shared USB controller" in str(v)                # the orthogonal warning rides the verdict
