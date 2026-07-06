# se299 live-hardware issue register + trace-acquisition plan (2026-07-04)

Context gathered per open issue from the live TX->RX bring-up, the traces captured to characterize
each, the traces still needed, and the determined APPROACH. Grounded in live GPIB traces on the real
8565EC (RX, :5555 pad18) + 68367C (TX, :5556 pad5) over the qemu bridge. ASCII only.

Bench state at capture: analyzer HEALTHY (ERR?=[111], sweep live) after a power-cycle; both units on
a direct 2.4mm cable; source capped 0 dBm / RX atten floored 20 dB (arm_direct_chain).

---

## ISSUE 1 -- 8565EC reference/timebase wedge (HARDWARE, load-triggered)  [central]

CONTEXT (audited, 2 workflows + manual decode 08560-90158 pp.690-704 + live tests):
- Codes 333(600 UNLK)/335(SMP UNLK)/337(FN UNLK)/499(CAL UNLK) = shared reference/timebase + cal-osc
  chain unlock (10 MHz ref / A21 OCXO / A15 RF / A4). YTO codes 317/319/351/353 SECONDARY. 313 is
  E-series-inapplicable -> A2 firmware/model-ID flag. 500-series = collateral to 499.
- LOAD-triggered, NOT thermal: 30 min gentle = ERR?=[111] healthy; heavy load (rapid retune / RF
  toggle / concurrent probing) relapses. Frozen trace = halted acquisition (ignores CF/RBW/RF).
- NOT GPIB-recoverable (IP/CAL/ADJALL/ADJIF-OFF); power-cycle only. Max-hold confound ruled out.

TRACES CAPTURED: cold ERR?=[111]+sweep LIVE; warm/wedged 14-code set; warm FDIAG (LO 3.785G, SMP
206.67M, HARM 14, MROLL 29.900M != RAWOSC/(2*POSTSC)=29.827M -> frac-N off-lock); cold FDIAG relation
HOLDS (MROLL == RAWOSC/(2*POSTSC) exactly); ADJIF-OFF does NOT un-freeze; RBW->DANL 0 dB shift; shape
byte-identical vs CF; 30-min gentle no-relapse log.

TRACES STILL NEEDED (decisive localizers -- need physical access):
- [DECISIVE] EXTERNAL 10 MHz reference on rear J9 + FREF EXT, relapse, log ERR?: if 333/335/337/499
  clear -> internal A21 OCXO primary; persist -> downstream A15/A4/A3 or a rail.  tool: diagnose_8565ec.py --external-ref
- Cold->relapse ERR? entry-ORDER + time (reference codes first? ~5-min? -- but note relapse is load-
  not-time triggered, so drive a KNOWN load while logging).  tool: diagnose_8565ec.py
- Warm power-supply RAIL voltages (a sagging rail unlocks multiple PLLs) + thermal freeze-spray/tap on
  A15/A21/A4 -- bench, non-GPIB.

APPROACH: (a) OPERATE GENTLY for now -- single consumer, stepwise retune, per-point settle, minimal RF
toggling (the toolchain already does this; it produced a clean band-0 SE figure). (b) For a robust
unattended full-band campaign, SERVICE the reference/synth section; run the external-ref trace first to
tell the tech A21-OCXO vs downstream. Service tree in 2026-07-03-gpib-low-level-audit.md.

## ISSUE 2 -- load-trigger characterization (sub-issue of 1, but distinct approach)

CONTEXT: the relapse trigger is operational stress, not warm-up. WHICH pattern (rapid retune rate? RF
toggle count? concurrent leases? command rate?) is unquantified -- knowing it sets the safe operating
envelope AND is a second line of evidence for a marginal reference (rapid re-lock stress).

TRACES STILL NEEDED (DESTRUCTIVE -- each run likely wedges the unit -> power-cycle):
- Controlled incremental stress from a fresh cold start, ERR?-logged, ONE variable at a time:
  (a) fixed freq, rapid RF on/off at increasing rate -> does toggle-rate alone trigger it?
  (b) RF on, rapid CF retune sweep at increasing rate/span -> does retune-rate trigger it? (prime suspect:
      rapid LO re-lock)
  (c) two concurrent leases/consumers hammering -> does contention trigger it?
  Record the threshold (rate / count / time-to-relapse) for each.

APPROACH: run ONE destructive stress trace per power-cycle (accept the wedge), starting with (b) rapid-
retune (the prime suspect). Outcome sets the documented safe envelope (max retune rate, max toggle rate)
OR, if any gentle pattern also trips it, escalates the service case. Lower priority than the external-ref
localization; do only if we need to operate the unit unserviced.

## ISSUE 3 -- high-band SE >2.9 GHz does not work with the band-0 read path  [blocks full-band SE]

CONTEXT (band-0 SE figure 10 MHz->2.5 GHz is DONE, 1.33->4.33 dB; above 2.9 GHz breaks):
TRACES CAPTURED (this session):
- 3.0 GHz: WORKS with preselector peak (PSDAC=132) -> SE 4.17 dB, ERR clean.
- 5.0 GHz: SOURCE UNLEVELED (OSB=0x04) at 0 dBm cmd; tone ~15 MHz off commanded.
- 10.0 GHz: tone IS receivable (-7.67 dBm, ~7.7 dB cable loss) but ONLY with preselector peak + a WIDE
  span search; it sits at 10.0033 GHz (+3.3 MHz). A plain zero-span-at-exact-CF read MISSES it.
- freq-accuracy sweep: above ~3 GHz a plain 400 MHz-span MKPK locks a FIXED spur at 3.00025 GHz @ -90
  (floor) regardless of commanded freq; band-0 offsets large+erratic in a wide span. The tone-frequency
  error GROWS with band = harmonic mixing (N) multiplying the marginal-reference LO error -> ties Issue 3
  to Issue 1 (the reference fault corrupts high-band frequency accuracy).

TRACES STILL NEEDED:
- With a SHARED 10 MHz reference (both units on one timebase, rear J9) -- does the offset collapse to
  ~0? (isolates reference-mismatch from the analyzer reference fault).
- Per-band SOURCE leveled-power envelope: sweep L1 down until OSB bit2(0x04) clears at 5/10/20/40 GHz;
  record the max leveled dBm per band.
- measure_peak WITH preselector-peak + MKF re-center in the loop (not the current bare zero-span):
  PP -> MKPK HI -> MKF? -> re-CF to MKF -> zero-span read; verify SE at 3/6/10/18/26/40 GHz.

APPROACH: build a HIGH-BAND read path in drivers/loop: (1) share the 10 MHz reference between source and
analyzer (kills the offset); (2) preselector-peak per point above 2.9 GHz (PP), already have
peak_preselector(); (3) after PP, MKPK HI + MKF? to FIND the tone, re-center CF on it, then zero-span
read (defeats the residual offset + the fixed-spur trap); (4) command per-band leveled source power +
assert OSB leveled. NOTE: trustworthy high-band frequency accuracy also REQUIRES Issue 1 resolved (the
reference error is harmonic-multiplied). Gate a high-band SE claim on both.

## ISSUE 4 -- source idn() uses *IDN? (can poison the socket)  [robustness]

CONTEXT + TRACE CAPTURED: OI (native) -> "6867 0.0140.00-120.0 3.02.35070326C3" (model/range/pwr/fw/
serial); *IDN? -> "ANRITSU,68367C,070326,2.35" -- BOTH answer on this unit (fw 2.35). OI is the
guaranteed-native identity; *IDN? is 488.2 (times out + poisons on a unit/fw that lacks it).
APPROACH: switch Anritsu68369.idn() to native OI (parse model/serial/fw from it); keep the existing
NetworkTransport.reconnect() fallback. Low risk, no hardware needed. Add a parse + a unit test.

## ISSUE 5 -- source RF dead-man de-key only fires for LEASED pads  [safety]

CONTEXT: bridge safe-state (pad5 -> RF0) de-keys only leased pads, but keying needs no lease (open bus
allows W) -> a client that writes RF1 without leasing leaves the source hot on crash.
TRACE NEEDED: use the RX analyzer as the detector (we can) -- write RF1 un-leased, crash the client,
confirm the bridge de-keys (tone drops on the analyzer). We already proved RF0 drops the tone (~57 dB),
so the analyzer is a valid dead-man witness -- no power meter required.
APPROACH: de-key pads WRITTEN-to (not only leased), or require a lease to write a safe-state pad; verify
with the analyzer-as-witness trace above. Safety-critical -- verify live before trusting.

## ISSUE 6 -- analyzer query_errors baseline-delta  [self-check usability]

CONTEXT + TRACE: healthy baseline ERR?=[111] (benign); wedged = the 14 hardware codes. The loop A-V7
self-check treats any ERR? as new -> perpetually fails on the chronic baseline.
APPROACH: snapshot the ERR? baseline at startup (expect just 111 when healthy), flag only DELTAS -- a
NEW 200-799 code is the real signal. No hardware needed.

## ISSUE 7 -- single-unit-safe E2E tests  [coverage, task #14]

CONTEXT: tests/test_e2e_live.py + test_e2e_coupling_live.py gate on BOTH units. Need analyzer-only
(measure floor / self-noise, no source) and source-only (set freq/level/OSB/readback + RF0 safe-state,
no analyzer) live tests that honest-skip when the targeted unit is absent.
APPROACH: two new gated test groups, conservative power, RF off on exit, honest-skip. No new traces.

---

## Trace-acquisition priority (what to run next, and gating)
1. Issue 1 external-10MHz-ref split (needs a 10 MHz source on J9) -- DECISIVE for the whole hardware call.
2. Issue 3 shared-reference + per-band-level + PP/MKF-recenter traces -- unblocks full-band SE (gated on 1).
3. Issue 4/6 code fixes (OI idn, ERR delta) -- no hardware, do anytime.
4. Issue 5 dead-man de-key with analyzer-witness -- safety, gentle.
5. Issue 2 destructive load-characterization -- only if operating unserviced; costs a power-cycle each.
6. Issue 7 single-unit tests -- code/test only.
