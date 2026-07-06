# se299 Device-Operation Audit -- Correct Operation vs Manufacturer Manuals

Purpose: verify that the se299 driver operates BOTH instruments completely correctly, every
operation traced to the persisted authoritative manual. This is the "do we drive each device
right?" audit; it is re-runnable whenever the driver changes. Reviewed by an RF/SE measurement
lens (preselection, leveling, detection, dynamic range).

Authoritative local documentation (gate-before-cite; both acquired + validated):
- Source: reference/operator-manuals/anritsu-68000-series-operation.md (+ .pdf, Anritsu 682XXB/
  683XXB Operation Manual 10370-10284; Native GPIB dictionary cross-checked to MG369xB PM
  10370-10366). Covers the 68367C (683XXC generation, same Native language).
- Analyzer: reference/operator-manuals/agilent-8560e-users-guide.md (+ .pdf, Agilent 8560 E-series
  User's Guide 08560-90158). Covers the 8565E/EC.

Driver under audit: rf-se/se299/drivers.py -- Anritsu68369 (source), Agilent856xEC (analyzer).

Legend: PASS = correct as-is; FIXED = deviation found and corrected in this pass; OP = operational
discipline (correct in code, must be honored by the caller/procedure).

---

## Source -- Anritsu 68000-series (68367C), Native GPIB

| # | Operation | Manual requirement | Driver | Status |
|---|-----------|--------------------|--------|--------|
| S1 | Enter CW + set frequency | `CFn <f> GH` selects CW mode AND sets Fn in one command; `Fn <f> GH` alone only loads a register and "does not affect the current output" | `set_freq` -> `CF1 <f> GH` | FIXED (was `CW1 F1 ...`, an invalid mnemonic + register-only load -> no tone) |
| S2 | Known-good output state | force internal leveling `IL1`, recouple + zero the step attenuator `AT0`/`ATT00`, clear RF-off pad `TR0`, no offset `LO0`, dBm mode `LOG` | `prepare()` sends `RST IL1 AT0 ATT00 TR0 LO0 LOG` | FIXED (added ATT00/TR0/LOG; rules out all six "leveled but no RF" software modes) |
| S3 | Set level (dBm) | `Ln <v> DM`, log mode | `set_power` -> `L1 <v> DM` | PASS |
| S4 | RF output on/off | `RF1`/`RF0` (RF1 = power-on default) | `rf_on`/`rf_off` | PASS |
| S5 | Completion/settling | 68367C fw 2.35 does NOT answer `*OPC?`/`*ESR?` (time out + poison the socket); use native `OSB` poll + fixed settle dwell | `await_settled` dwells (use_opc default False); `NetworkTransport.reconnect()` recovers a poisoned socket | PASS |
| S6 | Output verification | native `OF1`/`OL1`/`OSB` (register + status readback); OSB=0x00 = leveled/locked/no-error, but does NOT prove RF at the connector | `output_freq_mhz`/`output_level_dbm`/`status_byte`; new `settled_ok()` = OSB bit2 (unleveled) & bit3 (lock error) clear | FIXED (added `settled_ok`) |
| S7 | Max leveled power | leveled to the unit's rated max (bench unit leveled through +25 dBm, 0.01-40 GHz) | operator sets `L1`; verified leveled to +25 dBm live | PASS (OP) |

## Analyzer -- Agilent/HP 8565EC (8560 E-series), HP-IB

| # | Operation | Manual requirement | Driver | Status |
|---|-----------|--------------------|--------|--------|
| A1 | Clean known state | `IP` preset before configure; first sweep after `IP` is stale -> flush | `prepare()` -> `IP` + settle + `SNGLS` + throwaway `TS` | FIXED (a dirty state returned non-reproducible marker values until IP was added) |
| A2 | Preselector peaking > 2.9 GHz | above the 2.9 GHz band-0/1 crossover the YIG preselector must be peaked (`PP`, needs RBW > 100 Hz + nonzero high-band span) or a real tone reads low / is missed; `PSDAC` reuses the peak | new `peak_preselector(f)` (PP + returns PSDAC) and `set_preselector_dac()` (reuse) | FIXED (was ABSENT -- HIGH severity: invalidated every read above 2.9 GHz, i.e. most of the DC-40 GHz SE band) |
| A3 | Detector for the measurand | `DET POS` never under-reads a CW tone, but ALSO latches a low-duty PULSED masker's peak; `DET SMP` + averaging recovers a continuous CW tone from under a pulsed masker | `measure_peak` uses `DET POS`; new `measure_average()` = `DET SMP` + linear-power average (+ optional `VAVG`) | FIXED (added the masker-robust averaged read; live: pos-peak -21.7 vs avg -49.2 dBm exposes the pulsed masker) |
| A4 | Zero-span CW read | `SNGLS` / `SP 0HZ` / `RB`,`VB` (VBW>=RBW) / `DET POS` / `TS`;`DONE?` (sweep-complete sync) / `MKPK HI`;`MKA?` | `configure` + `measure_peak` follow exactly | PASS |
| A5 | Stale-sweep discipline | trace memory holds the prior sweep; always `TS` (a second `TS` after a hardware change: attenuation, preselector) before a marker query | `measure_peak` does `TS;DONE?` before `MKPK`; `peak_preselector`/`set_preselector_dac` re-`TS` after the PP/PSDAC hardware change | PASS |
| A6 | Reference level / attenuation | `AT` couples to `RL` in AUTO to avoid mixer compression; too-high RL buries weak signals; a real signal is atten-independent while a mixer spur drops faster than the added atten | `configure` sets `RL`; leaves `AT` at preset 10 dB AUTO (safe). Compression/real-vs-spur discipline is operational | PASS (OP) |
| A7 | Calibrated reading | keep sweep time/RBW/VBW autocoupled so `MEAS UNCAL` never asserts; CAL OUT is 300 MHz / -10 dBm | `configure` does not force sweep time (autocoupled) -> no UNCAL | PASS |

---

## Findings and disposition

All deviations found in this audit were CORRECTED and validated (209 hardware-free tests + live on
the golden two-VM). The two HIGH-value correctness items:

1. Source CW mnemonic (S1): the driver emitted no tone at all because `CW1 F1 ...` is not the CW
   command. Fixed to `CF1 <f> GH`; verified live (`OF1` returns the set frequency, tone confirmed).
2. Analyzer preselector peaking (A2): entirely absent, so every measurement above 2.9 GHz was
   subject to preselector mistrack (reading low or missing the tone). Added `peak_preselector`
   (PP + PSDAC readback) and `set_preselector_dac` (reuse the same peak for the reference and wall
   reads so both share an identical preselector state). Verified live (peaked to DAC 131 at 6 GHz).

## Operational discipline (correct in code; the procedure must honor it)

- Above 2.9 GHz, peak the preselector on the REFERENCE tone (source on), record the PSDAC, and
  reuse it for the wall read at that frequency (identical preselector state on both passes).
- Under a pulsed masker, use `measure_average` (sample detector + averaging), not the positive-peak
  read: the peak read returns the masker's pulse level at every frequency and buries the tone; the
  average recovers a continuous CW tone. The pos-peak-minus-average gap is itself a live pulsed-
  masker detector.
- The RF environment must be quiet (all maskers/jammers off) for an acceptance measurement; a
  broadband masker sets an interference floor that limits the measurable SE regardless of settings.
- `checkpath` should be run first to confirm the tone reaches the RX above the (masker-free) floor.

## Re-running this audit

Any change to `drivers.py` source/analyzer command strings should be re-checked against the two
persisted manuals above and this table updated. The command strings are additionally guarded by the
FakeTransport command-string tests in rf-se/se299/tests/.
