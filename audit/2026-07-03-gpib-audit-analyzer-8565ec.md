# GPIB Command-Correctness Audit -- HP/Agilent 8565EC Analyzer Driver

Scope: `class Agilent856xEC(SpectrumAnalyzer)` and base `SpectrumAnalyzer` in
`/Users/atian/p/office-setup-explorations/rf-se/se299/drivers.py`.
Method: static code trace of every `self.t.write(...)` / `self.t.query(...)` string
vs the 8560 E-series native command language. No live hardware touched.

Authoritative references (all in-repo):
- `reference/operator-manuals/hp-8560-e-series-programming.md` (HP 08560-90146; Ch.5-7 command dictionary) -- cited as REQ-HP8560-NNN.
- `reference/operator-manuals/agilent-8560e-users-guide.md` (Agilent 08560-90158; PP/PSDAC, MKCF, IP preset, input limits, MEAS UNCAL, ERR classes).
- `reference/product-datasheets/agilent-8565ec-spectrum-analyzer.md` (8565EC specifics; live IDN "HP8565E,001,006,007,008").

## Headline verdict

The low-level command STRINGS are correct. No CRITICAL/HIGH command-mnemonic,
unit-scaling, terminator, or query-form defect was found. Every mnemonic is a valid
8560 E-series native command (no SCPI `*IDN?`/`*OPC?`/`*OPT?` leaked in), the tricky
`MKBW -N,?` idiom is right, `SAVES`/`RCLS` (not `SAV`/`RCL`) is right, and the highest-risk
path -- trace readback -- correctly uses `TDF P` so the returned ASCII is already dBm and
needs no reference-level/log-scale conversion (the classic silent-wrong-number trap is
AVOIDED by design).

Driver-CODE findings are MEDIUM/LOW: missing defensive normalization, un-asserted AUNITS, a
misleading `sweeps` parameter, and a real-HW UNCAL blind spot. Several are latent
(current callers happen to pass safe values).

Separately, the live bridge trace (folded in below) surfaced a HIGH-severity INSTRUMENT-CONDITION
finding (F11): the real unit carries 13 persistent, non-clearing `ERR?` codes = LO-lock (YTO)
and IF auto-cal (LOG AMPL / step-gain) self-adjustments that have NOT converged. These are not
a driver-code bug, but they can inject a non-cancelling amplitude error into a wide-dynamic-range
SE (ref - wall) measurement, so the unit needs adjustment/calibration before its absolute and
wide-span readings are trusted. The live trace also VALIDATED the highest-risk paths
(`TDF P`/`TRA?` 601-point parse, `ID?`, markers), downgrading F5 (trace delimiter) to INFO.

---

## (1) Per-method command table

Legend: cmd = exact string(s) emitted; verdict OK unless noted.

| Method (line) | Exact command(s) sent | Manual-correct form | Verdict | Citation |
|---|---|---|---|---|
| `idn` (748) | `ID?` | `ID?` (identity+options; NO `*IDN?`) | OK | REQ-HP8560-070; live IDN datasheet |
| `prepare` (750-761) | `IP` ; sleep 1s ; `SNGLS` ; `TS` ; `DONE?` | IP preset, SNGLS, TS;DONE? barrier | OK | REQ-HP8560-001/003/005; UG IP p.495 |
| `configure` (763-776) | `SNGLS` ; `RB AUTO`\|`RB {n}HZ` ; `VB AUTO`\|`VB {n}HZ` ; `RL {x}DBM` ; `DET {POS/NEG/NRM/SMP}` ; `SP 0HZ` ; (`AT {n}DB`) | same | OK (see F2: no `AUNITS DBM`) | REQ-HP8560-030/031/020/040/011/023 |
| `measure_peak` (777-785) | `CF {n}HZ` ; `TS` ; `DONE?` ; `MKPK HI` ; `MKA?` | same | OK (assumes AUNITS=DBM, F2) | REQ-HP8560-010/003/060/061 |
| `measure_floor` (788-798) | `DET SMP` ; (measure_peak) ; `DET {prev}` | same; SMP correct token | OK | REQ-HP8560-040 |
| `peak_preselector` (804-824) | `CF{n}HZ` ; `SP{n}HZ` ; `RB{>=1e3}HZ` ; `TS;DONE?` ; `MKPK HI` ; `MKCF` ; `TS;DONE?` ; `PP;DONE?` ; `TS;DONE?` ; `PSDAC?` | same | OK (see F6: span-edge guard) | UG PP p.560, PSDAC p.563, MKCF p.7-114 |
| `set_preselector_dac` (826-830) | `PSDAC {int}` ; `TS;DONE?` | PSDAC 0-255 set + settle sweep | OK | UG PSDAC p.563 |
| `measure_average` (833-850) | `CF{n}HZ` ; `DET SMP` ; (`VAVG {N}` if N>1) ; `TS;DONE?` ; `TDF P` ; `TRA?` | tokens valid; but VAVG+1xTS semantics wrong | OK strings / see F3 | REQ-HP8560-041/051/050 |
| `sweep_trace` (852-859) | delegates set_frequency+arm_and_wait+read_trace | n/a | OK | -- |
| `set_frequency` (865-881) | `FA{n}HZ`/`FB{n}HZ` OR `CF{n}HZ`/`SP{n}HZ` | same; both-modes rejected | OK | REQ-HP8560-010/011/012/013 |
| `set_sweep_time` (882-887) | `ST AUTO` OR `ST {x}SC` | `ST <n>{S\|SC\|SEC\|MS\|US}` -- SC=seconds accepted | OK | REQ-HP8560-004 |
| `set_continuous` (888-889) | `CONTS` / `SNGLS` | same | OK | REQ-HP8560-001/002 |
| `arm_and_wait` (891-896) | `SNGLS` ; `TS` ; `DONE?` | TS;DONE? (no `*OPC?`) | OK (F7 note) | REQ-HP8560-003/005/006 |
| `read_trace` (898-910) | `TDF P` ; `TRA?`/`TRB?` ; `FA?` ; `FB?` | P=dBm ASCII; axis from FA/FB; 601 pts | OK (F2 units, F5 delimiter) | REQ-HP8560-050/051/052/012/013 |
| `set_attenuation` (912-924) | `AT AUTO` OR `AT {n}DB` (floored) | 0-60 dB decade on 8565E; AT AUTO | OK (F9 granularity) | REQ-HP8560-023 |
| `set_amplitude_units` (926-929) | `AUNITS {tok}` ; (`LG {n}DB`) | AUNITS DBM/DBMV/DBUV/V/W/AUTO/MAN; LG 1/2/5/10 | OK (F9) | REQ-HP8560-024/021 |
| `set_detector` (931-932) | `DET {mode}` (raw, NOT normalized) | DET POS/NEG/NRM/SMP only | DISCREPANCY | REQ-HP8560-040 (F1) |
| `set_video_average` (934-938) | `VAVG OFF` OR `VAVG {N}` | VAVG 1..999 / OFF | OK | REQ-HP8560-041 |
| `set_max_hold` (940-942) | `MXMH TRA`/`TRB` OR `CLRW TRA`/`TRB` | trace arg required | OK | REQ-HP8560-042/044 |
| `marker_peak` (944-948) | `MKPK HI` ; `MKA?` ; `MKF?` | same; MKF? default Hz | OK | REQ-HP8560-060/061/062 |
| `marker_bandwidth` (950-956) | from_trace default; native: `MKPK HI` ; `MKBW -{n},?` | `MKBW <neg-int>,?` -> Hz; NO standalone `MKBW?` | OK (correct idiom) | REQ-HP8560-066 |
| `query_options` (958-963) | `ID?` -> parts[1:] | ID? = identity+options (no `*OPT?`) | OK | REQ-HP8560-070 |
| `query_errors` (965-976) | `ERR?` -> nonzero ints | ERR? comma-sep 3-digit; 0=none; read clears | OK (classification is caller's job, F10) | REQ-HP8560-072; UG ERR classes |
| `query_status` (978-979) | `STB?` -> int | STB? decimal bit-sum (serial-poll equiv) | OK (no bit misinterpretation) | REQ-HP8560-073 |
| `measurement_uncalibrated` (981-986) | (no bus op) returns False | no reliable UNCAL bus bit | OK strings / see F4 | UG MEAS UNCAL; REQ-HP8560-073 |
| `save_state` (988-989) | `SAVES {n}` | SAVES (NOT SAV) | OK | REQ-HP8560-071 |
| `recall_state` (991-992) | `RCLS {n}` | RCLS (NOT RCL) | OK | REQ-HP8560-071 |
| base `marker_bandwidth`/`bandwidth_from_trace` (472-528) | pure math, no bus | n/a | OK | -- |

---

## (2) Ranked findings

No CRITICAL or HIGH findings. Command language is correct for the 8560 E-series.

### F1 -- MEDIUM -- `set_detector()` bypasses `normalize_detector()` (silent-ignore hazard)
- Location: `drivers.py:931-932`.
- Defect: `set_detector(mode)` sends `f"DET {mode}"` with the caller's raw string.
  `configure()` (line 770) routes its detector through `normalize_detector()`, but the
  canonical control-surface `set_detector()` does not. The 8565EC SILENTLY IGNORES an
  invalid `DET` argument (no fault, stale detector) -- the exact hazard the module's own
  comment at lines 709-712 documents. There is NO `PEAK`/`SAMPLE` spelling; only
  `POS/NEG/NRM/SMP` are accepted (REQ-HP8560-040). A GUI human-label ("sample", "peak")
  or an 8566-style token passed to `set_detector()` leaves the wrong detector engaged and
  produces a wrong amplitude with no error.
- Correct behavior: normalize before emitting, identical to `configure()`.
- Live impact today: LATENT. Current callers (`loop.py:441` "POS", `loop.py:486` "SMP",
  tests "POS"/"SMP") all pass valid mnemonics, so no active wrong number now. The gap is
  robustness/consistency for any future GUI or campaign call.
- Suggested fix: `self.t.write(f"DET {normalize_detector(mode)}")`.
- NEEDS-LIVE-VERIFICATION: no.

### F2 -- MEDIUM -- dBm reads never assert `AUNITS DBM` (silent-wrong-number under a state leak)
- Location: `configure()` `drivers.py:763-776`; consumed by `measure_peak` (782 `MKA?`),
  `measure_average` (845 `TRA?`), `read_trace` (899-901 `TDF P; TRA?`).
- Defect: `MKA?` and `TDF P` trace values are returned "in the current AUNITS"
  (REQ-HP8560-024/061). The driver interprets every one as dBm but never issues
  `AUNITS DBM`. It relies entirely on `prepare()`->`IP` (which resets AUNITS to dBm) and
  on no intervening `set_amplitude_units()` to a non-DBM unit. `set_amplitude_units("DBUV"/"V"/"W")`
  followed by a `measure_peak`/`read_trace` would silently reinterpret dBuV/volts/watts as
  dBm -- a wrong number with no error. (Note: `swept_screen` in `loop.py:439/452` does guard
  DBM, but it SETS `sweep.aunits` at 439 before raising at 452, so a rejected non-DBM sweep
  still leaves AUNITS changed for the next method.)
- Correct behavior: pin the unit deterministically. Emit `AUNITS DBM` in `configure()` (and/or
  assert it in the read methods). This does NOT change the correct `TDF P` design -- it just
  removes the external-state dependency. (Positive note: because the driver uses `TDF P`, it
  correctly AVOIDS the measurement-unit->dBm conversion trap; F2 is the residual unit risk.)
- Live impact: LATENT (needs a non-DBM `set_amplitude_units` state leak). Ranked MEDIUM
  because it is precisely the "silent wrong numbers" class flagged in the audit brief and the
  fix is one line.
- Suggested fix: add `self.t.write("AUNITS DBM")` in `configure()`.
- NEEDS-LIVE-VERIFICATION: no.

### F3 -- MEDIUM -- `measure_average(sweeps=N>1)` does not actually average N sweeps; leaves VAVG ON
- Location: `drivers.py:841-843`.
- Defect: with `sweeps>1` it sends `VAVG {N}` then a SINGLE `TS`. In single-sweep (`SNGLS`)
  mode, `VAVG N` needs N triggered sweeps to build the running average (REQ-HP8560-041); one
  `TS` yields only the first sweep (average-of-1). So the `sweeps` argument does NOT drive N
  acquisitions -- the real masker suppression comes entirely from the linear-power average over
  the 601 zero-span TIME samples (lines 846-850), which is correct and independent of VAVG.
  Two consequences: (a) `sweeps` is misleading/ineffective as a multi-sweep control; (b) `VAVG N`
  is left ON with no `VAVG OFF`/detector restore, a cross-method STATE LEAK -- a later
  `measure_peak` (which sets neither detector nor VAVG) would then read with SMP+VAVG still
  engaged, biasing a CW tone read.
- Correct behavior: either loop `TS` N times to complete the video average, or drop `VAVG`
  entirely and rely on the (already-correct) trace-point linear mean; and restore
  `VAVG OFF` + the prior detector on exit.
- Live impact: LATENT for the leak in production (`loop.py:322` calls with `sweeps=1`, so the
  VAVG branch is never taken there; only `test_device_operation.py:250` uses `sweeps=8`).
- Suggested fix: remove the `VAVG` write (the linear mean is the intended mechanism), or add a
  restore; document `sweeps` accordingly.
- NEEDS-LIVE-VERIFICATION: no.

### F4 -- MEDIUM -- `measurement_uncalibrated()` is a no-op on real hardware (UNCAL blind spot)
- Location: `drivers.py:981-986`; consumer `loop.py:450` (`swept_screen` UNCAL guard).
- Defect: the method always returns False on real HW, so `swept_screen`'s
  `raise AcquisitionRejected("MEAS UNCAL ...")` can NEVER fire against the instrument -- a
  too-fast sweep that reads a real tone LOW is not rejected. This is a genuine measurement-
  integrity gap even though it stems from a real instrument limitation: the 8560 has no
  dedicated MEAS-UNCAL status bit; `STB? & 2` ("message occurred") is set by ANY display
  message, not uniquely UNCAL (REQ-HP8560-073; UG MEAS UNCAL). The driver's choice not to
  over-trust that bit is defensible, but the guard gives false assurance on hardware.
- Correct behavior: keep `ST AUTO` / RBW/VBW auto-coupled so UNCAL cannot arise (UG guidance),
  and/or detect the on-screen "MEAS UNCAL" via a documented status/annotation query if one
  exists. Do NOT rely on `STB? & 2` alone.
- Suggested fix: document the real-HW limitation at the `swept_screen` call site, and enforce
  auto-coupled sweep time when a forced `sweep_time_s` risks UNCAL.
- NEEDS-LIVE-VERIFICATION: YES -- confirm whether the bench 8565E firmware exposes any bus-
  readable UNCAL indication. Suggested command (single accessor, read-only):
  set a deliberately-too-fast zero-span `ST` at a narrow RBW, `TS;DONE?`, then read `STB?` and
  `ERR?` and compare against a valid-time sweep to see if any code/bit uniquely tracks UNCAL.

### F5 -- LOW -- `read_trace`/`measure_average` trace parsing assumes comma/semicolon delimiter
- Location: `drivers.py:846` and `901` -- `raw.replace(";", ",").split(",")`.
- Defect: `TDF P` output is documented "ASCII, comma/CRLF delimited" (REQ-HP8560-051). If the
  8565E returns values delimited by CRLF only (no commas), after `.strip()` the split yields a
  single token with embedded newlines and `float()` raises `ValueError` uncaught -> `read_trace`
  crashes (not a wrong number, a hard failure). The manual and HP convention indicate comma
  delimiting for P format, so this is likely fine in practice.
- Correct behavior: split on comma/semicolon/whitespace to be format-robust.
- Suggested fix: `re.split(r"[,;\s]+", raw)` filtering empties.
- NEEDS-LIVE-VERIFICATION: YES -- capture one raw `TDF P; TRA?` reply and confirm the actual
  inter-value delimiter (comma vs CRLF vs comma+CRLF).

### F6 -- LOW -- `peak_preselector` default span can straddle the 2.9 GHz preselector edge
- Location: `drivers.py:804-812`.
- Defect: guard rejects only `f_hz <= 2.9e9`. With the default `span_hz=50e6`, a tone just
  above 2.9 GHz (e.g. 2.91 GHz) gives a low span edge of 2.885 GHz, below 2.9 GHz. `PP`
  requires the WHOLE span above 2.9 GHz (UG PP p.560); PP then posts an error and `PSDAC?`
  can return garbage. The `try/except` at 821-824 catches it and returns None (graceful
  degrade), so no wrong number -- but preselector peaking silently doesn't happen near the
  crossover, so a high-band tone there may still read low.
- Correct behavior: require `f_hz - span_hz/2 > 2.9e9` (shrink span or raise CF-relative
  window near the edge) before running PP.
- NEEDS-LIVE-VERIFICATION: no.

### F7 -- LOW/INFO -- `TS` and `DONE?` sent as separate program messages (not the `TS;DONE?` string)
- Location: `prepare` (760-761), `measure_peak` (779-780), `arm_and_wait` (895-896),
  `peak_preselector` (814-820).
- Note: the manual's canonical sync idiom is the single string `TS;DONE?` (REQ-HP8560-005).
  The driver sends `TS` as one write then `DONE?` as a separate query. Functionally equivalent:
  `TS` is a sequential/blocking command (REQ-HP8560-003) so the instrument will not execute the
  subsequent `DONE?` until the sweep completes, and the query read blocks until the reply. This
  is a widely-used equivalent, not a defect. Flagged only for completeness.
- NEEDS-LIVE-VERIFICATION: no.

### F8 -- LOW -- `prepare()` does not device-clear; `IP` leaves I/O buffers intact
- Location: `drivers.py:750-761`.
- Note: the UG states `IP` does not clear the I/O buffers (use CLEAR). The driver relies on the
  transport/bridge for buffer hygiene; the previously-fixed END/ibcnt bridge misread was the
  real manifestation of this class. A GPIB device-clear (DCL/SDC) at the start of `prepare()`
  would harden against a desynced input buffer. Transport-level, out of strict command scope.
- NEEDS-LIVE-VERIFICATION: no.

### F9 -- LOW -- Non-decade `AT` and out-of-set `LG` rely on instrument rounding
- Location: `set_attenuation` (924 `AT {db:.0f}DB`), `set_amplitude_units` (929 `LG {n:.0f}DB`).
- Note: 8565E `AT` is 0-60 dB in 10 dB decade steps and `LG` accepts only 1/2/5/10 dB/div
  (REQ-HP8560-023/021). A caller passing `db=15` sends `AT 15DB` (instrument rounds to a decade)
  or `db=70` exceeds the 8565E 60 dB max; `LG 3DB` is out of set. These are caller-supplied
  values the instrument clamps/rounds -- not command-form errors. Optional: clamp/validate in
  the driver.
- NEEDS-LIVE-VERIFICATION: no.

### F10 -- LOW -- `cli.py` treats any nonzero `ERR?` code as an error to "resolve" (no benign-111 classification)
- Location: `cli.py:137-139` (consumer, not the driver method).
- Note: the driver's `query_errors()` correctly returns the raw nonzero queue. `loop.py:673`
  classifies by range via `_error_queue_status` (100-series parser = WARN, >=200 = FAIL),
  matching the UG guidance that the bench 8565E posts a benign `111` on every zero-span `TS`.
  `cli.py` does NOT classify and prints "resolve before a real run" for a benign 111 -- a false
  alarm, not a wrong number. Driver method verdict remains OK; fix belongs at the cli caller.
- NEEDS-LIVE-VERIFICATION: no.

---

## Positive confirmations (things that are RIGHT and are common failure points)
- `idn()` uses `ID?` (not `*IDN?`); `query_options()` parses the live `HP8565E,001,006,007,008`
  reply correctly (drops the model, returns `('001','006','007','008')`). REQ-HP8560-070.
- No IEEE-488.2 asterisk commands leaked into the analyzer path (`DONE?`/`ERR?`/`STB?`/`ID?`
  native forms used; the 8560 has no `*OPC?`/`*OPT?`/`*IDN?`). REQ-HP8560-006.
- `read_trace` uses `TDF P` -> values are already dBm; no ref-level/log-scale conversion needed.
  This sidesteps the #1 silent-wrong-number risk (would exist with `TDF M` measurement units).
- `marker_bandwidth(from_trace=False)` uses the exact `MKBW -3,?` idiom (negative int + appended
  `,?`), NOT a bogus `MKBW 3DB`/`MKBW?`. REQ-HP8560-066.
- `SAVES`/`RCLS` used (not the 8566/8568 `SAV`/`RCL`). REQ-HP8560-071.
- Axis reconstruction in `read_trace` reads `FA?`/`FB?` from the instrument (not cached args)
  and uses actual `len(levels)` rather than hardcoding 601. REQ-HP8560-052.

---

## LIVE GROUND-TRUTH (coordinator bridge trace) -- confirmations

The coordinator traced the real 8565EC through the bridge (single accessor) and confirmed the
driver's command strings against live hardware:
- `idn()` -> "HP8565E,001,006,007,008"; `query_options()` -> `('001','006','007','008')` (F-none, matches).
- Read-only queries all valid and correctly typed: `FA?`->0, `FB?`->5.0E10, `CF?`->2.5E10,
  `SP?`->5.0E10, `RL?`->0, `AT?`->10, `RB?`->1.0E6, `VB?`->1.0E6, `DET?`->NRM, `AUNITS?`->DBM,
  `DONE?`->1. (Note `AUNITS?`->DBM at trace time; F2 remains a latent state-leak risk, not an
  active error here.)
- `marker_peak()` (`MKPK HI; MKA?`->-53.50; `MKF?`->4.8E10) works.
- `read_trace('A')` (`TDF P; TRA?`) returned 601 real dBm points, axis reconstructed 0..50 GHz
  correctly -- the highest-risk TDF/TRA? parse path is VALIDATED LIVE (and F5's comma-delimiter
  assumption is thereby confirmed for this firmware -> F5 downgraded to INFO).
- An invalid `ZZBOGUS` write and all driver commands added NO new error codes.

## F11 -- HIGH (instrument condition, not a driver-code bug) -- 13 persistent ERR? codes = LO-lock + IF auto-cal not converged; threatens SE amplitude/frequency validity

- Source of finding: live `query_errors()` returns a PERSISTENT, non-clearing set of 13 codes:
  `[361, 313, 333, 561, 562, 499, 591, 351, 353, 317, 319, 565, 337]`.
- Manual basis (HP/Agilent 8560 E-series Chapter 6/9 "Error Messages"): the ranges are
  **300-399 = LO and RF Hardware/Firmware Failures** and **400-599 = Automatic IF Errors**
  (internal IF self-adjustment). Codes decoded/classified:

  | Code | Range/class | Decoded meaning | Amplitude-relevant? |
  |---|---|---|---|
  | 313 | 300-399 LO/RF HW | sampler+roller oscillator combo not found for the required YTO start freq (LO synthesis) | freq/lock; indirect ampl |
  | 317 | 300-399 LO/RF HW | YTO main-coil COARSE DAC at limit during lock (YTO ERR not nulled) | freq/lock; indirect ampl |
  | 319 | 300-399 LO/RF HW | YTO main-coil FINE DAC at limit (pair of 317; YTO ERR not nulled to 0 V) | freq/lock; indirect ampl |
  | 333 | 300-399 LO/RF HW | LO/RF synthesis hardware/firmware failure (frac-N/sampler region) | freq/lock; indirect ampl |
  | 337 | 300-399 LO/RF HW | LO/RF synthesis hardware/firmware failure | freq/lock; indirect ampl |
  | 351 | 300-399 LO/RF HW | YTO loop settling error -- loop error voltage won't stabilize during YTO lock | freq/lock; indirect ampl |
  | 353 | 300-399 LO/RF HW | YTO loop settling error (as 351) | freq/lock; indirect ampl |
  | 361 | 300-399 LO/RF HW | LO/RF synthesis hardware/firmware failure | freq/lock; indirect ampl |
  | 499 | 400-599 Auto-IF cal | automatic IF adjustment error (IF path could not self-adjust) | YES (IF cal) |
  | 561 | 400-599 Auto-IF cal | "LOG AMPL" -- unable to adjust amplitude of the LOG SCALE (log-amp linearity) | YES (log-amp) |
  | 562 | 400-599 Auto-IF cal | LOG AMPL -- possible problem in the SECOND STEP-GAIN stage | YES (IF step gain) |
  | 565 | 400-599 Auto-IF cal | LOG AMPL band (559-581) -- log-amp/step-gain adjust failure | YES (log-amp/step gain) |
  | 591 | 400-599 Auto-IF cal | automatic IF adjustment error (amplitude/BW adjust region) | YES (IF cal) |

  (313/317/319/351/353 decoded from primary/forum sources verbatim; 333/337/361/499/565/591
  classified by the manual's documented range headings + the LOG AMPL 559-581 band. Exact
  one-line names for those six were not extractable from the truncated online copies.)

- Assessment for the SE (ref - wall) measurement:
  1. **LO/RF codes (313,317,319,333,337,351,353,361)** are YTO/first-LO synthesis and
     loop-settling faults, several reporting a tuning DAC AT ITS LIMIT -- a hardware-drift
     signature, not a transient. Risk: at some tunes the LO may fail to lock or mistrack,
     depressing a tone (off preselector) or misplacing it. Live `MKF?`->48 GHz worked, so lock
     is achievable there, but the persistent DAC-at-limit codes mean LO margin is marginal and
     the risk concentrates at band edges / specific frequencies -- every SE point must be
     validated for lock + preselector peak, not assumed.
  2. **Auto-IF codes (499,561,562,565,591)** mean the internal LOG-AMP and IF STEP-GAIN
     self-adjustment did not converge. A CONSTANT absolute-amplitude offset cancels in
     SE = ref - wall (same freq, same settings). BUT log-scale-linearity and step-gain errors do
     NOT cancel when the reference tone and the wall tone sit at very different points on the log
     scale / in different IF gain ranges -- which is exactly the SE case (a 40-80 dB ref-wall
     span). So these codes can inject a non-cancelling amplitude error into the SE number.
- Conclusion: this unit's internal auto-calibration (Automatic IF Adjustment) and YTO/LO
  adjustment have NOT converged; it needs service / a full adjustment + factory calibration
  before its ABSOLUTE amplitude and WIDE-DYNAMIC-RANGE readings can be trusted. Interim
  mitigations that maximize cancellation: take ref and wall at IDENTICAL RL / attenuation /
  step-gain and frequency; prefer a substitution scheme that pads the reference down to the SAME
  screen level as the wall read (both in the same log-amp/step-gain region); verify LO lock +
  preselector peak at each SE frequency.
- NEEDS-LIVE-VERIFICATION: YES (already partly done). Recommended single-accessor checks:
  (a) run the instrument's internal Automatic IF Adjustment (or `IP` then re-read `ERR?`) to see
  which codes clear vs re-enter; codes that re-enter are genuine hardware/cal faults.
  (b) at 2-3 representative SE frequencies (low band, ~2.9 GHz crossover, high band) confirm a
  known-level CAL/source tone reads within spec and that the amplitude is independent of a +10 dB
  attenuation change (the UG compression/validity test) -- this quantifies whether the cal codes
  actually move the number.
- Gate-before-cite: the per-code decodes come from the HP/Agilent 8560E Service Guide
  (08560-90157) and 8561E/8563E Service Guide (08563-90214), Chapter 6 "Error Messages", plus a
  Keysight community thread and an EEVblog repair thread (URLs in Sources). NONE of these is yet
  distilled in `reference/`. Per repo policy, a `reference/operator-manuals/` distillation of the
  8560E service-guide error-code table (300-399 LO/RF, 400-599 Auto-IF, LOG AMPL 559-581 band)
  must be written before these codes are cited in any numbered PROJECT doc.

## F12 -- MEDIUM -- `query_errors()` docstring/semantics: "read clears it" is only half-true (persistent codes always return -> false "fresh fault" positives)

- Location: `drivers.py:965-976` (comment "native error queue; read clears it").
- Defect: per the manual, `ERR?` clears TRANSIENT HP-IB/parser errors, but PERSISTENT
  hardware/cal conditions are RE-ENTERED after each read (REQ-HP8560-072: "persistent errors are
  re-entered"). Live proof: the 13 F11 codes do not clear. So `query_errors()` ALWAYS returns
  the unit's persistent baseline set, and any consumer that reads non-empty `ERR?` as "a fault
  just occurred during this operation" gets a permanent false positive. Concretely,
  `loop.py:_error_queue_status` classifies all 13 (>=200) as FAIL, so the A-V7 self-check
  (`loop.py:669-676`) will PERPETUALLY report FAIL on this real unit -- and, worse, it cannot
  distinguish those baseline codes from a genuinely NEW code posted by the sweep it is checking.
- Correct behavior: snapshot the persistent baseline (read `ERR?` twice at startup; the set that
  survives the first read is the baseline), then flag only DELTA codes (present now, absent from
  baseline) as newly-occurring faults. Fix the driver comment to state that persistent codes are
  re-entered.
- Suggested fix: (driver) correct the comment; (loop.py/cli.py consumers) diff against a startup
  baseline rather than testing non-empty.
- NEEDS-LIVE-VERIFICATION: no (behavior already observed live).

---

## Reference-corpus note
The three primary references (`hp-8560-e-series-programming.md`, `agilent-8560e-users-guide.md`,
`agilent-8565ec-spectrum-analyzer.md`) already list `rf-se/se299/drivers.py` in `citing-docs`; no
new reference is needed for the command-string audit (F1-F10). HOWEVER, F11's per-code error
decodes rely on the 8560E/8563E SERVICE guides (08560-90157 / 08563-90214) Chapter 6 error table,
which is NOT yet in `reference/` -- gate-before-cite before using those codes in a project doc.

## Sources (F11/F12 research)
- HP 8560E & 8560EC Service Guide 08560-90157: https://www.testunlimited.com/pdf/an/08560-90157.pdf
- 8561E/EC & 8563E/EC Service Guide 08563-90214: https://www.testunlimited.com/pdf/an/08563-90214.pdf
- HP 8563E-EC full text (archive.org): https://archive.org/stream/hp_8563e-ec/8563e-ec_djvu.txt
- Keysight community, 8563E errors 317/319/334/351/353/356/459/561/562/564/565: https://community.keysight.com/forums/s/question/0D52L00005IdpbPSAR/hp-8563e-problems-error-317-319-334-351-353-356-459-561-562-564-565
- EEVblog 8560E repair (3xx errors, A15 board): https://www.eevblog.com/forum/repair/hp-8560e-spectrum-analyzer-repair-no-input-signal-displayed-solved/
