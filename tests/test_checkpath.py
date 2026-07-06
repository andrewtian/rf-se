"""Hardware-free tests for the RF-path self-test (loop.check_path).

A fake source/analyzer pair models a live path (RX sees the tone) and a dead path (TX open, RX
only ambient), so the PATH-LIVE / NO-COUPLING verdict is exercised without hardware.

Run:  uv run python -m pytest rf-se/se299/tests/test_checkpath.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import loop


class _FakeSource:
    def __init__(self):
        self.on = False
    def prepare(self): pass
    def set_power(self, p): pass
    def set_freq(self, f): pass
    def rf_on(self): self.on = True
    def rf_off(self): self.on = False
    def await_settled(self, s=0.05, use_opc=True): pass


class _LivePathAnalyzer:
    """RX that sees the source: floor -90, tone -40 when the source is on."""
    def __init__(self, source): self.s = source
    def prepare(self): pass
    def configure(self, *a): pass
    def measure_peak(self, f_hz, settle):
        return (f_hz, -40.0 if self.s.on else -90.0)


class _DeadPathAnalyzer:
    """TX open: RX never sees the source. Sits on a -52 dBm AMBIENT signal regardless of RF."""
    def __init__(self, source): self.s = source
    def prepare(self): pass
    def configure(self, *a): pass
    def measure_peak(self, f_hz, settle):
        return (f_hz, -52.0)                       # same on and off -> no coupling


def _cfg():
    band = config.BandPlan("t", 1e9, 18e9, 3, 14.0, 12.0, -150.0)
    return config.Campaign(bands=(band,), label="checkpath-test")


def test_path_live_detected():
    src = _FakeSource()
    res = loop.check_path(_cfg(), src, _LivePathAnalyzer(src), [1e9, 6e9, 18e9])
    assert res["verdict"] == "PATH-LIVE"
    assert res["n_couple"] == 3
    assert all(r["delta_db"] == 50.0 for r in res["rows"])   # -40 - (-90)


def test_no_coupling_detected():
    src = _FakeSource()
    res = loop.check_path(_cfg(), src, _DeadPathAnalyzer(src), [1e9, 6e9, 18e9])
    assert res["verdict"] == "NO-COUPLING"
    assert res["n_couple"] == 0
    assert all(r["delta_db"] == 0.0 for r in res["rows"])
    assert res["max_ambient_dbm"] == -52.0                   # ambient reported (RX side live)


def test_guard_threshold_respected():
    src = _FakeSource()

    class _Marginal:
        def __init__(self, s): self.s = s
        def prepare(self): pass
        def configure(self, *a): pass
        def measure_peak(self, f_hz, settle):
            return (f_hz, -85.0 if self.s.on else -90.0)      # only 5 dB
    res = loop.check_path(_cfg(), src, _Marginal(src), [1e9], guard_db=6.0)
    assert res["verdict"] == "NO-COUPLING"                    # 5 dB < 6 dB guard
    res2 = loop.check_path(_cfg(), src, _Marginal(src), [1e9], guard_db=3.0)
    assert res2["verdict"] == "PATH-LIVE"                     # 5 dB >= 3 dB guard


def test_ambient_drift_rejected():
    # an ambient signal that DRIFTS IN and stays high (off1 low, on high, off2 high) must NOT be
    # called coupling -- the off/on/off bracket requires the tone to be reversible. This is the
    # exact live failure mode: a -53 dBm ambient appears mid-measurement and never leaves.
    src = _FakeSource()

    class _AmbientDrift:
        def __init__(self, s):
            self.s = s
            self.n = 0
        def prepare(self): pass
        def configure(self, *a): pass
        def measure_peak(self, f_hz, settle):
            self.n += 1
            # read 1 (off1) clean floor; reads 2+ (on, off2) the ambient has drifted in and stuck
            return (f_hz, -90.0 if self.n == 1 else -53.0)
    res = loop.check_path(_cfg(), src, _AmbientDrift(src), [1e9], guard_db=6.0)
    assert res["verdict"] == "NO-COUPLING"                    # on(-53) - max(off1 -90, off2 -53) = 0
    assert res["rows"][0]["delta_db"] == 0.0


def test_check_path_leaves_source_off():
    src = _FakeSource()
    src.rf_on()
    loop.check_path(_cfg(), src, _LivePathAnalyzer(src), [1e9])
    assert src.on is False                                    # ends with TX off (safe)
