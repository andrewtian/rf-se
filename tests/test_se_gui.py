"""Hardware-free tests for the live SE figure GUI (se_gui.py).

The pure SEFigureModel is tested directly (accumulation, the SE(f) curve, running worst-case,
progress, verdict/floor-limited handling, and every worst_text state) -- these ALWAYS run (no Qt).
The PySide6 + pyqtgraph view feed path (the campaign thread -> queue -> main-thread drain -> model
-> pyqtgraph scatter + native controls) is tested headless under QT_QPA_PLATFORM=offscreen (the Qt
analog of matplotlib's Agg backend), and skips via importorskip when the optional `se299-gui` group
is not installed. The view assertions are on real widget/plot STATE (scatter spot positions +
verdict brushes, control values, the operator Stop -> CampaignAborted unwind), not just "no raise".
No window is opened; no hardware is touched.

Run:  uv run python -m pytest rf-se/se299/tests/test_se_gui.py -q          (needs --group se299-gui
      for the view tests; the model tests run in the shared CAD venv with no Qt)
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # headless Qt; set before any QApplication

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import coordinator
import se_gui


def _ref_row(f_hz, cap=110.0, ea8=True, band="b", ref_dbm=None, src_power_dbm=None):
    return {"f_hz": f_hz, "band": band, "capability_db": cap, "ea8_ok": ea8,
            "ref_dbm": ref_dbm, "src_power_dbm": src_power_dbm}


def _wall_row(f_hz, se=105.0, floor_limited=False, verdict="PASS", band="b", target=100.0,
              wall_dbm=None):
    return {"f_hz": f_hz, "band": band, "se_db": se, "se_reported_db": se,
            "floor_limited": floor_limited, "verdict": verdict, "target_db": target,
            "wall_dbm": wall_dbm}


def _fig(se_db, f_hz, points, lower_bound=False, band="b"):
    return {"se_db": se_db, "lower_bound": lower_bound, "band": band, "f_hz": f_hz,
            "points": points, "any_fail": False}


# ---------------------------------------------------------------- model

def _model():
    return se_gui.SEFigureModel(se_gui.build_cfg(gain_dbi=33, rbw_hz=1000.0))


def test_model_accumulates_reference_then_wall_curve():
    m = _model()
    m.set_phase("reference")
    for i, f in enumerate((1e9, 2e9)):
        m.add_reference_point(i, _ref_row(f))
    assert len(m.reference_points) == 2
    m.set_phase("wall")
    m.add_wall_point(_fig(105.0, 2e9, 1), _wall_row(2e9, se=105.0))
    m.add_wall_point(_fig(98.0, 1e9, 2), _wall_row(1e9, se=98.0))
    freqs, se, verdicts, floor = m.curve()
    assert freqs == [1.0, 2.0]                      # sorted by frequency (GHz)
    assert se == [98.0, 105.0]
    assert verdicts == ["PASS", "PASS"] and floor == [False, False]


def test_model_worst_case_tracks_running_minimum():
    m = _model()
    m.set_phase("wall")
    m.add_wall_point(_fig(105.0, 2e9, 1), _wall_row(2e9, se=105.0))
    m.add_wall_point(_fig(88.0, 5e9, 2), _wall_row(5e9, se=88.0))     # tighter -> new worst
    assert m.worst["se_db"] == 88.0 and abs(m.worst["f_hz"] - 5e9) < 1


def test_model_progress_per_phase():
    m = _model()
    total = m.total_points
    m.set_phase("reference")
    m.add_reference_point(0, _ref_row(1e9))
    assert m.progress() == ("reference", 1, total)
    m.set_phase("wall")
    m.add_wall_point(_fig(100.0, 1e9, 1), _wall_row(1e9))
    assert m.progress() == ("wall", 1, total)


def test_model_floor_limited_point_flagged_in_curve():
    m = _model()
    m.set_phase("wall")
    m.add_wall_point(_fig(90.0, 1e9, 1, lower_bound=True), _wall_row(1e9, se=90.0, floor_limited=True))
    _, _, _, floor = m.curve()
    assert floor == [True]


def test_model_worst_text_states():
    m = _model()
    assert "waiting" in m.worst_text().lower()                       # idle
    m.set_phase("reference"); m.add_reference_point(0, _ref_row(1e9))
    assert "acquiring reference" in m.worst_text().lower()           # reference
    m.set_phase("wall")
    m.add_wall_point(_fig(97.5, 3e9, 1, lower_bound=True), _wall_row(3e9, se=97.5, floor_limited=True))
    assert ">=" in m.worst_text()                                    # floor-limited -> lower bound
    m.set_summary({"campaign_pass": True})
    assert "PASS" in m.worst_text()
    m2 = _model(); m2.set_phase("wall")
    m2.add_wall_point(_fig(50.0, 3e9, 1), _wall_row(3e9, se=50.0, verdict="FAIL"))
    m2.set_summary({"campaign_pass": False})
    assert "FAIL" in m2.worst_text()


def test_model_error_and_abort_text():
    m = _model()
    m.set_error(se_gui.CampaignAborted())
    assert m.phase == "aborted" and "STOPPED" in m.worst_text()
    m2 = _model()
    m2.set_error(RuntimeError("boom"))
    assert m2.phase == "error" and "ERROR" in m2.worst_text() and "boom" in m2.worst_text()


def test_model_target_and_band_spans_from_cfg():
    m = _model()
    assert m.target_db() == 100.0                                    # DEFAULT_BANDS target
    spans = m.band_spans_ghz()
    assert spans and spans[0][1] == 1.0 and spans[-1][2] == 40.0     # 1..40 GHz coverage


def test_model_reset_clears_state():
    m = _model()
    m.set_phase("wall"); m.add_wall_point(_fig(100.0, 1e9, 1), _wall_row(1e9))
    m.reset()
    assert m.phase == "idle" and m.wall_points == [] and m.worst is None


# ---------------------------------------------------------------- 8565EC feed / TX / peak (model)

def test_model_received_feed_tx_and_peak():
    m = _model()
    m.set_phase("reference")
    m.add_reference_point(0, _ref_row(1e9, ref_dbm=-3.0, src_power_dbm=0.0))
    m.add_reference_point(1, _ref_row(2e9, ref_dbm=-1.5, src_power_dbm=0.0))
    m.set_phase("wall")
    m.add_wall_point(_fig(100.0, 1e9, 1), _wall_row(1e9, se=100.0, wall_dbm=-60.0))
    m.add_wall_point(_fig(98.0, 2e9, 2), _wall_row(2e9, se=98.0, wall_dbm=-55.0))

    rc = m.received_curve()                              # the raw 8565EC feed per pass
    assert rc["reference"] == ([1.0, 2.0], [-3.0, -1.5])
    assert rc["wall"] == ([1.0, 2.0], [-60.0, -55.0])
    assert m.tx_power_dbm() == 0.0                       # uniform TX -> a single line value
    assert m.tx_power_text() == "TX +0.0 dBm"
    # highest received power found across BOTH passes = the -1.5 dBm reference point @ 2 GHz
    assert m.peak_received() == {"dbm": -1.5, "f_hz": 2e9, "pass": "reference"}
    assert m.peak_text() == "peak RX -1.5 dBm @ 2.00 GHz (reference)"


def test_model_tx_power_varies_across_bands_returns_none_and_range_text():
    b1 = config.BandPlan("b1", 1e9, 1e9, 1, 33.0, 0.0, -143.0)     # source_power_dbm = 0
    b2 = config.BandPlan("b2", 5e9, 5e9, 1, 33.0, 10.0, -143.0)    # source_power_dbm = +10
    m = se_gui.SEFigureModel(config.Campaign(bands=(b1, b2)))
    assert m.tx_power_dbm() is None                      # no single TX line when power varies
    assert m.tx_power_text() == "TX +0.0..+10.0 dBm"


def test_model_received_feed_back_compat_when_rows_lack_levels():
    # older fixtures without ref_dbm/wall_dbm drop out of the feed cleanly (no KeyError, no peak).
    m = _model()
    m.add_reference_point(0, {"f_hz": 1e9, "band": "b", "capability_db": 110.0, "ea8_ok": True})
    m.add_wall_point(_fig(100.0, 1e9, 1), {"f_hz": 1e9, "band": "b", "se_reported_db": 100.0,
                                           "floor_limited": False, "verdict": "PASS"})
    rc = m.received_curve()
    assert rc["reference"] == ([], []) and rc["wall"] == ([], [])
    assert m.peak_received() is None
    assert m.peak_text() == "peak RX: (waiting)"


# ---------------------------------------------------------------- GUI feed path (Agg)

class _FakeCoord:
    """A scripted coordinator: fires the reference + wall callbacks with canned rows, exactly as
    loop/coordinator would, so the GUI feed path is exercised without hardware."""
    def __init__(self, ref_rows, wall_rows, campaign_pass=True, call_shield=False, path_fault=None):
        self.ref_rows, self.wall_rows, self._pass = ref_rows, wall_rows, campaign_pass
        self._call_shield = call_shield               # fire on_shield_prompt between ref and wall
        self._path_fault = path_fault                 # dict -> run_campaign raises PathNotLive(cp)
        self._depth = 0                               # controlled() re-entrancy mirror

    def ensure_ready(self):
        return True

    @contextlib.contextmanager
    def controlled(self):                             # no-op re-entrant CM mirroring Coordinator
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    def check_path(self, freqs_hz, bench=None, guard_db=6.0):
        return self._path_fault or {"verdict": "PATH-LIVE", "n_couple": 3, "n": 3}

    def run_campaign(self, bench=None, on_se_update=None, on_reference_point=None,
                     on_shield_prompt=None, pre_check_path=False, check_path_guard_db=6.0):
        if pre_check_path and self._path_fault is not None:      # dead RF path -> the pre-gate blocks
            raise coordinator.PathNotLive(self._path_fault)
        for i, row in enumerate(self.ref_rows):
            if on_reference_point:
                on_reference_point(i, row)
        if self._call_shield and on_shield_prompt:
            on_shield_prompt()                        # the operator shield-insert pause (blocks worker)
        worst = None
        for i, row in enumerate(self.wall_rows):
            se = row["se_reported_db"]
            if worst is None or se < worst:
                worst = se
            fig = _fig(worst, row["f_hz"], i + 1)
            if on_se_update:
                on_se_update(fig, row)
        return {"summary": {"campaign_pass": self._pass}, "reference": {}, "wall": {},
                "se_figure": _fig(worst, self.wall_rows[-1]["f_hz"], len(self.wall_rows))}


def _gui_with(ref_rows, wall_rows, campaign_pass=True):
    pytest.importorskip("PySide6")                   # view tests need the optional se299-gui group
    model = _model()
    factory = lambda gain, rbw, *a: (_FakeCoord(ref_rows, wall_rows, campaign_pass), None)
    return model, se_gui.SELiveGUI(model, factory)


def test_gui_feed_path_paints_curve_and_drains():
    ref = [_ref_row(1e9), _ref_row(2e9)]
    wall = [_wall_row(1e9, se=102.0), _wall_row(2e9, se=95.0)]
    model, gui = _gui_with(ref, wall)
    gui._run_campaign()                              # synchronous: pushes events into the queue
    gui._drain()                                     # main-thread drain into the model
    assert model.phase == "done"
    freqs, se, verdicts, _ = model.curve()
    assert freqs == [1.0, 2.0] and se == [102.0, 95.0]
    assert model.worst["se_db"] == 95.0
    scatter = gui.render()                           # paints the pyqtgraph scatter
    xs, ys = scatter.getData()                       # REAL state: the plotted points match the curve
    assert [round(float(x), 6) for x in xs] == [1.0, 2.0]
    assert [round(float(y), 6) for y in ys] == [102.0, 95.0]
    assert "95.0 dB" in gui.headline.text() and "GHz" in gui.headline.text()
    assert gui.progress_txt.text().startswith("phase=done")


def test_no_coupling_pre_gate_surfaces_distinct_fault_banner():
    # G.1: the mandatory RF-path pre-gate. A dead path (TX tone never rises above the RX floor) must
    # abort with a DISTINCT NO-COUPLING banner that names the dead path -- NOT a generic ERROR (which
    # reads as a software bug). Proves PathNotLive is caught separately and rendered in the headline.
    pytest.importorskip("PySide6")
    model = _model()
    cp = {"verdict": "NO-COUPLING", "n_couple": 0, "n": 3, "max_ambient_dbm": -88.0}
    factory = lambda gain, rbw, *a: (
        _FakeCoord([_ref_row(1e9)], [_wall_row(1e9)], path_fault=cp), None)
    gui = se_gui.SELiveGUI(model, factory)
    gui._run_campaign()                                  # synchronous: the pre-gate raises PathNotLive
    gui._drain()                                         # main-thread drain -> set_path_fault
    assert model.phase == "path_fault"
    txt = model.worst_text()
    assert "NO-COUPLING" in txt and "0/3" in txt         # names the dead path + coupled/total points
    assert not txt.startswith("ERROR")                   # distinct from the generic error branch
    gui.render()                                         # refresh the view from the model
    assert "NO-COUPLING" in gui.headline.text()          # surfaced in the headline label


def test_gui_render_paints_received_feed_tx_line_and_peak():
    ref = [_ref_row(1e9, ref_dbm=-3.0, src_power_dbm=0.0),
           _ref_row(2e9, ref_dbm=-1.0, src_power_dbm=0.0)]
    wall = [_wall_row(1e9, se=102.0, wall_dbm=-62.0),
            _wall_row(2e9, se=95.0, wall_dbm=-58.0)]
    model, gui = _gui_with(ref, wall)
    gui._run_campaign(); gui._drain()
    gui.render()
    # the reference + wall received-level curves carry the raw 8565EC feed
    rx, ry = gui._ref_feed.getData()
    assert [round(float(x), 6) for x in rx] == [1.0, 2.0]
    assert [round(float(y), 6) for y in ry] == [-3.0, -1.0]
    wx, wy = gui._wall_feed.getData()
    assert [round(float(y), 6) for y in wy] == [-62.0, -58.0]
    # TX-power line at the known source output, visible (uniform power across the run)
    assert float(gui._tx_line.value()) == 0.0
    assert gui._tx_line.isVisible()
    # peak marker at the strongest received tone: -1.0 dBm @ 2 GHz (the reference pass)
    px, py = gui._peak_pt.getData()
    assert [round(float(x), 6) for x in px] == [2.0]
    assert [round(float(y), 6) for y in py] == [-1.0]
    assert "TX +0.0 dBm" in gui.feed_txt.text()
    assert "peak RX -1.0 dBm @ 2.00 GHz" in gui.feed_txt.text()


def test_gui_render_verdict_brushes_and_floor_hollow():
    # a PASS point (solid green) and a floor-limited FAIL point (hollow: white fill, coloured edge)
    ref = [_ref_row(1e9)]
    wall = [_wall_row(1e9, se=105.0, verdict="PASS"),
            _wall_row(2e9, se=40.0, verdict="FAIL", floor_limited=True)]
    model, gui = _gui_with(ref, wall)
    gui._run_campaign(); gui._drain()
    scatter = gui.render()
    spots = scatter.points()
    assert len(spots) == 2
    pass_brush = spots[0].brush().color().name()             # PASS -> solid green fill
    fail_brush = spots[1].brush().color().name()             # floor-limited -> white (hollow) fill
    assert pass_brush == se_gui.VERDICT_COLOR["PASS"].lower()
    assert fail_brush == "#ffffff"
    assert spots[1].pen().color().name() == se_gui.VERDICT_COLOR["FAIL"].lower()   # coloured edge


def test_gui_stop_aborts_campaign_cleanly():
    ref = [_ref_row(1e9), _ref_row(2e9), _ref_row(3e9)]
    wall = [_wall_row(1e9)]
    model, gui = _gui_with(ref, wall)
    gui._stop.set()                                  # operator pressed Stop before it ran
    gui._run_campaign()
    gui._drain()
    assert model.phase == "aborted"                  # CampaignAborted unwound, surfaced to model
    assert "STOPPED" in model.worst_text()
    gui.render()                                     # render an aborted model must not raise
    assert "STOPPED" in gui.headline.text()


def test_gui_operator_controls_change_config():
    model, gui = _gui_with([_ref_row(1e9)], [_wall_row(1e9)])
    gui.combo_gain.setCurrentText("25")              # native QComboBox drives the handler
    assert gui._gain == 25
    gui.spin_rbw.setValue(300.0)                      # native QDoubleSpinBox drives the handler
    assert gui._rbw == 300.0
    # sweep-band + tone spinners: 'auto' (sentinel) means None; a valid lo<hi pair sets the band
    gui.seed_span(2.0, 8.0); gui.seed_power(15.0)
    assert gui._span_lo == 2.0 and gui._span_hi == 8.0 and gui._power == 15.0
    gui.spin_slo.set_optional(None)                   # either bound on auto clears back to default
    gui._on_span()
    assert gui._span_lo is None and gui._span_hi is None


def test_build_cfg_applies_gain_and_rbw():
    c25 = se_gui.build_cfg(gain_dbi=25, rbw_hz=500.0)
    assert c25.analyzer.rbw_hz == 500.0
    assert c25.bands[-1].antenna_gain_dbi == config.WR28_STANDARD_25DBI.antenna_gain_dbi
    c33 = se_gui.build_cfg(gain_dbi=33, rbw_hz=1000.0)
    assert c33.bands[-1].antenna_gain_dbi == 33.0


def test_build_cfg_operator_sweep_band_and_tone():
    # operator sets the SWEEP FREQUENCY via span_lo/hi -> a single custom band replaces the default
    c = se_gui.build_cfg(gain_dbi=33, rbw_hz=1000.0, span_lo_ghz=2.0, span_hi_ghz=8.0,
                         points=7, power_dbm=15.0)
    assert len(c.bands) == 1
    b = c.bands[0]
    assert b.f_lo_hz == 2e9 and b.f_hi_hz == 8e9 and b.n_points == 7
    assert b.source_power_dbm == 15.0                        # operator TONE power applied
    freqs = [f for f, _ in c.frequencies()]
    assert min(freqs) == pytest.approx(2e9) and max(freqs) == pytest.approx(8e9)  # log-spaced band


def test_build_cfg_tone_power_overrides_default_bands():
    c = se_gui.build_cfg(gain_dbi=33, rbw_hz=1000.0, power_dbm=20.0)   # no span -> default plan
    assert len(c.bands) == len(config.DEFAULT_BANDS)
    assert all(b.source_power_dbm == 20.0 for b in c.bands)  # tone override applied to every band


def test_shield_prompt_pauses_worker_between_passes_until_continue():
    # P0-5: the campaign must PAUSE between the reference and wall passes for the operator to insert
    # the shield, blocking the WORKER (not the Qt main thread), released by the continue button; Stop
    # during the pause unwinds cleanly.
    import time
    ref, wall = [_ref_row(1e9)], [_wall_row(1e9, se=100.0)]
    model = _model()
    factory = lambda gain, rbw, *a: (_FakeCoord(ref, wall, call_shield=True), None)
    gui = se_gui.SELiveGUI(model, factory)
    gui._on_run()                                        # runs in a background thread
    for _ in range(300):                                # pump the main-thread drain to the prompt
        gui._drain()
        if gui.btn_shield.isEnabled():
            break
        time.sleep(0.01)
    assert gui.btn_shield.isEnabled()                   # release button armed
    assert model.phase == "insert-shield"              # paused between ref and wall
    assert gui._thread.is_alive()                      # worker is PARKED, not finished
    gui._on_shield_continue()                          # operator: shield inserted
    gui._thread.join(timeout=3)
    gui._drain()
    assert not gui._thread.is_alive() and model.phase == "done"   # released -> wall -> done
    assert not gui.btn_shield.isEnabled()              # button disabled again after continue


def test_shield_prompt_stop_during_pause_aborts_cleanly():
    import time
    ref, wall = [_ref_row(1e9)], [_wall_row(1e9)]
    model = _model()
    factory = lambda gain, rbw, *a: (_FakeCoord(ref, wall, call_shield=True), None)
    gui = se_gui.SELiveGUI(model, factory)
    gui._on_run()
    for _ in range(300):
        gui._drain()
        if gui.btn_shield.isEnabled():
            break
        time.sleep(0.01)
    gui._on_stop()                                      # Stop while parked at the shield prompt
    gui._thread.join(timeout=3)
    gui._drain()
    assert not gui._thread.is_alive() and model.phase == "aborted"   # CampaignAborted unwound
