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


def test_measure_wall_rejects_tx_power_mismatch_vs_calibration():
    # W2.3 substitution guard: SE = reference - wall is valid ONLY if the SAME TX drive fed both
    # passes. A calibration captured at 12 dBm then a wall pass CONFIGURED at 10 dBm (e.g. cfg edited
    # between calibrate and measure) must be REJECTED -- not silently offset SE by the 2 dB delta.
    import config
    import control_plane
    # positional BandPlan: (name, f_lo, f_hi, n_points, antenna_gain_dbi, source_power_dbm, danl)
    cp_cal = control_plane.simulated(config.Campaign(
        bands=(config.BandPlan("t", 1e9, 2e9, 2, 14.0, 12.0, -150.0, target_se_db=80.0),), label="cal"))
    ref = cp_cal.make_coordinator().acquire_reference(bench=cp_cal.bench)   # calibration at 12 dBm
    assert all(r["src_power_dbm"] == 12.0 for r in ref.values())
    cp_wall = control_plane.simulated(config.Campaign(                     # same freqs, 10 dBm drive
        bands=(config.BandPlan("t", 1e9, 2e9, 2, 14.0, 10.0, -150.0, target_se_db=80.0),), label="wall"))
    with pytest.raises(loop.AcquisitionRejected) as ei:
        cp_wall.make_coordinator().measure_wall(ref, bench=cp_wall.bench)
    assert "TX power" in str(ei.value)                       # names the mismatch, not an opaque abort


# ---- G.4 ambient bracket: the 0 dB reference must fall BACK to the floor when our tone stops ----

class _FakeSrc:                                              # a minimal duck-typed source (no radiate)
    def prepare(self): pass
    def set_power(self, p): pass
    def set_freq(self, f): pass
    def rf_on(self): pass
    def rf_off(self): pass
    def await_settled(self, s, opc): pass


class _AmbientAnalyzer:
    """measure_floor call #1 = off1 (source-off floor), call #2 = off2 (the trailing ambient probe);
    measure_peak = the source-ON reference. Scriptable to model a PERSISTENT external tone (off2
    elevated) vs a clean bench (off2 == off1). One point + a target that clears at the first rung, so
    the ladder never narrows -> measure_floor is called exactly twice."""
    def __init__(self, off1, off2, ref):
        self._floors, self._i, self._ref = [off1, off2], 0, ref
    def prepare(self): pass
    def configure(self, *a): pass
    def measure_floor(self, f, s):
        v = self._floors[min(self._i, len(self._floors) - 1)]
        self._i += 1
        return (f, v)
    def measure_peak(self, f, s):
        return (f, self._ref)


def _g4_cfg():
    import config
    # one point at 1 GHz; target_se_db=10 so cap clears the first rung and the RBW ladder never narrows
    return config.Campaign(
        bands=(config.BandPlan("t", 1e9, 2e9, 1, 14.0, 10.0, -150.0, target_se_db=10.0),), label="g4")


def test_acquire_reference_clean_reference_is_reversible():
    # off2 == off1 (clean bench): our tone drove the ON read and vanished when RF went off -> ref
    # clears max(off1, off2) by the guard -> reversible, additive keys present, SE path unchanged.
    cfg = _g4_cfg()
    rows = loop.acquire_reference(cfg, _FakeSrc(), _AmbientAnalyzer(off1=-100.0, off2=-100.0, ref=-30.0))
    r = rows[0]
    assert r["ref_reversible"] is True
    assert r["ref_off2_dbm"] == -100.0 and r["ambient_dbm"] == -100.0
    assert r["floor_dbm"] == -100.0 and r["ref_dbm"] == -30.0      # off1/ON reads unchanged


def test_acquire_reference_flags_irreversible_ambient():
    # a PERSISTENT external tone: it inflates the ON read (ref) AND is still there at off2, so ref does
    # NOT clear max(off1, off2) by the 6 dB guard -> flagged NOT reversible so summarize can gate it,
    # rather than letting ambient masquerade as coupling and be trusted as real capability.
    cfg = _g4_cfg()
    rows = loop.acquire_reference(cfg, _FakeSrc(), _AmbientAnalyzer(off1=-100.0, off2=-33.0, ref=-30.0))
    r = rows[0]
    assert r["ref_reversible"] is False                           # -30 < max(-100,-33) + 6 = -27
    assert r["ambient_dbm"] == -33.0 and r["ref_off2_dbm"] == -33.0
    assert r["floor_dbm"] == -100.0                               # off1 (clean) still the recorded floor


def test_sim_reference_rows_carry_reversible_keys():
    # the real sim (clean bench: source tone reverses cleanly) -> every reference row is reversible and
    # carries the additive keys; guards the "reversible case leaves SE unchanged" claim on the sim.
    import config
    import control_plane
    cp = control_plane.simulated(config.Campaign(
        bands=(config.BandPlan("t", 1e9, 2e9, 2, 14.0, 12.0, -150.0, target_se_db=80.0),), label="g4sim"))
    rows = cp.make_coordinator().acquire_reference(bench=cp.bench)
    for r in rows.values():
        assert r["ref_reversible"] is True
        assert "ref_off2_dbm" in r and "ambient_dbm" in r
