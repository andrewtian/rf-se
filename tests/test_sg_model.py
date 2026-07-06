"""Pure tests for SourceModel: state, RF-default-off, step-sweep points, absent. No Qt.

Run:  uv run python -m pytest rf-se/se299/tests/test_sg_model.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sg_gui


def test_rf_defaults_off():
    m = sg_gui.SourceModel()
    assert m.rf_on is False


def test_set_state_and_readout():
    m = sg_gui.SourceModel()
    m.set_state(freq_hz=2.45e9, power_dbm=-10.0, rf_on=True)
    assert m.freq_hz == 2.45e9 and m.power_dbm == -10.0 and m.rf_on is True
    assert "2.45" in m.readout_text() and "-10" in m.readout_text() and "ON" in m.readout_text()


def test_step_sweep_points_inclusive():
    m = sg_gui.SourceModel()
    m.sweep_start_hz, m.sweep_stop_hz, m.sweep_step_hz = 1e9, 1.3e9, 1e8
    pts = m.sweep_points()
    assert pts[0] == 1e9 and pts[-1] <= 1.3e9 + 1 and len(pts) == 4


def test_absent_text():
    m = sg_gui.SourceModel()
    m.set_absent(True)
    assert "ABSENT" in m.readout_text().upper()


def test_sweep_points_zero_step_returns_single_point():
    m = sg_gui.SourceModel()
    m.sweep_start_hz, m.sweep_stop_hz, m.sweep_step_hz = 1e9, 6e9, 0.0
    assert m.sweep_points() == [1e9]        # no hang / OOM on zero step


def test_sweep_points_capped_for_tiny_step():
    m = sg_gui.SourceModel()
    m.sweep_start_hz, m.sweep_stop_hz, m.sweep_step_hz = 0.0, 1e9, 1.0   # would be 1e9 points uncapped
    pts = m.sweep_points()
    assert len(pts) == sg_gui.MAX_SWEEP_POINTS       # capped, no OOM


def test_source_readout_distinguishes_fault_from_absent():
    import sg_gui
    m = sg_gui.SourceModel()
    m.set_absent(True, "no matching-model device on the bus")
    assert "ABSENT" in m.readout_text() and "FAULT" not in m.readout_text()
    m.set_absent(True, "power-cycle the tx adapter (NI GPIB-USB-HS)")
    assert "FAULT" in m.readout_text() and "power-cycle" in m.readout_text()
