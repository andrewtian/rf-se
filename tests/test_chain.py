"""Chain-continuity sweep (loop.chain_sweep): a validated emitted-vs-received ramp UP the preset
bands, with the per-band settle-confirm optimization (OSB leveled+locked once per band, a fixed
settle dwell per point within the band -- the source-bus speed lever). Hardware-free (sim).

Run:  uv run python -m pytest rf-se/se299/tests/test_chain.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import control_plane
import loop


def _sim_coord(bands):
    cfg = config.Campaign(bands=bands, label="chain-test")
    cp = control_plane.simulated(cfg)
    coord = cp.make_coordinator()
    assert coord.ensure_ready()
    return cfg, cp, coord


def test_chain_sweep_ramps_all_bands_low_to_high_and_validates_received():
    band_a = config.BandPlan("A 1-6", 1e9, 6e9, 4, 14.0, 12.0, -150.0)
    band_b = config.BandPlan("B 6-18", 6e9, 18e9, 3, 25.0, 11.0, -148.0)
    cfg, cp, coord = _sim_coord((band_a, band_b))
    res = loop.chain_sweep(cfg, coord.source, coord.analyzer, bench=cp.bench, guard_db=6.0)
    assert res["n"] == 7 and res["n_couple"] == 7           # every point carried (strong sim link)
    assert res["verdict"] == "CHAIN-LIVE" and res["chain_live"] is True
    freqs = [r["f_hz"] for r in res["rows"]]
    assert freqs == sorted(freqs)                           # strictly low -> high
    assert {b["band"] for b in res["bands"]} == {"A 1-6", "B 6-18"}
    assert all(b["n_couple"] == b["n"] for b in res["bands"])
    # emitted-vs-received recorded relative to the KNOWN TX setpoint power
    for r in res["rows"]:
        assert abs(r["coupling_db"] - (r["tone_dbm"] - r["src_power_dbm"])) < 1e-9
        assert r["delta_db"] == r["tone_dbm"] - r["floor_dbm"]


def test_chain_sweep_confirms_osb_once_per_band_then_dwells():
    # the source-bus lever: OSB leveled+locked confirmed ONCE per band start (await_settled), a
    # fixed settle dwell for every other point in the band (settle) -- never the ~85 ms OSB
    # round-trip per point.
    band_a = config.BandPlan("A", 1e9, 6e9, 4, 14.0, 12.0, -150.0)
    band_b = config.BandPlan("B", 6e9, 18e9, 3, 25.0, 11.0, -148.0)
    cfg, cp, coord = _sim_coord((band_a, band_b))
    cp.bench.settled_count = 0
    cp.bench.dwell_count = 0
    res = loop.chain_sweep(cfg, coord.source, coord.analyzer, bench=cp.bench)
    n = res["n"]                                            # 4 + 3 = 7 points, 2 bands
    assert cp.bench.settled_count == 2                      # one OSB confirm per band start
    assert cp.bench.dwell_count == n - 2                    # fixed dwell for the rest
    confirmed = [r["f_hz"] for r in res["rows"] if r["settle_confirmed"]]
    # exactly the FIRST point of each band is OSB-confirmed (band-first frequencies, verbatim)
    seen, expected = set(), []
    for r in res["rows"]:
        if r["band"] not in seen:
            seen.add(r["band"])
            expected.append(r["f_hz"])
    assert confirmed == expected


def test_chain_sweep_orders_tx_settle_before_rx_read():
    # ORDERING INVARIANT: within each point the source is commanded + settled/confirmed BEFORE the
    # tone is read. On the sim, settled_count+dwell_count increments once per point, and every
    # point yields a finite tone -- a read taken mid-transition (no settle) would not be recorded.
    band = config.BandPlan("A", 1e9, 6e9, 3, 14.0, 12.0, -150.0)
    cfg, cp, coord = _sim_coord((band,))
    cp.bench.settled_count = 0
    cp.bench.dwell_count = 0
    res = loop.chain_sweep(cfg, coord.source, coord.analyzer, bench=cp.bench)
    assert cp.bench.settled_count + cp.bench.dwell_count == res["n"]   # one settle per point
    assert all(r["tone_dbm"] == r["tone_dbm"] for r in res["rows"])    # finite (not NaN) reads


def test_chain_sweep_flags_a_dead_chain_not_a_false_pass():
    # nothing couples (a broken/open chain) -> NO-COUPLING, never a silent pass
    band = config.BandPlan("A", 1e9, 6e9, 3, 14.0, 12.0, -150.0)
    cfg, cp, coord = _sim_coord((band,))
    cp.bench.separation_m = 1e9                             # tone can never rise above the floor
    res = loop.chain_sweep(cfg, coord.source, coord.analyzer, bench=cp.bench, guard_db=6.0)
    assert res["n_couple"] == 0
    assert res["verdict"] == "NO-COUPLING" and res["chain_live"] is False
