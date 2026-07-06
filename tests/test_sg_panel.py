"""Headless tests for SignalGeneratorPanel: RF default off, controls -> model/engine, render.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui \
        python -m pytest rf-se/se299/tests/test_sg_panel.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sg_gui


class _FakeSource:
    def set_freq(self, hz): pass

    def set_power(self, dbm): pass

    def rf_on(self): pass

    def rf_off(self): pass

    def await_settled(self, settle_s=0.05, use_opc=True): pass

    def settled_ok(self): return True


class _FakeHub:
    def __init__(self): self._s = _FakeSource()

    @property
    def source(self): return self._s

    def acquire(self, instrument, engine): return (True, None)

    def release(self, instrument, engine): pass


def _panel():
    pytest.importorskip("PySide6")
    return sg_gui.build_sg_panel(_FakeHub())


def test_rf_checkbox_defaults_off():
    p = _panel()
    assert p.chk_rf.isChecked() is False and p.model.rf_on is False


def test_freq_power_controls_flow_to_model():
    p = _panel()
    p.spin_freq.setValue(5.0)     # GHz
    p.spin_power.setValue(-5.0)
    assert p.model.freq_hz == 5.0e9 and p.model.power_dbm == -5.0


def test_render_shows_rf_state():
    p = _panel()
    p.chk_rf.setChecked(True)
    p._drain(); p.render()
    assert "RF ON" in p.readout.text() or "RF on" in p.readout.text().replace("off", "")


def test_stop_forces_rf_off(monkeypatch):
    p = _panel()
    called = {"n": 0}
    monkeypatch.setattr(p.engine, "rf_off_safe", lambda: called.__setitem__("n", 1))
    p.stop()
    assert called["n"] == 1


def test_tx_freq_spin_arrow_steps_are_frequency_appropriate():
    # the TX frequency spinner steps by the freqstep ladder (Up/Down = a fraction of the current
    # frequency), not a fixed step -- so one press is sensible across DC-40 GHz.
    import qt_common
    p = _panel()
    assert isinstance(p.spin_freq, qt_common.FreqStepSpinBox)
    p.spin_freq.setValue(5.0)            # 5 GHz
    p.spin_freq.stepBy(1)                # one Up press (no Shift -> coarse)
    assert abs(p.spin_freq.value() - 5.1) < 1e-9      # 100 MHz step at 5 GHz
    p.spin_freq.stepBy(-1)
    assert abs(p.spin_freq.value() - 5.0) < 1e-9
    assert p.model.freq_hz == 5.0e9      # and it flows through to the model


def test_tx_freq_spin_step_adapts_across_a_decade():
    p = _panel()
    p.spin_freq.setValue(10.0)           # 10 GHz -> coarse step is now 1 GHz
    p.spin_freq.stepBy(1)
    assert abs(p.spin_freq.value() - 11.0) < 1e-9
