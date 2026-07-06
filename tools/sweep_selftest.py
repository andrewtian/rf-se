"""tools/sweep_selftest.py -- low->high full-band GO/NO-GO self-test for the source+analyzer sweep.

THE question this answers: does commanding a tone and reading it back actually WORK at every frequency
from as low as possible to as high as possible? For each point it commands a KNOWN tone, CONFIRMS the
68367C is emitting on-frequency + leveled (native OF1 / OSB), then reads the 8565EC over a fixed window
through the SAME path the operator Point Op GUI uses (configure -> set_frequency -> preselector-peak +
window-restore above 2.9 GHz -> fresh-sweep full-trace read), and classifies each point:

  PASS     tone rises clearly (>= TONE_MARGIN dB) over the floor -- the sweep works at this frequency
  NO-TONE  source leveled + on-freq but no tone at the analyzer (coupling / path issue, NOT the sweep)
  NO-SRC   source not leveled or off-frequency (a source problem, not the read path)
  BLANK    analyzer returned a cleared / unswept trace after retries (a READ-PATH bug -- the thing to fix)

Single EXCLUSIVE consumer, health-gated (halts on a wedged analyzer instead of logging garbage), source
capped 0 dBm + RX input attenuation floored 20 dB (arm_direct_chain), RF off on exit. Prints a per-point
table then a VERDICT naming the verified low..high span and any BLANK / NO-TONE point. This is the
rehearsed way to KNOW the full-band sweep is healthy end to end -- passing means the operator sweep works.

Run (golden two-VM up):
  uv run --group se299-gui python tools/sweep_selftest.py
  uv run --group se299-gui python tools/sweep_selftest.py --freqs 5e8,1e9,2.4e9,3e9,6e9,1e10,4e10
  uv run --group se299-gui python tools/sweep_selftest.py --power -10 --span 5e6
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drivers

RX_ADDR = ("127.0.0.1", int(os.environ.get("SE299_ANALYZER_PORT", "5555")), int(os.environ.get("SE299_ANALYZER_PAD", "18")))
TX_ADDR = ("127.0.0.1", int(os.environ.get("SE299_SOURCE_PORT", "5556")), int(os.environ.get("SE299_SOURCE_PAD", "5")))

# low -> high, crossing the 2.9 GHz preselector boundary. Dense-ish low, a rung per preselected band up
# top. "as low as possible": the 68367C source floor is ~10 MHz, but the operator directive skips the
# 10 MHz source, so the default low rung is 500 MHz (override with --freqs to push lower/higher).
DEFAULT_FREQS_HZ = [500e6, 1e9, 2.0e9, 2.45e9, 3.0e9, 5.0e9, 10e9, 18e9, 26.5e9, 40e9]

PRESELECTOR_MIN_HZ = 2.9e9
TONE_MARGIN_DB = 15.0          # peak must clear the floor by this much to count as a tone
_DEGENERATE_SPAN_DB = 0.3      # trace peak-to-trough below this = a CLEARED/unswept trace (blank), not real
_READ_RETRIES = 4             # re-sweep this many times if the trace comes back blank (first-sweep flush)
_SRC_SETTLE_S = 0.8           # fixed 683xx synth dwell after a CW retune (use_opc=False); LIVE-tuned value
_REF_CODES = drivers.REFERENCE_UNLOCK_CODES


def _median(xs):
    s = sorted(xs)
    return s[len(s) // 2]


def _healthy(ana):
    """(ok, ref_codes, sweeping): analyzer is usable if it shows no reference-unlock codes AND its floor
    sweep is live. Sound only on the noisy floor (RF off) -- a strong stable tone is a near-constant
    trace indistinguishable from a frozen one, so we gate BEFORE keying the source each point."""
    try:
        codes = {c for c in ana.query_errors() if c in _REF_CODES}
    except Exception:
        codes = set()
    try:
        sweeping = ana._sweep_is_live()
    except Exception:
        sweeping = True
    return (not codes and sweeping, codes, sweeping)


def _gui_read(ana, f_hz, span_hz, ref_dbm):
    """Read a trace exactly the way the Point Op SpectrumEngine does: configure + set_frequency, and
    above 2.9 GHz peak the preselector then RESTORE the measurement window (peak_preselector self-guards
    against mis-tuning onto noise and leaves SP=200 MHz + an MKCF recenter, so we re-assert CF+span).
    Retries the sweep if the trace comes back CLEARED/unswept (all-equal, distinguishable from a real
    tone/floor which carries noise) -- the first sweep after a fresh configure can be blank."""
    ana.configure(0.0, 0.0, ref_dbm, "POS")                 # rbw/vbw auto, ref, positive-peak detector
    ana.set_frequency(center_hz=f_hz, span_hz=span_hz)
    ana.set_attenuation(db=20)
    if f_hz > PRESELECTOR_MIN_HZ:
        ana.peak_preselector(f_hz)                          # no-op if the tone is not yet present
        ana.set_frequency(center_hz=f_hz, span_hz=span_hz)  # restore the operator window after PP's zoom
    freqs, levels = [], []
    for _ in range(_READ_RETRIES):
        ana.arm_and_wait(timeout_s=6.0)
        freqs, levels = ana.read_trace("A")
        if levels and (max(levels) - min(levels)) > _DEGENERATE_SPAN_DB:
            break                                           # a real swept trace (tone or noisy floor)
    return freqs, levels


def _classify(src_ok, freqs, levels):
    if not levels or (max(levels) - min(levels)) <= _DEGENERATE_SPAN_DB:
        return "BLANK", (max(levels) - _median(levels) if levels else 0.0)
    over = max(levels) - _median(levels)
    if not src_ok:
        return "NO-SRC", over
    return ("PASS" if over >= TONE_MARGIN_DB else "NO-TONE"), over


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freqs", type=str, default="", help="comma-separated Hz points (default low->40 GHz)")
    ap.add_argument("--power", type=float, default=-10.0, help="source power dBm (capped 0 dBm)")
    ap.add_argument("--span", type=float, default=5e6, help="analyzer span Hz (operator window)")
    ap.add_argument("--ref", type=float, default=0.0, help="analyzer reference level dBm")
    args = ap.parse_args()
    freqs = ([float(x) for x in args.freqs.split(",") if x.strip()] if args.freqs
             else list(DEFAULT_FREQS_HZ))

    rx = drivers.NetworkTransport(*RX_ADDR, timeout_ms=30000)
    tx = drivers.NetworkTransport(*TX_ADDR, timeout_ms=20000)
    try:
        drivers.lease_exclusive(rx, "8565EC analyzer (RX)", ttl_s=600)
        drivers.lease_exclusive(tx, "68367C source (TX)", ttl_s=600)
    except drivers.SingleConsumerConflict as e:
        print(f"ABORT (single-consumer): {e}")
        return 3

    ana = drivers.Agilent856xEC(rx)
    src = drivers.Anritsu68369(tx)
    drivers.arm_direct_chain(src, ana, source_cap_dbm=0.0, rx_min_atten_db=20.0, cable_loss_db=0.0)
    src.prepare()

    ok, ref, sweeping = _healthy(ana)
    if not ok:
        print(f"ABORT (analyzer wedged): reference codes {sorted(ref) or 'none'}, "
              f"sweep {'LIVE' if sweeping else 'FROZEN'}. Power-cycle the 8565EC and confirm with "
              f"tools/diagnose_8565ec.py (ERR?=[111], sweep LIVE) before the self-test.")
        return 2

    print(f"low->high sweep self-test: {len(freqs)} points, tone {args.power:+.0f} dBm, "
          f"span {args.span/1e6:.1f} MHz, tone-margin {TONE_MARGIN_DB:.0f} dB")
    print(f"{'freq':>10} | {'src OF1':>11} {'lvl':>5} | {'peak dBm':>9} {'floor':>7} {'over dB':>8} | verdict")
    print("-" * 72)
    rows = []
    last_good = None
    try:
        for f in freqs:
            ok, ref, sweeping = _healthy(ana)                # per-point floor health gate (RF still off)
            if not ok:
                print(f"\nABORT (analyzer wedged before {f/1e9:.3f} GHz): reference codes "
                      f"{sorted(ref) or 'none'}, sweep {'LIVE' if sweeping else 'FROZEN'}. Last good: "
                      f"{last_good or 'none'}. Power-cycle + tools/diagnose_8565ec.py, then re-run.")
                return 2
            src.set_freq(f)
            src.set_power(args.power)
            src.rf_on()
            src.await_settled(_SRC_SETTLE_S, use_opc=False)  # fixed dwell: the tone must be present BEFORE PP
            of1_hz = src.output_freq_mhz() * 1e6
            osb = src.status_byte()
            src_ok = abs(of1_hz - f) < max(1e6, f * 1e-4) and (osb & 0x0C) == 0   # on-freq + leveled+locked
            try:
                freqs_r, levels = _gui_read(ana, f, args.span, args.ref)
            finally:
                src.rf_off()
            verdict, over = _classify(src_ok, freqs_r, levels)
            peak = max(levels) if levels else float("nan")
            floor = _median(levels) if levels else float("nan")
            lvl = "OK" if (osb & 0x0C) == 0 else ("UNLEV" if osb & 0x04 else "LCK?")
            rows.append({"f_hz": f, "verdict": verdict, "over_db": over, "peak_dbm": peak,
                         "floor_dbm": floor, "src_of1_hz": of1_hz, "src_leveled": lvl})
            print(f"{f/1e9:>8.3f}G | {of1_hz/1e9:>9.4f}G {lvl:>5} | {peak:>9.1f} {floor:>7.1f} "
                  f"{over:>8.1f} | {verdict}")
            last_good = f"{f/1e9:.3f} GHz"
    finally:
        src.rf_off()
        try:
            ana.t.write("CONTS")
        except Exception:
            pass

    # VERDICT
    passed = [r for r in rows if r["verdict"] == "PASS"]
    blanks = [r for r in rows if r["verdict"] == "BLANK"]
    notone = [r for r in rows if r["verdict"] == "NO-TONE"]
    nosrc = [r for r in rows if r["verdict"] == "NO-SRC"]
    print("-" * 72)
    if passed:
        lo, hi = passed[0]["f_hz"], passed[-1]["f_hz"]
        print(f"VERIFIED sweep span: {lo/1e9:.3f} GHz .. {hi/1e9:.3f} GHz "
              f"({len(passed)}/{len(rows)} points PASS)")
    else:
        print("VERIFIED sweep span: NONE -- no point produced a tone over the floor")
    def _flist(rs):
        return ", ".join(f"{r['f_hz']/1e9:.3f}G" for r in rs)
    if blanks:
        print(f"BLANK (read-path bug) at: {_flist(blanks)}")
    if notone:
        print(f"NO-TONE (coupling/path, source was OK) at: {_flist(notone)}")
    if nosrc:
        print(f"NO-SRC (source not leveled/on-freq) at: {_flist(nosrc)}")
    healthy = not blanks and not nosrc and bool(passed)
    print(f"\nRESULT: {'SWEEP HEALTHY' if healthy and not notone else 'ISSUES FOUND'} "
          f"(BLANK is a read bug to fix; NO-TONE is a physical coupling issue, not the sweep).")
    return 0 if (healthy and not blanks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
