"""Headless (offscreen) tests for SpectrumAnalyzerPanel: controls -> model/engine, and render()
-> pyqtgraph curve state. Skips without the se299-gui group.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui \
        python -m pytest rf-se/se299/tests/test_sa_panel.py -q
"""
from __future__ import annotations

import os
import queue
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sa_gui


class _FakeAnalyzer:
    t = None

    def set_timeout(self, ms): pass

    def set_frequency(self, center_hz=None, span_hz=None): pass

    def configure(self, *a): pass

    def set_sweep_time(self, seconds=None, auto=False): pass

    def set_max_hold(self, on, trace="A"): pass

    def arm_and_wait(self, timeout_s=10.0, fresh=True): pass

    def read_trace(self, trace="A", calibrate=False): return ([2.4e9, 2.45e9, 2.5e9], [-90.0, -40.0, -88.0])

    def marker_peak(self): return (2.45e9, -40.0)


class _FakeHub:
    analyzer = _FakeAnalyzer()

    def acquire(self, instrument, engine): return (True, None)

    def release(self, instrument, engine): pass


def _panel():
    pytest.importorskip("PySide6")
    an = _FakeAnalyzer()
    hub = _FakeHub(); hub.analyzer = an
    return sa_gui.build_sa_panel(hub)


def test_panel_renders_trace_from_engine():
    p = _panel()
    p.engine.enqueue(("apply_settings", p.model.settings))
    p.engine.step_once()          # produce a trace onto the queue
    p._drain()                    # queue -> model
    live, mh, mk = p.render()
    xs, ys = live.getData()
    assert [round(float(x), 3) for x in xs] == [2.4, 2.45, 2.5]
    assert [float(y) for y in ys] == [-90.0, -40.0, -88.0]


def test_center_control_updates_settings_and_enqueues():
    p = _panel()
    p.spin_center.setValue(5.0)   # GHz
    assert p.model.settings.center_hz == 5.0e9
    # Verify apply_settings was enqueued
    found_apply = False
    while not p.engine._cmds.empty():
        kind, payload = p.engine._cmds.get_nowait()
        if kind == "apply_settings":
            found_apply = True
    assert found_apply, "apply_settings not enqueued after center change"


def test_maxhold_toggle_flows_to_model():
    p = _panel()
    p.chk_maxhold.setChecked(True)
    assert p.model.settings.max_hold is True
    # Verify apply_settings was enqueued
    found_apply = False
    while not p.engine._cmds.empty():
        kind, payload = p.engine._cmds.get_nowait()
        if kind == "apply_settings":
            found_apply = True
    assert found_apply, "apply_settings not enqueued after maxhold toggle"


def test_absent_render_does_not_raise():
    p = _panel()
    p.model.set_absent(True)
    p.render()
    assert "ABSENT" in p.readout.text().upper()
