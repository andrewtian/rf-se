"""Pure tests for SpectrumModel: trace accumulation, max-hold, video-average, marker, absent,
preselector applicability. No Qt.

Run:  uv run python -m pytest rf-se/se299/tests/test_sa_model.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sa_gui


def _model():
    m = sa_gui.SpectrumModel()
    m.settings.center_hz = 2.45e9
    m.settings.span_hz = 100e6
    return m


def test_set_trace_updates_live_curve():
    m = _model()
    m.set_trace([2.4e9, 2.45e9, 2.5e9], [-90.0, -40.0, -88.0])
    fg, live, mh = m.curve()
    assert fg == [2.4, 2.45, 2.5] and live == [-90.0, -40.0, -88.0]


def test_max_hold_holds_peak_across_sweeps():
    m = _model()
    m.settings.max_hold = True
    m.set_trace([2.4e9, 2.45e9], [-90.0, -40.0])
    m.set_trace([2.4e9, 2.45e9], [-50.0, -95.0])     # first bin rises, second falls
    _, _, mh = m.curve()
    assert mh == [-50.0, -40.0]                        # per-bin maximum retained


def test_video_average_runs_mean():
    m = _model()
    m.settings.video_avg = True
    m.set_trace([2.4e9], [-80.0])
    m.set_trace([2.4e9], [-60.0])
    _, live, _ = m.curve()
    assert live == [-70.0]                             # running mean of the two sweeps


def test_marker_and_readout():
    m = _model()
    m.set_trace([2.45e9], [-40.0])
    m.set_marker(2.45e9, -40.0)
    assert m.marker == (2.45e9, -40.0)
    assert "2.45" in m.readout_text() and "-40.0" in m.readout_text()


def test_absent_flag_and_text():
    m = _model()
    m.set_absent(True)
    assert m.absent is True and "ABSENT" in m.readout_text().upper()


def test_preselector_applicable_above_2_9_ghz():
    m = _model()
    m.settings.center_hz = 1e9
    assert m.preselector_applicable() is False
    m.settings.center_hz = 10e9
    assert m.preselector_applicable() is True


def test_max_hold_captures_raw_peak_even_with_video_avg():
    m = _model()
    m.settings.max_hold = True
    m.settings.video_avg = True
    m.set_trace([2.45e9], [-90.0])
    m.set_trace([2.45e9], [-40.0])     # raw peak -40; averaged live would be -65
    _, live, mh = m.curve()
    assert mh == [-40.0]                # max-hold holds the RAW peak
    assert live == [-65.0]             # live is the running average


def test_avg_count_zero_does_not_crash():
    m = _model()
    m.settings.video_avg = True
    m.settings.avg_count = 0
    m.set_trace([2.45e9], [-80.0])
    m.set_trace([2.45e9], [-60.0])     # must not raise ZeroDivisionError
    _, live, _ = m.curve()
    assert live == [-60.0]             # avg_count floored to 1 -> a = 1.0 -> new sweep replaces average


def test_readout_distinguishes_fault_from_absent():
    # P0-3: the link's actionable reason reaches the readout -- a wedged adapter reads as FAULT with
    # "power-cycle...", a plain not-on-bus reads as ABSENT. The two must be visibly distinct.
    m = _model()
    m.set_absent(True, "no matching-model device on the bus")
    assert "ABSENT" in m.readout_text() and "FAULT" not in m.readout_text()
    m.set_absent(True, "TX 68367C pad 5 not answering after 3 tries: power-cycle the tx adapter (NI GPIB-USB-HS)")
    t = m.readout_text()
    assert "FAULT" in t and "power-cycle the tx adapter" in t
