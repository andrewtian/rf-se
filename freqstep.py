"""Frequency-appropriate step ladder for arrow-key stepping of the TX/RX center frequency.

A fixed Hz step is wrong across DC-40 GHz: 1 MHz is a huge jump at 10 MHz and invisible at 40 GHz.
Instead the step scales with the frequency's DECADE, so one arrow press is always a sensible ~1-10%
of the current frequency:

    coarse (arrow)      = 10^(floor(log10 f) - 1)   -> 1/100 of the decade, ~100 steps/decade
    fine   (shift+arrow)= coarse / 10               -> ~1000 steps/decade

    f              coarse step     fine step
    30 MHz         1 MHz           100 kHz
    500 MHz        10 MHz          1 MHz
    5 GHz          100 MHz         10 MHz
    25 GHz         1 GHz           100 MHz

Pure (no Qt) so it is unit-testable and shared by the TX (sg_gui) and RX (sa_gui) panels.
"""
from __future__ import annotations

import math


def coarse_step_hz(f_hz: float) -> float:
    """The coarse (single arrow-press) step at f_hz = 10^(floor(log10 f) - 1). Floors at 1 Hz."""
    f = max(float(f_hz), 1.0)
    return 10.0 ** (math.floor(math.log10(f)) - 1)


def fine_step_hz(f_hz: float) -> float:
    """10x finer than coarse (shift + arrow)."""
    return coarse_step_hz(f_hz) / 10.0


def step_freq(f_hz: float, up: bool, fine: bool = False,
              lo_hz: float = 10e6, hi_hz: float = 40e9) -> float:
    """One arrow press up/down from f_hz by the frequency-appropriate step, SNAPPED to the step
    grid (so repeated presses land on round numbers, e.g. 5.0 -> 5.1 -> 5.2 GHz) and clamped to
    [lo_hz, hi_hz]. Stepping across a decade boundary adopts the new decade's step size."""
    step = fine_step_hz(f_hz) if fine else coarse_step_hz(f_hz)
    if up:
        nf = (math.floor(f_hz / step) + 1) * step        # next grid point strictly above
    else:
        nf = (math.ceil(f_hz / step) - 1) * step         # next grid point strictly below
    return min(max(nf, float(lo_hz)), float(hi_hz))


def ladder(lo_hz: float, hi_hz: float, fine: bool = False, max_points: int = 100000) -> list:
    """The full coarse (or fine) grid walked from lo_hz up to hi_hz -- the 'rehearsal' of what the
    arrow key will do across a band. Used to sanity-check the ladder and by callers that want the
    stepping sequence. Always terminates (max_points guard)."""
    out = [float(lo_hz)]
    f = float(lo_hz)
    for _ in range(max_points):
        nf = step_freq(f, up=True, fine=fine, lo_hz=lo_hz, hi_hz=hi_hz)
        if nf <= f:                                       # clamped at hi_hz -> done
            break
        out.append(nf)
        f = nf
        if f >= hi_hz:
            break
    return out
