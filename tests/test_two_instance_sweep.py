"""Two-instance DC-to-40-GHz sweep: instance 1 (coordinator, RX side) DRIVES the sweep while
instance 2 (TX source) FOLLOWS every point through the stack. Hardware-free (sim control plane),
so it actually runs and proves the drive path: a spy on the source records every retune the
coordinator pushes, and we assert the TX was commanded to EVERY plan frequency across DC-40 GHz.

The live version (both units on the network) is test_e2e_live.test_live_two_instance_sweep_*.

Run:  uv run python -m pytest rf-se/se299/tests/test_two_instance_sweep.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import control_plane


def _spy_source(coord):
    """Wrap the resolved source's set_freq to record every frequency the coordinator drives
    through the stack. Returns the recording list (the source object is shared with the sweep)."""
    src = coord.source
    seen = []
    orig = src.set_freq

    def spy(f, _o=orig):
        seen.append(f)
        return _o(f)

    src.set_freq = spy
    return seen


def test_dc_to_40ghz_plan_spans_the_mission_band():
    cfg = config.Campaign(bands=config.DC_TO_40GHZ_BANDS, label="dc40")
    plan = [f for f, _ in cfg.frequencies()]
    assert len(cfg.bands) == 4                              # DC low + the 3 default bands
    assert min(plan) == pytest.approx(10e6)                # DC end = source floor, 10 MHz
    assert max(plan) == pytest.approx(40e9)                # top = 40 GHz
    # monotonically increasing coverage across the whole span
    assert plan == sorted(plan)
    assert any(f < 1e9 for f in plan) and any(f > 26e9 for f in plan)


def test_two_instance_sweep_tx_follows_every_point():
    # instance 1 (coordinator) drives the full DC-to-40-GHz sweep; the TX source must be retuned
    # to follow at EVERY point (source_tracked), across the whole mission band.
    cfg = config.Campaign(bands=config.DC_TO_40GHZ_BANDS, label="dc40-sweep")
    cp = control_plane.simulated(cfg)
    coord = cp.make_coordinator()
    assert coord.ensure_ready()                            # open the rx+tx links (two instances)
    seen = _spy_source(coord)

    res = coord.sweep()                                    # freqs_hz=None -> the FULL cfg plan

    plan = [f for f, _ in cfg.frequencies()]
    assert res["source_tracked"] is True                   # TX followed
    assert res["tracking"] == "software-lockstep"
    assert len(res["levels_dbm"]) == len(plan)             # a reading per swept point
    # the SOURCE was commanded to every plan frequency, in order, through the stack:
    assert len(seen) == len(plan)
    assert all(abs(a - b) < 1.0 for a, b in zip(seen, plan))
    assert min(seen) == pytest.approx(10e6) and max(seen) == pytest.approx(40e9)   # DC-40 GHz


def test_two_instance_sweep_leaves_source_off():
    cfg = config.Campaign(bands=config.DC_TO_40GHZ_BANDS, label="dc40-off")
    cp = control_plane.simulated(cfg)
    coord = cp.make_coordinator()
    assert coord.ensure_ready()
    coord.source.rf_on()                                   # pretend it was left on
    coord.sweep()
    assert coord.source.b.src_rf_on is False               # sweep ends with the TX off (safe)


def test_explicit_freq_list_sweep():
    # instance 1 can also drive an explicit frequency list (a targeted sub-sweep); TX still follows
    cfg = config.Campaign(bands=config.DC_TO_40GHZ_BANDS, label="dc40-list")
    cp = control_plane.simulated(cfg)
    coord = cp.make_coordinator()
    assert coord.ensure_ready()
    seen = _spy_source(coord)
    freqs = [50e6, 500e6, 5e9, 20e9, 39e9]
    res = coord.sweep(freqs_hz=freqs)
    assert res["source_tracked"] is True
    assert [round(f) for f in seen] == [round(f) for f in freqs]
    assert len(res["levels_dbm"]) == len(freqs)
