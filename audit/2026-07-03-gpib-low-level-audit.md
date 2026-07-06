# Low-level GPIB operation audit -- 8565EC analyzer + 68369A source + bridge

Date: 2026-07-03. Scope: verify that the low-level GPIB operation to EACH unit is correct --
command mnemonics, units, termination, status/read handling, addressing, timeouts, and the
safety-critical RF dead-man. ASCII only.

## Method

- Three independent static+manual trace agents, each against the in-repo programming manuals:
  - Analyzer 8565EC command set vs `reference/operator-manuals/hp-8560-e-series-programming.md`
    -> `audit/2026-07-03-gpib-audit-analyzer-8565ec.md`
  - Source 68369A command set vs `reference/operator-manuals/anritsu-68000-series-operation.md`
    -> `audit/2026-07-03-gpib-audit-source-68369a.md`
  - Bridge / linux-gpib I/O layer vs linux-gpib semantics
    -> `audit/2026-07-03-gpib-audit-bridge-linuxgpib.md`
- LIVE ground-truth trace of the 8565EC through the real bridge (the analyzer is powered + cabled).
  The 68369A/68367C source is currently ABSENT from the GPIB bus, so it was audited statically only
  -- its live verification is BLOCKED until it answers at pad 5.

## Why the source is absent (root-caused 2026-07-03, all SOFTWARE causes ruled out)

The source is genuinely off/uncabled -- NOT any of the software faults that have masqueraded as
"absent" before (this exact symptom was a linux-gpib driver bug on 2026-07-01). Ruled out:
- WRONG ADDRESS -- a full 1-30 primary-address device-clear scan of the HS bus found NOTHING at any
  address (not just pad 5 empty; the whole bus is empty).
- IBGTS / HS-firmware DRIVER BUG -- the Gersoft-lab modern ni_usb_gpib driver (the fix that made the
  68367C answer IDN on 2026-07-02) is CONFIRMED loaded in the guest: the "se299 kernel-API compat:
  Gersoft modern" shim marker is in the installed ni_usb_gpib.c, and the runtime emits the Gersoft
  "pipe resync" line. So the addressing engine is not being wiped.
- USB PASSTHROUGH -- the NI GPIB-USB-HS attached cleanly (`ni_usb_gpib: ... attached to gpib0`),
  /dev/gpib0 exists; moving the adapter to a different USB hub did not break passthrough.
- READ-PATH CORRUPTION -- a clean single write to pad 5 returns ENOL "no listeners" (ibsta=0x8000)
  with a COMPLETELY EMPTY kernel log (no `buffer[6]` read-corruption, no `retval=-5`/NIUSB_NO_BUS).
  A healthy driver correctly reporting "nobody answered," not a driver fault.
CONCLUSION: the 68367C is powered off, its IEEE-488 cable is unseated at one end, or its GPIB
interface is disabled/not-addressable at address 5. Only a physical action resolves it. The VM,
bridge, driver, and passthrough are all healthy; the source service crash-loops BY DESIGN (it
refuses to start without a real device) and will come up within ~5 s once the 68367C answers.

## Verdict per unit

- ANALYZER (8565EC): command strings are CORRECT for the 8560 E-series -- no wrong mnemonic, no
  wrong unit/scaling, no leaked SCPI (correct native `ID?`/`ERR?`/`DONE?`, `SAVES`/`RCLS`,
  `MKBW -3,?`), and `read_trace` uses `TDF P` so the returned ASCII is already dBm. LIVE-VALIDATED:
  `ID?`, `query_options`, `FA?/FB?/DONE?`, markers, and the high-risk `TDF P`/`TRA?` 601-point parse
  all return correct data. Two latent driver defects fixed (below). INSTRUMENT HEALTH: this unit
  reports 13 persistent hardware/calibration error codes (see below).
- SOURCE (68369A): command strings are CORRECT (native 68xxx: `CF1..GH`, `L1..DM`, `RF1`/`RF0`,
  `LST`/list-sweep). The safety-critical RF-off dead-man (`RF0`, client + bridge safe-state) is
  CORRECT. One HIGH status-read defect fixed (below). NEEDS-LIVE items pending power-on.
- BRIDGE / linux-gpib: ibtmo table, device-scoped ibclr/ibonl recover, `_BUS_LOCK` write+read
  atomicity, board index, and write-EOI are all CORRECT. One HIGH fault-misclassification defect
  fixed (below); several MEDIUM hardening items remain (below).

## Fixes applied this pass (all with regression tests; board 517 passed / 5 skipped)

1. HIGH -- source status-byte corruption (`drivers.py`). `Anritsu68369.status_byte()` read the raw
   binary OSB via `NetworkTransport.query()`, whose `.decode("ascii","replace").strip()` zeroes any
   status byte that is ascii whitespace -- including 0x0C (RF-unleveled + lock-error, the worst
   case) -> read as 0 -> `settled_ok()` FALSELY reports leveled+locked, defeating the interlock
   exactly when both faults are present. Fix: added `query_raw()` (bytes, no decode/strip) to the
   transports; `status_byte()` uses it. Client-side only (the wire protocol base64-frames payloads,
   so the byte arrives intact). Tests: `test_source_status_byte_survives_ascii_strip_corruption`.
2. HIGH -- bridge fault misclassification (`ni_gpib_server.py`). This guest's linux-gpib build has
   NO `gpib.iberr()`, so `_status` reports iberr=0 for every fault; since iberr==0 == EDVR, the
   classifier collapsed EVERY fault to ADAPTER_WEDGED -- a merely powered-off instrument told the
   operator to power-cycle the NI adapter. Fix: when iberr is 0, `_classify_fault` reads the verdict
   from ibsta/ibcnt (errno-in-ibcnt -> ADAPTER_WEDGED; TIMO -> DEVICE_SILENT; ERR-without-TIMO ->
   BUS_WEDGED). LIVE-VALIDATED: an absent pad now classifies BUS_WEDGED (ibsta=0x8000), not
   ADAPTER_WEDGED. Tests: `test_classify_fault_without_iberr_uses_ibsta_ibcnt`.
   (Companion to the earlier `_status` per-field fix that restored ibsta/ibcnt.)
3. MEDIUM -- analyzer `set_detector()` skipped `normalize_detector()`, so a human label ("sample")
   was written verbatim ("DET sample") and SILENTLY IGNORED by the 8565EC (stale detector -> wrong
   number). Fix: normalize like `configure()`. LIVE-VALIDATED: `set_detector("sample")` -> DET?=SMP.
   Test: `test_set_detector_normalizes_human_labels`.
4. MEDIUM -- analyzer `configure()` never asserted amplitude units, so a leaked V/W `AUNITS` state
   from a prior session gives silent wrong dBm reads. Fix: `configure()` emits `AUNITS DBM`.
   LIVE-VALIDATED (`AUNITS DBM` accepted). Test: `test_configure_pins_amplitude_units_to_dbm`.

## Remaining recommendations (not yet applied -- prioritized)

- MEDIUM/SAFETY (bridge M1): the RF dead-man de-key fires only for LEASED pads, but keying needs no
  lease (open bus allows W). A client that writes `RF1` without leasing leaves the source hot on
  crash. Recommend: de-key pads WRITTEN-to, or require a lease to write a safe-state pad. Change to
  safety-critical teardown -- verify live with a power meter before trusting (NEEDS-LIVE, source off).
- MEDIUM (source): `idn()` uses `*IDN?`; native `OI` is the guaranteed identity. `*IDN?` is only
  "usually" implemented on native 683xx firmware and, if unanswered, times out and POISONS the
  socket -- and it is used as a liveness probe (control_plane). Recommend `OI`, or a reconnect
  fallback. NEEDS-LIVE (confirm the bench unit answers `*IDN?` vs only `OI`).
- MEDIUM (analyzer F12): `query_errors()` "read clears it" is only half-true -- persistent hardware
  codes re-enter, so it always returns the 13-code baseline and the `loop.py` A-V7 self-check
  perpetually fails / cannot spot a NEW code. Recommend: snapshot the baseline at startup, flag only
  deltas.
- MEDIUM (bridge M3/M4): `Z recover`/`ping` act on the caller's bound pad without lease arbitration
  (an observer could SDC/ibonl a device another session controls); and a safe-state de-key write
  failure at teardown is logged but never retried/escalated.
- LOW: source list-sweep `LEA`-before-`LIB/LIE` ordering and a possible `RSS`/first-`UP` off-by-one;
  analyzer `measure_average(sweeps>1)` sends `VAVG N` but takes one `TS` (no real N-sweep average;
  production uses sweeps=1). Both latent.

## DEFECT: nature, impact, canonical resolution (2026-07-04, consolidated)

NATURE. The defect is in the 8565E ANALYZER's frequency-reference / first-LO phase-lock chain: the
STANDARD PRECISION 10 MHz reference (A21 OCXO -- confirmed present, our unit has NO Option 103 which
would delete it) and its downstream reference-derived PLLs (600 MHz reference on A15, sampling osc,
fractional-N, cal oscillator on A4). It is a single marginal-lock fault seen at two severities:
  (a) FREQUENCY ERROR (mild, always present, high-band only): the reference sits slightly off, so the
      phase-locked first LO is off; harmonic mixing multiplies that error by the harmonic number N, so
      it is negligible at band 0 (N=1) but reaches ~330 ppm / +3.3 MHz at 10 GHz. A precision OCXO is
      spec'd sub-ppm, so 330 ppm is unambiguously a hardware defect, not a spec limit.
  (b) LOCK-LOSS WEDGE (severe, intermittent, under stress): under operational stress (rapid retune,
      the periodic ~5-min auto-cal, heavy/concurrent GPIB load) the marginal reference-derived PLLs
      unlock outright (333 600-UNLK / 335 SMP-UNLK / 337 FN-UNLK / 499 CAL-UNLK), the first LO can no
      longer lock, acquisition HALTS (frozen trace), and the codes LATCH until a power-cycle re-inits
      the loops. Not GPIB-recoverable. It is thermal/stress-marginal -- field-typical causes: aging
      high-ESR electrolytics on the reference/synth board, a sagging (often negative) supply rail, an
      OCXO oven/crystal aging, or a thermally-intermittent joint. (313 additionally hints at an A2
      controller/firmware model-ID quirk -- a secondary dimension.) The SOURCE (68367C) is healthy:
      exact OF1 readback + OSB leveled+locked; the fault is entirely the analyzer.

WHAT IT PREVENTS.
  - WORKS (unaffected): band-0 SE (DC-2.9 GHz) -- a valid, repeatable direct-cable SE figure (1.3->4.3
    dB); the whole networked two-client toolchain; band-0 amplitude/frequency reads.
  - PREVENTED: (1) trustworthy SE ABOVE 2.9 GHz -- the harmonic-multiplied frequency error puts the
    tone MHz off commanded, so preselector-peak + zero-span reads land on the wrong frequency / LO
    spurs; frequency accuracy is untrustworthy and the amplitude read is unreliable. This blocks the
    2.9-40 GHz span of the DC-40 GHz SE goal (~93% of the band). (2) SUSTAINED/UNATTENDED operation --
    the intermittent wedge can freeze any long sweep mid-run (needs a power-cycle), so even band-0 must
    be driven gently and cannot be trusted unattended for a full campaign. (3) WIDE-DYNAMIC-RANGE
    amplitude fidelity -- the auto-IF-cal codes mean log-linearity/step-gain across a 40-80 dB ref-wall
    span is not guaranteed even when "working" (a constant offset cancels in SE=ref-wall, log-linearity
    errors do not).

CANONICAL RESOLUTION (two tiers).
  TIER 1 -- LOCALIZE (cheap, decisive; the deferred external-10-MHz-reference test): feed a known-good
    10 MHz into the rear EXT REF (J9), FREF EXT, re-run tools/diagnose_8565ec.py --external-ref.
    Offset collapses + reference codes clear => the internal A21 OCXO is the culprit; codes persist =>
    downstream (A15 600 MHz gen / A4 cal osc / A3 interface ADC / a supply rail).
  TIER 2 -- CANONICAL REPAIR (bench service per the 8564E/8565E Service Guide 08560-90157 / 08563-90214,
    333+499 tree): (1) verify 10 MHz ref at A4J7 (~0 dBm); (2) measure supply rails under warm load
    (a sagging negative rail unlocks several PLLs at once); (3) recap the reference/synth assembly --
    replace high-ESR electrolytics (measure ESR, not just C), reflow the thermally-intermittent joint
    (freeze-spray/heat-gun to localize); (4) replace/recalibrate the A21 OCXO or timebase board if the
    reference itself is bad; (5) run the full Automatic-IF + YTO/LO adjustment via the 8565E TAM (Test
    & Adjustment Module) to re-align; (6) resolve the A2 firmware/model-ID (313) if it persists.
  WORKAROUND (no teardown; the pragmatic canonical fix for the FREQUENCY error): DISCIPLINE both
    instruments to ONE shared 10 MHz HOUSE reference. If Tier-1 shows the synth accepts an external
    ref, a lab 10 MHz standard feeding both the 8565E and the 68367C EXT-REF ins fixes the analyzer's
    frequency accuracy AND removes the source<->analyzer offset -- unblocking high-band SE without
    opening the box. (It does not necessarily cure the intermittent WEDGE, which may still need the
    recap/service.) This is exactly the deferred "10 MHz source" work.
  REPLACE: given a ~25-year-old unit with a marginal precision reference, a calibrated replacement
    8565E/EC (or modern analyzer) is a valid resolution if service cost approaches replacement.

## KEY REVISION (2026-07-04, post-diagnose-tool): the wedge is LOAD-triggered, NOT warm-up/thermal

A clean cold-start run of tools/diagnose_8565ec.py under GENTLE load (one consumer, 20 s polls, no
RF toggling, no rapid retuning) kept the 8565EC HEALTHY for the FULL 30 minutes: ERR? = [111]
(benign only), sweep LIVE at every poll t+0 -> t+1810 s. NO relapse. This REFUTES the "periodic
5-minute auto-cal fails on a warming marginal reference" mechanism proposed below -- if that were the
trigger it would have relapsed near t+300 s regardless of load. It did not.

Then, in the same healthy+warm window, the full SE-figure sweep (tools/live_se_figure_sweep.py, 10
points 10 MHz->2.5 GHz, per-point RF toggle + retune) COMPLETED CLEANLY -- a smooth monotonic
direct-cable insertion loss 1.33 dB @10 MHz -> 4.33 dB @2.5 GHz, ~63 dB coupling, OSB=0x00 throughout.

CONCLUSION: the relapse is triggered by OPERATIONAL STRESS (rapid LO retuning, RF toggling, and/or
aggressive/concurrent GPIB probing -- the heavy patterns used during the earlier debugging), NOT by
warm-up time. The instrument is USABLE for SE measurement when driven gently: single consumer,
stepwise (not rapid) retune, adequate per-point settle, minimal RF toggling. The hardware IS still
marginal (it DID relapse repeatedly under heavy load, and the reference/LO codes are real class-
200-799), so it remains a service candidate for a robust full-band campaign -- but it is not the
hard "relapses every 5 min no matter what" fault the thermal hypothesis implied. Operating guidance:
keep the se299 gentle-read path (CONTS + stabilize + CLRW, single-consumer, per-point settle) and
avoid rapid retune bursts / concurrent probing; if a robust unattended DC-40 GHz campaign is needed,
service the reference/synth section first. The reference-chain decode + service tree below still
stands as the localization if/when it is serviced.

## AUDITED ROOT-CAUSE of the 8565EC wedge (2026-07-04; 2 workflows + live discriminating tests)

An adversarial 5-lens workflow + a manual/service-guide decode agent + live GPIB discriminating tests
CORRECTED and refined the earlier "marginal YTO/LO" framing. Verdict on that earlier claim:
PARTIALLY-CORRECT -- the PROGNOSIS (real class-200-799 hardware fault, needs bench service, not
GPIB-recoverable) is confirmed, but the MECHANISM and the named assembly were wrong.

ROOT CAUSE (corrected): a thermally/warm-up-triggered fault in the SHARED FREQUENCY-REFERENCE /
TIMEBASE + CAL-OSCILLATOR chain, NOT the YTO. The always-present codes 333 (600 MHz ref PLL unlk),
335 (sampler unlk), 337 (fractional-N unlk), 499 (cal-oscillator unlk) are all PLLs phase-referenced
to the internal 10 MHz timebase. The service guide's OWN cross-reference for 333 co-present with 499
says suspect the 10 MHz reference / A21 OCXO / Opt-103 TCXO / A15 RF assembly / A4 log-amp+cal-osc
board. The YTO codes (317/319/351/353) are INTERMITTENT and SECONDARY -- the YTO loop thrashes its
tuning DACs downstream of a marginal reference comb. The 500-series (561/562/564/565/591 LOG AMPL)
are COLLATERAL to 499 (the auto-IF cal has no locked cal oscillator to align to). ANOMALIES the
earlier framing ignored: 313 (FREQ ACC) is a roller-PLL code that does NOT apply to 8560 E-series HW
-> on an 8565E it flags a firmware model-number-ID / A2-controller (digital) issue; 361 (SPAC CAL)
implicates the A14 sweep generator.

RELAPSE MECHANISM (corrected): NOT "LO drifts past a capture threshold." The 8560 runs automatic IF
alignment at power-up AND EVERY ~5 MINUTES (+ span-accuracy cal + LO/IF realign). Power-on self-align
passes cold; as the unit warms, the 5-min auto-cal re-runs, the now-marginal reference/cal-oscillator
fails to lock, and the codes LATCH until the next power-on self-align. This exactly fits "clean cold,
relapses after minutes, instant power-cycle recovery, no GPIB cal helps."

THE FROZEN TRACE (corrected + confounds ruled out by live tests):
- MAX-HOLD software confound RULED OUT: range_mode.py:164 forces MXMH TRA, which mimics a frozen
  trace -- but with NO se299 process running and an explicit clean MNMH TRA + CLRW TRA + TM FREE +
  CONTS, the trace STILL froze (0/601 change) and RF toggle gave 0 dB delta. Real hardware, not
  trace-mode. (Still: force CLRW in the sustained loops to remove the confound permanently.)
- The held trace is a FULLY FROZEN BUFFER that responds to NOTHING: a 100x RBW change (1 MHz->10 kHz)
  shifts the source-off DANL 0.0 dB (a live trace-write path must move it ~20 dB), and the 601-point
  shape is BYTE-FOR-BYTE IDENTICAL at CF 1.5 GHz vs 2.5 GHz. So it is neither "LO parked" nor "LO
  swept then held" -- ACQUISITION IS HALTED: trace memory holds a stale pattern while the reference/
  LO chain is unlocked and no valid measurement completes. (The earlier "2.5 dB spatial structure
  proves the LO swept" was wrong -- that is just frozen buffer content.)
- The "TS completes in ~35 ms" evidence is a BRIDGE DONE?-race artifact (TS latency did not scale
  with ST: 50 ms->1074 ms, 1 s->25 ms), so it does NOT prove the sweep engine runs. Discard it.

STILL OPEN (need power-cycle / physical access -- the decisive localizers):
- EXTERNAL 10 MHz REFERENCE substitution (rear J9, FREF EXT): if 333/335/337/499 clear on a known-good
  external ref, the internal A21 OCXO is primary; if they persist, the fault is DOWNSTREAM (A15 600 MHz
  gen / A4 cal osc / the loops / A3 interface ADC). THE decisive split; not yet run.
- COLD-vs-WARM FDIAG (LO/SMP/HARM/MROLL/RAWOSC/POSTSC) + ERR? entry-ORDER logged from cold through the
  relapse (reference codes appearing FIRST + a ~5-min onset confirms reference-primary + auto-cal
  trigger). WARM baseline captured 2026-07-04: LO 3.785 GHz, SMP 206.67 MHz, HARM 14, MROLL 29.900 MHz,
  RAWOSC 298.27 MHz, POSTSC 5 (note MROLL != RAWOSC/(2*POSTSC)=29.827, a ~2.4 ppm frac-N error).
- CO-EQUAL hardware candidates NOT yet excluded: A3 interface ADC marginality (reads every loop's
  error voltage; a drifting threshold falsely declares all loops unlocked) and a POWER-SUPPLY RAIL
  sagging under thermal load (unlocks several PLLs at once). Measure rails warm; thermal-localize
  (fan / freeze-spray) the A15/A21/A4 boards + a tap/flex test for a thermal-intermittent joint.
- BENCH SERVICE: follow the 333+499 tree -- 10 MHz ref at A4J7 (~0 dBm), A21 OCXO / A15 / A4 / A3 ADC;
  full Automatic-IF + YTO adjustment; A2 firmware/model-ID check for the anomalous 313. Guides:
  08560-90157, 08563-90214. (Field-common causes of thermal REF/YTO unlock: high-ESR electrolytics,
  negative-rail drift, OCXO/mounting.)

## RX RECOVERED by power-cycle + real driver bug found & fixed (2026-07-04 FINAL)

After the operator POWER-CYCLED the 8565EC, the sweep-hold and ALL hardware-class errors CLEARED:
ERR? = 0 (was 13 codes), sweep re-acquires live (595/601 points change per sweep), and the RX
GENUINELY RECEIVES the source tone -- proven repeatedly: RF on/off gives a ~57 dB delta, the peak
tracks the source FREQUENCY across a fixed span (1.90->2.10 GHz), and tracks POWER 1:1 (source
-25/-15/-5 dBm -> RX -29/-19/-9 dBm), with a steady ~4 dB direct-2.4mm-cable loss (the earlier
"66 dB path loss" was an artifact of the frozen state). So the RX works and the TX->RX path is real.

REAL DRIVER BUG FOUND + FIXED (drivers.py): the canonical SNGLS single-sweep read (SNGLS; TS; DONE?;
MKPK; MKA?) returns a STALE trace over the qemu-passthrough GPIB bridge -- the DONE? sync does not
block for the new sweep, so every marker read reports the PRIOR input state (a source RF toggle read
0 dB delta: tone == floor). LIVE-PROVEN discriminator: identical sequence, same settle -- SNGLS gives
0 dB deltas, CONTS gives 57-60 dB deltas, 4/4. FIX: prepare()/configure()/arm_and_wait() now use
CONTS (continuous free-run); measure_peak takes TWO sweeps (flush the one-behind trace + a fresh one).
Board stays green (522 passed). This is the true root cause of "RX not reading the tone" and it is now
corrected in code. (The 8560 manual's SNGLS sequence assumes direct GPIB, not a networked bridge.)

RESIDUAL -- INTERMITTENT ANALYZER WEDGE (hardware, not code): the 8565EC still INTERMITTENTLY freezes
the trace at a stuck value mid-session. The SAME code (e.g. the zero-span CONTS RF-toggle test) gives
clean 57-60 dB deltas on one run and a wedged constant on the next -- identical command sequence, so
the variable is the instrument, not software. This matches the unit's LO/YTO history: the power-cycle
cleared the LATCHED errors but the underlying synth is marginal and intermittently loses the sweep.
RECOMMENDATION: give the 8565EC a full WARM-UP (manual: >=5 min band-0, ~30 min for warranted
accuracy / high band) before the SE campaign, and if the intermittent wedge persists warm, the LO/YTO
assembly needs bench service. All software (driver read path, bridge, networked two-client topology)
is confirmed correct and independent of this residual hardware intermittency.

## RX FROZEN-SWEEP -- 8565EC sweep/LO fault (2026-07-04 REFINED; user confirmed frozen SCREEN; SUPERSEDED by the RECOVERED section above)

CORRECTION to the "measurement path dead" section below: the RX is NOT producing zero data. With the
IP-preset REMOVED from the read path, a fresh full setup captures ONE real sweep with genuine
structure (a noise floor of STDEV ~1.4-4.6 dB and a real peak), proving the receive chain works --
consistent with the operator SEEING a signal on the screen. The true fault is that the analyzer
WILL NOT RE-SWEEP: after that one captured sweep it FREEZES. Evidence, all with settings CONFIRMED
CORRECT AGAINST THE MANUAL (TM? = FREE free-run, ST? = 50 ms, FREF? = INT, CLRW TRA, VAVG OFF,
GATE OFF):
- Repeated TS or free-run CONTS: 0/601 trace points change over 0.7 s (a live sweep changes many).
- The marker peak never tracks the source frequency (source stepped 1.98->2.02 GHz, peak stays put)
  and never tracks source POWER (source -30->+10 dBm, peak constant) -- true even with the RX link
  DISCONNECTED between reads, so it is not the GPIB hold freezing it.
- The absolute level of the one captured sweep VARIES WILDLY run-to-run (-113, -55, -54, -4.5 dBm,
  same source-off/known input) -- the IF/log-amp gain cal is unstable (matches the 500-series codes).
- The OPERATOR CONFIRMS the front-panel SCREEN is frozen (not just the GPIB trace).
- No GPIB command restores sweeping (FREF INT, TM FREE, CONTS, CLRW, VAVG OFF, GATE OFF, ADJALL/CAL).
- Persistent hardware-class ERR? throughout: 300-series 313/317/319/333/337/351/353/361 (LO/YTO
  synthesis + lock/settle) + 500-series 561/562/564/565/591 (Automatic-IF cal). Manual taxonomy:
  200-799 = HARDWARE, needs service; 100-199 (111/112/120) are benign parser codes.
VERDICT: an instrument-level 8565EC fault -- the sweep/LO is not running reliably (captures one
untrustworthy sweep per retune, then holds), corroborated by the LO/YTO + IF-cal hardware errors and
the unstable absolute level. Even the one captured sweep is NOT amplitude-trustworthy. NOT fixable
over GPIB. RECOVERY (physical, in order): (1) power-cycle the 8565EC + >=5 min (ideally 30 min)
warm-up -> re-runs power-on LO/IF self-alignment, may clear a latched LO-unlock and the sweep hold;
(2) front-panel PRESET (green key); (3) front-panel FULL CAL with CAL OUT (300 MHz, -10 dBm) jumpered
to RF INPUT. If the 300-series LO/YTO codes persist WARM after (1), the first-LO/YTO or IF assembly
needs service. Everything on the software / GPIB / bridge / networked two-client side is confirmed
correct and independent of this fault.

## RX MEASUREMENT PATH DEAD -- 8565EC analog chain fault (2026-07-04, NOT software; SUPERSEDED by the REFINED section above)

Live direct-cable loopback (68367C source -> 2.4mm cable -> 8565EC input) with the source VERIFIED
emitting (independent readback OF1=2000 MHz, OL1=0.00 dBm, OSB=0 leveled+locked): the analyzer does
NOT see the tone, and does not even show a real noise floor. The raw 601-point trace (`TDF P` +
`TRA?`, bypassing the marker) reads EXACTLY the bottom graticule (RL - 100 dB) with STDEV = 0.00 dB
at every reference level tested (RL +10 -> -90, RL -30 -> -130, RL -50 -> -150), every span (20 MHz,
100 MHz, 500 MHz, 4 GHz) and both bands (500 MHz, 2 GHz). A live sweep of a real analyzer ALWAYS has
thermal-noise structure (STDEV > 0); a perfectly flat trace = the IF/detector chain produces no data
= no downconversion reaching the detector. RULED OUT (so it is not a software/config artifact):
- ref-level clipping (floor never comes on-screen even at RL -50, bottom -150 dBm)
- the marker read (raw TRA? array is flat too, not just MKA?)
- external frequency reference with no ref (FREF? = INT already; forcing FREF INT no change)
- sweep not running (CONTS + 2 s settle + re-read: still flat)
- self-alignment (ADJALL/CAL ALL/CALALL/ADJIF ON/CAL FREQ attempts: no change)
- the source (independently verified emitting via its own native readbacks)
Persistent errors throughout: 300-series (LO/YTO synthesis + frequency) + 500-series (Automatic-IF
cal) + 111/112/499/591. VERDICT: an instrument-level fault in the 8565EC's LO/IF analog chain, NOT
fixable over GPIB.

CONFIRMED by an elite 5-lens expert ensemble + adversarial synthesis (2026-07-04) AND two decisive
follow-up tests that closed the ensemble's dissent caveats:
- Consensus: WE ARE OPERATING THE RX CORRECTLY WITH THE TX. Our command sequence matches the 8560
  E-series manual verbatim; both failing tones (0.5, 2 GHz) are band 0 (unpreselected) so the one
  band-dependent RX gotcha (missing preselector PP/PSDAC) is ruled out; the bridge read path is
  faithful (0.90). Classification hardware-fault (0.82). The 300/500-series codes are class 200-799
  = instrument-needs-service and no GPIB mnemonic can produce or clear them.
- Decisive test A (terminated-input self-noise floor, TX-INDEPENDENT; source OFF, AT 0, RL -60/-80,
  RB 1 MHz/100 kHz, nonzero 100 MHz span, TH OFF + ROFFSET 0 + CLRW forced, DET SMP and POS): the
  trace is STILL dead-flat at exactly RL-100 (RL-60 -> -160, RL-80 -> -180), STDEV = 0.00. A live IF
  chain ALWAYS shows a structured DANL floor; its total absence = the receive chain produces no data.
  This clears the "held-VIEW-trace / threshold / offset" confound (all cleared) and does not depend
  on the source or the cal oscillator.
- Decisive test B (fixed-tone pickup, side by side): source 2 GHz, 0 dBm, RF1, OSB=0x00; RX spanned
  peak search reads -90.0 dBm; source RF0 reads -90.0 dBm; DELTA = 0.0 dB (RX sees nothing either way).
Remaining recoverable sub-case NOT yet tried (physical): a latched LO/alignment unlock that a
power-cycle + >=5-30 min warm-up (power-on self-align) + a genuine front-panel FULL CAL (CAL OUT
300 MHz jumpered to INPUT) could clear. Note the 499 CAL-UNLK confound: a NEGATIVE CAL-OUT result
alone cannot separate a dead receive chain from a dead cal oscillator -- test A (no cal dependency)
is the cleaner discriminator and it is dead. PHYSICAL recovery required, in order: (1) power-cycle the 8565EC + >=5 min warm-up
(re-runs power-on alignment; may clear a latched LO-unlock); (2) front-panel self-CAL with CAL OUT
(300 MHz, -10 dBm) jumpered to the RF INPUT -> CAL ALL (frequency + amplitude); (3) if the 300-series
LO/YTO errors persist after (1)+(2), the first-LO/YTO or IF assembly needs service. Everything on the
software / GPIB / networked / two-client side is confirmed correct and independent of this fault.

## Instrument health -- 8565EC (NOT a software issue)

Live `ERR?` returns 13 persistent codes `[361,313,333,561,562,499,591,351,353,317,319,565,337]`
that re-enter on every read. Decoded (8560E service guides; NOT yet in `reference/` -- gate before
citing in project docs): 300-399 = LO/YTO synthesis + loop-settling, several with a tuning DAC at
limit; 400-599 = Automatic-IF (log-amp + IF step-gain) self-adjust. A constant absolute offset
cancels in SE = ref - wall, but log-linearity / step-gain errors do NOT cancel across a 40-80 dB
ref-wall span, and the LO codes threaten lock at some tunes. RECOMMEND: run the 8565EC Automatic IF
Adjustment + YTO adjustment / calibration before trusting wide-dynamic-range SE numbers.

## NEEDS-LIVE-VERIFICATION (blocked until the 68369A is powered on at pad 5)

- OSB fix (#1) confirmed on real source status bytes + a power-meter de-key check for the RF0
  dead-man and the M1 lease-less-keying gap.
- Whether the bench 68369A answers `*IDN?` or only native `OI` (source idn recommendation).
- Anritsu GPIB terminator asserts EOI (bridge read termination, bridge M2).
