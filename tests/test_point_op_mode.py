"""Point-Operation bench mode (point_op_mode) + its bench tab. PointOpModel is Qt-free; the panel +
arrow-key + coordinated-tuning tests are Qt-gated (se299-gui group, offscreen).

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui \
        python -m pytest rf-se/se299/tests/test_point_op_mode.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import point_op_mode


# ---- pure model (no Qt) --------------------------------------------------------------------

def test_point_op_model_shift_freq_uses_ladder_and_clamps():
    m = point_op_mode.PointOpModel()
    m.freq_hz = 5e9
    assert m.shift_freq(up=True) == pytest.approx(5.1e9)          # +100 MHz coarse at 5 GHz
    m.freq_hz = 5e9
    assert m.shift_freq(up=False, fine=True) == pytest.approx(5e9 - 10e6)   # 10 MHz fine step
    m.freq_hz = 10e6
    assert m.shift_freq(up=False) == point_op_mode.FLOOR_MIN_HZ   # clamped at the source floor


def test_point_op_model_bump_power_steps_and_clamps():
    m = point_op_mode.PointOpModel()
    m.power_dbm = -10.0
    assert m.bump_power(up=True) == -9.0                          # +1 dB coarse
    assert m.bump_power(up=False, fine=True) == -9.1             # 0.1 dB fine
    m.power_dbm = 16.5
    assert m.bump_power(up=True) == point_op_mode.POWER_MAX_DBM   # clamps at +17 dBm


def test_point_op_model_se_is_reference_minus_current():
    m = point_op_mode.PointOpModel()
    assert m.se_db() is None                                      # nothing read yet
    m.set_current(-2.0)
    assert m.big_text() == "RX -2.0 dBm"                          # no reference -> show the level
    assert m.se_db() is None
    m.set_reference()                                            # capture -2.0 dBm as the baseline
    m.set_current(-62.0)                                        # shield in place -> weaker
    assert m.se_db() == pytest.approx(60.0)                       # -2 - (-62) = 60 dB SE
    assert m.big_text() == "SE +60.0 dB"
    m.clear_reference()
    assert m.se_db() is None


def test_point_op_model_readout_and_waiting_headline():
    m = point_op_mode.PointOpModel()
    assert m.big_text() == "RX --  (press Run)"
    t = m.readout_text()
    assert "2.450000 GHz" in t and "off" in t and "ref -- dBm" in t


# ---- panel + coordinated arrow keys (Qt) ---------------------------------------------------

def _bench():
    pytest.importorskip("PySide6")
    import bench_gui
    return bench_gui.build_bench("sim", "sim")


def _drain_tx(engine):
    out = []
    while not engine._cmds.empty():
        out.append(engine._cmds.get_nowait())
    return out


def test_bench_hosts_the_point_op_tab():
    b = _bench()
    names = [b.tabs.tabText(i) for i in range(b.tabs.count())]
    assert any("Point Op" in n for n in names)


def test_point_op_left_right_shifts_rx_and_tx_frequency_together():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(5.0)               # 5 GHz
    pp._apply()
    _drain_tx(pp.tx_engine)                   # clear the apply from the setup
    pp._shift_freq(up=True, fine=False)       # Right arrow -> +100 MHz at 5 GHz
    assert abs(pp.model.freq_hz - 5.1e9) < 1e-3                   # model/display updates IMMEDIATELY
    assert pp._apply_timer.isActive()                            # instrument apply is DEBOUNCED (armed)
    pp._apply()                                                  # operator pauses -> the apply fires
    assert abs(pp._rx_settings.center_hz - 5.1e9) < 1e-3          # RX center followed the point
    apply_cmds = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"]
    assert apply_cmds and abs(apply_cmds[-1][1] - 5.1e9) < 1e-3   # TX freq followed to the SAME point


def test_point_op_up_down_changes_tx_level_only_not_frequency():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(5.0)
    pp.spin_power.setValue(-10.0)
    pp._apply()
    _drain_tx(pp.tx_engine)
    pp._bump_power(up=True, fine=False)       # Up arrow -> +1 dB
    assert pp.model.power_dbm == -9.0
    assert pp.spin_power.value() == -9.0                          # reflected in the control IMMEDIATELY
    pp._apply()                                                  # operator pauses -> debounced apply fires
    apply_cmds = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"]
    assert apply_cmds and apply_cmds[-1][2] == -9.0               # TX power bumped
    assert abs(apply_cmds[-1][1] - 5e9) < 1e-3                    # frequency unchanged


def test_point_op_installs_the_eight_arrow_shortcuts():
    b = _bench()
    pp = b.point
    seqs = sorted(sc.key().toString() for sc in pp._shortcuts)
    assert len(pp._shortcuts) == 8
    for want in ("Up", "Down", "Left", "Right", "Shift+Up", "Shift+Left"):
        assert want in seqs


def test_point_op_render_reads_peak_and_shows_se_after_reference():
    b = _bench()
    pp = b.point
    pp.model.freq_hz = 5.0e9                                      # commanded point = where the tone is
    freqs = [4.999e9, 5.0e9, 5.001e9]
    pp.rx_model.set_trace(freqs, [-70.0, -12.0, -71.0])           # a tone peak at 5 GHz
    pp.render()
    assert pp.model.current_dbm == -12.0
    assert "RX -12.0 dBm" in pp.big.text()
    px, py = pp._marker.getData()
    assert [round(float(x), 6) for x in px] == [5.0] and [round(float(y), 6) for y in py] == [-12.0]
    # capture the no-barrier reference (valid on-freq tone), then read a weaker tone -> live SE
    pp._set_reference()                                          # reference = -12 dBm
    pp.rx_model.set_trace(freqs, [-70.0, -62.0, -71.0])
    pp.render()
    assert pp.model.se_db() == pytest.approx(50.0)               # -12 - (-62)
    assert "SE +50.0 dB" in pp.big.text()


# ---- jump-to-frequency presets + save measurement (Qt) -------------------------------------

def test_point_op_preset_jump_retunes_both_units_to_absolute_freq():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(1.0); pp._apply()
    _drain_tx(pp.tx_engine)                                       # clear the setup apply
    pp._jump_to(2.45e9)                                          # a WiFi/ISM preset
    assert abs(pp.model.freq_hz - 2.45e9) < 1e-3                 # model/display jump immediately
    assert abs(pp.spin_freq.value() - 2.45) < 1e-6
    assert pp._apply_timer.isActive()                           # instrument retune is DEBOUNCED (armed)
    pp._apply()                                                  # operator pauses -> apply fires
    assert abs(pp._rx_settings.center_hz - 2.45e9) < 1e-3        # RX center followed the preset
    apply_cmds = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"]
    assert apply_cmds and abs(apply_cmds[-1][1] - 2.45e9) < 1e-3  # TX followed to the SAME point (joint)


def test_point_op_preset_jump_clamps_to_joint_range():
    b = _bench()
    pp = b.point
    pp._jump_to(100e9)                                          # above the 40 GHz source ceiling
    assert pp.model.freq_hz == point_op_mode.FLOOR_MAX_HZ
    pp._jump_to(1.0)                                            # below the 10 MHz source floor
    assert pp.model.freq_hz == point_op_mode.FLOOR_MIN_HZ


def test_point_op_save_measurement_writes_json(tmp_path):
    import measurements
    b = _bench()
    pp = b.point
    pp._meas_dir = str(tmp_path)                                # redirect output out of the repo
    pp.model.freq_hz = 2.45e9
    pp.rx_model.set_trace([2.449e9, 2.45e9, 2.451e9], [-90.0, -14.0, -88.0])
    pp._save_measurement()
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and "saved" in pp._ref_msg
    freqs, levels, rec = measurements.load_measurement(str(files[0]))
    assert levels == [-90.0, -14.0, -88.0]
    assert rec["context"]["center_hz"] == 2.45e9 and rec["context"]["mode"] == "point_op"


def test_point_op_save_measurement_noop_without_trace(tmp_path):
    b = _bench()
    pp = b.point
    pp._meas_dir = str(tmp_path)
    pp._save_measurement()                                      # no trace yet
    assert list(tmp_path.glob("*.json")) == [] and "nothing to save" in pp._ref_msg


# ---- live-state debug pane (model) ---------------------------------------------------------

def test_point_op_tx_state_text_formats_source_truth():
    m = point_op_mode.PointOpModel()
    assert m.tx_state_text() == "TX 68367C:  (waiting)"
    m.set_tx_state({"of1_mhz": 5000.0, "osb": 0x00, "level_dbm": -5.0})
    t = m.tx_state_text()
    assert "OF1 5.000000 GHz" in t and "OL1 -5.0 dBm" in t and "leveled/locked" in t
    m.set_tx_state({"of1_mhz": 5000.0, "osb": 0x0C, "level_dbm": -5.0})    # unleveled + unlocked
    assert "UNLEV/UNLOCK" in m.tx_state_text()


def test_point_op_rx_state_text_flags_wedge_on_errors():
    m = point_op_mode.PointOpModel()
    assert m.rx_state_text() == "RX 8565EC:  (waiting)"
    m.set_rx_state({"center_hz": 2e9, "span_hz": 5e6, "rbw_hz": 0.0, "detector": "POS", "errors": []})
    t = m.rx_state_text()
    assert "CF 2.000000 GHz" in t and "SP 5.000 MHz" in t and "RB auto" in t
    assert "ERR none" in t and "WEDGE" not in t
    m.set_rx_state({"center_hz": 2e9, "span_hz": 5e6, "rbw_hz": 1000.0, "detector": "POS",
                    "errors": [333, 335]})
    t = m.rx_state_text()
    assert "ERR 333,335" in t and "WEDGE" in t and "RB 1000 Hz" in t


def test_point_op_rx_state_text_annunciates_model_vs_device_drift():
    m = point_op_mode.PointOpModel()
    m.set_rx_state({"center_hz": 2.45e9, "span_hz": 5e6, "rbw_hz": 0.0, "detector": "POS",
                    "errors": [], "drift": ["ref_level_dbm: want -10.0, got -5.0"]})
    t = m.rx_state_text()
    assert "DRIFT" in t and "ref_level_dbm" in t           # reconciliation surfaces the disagreement
    # no drift -> no annunciation
    m.set_rx_state({"center_hz": 2.45e9, "span_hz": 5e6, "rbw_hz": 0.0, "detector": "POS",
                    "errors": [], "drift": []})
    assert "DRIFT" not in m.rx_state_text()


# ---- rehearsal: no input combination leaves an invalid state --------------------------------

def test_point_op_rehearsal_frequency_clamps_at_both_band_edges():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(40.0); pp._apply()
    pp._shift_freq(up=True); pp._shift_freq(up=True)          # push past 40 GHz
    assert pp.model.freq_hz == point_op_mode.FLOOR_MAX_HZ     # never above the source ceiling
    pp.spin_freq.setValue(0.01); pp._apply()                 # 10 MHz floor
    pp._shift_freq(up=False); pp._shift_freq(up=False)       # push below 10 MHz
    assert pp.model.freq_hz == point_op_mode.FLOOR_MIN_HZ     # never below the source floor


def test_point_op_rehearsal_power_clamps_at_both_rails():
    b = _bench()
    pp = b.point
    pp.spin_power.setValue(17.0); pp._apply()
    pp._bump_power(up=True); pp._bump_power(up=True)
    assert pp.model.power_dbm == point_op_mode.POWER_MAX_DBM
    pp.spin_power.setValue(-60.0); pp._apply()
    pp._bump_power(up=False); pp._bump_power(up=False)
    assert pp.model.power_dbm == point_op_mode.POWER_MIN_DBM


def _drain_rx(engine):
    out = []
    while not engine._cmds.empty():
        out.append(engine._cmds.get_nowait())
    return out


def test_point_op_preselector_peaked_only_above_the_2p9ghz_crossover():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(2.0); pp._apply()                  # below 2.9 GHz -> no preselector
    kinds = [c[0] for c in _drain_rx(pp.rx_engine)]
    assert "apply_settings" in kinds and "preselector_peak" not in kinds
    pp.spin_freq.setValue(10.0); pp._apply()                 # above 2.9 GHz -> peak the YIG
    kinds = [c[0] for c in _drain_rx(pp.rx_engine)]
    assert "preselector_peak" in kinds


def test_point_op_nudge_shifts_both_units_by_the_fixed_delta():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(5.0); pp._apply()
    _drain_tx(pp.tx_engine)                                       # clear the setup apply
    pp._nudge(+1e9)                                              # the named +1 GHz control
    assert abs(pp.model.freq_hz - 6e9) < 1e-3                    # model/display step immediately
    assert abs(pp.spin_freq.value() - 6.0) < 1e-6
    pp._apply()                                                  # debounced retune fires
    assert abs(pp._rx_settings.center_hz - 6e9) < 1e-3          # RX followed
    apply_cmds = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"]
    assert apply_cmds and abs(apply_cmds[-1][1] - 6e9) < 1e-3    # TX followed to the SAME point (joint)
    pp._nudge(-1e9)                                              # -1 GHz returns
    assert abs(pp.model.freq_hz - 5e9) < 1e-3


def test_point_op_nudge_clamps_at_both_band_edges():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(40.0); pp._apply()
    pp._nudge(+1e9)                                             # push past the 40 GHz source ceiling
    assert pp.model.freq_hz == point_op_mode.FLOOR_MAX_HZ
    pp.spin_freq.setValue(0.01); pp._apply()                   # 10 MHz floor
    pp._nudge(-1e9)                                            # push below the 10 MHz source floor
    assert pp.model.freq_hz == point_op_mode.FLOOR_MIN_HZ


def test_point_op_commands_both_units_across_dc_to_40ghz():
    """Ensure the GUI's joint set-frequency path works at representative points spanning DC->40 GHz: each
    commands BOTH units to the same f and peaks the preselector only above 2.9 GHz."""
    b = _bench()
    pp = b.point
    for f in (10e6, 100e6, 300e6, 1e9, 2.45e9, 2.9e9, 5e9, 10e9, 18e9, 26.5e9, 40e9):
        _drain_tx(pp.tx_engine); _drain_rx(pp.rx_engine)         # clear prior enqueue
        pp._jump_to(f); pp._apply()
        assert abs(pp._rx_settings.center_hz - f) < 1e-3, f"RX not at {f}"
        tx = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"]
        assert tx and abs(tx[-1][1] - f) < 1e-3, f"TX not at {f}"        # both units to the SAME f
        peaked = "preselector_peak" in [c[0] for c in _drain_rx(pp.rx_engine)]
        assert peaked == (f >= point_op_mode.PRESELECTOR_MIN_HZ), f"preselector gating wrong at {f}"


def test_point_op_rf_toggle_enqueues_the_matching_tx_state():
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(2.0); pp.chk_rf.setChecked(True); pp._apply()
    on_cmd = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"][-1]
    assert on_cmd[3] is True                                 # RF commanded ON
    pp.chk_rf.setChecked(False); pp._apply()
    off_cmd = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"][-1]
    assert off_cmd[3] is False                               # RF commanded OFF


def test_point_op_mixed_input_sequence_keeps_the_model_valid_throughout():
    b = _bench()
    pp = b.point
    ops = [lambda: pp._shift_freq(up=True), lambda: pp._shift_freq(up=False, fine=True),
           lambda: pp._bump_power(up=True), lambda: pp._bump_power(up=False, fine=True),
           lambda: pp._set_reference(), lambda: pp._clear_reference()]
    for i in range(60):
        ops[i % len(ops)]()
        assert point_op_mode.FLOOR_MIN_HZ <= pp.model.freq_hz <= point_op_mode.FLOOR_MAX_HZ
        assert point_op_mode.POWER_MIN_DBM <= pp.model.power_dbm <= point_op_mode.POWER_MAX_DBM
        pp.model.big_text(); pp.model.readout_text()         # never raises regardless of ref/current


def test_point_op_state_poll_populates_the_debug_pane_from_engine_events():
    b = _bench()
    pp = b.point
    pp.rx_engine.enqueue(("read_state", None))
    pp.tx_engine.enqueue(("read_state", None))
    pp.rx_engine.step_once()                                 # sim: emits rx_state (+ a trace)
    pp.tx_engine.step_once()                                 # sim: emits tx_state
    pp._drain(); pp.render()
    assert pp.model.tx_state != {} and pp.model.rx_state != {}
    assert "TX 68367C:" in pp.debug.text() and "RX 8565EC:" in pp.debug.text()


def test_point_op_tick_polls_state_every_fifth_call():
    b = _bench()
    pp = b.point
    # drain any startup commands, then tick 5x -> exactly one read_state pair enqueued
    _drain_rx(pp.rx_engine); _drain_tx(pp.tx_engine)
    for _ in range(4):
        pp._tick()
    assert all(c[0] != "read_state" for c in _peek(pp.rx_engine))   # not yet at the 5th tick
    pp._tick()
    assert any(c[0] == "read_state" for c in _drain_rx(pp.rx_engine))
    assert any(c[0] == "read_state" for c in _drain_tx(pp.tx_engine))


def _peek(engine):
    return list(engine._cmds.queue)


def test_point_op_apply_carries_a_real_source_settle():
    # regression: the 0.05 s engine default reads a suppressed tone after a retune; point-op must
    # pass a real settle (POINT_SETTLE_S) as the 5th element of the tx "apply" so the ALC output is
    # fully ramped before the analyzer reads. Live-proven: 0.05 s -> -72.8 dBm, 0.6 s -> true tone.
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(2.1); pp.chk_rf.setChecked(True); pp._apply()
    apply_cmd = [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"][-1]
    assert len(apply_cmd) >= 5 and apply_cmd[4] == point_op_mode.POINT_SETTLE_S
    assert point_op_mode.POINT_SETTLE_S >= 0.4      # below this the tone reads suppressed live


# ---- drive-rate limiting: conflate superseded retunes (arrow auto-repeat must not flood) -----

def test_point_op_arrow_shortcuts_have_autorepeat_disabled():
    b = _bench()
    pp = b.point
    assert pp._shortcuts and all(not sc.autoRepeat() for sc in pp._shortcuts)


def test_source_engine_conflates_a_burst_of_applies_to_the_last():
    b = _bench()
    pp = b.point
    pp.tx_engine.step_once()                          # acquire tx (hub.source is None until leased)
    src = pp.tx_engine.hub.source
    calls = []
    orig = src.set_freq
    src.set_freq = lambda f: (calls.append(f), orig(f))[1]
    for gz in (2.0, 2.1, 2.2, 2.3, 2.4, 2.5):          # a burst, as key auto-repeat would produce
        pp.tx_engine.enqueue(("apply", gz * 1e9, 0.0, False, 0.0))
    pp.tx_engine.step_once()
    assert len(calls) == 1 and abs(calls[-1] - 2.5e9) < 1.0     # ONE retune, to the final target


def test_spectrum_engine_conflates_a_burst_of_apply_settings():
    b = _bench()
    pp = b.point
    import sa_gui
    pp.rx_engine.step_once()                          # acquire rx (hub.analyzer is None until leased)
    an = pp.rx_engine.hub.analyzer
    centers = []
    orig = an.set_frequency
    an.set_frequency = lambda **kw: (centers.append(kw.get("center_hz")), orig(**kw))[1]
    presel = []
    if hasattr(an, "peak_preselector"):
        op = an.peak_preselector
        an.peak_preselector = lambda c: (presel.append(c), op(c))[1]
    for gz in (3.0, 3.1, 3.2, 3.3):
        s = sa_gui.SpectrumSettings()
        s.center_hz, s.span_hz, s.detector = gz * 1e9, 5e6, "peak"
        pp.rx_engine.enqueue(("apply_settings", s))
        pp.rx_engine.enqueue(("preselector_peak", None))
    pp.rx_engine.step_once()
    # conflated: every analyzer retune targets the FINAL center (3.3 GHz), never an intermediate one.
    # High band re-asserts the span after the preselector peak, so >1 set_frequency call is expected --
    # but all to 3.3 GHz (the burst produced one retune target, not four).
    assert centers and all(abs(c - 3.3e9) < 1.0 for c in centers)
    if hasattr(an, "peak_preselector"):
        assert len(presel) <= 1                                  # at most one preselector peak this step


def test_point_op_rapid_arrow_burst_debounces_to_one_retune_per_unit():
    """A fast burst of arrow taps must retune each instrument exactly ONCE (to the final point), not
    once per tap -- else the 68367C ratchets its attenuator/band relays and the 8565EC re-CLRWs (blank)
    on every keystroke. The taps update the model/display live but the instrument apply is debounced;
    when the operator pauses, one apply lands and each unit retunes once. Driver-level count."""
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(5.0); pp._apply()
    pp.tx_engine.step_once(); pp.rx_engine.step_once()           # drain the setup apply
    srccalls, ancalls = [], []
    src, an = pp.tx_engine.hub.source, pp.rx_engine.hub.analyzer
    os_ = src.set_freq; src.set_freq = lambda f: (srccalls.append(f), os_(f))[1]
    oa = an.set_frequency; an.set_frequency = lambda **kw: (ancalls.append(kw.get("center_hz")), oa(**kw))[1]
    for _ in range(8):                                           # 8 rapid Right presses
        pp._shift_freq(up=True)
    assert not srccalls and not ancalls                         # NOTHING retuned mid-burst (no ratchet)
    assert pp._apply_timer.isActive()                           # one apply pending
    pp._apply()                                                 # operator pauses -> the debounced apply
    pp.tx_engine.step_once(); pp.rx_engine.step_once()
    assert len(srccalls) == 1                                   # source retuned once, not eight
    assert abs(srccalls[-1] - pp.model.freq_hz) < 1.0
    # high band re-asserts the span after the preselector peak, so the analyzer may see >1
    # set_frequency; what matters is every one targets the FINAL point (no intermediate retunes leaked)
    assert ancalls and all(abs(a - pp.model.freq_hz) < 1.0 for a in ancalls)


# ---- no silent blank PSD: surface an unavailable analyzer/source ----------------------------

def test_point_op_surfaces_analyzer_unavailable_reason():
    b = _bench()
    pp = b.point
    pp.rx_engine.enqueue.__self__  # noqa: keep import path warm
    pp._rxq.put(("absent", "rx leased by session 280"))          # analyzer held by another session
    pp._drain(); pp.render()
    assert "RX ANALYZER UNAVAILABLE" in pp.big.text()
    assert "session 280" in pp.readout.text()                     # the operator sees WHY, not a blank
    # a fresh sweep clears it and normal readout returns
    pp.rx_model.set_trace([2.4999e9, 2.5e9, 2.5001e9], [-90.0, -8.0, -91.0])
    pp._rxq.put(("trace", [2.4999e9, 2.5e9, 2.5001e9], [-90.0, -8.0, -91.0]))
    pp._drain(); pp.render()
    assert "UNAVAILABLE" not in pp.big.text() and pp._rx_absent is None


def test_point_op_surfaces_source_unavailable_in_debug_pane():
    b = _bench()
    pp = b.point
    pp._txq.put(("absent", "tx power-cycle the adapter"))
    pp._drain(); pp.render()
    assert "TX 68367C:  UNAVAILABLE" in pp.debug.text() and "power-cycle" in pp.debug.text()


# ---- reading-validity indicator + gated reference capture ----------------------------------

def test_point_op_reading_status_classifies_tone_floor_and_offfreq():
    m = point_op_mode.PointOpModel()
    m.freq_hz = 5.0e9
    assert m.reading_status() == ("NO SWEEP", False)                 # nothing read yet
    m.set_reading(-8.0, 5.0e9, -80.0)                               # strong tone, on freq
    assert m.reading_status() == ("TONE OK", True)
    m.set_reading(-78.0, 5.0e9, -80.0)                             # only 2 dB over floor -> not a tone
    assert m.reading_status() == ("NO TONE", False)
    m.set_reading(-8.0, 5.010e9, -80.0)                            # strong peak but 10 MHz off point
    txt, ok = m.reading_status()
    assert ok is False and "OFF-FREQ" in txt


def test_point_op_settling_suppresses_offfreq_flash():
    # mid-retune the RX sweeps at the new CF ~0.6 s before the TX tone arrives; the transient must read
    # SETTLING, not the alarming OFF-FREQ, and must resume the real status once the TX settles.
    m = point_op_mode.PointOpModel()
    m.freq_hz = 5.0e9
    m.set_reading(-8.0, 5.010e9, -80.0)                            # a peak 10 MHz off -> would be OFF-FREQ
    m.set_settling(True)
    assert m.reading_status() == ("SETTLING", False)               # transient hidden, capture still refused
    m.set_settling(False)
    txt, ok = m.reading_status()
    assert "OFF-FREQ" in txt and ok is False                       # settled -> the real status is back


def test_point_op_apply_arms_settling_only_with_rf():
    b = _bench(); pp = b.point
    pp.chk_rf.setChecked(False)
    pp._apply()
    assert pp.model.settling is False                              # RF off -> no tone to settle, honest NO TONE
    pp.chk_rf.setChecked(True)
    pp._apply()
    assert pp.model.settling is True                               # RF on retune -> arm SETTLING


def test_point_op_settling_holds_until_a_fresh_sweep_lands_after_settled():
    # SETTLING must NOT clear on the TX 'settled' event alone -- the RX may still show the pre-retune
    # trace. It clears only when the next sweep lands (covers a slow feed that lags the settle by >1 s).
    b = _bench(); pp = b.point
    pp.chk_rf.setChecked(True)
    pp._apply()
    assert pp.model.settling is True
    pp._txq.put(("settled", True))
    pp._drain()
    assert pp.model.settling is True                               # settled alone does NOT clear it
    pp._rxq.put(("trace", [2.45e9], [-10.0]))
    pp._drain()
    assert pp.model.settling is False                             # the fresh post-settle sweep clears it


def test_point_op_button_retune_arms_settling_before_apply():
    # a preset/step/arrow retune (RF on) holds SETTLING from the button press, before the debounced _apply,
    # so the pre-apply window never flashes OFF-FREQ off the stale trace.
    b = _bench(); pp = b.point
    pp.chk_rf.setChecked(True); pp._apply()
    pp._txq.put(("settled", True)); pp._drain()                  # settled -> arm the clear
    pp._rxq.put(("trace", [2.45e9], [-10.0])); pp._drain()       # next sweep -> settling clears
    assert pp.model.settling is False                             # settled state reached
    pp._jump_to(2.44e9)
    assert pp.model.settling is True                              # button retune re-arms immediately


def test_point_op_edge_floor_detects_a_wide_tone_filling_a_narrow_span():
    """Regression (live-proven): a real tone that FILLS the narrow point span makes a median-of-span
    floor track the tone itself (floor ~= peak -> a real tone misreads as 'NO TONE' and reference
    capture is wrongly blocked). The edge-based floor reads the skirts (outer eighths) and reports
    TONE OK, so the substitution loop works when the operator narrows the span onto the tone."""
    b = _bench()
    pp = b.point
    pp.model.freq_hz = 2.45e9
    n = 601
    lo, hi = pp.model.span_lo_hz(), pp.model.span_hi_hz()
    freqs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    edge = n // 8
    levels = [-85.0 if (i < edge or i >= n - edge) else -6.0 for i in range(n)]  # tone fills the middle
    levels[n // 2] = -5.0                            # true peak exactly on the commanded point (on-freq)
    pp.rx_model.set_trace(freqs, levels)
    pp.render()
    assert pp.model.floor_dbm is not None and pp.model.floor_dbm <= -80.0   # skirts, not the -6 plateau
    assert pp.model.reading_status() == ("TONE OK", True)                   # median floor would say NO TONE
    pp._set_reference()
    assert pp.model.reference_dbm == -5.0


def test_point_op_reference_capture_gated_on_valid_reading():
    b = _bench()
    pp = b.point
    pp.model.freq_hz = 5.0e9
    pp.rx_model.set_trace([4.999e9, 5.0e9, 5.001e9], [-95.0, -90.0, -96.0])  # floor only, no tone
    pp.render(); pp._set_reference()
    assert pp.model.reference_dbm is None and "NOT captured" in pp._ref_msg   # refused off the floor
    pp.rx_model.set_trace([4.999e9, 5.0e9, 5.001e9], [-90.0, -6.0, -91.0])   # a real tone now
    pp.render(); pp._set_reference()
    assert pp.model.reference_dbm == -6.0 and "captured" in pp._ref_msg


def test_point_op_psd_has_labeled_tx_line_and_peak_label():
    b = _bench()
    pp = b.point
    pp.spin_power.setValue(-3.0); pp._apply()
    pp.model.freq_hz = 5.0e9
    pp.rx_model.set_trace([4.999e9, 5.0e9, 5.001e9], [-90.0, -7.0, -91.0])
    pp.render()
    assert abs(pp._tx_line.value() - (-3.0)) < 1e-6                  # TX line sits at the commanded power
    assert "peak -7.0 dBm" in pp._peak_label.toPlainText()          # peak value labeled at the marker


# ---- input debounce (continuous controls coalesce; arrows stay immediate) -------------------

def test_point_op_on_change_is_debounced_not_immediate():
    b = _bench()
    pp = b.point
    _drain_tx(pp.tx_engine)                                          # clear setup
    pp.spin_freq.setValue(3.0)                                       # a control edit -> debounced
    assert pp._apply_timer.isActive()                               # scheduled, not applied yet
    assert not any(c[0] == "apply" for c in list(pp.tx_engine._cmds.queue))
    pp._apply()                                                     # simulate the debounce firing
    assert any(c[0] == "apply" for c in _drain_tx(pp.tx_engine))


def test_point_op_arrow_updates_display_immediately_but_debounces_the_apply():
    """An arrow tap updates the model + on-screen control IMMEDIATELY (responsive tuning) but ARMS the
    debounce instead of retuning the hardware on the spot -- so a burst does not ratchet the source /
    blank the analyzer. The retune lands once the operator pauses (the debounce fires)."""
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(5.0); pp._apply(); _drain_tx(pp.tx_engine)
    pp._apply_timer.stop()
    pp._shift_freq(up=True)                                          # Right arrow
    assert abs(pp.model.freq_hz - 5.1e9) < 1e-3                    # model/display updated IMMEDIATELY
    assert pp.spin_freq.value() == pytest.approx(5.1)              # the control reflects it now
    assert pp._apply_timer.isActive()                             # instrument apply is DEBOUNCED (armed)
    assert not [c for c in _drain_tx(pp.tx_engine) if c[0] == "apply"]   # no retune until the pause


def test_point_op_psd_draws_reference_line_and_se_gap_when_referenced():
    b = _bench()
    pp = b.point
    pp.model.freq_hz = 5.0e9
    fr = [4.999e9, 5.0e9, 5.001e9]
    pp.rx_model.set_trace(fr, [-90.0, -6.0, -91.0]); pp.render()     # strong on-freq tone, no ref yet
    assert not pp._ref_line.isVisible() and pp._se_label.toPlainText() == ""
    pp._set_reference()                                             # reference = -6 dBm
    pp.rx_model.set_trace(fr, [-90.0, -54.0, -91.0]); pp.render()    # shield in -> weaker
    assert pp._ref_line.isVisible() and abs(pp._ref_line.value() - (-6.0)) < 1e-6
    assert "ref -6.0 dBm" in pp._ref_line.label.format               # label matches the value
    assert pp._se_label.toPlainText() == "SE 48 dB"                  # -6 - (-54)
    _xs, ys = pp._se_gap.getData()
    assert list(ys)[0] == -6.0 and list(ys)[-1] == -54.0            # gap spans reference -> peak
    pp._clear_reference(); pp.render()
    assert not pp._ref_line.isVisible() and pp._se_label.toPlainText() == ""


def test_point_op_pins_x_axis_to_the_sweep_window_not_zero():
    """The PSD x-axis must stay on [center - span/2, center + span/2] GHz. Otherwise pyqtgraph
    auto-ranges x to include the empty trace + the TextItems parked at (0,0), so once the real sweep
    arrives at ~GHz the view stretches from 0 and the window is squeezed off the right edge ('the axis
    scrolls right until it disappears on start')."""
    b = _bench()
    pp = b.point
    pp.spin_freq.setValue(5.0)           # 5 GHz
    pp.spin_span.setValue(10.0)          # 10 MHz span -> window [4.995, 5.005] GHz
    pp._apply()
    (x0, x1), _ = pp.plot.getViewBox().viewRange()
    assert x0 > 1.0                       # the view does NOT stretch back toward x=0
    assert abs(x0 - 4.995) < 2e-3         # center - span/2
    assert abs(x1 - 5.005) < 2e-3         # center + span/2
