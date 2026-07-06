"""Hardware-free regression tests for the IEEE-299 substitution SE automation.

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_se299.py -q
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import budget
import config as cfg_mod
import drivers
import loop


# ----------------------------------------------------------------- budget

def test_fspl_40ghz_0p6m():
    # doc 159 sec 4.2: FSPL(40 GHz, 0.6 m) ~= 60 dB
    assert budget.fspl_db(40e9, 0.6) == pytest.approx(60.0, abs=0.3)


def test_capability_matches_doc159_4_2b():
    # +3 dBm @ 40 GHz, 0.6 m, DANL -143, RBW 1 kHz, margin 10:
    #   33 dBi -> ~112 dB (no LNA);  25 dBi -> ~96 dB (needs LNA)
    cap33 = budget.se_capability_db(3, 33, 33, 40e9, 0.6, -143, 1000, 10)
    cap25 = budget.se_capability_db(3, 25, 25, 40e9, 0.6, -143, 1000, 10)
    assert cap33 == pytest.approx(112.0, abs=1.0)
    assert cap25 == pytest.approx(96.0, abs=1.0)
    assert cap33 - cap25 == pytest.approx(16.0, abs=0.1)   # +8 dBi/horn x2


def test_rbw_is_free_gain():
    # narrowing RBW one decade buys +10 dB capability
    hi = budget.se_capability_db(3, 25, 25, 40e9, 0.6, -143, 1000, 10)
    lo = budget.se_capability_db(3, 25, 25, 40e9, 0.6, -143, 100, 10)
    assert lo - hi == pytest.approx(10.0, abs=0.01)


# ----------------------------------------------------------------- config

def test_frequencies_cover_all_bands_in_range():
    cfg = cfg_mod.default()
    freqs = cfg.frequencies()
    assert len(freqs) == sum(b.n_points for b in cfg.bands)
    for f, b in freqs:
        assert b.f_lo_hz - 1 <= f <= b.f_hi_hz + 1
    xs = [f for f, _ in freqs]
    assert xs == sorted(xs)                                # monotonic


def test_settings_key_changes_with_rbw():
    a = cfg_mod.default()
    b = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(rbw_hz=100.0))
    assert a.settings_key() != b.settings_key()


# --------------------------------------------------- EA8 gate (PC6) via sim

def test_top_band_passes_ea8_with_33dbi():
    # the elite 33 dBi WR-28 pair clears 100 dB no-LNA across the whole 26.5-40 band
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    ref = loop.acquire_reference(cfg, src, sa, bench)
    top_band = [r for r in ref.values() if r["f_hz"] >= 26.5e9]
    assert top_band and all(r["ea8_ok"] for r in top_band)
    assert min(r["capability_db"] for r in top_band) >= 100.0


def test_top_band_fails_ea8_at_40ghz_with_25dbi():
    # pin the ladder to a SINGLE rung (1 kHz only) to isolate the raw, non-adaptive capability
    # this test documents -- the C3 adaptive ladder (default rbw_ladder_hz) is designed to
    # RECOVER exactly this shortfall by narrowing RBW; see
    # test_sweep.test_adaptive_rbw_ladder_recovers_ea8_and_stays_symmetric_with_wall for that.
    bands = list(cfg_mod.DEFAULT_BANDS)
    bands[-1] = cfg_mod.WR28_STANDARD_25DBI
    cfg = cfg_mod.Campaign(bands=tuple(bands),
                           analyzer=cfg_mod.AnalyzerSettings(rbw_ladder_hz=(1000.0,)))
    src, sa, bench = drivers.open_instruments(cfg)
    ref = loop.acquire_reference(cfg, src, sa, bench)
    top = max(ref)                                         # last index = ~40 GHz
    assert ref[top]["f_hz"] == pytest.approx(40e9, rel=1e-6)
    assert ref[top]["rbw_hz"] == 1000.0                    # ladder pinned off: stayed at 1 kHz
    assert not ref[top]["ea8_ok"]                          # 25 dBi cannot see 100 dB at 1 kHz
    assert ref[top]["capability_db"] == pytest.approx(96.0, abs=2.0)


def test_midband_is_tight_at_1khz_finding_4_2b():
    # doc 159 sec 4.2b finding #3: the 1-18 GHz band (low-gain broadband horns)
    # is ALSO below the 100 dB line at 1 kHz -- and the 18-40 LNA cannot help there.
    # Ladder pinned to a single rung: the C3 adaptive ladder (on by default) narrows exactly
    # this shortfall away, which would otherwise falsify the "tight at 1 kHz" premise here.
    cfg = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(rbw_ladder_hz=(1000.0,)))
    src, sa, bench = drivers.open_instruments(cfg)
    ref = loop.acquire_reference(cfg, src, sa, bench)
    band1 = [r for r in ref.values() if r["band"].startswith("1-18")]
    assert any(not r["ea8_ok"] for r in band1)             # tight at the top of the band
    assert all(r["rbw_hz"] == 1000.0 for r in band1)       # ladder pinned off: stayed at 1 kHz


def test_narrow_rbw_clears_every_band():
    # RBW is the free lever (+10 dB/decade): 10 Hz clears all bands incl. the mid-band
    cfg = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(rbw_hz=10.0))
    src, sa, bench = drivers.open_instruments(cfg)
    ref = loop.acquire_reference(cfg, src, sa, bench)
    assert all(r["ea8_ok"] for r in ref.values())


# --------------------------------------------------- SE compute via sim

def test_demo_se_tracks_the_model_where_not_floor_limited():
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    se_truth = drivers.demo_enclosure_se()
    bench.se_model = se_truth
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    checked = 0
    for i, m in wall.items():
        if not m["floor_limited"]:                         # measurable: SE == truth
            assert m["se_db"] == pytest.approx(se_truth(m["f_hz"]), abs=1.5)
            checked += 1
    assert checked >= 1                                    # the door-seal notch is measurable


def test_floor_limited_reported_as_lower_bound():
    # an enclosure better than the setup can see -> floor-limited, SE >= capability
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 130.0                       # exceeds capability everywhere
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    seen = 0
    for i, m in wall.items():
        if m["floor_limited"]:
            assert m["se_reported_db"] == pytest.approx(reference[i]["capability_db"], abs=0.01)
            assert m["verdict"] in ("PASS", "INCONCLUSIVE")
            seen += 1
    assert seen >= 1


def test_clean_enclosure_passes_at_narrow_rbw():
    # 10 Hz RBW (every band clears EA8) + a clean 105 dB wall -> full campaign PASS
    cfg = cfg_mod.Campaign(analyzer=cfg_mod.AnalyzerSettings(rbw_hz=10.0))
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 105.0                       # clean 105 dB, no leaks
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    summary = loop.summarize(reference, wall)
    assert summary["n_points"] == len(cfg.frequencies())
    assert summary["ea8_fail_count"] == 0
    assert summary["campaign_pass"] is True


def test_localize_finds_hot_seam_digitally():
    # digital level-vs-position scan must locate the injected hot seam (no display involved)
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    bench.leak_profile = drivers.demo_seam_leak(hot_position_m=1.2)
    positions = [round(0.05 * i, 3) for i in range(49)]      # 0..2.4 m at 5 cm
    rows, peak = loop.localize(cfg, src, sa, 38e9, positions, bench=bench)
    assert len(rows) == len(positions)
    assert abs(peak["position"] - 1.2) <= 0.1                # found the seam
    baseline = sorted(r["level_dbm"] for r in rows)[len(rows) // 2]
    assert peak["level_dbm"] - baseline > 15                 # stands above the quiet wall


def test_band_for_boundary():
    cfg = cfg_mod.default()
    assert cfg.band_for(10e9).name.startswith("1-18")
    assert cfg.band_for(35e9).name.startswith("26.5-40")


def test_injected_leak_is_caught():
    # the demo enclosure has a deep slot near 38 GHz -> some FAIL/INCONCLUSIVE
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    summary = loop.summarize(reference, wall)
    assert summary["campaign_pass"] is False


# --------------------------------------------------- C2: two-pass metrology correctness

def test_ref_and_wall_rows_carry_c2_metrology_keys():
    # every ref row records its own floor detector + (possibly None) preselector DAC; every
    # wall row records its OWN source-off floor + detector. New keys ADDED, none removed.
    cfg = cfg_mod.default()
    src, sa, bench = drivers.open_instruments(cfg)
    reference, wall = loop.run_demo(cfg, src, sa, bench)
    assert all("preselector_dac" in r and r["floor_detector"] == "SMP" for r in reference.values())
    assert all("wall_floor_dbm" in r and r["floor_detector"] == "SMP" for r in wall.values())
    assert all("preselector_dac" in r for r in wall.values())


class _RecordingPreselectorAnalyzer:
    """Wraps a real SimSpectrumAnalyzer, adding a RECORDING preselector: the sim has no
    preselector physics (peak_preselector always returns None per the base class), so this fake
    stands in for a real 8565EC's peak/reuse behavior while everything else (measure_peak,
    measure_floor, configure...) delegates straight through to the real sim instance."""

    def __init__(self, inner):
        self._inner = inner
        self.peak_calls = []       # f_hz peaked in the reference pass
        self.applied_dacs = []     # dac values applied via set_preselector_dac in the wall pass

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def peak_preselector(self, f_hz, span_hz=50e6, rbw_hz=1e3):
        self.peak_calls.append(f_hz)
        return 217                 # deterministic recorded PSDAC

    def set_preselector_dac(self, dac):
        self.applied_dacs.append(dac)


def test_preselector_peaked_once_in_ref_pass_and_reused_identically_in_wall_pass():
    # FIX A: a >2.9 GHz point peaks the preselector ONCE (reference pass, on the live tone) and
    # reuses the EXACT recorded PSDAC in the wall pass (the through-shield tone is too weak to
    # re-peak reliably) -- never a second peak in the wall pass.
    band = cfg_mod.BandPlan("hi", 5e9, 5e9, 1, 33.0, 3.0, -143.0)
    cfg = cfg_mod.Campaign(bands=(band,))
    src, sa, bench = drivers.open_instruments(cfg)
    fake = _RecordingPreselectorAnalyzer(sa)
    reference = loop.acquire_reference(cfg, src, fake, bench=bench)
    assert fake.peak_calls == [5e9]
    assert reference[0]["preselector_dac"] == 217
    wall = loop.measure_wall(cfg, src, fake, reference, bench=bench)
    assert fake.applied_dacs == [217]                # identical dac reused
    assert wall[0]["preselector_dac"] == 217
    assert len(fake.peak_calls) == 1                 # still peaked exactly once, total


def test_preselector_not_peaked_below_2p9ghz():
    band = cfg_mod.BandPlan("lo", 1e9, 1e9, 1, 14.0, 12.0, -150.0)
    cfg = cfg_mod.Campaign(bands=(band,))
    src, sa, bench = drivers.open_instruments(cfg)
    fake = _RecordingPreselectorAnalyzer(sa)
    reference = loop.acquire_reference(cfg, src, fake, bench=bench)
    assert fake.peak_calls == []                     # below 2.9 GHz: never peaked
    assert reference[0]["preselector_dac"] is None
    wall = loop.measure_wall(cfg, src, fake, reference, bench=bench)
    assert fake.applied_dacs == []                   # nothing recorded to reuse
    assert wall[0]["preselector_dac"] is None


class _DriftingFloorAnalyzer:
    """Wraps a real analyzer; from the (worse_after+1)th measure_floor call onward, returns an
    artificially WORSE (higher) floor -- models a source-off floor that drifts between the
    reference and wall passes. Exercises FIX B: the wall pass must read its OWN floor, and
    floor_limited must gate on the WORSE of the two, not the stale reference floor."""

    def __init__(self, inner, worse_after, worse_floor_dbm):
        self._inner = inner
        self._floor_calls = 0
        self._worse_after = worse_after
        self._worse_floor_dbm = worse_floor_dbm

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def measure_floor(self, f_hz, settle_s):
        self._floor_calls += 1
        if self._floor_calls > self._worse_after:
            return (f_hz, self._worse_floor_dbm)
        return self._inner.measure_floor(f_hz, settle_s)


def test_wall_pass_reads_its_own_floor_and_gates_on_the_worse_of_the_two():
    # FIX B: the wall pass takes its OWN source-off floor (never just reuses the stale reference
    # floor), and floor_limited is tested against max(ref_floor, wall_floor).
    band = cfg_mod.BandPlan("t", 5e9, 5e9, 1, 33.0, 3.0, -143.0, target_se_db=60.0)
    cfg = cfg_mod.Campaign(bands=(band,))
    src, sa, bench = drivers.open_instruments(cfg)
    # 1st measure_floor call (reference pass) = the real sim floor (~-112 dBm); every call after
    # that (the wall pass) is forced to a much WORSE -60 dBm floor.
    fake = _DriftingFloorAnalyzer(sa, worse_after=1, worse_floor_dbm=-60.0)
    reference = loop.acquire_reference(cfg, src, fake, bench=bench)
    assert reference[0]["floor_dbm"] < -100.0                     # untouched: normal sim floor
    wall = loop.measure_wall(cfg, src, fake, reference, bench=bench)
    assert wall[0]["wall_floor_dbm"] == pytest.approx(-60.0)      # the wall pass's OWN floor
    assert wall[0]["wall_floor_dbm"] > reference[0]["floor_dbm"]  # and it is the WORSE one
    conservative = max(reference[0]["floor_dbm"], wall[0]["wall_floor_dbm"])
    assert wall[0]["floor_limited"] == (wall[0]["wall_dbm"] <= conservative + cfg.margin_db)
    assert wall[0]["floor_limited"] is True    # this scenario is constructed to be floor-limited
    # capability (the EA8 property) is still the REFERENCE row's, not recomputed from wall_floor
    assert wall[0]["se_reported_db"] == pytest.approx(reference[0]["capability_db"], abs=0.01)
