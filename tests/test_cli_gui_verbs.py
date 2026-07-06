"""The sa/sg/bench cli verbs construct (offscreen) without launching the event loop. We monkeypatch
run_live to a no-op so cmd_* returns without blocking, and assert the panels/window were built.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui \
        python -m pytest rf-se/se299/tests/test_cli_gui_verbs.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cli   # Qt-free at module top (GUI cmd_* import sa_gui/bench_gui/qt_common lazily). qt_common
            # is imported INSIDE each test (after importorskip) so this file collects with no Qt group.


class _A:
    analyzer = "sim"; source = "sim"; interval_ms = 50; vm = False; vm_plan = False


def test_cmd_bench_builds_and_returns(monkeypatch):
    pytest.importorskip("PySide6")
    import qt_common
    monkeypatch.setattr(qt_common, "run_live", lambda *a, **k: None)   # do not block on exec
    rc = cli.cmd_bench(_A())
    assert rc == 0


def test_cmd_sa_builds_and_returns(monkeypatch):
    pytest.importorskip("PySide6")
    import qt_common
    monkeypatch.setattr(qt_common, "run_live", lambda *a, **k: None)
    rc = cli.cmd_sa(_A())
    assert rc == 0


def test_cmd_sg_builds_and_returns(monkeypatch):
    pytest.importorskip("PySide6")
    import qt_common
    monkeypatch.setattr(qt_common, "run_live", lambda *a, **k: None)
    rc = cli.cmd_sg(_A())
    assert rc == 0


def test_cmd_sa_vm_plan_prints_and_returns(monkeypatch, capsys):
    """--vm-plan must short-circuit to the launch-plan printout (no bench/GUI build, no run_live
    needed) -- this is the Task 10 fix: sa/sg/bench used to silently ignore --vm-plan/--vm."""
    pytest.importorskip("PySide6")
    from gpib_bridge import vm as vmmod

    monkeypatch.setattr(vmmod, "launch_plan", lambda *a, **k: "PLAN")

    class A(_A):
        vm_plan = True
        vm_port = 5555
        vm_source_port = 5556

    rc = cli.cmd_sa(A())
    assert rc == 0
    assert "PLAN" in capsys.readouterr().out
