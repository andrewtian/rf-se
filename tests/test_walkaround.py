"""Hardware-free tests for the near-field-probe walkaround GUI (walkaround.py).

The pure NearFieldModel is tested directly (frame accumulation, max-hold, baseline, heat
classification, marks, reset-peak, history cap, CSV) -- these ALWAYS run (no Qt). The PySide6 +
pyqtgraph view feed path (bg walkaround -> queue -> main-thread drain -> pyqtgraph trace + heat
label + marks) is tested headless under QT_QPA_PLATFORM=offscreen, skipped via importorskip when the
optional `se299-gui` group is absent. View assertions are on real plot STATE (trace data, max-hold
line position, heat-label colour, marks scatter) + the operator freq/tone/mark controls.

Run:  uv run python -m pytest rf-se/se299/tests/test_walkaround.py -q     (view tests need --group
      se299-gui; the model tests run in the shared CAD venv with no Qt)
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # headless Qt; set before any QApplication

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import walkaround


# ---------------------------------------------------------------- model

def test_model_accumulates_frames_and_max_hold():
    m = walkaround.NearFieldModel(5e9)
    for i, lvl in enumerate([-110, -108, -70, -95, -109]):   # a leak spike at frame 2
        m.add_frame(i, lvl)
    assert m.frame_count == 5
    assert m.current_level_dbm == -109                        # last read
    assert m.max_hold_dbm == -70 and m.max_hold_frame == 2    # peak HELD past the leak
    assert m.baseline_dbm == -110                             # quiet floor


def test_model_heat_classification_by_rise_over_floor():
    m = walkaround.NearFieldModel(5e9)
    m.add_frame(0, -110)                                       # baseline
    m.add_frame(1, -108); assert m.heat() == "quiet"          # +2
    m.add_frame(2, -104); assert m.heat() == "cool"           # +6
    m.add_frame(3, -98);  assert m.heat() == "warm"           # +12
    m.add_frame(4, -80);  assert m.heat() == "hot"            # +30
    assert round(m.rise_db()) == 30


def test_model_reset_peak_and_marks():
    m = walkaround.NearFieldModel(5e9)
    for i, lvl in enumerate([-110, -60, -105]):
        m.add_frame(i, lvl)
    assert m.max_hold_dbm == -60
    m.reset_peak()                                            # restart the hold at the current level
    assert m.max_hold_dbm == -105
    mk = m.add_mark("door-seam")
    assert mk["label"] == "door-seam" and mk["level_dbm"] == -105
    assert len(m.marks) == 1
    m.clear_marks(); assert m.marks == []


def test_model_history_capped():
    m = walkaround.NearFieldModel(5e9)
    for i in range(m.HISTORY_CAP + 50):
        m.add_frame(i, -100.0)
    assert len(m.levels) == m.HISTORY_CAP                     # capped, keeps the tail
    assert m.frame_count == m.HISTORY_CAP + 50


def test_model_marks_csv():
    m = walkaround.NearFieldModel(38e9)
    m.add_frame(0, -110); m.add_frame(1, -40); m.add_mark("hot1")
    csv = m.marks_csv()
    assert "label,level_dbm,frame,freq_hz" in csv and "hot1,-40.00,2,38000000000" in csv


def test_model_headline_states():
    m = walkaround.NearFieldModel(5e9)
    assert "press Start" in m.headline_text()
    m.add_frame(0, -110); m.add_frame(1, -70)
    assert "HOT" in m.headline_text() and "dBm" in m.headline_text()
    m.set_error(RuntimeError("boom")); assert "ERROR" in m.headline_text()


# ---------------------------------------------------------------- GUI feed path (Agg)

class _FakeWalkCoord:
    """Scripted coordinator: replays canned probe levels through on_frame, records the tone
    freq/power it was driven with (so we can assert the GUI passed them)."""
    def __init__(self, levels):
        self.levels = levels
        self.freq_seen = None
        self.power_seen = None

    def ensure_ready(self):
        return True

    def walkaround(self, freq_hz, on_frame, should_stop, bench=None, use_average=False,
                   power_dbm=None):
        self.freq_seen, self.power_seen = freq_hz, power_dbm
        i = 0
        for lvl in self.levels:
            if should_stop():
                break
            on_frame(i, lvl); i += 1
        return i


def _gui_with(levels):
    pytest.importorskip("PySide6")                           # view tests need the se299-gui group
    model = walkaround.NearFieldModel(5e9)
    coord = _FakeWalkCoord(levels)
    gui = walkaround.NearFieldGUI(model, walk_factory=lambda gain, rbw: (coord, None))
    return model, gui, coord


def test_gui_feed_path_paints_and_holds_peak():
    model, gui, _ = _gui_with([-110, -108, -65, -100, -109])
    gui._run_walk()                                           # synchronous: pushes to the queue
    gui._drain()                                              # main-thread drain into the model
    assert model.phase == "done"
    assert model.frame_count == 5 and model.max_hold_dbm == -65
    line, maxline, marks = gui.render()                       # paints the pyqtgraph trace
    xs, ys = line.getData()                                   # REAL state: the plotted trace
    assert list(ys) == [-110, -108, -65, -100, -109]
    assert abs(maxline.value() - (-65)) < 1e-9               # max-hold line held at the leak peak
    assert "dBm" in gui.headline.text()


def test_gui_render_heat_colour_reflects_leak():
    model, gui, _ = _gui_with([-110, -60])                    # +50 dB over floor -> HOT
    gui._run_walk(); gui._drain()
    gui.render()
    assert model.heat() == "hot"
    assert walkaround.HEAT_COLOR["hot"] in gui.headline.styleSheet()   # red heat colour applied


def test_gui_passes_operator_freq_and_tone_power():
    model, gui, coord = _gui_with([-100])
    gui.spin_freq.setValue(12.5)                             # operator sets sweep freq via the spinner
    gui.spin_pow.set_optional(12.0); gui._on_power()          # operator sets tone power via the spinner
    #                                                          (in-range: the spinner caps at the source's
    #                                                          17 dBm hard ceiling, below the 8565EC +30 dBm max)
    gui._run_walk()
    assert coord.freq_seen == 12.5e9                          # the tone was set to the GUI value
    assert coord.power_seen == 12.0
    gui.spin_pow.set_optional(None); gui._on_power()
    assert gui._power_dbm is None                             # 'auto' (sentinel) -> band default


def test_gui_mark_logs_current_reading():
    model, gui, _ = _gui_with([-110, -50])
    gui._run_walk(); gui._drain()
    gui._on_mark()                                            # operator marks the current spot
    _, _, marks = gui.render()
    assert len(model.marks) == 1 and model.marks[0]["level_dbm"] == -50
    assert len(marks.getData()[0]) == 1                       # the mark is plotted on the trace


def test_gui_error_surfaced():
    pytest.importorskip("PySide6")
    class _Dead:
        def ensure_ready(self): return False
    model = walkaround.NearFieldModel(5e9)
    gui = walkaround.NearFieldGUI(model, walk_factory=lambda g, r: (_Dead(), None))
    gui._run_walk(); gui._drain()
    assert model.phase == "error" and "not READY" in model.error
