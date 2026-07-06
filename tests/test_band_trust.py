"""Task 4: band-aware operation. Every measured row is tagged band_trust = 'trusted' (<=2.9 GHz, no
preselector + no harmonic-multiplied reference error) or 'provisional' (>2.9 GHz, where the marginal
8565EC reference is harmonic-multiplied so the FREQUENCY is untrustworthy until serviced). And the
stepped-CW screening sweep routes high-band reads through measure_tracked_peak (which peaks the YIG
and re-centers on the offset tone) instead of a bare measure_peak-at-exact-CF that would miss it.

These are the hardware-free guards; the sim has no reference offset (measure_tracked_peak delegates to
measure_peak on the sim), so the tests assert the ROUTING and the TAGS, not a recovered offset.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
        rf-se/se299/tests/test_band_trust.py -q -n0
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import drivers
import loop


def _band(name, f_ghz, gain_dbi=25.0):
    return cfg_mod.BandPlan(name, f_ghz * 1e9, f_ghz * 1e9, 1, gain_dbi, 3.0, -143.0)


def test_band_trust_helper_splits_at_the_preselector_crossover():
    assert loop._band_trust(1e9) == "trusted"
    assert loop._band_trust(2.9e9) == "trusted"        # boundary is inclusive (<=)
    assert loop._band_trust(2.9e9 + 1) == "provisional"
    assert loop._band_trust(10e9) == "provisional"


def test_reference_and_wall_rows_carry_band_trust():
    # a low-band and a high-band point: the low row is 'trusted', the high row 'provisional'.
    cfg = cfg_mod.Campaign(bands=(_band("lo-1ghz", 1.0), _band("hi-10ghz", 10.0)))
    src, sa, bench = drivers.open_instruments(cfg)
    bench.se_model = lambda f: 80.0
    reference = loop.acquire_reference(cfg, src, sa, bench)
    wall = loop.measure_wall(cfg, src, sa, reference, bench)
    assert reference[0]["band_trust"] == "trusted" and reference[1]["band_trust"] == "provisional"
    assert wall[0]["band_trust"] == "trusted" and wall[1]["band_trust"] == "provisional"


class _SpyAnalyzer(drivers.SimSpectrumAnalyzer):
    """A sim analyzer that records which read primitive was used per frequency, so the band-aware
    ROUTING (high band -> measure_tracked_peak, low band -> measure_peak) is asserted directly."""

    def __init__(self, bench):
        super().__init__(bench=bench)
        self.peak_calls, self.tracked_calls = [], []

    def measure_peak(self, f_hz, settle_s):
        self.peak_calls.append(f_hz)
        return super().measure_peak(f_hz, settle_s)

    def measure_tracked_peak(self, f_hz, search_span_hz=0.0, settle_s=0.0):
        self.tracked_calls.append(f_hz)
        return super().measure_tracked_peak(f_hz, search_span_hz, settle_s)


def test_stepped_sweep_routes_high_band_through_tracked_peak_and_low_band_through_peak():
    cfg = cfg_mod.Campaign(bands=(_band("lo-1ghz", 1.0), _band("hi-10ghz", 10.0)))
    src, _, bench = drivers.open_instruments(cfg)
    sa = _SpyAnalyzer(bench)
    frame = loop.stepped_cw_sweep(cfg, src, sa, [1e9, 10e9], bench=bench)
    # ROUTING: 10 GHz went through the tone-finder; 1 GHz did NOT (it took the exact-CF read).
    # (On the sim, measure_tracked_peak internally delegates to measure_peak -- no reference offset to
    # recover -- so 10 GHz also shows in peak_calls; the routing DECISION is what we assert here.)
    assert 10e9 in sa.tracked_calls                    # high band routed to the tone-finder
    assert 1e9 in sa.peak_calls and 1e9 not in sa.tracked_calls   # low band NOT routed to it
    # the frame tags each point's trust and records the trusted-band ceiling.
    assert frame["band_trust_by_point"] == ["trusted", "provisional"]
    assert frame["trusted_band_max_hz"] == 2.9e9
    assert len(frame["levels_dbm"]) == 2


def test_stepped_sweep_all_low_band_is_all_trusted_and_never_tracks():
    cfg = cfg_mod.Campaign(bands=(_band("lo-1ghz", 1.0), _band("lo-2ghz", 2.0)))
    src, _, bench = drivers.open_instruments(cfg)
    sa = _SpyAnalyzer(bench)
    frame = loop.stepped_cw_sweep(cfg, src, sa, [1e9, 2e9], bench=bench)
    assert frame["band_trust_by_point"] == ["trusted", "trusted"]
    assert sa.tracked_calls == []                       # no high-band point -> tone-finder never used
