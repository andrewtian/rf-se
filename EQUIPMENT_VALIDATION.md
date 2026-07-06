# se299 Equipment Validation -- Signal Chain, Coupling Audit, Validation Sequences, Live/Emulated Strategy

Scope: prove we drive and couple the two instruments (Anritsu 68367C source, HP/Agilent 8565EC
analyzer) correctly, with every operation traced to the persisted manuals
(reference/operator-manuals/anritsu-68000-series-operation.md + agilent-8560e-users-guide.md), and
decide the live-vs-emulated testing strategy. Companion to DEVICE_OPERATION_AUDIT.md (per-command
correctness) and CANONICAL_SE_BASIS.md (the SE method).

---

## 1. Signal chain (two coupled planes)

```
  CONTROL / DIGITAL PLANE  (verified end-to-end)
  ---------------------------------------------------------------------------
   Mac host (se299)                                   Mac host (se299)
     drivers.NetworkTransport                           drivers.NetworkTransport
        | TCP :5556                                        | TCP :5555
        v                                                  v
   qemu VM  linux-gpib                                 qemu VM  linux-gpib
   NI GPIB-USB (pad 5)                                 NI GPIB-USB (pad 18)
        | IEEE-488 bus                                     | IEEE-488 bus
        v                                                  v
   Anritsu 68367C  <--- native GPIB (CF1/L1/RF1/OF1/OL1/OSB) --->  answers
   HP 8565EC       <--- 8560 lang  (IP/CF/RB/TS/MKPK/MKA?/PP/PSDAC) --->  answers

  RF PLANE  (the physical coupling under question)
  ---------------------------------------------------------------------------
   68367C RF OUT --coax--> LPDA (TX)  ) ) )   air / reflective chamber   ( ( (  horn (RX) --coax--> 8565EC RF IN
        (+25 dBm leveled, 1-40 GHz)         (path loss + antennas)            (preselected > 2.9 GHz)
```

Two DISTINCT couplings must both hold for a real SE number:
- DIGITAL coupling = the host can command each unit and read it back correctly.
- RF coupling = the transmitted tone physically reaches the RX antenna above the noise floor.

## 2. Coupling audit (the determination)

DIGITAL coupling: VERIFIED. Both units answer their identity queries through the golden two-VM
bridge; every command string is confirmed against the persisted manuals (DEVICE_OPERATION_AUDIT.md,
all deviations fixed); the source transmits a real CW tone (OF1/OL1 readback match the command, and
it levels to +25 dBm across 1-40 GHz -- an earlier OSB=0x04 "unleveled" was a transient read before
ALC settle, not reproduced); the analyzer returns real, frequency-varying traces from a clean IP
preset. The two live end-to-end tests (test_e2e_live.py) pass: both units answer and the canonical
reference pass runs source-tracked with well-formed acceptance rows.

RF coupling: NOT reliably measurable in the current environment. Extensive live tracing produced
non-reproducible reads (the floor wandered -100/-93/-48/-32/-26/-21 dBm between runs; ON sometimes
read below OFF; peaks landed off the source frequency). Root causes identified and now handled in
the driver/procedure: (a) above 2.9 GHz the YIG preselector must be PEAKED or a real tone reads low
or is missed -- this was entirely absent and is the single largest measurement-correctness fix
(peak_preselector/set_preselector_dac added); (b) a positive-peak detector latches any pulsed
interferer's peak and buries a continuous CW tone -- a sample-detector + averaging read
(measure_average) recovers it, and the pos-peak-minus-average gap is itself an interference detector;
(c) AT 0 dB overloads the mixer into broadband spurs -- a real signal is attenuation-independent
while a spur drops faster than the added attenuation (the real-vs-artifact test). One clean window
did show the CW tone at ~-48 dBm (+25 dBm drive, ~73 dB path loss) 45 dB above a -93 dBm floor, so
the RF path CAN couple; it is weak and easily buried, so a trustworthy SE number requires the
controlled validation sequence below in a quiet RF environment.

## 3. Per-device validation sequence (ordered; each step cites the manual + its test)

Run in order; a step gates the next. "test" = the hardware-free regression that guards the command
string; "live" = the on-hardware confirmation.

### Source -- Anritsu 68367C (cite anritsu-68000-series-operation.md)
- S-V1 identity: `*IDN?` -> ANRITSU,68367C (native answers *IDN? only). test: test_source_idn_queries_star_idn.
- S-V2 clean state: prepare() = `RST IL1 AT0 ATT00 TR0 LO0 LOG` (rules out all 6 "leveled but no RF" modes). test: test_source_prepare_sends_known_good_clearing_string.
- S-V3 CW output: `CF1 <f> GH` then `OF1` readback == commanded MHz (CF1 sets CW mode + value; NOT CW1). test: test_source_set_freq_uses_CF1_not_CW1; live: OF1 confirmed.
- S-V4 level: `L1 <p> DM` then `OL1` == commanded dBm. test: test_source_level_and_rf_mnemonics + test_source_native_readbacks.
- S-V5 leveled+locked: `OSB` bit2 (unleveled) & bit3 (lock error) clear -> settled_ok(). test: test_source_settled_ok / test_source_status_byte_parses_binary_osb.
- S-V6 max leveled power: sweep `L1` watching OSB bit2 -> ceiling (+25 dBm, 1-40 GHz). live.
- S-V7 output on/off: `RF1`/`RF0`. test: test_source_level_and_rf_mnemonics.
- S-V8 (optional) list/step sweep: `LST/ELN/ELI/LF/LEA/LIB/LIE/LDT..MS/MNT` + per-point `UP` (native; NOT LSP/SWP/*TRG). test: test_68369_list_sweep_command_strings.

### Analyzer -- HP/Agilent 8565EC (cite agilent-8560e-users-guide.md)
- A-V1 identity: `ID?` -> HP8565E (8560 lang, not *IDN?). test: test_analyzer_idn_queries_id.
- A-V2 clean state: prepare() = `IP` + settle + `SNGLS` + flush `TS` (a dirty state returns non-reproducible marker values). test: test_analyzer_prepare_presets_and_flushes.
- A-V3 amplitude cal (PHYSICAL, operator): connect the front-panel 300 MHz / -10 dBm CAL OUT to the RF input; `CF 300MHZ; SP 100KHZ; TS; MKPK HI; MKA?` must read -10 dBm within the amplitude budget; else `RLCAL`. This is the trust anchor for every amplitude read. cite: cal spec.
- A-V4 zero-span read: `SNGLS/SP 0HZ/RB/VB(>=RBW)/DET POS/TS;DONE?/MKPK HI/MKA?`. test: test_sweep zero-span/marker tests.
- A-V5 preselector peak > 2.9 GHz: peak_preselector -> `PP` returns a PSDAC (0-255); reuse via set_preselector_dac so reference and wall share one preselector state. test: test_preselector_peak_sequence_above_2p9ghz / test_preselector_peak_noop_below_2p9ghz; live: DAC 131 @ 6 GHz.
- A-V6 detector for the environment: DET POS for a quiet-band CW tone; measure_average (DET SMP + linear avg) when a pulsed interferer is present. test: test_measure_average_uses_sample_detector_and_linear_mean.
- A-V7 error/status clean: query_errors() == [] ; query_status(). test: test_analyzer_query_errors_filters_zero / test_analyzer_query_status_parses_stb.
- A-V8 continuous vs single: set_continuous(CONTS/SNGLS). test: test_analyzer_set_continuous_toggles_conts_sngls.

### Pair -- coupling + campaign
- P-V1 RF path: `checkpath` reversible-tone go/no-go (PATH-LIVE / NO-COUPLING). test: test_checkpath (5).
- P-V2 reference: `calibrate` graded reference pass, saved. test: test_calibration (6).
- P-V3 wall + SE: `wall --calibration` -> SE(f)=reference-wall, affirmative campaign_pass. test: test_calibration_feeds_measure_wall.

## 4. Live-vs-emulated strategy (the determination)

Use BOTH, with a clear division of labor:

- EMULATED (SimBench / Sim* drivers) is the basis for ALL logic, algorithm, sequence, and
  integrity validation: it is deterministic, gives complete coverage (226 hardware-free tests),
  has no bus contention, and runs in seconds. Correctness of the SE math, the EA8 gate, the
  affirmative campaign_pass, the calibrate/wall/checkpath flows, and the GUI is proven here.
- COMMAND-STRING adherence is validated hardware-free with FakeTransport (asserting the exact bytes
  the driver puts on the bus, traced to the manuals) AND confirmed on hardware by the golden two-VM
  e2e tests (both units accept the strings and answer).
- LIVE hardware is reserved for (a) that command-adherence confirmation and (b) the FINAL ACCEPTANCE
  measurement, which must run the per-device validation sequence above in a QUIET RF environment
  (all maskers/jammers off), starting from the A-V3 CAL-OUT amplitude check, with the preselector
  peaked above 2.9 GHz and checkpath confirming PATH-LIVE before any SE number is trusted.

Rationale: live RF measurement in this environment was non-reproducible, so it cannot be the basis
for software correctness -- only for final physical acceptance under controlled conditions. The
emulator proves the tool is right; the live golden two-VM proves the commands are accepted; a
controlled quiet-chamber run produces the certified number. Do not concurrently drive the GPIB bus
from more than one controller (it wedges the adapter), and never hand-poke the qemu VM (stray
gpib_config wedges the board until a fresh boot).
