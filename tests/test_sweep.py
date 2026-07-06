"""Hardware-free tests for the canonical 8565EC sweep mode + measurement-integrity
guardrails + cavity Q.

Two layers:
  1. FakeTransport pins the exact GPIB command STRINGS the Agilent856xEC driver emits
     (closes the pragma:no-cover gap; the mnemonics themselves are confirmed against
     the HP 8560 manual, reference/operator-manuals/hp-8560-e-series-programming.md).
  2. The simulator exercises the sweep/floor/detector/averaging/Q physics and every
     integrity guard, so a screening sweep can never certify a leaky enclosure.

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_sweep.py -q
"""
from __future__ import annotations

import math
import os
import statistics
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import connection as conn
import discover as disc
import drivers
import loop
import probe_sweep as ps


# ============================================================ FakeTransport (strings)

class FakeTransport:
    """Records writes and scripts query replies -- lets us assert the exact command
    strings the real Agilent856xEC driver would put on the bus, with no hardware."""

    def __init__(self, replies=None):
        self.writes = []
        self.queries = []
        self.timeout_ms = None
        self._replies = dict(replies or {})

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.queries.append(cmd)
        if cmd in self._replies:
            return self._replies[cmd]
        for k, v in self._replies.items():
            if cmd.startswith(k):
                return v
        return "0"

    def set_timeout(self, ms):
        self.timeout_ms = ms

    def close(self):
        pass


def _drv(replies=None):
    return drivers.Agilent856xEC(FakeTransport(replies))


def test_856x_set_frequency_start_stop_strings():
    a = _drv()
    a.set_frequency(start_hz=1e9, stop_hz=6e9)
    assert "FA 1000000000HZ" in a.t.writes
    assert "FB 6000000000HZ" in a.t.writes


def test_856x_set_frequency_center_span_strings():
    a = _drv()
    a.set_frequency(center_hz=10e9, span_hz=0)
    assert "CF 10000000000HZ" in a.t.writes
    assert "SP 0HZ" in a.t.writes


def test_856x_set_frequency_rejects_mixed_forms():
    a = _drv()
    with pytest.raises(ValueError):
        a.set_frequency(center_hz=1e9, start_hz=2e9)


def test_856x_arm_and_wait_single_take_done_and_timeout():
    a = _drv()
    a.arm_and_wait(timeout_s=2.0)
    # CONTS + TWO sweeps: a single-sweep SNGLS+TS reads a STALE trace over the networked GPIB bridge
    # (live-proven); the first TS flushes the one-behind trace, the second is the fresh one read_trace
    # reads.
    assert a.t.writes == ["CONTS", "TS", "TS"]
    assert "DONE?" in a.t.queries
    assert a.t.timeout_ms == 2000


def test_856x_read_trace_tdf_p_then_tra_axis_from_instrument():
    # the 8565EC trace is ALWAYS 601 points; the axis is rebuilt from FA?/FB? (not cached args). A short
    # (truncated/desynced) ASCII reply is now rejected by the read path (gate C7), so the fixture is a
    # full 601-point trace -- which is what the instrument actually returns.
    tra = ",".join(f"{-90.0 + i * 0.01:.2f}" for i in range(601))
    a = _drv({"TRA?": tra, "FA?": "1000000000", "FB?": "6000000000"})
    freqs, levels = a.read_trace("A")
    assert "TDF P" in a.t.writes
    assert "TRA?" in a.t.queries and "FA?" in a.t.queries and "FB?" in a.t.queries
    assert len(levels) == 601 and levels[0] == -90.0 and levels[-1] == pytest.approx(-84.0)
    assert freqs[0] == pytest.approx(1e9) and freqs[-1] == pytest.approx(6e9)


def test_856x_param_command_strings():
    a = _drv()
    a.set_detector("SMP");             assert "DET SMP" in a.t.writes
    a.set_attenuation(db=10);          assert "AT 10DB" in a.t.writes
    a.set_attenuation(auto=True);      assert "AT AUTO" in a.t.writes
    a.set_amplitude_units("DBM");      assert "AUNITS DBM" in a.t.writes
    a.set_video_average(16);           assert "VAVG 16" in a.t.writes
    a.set_video_average(0);            assert "VAVG OFF" in a.t.writes
    a.set_max_hold(True);              assert "MXMH TRA" in a.t.writes
    a.set_max_hold(False);             assert "CLRW TRA" in a.t.writes
    a.set_sweep_time(auto=True);       assert "ST AUTO" in a.t.writes
    a.set_sweep_time(seconds=0.5);     assert "ST 0.500000SC" in a.t.writes


def test_856x_save_recall_use_SAVES_RCLS():
    # the 8560 uses SAVES/RCLS, NOT the 8566/8568 SAV/RCL
    a = _drv()
    a.save_state(3);   assert "SAVES 3" in a.t.writes
    a.recall_state(3); assert "RCLS 3" in a.t.writes


def test_856x_query_options_parses_ID_not_OPT():
    # the 8560 has no *OPT?; options come from the ID? reply
    a = _drv({"ID?": "HP8565E,006,008"})
    opts = a.query_options()
    assert opts == ("006", "008")
    assert "*OPT?" not in a.t.queries and "ID?" in a.t.queries


def test_856x_marker_bandwidth_native_uses_negative_query():
    # native MKBW form is "MKBW -N,?" -> Hz (no DB suffix, no standalone MKBW?)
    a = _drv({"MKBW -3,?": "2000000"})
    bw = a.marker_bandwidth(n_db=3.0, from_trace=False)
    assert bw == pytest.approx(2e6)
    assert "MKPK HI" in a.t.writes
    assert any(q.startswith("MKBW -3,?") for q in a.t.queries)


# ============================================================ simulator behavior

def _se_analyzer(cfg=None):
    cfg = cfg or cfg_mod.default()
    return drivers.open_instruments(cfg)  # (src, analyzer, bench)


def test_sim_set_frequency_center_span_equals_start_stop():
    _, a, _ = _se_analyzer()
    a.set_frequency(center_hz=3.5e9, span_hz=5e9)
    lo1, hi1 = a.span_lo_hz, a.span_hi_hz
    a.set_frequency(start_hz=1e9, stop_hz=6e9)
    assert (lo1, hi1) == pytest.approx((a.span_lo_hz, a.span_hi_hz))


def test_sim_set_frequency_rejects_mixed():
    _, a, _ = _se_analyzer()
    with pytest.raises(ValueError):
        a.set_frequency(center_hz=1e9, stop_hz=2e9)


def test_sim_read_trace_axis_reconstructed_from_span():
    a = drivers.SimSpectrumAnalyzer(nf_model=drivers.demo_nearfield_spectrum())
    a.set_frequency(start_hz=1e9, stop_hz=6e9)
    freqs, levels = a.read_trace("A")
    assert len(freqs) == 601 == len(levels)
    assert freqs[0] == pytest.approx(1e9) and freqs[-1] == pytest.approx(6e9)


def test_attenuation_cancels_in_se_ratio():
    cfg = cfg_mod.default()

    def se_of(atten):
        src, sa, bench = drivers.open_instruments(cfg)
        sa.set_attenuation(db=atten)
        reference, wall = loop.run_demo(cfg, src, sa, bench)
        return {i: wall[i]["se_db"] for i in wall}

    se0, se10 = se_of(0.0), se_of(10.0)
    for i in se0:
        assert se0[i] == pytest.approx(se10[i], abs=1e-6)


def test_amplitude_units_roundtrip():
    _, a, _ = _se_analyzer()
    a.set_amplitude_units("DBUV")
    assert a.aunits == "DBUV"


def test_sample_detector_floor_below_positive_peak():
    _, a, bench = _se_analyzer()
    bench.src_rf_on = False
    bench.gain, bench.danl = 33.0, -143.0
    a.set_frequency(start_hz=26.5e9, stop_hz=40e9)
    a.set_detector("POS"); _, pos = a.read_trace()
    a.set_detector("SMP"); _, smp = a.read_trace()
    assert statistics.mean(smp) < statistics.mean(pos) - 1.0   # ~2.5 dB peak bias


def test_video_average_reduces_noise_variance():
    _, a, bench = _se_analyzer()
    bench.src_rf_on = False
    bench.gain, bench.danl = 33.0, -143.0
    a.set_frequency(start_hz=1e9, stop_hz=2e9)
    a.set_video_average(0);   _, raw = a.read_trace()
    a.set_video_average(100); _, avg = a.read_trace()
    assert statistics.pstdev(avg) < statistics.pstdev(raw)


def test_max_hold_accumulates_across_sweeps():
    _, a, bench = _se_analyzer()
    bench.src_rf_on = False
    bench.gain, bench.danl = 33.0, -143.0
    a.set_frequency(start_hz=1e9, stop_hz=2e9)
    a.set_max_hold(True)
    _, first = a.read_trace()
    held = first
    for _ in range(25):
        _, held = a.read_trace()
    assert all(h >= f - 1e-9 for h, f in zip(held, first))     # running max never drops


def test_stepped_cw_sees_deeper_floor_than_swept_span():
    _, a, bench = _se_analyzer()
    bench.src_rf_on = False
    bench.gain, bench.danl = 33.0, -143.0
    a.set_frequency(start_hz=30e9, stop_hz=40e9)
    bench.rbw_hz = 10.0;  _, narrow = a.read_trace()           # stepped acceptance RBW
    bench.rbw_hz = 1e6;   _, wide = a.read_trace()             # swept-span RBW
    assert statistics.mean(narrow) < statistics.mean(wide) - 40


# ============================================================ integrity guardrails

def test_off_grid_notch_missed_by_swept_but_caught_by_stepped():
    """A narrow leak between the 601 swept points is invisible to a swept trace but a
    stepped-CW dwell placed AT the leak frequency catches it (FM2 aliasing guard)."""
    cfg = cfg_mod.default()
    f_notch = 35.005e9                                          # between the swept grid points

    def se(f):
        leak = 70.0 * math.exp(-((f - f_notch) ** 2) / (2 * (0.5e6) ** 2))
        return 130.0 - leak                                    # dips to 60 dB at the notch

    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = se
    bench.wall_present = True
    bench.src_rf_on = True
    bench.gain, bench.danl = 33.0, -143.0
    bench.rbw_hz = 1e3
    sa.set_frequency(start_hz=30e9, stop_hz=40e9)
    _, swept = sa.read_trace()
    swept_hot = max(swept)

    src2, sa2, bench2 = drivers.open_instruments(cfg)
    bench2.se_model = se
    bench2.gain, bench2.danl = 33.0, -143.0
    frame = loop.stepped_cw_sweep(cfg, src2, sa2, [34.9e9, f_notch, 35.1e9], bench=bench2)
    assert frame["hot_freq_hz"] == pytest.approx(f_notch)
    assert frame["hot_level_dbm"] > swept_hot + 10             # stepped catches, swept misses


def test_swept_screen_rejects_span_too_coarse_for_notch():
    _, sa, _ = _se_analyzer()
    sweep = cfg_mod.SweepSettings(mode="swept", span_lo_hz=30e9, span_hi_hz=40e9, n_points=601)
    with pytest.raises(loop.AcquisitionRejected):
        loop.swept_screen(sa, sweep, expect_points=601, expected_notch_hz=1e6)


def test_swept_screen_rejects_non_dbm_units():
    _, sa, _ = _se_analyzer()
    sweep = cfg_mod.SweepSettings(mode="swept", span_lo_hz=1e9, span_hi_hz=6e9, aunits="V")
    with pytest.raises(loop.AcquisitionRejected):
        loop.swept_screen(sa, sweep)


def test_swept_screen_rejects_uncal():
    _, sa, _ = _se_analyzer()
    sa.uncal = True
    sweep = cfg_mod.SweepSettings(mode="swept", span_lo_hz=1e9, span_hi_hz=6e9)
    with pytest.raises(loop.AcquisitionRejected):
        loop.swept_screen(sa, sweep)


def test_swept_screen_valid_frame_is_labeled_screening():
    _, sa, _ = _se_analyzer()
    sweep = cfg_mod.SweepSettings(mode="swept", span_lo_hz=1e9, span_hi_hz=6e9)
    frame = loop.swept_screen(sa, sweep)
    assert frame["acq_mode"] == "swept-span" and frame["purpose"] == "screening"
    assert len(frame["levels_dbm"]) == 601


def test_screen_row_can_never_make_campaign_pass():
    cfg = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(rbw_hz=10.0))
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 105.0
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    assert loop.summarize(reference, wall)["campaign_pass"] is True
    k = max(wall)                                              # inject a screening row
    wall[k] = dict(wall[k], acq_mode="swept-span", purpose="screening", verdict="SCREEN-CLEAR")
    assert loop.summarize(reference, wall)["campaign_pass"] is False


def test_measure_wall_rejects_reference_axis_mismatch():
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    reference = loop.acquire_reference(cfg, src, sa, bench)
    k = min(reference)
    reference[k]["f_hz"] = reference[k]["f_hz"] + 5e9          # shift the reference axis
    with pytest.raises(loop.AcquisitionRejected):
        loop.measure_wall(cfg, src, sa, reference, bench)


#  C3: adaptive RBW ladder ----------------------------------------------------------------

def test_adaptive_rbw_ladder_recovers_ea8_and_stays_symmetric_with_wall():
    # a point that is EA8-limited at 1 kHz (25 dBi horn pair, 40 GHz -> ~96 dB capability,
    # doc 159 sec 4.2b; not ea8_ok when the ladder is pinned off, see test_se299's
    # test_top_band_fails_ea8_at_40ghz_with_25dbi) auto-narrows to the next ladder rung
    # (100 Hz -> +10 dB, "RBW is free gain") until it clears the 100 dB target -- while a
    # comfortable point (plenty of margin already at 1 kHz) is left untouched.
    easy = cfg_mod.BandPlan("easy-5ghz-33dbi", 5e9, 5e9, 1, 33.0, 3.0, -143.0)
    hard = cfg_mod.BandPlan("hard-40ghz-25dbi", 40e9, 40e9, 1, 25.0, 3.0, -143.0)
    cfg = cfg_mod.Campaign(bands=(easy, hard))                    # default rbw_ladder_hz active
    src, sa, bench = drivers.open_instruments(cfg)
    reference = loop.acquire_reference(cfg, src, sa, bench)
    easy_row, hard_row = reference[0], reference[1]
    assert easy_row["rbw_hz"] == 1000.0                # comfortable point: stays at the default
    assert easy_row["ea8_ok"] is True
    assert hard_row["rbw_hz"] == 100.0                 # narrowed exactly one rung, no further
    assert hard_row["capability_db"] > 96.0            # improved vs the raw (ladder-off) 1 kHz number
    assert hard_row["ea8_ok"] is True                  # ...and now clears the target
    assert all("rbw_hz" in r for r in reference.values())

    wall = loop.measure_wall(cfg, src, sa, reference, bench)
    # SYMMETRIC PER INDEX (the critical invariant): the wall pass reads each point back at the
    # SAME rbw the reference pass ended on for that index -- never one global RBW for every point.
    for i in reference:
        assert wall[i]["rbw_hz"] == reference[i]["rbw_hz"]
    assert wall[0]["rbw_hz"] == 1000.0
    assert wall[1]["rbw_hz"] == 100.0

    summary = loop.summarize(reference, wall)
    assert summary["rbw_symmetric"] is True            # new key; doesn't disturb the rest
    assert "campaign_pass" in summary                  # schema intact (only keys ADDED)


def test_every_ref_and_wall_row_carries_rbw_hz_and_stays_symmetric_on_default_campaign():
    # integration check over the REAL default band plan (a mix of comfortable and EA8-limited
    # points, per test_se299's test_midband_is_tight_at_1khz_finding_4_2b): every row carries
    # rbw_hz, and it matches per index between the reference and wall passes.
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    assert all("rbw_hz" in r for r in reference.values())
    assert all("rbw_hz" in r for r in wall.values())
    for i in reference:
        assert reference[i]["rbw_hz"] == wall[i]["rbw_hz"]
    assert loop.summarize(reference, wall)["rbw_symmetric"] is True


def test_ladder_pinned_single_rung_disables_auto_narrowing():
    # overriding rbw_hz alone (without touching rbw_ladder_hz) still auto-narrows via the
    # default ladder's entries below it; pinning rbw_ladder_hz to just (rbw_hz,) is how a
    # caller opts OUT of adaptation entirely (used by test_se299's raw-capability tests).
    hard = cfg_mod.BandPlan("hard-40ghz-25dbi", 40e9, 40e9, 1, 25.0, 3.0, -143.0)
    cfg = cfg_mod.Campaign(bands=(hard,),
                           analyzer=cfg_mod.AnalyzerSettings(rbw_ladder_hz=(1000.0,)))
    src, sa, bench = drivers.open_instruments(cfg)
    reference = loop.acquire_reference(cfg, src, sa, bench)
    assert reference[0]["rbw_hz"] == 1000.0
    assert reference[0]["ea8_ok"] is False              # never narrowed -> stays EA8-limited


def test_settings_key_includes_attenuation_and_units_but_keeps_legacy_prefix():
    a = cfg_mod.default()
    b = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(attenuation_db=10.0))
    c = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(aunits="DBUV"))
    assert a.settings_key() != b.settings_key()
    assert a.settings_key() != c.settings_key()
    assert a.settings_key()[:5] == b.settings_key()[:5]        # legacy [:5] slice preserved


def test_sim_query_options_reports_owned_unit():
    _, sa, _ = _se_analyzer()
    opts = sa.query_options()
    assert "006" in opts and "008" in opts and "002" not in opts


def test_sim_save_recall_state_roundtrip():
    _, sa, _ = _se_analyzer()
    sa.set_detector("POS"); sa.set_attenuation(db=10); sa.set_video_average(4)
    sa.save_state(1)
    sa.set_detector("SMP"); sa.set_attenuation(db=30); sa.set_video_average(0)
    sa.recall_state(1)
    assert sa.detector == "POS" and sa.atten_db == 10 and sa.video_avg == 4


# ============================================================ cavity Q

def test_bandwidth_from_trace_matches_lorentzian_width():
    f0, q = 10e9, 2000.0
    model = drivers.demo_cavity_resonance(f0, q)
    lo, hi, n = f0 - 50e6, f0 + 50e6, 601
    freqs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    levels = [model(f) for f in freqs]
    assert drivers.bandwidth_from_trace(freqs, levels, 3.0) == pytest.approx(f0 / q, rel=0.05)


def test_cavity_q_recovers_known_q():
    _, sa, bench = _se_analyzer()
    f0, q = 10e9, 5000.0
    bench.resonance = drivers.demo_cavity_resonance(f0, q)
    cav = cfg_mod.CavitySettings(span_lo_hz=f0 - 20e6, span_hi_hz=f0 + 20e6,
                                 n_points=601, n_db_down=3.0, video_avg=16)
    res = loop.cavity_q(sa, cav, bench=bench)
    assert res["f0_hz"] == pytest.approx(f0, abs=1e6)
    assert res["q"] == pytest.approx(q, rel=0.2)


def test_composite_q_formula():
    lam = 299_792_458.0 / 10e9
    expect = 16 * math.pi ** 2 * 32.0 * 1e-3 / lam ** 3
    assert loop.composite_q(32.0, 1e-3, 10e9) == pytest.approx(expect, rel=1e-9)


# ============================================================ stepped-path plumbing

def _sim_se_link(span=(1e9, 6e9)):
    """An AnalyzerLink whose analyzer carries a bench (for read_via / stepped path)."""
    cfg = cfg_mod.default()
    _, _, bench = drivers.open_instruments(cfg)
    return conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=span,
        discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSpectrumAnalyzer(bench=bench), retries=3), bench


def test_read_via_runs_under_link_envelope():
    link, _ = _sim_se_link()
    link.connect()
    opts = link.read_via(lambda a: a.query_options())
    assert "008" in opts


def test_read_via_requires_ready():
    link = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9),
        discover_fn=lambda: [], open_fn=lambda dev: None, retries=1)
    link.connect()                                             # ABSENT
    with pytest.raises(conn.LinkNotReady):
        link.read_via(lambda a: a.query_options())


class _FlakyPoint:
    """A sim analyzer whose FIRST measure_peak raises (a dropped link)."""

    def __init__(self, state):
        self.state = state

    def idn(self):
        return "SIM,8565E-flaky,0,sim"

    def measure_peak(self, f_hz, settle_s=0.0):
        if self.state["drop_once"]:
            self.state["drop_once"] = False
            raise IOError("simulated GPIB drop")
        return (f_hz, -70.0)

    def sweep_trace(self, lo, hi, n, settle_s=0.0):
        return ([lo], [-70.0])

    def close(self):
        pass


def test_read_point_auto_reconnects_after_drop():
    state = {"drop_once": True}
    link = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9),
        discover_fn=disc.sim_inventory,
        open_fn=lambda dev: _FlakyPoint(state), retries=3)
    link.ensure()
    with pytest.raises(conn.LinkDropped):                      # first read drops the link
        link.read_point(10e9)
    assert link.ensure()                                      # reconnects transparently
    mkf, amp = link.read_point(10e9)
    assert amp == pytest.approx(-70.0)


def test_probe_sweeper_stepped_mode_uses_acquire_fn():
    link, _ = _sim_se_link()
    steps = [1e9, 2e9, 3e9]

    def acquire(analyzer):
        levels = [analyzer.measure_peak(f, 0.0)[1] for f in steps]
        return (steps, levels)

    sweeper = ps.ProbeSweeper(link, span=(1e9, 3e9), acquire_fn=acquire)
    frames = sweeper.run(sweeps=2)
    assert len(frames) == 2
    assert len(frames[-1].levels) == 3
    assert frames[-1].status.valid is True


# ============================================================ source-tracks-sweep

def test_stepped_sweep_frame_is_source_tracked():
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    frame = loop.stepped_cw_sweep(cfg, src, sa, [1e9, 2e9], bench=bench)
    assert frame["source_tracked"] is True and frame["tracking"] == "software-lockstep"


def test_swept_screen_frame_is_not_source_tracked():
    _, sa, _ = _se_analyzer()
    frame = loop.swept_screen(sa, cfg_mod.SweepSettings(mode="swept", span_lo_hz=1e9,
                                                        span_hi_hz=6e9))
    assert frame["source_tracked"] is False


def test_require_source_tracked_rejects_untracked_screen():
    _, sa, _ = _se_analyzer()
    screen = loop.swept_screen(sa, cfg_mod.SweepSettings(mode="swept", span_lo_hz=1e9,
                                                         span_hi_hz=6e9))
    with pytest.raises(loop.AcquisitionRejected):
        loop.require_source_tracked(screen)


def test_require_source_tracked_accepts_tracked_sweep():
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    frame = loop.tracked_sweep(cfg, src, sa, [1e9, 2e9], bench=bench)
    assert loop.require_source_tracked(frame) is frame


def test_summarize_requires_source_tracked_rows():
    cfg = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(rbw_hz=10.0))
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 105.0
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    assert loop.summarize(reference, wall)["campaign_pass"] is True
    k = max(wall)
    wall[k] = dict(wall[k], source_tracked=False)         # source did not track -> cannot pass
    assert loop.summarize(reference, wall)["campaign_pass"] is False


def test_tracked_sweep_software_delegates_to_stepped():
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    frame = loop.tracked_sweep(cfg, src, sa, [1e9, 2e9, 3e9], bench=bench)
    assert frame["source_tracked"] is True and frame["tracking"] == "software-lockstep"


def test_tracked_sweep_hardware_loads_list_and_tracks():
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    freqs = [2e9, 3e9, 4e9]
    frame = loop.tracked_sweep(cfg, src, sa, freqs, bench=bench, hardware=True)
    assert frame["source_tracked"] is True and frame["tracking"] == "hardware-list-sweep"
    assert bench.sweep_list == freqs                      # the source loaded the list sweep
    assert len(frame["levels_dbm"]) == 3


def test_68369_list_sweep_command_strings():
    # native 68000-series list sweep, confirmed vs Anritsu MG369xB GPIB PM (10370-10366).
    # Guards against regressing to the wrong LSP/DWL SC/SWP LST/TRG EXT/*TRG mnemonics.
    t = FakeTransport()
    sg = drivers.Anritsu68369(t)
    sg.set_list_sweep([1e9, 2e9], dwell_s=0.01)
    sg.arm_sweep()
    sg.trigger_point()
    assert "LST" in t.writes and "ELN1" in t.writes and "ELI0" in t.writes
    assert any(w.startswith("LF ") for w in t.writes)         # LF loads the frequency list
    assert "LDT 10.000 MS" in t.writes                        # dwell in MS (0.01 s), not SC
    assert "LEA" in t.writes and "LIB0" in t.writes and "LIE1" in t.writes
    assert "MNT" in t.writes                                  # manual-step trigger mode
    assert "RSS" in t.writes                                  # arm = reset to start index
    assert "UP" in t.writes                                   # per-point advance (native, not *TRG)
    # the wrong/unsupported mnemonics must NOT appear
    for bad in ("LSP", "SWP LST", "SWP ARM", "TRG EXT", "*TRG"):
        assert not any(bad in w for w in t.writes), bad
