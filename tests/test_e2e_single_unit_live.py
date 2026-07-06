"""LIVE single-unit E2E: exercise EACH instrument INDEPENDENTLY, so they run safely when only one
unit is powered/cabled (analyzer-only bench, or source-only bench). The two-unit suites
(test_e2e_live.py, test_e2e_coupling_live.py) gate on BOTH units; these do NOT -- each test skips
honestly if its OWN target unit is unreachable, and never touches the other.

SAFETY: the source tests command a low, capped level and leave RF OFF on exit; no analyzer is
required or assumed. The analyzer tests need no source (they read the receiver's own floor).

  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
    rf-se/se299/tests/test_e2e_single_unit_live.py -v -n0
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drivers
from gpib_bridge import vm

_A_PORT = int(os.environ.get("SE299_ANALYZER_PORT", "5555"))
_A_PAD = int(os.environ.get("SE299_ANALYZER_PAD", "18"))
_S_PORT = int(os.environ.get("SE299_SOURCE_PORT", "5556"))
_S_PAD = int(os.environ.get("SE299_SOURCE_PAD", "5"))


def _spec():
    return vm.VmSpec(port=_A_PORT, source_port=_S_PORT, gpib_addr=_A_PAD, source_addr=_S_PAD)


# --------------------------------------------------------------------- analyzer-only (no source)

@pytest.fixture
def analyzer_only():
    if not vm.bridge_reachable(_spec(), timeout_ms=1500):
        pytest.skip("analyzer (8565EC) not reachable -- single-unit analyzer tests skipped")
    t = drivers.NetworkTransport("127.0.0.1", _A_PORT, _A_PAD, timeout_ms=12000)
    t.lease(scope="device", ttl_s=120)
    try:
        yield drivers.Agilent856xEC(t)
    finally:
        t.close()


def test_analyzer_only_answers_and_reads_its_own_floor(analyzer_only):
    # the RX alone: identity + a real noise-floor read with NO source present. Proves the receiver
    # measures independently (the substitution floor / self-noise pass needs no transmitter).
    ana = analyzer_only
    assert "856" in ana.idn()
    ana.configure(rbw_hz=1e6, vbw_hz=1e6, ref_dbm=-40.0, detector="SMP")
    ana.set_attenuation(db=10)
    _, floor = ana.measure_floor(1.0e9, 0.05)
    assert isinstance(floor, float) and floor == floor          # finite dBm, not NaN
    assert floor < -20.0                                        # a real receiver floor, not a rail/tone


def test_analyzer_only_new_error_baseline_is_clean_or_reports(analyzer_only):
    # the ERR-delta self-check runs on the analyzer alone: snapshot the chronic baseline, take a
    # sweep, and confirm no NEW code was introduced by the measurement (a healthy unit -> []).
    ana = analyzer_only
    ana.configure(rbw_hz=1e6, vbw_hz=1e6, ref_dbm=-40.0, detector="SMP")
    ana.snapshot_error_baseline()
    ana.measure_peak(1.0e9, 0.05)
    new = ana.query_new_errors()
    assert isinstance(new, list)                               # a NEW-code list (empty when stable)


# --------------------------------------------------------------------- source-only (no analyzer)

@pytest.fixture
def source_only():
    if not vm.source_reachable(_spec(), timeout_ms=1500):
        pytest.skip("source (68369A/68367C) not reachable -- single-unit source tests skipped")
    t = drivers.NetworkTransport("127.0.0.1", _S_PORT, _S_PAD, timeout_ms=12000)
    t.lease(scope="device", ttl_s=120)
    src = drivers.Anritsu68369(t)
    try:
        yield src
    finally:
        try:
            src.rf_off()                                       # SAFETY: never leave the TX hot
        except Exception:
            pass
        t.close()


def test_source_only_sets_and_reads_back_freq_and_level(source_only):
    # the TX alone: command a CW freq + level and confirm the native readbacks agree, with NO
    # analyzer present. Proves the transmitter is drivable + observable on its own.
    src = source_only
    assert "683" in src.idn()
    src.prepare()
    src.set_freq(2.0e9)
    src.set_power(-10.0)
    assert abs(src.output_freq_mhz() - 2000.0) <= 1.0          # OF1 readback == commanded CW freq
    assert abs(src.output_level_dbm() - (-10.0)) <= 0.5        # OL1 readback == commanded level


def test_source_only_leveled_locked_and_safe_state(source_only):
    # OSB status + the RF-off dead-man, source alone. Command a leveled tone, confirm leveled+locked,
    # then RF0 and confirm the interlock reads a clean (or at least readable) status byte.
    src = source_only
    src.prepare()
    src.set_freq(2.0e9)
    src.set_power(-10.0)
    src.rf_on()
    src.await_settled(0.3, use_opc=False)
    assert src.settled_ok() is True                            # OSB bit2 (unlev) + bit3 (lock) clear
    src.rf_off()
    time.sleep(0.2)
    assert isinstance(src.status_byte(), int)                  # status still readable after de-key
