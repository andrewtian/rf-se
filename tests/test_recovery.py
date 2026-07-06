"""Hardware-free tests for the session-level SOFT recovery orchestrator (#44.2/44.3).

soft_recover de-keys the source FIRST, then QMP virtual-replugs + revalidates up to a budget;
recover_power is the honest deferred VBUS seam. All I/O is injected, so the state machine is fully
testable; the LIVE proof (a real replug re-enumerates; an FX2 -110 is NOT cleared by a QMP reset) is
hardware-gated (44.4, run_live_replug_recovery.py).

Run:  uv run python -m pytest rf-se/se299/tests/test_recovery.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import recovery


def test_soft_recover_dekeys_before_any_replug():
    order = []
    out = recovery.soft_recover(
        dekey_fn=lambda: order.append("dekey"),
        replug_fn=lambda: order.append("replug"),
        reachable_fn=lambda: True,                        # answers after the first replug
        budget=3)
    assert out.recovered is True and out.attempts == 1
    assert order == ["dekey", "replug"]                   # de-key STRICTLY before the first replug


def test_soft_recover_returns_on_first_reachable():
    calls = {"replug": 0}
    reach = iter([False, False, True])                    # answers on the 3rd attempt
    out = recovery.soft_recover(
        dekey_fn=lambda: None,
        replug_fn=lambda: calls.__setitem__("replug", calls["replug"] + 1),
        reachable_fn=lambda: next(reach),
        budget=5)
    assert out.recovered is True and out.attempts == 3 and calls["replug"] == 3


def test_soft_recover_exhausts_budget_then_reports_hard():
    calls = {"replug": 0}
    out = recovery.soft_recover(
        dekey_fn=lambda: None,
        replug_fn=lambda: calls.__setitem__("replug", calls["replug"] + 1),
        reachable_fn=lambda: False,                       # never answers (a HARD FX2 -110 wedge)
        budget=2)
    assert out.recovered is False and out.attempts == 2 and calls["replug"] == 2


def test_soft_recover_dekeys_even_with_zero_budget():
    order = []
    out = recovery.soft_recover(
        dekey_fn=lambda: order.append("dekey"),
        replug_fn=lambda: order.append("replug"),
        reachable_fn=lambda: True,
        budget=0)
    assert out.recovered is False and out.attempts == 0
    assert order == ["dekey"]                             # safety de-key runs even when no replug is tried


def test_recover_power_is_honest_unsupported():
    out = recovery.recover_power("tx")
    assert out.recovered is False                         # NEVER claims success (no uhubctl hub)
    assert "unsupported" in out.detail and "tx" in out.detail
