"""Canonical per-device ABSOLUTE-state records + read_state() + reconciliation (gates C1-C4).

Hardware-free: the sim drivers, and a scripted fake transport (reused from test_device_operation) that
returns the 8560 interrogate-form replies / native OF1/OL1/OSB. Proves read_state() parses the device
truth and that reconciliation flags exactly the drifted field.

Run:  uv run python -m pytest rf-se/se299/tests/test_device_state.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import device_state as ds
import drivers
from test_device_operation import FakeT                    # scripted fake transport (records + replies)


# ---- pure reconciliation logic --------------------------------------------------------------

def _astate(**kw):
    base = dict(center_hz=2.45e9, span_hz=5e6, rbw_hz=30e3, vbw_hz=30e3, ref_level_dbm=-10.0,
                atten_db=0.0, detector="POS", scale_db_div=10.0, aunits="DBM", sweep_time_s=0.05)
    base.update(kw)
    return ds.AnalyzerState(**base)


def test_reconcile_analyzer_consistent_is_empty():
    st = _astate()
    assert ds.reconcile_analyzer(st, center_hz=2.45e9, span_hz=5e6, ref_level_dbm=-10.0,
                                 detector="pos") == []      # case-insensitive detector match


def test_reconcile_analyzer_friendly_detector_matches_mnemonic():
    # the GUI feed holds the FRIENDLY label ("peak"); the device reports the MNEMONIC ("POS"). These are
    # the same detector -> must NOT flag a drift (else the live Point-Op pane shows a permanent false alarm).
    st = _astate(detector="POS")
    assert ds.reconcile_analyzer(st, detector="peak") == []
    assert ds.reconcile_analyzer(_astate(detector="SMP"), detector="sample") == []
    assert ds.reconcile_analyzer(_astate(detector="NEG"), detector="neg-peak") == []
    # a REAL detector change is still caught (device on NEG, model wanted peak/POS)
    d = ds.reconcile_analyzer(_astate(detector="NEG"), detector="peak")
    assert [x.field for x in d] == ["detector"]


def test_reconcile_analyzer_flags_only_the_drifted_field_and_amplitude():
    st = _astate(ref_level_dbm=-5.0)                         # device RL diverged from desired
    d = ds.reconcile_analyzer(st, center_hz=2.45e9, ref_level_dbm=-10.0)
    assert [x.field for x in d] == ["ref_level_dbm"]
    assert ds.analyzer_amplitude_drift(d)                   # RL drift MUST clear the binary cal cache


def test_reconcile_analyzer_scale_drift_is_amplitude():
    st = _astate(scale_db_div=5.0)
    d = ds.reconcile_analyzer(st, scale_db_div=10.0)
    assert [x.field for x in d] == ["scale_db_div"] and ds.analyzer_amplitude_drift(d)


def test_reconcile_analyzer_auto_rbw_never_flagged():
    st = _astate(rbw_hz=1e6)                                 # instrument auto-resolved to 1 MHz
    assert ds.reconcile_analyzer(st, rbw_hz=None) == []      # desired AUTO -> not a drift


def test_reconcile_analyzer_center_within_ppm_is_ok():
    st = _astate(center_hz=2.45e9 + 1.0)                     # 1 Hz at 2.45 GHz is within 1 ppm
    assert ds.reconcile_analyzer(st, center_hz=2.45e9) == []


def test_reconcile_source_flags_freq_and_syntax_reject():
    ok = ds.SourceState(freq_hz=5e9, level_dbm=-5.0, leveled=True, locked=True, syntax_ok=True)
    assert ds.reconcile_source(ok, freq_hz=5e9, level_dbm=-5.0) == []
    bad = ds.SourceState(freq_hz=4e9, level_dbm=-5.0, leveled=True, locked=True, syntax_ok=False)
    fields = {x.field for x in ds.reconcile_source(bad, freq_hz=5e9, level_dbm=-5.0)}
    assert fields == {"freq_hz", "syntax_ok"}               # freq drift + a rejected command (OSB bit5)


# ---- sim read_state (parity with the real path, hardware-free) ------------------------------

def test_sim_analyzer_read_state_reflects_configure_and_tune():
    an = drivers.SimSpectrumAnalyzer(drivers.SimBench())
    an.configure(rbw_hz=30e3, vbw_hz=0, ref_dbm=-12.0, detector="pos")
    an.set_frequency(center_hz=2.45e9, span_hz=5e6)
    st = an.read_state()
    assert abs(st.center_hz - 2.45e9) < 1 and abs(st.span_hz - 5e6) < 1
    assert st.ref_level_dbm == -12.0 and st.detector == "POS" and st.rbw_hz == 30e3
    assert ds.reconcile_analyzer(st, center_hz=2.45e9, span_hz=5e6, ref_level_dbm=-12.0,
                                 detector="POS") == []       # model==device


def test_sim_source_read_state_reflects_set():
    sg = drivers.SimSignalGenerator(drivers.SimBench())
    sg.set_freq(5e9)
    sg.set_power(-5.0)
    st = sg.read_state()
    assert abs(st.freq_hz - 5e9) < 1 and st.level_dbm == -5.0 and st.syntax_ok
    assert ds.reconcile_source(st, freq_hz=5e9, level_dbm=-5.0) == []


# ---- real driver read_state via a scripted fake transport (interrogate forms) ----------------

def test_agilent_read_state_parses_interrogate_replies():
    t = FakeT({"CF?": "2450000000", "SP?": "5000000", "RB?": "30000", "VB?": "30000",
               "RL?": "-10.0", "AT?": "0", "DET?": "POS", "AUNITS?": "DBM", "LG?": "10", "ST?": "0.05"})
    st = drivers.Agilent856xEC(t).read_state()
    assert abs(st.center_hz - 2.45e9) < 1 and abs(st.span_hz - 5e6) < 1
    assert st.ref_level_dbm == -10.0 and st.scale_db_div == 10.0
    assert st.detector == "POS" and st.aunits == "DBM"
    assert {"CF?", "SP?", "RB?", "RL?", "DET?", "LG?"}.issubset(set(t.queries))


def test_agilent_read_state_tolerates_units_suffix():
    t = FakeT({"CF?": "2450000000 HZ", "SP?": "5000000 HZ", "RB?": "30000 HZ", "VB?": "30000 HZ",
               "RL?": "-10.0 DBM", "AT?": "0 DB", "DET?": "POS", "AUNITS?": "DBM",
               "LG?": "10 DB", "ST?": "50 MS"})
    st = drivers.Agilent856xEC(t).read_state()
    assert abs(st.center_hz - 2.45e9) < 1 and st.ref_level_dbm == -10.0   # suffix stripped by _leading_float


def test_anritsu_read_state_parses_of1_ol1_osb_and_syntax_bit():
    t = FakeT({"OF1": "5000.0", "OL1": "-5.0", "OSB": bytes([0x00])})
    st = drivers.Anritsu68369(t).read_state()
    assert abs(st.freq_hz - 5e9) < 1 and st.level_dbm == -5.0
    assert st.leveled and st.locked and st.syntax_ok
    t2 = FakeT({"OF1": "5000.0", "OL1": "-5.0", "OSB": bytes([0x20])})   # bit5 set = syntax error
    st2 = drivers.Anritsu68369(t2).read_state()
    assert not st2.syntax_ok and st2.leveled and st2.locked


# ---- feed-correctness driver fixes (gates C6 + C7) ------------------------------------------

def test_agilent_set_resolution_bandwidth_restore_command():
    t = FakeT()
    an = drivers.Agilent856xEC(t)
    an.set_resolution_bandwidth(30e3, auto=False)
    an.set_resolution_bandwidth(0, auto=True)
    an.set_resolution_bandwidth(None, auto=False)          # None/<=0 -> coupled AUTO (never 'RB 0HZ')
    assert t.writes == ["RB 30000HZ", "RB AUTO", "RB AUTO"]


def test_agilent_invalidate_calibration_clears_bin_cal():
    an = drivers.Agilent856xEC(FakeT())
    an._bin_cal = (1 / 6.0, -110.0)                        # a pretend cached map
    an.invalidate_calibration()
    assert an._bin_cal is None                             # forced to re-derive on the next read


def test_agilent_ascii_read_guards_truncated_trace():
    full = ",".join(["-100.0"] * 601)
    freqs, levels = drivers.Agilent856xEC(FakeT({"TRA?": full}))._read_trace_ascii("A")
    assert len(levels) == 601                              # a full 601-point read is fine
    with pytest.raises(ValueError):                        # 3 points != 601 -> refuse to publish a short
        drivers.Agilent856xEC(FakeT({"TRA?": "-100.0,-99.0,-98.0"}))._read_trace_ascii("A")
    assert drivers.Agilent856xEC(FakeT({"TRA?": ""}))._read_trace_ascii("A") == ([], [])   # empty=keep-last
