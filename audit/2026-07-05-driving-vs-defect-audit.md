# Driving-vs-defect audit -- could WE be causing the 8565EC symptoms over GPIB?

Date: 2026-07-05. Scope: an adversarial, READ-ONLY challenge to the standing "hardware defect"
verdict for the HP/Agilent 8565EC analyzer. Question: are the two observed symptoms attributable to
US driving the analyzer incorrectly over GPIB, rather than a genuine hardware fault? No code changed;
no bus transactions or VM restarts were issued for this audit. ASCII only.

Two symptoms under challenge:
  (a) REFERENCE-PLL WEDGE under stress -- 333/335/337/499 latch, sweep freezes, not GPIB-recoverable.
  (b) HIGH-BAND FREQUENCY ERROR -- ~330 ppm / +3.3 MHz at 10 GHz (intermittent).

Sources reviewed: drivers.py class Agilent856xEC (prepare/configure/measure_peak/measure_floor/
measure_tracked_peak/peak_preselector/set_attenuation/set_frequency/read_trace/_read_and_calibrate/
arm_and_wait); loop.py (acquire_reference/measure_wall/chain_sweep read pattern); sa_gui.py +
point_op_mode.py + range_mode.py (GUI read patterns); coordinator.py (health gate);
instrument_hub.py + drivers.lease_exclusive (concurrency); config.py (settle defaults);
reference/operator-manuals/hp-8560-e-series-programming.md (08560-90146) and
agilent-8560e-users-guide.md (08560-90158); audit/2026-07-03-gpib-low-level-audit.md (DEFECT
section) and audit/2026-07-04-issue-register-and-trace-plan.md; tools/diagnose_8565ec.py;
tests/test_defect_8565ec_reference_live.py (task #16 = the destructive stress-confirmation test).

## Summary verdict

The "hardware defect" verdict SURVIVES the challenge. Two dispositive facts cannot be produced by any
command we send:

1. The reference-unlock codes 333/335/337/499 are class-200-799 HARDWARE codes (agilent-8560e-users-
   guide.md, spec "ERR? error-code classes"; loop.classify_8560_error, loop.py:669-686). They are set
   by the instrument's own lock-detect hardware. There is NO 8560 remote mnemonic that creates a PLL
   unlock code; our production path never even sends a cal/reference command that could try (no
   ADJIF/ADJALL/CAL ALL/FREF anywhere in the driver -- grep-confirmed; only the diagnose tool and the
   gated defect test touch FREF).
2. The SOURCE is isolated as healthy: the 68367C reads OF1 back EXACT and OSB leveled+locked at
   2/6/10 GHz (test_defect_8565ec_reference_live.py:69-88; drivers.Anritsu68369.read_state
   drivers.py:791-801). So the +3.3 MHz tone offset is the ANALYZER's frequency axis, not the emitted
   frequency. No GPIB command changes the analyzer's 10 MHz reference frequency (FREF INT is the
   default and we never send FREF), so a 330 ppm reference offset cannot be self-inflicted.

Separating the two claims the task asks for:
  - "DRIVING MODULATES THE WEDGE" (symptom a): TRUE. The wedge is LOAD-triggered (audit 2026-07-03,
    "KEY REVISION"); our retune rate, RF-toggle rate, and single-vs-multi consumer choice all move the
    trigger probability. Driving makes the wedge MORE or LESS likely. It does not CAUSE it -- a healthy
    analyzer does not unlock its reference PLLs under mere retuning, and gentle driving kept the unit
    healthy for 30 minutes.
  - "DRIVING CAUSES THE FREQUENCY ERROR" (symptom b): FALSE. The ~330 ppm offset is the internal
    reference/harmonic-mixing error, measured AFTER the driver's correct wide-span preselector recipe
    already removed the undersampling/spur-lock artifacts that driving CAN add. Driving cannot create
    the reference offset; it can only add or remove a config artifact ON TOP of it.

## Per-hypothesis table

Rating is per symptom: (a) = wedge, (b) = frequency error. C = CONTRIBUTES, N = DOES-NOT-CONTRIBUTE,
U = UNKNOWN. "Contributes" for (a) means "modulates/worsens the load-triggered wedge," never "is the
root cause."

| # | Hypothesis (driving as SOFTWARE cause) | Driver cite | Manual cite | (a) | (b) | Reasoning |
|---|---|---|---|---|---|---|
| 1 | Retune faster than the analyzer settles | measure_peak CF write drivers.py:1059 + settle drivers.py:1064-1065; config settle_s=0.2 config.py:38 | CF 7-53 (REQ-HP8560-010); settle/warm-up UG spec "IP preset"/"Amplitude calibrator" | C | N | Each CF write is a first-LO relock; on a marginal reference every relock is a chance to unlock, so RATE matters. But the driver dwells settle_s (0.2 s) after each CF BEFORE the first sweep, so per-point cadence is ~1 retune/sec, far below the destructive burst. Cannot create a reference offset. |
| 2 | Rapid-retune stress (bursts) | loop.acquire_reference per-point loop 125-196; chain_sweep 952-968 | CF 7-53; TS 7-218 | C | N | The load-trigger. Production retunes ONE point at a time with settle + multi-sweep between; it is the gentle envelope the 30-min run validated, not the 40x no-settle 1->36 GHz burst of task #16. Worsens (a) only if driven hard; irrelevant to (b). |
| 3 | CONTS vs SNGLS usage | prepare CONTS drivers.py:930; configure CONTS 935; arm_and_wait CONTS 1295; measure_peak stabilize loop 1082-1091 | CONTS 7-60, SNGLS 7-181, TS 7-218, DONE? 7-72 (REQ-HP8560-001..005) | N | N | CONTS was adopted because SNGLS+TS+DONE? returns a STALE trace over the bridge (audit "RX RECOVERED"). In ZERO span (SP 0HZ, configure ends at drivers.py:960) the first LO is PARKED, so CONTS free-run re-acquires time-domain data withOUT retuning the LO. It is a GENTLE pattern, not a stressor. Correct per manual. |
| 4 | Stabilize loop hammers TS/MKPK/MKA? | measure_peak 1082-1091 (_READ_STABLE_TRIES=4 drivers.py:1000); measure_tracked_peak 1139-1147 | TS 7-218; MKPK 7-129; MKA 7-108 | N | N | The loop takes up to ~5 sweeps per point, but in ZERO span these are IF re-acquisitions with the LO parked -- no relock per extra sweep. Only the ONE CF write per point relocks. Tries were already cut 10->4 (drivers.py:996-1000) to reduce relock stress; further marker reads add none. Not a wedge driver. |
| 5 | Concurrent / un-leased multi-consumer access | lease_exclusive drivers.py:1507-1530; SingleConsumerConflict 1492-1504; InstrumentHub one-owner instrument_hub.py:74-151; diagnose single-consumer diagnose_8565ec.py:57-58 | n/a (bridge arbitration) | C(mitigated) | N | Concurrent probing is a DOCUMENTED worsener (drivers.py:1492-1497). The architecture actively PREVENTS it: one exclusive device lease per instrument, standalone tools REFUSE to add a second consumer, and within a process transactions serialize on _txn_lock (drivers.py:192, 238-250). So this is a driving MITIGATION, not an active contributor -- provided the lease discipline is honored. |
| 6 | Auto-cal (ADJIF/ADJALL/ADJCRT) interaction | none sent (grep-confirmed); IP self-align prepare() drivers.py:921-923 | ADJIF "continuously self-adjusts, normally ON" UG spec "Amplitude calibrator"; IP UG spec "IP preset state" | N | N | We NEVER command ADJIF/ADJALL/ADJCRT/CAL in production. The ~5-min IF auto-cal is the INSTRUMENT's internal cadence, not GPIB-driven; and it was REFUTED as the trigger (gentle 30-min run stayed healthy across several cal cycles, audit "KEY REVISION"). The one self-align we force is IP, once per campaign, cold (prepare()). Not a stressor. |
| 7 | Missing settle/wait between commands | measure_peak settle 1064-1065; arm_and_wait dwell 1289-1298; peak_preselector TS-after-PP 1200; _SWEEP_LIVE_DWELL_S/_ARM_DWELL_S 1013/1021 | TS blocks 7-218; "second TS after a hardware change" UG spec "Preselector peaking"/"Valid zero-span read sequence" | N | N | The OPPOSITE of the hypothesis: the driver adds REAL dwells everywhere DONE? cannot block across the bridge (arm_and_wait dwell >= sweep time; a 2nd TS after PP). Settling is over-provided, not missing. Good hygiene reduces (a); no bearing on (b). |
| 8 | Preselector-peak (PP/PSTATE/PSDAC) misuse | peak_preselector drivers.py:1164-1204 (PP 1197, TS-after 1200, real-tone guard 1188-1194); reuse set_preselector_dac 1206-1210 | PP p.560, PSDAC p.563, MKCF p.7-114 UG specs; "dominant correctness hazard above 2.9 GHz" | C(high band) | N | PP usage is CORRECT: marker on signal in a nonzero span RBW>100 Hz, PP, then a 2nd TS; a real-tone guard prevents mis-tuning the YIG onto noise; PSTATE is NOT conflated (manual: PSTATE/MKPX unrelated). BUT PP + the 200 MHz search span DO sweep the LO/YIG above 2.9 GHz -- inherent high-band LO exercise, so more relock opportunity per high-band point. Modulates (a) in high band; does not create the reference offset (b). |
| 9 | Command that triggers a cal / PLL re-lock that can fail under load | CF relock per point 1059; PP YIG tune 1197; IP self-align 921-923 | CF 7-53; PP p.560; IP UG spec | C | N | The only PLL re-locks we force are (i) per-point first-LO retune and (ii) high-band YIG peak. On a marginal reference each is a fail opportunity, so under HEAVY cadence they raise wedge odds. None of them writes the reference frequency, so none can produce the ppm error. |
| 10 | TDF / SNGLS / DONE? handshake correctness | read_trace 1300-1314; _read_and_calibrate SNGLS+restore 1353-1401; TDF P/B 1325/1339 | TDF 7-209 (REQ-051); TRA 7-208; DONE? 7-72; 601 pts 7-208 | N | N | Handshake is correct and bridge-race-aware: TDF specified before every read, 601-point guard, SNGLS used only to FREEZE for the binary cal then restored to CONTS (1400), stability confirmed by two agreeing binary reads rather than trusting DONE?. No malformed sequence that could wedge or mis-tune. |
| 11 | Documented duty-cycle / timing constraint exceeded (MEAS UNCAL) | measure_peak zero-span dwell 1064-1082; measurement_uncalibrated=False 1475-1480 | zero-span min ST 50 us but RBW-filter settle ~1.2/RBW; MEAS UNCAL UG specs "Sweep time vs RBW"/"MEAS UNCAL" | N | N | At very narrow RBW (ladder 10 Hz, config.py:50) a too-short zero-span dwell can raise MEAS UNCAL and read a tone LOW -- an AMPLITUDE risk, not a wedge or a frequency error. Cumulative settle + 4-sweep stabilize likely covers ~1/RBW; the 8560 has no reliable UNCAL bus bit (STB? has no UNCAL bit, REQ-HP8560-073) so the driver conservatively reports False. Orthogonal to both symptoms. |
| 12 | STB? serial-poll query poisons the socket (aggravates "hammering") | query_status STB? drivers.py:1472-1473 (NOT called in any production read path -- grep-confirmed) | STB 7-201 (REQ-HP8560-073) | U | N | Latent only: query_status() is defined but never invoked by the loop/GUI read paths, so it does not currently touch the bus. If a future caller used it, a poisoned socket could look like a wedge (absent link), but that is a transport artifact, not the reference wedge, and never a frequency error. Flag to keep it out of hot paths. |

## What our driving DOES affect

- WEDGE PROBABILITY (symptom a) is a driving-tunable knob because the fault is load-triggered
  (audit 2026-07-03 "KEY REVISION": gentle 30 min = healthy; heavy load = relapse). The levers we
  own: per-point retune rate (measure_peak settle_s + stabilize loop already pace it), high-band
  PP/YIG exercise (drivers.py:1164-1204), RF-toggle cadence on the source (loop.py:136/143/161), and
  the single-consumer discipline (drivers.lease_exclusive). Every one of these is already tuned toward
  gentle in the current code (single consumer, zero-span parked LO, tries cut to 4, debounced GUI
  retunes at point_op_mode.py:382-415 with POINT_SETTLE_S=0.6, coalesced apply). Range mode step-
  sweeps only the SOURCE while the analyzer sits at a fixed center in max-hold (range_mode.py:161-175),
  so it does not rapidly retune the analyzer LO at all.
- APPARENT high-band frequency reliability: driving CAN add a frequency-READ artifact on top of the
  reference error. An undersampled span (RBW too narrow for the span) makes MKPK HI lock a between-bin
  spur -- LIVE-observed as a -63 dBm spur +15.9 MHz off at a 50 MHz span / RB 1 kHz (peak_preselector
  docstring, drivers.py:1169-1173). This IS a driving/config artifact, and the driver already fixes it
  with the wide-span matched-RBW recipe (200 MHz / 300 kHz) in measure_tracked_peak (drivers.py:1122-
  1127). So driving affects whether we ADD spur error, but the residual +3.3 MHz that remains with the
  correct recipe is the hardware reference error, not ours.

## What our driving does NOT explain

- The LATCHED reference codes 333/335/337/499. No 8560 mnemonic produces a PLL-unlock code; these are
  hardware lock-detect outputs (class 200-799 = needs service, agilent md "ERR? error-code classes").
  Our production path sends only standard measurement mnemonics -- no ADJIF/ADJALL/CAL/FREF -- so there
  is no command-injection route to these codes.
- Not-GPIB-recoverable + power-cycle-only recovery. A software-caused state would clear over the bus
  (re-preset, re-configure, reconnect); the audit tried IP/CAL/ADJALL/ADJIF-OFF/CLRW/reconnect and
  none un-froze it (audit 2026-07-03 "RX FROZEN-SWEEP"). Only a power-cycle re-inits the loops. That is
  hardware latching, not a driver state machine.
- The ~330 ppm / +3.3 MHz offset. It is FREQUENCY-PROPORTIONAL and grows with band (harmonic number N
  multiplying the reference error) -- the signature of a reference/OCXO error, not a command error (a
  wrong CF would be a fixed offset, not frequency-proportional). The source is proven exact + locked, so
  the axis error is the analyzer's internal reference. Nothing we write changes the OCXO frequency.
  (Aside: the driver comments at drivers.py:1114-1118 and 1172 say "+0.33 MHz / +33 ppm" whereas the
  DEFECT audit and the issue register say "+3.3 MHz / 330 ppm" (10.0033 GHz). That is a documentation
  discrepancy to reconcile; it does not change the causation finding -- either way it is the reference.)

## Recommended driving hardening (reduce wedge probability; none is a fix for the hardware)

1. WARM UP before any campaign: >= 5 min band-0, ~30 min for high-band accuracy (agilent md "IP preset
   state" / warm-up note). Operational, not a code change.
2. KEEP the single-consumer lease absolute: never run diagnose_8565ec.py or a second GUI/campaign
   against a leased analyzer (drivers.lease_exclusive already enforces refuse-on-conflict). This is the
   biggest documented worsener (drivers.py:1492-1497).
3. CAP the per-point retune cadence explicitly. settle_s (config.py:38) paces it today; a hard minimum
   inter-retune spacing (or coalescing adjacent points) would bound the worst case for a long unattended
   wall pass, complementing coordinator health_every (coordinator.py:318-321).
4. MINIMIZE redundant high-band retunes: the preselector flow writes CF several times (peak_preselector
   drivers.py:1177, then the caller re-writes CF, then measure_tracked_peak MKCF-recenters at 1157).
   Fewer CF writes per high-band point = fewer YIG/LO relocks on the marginal reference.
5. DO NOT re-issue IP / prepare() mid-campaign (it forces a self-align, drivers.py:921-923). prepare()
   is already once-at-start; keep it there.
6. KEEP query_status()/STB? out of hot read paths (drivers.py:1472-1473); prefer ERR? + the trace-change
   liveness check the code already uses (diagnose_8565ec.py rationale, lines 13-15).
7. Continue gating every measurement on analyzer_health (ref codes AND sweep-live, coordinator.py:151-
   176) so a wedge HALTS the run instead of streaming stale numbers -- already in place; retain it.

## Gate decision for task #16 (the destructive stress-confirmation test)

Task #16 = tests/test_defect_8565ec_reference_live.py::test_stress_induces_reference_lock_loss_wedge
(SE299_DEFECT_STRESS=1): 40 iterations of rapid wide retune (1->36 GHz, no settle) + RF toggle + TS,
asserting the reference-unlock codes appear (test lines 95-125). Issue register lists it as the
"Issue 2 destructive load-characterization," priority 5, run only if operating unserviced (audit
2026-07-04 lines 46-57).

DECISION: SAFE to run, and VALID -- but LOW-VALUE given the evidence already in hand.

- SAFE (non-damaging): the induced wedge is a LATCHED PLL-unlock recovered by a power-cycle, repeatedly
  demonstrated non-destructive across the debugging campaign. Input overdrive is precluded -- the test
  arms arm_direct_chain (source capped 0 dBm, RX atten floored 20 dB, test line 106), well under the
  8565EC +30 dBm / >=10 dB-atten input rating (agilent md "Input damage limits"). The only cost is one
  power-cycle. It self-guards: skips if already wedged (test line 105).
- VALID (not a self-inflicted software artifact): a positive result is confirmed by the class-200-799
  reference codes, which no command can synthesize -- so "stress -> 333/335/337/499 latch" is
  dispositive hardware evidence, not our driving misread as a fault. Our aggressive driving here is the
  INTENTIONAL over-stress that probes a marginal reference; it does not confound the conclusion.
- LOW-VALUE: the hardware verdict is already well-supported without spending a power-cycle -- by
  (i) source isolation (OF1 exact + OSB locked), (ii) latched class-200-799 reference codes, (iii) the
  load-triggered relapse with a clean gentle 30-min baseline, and (iv) power-cycle-only recovery. Run
  task #16 only if a DETERMINISTIC reproduction is required for a service/replace sign-off.
- PRECONDITIONS if run: warm unit; single consumer (no other lease held); arm_direct_chain in force
  (built in); an operator available to power-cycle afterward; run it LAST in a session. Do NOT run it
  before or during any measurement you care about -- it will wedge the analyzer by design.

CONCLUSION: our driving MODULATES the wedge (it is load-triggered and we set the load) but does NOT
CAUSE it, and it does NOT explain the ~330 ppm high-band frequency error at all. The "hardware defect"
verdict stands. Task #16 is safe and valid to run, and only worth the power-cycle if a deterministic
confirmation is needed beyond the already-conclusive evidence.
