"""Range bench mode (range_mode) + the modular tabbed bench host. RangeModel is Qt-free; the panel
+ modular-host tests are Qt-gated (se299-gui group, offscreen).

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui \
        python -m pytest rf-se/se299/tests/test_range_mode.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import range_mode


# ---- pure model (no Qt) --------------------------------------------------------------------

def test_range_model_span_and_paint_points_cover_the_range():
    m = range_mode.RangeModel()
    m.center_hz, m.range_hz, m.points = 5e9, 500e6, 5
    assert m.span_lo_hz() == 5e9 - 250e6
    assert m.span_hi_hz() == 5e9 + 250e6
    pts = m.paint_points()
    assert len(pts) == 5 and pts[0] == m.span_lo_hz() and pts[-1] == m.span_hi_hz()
    assert pts == sorted(pts)                              # low -> high across the range


def test_range_model_clamps_span_to_the_source_band():
    m = range_mode.RangeModel()
    m.center_hz, m.range_hz = 20e6, 200e6                  # would run below the 10 MHz floor
    assert m.span_lo_hz() == range_mode.FLOOR_MIN_HZ


def test_range_model_readout_shows_center_and_range():
    m = range_mode.RangeModel()
    m.center_hz, m.range_hz = 5e9, 500e6
    t = m.readout_text()
    assert "5.0000 GHz" in t and "500.0 MHz" in t and "idle" in t


# ---- panel + modular host (Qt) -------------------------------------------------------------

def _bench():
    pytest.importorskip("PySide6")
    import bench_gui
    return bench_gui.build_bench("sim", "sim")


def test_bench_hosts_full_and_range_modes_as_tabs():
    b = _bench()
    names = [b.tabs.tabText(i) for i in range(b.tabs.count())]
    assert any("Full Bench" in n for n in names) and any("Range" in n for n in names)
    assert b.sa is not None and b.sg is not None          # back-compat handles preserved


def test_range_center_arrow_step_shifts_the_whole_range():
    b = _bench()
    rp = b.range
    rp.spin_center.setValue(5.0)                           # 5 GHz
    rp._apply()
    lo0 = rp.model.span_lo_hz()
    rp.spin_center.stepBy(1)                               # arrow up -> +100 MHz at 5 GHz
    rp._apply()
    assert abs(rp.model.center_hz - 5.1e9) < 1e-3
    assert abs(rp.model.span_lo_hz() - (lo0 + 100e6)) < 1e-3   # the whole range moved up


def test_range_rf_on_paints_the_range_with_a_looping_sweep():
    b = _bench()
    rp = b.range
    rp.spin_center.setValue(5.0)
    rp.spin_range.setValue(400.0)                          # 400 MHz range
    rp.chk_rf.setChecked(True)
    rp._apply()
    cmds = []
    while not rp.tx_engine._cmds.empty():
        cmds.append(rp.tx_engine._cmds.get_nowait())
    kinds = [c[0] for c in cmds]
    assert "apply" in kinds and "step_sweep" in kinds
    sweep = next(c for c in cmds if c[0] == "step_sweep")
    assert sweep[3] is True                                # loop=True -> continuous paint
    pts = sweep[1]
    assert pts[0] == rp.model.span_lo_hz() and pts[-1] == rp.model.span_hi_hz()


def test_range_rx_shows_only_the_range_in_maxhold():
    b = _bench()
    rp = b.range
    rp.spin_center.setValue(5.0)
    rp.spin_range.setValue(400.0)
    rp._apply()
    s = rp._rx_settings
    assert s.center_hz == 5e9 and s.span_hz == 400e6       # RX centered on + spanned to the range
    assert s.max_hold is True                              # painted band accumulates


def test_tab_switch_gives_only_the_active_mode_ownership(monkeypatch):
    b = _bench()
    events = []
    for mode, tag in ((b.full, "full"), (b.range, "range")):
        monkeypatch.setattr(mode, "suspend", lambda t=tag: events.append(("suspend", t)))
        monkeypatch.setattr(mode, "resume", lambda t=tag: events.append(("resume", t)))
    b._on_tab(1)                                           # activate the Range tab
    assert ("resume", "range") in events and ("suspend", "full") in events
