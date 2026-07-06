"""diagnose_8565ec.py -- run RIGHT AFTER power-cycling the 8565EC to auto-capture the cold->relapse
diagnostic sequence that localizes the reference/LO fault (audit 2026-07-04). It answers, without a
bench tech:
  * what does the analyzer report COLD (ERR?=0? sweep live? FDIAG oscillator values)
  * WHEN does it relapse (seconds from cold) and does it pin to the ~5-min auto-cal cadence
  * in what ORDER do the codes appear -- reference codes (333/335/337/499) FIRST => reference-chain
    primary, YTO codes (317/319/351/353) following => secondary (confirms the corrected diagnosis)
  * how the FDIAG oscillator values DIFF cold vs warm (which loop drifted)
  * [optional] whether feeding an EXTERNAL 10 MHz reference clears the reference codes -- the decisive
    split between the internal A21 OCXO and a downstream (A15/A4/A3) fault

It is a SINGLE-consumer, GENTLE poller (default 30 s) -- aggressive concurrent probing can wedge the
NI board (documented). It never sends the STB text query (that is a serial-poll register, not a
command, and querying it poisons the socket); it uses ERR? + a trace-change sweep-alive check.

  # cold-start, log for 30 min or until relapse (+confirmation), print the summary:
  QT_QPA_PLATFORM=offscreen uv run python rf-se/se299/tools/diagnose_8565ec.py
  # if you have a known-good 10 MHz on the rear EXT REF (J9), add the decisive reference test:
  QT_QPA_PLATFORM=offscreen uv run python rf-se/se299/tools/diagnose_8565ec.py --external-ref
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drivers

ADDR = ("127.0.0.1", int(os.environ.get("SE299_ANALYZER_PORT", "5555")), int(os.environ.get("SE299_ANALYZER_PAD", "18")))
FDIAG_OSC = ("LO", "SMP", "HARM", "MROLL", "RAWOSC", "POSTSC")
# reference/timebase + cal-osc unlock codes, as STRINGS (this tool compares the raw ERR? tokens).
# Canonical source is drivers.REFERENCE_UNLOCK_CODES -- derived here so the set never drifts.
REF_CODES = {str(c) for c in drivers.REFERENCE_UNLOCK_CODES}
# WARM/wedged baseline captured 2026-07-04 @ CF 1 GHz (for reference in the cold-vs-warm diff)
WARM_BASELINE = {"LO": 3.785455335e9, "SMP": 2.06668511e8, "HARM": 14.0,
                 "MROLL": 2.9899778e7, "RAWOSC": 2.98266060e8, "POSTSC": 5.0}


def _hw_codes(codes):
    """the codes that indicate hardware (200-799 per the 8560 manual); 100-series are benign parser."""
    out = []
    for c in codes:
        try:
            n = int(c)
        except ValueError:
            continue
        if 200 <= n <= 799:
            out.append(c)
    return out


class Rx:
    """single leased analyzer link with reconnect-on-poison (a bad mnemonic must not kill a 30 min run)."""
    def __init__(self):
        self.t = drivers.NetworkTransport(*ADDR, timeout_ms=12000)
        # SINGLE CONSUMER (Task 3): refuse if another consumer already holds the analyzer -- a second
        # concurrent prober is exactly the contention that worsens the reference wedge.
        drivers.lease_exclusive(self.t, "8565EC analyzer (RX)", ttl_s=600)

    def q(self, cmd):
        try:
            return self.t.query(cmd).strip()
        except Exception:
            try:
                self.t.reconnect(); self.t.lease(scope="device", ttl_s=600)
            except Exception:
                pass
            return "<no-resp>"

    def w(self, cmd):
        try:
            self.t.write(cmd)
        except Exception:
            try:
                self.t.reconnect(); self.t.lease(scope="device", ttl_s=600); self.t.write(cmd)
            except Exception:
                pass

    def err(self):
        r = self.q("ERR?")
        return [c for c in r.replace(" ", "").split(",") if c.strip().isdigit()] if r != "<no-resp>" else []

    def fdiag(self, cf_mhz=1000):
        self.w(f"CF {cf_mhz}MHZ")
        d = {}
        for osc in FDIAG_OSC:
            r = self.q(f"FDIAG {osc},?")
            try:
                d[osc] = float(r)
            except ValueError:
                d[osc] = None
        return d

    def sweep_alive(self):
        """trace-change check: a live sweep re-acquires (points differ), a wedged one holds."""
        def snap():
            self.w("TDF P"); self.w("TS"); self.q("DONE?")
            r = self.q("TRA?")
            return [float(x) for x in r.split(",") if x.strip() and any(ch.isdigit() for ch in x)]
        self.w("CF 1500MHZ"); self.w("SP 100MHZ"); self.w("CLRW TRA")
        a = snap(); time.sleep(0.5); b = snap()
        n = min(len(a), len(b))
        return (sum(1 for i in range(n) if abs(a[i] - b[i]) > 0.05) if n else 0) > 3

    def close(self):
        try:
            self.t.close()
        except Exception:
            pass


def _fmt_fdiag(d):
    return " ".join(f"{k}={('%.6g' % v) if v is not None else '?'}" for k, v in d.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=1800.0, help="max seconds to log (default 1800 = 30 min)")
    ap.add_argument("--interval", type=float, default=30.0, help="poll interval seconds (gentle; default 30)")
    ap.add_argument("--external-ref", action="store_true",
                    help="after relapse, test FREF EXT (needs a known-good 10 MHz on rear J9) -- the decisive split")
    args = ap.parse_args()

    try:
        rx = Rx()
    except drivers.SingleConsumerConflict as e:
        print(f"ABORT (single-consumer): {e}")
        return 3
    print(f"ID? = {rx.q('ID?')}")
    print("Set a fixed known state (IP + CF 1 GHz) and log ERR?/sweep/FDIAG from COLD ...\n")
    rx.w("IP"); time.sleep(1.0); rx.w("CF 1000MHZ"); rx.w("SP 0HZ")

    t0 = time.time()
    cold_err = rx.err()
    cold_fdiag = rx.fdiag()
    cold_alive = rx.sweep_alive()
    print(f"COLD  t+0s   ERR?={cold_err or ['0']}  sweep={'LIVE' if cold_alive else 'FROZEN'}")
    print(f"      FDIAG {_fmt_fdiag(cold_fdiag)}\n")

    seen = set(_hw_codes(cold_err))
    order = []                          # (elapsed_s, code) as each NEW hardware code first appears
    relapse_t = None
    relapse_fdiag = None
    confirm_polls = 0

    while time.time() - t0 < args.duration:
        time.sleep(args.interval)
        el = time.time() - t0
        codes = rx.err()
        alive = rx.sweep_alive()
        hw = _hw_codes(codes)
        for c in hw:
            if c not in seen:
                seen.add(c); order.append((el, c))
        flag = ""
        if relapse_t is None and (hw or not alive):
            relapse_t = el
            relapse_fdiag = rx.fdiag()
            flag = "  <== RELAPSE"
        print(f"      t+{el:5.0f}s ERR?={codes or ['0']}  sweep={'LIVE' if alive else 'FROZEN'}{flag}")
        if relapse_t is not None:
            confirm_polls += 1
            if confirm_polls >= 3:        # a few polls past relapse to capture the full code set + order
                break

    # optional decisive reference-source split
    ext_result = None
    if args.external_ref and relapse_t is not None:
        print("\n--- EXTERNAL 10 MHz REFERENCE test (FREF EXT; needs a known-good 10 MHz on rear J9) ---")
        print(f"  FREF? before = {rx.q('FREF?')}")
        rx.w("FREF EXT"); time.sleep(3.0)
        after = rx.err()
        print(f"  FREF? after EXT = {rx.q('FREF?')}   ERR? on external ref = {after or ['0']}")
        cleared = REF_CODES - set(after)
        ext_result = (sorted(REF_CODES & set(after)), sorted(cleared))
        rx.w("FREF INT")
        print(f"  -> reference codes STILL present on ext ref: {ext_result[0] or 'none'} ; cleared: {ext_result[1] or 'none'}")
        print("     (cleared => internal A21 OCXO is primary; still present => downstream A15/A4/A3 or a rail)")

    # ---- summary ----
    print("\n================ DIAGNOSIS SUMMARY ================")
    print(f"COLD ERR?           : {cold_err or ['0']}  (sweep {'LIVE' if cold_alive else 'FROZEN'})")
    if relapse_t is None:
        print(f"RELAPSE             : NONE within {args.duration:.0f}s -- unit stayed healthy (warm-up may have fixed it)")
    else:
        print(f"RELAPSE at          : t+{relapse_t:.0f}s ({relapse_t/60:.1f} min)  "
              f"{'~5-min auto-cal cadence' if 240 <= relapse_t <= 360 or (relapse_t % 300) < 40 else ''}")
        print("CODE APPEARANCE ORDER (elapsed s -> code):")
        for el, c in order:
            print(f"   t+{el:5.0f}s  {c}")
        ref_first = order and order[0][1] in REF_CODES
        print(f"   -> {'REFERENCE code appeared FIRST => reference-chain PRIMARY, YTO secondary (confirms corrected diagnosis)' if ref_first else 'a YTO/other code led -- re-examine (would favor YTO-primary)'}")
        print("COLD vs WARM FDIAG (which loop moved):")
        for osc in FDIAG_OSC:
            cv, wv = cold_fdiag.get(osc), (relapse_fdiag or {}).get(osc)
            d = f"{wv-cv:+.6g}" if (cv is not None and wv is not None) else "?"
            print(f"   {osc:7s} cold={('%.6g'%cv) if cv is not None else '?':>14} warm={('%.6g'%wv) if wv is not None else '?':>14}  delta={d}")
    if ext_result is not None:
        print(f"EXTERNAL-REF split  : reference codes still-present={ext_result[0] or 'none'} cleared={ext_result[1] or 'none'}")
    print("Full decode + service tree: rf-se/se299/audit/2026-07-03-gpib-low-level-audit.md")
    rx.close()
    return 0 if relapse_t is None else 1


if __name__ == "__main__":
    sys.exit(main())
