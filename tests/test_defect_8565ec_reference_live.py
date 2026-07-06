"""LIVE defect-confirmation suite for the 8565EC reference/first-LO fault (audit 2026-07-04,
section "DEFECT: nature, impact, canonical resolution"). This is a DIAGNOSTIC harness, not a
correctness gate: it asserts the SIGNATURES that isolate the fault to the ANALYZER's precision
frequency-reference chain (A21 OCXO / A15 / A4) and confirm it, so the finding is reproducible for a
service/replacement decision.

Signatures asserted:
  1. SOURCE IS HEALTHY (gentle) -- the 68367C reports OF1 EXACT + OSB leveled+locked at 2/6/10 GHz,
     so any frequency error is the ANALYZER's, not the transmitter's. (Robust, always true.)
  2. STRESS INDUCES THE REFERENCE LOCK-LOSS WEDGE (opt-in, DESTRUCTIVE) -- under rapid retune + RF
     toggling the reference-derived PLLs unlock (333/335/337/499) and the sweep freezes; recovers only
     on a power-cycle. This was the DETERMINISTIC signature throughout debugging. Opt-in because it
     wedges the analyzer (needs a power-cycle after): set SE299_DEFECT_STRESS=1.
  3. WEDGED-STATE FINGERPRINT (conditional) -- when the analyzer IS wedged, assert the reference codes
     are present, the sweep is frozen, and the FDIAG synth relation MROLL == RAWOSC/(2*POSTSC) is
     BROKEN. Skips when the analyzer is healthy.
  4. EXTERNAL-REFERENCE LOCALIZATION (opt-in, needs a known-good 10 MHz on rear J9) -- FREF EXT then
     ERR?: codes clear => internal A21 OCXO; persist => downstream A15/A4/A3. set SE299_EXT_REF=1.

  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
    rf-se/se299/tests/test_defect_8565ec_reference_live.py -v -n0
  # to run the destructive stress confirmation (wedges the analyzer):
  SE299_DEFECT_STRESS=1 QT_QPA_PLATFORM=offscreen uv run ... -k stress
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drivers
from gpib_bridge import vm

_A = ("127.0.0.1", int(os.environ.get("SE299_ANALYZER_PORT", "5555")), int(os.environ.get("SE299_ANALYZER_PAD", "18")))
_S = ("127.0.0.1", int(os.environ.get("SE299_SOURCE_PORT", "5556")), int(os.environ.get("SE299_SOURCE_PAD", "5")))
# reference/timebase + cal-osc unlock codes, as STRINGS (this suite compares raw ERR? tokens).
# Canonical source is drivers.REFERENCE_UNLOCK_CODES -- derived so the set never drifts.
_REF_CODES = {str(c) for c in drivers.REFERENCE_UNLOCK_CODES}   # 600-ref / sampler / frac-N / cal-osc


def _spec():
    return vm.VmSpec(port=_A[1], source_port=_S[1], gpib_addr=_A[2], source_addr=_S[2])


def _hw_codes(raw):
    return [c for c in raw.replace(" ", "").split(",") if c.strip().isdigit() and 200 <= int(c) <= 799]


def _analyzer():
    if not vm.bridge_reachable(_spec(), timeout_ms=1500):
        pytest.skip("analyzer (8565EC) not reachable")
    t = drivers.NetworkTransport(*_A, timeout_ms=15000); t.lease(scope="device", ttl_s=200)
    return t


def _source():
    if not vm.source_reachable(_spec(), timeout_ms=1500):
        pytest.skip("source (68367C) not reachable")
    t = drivers.NetworkTransport(*_S, timeout_ms=12000); t.lease(scope="device", ttl_s=200)
    return t


# ------------------------------------------------------------- 1. source healthy -> isolates analyzer

def test_source_frequency_reference_is_exact_isolating_the_analyzer():
    # The transmitter's own frequency reference is GOOD: OF1 reads back the commanded CW frequency
    # EXACTLY and OSB reports leveled+locked. So the high-band frequency error we see on the analyzer
    # cannot be the source -- it isolates the fault to the 8565EC.
    t = _source()
    try:
        src = drivers.Anritsu68369(t)
        for f in (2e9, 6e9, 10e9):
            src.set_freq(f); src.set_power(0.0); src.rf_on(); time.sleep(0.5)
            of1_hz = src.output_freq_mhz() * 1e6
            osb = src.status_byte()
            assert abs(of1_hz - f) <= 1000.0, f"source OF1 off by {of1_hz-f:.0f} Hz @ {f/1e9} GHz (source, not analyzer)"
            assert (osb & 0x08) == 0, f"source lock error (OSB 0x{osb:02X}) @ {f/1e9} GHz"
        src.rf_off()
    finally:
        try:
            drivers.Anritsu68369(t).rf_off()
        except Exception:
            pass
        t.close()


# --------------------------------------------------------------- 2. stress induces the wedge (opt-in)

@pytest.mark.skipif(os.environ.get("SE299_DEFECT_STRESS") != "1",
                    reason="destructive: wedges the analyzer (needs a power-cycle). Set SE299_DEFECT_STRESS=1 to run.")
def test_stress_induces_reference_lock_loss_wedge():
    # THE deterministic confirmation: drive the analyzer's first LO hard (rapid wide retune + RF
    # toggling) from a healthy state; the marginal reference-derived PLLs unlock (333/335/337/499) and
    # the sweep freezes. A healthy analyzer would NOT unlock under mere retuning.
    at = _analyzer(); st = _source()
    ana = drivers.Agilent856xEC(at); src = drivers.Anritsu68369(st)
    def hw():
        return _hw_codes(at.query("ERR?"))
    try:
        if hw():
            pytest.skip("analyzer already wedged -- power-cycle before running the stress confirmation")
        drivers.arm_direct_chain(src, ana, source_cap_dbm=0.0, rx_min_atten_db=20.0)
        wedged = False
        for i in range(40):                       # rapid wide retune + RF toggle burst
            f = 1e9 + (i % 10) * 3.9e9            # 1 -> ~36 GHz sweep, fast, no settle (LO stress)
            src.set_freq(f); at.write(f"CF {f/1e6:.0f}MHZ"); at.write("SP 0HZ")
            src.rf_on() if i % 2 else src.rf_off()
            at.write("TS")
            if i % 5 == 4 and hw():
                wedged = True; break
        src.rf_off()
        codes = hw()
        assert wedged and (set(codes) & _REF_CODES), (
            f"stress did not induce the reference lock-loss wedge (codes={codes}) -- the analyzer "
            "reference may have been repaired/replaced, or the stress was insufficient")
    finally:
        try:
            src.rf_off()
        except Exception:
            pass
        at.close(); st.close()


# --------------------------------------------------------------- 3. wedged-state fingerprint (conditional)

def test_wedged_state_fingerprint_when_analyzer_is_wedged():
    # When the analyzer IS wedged, confirm the full fingerprint: reference lock-loss codes present, the
    # sweep frozen (trace does not re-acquire), and the FDIAG synth relation broken. Skips if healthy.
    t = _analyzer()
    try:
        codes = _hw_codes(t.query("ERR?"))
        if not (set(codes) & _REF_CODES):
            pytest.skip("analyzer not currently wedged (no reference lock-loss codes) -- nothing to fingerprint")
        # sweep frozen?
        def snap():
            t.write("CF 1500MHZ"); t.write("SP 100MHZ"); t.write("CLRW TRA"); t.write("TDF P")
            t.write("TS"); t.query("DONE?")
            return [float(x) for x in t.query("TRA?").split(",") if x.strip() and any(c.isdigit() for c in x)]
        a = snap(); time.sleep(0.5); b = snap()
        n = min(len(a), len(b)); changed = sum(1 for i in range(n) if abs(a[i] - b[i]) > 0.05) if n else 0
        assert changed <= 3, f"expected a frozen sweep while wedged, but {changed}/{n} points changed"
        # FDIAG synth relation broken?  MROLL should == RAWOSC / (2*POSTSC) when locked
        def fd(osc):
            t.write("CF 1000MHZ")
            try:
                return float(t.query(f"FDIAG {osc},?"))
            except Exception:
                return None
        mroll, raw, post = fd("MROLL"), fd("RAWOSC"), fd("POSTSC")
        if None not in (mroll, raw, post) and post:
            expected = raw / (2 * post)
            assert abs(mroll - expected) > 1e3, (
                f"FDIAG relation held ({mroll:.0f} ~= {expected:.0f}) -- reference may be locked despite codes")
    finally:
        t.close()


# --------------------------------------------------------------- 4. external-reference localization (opt-in)

@pytest.mark.skipif(os.environ.get("SE299_EXT_REF") != "1",
                    reason="needs a known-good 10 MHz on rear J9. Set SE299_EXT_REF=1 to run the A21-vs-downstream split.")
def test_external_reference_localizes_A21_vs_downstream():
    # DECISIVE localizer: on a wedged unit, switch to an external 10 MHz reference. If the reference
    # lock-loss codes CLEAR, the internal A21 OCXO is the culprit; if they PERSIST, the fault is
    # downstream (A15 600 MHz gen / A4 cal osc / A3 ADC / a supply rail). Reports, does not hard-fail.
    t = _analyzer()
    try:
        before = _hw_codes(t.query("ERR?"))
        if not (set(before) & _REF_CODES):
            pytest.skip("analyzer not wedged -- run this while the reference codes are present")
        t.write("FREF EXT"); time.sleep(3.0)
        after = _hw_codes(t.query("ERR?"))
        t.write("FREF INT")
        still = set(after) & _REF_CODES
        verdict = "internal A21 OCXO (codes cleared on external ref)" if not still else \
                  f"downstream A15/A4/A3 or a rail (codes persist on external ref: {sorted(still)})"
        print(f"\nEXTERNAL-REF LOCALIZATION: {verdict}")
        assert isinstance(after, list)            # informational; the print carries the verdict
    finally:
        t.close()
