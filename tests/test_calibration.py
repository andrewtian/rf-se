"""Hardware-free tests for the calibration (reference-pass) capability in loop.py.

Covers the quality grading (USABLE / PARTIAL / FLOOR-LIMITED), the known-TX + coupling fields,
the write/load roundtrip, and schema enforcement. No hardware, no matplotlib.

Run:  uv run python -m pytest rf-se/se299/tests/test_calibration.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import loop


def _ref_row(f_hz, ref_dbm, floor_dbm, src_power_dbm=12.0, target=100.0, margin=10.0):
    cap = ref_dbm - floor_dbm - margin
    return {
        "band": "b", "f_hz": f_hz, "src_power_dbm": src_power_dbm,
        "floor_dbm": floor_dbm, "ref_dbm": ref_dbm,
        "coupling_db": ref_dbm - src_power_dbm,
        "capability_db": cap, "target_db": target, "ea8_ok": cap >= target,
        "acq_mode": "stepped-cw-zerospan", "purpose": "acceptance", "source_tracked": True,
    }


def test_summary_usable_when_all_points_above_floor():
    # every reference well above the floor (strong link) -> USABLE
    ref = {0: _ref_row(1e9, -20.0, -110.0), 1: _ref_row(2e9, -25.0, -110.0)}
    s = loop.calibration_summary(ref)
    assert s["status"] == "USABLE" and s["n_above_floor"] == 2
    assert s["src_power_dbm"] == 12.0
    assert s["median_coupling_db"] == pytest.approx(-32.0)   # median of (-32, -37)


def test_summary_floor_limited_when_no_point_above_floor():
    # reference sits AT the floor everywhere (the weak in-enclosure case) -> FLOOR-LIMITED
    ref = {0: _ref_row(1e9, -110.0, -110.0), 1: _ref_row(2e9, -109.5, -110.0)}
    s = loop.calibration_summary(ref)
    assert s["status"] == "FLOOR-LIMITED" and s["n_above_floor"] == 0
    assert s["median_coupling_db"] is None                   # nothing above floor to average


def test_summary_partial_mixed():
    ref = {0: _ref_row(1e9, -20.0, -110.0), 1: _ref_row(2e9, -109.8, -110.0)}
    s = loop.calibration_summary(ref)
    assert s["status"] == "PARTIAL" and s["n_above_floor"] == 1


def test_write_load_roundtrip(tmp_path):
    ref = {0: _ref_row(1e9, -20.0, -110.0), 1: _ref_row(2e9, -25.0, -110.0)}
    s = loop.calibration_summary(ref)

    class _Cfg:                                              # minimal stand-in for settings_key()
        def settings_key(self):
            return (1e3, 1e3, -10.0, "POS", 0.0, 999)
    path = os.path.join(tmp_path, "cal.json")
    loop.write_calibration(path, _Cfg(), ref, s, note="unit-test")
    loaded = loop.load_calibration(path)
    assert set(loaded) == {0, 1}                             # integer-indexed, measure_wall-ready
    assert loaded[0]["ref_dbm"] == -20.0
    assert loaded[1]["src_power_dbm"] == 12.0


def test_load_rejects_foreign_schema(tmp_path):
    import json
    path = os.path.join(tmp_path, "bad.json")
    with open(path, "w") as fh:
        json.dump({"schema": "something-else", "reference": []}, fh)
    with pytest.raises(loop.AcquisitionRejected):
        loop.load_calibration(path)


def test_calibration_feeds_measure_wall(tmp_path):
    # a loaded calibration must be a drop-in reference for measure_wall (the two-step workflow:
    # calibrate now, measure later). Drive it through the simulator.
    import config
    import control_plane
    band = config.BandPlan("t", 1e9, 2e9, 2, 14.0, 12.0, -150.0, target_se_db=80.0)
    cfg = config.Campaign(bands=(band,), label="cal-wall")
    cp = control_plane.simulated(cfg)
    coord = cp.make_coordinator()
    ref = coord.acquire_reference(bench=cp.bench)
    assert all("src_power_dbm" in r and "coupling_db" in r for r in ref.values())
    s = loop.calibration_summary(ref)
    path = os.path.join(tmp_path, "sim-cal.json")
    loop.write_calibration(path, cfg, ref, s)
    reloaded = loop.load_calibration(path)
    wall = coord.measure_wall(reloaded, bench=cp.bench)      # loaded cal drives the wall pass
    assert len(wall) == 2 and all("se_db" in r for r in wall.values())
