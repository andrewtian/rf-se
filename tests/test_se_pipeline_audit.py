"""Hardware-free tests for the SE-pipeline methodology audit corrections (loop.py).

Covers the four measurement-correctness fixes made against the substitution-SE methodology brief:
  point 1  VBW <= RBW at every rung (the adaptive ladder narrows RBW; VBW must track down).
  point 2  identical ref-vs-wall analyzer settings -- recorded per row as a fingerprint and gated
           per index in summarize(); attenuation is APPLIED (was a dead config knob) so it is a
           real, matched setting rather than an implicit one.
  point 3  per-point noise floor + dynamic range logged in the CSV (the wall-pass floor was dropped).
  point 4  baseline-drift re-check (loop.reference_drift / Coordinator.recheck_reference).

All sim / spy, following test_band_trust + test_campaign_se patterns. No hardware, no Qt.

Run:  uv run python -m pytest rf-se/se299/tests/test_se_pipeline_audit.py -q
"""
from __future__ import annotations

import csv as _csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import control_plane
import drivers
import loop


# ------------------------------------------------------------------ spy analyzer

class _SettingsSpy(drivers.SimSpectrumAnalyzer):
    """Records every configure() and set_attenuation() so the identical-settings application
    (VBW<=RBW clamp + attenuation write) is asserted directly, while still simulating the read."""

    def __init__(self, bench):
        super().__init__(bench=bench)
        self.configure_calls = []          # (rbw, vbw, ref_level, detector)
        self.atten_calls = []              # db

    def configure(self, rbw_hz, vbw_hz, ref_dbm, detector):
        self.configure_calls.append((rbw_hz, vbw_hz, ref_dbm, detector))
        super().configure(rbw_hz, vbw_hz, ref_dbm, detector)

    def set_attenuation(self, db=None, auto=False):
        if db is not None:
            self.atten_calls.append(db)
        super().set_attenuation(db=db, auto=auto)


def _easy_cfg(analyzer=None, target=55.0):
    """One comfortable band (33 dBi @ 2-6 GHz) whose EA8 capability clears a low target with room
    to spare -> no point is floor-limited, so the SE recovers the injected model and PASSES."""
    band = cfg_mod.BandPlan("easy-2-6ghz-33dbi", 2e9, 6e9, 3, 33.0, 3.0, -143.0, target_se_db=target)
    kw = {"bands": (band,)}
    if analyzer is not None:
        kw["analyzer"] = analyzer
    return cfg_mod.Campaign(**kw)


# ------------------------------------------------------------------ point 1: VBW <= RBW

def test_vbw_is_clamped_to_rbw_through_the_adaptive_ladder():
    # a hard point (25 dBi @ 40 GHz ~= 96 dB @ 1 kHz) forces the ladder to narrow RBW to 100 Hz.
    hard = cfg_mod.BandPlan("hard-40ghz-25dbi", 40e9, 40e9, 1, 25.0, 3.0, -143.0)
    cfg = cfg_mod.Campaign(bands=(hard,))                 # default rbw_ladder_hz (1k, 100, 10) active
    src, _, bench = drivers.open_instruments(cfg)
    sa = _SettingsSpy(bench)
    reference = loop.acquire_reference(cfg, src, sa, bench)

    assert reference[0]["rbw_hz"] == 100.0               # the ladder narrowed exactly one rung
    # EVERY configure the ladder issued kept VBW <= RBW -- never a video BW wider than the res BW.
    assert sa.configure_calls, "expected the ladder to configure the analyzer"
    for rbw, vbw, _rl, _det in sa.configure_calls:
        assert vbw <= rbw, f"VBW {vbw} > RBW {rbw} (violates VBW<=RBW)"
    # and the narrowed rung actually tracked VBW down with it (not left pinned at the 1 kHz default)
    assert (100.0, 100.0, cfg.analyzer.ref_level_dbm, cfg.analyzer.detector) in sa.configure_calls


# ------------------------------------------------------------------ point 2: attenuation applied

def test_configured_attenuation_is_applied_and_recorded_not_a_dead_knob():
    cfg = _easy_cfg(analyzer=cfg_mod.AnalyzerSettings(attenuation_db=5.0))
    src, _, bench = drivers.open_instruments(cfg)
    sa = _SettingsSpy(bench)
    bench.se_model = lambda f: 70.0
    reference = loop.acquire_reference(cfg, src, sa, bench)
    wall = loop.measure_wall(cfg, src, sa, reference, bench)

    # the declared attenuation reached the instrument (previously configure() ignored it entirely)
    assert sa.atten_calls, "attenuation was never applied"
    assert all(db == 5.0 for db in sa.atten_calls)
    # and it is recorded in the per-row settings fingerprint on BOTH passes
    for i in reference:
        assert reference[i]["settings"]["attenuation_db"] == 5.0
        assert wall[i]["settings"]["attenuation_db"] == 5.0


# ------------------------------------------------------------------ point 2: identical-settings gate

def test_reference_and_wall_settings_fingerprints_match_and_gate_the_campaign():
    cfg = _easy_cfg()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 70.0
    reference = loop.acquire_reference(cfg, src, sa, bench)
    wall = loop.measure_wall(cfg, src, sa, reference, bench)

    keys = {"rbw_hz", "vbw_hz", "ref_level_dbm", "detector", "attenuation_db"}
    for i in reference:
        assert set(reference[i]["settings"]) == keys
        assert reference[i]["settings"] == wall[i]["settings"]    # identical per index
        assert reference[i]["settings"]["vbw_hz"] <= reference[i]["settings"]["rbw_hz"]

    summary = loop.summarize(reference, wall)
    assert summary["settings_symmetric"] is True
    assert summary["rbw_symmetric"] is True
    assert summary["campaign_pass"] is True


def test_asymmetric_settings_between_passes_fail_the_campaign():
    # the single most important invariant: a divergent setting between ref and wall is a fixed dB
    # offset masquerading as SE, so it must fail campaign_pass -- even if every verdict is PASS.
    cfg = _easy_cfg()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 70.0
    reference = loop.acquire_reference(cfg, src, sa, bench)
    wall = loop.measure_wall(cfg, src, sa, reference, bench)
    assert loop.summarize(reference, wall)["campaign_pass"] is True    # baseline: passes clean

    k = max(wall)                                        # tamper ONE wall point's attenuation
    wall[k] = dict(wall[k], settings=dict(wall[k]["settings"], attenuation_db=99.0))
    s = loop.summarize(reference, wall)
    assert s["settings_symmetric"] is False
    assert s["campaign_pass"] is False


# ------------------------------------------------------------------ point 3: floor + DR logged

def test_csv_logs_per_point_noise_floor_and_dynamic_range(tmp_path):
    cfg = _easy_cfg()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 70.0
    reference = loop.acquire_reference(cfg, src, sa, bench)
    wall = loop.measure_wall(cfg, src, sa, reference, bench)
    out = loop.write_run(str(tmp_path), cfg, reference, wall, loop.summarize(reference, wall))

    with open(os.path.join(out, "se_results.csv"), encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    assert rows
    for row in rows:
        assert "wall_floor_dbm" in row and "dr_db" in row       # both floors + DR now logged
        # DR = baseline - noise floor (the quantity the DR gate checks, methodology point 3).
        # abs=0.02: dr_db is formatted from the unrounded ref/floor, so it can differ from the
        # difference of the two independently-2dp-rounded CSV columns by up to one ULP each.
        assert float(row["dr_db"]) == pytest.approx(
            float(row["ref_dbm"]) - float(row["floor_dbm"]), abs=0.02)
        # the wall-pass floor is a real logged number (was previously dropped from the CSV)
        float(row["wall_floor_dbm"])


def test_floor_limited_point_is_flagged_as_lower_bound_with_floor_logged():
    # DR-limited flagging (methodology point 3): an SE deeper than the setup can see -> the wall
    # sits at the floor -> reported as a LOWER BOUND (SE >= capability), never a fabricated value,
    # with the noise floor recorded on the row.
    cfg = _easy_cfg(target=55.0)
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 300.0                     # far beyond the setup's dynamic range
    reference = loop.acquire_reference(cfg, src, sa, bench)
    wall = loop.measure_wall(cfg, src, sa, reference, bench)
    for i, m in wall.items():
        assert m["floor_limited"] is True
        assert m["se_reported_db"] == pytest.approx(reference[i]["capability_db"], abs=0.01)
        assert m["se_reported_db"] < 300.0              # a lower bound, not the injected model
        assert "wall_floor_dbm" in m                    # noise floor logged per point


# ------------------------------------------------------------------ point 4: baseline-drift re-check

def test_reference_drift_stable_when_the_baseline_holds():
    cfg = _easy_cfg()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 70.0
    reference = loop.acquire_reference(cfg, src, sa, bench)

    drift = loop.reference_drift(cfg, src, sa, reference, bench=bench, tol_db=3.0)
    assert drift["verdict"] == "STABLE"
    assert drift["drift_ok"] is True
    assert drift["max_abs_drift_db"] < 0.5              # deterministic sim re-read -> ~0 drift
    assert all(r["within_tol"] for r in drift["rows"])
    assert drift["n"] == len(cfg.frequencies())


def test_reference_drift_flags_a_baseline_that_moved_beyond_tolerance():
    cfg = _easy_cfg()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 70.0
    reference = loop.acquire_reference(cfg, src, sa, bench)

    bench.separation_m *= 2.0                           # +6 dB FSPL -> the reference drops ~6 dB
    drift = loop.reference_drift(cfg, src, sa, reference, bench=bench, tol_db=3.0)
    assert drift["verdict"] == "DRIFTED"
    assert drift["drift_ok"] is False
    assert drift["max_abs_drift_db"] > 3.0
    for r in drift["rows"]:
        assert r["drift_db"] < -3.0                     # dropped, not risen
        assert r["within_tol"] is False


def test_coordinator_recheck_reference_runs_over_the_pair():
    cfg = _easy_cfg()
    cp = control_plane.simulated(cfg)
    cp.bench.se_model = lambda f: 70.0
    coord = cp.make_coordinator()
    reference = coord.acquire_reference(bench=cp.bench)

    drift = coord.recheck_reference(reference, bench=cp.bench, tol_db=3.0)
    assert drift["verdict"] == "STABLE"
    assert drift["drift_ok"] is True


# ---------------------------------------------------------------- check_path pre-gate (coordinator)

def test_run_campaign_pre_check_path_passes_on_a_live_path_sim():
    # the sim couples (TX tone rises above the RX floor), so the pre-gate is PATH-LIVE and the
    # campaign runs to completion exactly as without the gate.
    cfg = _easy_cfg()
    cp = control_plane.simulated(cfg)
    cp.bench.se_model = lambda f: 70.0
    coord = cp.make_coordinator()
    result = coord.run_campaign(bench=cp.bench, pre_check_path=True)
    assert result["summary"]["campaign_pass"] is True


def test_run_campaign_pre_check_path_aborts_before_reference_on_a_dead_path(monkeypatch):
    # force the pre-gate to see NO-COUPLING -> the campaign must raise PathNotLive BEFORE acquiring
    # a reference (a dead path otherwise reports SE ~= 0 as infinite shielding).
    import coordinator as coord_mod
    cfg = _easy_cfg()
    cp = control_plane.simulated(cfg)
    cp.bench.se_model = lambda f: 70.0
    coord = cp.make_coordinator()
    acquired = {"ran": False}
    real_acq = coord.acquire_reference
    monkeypatch.setattr(coord, "acquire_reference",
                        lambda *a, **k: acquired.__setitem__("ran", True) or real_acq(*a, **k))
    monkeypatch.setattr(loop, "check_path", lambda *a, **k: {
        "verdict": "NO-COUPLING", "n_couple": 0, "n": 3, "max_ambient_dbm": -52.0, "rows": []})
    with pytest.raises(coord_mod.PathNotLive):
        coord.run_campaign(bench=cp.bench, pre_check_path=True)
    assert acquired["ran"] is False              # aborted BEFORE the reference pass
