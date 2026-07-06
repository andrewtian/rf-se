"""Frequency-appropriate arrow-key step ladder (freqstep): step size scales with the decade so one
press is a sensible fraction of the current frequency across DC-40 GHz. Pure, no Qt.

Run:  uv run python -m pytest rf-se/se299/tests/test_freqstep.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import freqstep as fs


def test_coarse_step_scales_with_decade():
    assert fs.coarse_step_hz(30e6) == 1e6            # 30 MHz -> 1 MHz
    assert fs.coarse_step_hz(500e6) == 10e6          # 500 MHz -> 10 MHz
    assert fs.coarse_step_hz(5e9) == 100e6           # 5 GHz -> 100 MHz
    assert fs.coarse_step_hz(25e9) == 1e9            # 25 GHz -> 1 GHz
    assert fs.fine_step_hz(5e9) == 10e6              # fine = coarse / 10


def test_step_is_sensible_fraction_everywhere():
    # one coarse press is between ~1% and ~10% of the current frequency across the whole band
    for f in (30e6, 100e6, 500e6, 1e9, 5e9, 10e9, 25e9, 40e9):
        frac = fs.coarse_step_hz(f) / f
        assert 0.01 <= frac <= 0.10 + 1e-9


def test_step_up_down_snaps_to_grid_and_is_reversible():
    # from a grid point, up then down returns to the start
    assert fs.step_freq(5.0e9, up=True) == 5.1e9
    assert fs.step_freq(5.0e9, up=False) == 4.9e9
    assert fs.step_freq(fs.step_freq(5.0e9, up=True), up=False) == 5.0e9
    # an off-grid value snaps to the nearest grid point in the press direction
    assert fs.step_freq(5.05e9, up=True) == 5.1e9
    assert fs.step_freq(5.05e9, up=False) == 5.0e9


def test_step_adopts_new_decade_across_the_boundary():
    # walking up through 10 GHz: 100 MHz steps below, 1 GHz steps above
    assert fs.step_freq(9.9e9, up=True) == 10.0e9    # 100 MHz step just under the boundary
    assert fs.step_freq(10.0e9, up=True) == 11.0e9   # 1 GHz step just over it


def test_step_clamps_to_range():
    assert fs.step_freq(40e9, up=True, hi_hz=40e9) == 40e9        # cannot exceed the ceiling
    assert fs.step_freq(10e6, up=False, lo_hz=10e6) == 10e6       # cannot go below the floor


def test_ladder_is_monotonic_spans_the_band_and_terminates():
    lad = fs.ladder(10e6, 40e9)
    assert lad[0] == 10e6 and lad[-1] == 40e9
    assert all(lad[i] < lad[i + 1] for i in range(len(lad) - 1))  # strictly increasing
    assert 250 <= len(lad) <= 400                                 # ~100 steps/decade, bounded
