"""Hardware-free tests asserting an actual SE(f) VALUE + an affirmative campaign_pass through
the FULL campaign path (Coordinator.run_campaign -> loop.summarize). Context C5.

Gap this closes: test_sweep.py and test_networked.py assert internal consistency (keys
present, schema intact, ordering/monotonicity of a running figure, screening rows excluded)
via loop.run_demo or the Coordinator, but no existing test asserts that a measured SE(f)
actually RECOVERS a KNOWN injected enclosure SE model, nor that summarize(...)["campaign_pass"]
is True for a real passing run through the Coordinator (only negative/schema tests exercise
that key -- e.g. test_screen_row_can_never_make_campaign_pass, test_summarize_requires_
source_tracked_rows). This is the hardware-free mirror the live cert test (C8) will
re-assert on real metal.

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_campaign_se.py -q
"""
from __future__ import annotations

import dataclasses
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import control_plane
import loop


# ================================================================== known SE model (scenario 1)

def _known_se(f_hz: float) -> float:
    """A smooth, KNOWN true enclosure SE(f): 62-78 dB (modest -- doc 159's link budget can SEE
    it everywhere on the DEFAULT bands with room to spare, so no point is floor-limited), varying
    with frequency (no notches) so the recovery assertion below is not trivially a constant."""
    f_ghz = f_hz / 1e9
    return 70.0 - 8.0 * math.sin(f_ghz / 5.0)


def _passing_campaign():
    """A sim campaign over the DEFAULT link-budget bands (real gain/power/DANL numbers, doc 159
    sec 4.2b) but with target_se_db LOWERED to 55 dB -- comfortably under _known_se's 62 dB
    floor -- so every point's verdict is an honest PASS once the measured SE recovers the
    injected model. Returns (cfg, ControlPlane) with the known se_model already installed on
    the shared SimBench."""
    low_target_bands = tuple(
        dataclasses.replace(b, target_se_db=55.0) for b in cfg_mod.DEFAULT_BANDS)
    cfg = cfg_mod.Campaign(bands=low_target_bands)
    cp = control_plane.simulated(cfg)
    cp.bench.se_model = _known_se           # override install_bench_models' demo default
    return cfg, cp


def test_campaign_se_recovers_the_injected_model_and_passes():
    """The core C5 assertion: run the FULL campaign path (Coordinator.run_campaign) against a
    sim enclosure whose true SE(f) is KNOWN, and check the measured se_reported_db actually
    RECOVERS it -- not merely that keys/shape are present -- then check campaign_pass is True."""
    cfg, cp = _passing_campaign()
    coord = cp.make_coordinator()

    ref_updates, wall_updates = [], []
    result = coord.run_campaign(
        bench=cp.bench,
        on_reference_point=lambda i, row: ref_updates.append(row),
        on_se_update=lambda fig, row: wall_updates.append(row))

    reference, wall, summary = result["reference"], result["wall"], result["summary"]
    n = len(cfg.frequencies())
    assert len(ref_updates) == n and len(wall_updates) == n     # both hooks actually fired

    # Tolerance derivation (NOT a bare magic number): SimBench.amplitude_dbm() power-sums the
    # true signal with a deterministic per-frequency gaussian floor bias (sigma 0.5 dB, seeded
    # only by (bench.seed, round(f)) -- identical draw in the reference and wall reads at the
    # same f, so it mostly cancels in the SE=ref-wall ratio) via db_power_sum. The residual
    # distortion from that incoherent sum is bounded by 10*log10(1 + 10**(-margin_above_floor/10))
    # dB, which is a small fraction of a dB once the signal clears the floor by tens of dB (as
    # verified per-point below via capability_db). cfg.margin_db (the campaign's own EA8
    # dynamic-range headroom, 10 dB by default) sets the natural scale for that residual; we
    # allow margin_db / 5 = 2 dB, comfortably covering the incoherent-sum term while staying far
    # tighter than the 10 dB EA8 margin itself.
    tol_db = cfg.margin_db / 5.0

    not_floor_limited = 0
    for i, row in wall.items():
        f_hz = row["f_hz"]
        cap = reference[i]["capability_db"]
        true_se = _known_se(f_hz)
        # Precondition (this is what makes the scenario NOT floor-limited): the dynamic-range
        # capability clears the injected SE with room to spare.
        assert cap >= true_se + 15.0, (
            f"{row['band']} @ {f_hz / 1e9:.3f} GHz: capability {cap:.1f} dB does not clear "
            f"injected SE {true_se:.1f} dB by enough margin -- scenario is mis-tuned")
        if row["floor_limited"]:
            continue
        not_floor_limited += 1
        # THE core assertion: the measured SE recovers the KNOWN injected model within tol_db.
        assert row["se_reported_db"] == pytest.approx(true_se, abs=tol_db), (
            f"{row['band']} @ {f_hz / 1e9:.3f} GHz: measured {row['se_reported_db']:.2f} dB "
            f"vs injected {true_se:.2f} dB (tol {tol_db} dB)")
        # se_reported_db == se_db when not floor-limited (loop.measure_wall: se_report = se).
        assert row["se_db"] == row["se_reported_db"]

    assert not_floor_limited == n, "scenario is tuned so NONE of the points are floor-limited"

    assert loop.summarize(reference, wall) == summary   # the Coordinator embeds this exact call
    assert summary["campaign_pass"] is True
    assert summary["n_points"] == n
    assert summary["ea8_fail_count"] == 0
    assert summary["verdicts"] == {"PASS": n}
    assert summary["rbw_symmetric"] is True


def test_campaign_floor_limited_yields_inconclusive_not_a_false_pass():
    """A deliberately-too-high injected SE (far beyond the setup's dynamic range, even after the
    adaptive RBW ladder's max narrowing) drives every point floor-limited. summarize() must
    report the honest INCONCLUSIVE lower-bound verdict -- NEVER a false PASS -- and
    campaign_pass must be False. This is the other half of the C5 gap: today nothing asserts the
    campaign path behaves honestly (not falsely-PASS) when the SE is unmeasurably deep."""
    too_high_target = 400.0
    high_target_bands = tuple(
        dataclasses.replace(b, target_se_db=too_high_target) for b in cfg_mod.DEFAULT_BANDS)
    cfg = cfg_mod.Campaign(bands=high_target_bands)
    cp = control_plane.simulated(cfg)
    cp.bench.se_model = lambda f: 400.0   # far beyond any band's capability (~100-145 dB)
    coord = cp.make_coordinator()

    result = coord.run_campaign(bench=cp.bench)
    reference, wall, summary = result["reference"], result["wall"], result["summary"]
    n = len(cfg.frequencies())

    assert all(r["floor_limited"] for r in wall.values())
    assert all(r["verdict"] == "INCONCLUSIVE" for r in wall.values())
    for i, row in wall.items():
        # lower-bound behavior: the reported SE is the measured capability (SE >= capability),
        # never the fictitious, unmeasurable 400 dB injected model.
        assert row["se_reported_db"] == pytest.approx(reference[i]["capability_db"])
        assert row["se_reported_db"] < too_high_target

    assert loop.summarize(reference, wall) == summary
    assert summary["campaign_pass"] is False
    assert summary["ea8_fail_count"] == n     # every point failed EA8 even after max narrowing


def test_passing_campaign_also_fires_shield_prompt_between_passes():
    """Same passing scenario, but exercising the C4 on_shield_prompt hook: the physical-shield
    step must fire exactly once, strictly between the reference and wall passes, while the
    campaign still recovers a real SE value and passes -- not just key-presence."""
    cfg, cp = _passing_campaign()
    coord = cp.make_coordinator()
    log = []
    result = coord.run_campaign(
        bench=cp.bench,
        on_reference_point=lambda i, row: log.append(("ref", i)),
        on_se_update=lambda fig, row: log.append(("wall", row["f_hz"])),
        on_shield_prompt=lambda: log.append(("shield",)))

    shield_idxs = [i for i, e in enumerate(log) if e == ("shield",)]
    ref_idxs = [i for i, e in enumerate(log) if e[0] == "ref"]
    wall_idxs = [i for i, e in enumerate(log) if e[0] == "wall"]
    assert len(shield_idxs) == 1
    assert ref_idxs and wall_idxs
    assert max(ref_idxs) < shield_idxs[0] < min(wall_idxs)

    assert result["summary"]["campaign_pass"] is True
    # the SE VALUE assertion still holds with the shield hook wired in
    worst = min(result["wall"].values(), key=lambda r: r["se_reported_db"])
    assert worst["se_reported_db"] >= 55.0 - 1e-6      # >= the lowered target everywhere


def test_controlled_context_is_reentrant_single_lease_single_release():
    # G.1: controlled() is the single home of the take/release discipline. Nesting must lease ONCE (on
    # the outer enter) and release ONCE (on the outer exit), so a pre-gate that holds control and then
    # calls a primitive which also wants control keeps ONE lease and the RF-off backstop fires once.
    _, cp = _passing_campaign()
    coord = cp.make_coordinator()
    calls = {"take": 0, "rel": 0}
    coord.take_control = lambda: calls.__setitem__("take", calls["take"] + 1)
    coord.release_control = lambda: calls.__setitem__("rel", calls["rel"] + 1)
    with coord.controlled():
        with coord.controlled():                          # nested: no re-lease, no early release
            assert calls["take"] == 1 and calls["rel"] == 0
    assert calls == {"take": 1, "rel": 1}


def test_controlled_context_releases_even_when_body_raises():
    # the guaranteed RF-off backstop (release_control) must run on the outer exit even if the body
    # raises -- a raising pre-gate/campaign never leaves the source keyed past control release.
    _, cp = _passing_campaign()
    coord = cp.make_coordinator()
    calls = {"take": 0, "rel": 0}
    coord.take_control = lambda: calls.__setitem__("take", calls["take"] + 1)
    coord.release_control = lambda: calls.__setitem__("rel", calls["rel"] + 1)
    with pytest.raises(ValueError):
        with coord.controlled():
            raise ValueError("boom")
    assert calls == {"take": 1, "rel": 1}                 # released exactly once despite the raise
