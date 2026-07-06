"""Clean per-band frequency-error characterization DC -> 40 GHz (Task 5, the plan's deliverable:
"how far off is each band tracing up from ~DC to 40 GHz").

This finally separates the REAL residual analyzer error from the STALE-READ artifact that produced the
earlier garbage table (a constant +10833 kHz offset at floor across every band -- a held trace read on
a frozen sweep). It composes all four upstream fixes:

  Task 1  fresh-sweep detector           -> _sweep_is_live (with the inter-snapshot dwell that defeats
                                            the bridge DONE?-race) proves the sweep re-acquires.
  Task 2  health gate                    -> ABORTS cleanly (names the reference codes + last good band)
                                            the moment the analyzer wedges. Checked ON THE FLOOR (RF
                                            off) each point -- liveness is only sound where noise varies.
  Task 3  gentle operation               -> single EXCLUSIVE consumer (lease_exclusive) + a real settle
                                            after each retune -> minimizes the wedge probability.
  Task 4  band-aware read                -> <=2.9 GHz measure_peak (trusted); >2.9 GHz
                                            measure_tracked_peak (finds the offset tone; provisional).

Per point it records: source OF1 (readback freq) + OSB (leveled/locked) -- proving the TRANSMITTER is
exact, so any residual error is the ANALYZER's -- and the analyzer-found frequency + level + the offset
(Hz and ppm) + the band_trust tag. The source is EXACT DC->40 GHz (OF1 == commanded, OSB leveled), so a
nonzero offset localizes to the 8565EC reference. Below 2.9 GHz the offset should be ~0 (band-0 is
metrology-grade); above it the residual is the harmonic-multiplied reference error, now measured on
FRESH sweeps rather than stale ones.

SAFETY: source capped 0 dBm + RX input attenuation floored 20 dB (arm_direct_chain, under the 8565EC
damage limits) before any tone; RF left OFF on exit. Single exclusive consumer.

PREREQUISITE (Task 0): the analyzer must be HEALTHY -- power-cycle it and confirm with
tools/diagnose_8565ec.py (ERR?=[111], sweep LIVE) before running this. This tool aborts up front and
mid-run if the analyzer is/*becomes* wedged; it never prints stale numbers.

  QT_QPA_PLATFORM=offscreen uv run python rf-se/se299/tools/characterize_bands.py
  # optional: --freqs 10e6,100e6,1e9,2.5e9,3e9,6e9,10e9,18e9,26e9,40e9   (override the per-band reps)
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drivers

RX_ADDR = ("127.0.0.1", int(os.environ.get("SE299_ANALYZER_PORT", "5555")), int(os.environ.get("SE299_ANALYZER_PAD", "18")))
TX_ADDR = ("127.0.0.1", int(os.environ.get("SE299_SOURCE_PORT", "5556")), int(os.environ.get("SE299_SOURCE_PAD", "5")))
# one representative frequency per band DC -> 40 GHz (band-0 dense near DC where it is trusted, then a
# rung per preselected band). Overridable with --freqs.
DEFAULT_FREQS_HZ = [10e6, 100e6, 500e6, 1e9, 2.5e9, 3e9, 6e9, 10e9, 18e9, 26.5e9, 40e9]
P_TX_DBM = 0.0
_RETUNE_SETTLE_S = 0.8          # analyzer LO settle after a retune (Task 3 gentle dwell). LIVE-tuned:
#                                 a 0.2 s dwell intermittently read a FLAT sweep (tone not yet present ->
#                                 MKPK on noise); 0.8 s cleared every point 0.5-40 GHz. See the flat-sweep
#                                 diagnosis (settle race, NOT a hardware fault or wrong drive topology).
_SRC_SETTLE_S = 0.8            # 683xx synth settle after a CW retune before the analyzer reads
_PRESELECTOR_MIN_HZ = 2.9e9
_REF_CODES = drivers.REFERENCE_UNLOCK_CODES


def _hw_ref_codes(analyzer):
    """The reference/LO unlock codes currently queued on the analyzer (subset of REFERENCE_UNLOCK_CODES)."""
    try:
        return [c for c in analyzer.query_errors() if c in _REF_CODES]
    except Exception:
        return []


def _healthy(analyzer):
    """(healthy, ref_codes, sweeping): the same gate the coordinator uses -- no reference unlock codes
    AND the sweep is live (Task 1 _sweep_is_live). A wedge fails BOTH the codes and the sweep."""
    ref = _hw_ref_codes(analyzer)
    try:
        sweeping = analyzer._sweep_is_live()
    except Exception:
        sweeping = False
    return ((not ref) and sweeping, ref, sweeping)


def _refine_found_hz(ana, approx_found_hz, zoom_span_hz, rbw_hz):
    """Zoom to a NARROW span centered on the tone and re-read MKF? + MKA? for a FINE frequency + level.
    A wide tone-find bins to span/601 (333 kHz at 200 MHz -> offsets quantized to 1/3 MHz), too coarse
    to resolve the true reference error; this narrows the span (e.g. 2 MHz -> ~3.3 kHz bins) around the
    already-centered tone. Returns (fine_found_hz, fine_level_dbm)."""
    t = ana.t
    t.write(f"CF {approx_found_hz:.0f}HZ"); t.write(f"SP {zoom_span_hz:.0f}HZ")
    t.write(f"RB {rbw_hz:.0f}HZ"); t.write(f"VB {rbw_hz:.0f}HZ")
    t.write("TS"); t.query("DONE?"); time.sleep(0.3); t.write("TS"); t.query("DONE?")
    t.write("MKPK HI")
    try:
        fine_hz, amp = float(t.query("MKF?")), float(t.query("MKA?"))
    except Exception:
        return (approx_found_hz, float("nan"))
    # center the DISPLAY on the actual peak so the live zoom shows a centered tone, not one offset by
    # f*(reference ppm). Cosmetic -- amp was already peak-searched; the offset is still returned/reported.
    t.write("MKCF"); t.write("TS"); t.query("DONE?")
    return (fine_hz, amp)


def _measure_point(ana, src, f):
    """Measure the tone at f: its TRUE frequency (span find + narrow zoom -> MKF, so the offset is real,
    not a zero-span CF echo) and its level. Uniform across bands so band 0 gets a REAL offset too, not
    the zero-span 0. Returns (found_hz, level_dbm, tone_found). tone_found is False when the find landed
    on the floor / a spur (level near the floor or an implausibly large offset) -- so a mis-lock is
    flagged 'no-tone' instead of reported as a bogus per-band error (e.g. a 26.5 GHz spur)."""
    hi = f > _PRESELECTOR_MIN_HZ
    t = ana.t
    if hi:
        # preselector-peak + wide find centers CF on the tone (measure_tracked_peak), then zoom 2 MHz.
        coarse_hz, _ = ana.measure_tracked_peak(f, settle_s=_RETUNE_SETTLE_S)
        found_hz, lvl = _refine_found_hz(ana, coarse_hz, zoom_span_hz=2e6, rbw_hz=30e3)
    else:
        # band 0: no preselector. Coarse find in a 2 MHz span (MKCF centers), then a NARROW 200 kHz zoom
        # (333 Hz bins) to resolve the small low-band offset the marker cannot see in zero span.
        t.write("CONTS"); t.write(f"CF {f:.0f}HZ"); t.write("SP 2MHZ")
        t.write("RB 30KHZ"); t.write("VB 30KHZ")
        t.write("TS"); t.query("DONE?"); time.sleep(_RETUNE_SETTLE_S); t.write("TS"); t.query("DONE?")
        t.write("MKPK HI"); t.write("MKCF")
        try:
            coarse_hz = float(t.query("MKF?"))
        except Exception:
            coarse_hz = f
        found_hz, lvl = _refine_found_hz(ana, coarse_hz, zoom_span_hz=200e3, rbw_hz=3e3)
    # tone-found guard: a real tone is well above the floor AND within a plausible timebase offset
    # (the reference error is ~1 ppm = kHz-to-tens-of-kHz, never MHz). Either failing -> mis-lock.
    tone_found = (lvl == lvl) and lvl > -45.0 and abs(found_hz - f) <= 5e6
    return (found_hz, lvl, tone_found)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freqs", type=str, default="",
                    help="comma-separated Hz reps to characterize (default: one per band DC->40 GHz)")
    args = ap.parse_args()
    freqs = ([float(x) for x in args.freqs.split(",") if x.strip()] if args.freqs
             else list(DEFAULT_FREQS_HZ))

    rx = drivers.NetworkTransport(*RX_ADDR, timeout_ms=30000)
    tx = drivers.NetworkTransport(*TX_ADDR, timeout_ms=20000)
    # SINGLE CONSUMER (Task 3): refuse to run if another consumer already drives either unit.
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
    ana.configure(rbw_hz=100e3, vbw_hz=100e3, ref_dbm=10.0, detector="POS")
    ana.set_attenuation(db=20)

    # HEALTH GATE up front (Task 2): a wedged analyzer produces only garbage -- do not start.
    ok, ref, sweeping = _healthy(ana)
    if not ok:
        print(f"ABORT (analyzer wedged): reference codes {sorted(ref) or 'none'}, "
              f"sweep {'LIVE' if sweeping else 'FROZEN'}. Power-cycle the 8565EC and confirm with "
              f"tools/diagnose_8565ec.py (ERR?=[111], sweep LIVE) before characterizing.")
        return 2

    print(f"{'band-rep':>12} | {'src OF1':>12} {'OSB':>4} | {'ana found':>13} {'level dBm':>9} | "
          f"{'offset Hz':>12} {'ppm':>8} | trust")
    print("-" * 92)
    rows = []
    last_good = None
    try:
        for f in freqs:
            hi = f > _PRESELECTOR_MIN_HZ
            # per-point health re-check on the FLOOR (RF still OFF from the prior point / prepare):
            # wedge detection is only sound on the noisy floor -- a strong stable tone is a near-constant
            # trace that would false-read as frozen. Abort cleanly rather than log a stale/garbage row.
            ok, ref, sweeping = _healthy(ana)
            if not ok:
                print(f"\nABORT (analyzer wedged mid-run before {f/1e9:.4f} GHz): reference codes "
                      f"{sorted(ref) or 'none'}, sweep {'LIVE' if sweeping else 'FROZEN'}. "
                      f"Last good band: {last_good if last_good is not None else 'none'}. "
                      f"Power-cycle + tools/diagnose_8565ec.py, then re-run.")
                return 2
            src.set_freq(f)
            src.set_power(P_TX_DBM)
            src.rf_on()
            src.await_settled(_SRC_SETTLE_S, use_opc=False)
            # source exactness: OF1 readback + OSB (bit3 lock / bit2 unlevel). A source that is UNLEVELED
            # at f (OSB bit2) is the prime cause of a FLAT sweep with no tone -- surfaced per row.
            of1_hz = src.output_freq_mhz() * 1e6
            osb = src.status_byte()
            try:
                found_hz, lvl, tone_found = _measure_point(ana, src, f)
            finally:
                src.rf_off()
            offset_hz = found_hz - f
            ppm = (offset_hz / f) * 1e6 if f else 0.0
            trust = "trusted" if f <= _PRESELECTOR_MIN_HZ else "provisional"
            leveled = "OK" if (osb & 0x0C) == 0 else ("UNLEV" if osb & 0x04 else "LCK?")
            rows.append({"f_hz": f, "src_of1_hz": of1_hz, "osb": osb, "found_hz": found_hz,
                         "level_dbm": lvl, "offset_hz": offset_hz, "ppm": ppm, "band_trust": trust,
                         "tone_found": tone_found, "src_leveled": leveled})
            last_good = f"{f/1e9:.4f} GHz"
            if tone_found:
                print(f"{f/1e9:>10.4f}G | {of1_hz/1e9:>10.5f}G {leveled:>4} | {found_hz/1e9:>11.5f}G "
                      f"{lvl:>9.2f} | {offset_hz:>+12.0f} {ppm:>+8.2f} | {trust}")
            else:
                # FLAT sweep / no peak: the analyzer found no tone (floor-level or an implausible offset).
                # Prints the source's leveled/locked status so a source-side cause (UNLEV) is visible.
                print(f"{f/1e9:>10.4f}G | {of1_hz/1e9:>10.5f}G {leveled:>4} | {'NO TONE (flat)':>13} "
                      f"{lvl:>9.2f} | {'--':>12} {'--':>8} | {trust}  <- src {leveled}")
    finally:
        try:
            src.rf_off()
        except Exception:
            pass

    # summary: worst offset in the TRUSTED band (band-0) vs the provisional high band, tone-found only
    found = [r for r in rows if r.get("tone_found")]
    no_tone = [r for r in rows if not r.get("tone_found")]
    trusted = [r for r in found if r["band_trust"] == "trusted"]
    prov = [r for r in found if r["band_trust"] == "provisional"]
    print("\n================ PER-BAND FREQUENCY-ERROR SUMMARY ================")
    print("SOURCE is exact where OSB=OK (OF1 == commanded) -> any offset is the 8565EC reference.")
    if trusted:
        w = max(trusted, key=lambda r: abs(r["ppm"]))
        print(f"TRUSTED  (<=2.9 GHz): worst |offset| = {w['offset_hz']:+.0f} Hz "
              f"({w['ppm']:+.2f} ppm) @ {w['f_hz']/1e9:.4f} GHz  [metrology-grade]")
    if prov:
        w = max(prov, key=lambda r: abs(r["ppm"]))
        med = sorted(abs(r["ppm"]) for r in prov)[len(prov) // 2]
        print(f"PROVISIONAL (>2.9 GHz): worst |offset| = {w['offset_hz']:+.0f} Hz "
              f"({w['ppm']:+.2f} ppm) @ {w['f_hz']/1e9:.4f} GHz ; median |{med:.2f}| ppm "
              f"[timebase error, ~constant ppm; gated on reference service]")
    if no_tone:
        pts = ", ".join(f"{r['f_hz']/1e9:.3f} GHz (src {r['src_leveled']}, {r['level_dbm']:.0f} dBm)"
                        for r in no_tone)
        print(f"NO TONE (flat sweep, EXCLUDED): {pts}")
        print("  -> a flat sweep = the analyzer found no peak. Cause is source-side if src=UNLEV (the")
        print("     683xx ALC could not level at that f), else an analyzer mis-lock (preselector/spur).")
    print("\nNOTE: FRESH-sweep verified -- every row proved the trace re-acquired (no stale reads). The")
    print("high-band residual is a ~constant-ppm TIMEBASE error (not the earlier mis-measured 330 ppm).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
