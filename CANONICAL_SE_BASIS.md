# Canonical SE Measurement Basis (se299) -- Executive Technical Committee Brief

Status: toolchain COMPLETE + validated (226 hardware-free tests + 2 live end-to-end on real
instruments); one OPEN physical bench item (transmit-path continuity). Audience: technical review
committee. Scope: the automated shielding-effectiveness (SE) acceptance measurement for the HMEMC
enclosure, DC-40 GHz, per the mission charter (doc 172, ADR-018) and the RF-verification ownership
doc (doc 147). This document is the single coherent basis for how a concurrent, dual-instrument SE
number is produced, why it is trustworthy, and what remains.

---

## 1. Method (canonical)

The measurement is the IEEE-299 SUBSTITUTION method (IEEE-299-2006; doc 147 sec 4.1, doc 159).
Source + TX antenna on one side, spectrum analyzer + RX antenna on the other, NOTHING crossing the
shield; all acquisition is over the digital bus (GPIB/10GbE), never read from the instrument
display.

Per frequency f, two RX-measured levels are taken with the TX power held CONSTANT:

    reference(f)  = RX level with the reference path (no shield / empty geometry)
    wall(f)       = RX level with the shield in the path

    SE(f) = reference(f) - wall(f)          [both RX-measured, in dB]

Load-bearing correction (was a recurring misconception): SE is NOT "known TX minus received RX."
The transmit power is common to both passes and CANCELS in the difference. The TX therefore needs
only to be STABLE and REPEATABLE across the two passes, not absolutely known. A reading at the
noise floor yields only a LOWER BOUND (SE >= capability), never a smaller certified number.

## 1a. Standards basis (repo-canonical hierarchy)

The standards hierarchy behind "IEEE-299 substitution method" is fixed project-wide by the mission
charter (doc 172, `shielded-enclosure-setup/172-shielding-standards-and-mission-charter.md`, ADR-018)
and is restated here, not re-derived:

- **IEEE Std 299-2006** -- the measurement method (enclosure substitution, native ceiling 18 GHz).
- **MIL-HDBK-1195** -- design + construction basis (seams, gaskets, waveguide-below-cutoff; the
  realistic SE ceiling of bolted construction).
- **MIL-STD-188-125-1** -- Performance-Requirement/Design-Objective vocabulary AND the free-space
  antenna technique that extends the measurement above 18 GHz through this campaign's 40 GHz
  Tier-1 ceiling. Above 18 GHz (this rig's 18-26.5 and 26.5-40 GHz bands) IEEE-299 and
  MIL-STD-188-125-1 are always cited together -- neither alone covers the full DC-40 GHz span
  measured here.

Down-ranked / excluded (doc 172 sec 2 "Down-ranked / excluded"):
- **MIL-STD-285 is historical only** -- cancelled 1997, superseded by IEEE Std 299. Cited, when at
  all, for historical context, never as the governing method.
- **ASTM D4935 is EXCLUDED** -- a planar-coupon transmission-line measurement, not a
  whole-enclosure measure; not used anywhere in this basis.

Research-scaffolding note: the repo-root document
`compass_artifact_wf-89de1e4c-e867-47b9-b8fd-084ff2f0d947_text_markdown.md` frames the method as
"IEEE 299 / MIL-STD-285 / ASTM D4935" and cites an approximate ">= ~6 dB" IEEE-299 dynamic-range
guidance figure. That framing predates doc 172 and is SUBORDINATE to it -- it is retained as
research scaffolding (its instrument-level GPIB-mnemonic research fed the drivers in sec 4) but is
NOT the canonical standards basis; doc 172's hierarchy above governs. Its ~6 dB figure is the LOWER
end of the dynamic-range band this rig's `margin_db` sits at the conservative top of (sec 3a).

Reconstructed-standard caveat: `reference/gov-standards/ieee-299-2006.md` itself records
`local-pdf: not-acquired` -- its distillation is RECONSTRUCTED from MIL-HDBK-1195, UFGS 13 49 20, and
MIL-STD-188-125-1 (all three primary-sourced and in-repo), not read from the primary IEEE-299 text.
The method used here is therefore standards-ANCHORED (three independent, acquired, cross-consistent
sources agree on it) but the primary IEEE-299-2006 document is not yet in-repo; gate-before-cite
still holds because every requirement cited traces to an acquired source, with IEEE-299 included
only by reconstruction pending acquisition.

## 2. Known TX from the power setting (committee question, resolved)

The source level command (`L1 <dBm> DM`) sets a calibrated power AT THE SOURCE OUTPUT CONNECTOR,
within the generator's leveling accuracy (Anritsu 68367C, ~+/-1.5 dB typical), confirmed live via
the native `OL1` readback. It is NOT the radiated or received power (that additionally needs cable
loss, antenna gain, and mismatch, all frequency-dependent). This is sufficient because TX cancels
in SE = reference - wall; the known connector-plane power is recorded per point for audit and for
the link-budget sanity check (which is exactly what flags a dead transmit path -- see sec 7).

## 3. Measurement integrity (why a number is trustworthy)

- Acceptance is STEPPED-CW, zero-span, positive-peak, narrow-RBW, source-tracked per point. Only
  these rows can certify. The fast swept-span mode is a SCREEN only (its span-coupled RBW floor is
  blind to deep narrow leaks) and is structurally excluded from any pass verdict.
- EA8 dynamic-range gate (doc 159 PC6): capability(f) = reference(f) - floor(f) - margin. A point
  can only certify SE up to its capability; below that it is reported as a lower bound. `ea8_ok`
  requires capability >= target.
- Affirmative `campaign_pass`: True ONLY if points exist, EA8 passes everywhere, every row is a
  stepped-CW acceptance datum, source-tracked, and every verdict is PASS. A screening row can never
  make a campaign pass.
- Floor-limited handling: a wall reading within margin of the floor is a lower bound (SE >= X);
  `se_db` is never surfaced as certified when floor-limited.

## 3a. Acceptance gates (canonical, as computed by loop.py)

This section is the single canonical statement of the EA8/PC6 gate and the affirmative
`campaign_pass` predicate; both are implemented in `loop.py` and MUST be read from there if this
prose and the code ever disagree.

**EA8 capability** (`loop.acquire_reference`, `budget.se_capability_db`):

    capability(f) = reference(f) - floor(f) - margin_db

`margin_db` defaults to 10 dB (`config.Campaign.margin_db`) -- the CONSERVATIVE top of IEEE-299's
dynamic-range guidance band: the research scaffolding (compass artifact, sec 1a) cites ">= ~6 dB"
above target SE, while the acquired reconstruction (`reference/gov-standards/ieee-299-2006.md`)
states the system dynamic range "must exceed the target SE by at least 10 dB." The repo commits to
the top of that 6-10 dB band; this is documented here, not changed.

`ea8_ok` (per point) := `capability >= band.target_se_db`. The C3 adaptive RBW ladder
(`config.AnalyzerSettings.rbw_ladder_hz`) re-measures an EA8-limited point at successively narrower
RBW (each decade buys +10 dB per `budget.noise_floor_dbm`) until `capability` clears the target or
the ladder is exhausted; the FINAL `rbw_hz` tried is recorded per point and `measure_wall` reads
that same point back at that same `rbw_hz` (symmetric per index -- SE is a ratio).

**Floor-limited lower-bound rule** (`loop.measure_wall`):

    floor = max(reference_floor, wall_floor)          # worse-case of the two passes' floors
    floor_limited = wall <= floor + margin_db

If `floor_limited`, the wall reading cannot be trusted deeper than the reference pass already
proved it could see: `se_reported_db = capability` (a LOWER BOUND, SE >= capability) and
`verdict = PASS` iff `capability >= target` else `INCONCLUSIVE` (more dynamic range is needed --
narrower RBW, an RX LNA, or higher-gain horns; never `FAIL` on a floor-limited read). Otherwise
`se_reported_db = se_db` and `verdict = PASS` iff `se_db >= target` else `FAIL`.

**Affirmative `campaign_pass`** (`loop.summarize`), stated exactly as the code computes it:

    campaign_pass  :=       points exist
                     AND     EA8 passes everywhere          (ea8_ok True at every reference row)
                     AND     every row is acceptance-mode    (acq_mode == "stepped-cw-zerospan"
                                                               AND purpose == "acceptance")
                     AND     every row is source_tracked
                     AND     every verdict is PASS
                     AND     rbw_symmetric                   (reference[i]["rbw_hz"] ==
                                                               wall[i]["rbw_hz"] for every index i)

This is AFFIRMATIVE by construction: a screening row (swept-span, near-field probe) fails the
acceptance-mode/purpose test and so structurally cannot make a campaign pass; an asymmetric
per-index RBW (C3) cannot either, even if every individual SE number happens to clear its target.

## 4. Instrument chain + confirmed driver corrections

RX: HP/Agilent 8565EC spectrum analyzer. PRIMARY cite: the Agilent/HP 8560 E-Series and EC-Series
Spectrum Analyzers User's Guide (08560-90158), distilled in
reference/operator-manuals/agilent-8560e-users-guide.md -- the exact manual family covering the
EC-series unit on the bench. Corroborating cross-ref: the HP 8560 E-Series programming reference
(08560-90146, reference/operator-manuals/hp-8560-e-series-programming.md), which carries the
identical 8560-series remote language and supplies the fuller GPIB command dictionary (error-code
ranges, MKBW, SAVES/RCLS) used elsewhere in `drivers.py`. TX: Anritsu 68367C synthesized source
(68000-series native language, distilled in
reference/operator-manuals/anritsu-68000-series-operation.md). Both driven over the network GPIB
bridge (see NETWORKED_OPERATION_SPEC.md); a two-VM "golden" deployment gives each unit its own
controller on loopback.

Two faults were found and fixed, each confirmed on the live golden two-VM setup:

- SOURCE CW output is `CF1 <GHz> GH` ("set CW mode at F1" + value in one command), NOT the prior
  `CW1 F1 ...`. `CW1` is an invalid mnemonic (silently discarded); `F1 <v> GH` alone only loads a
  register and, per the manual, "does not affect the current output" -- so the source sat in its
  power-up sweep mode radiating no CW tone. Confirmed vs the Anritsu 682XXB/683XXB Operation Manual
  (P/N 10370-10284), cross-checked to the MG369xB GPIB Programming Manual (P/N 10370-10366 --
  identical 68000-series native language), both distilled in
  reference/operator-manuals/anritsu-68000-series-operation.md; verified live via native
  `OF1`/`OL1`/`OSB` readback. (The 68367C fw 2.35 answers `*IDN?` but not `*OPC?`/`*ESR?`, which
  time out and poison the socket; native readback + a settle dwell are used instead.)
- ANALYZER must be preset (`IP`) to a clean state and its first (stale) sweep flushed before any
  read; from a dirty state the marker returns non-physical, non-reproducible values (identical
  configure calls returned -80 then -1.7 dBm). Preset is issued once at campaign start.

## 4a. Representative trace appendix (exact wire sequences, current code)

Every line below is the ACTUAL command/query string the current drivers write, in call order, as
literally executed by `loop.acquire_reference` / `loop.measure_wall` against
`drivers.Anritsu68369` / `drivers.Agilent856xEC`. This is deterministic driver output, not
illustrative pseudocode; each mnemonic is cited to its manual (and page, where the reference
frontmatter records one).

**(1) Once-per-pass preamble** (`source.prepare()` + `analyzer.prepare()` + the first
`analyzer.configure()`):

    RST                 native reset  (anritsu-68000-series-operation.md, OM 10370-10284 sec 3-11)
    IL1                 internal ALC leveling  (MG369xB PM 10370-10366 p.3-56/3-72)
    AT0                 re-couple step attenuator to ALC  (10370-10366 p.2-32/3-9)
    ATT00               zero the step-attenuator pad  (10370-10366 p.2-32/3-9)
    TR0                 0 dB RF-off attenuation, not the 40 dB TR1 trap  (10370-10366 Table 2-14)
    LO0                 zero level offset  (anritsu-68000-series-operation.md)
    LOG                 dBm level mode  (10370-10366 L1/OL1 dictionary p.3-58/3-97)
    IP                  analyzer instrument preset  (agilent-8560e-users-guide.md, UG 08560-90158
                        Ch.7 p.495, Table 7-7)
    [sleep 1.0 s]       IP self-cal/settle dwell
    SNGLS               single-sweep mode  (UG 08560-90158 Ch.7)
    TS                  flush the stale post-preset sweep  (UG 08560-90158 Ch.7 p.642)
    DONE?               wait for sweep complete  (UG 08560-90158 Ch.7 p.459)
    SNGLS               configure(): re-assert single-sweep
    RB 1000HZ           resolution bandwidth (campaign default rbw_hz)
    VB 1000HZ           video bandwidth
    RL 10.0DBM          reference level (campaign ref_level_dbm)
    DET POS             positive-peak detector  (UG 08560-90158 Ch.7 p.453)
    SP 0HZ              zero span -- CW power vs time at CF  (UG 08560-90158 Ch.7)

**(2) Reference-floor read** (`measure_floor`, source off, per point f):

    RF0                 RF output off  (10370-10366 p.3-112)
    DET SMP             sample detector for the true (unbiased) floor  (UG 08560-90158 p.453)
    CF <f>HZ            tune to f, zero span
    TS                  take one sweep (blocks)  (p.642)
    DONE?               sweep-complete sync  (p.459)
    MKPK HI             marker -> highest peak  (p.528)
    MKA?                read marker amplitude, dBm  (p.528)
    DET POS             restore configure()'s detector

**(3) Reference-RF-on read** (per point f, at the current RBW rung):

    RF1                 RF output on  (10370-10366 p.3-112)
    OSB (x<=3, early-exit)   native settle handshake: poll until leveled+locked (bit2
                        RF-Unleveled=0, bit3 Lock-Error=0; 10370-10366 Fig 2-10 p.2-48)
    [sleep settle_s]    analog settle dwell (0.05 s default)
    -- if f > 2.9 GHz (peak_preselector; DAC recorded once, at the widest rung): --
    CF <f>HZ            center on the tone
    SP 50000000HZ       50 MHz span, must be entirely > 2.9 GHz  (UG 08560-90158 p.560)
    RB 1000HZ           RBW > 100 Hz required for PP  (p.560)
    TS ; DONE?          sweep + sync
    MKPK HI             marker -> peak
    MKCF                marker -> center frequency  (p.7-114)
    TS ; DONE?          re-sweep after the MKCF retune
    PP                  peak the preselector (blocks until done)  (p.560)
    DONE?               wait for PP to finish
    TS ; DONE?          fresh sweep -- PP changed hardware, trace is stale until this completes
    PSDAC?              read the peaked DAC (0-255), recorded as preselector_dac  (p.563)
    -- re-assert the zero-span acceptance config (PP zoomed away from it): --
    SNGLS ; RB <rbw>HZ ; VB <vbw>HZ ; RL <ref>DBM ; DET POS ; SP 0HZ
    -- tone read: --
    CF <f>HZ ; TS ; DONE? ; MKPK HI ; MKA?
    RF0                 RF output off

**(4) Wall read** (`measure_wall`, per point f, at the SAME rbw_hz the reference row ended on):

    SNGLS ; RB <rbw>HZ ; VB <vbw>HZ ; RL <ref>DBM ; DET POS ; SP 0HZ   (re-configure at ref_row rbw)
    L1 <p>DM            set source level  (10370-10366 p.3-58/3-97)
    CF1 <f/1e9> GH       set CW mode + frequency in one command  (10370-10366 Table 2-5 p.2-20)
    RF0                 RF output off
    DET SMP ; CF <f>HZ ; TS ; DONE? ; MKPK HI ; MKA? ; DET POS   (wall's OWN source-off floor --
                        never the stale reference-pass floor)
    RF1                 RF output on
    OSB (x<=3, early-exit) ; [sleep settle_s]     native settle handshake
    -- if the reference row peaked a preselector at this index (f > 2.9 GHz): --
    PSDAC <dac>          reuse the EXACT DAC peaked in the reference pass -- the through-shield
                        tone here is too weak to re-peak reliably  (p.563)
    TS ; DONE?           hardware applies at end of sweep
    -- tone read: --
    CF <f>HZ ; TS ; DONE? ; MKPK HI ; MKA?
    RF0                 RF output off

**Adaptive-RBW branch (C3):** if the widest rung's `capability < target` (EA8-limited), the
reference pass repeats step (2)+(3) at the next `rbw_ladder_hz` entry (default 1000 -> 100 -> 10 Hz)
-- its OWN floor and OWN tone read at the new RBW, re-`configure()`d each time -- WITHOUT re-peaking
the preselector (the DAC from the widest-rung peak is reused verbatim; PP needs RBW > 100 Hz and is
hardware-state independent of RBW once set) -- stopping as soon as capability clears the target or
the ladder is exhausted. `measure_wall` then reads that point back at whichever `rbw_hz` the
reference pass finally landed on (symmetric per index).

**Captured live example.** A golden two-VM run on 2026-07-03, against the real Anritsu 68369 +
Agilent 8565EC (each on its own bridge controller), exercised this exact sequence end-to-end across
1 GHz -> 40 GHz: preamble, reference floor/RF-on reads (including preselector peaking above 2.9 GHz),
and wall reads. RX read a flat -90 dBm floor at every point with the TX blasting -- i.e. the
DIGITAL/command loop above is PROVEN live (every write/query executed, every readback returned a
real number); what has not yet coupled is the RF itself (RX observing the TX tone), which is the
open transmit-path item (sec 7), localized to the TX cable/connector chain, not to this software.
The command bytes above are the actual current driver output (deterministic, not illustrative); the
2026-07-03 run is offered as the one captured live example of them executing against real hardware.

## 5. Operator workflow (gated)

Three gated steps; each gates the next (README "Operator runbook" has the commands). `ADDR` is
`sim` | `net:HOST:PORT:PAD` | a VISA string, or `--vm`/`--vm-mode golden` for seamless bring-up.

1. checkpath -- RF-path go/no-go BEFORE trusting SE. Transmits a tone and requires it to be
   REVERSIBLE (rise above the floor when TX on AND fall back when off); an off/on/off bracket
   rejects an ambient signal that merely drifts in. Verdict PATH-LIVE / NO-COUPLING + which side is
   live + a connector checklist.
2. calibrate -- reference pass in the current geometry, graded USABLE / PARTIAL / FLOOR-LIMITED,
   saved as loadable JSON, recording known TX + measured coupling per point.
3. se-gui / coordinator -- run the campaign; the GUI paints the live SE(f) curve coloured by
   per-point verdict with operator controls (Run/Stop, top-band gain, RBW); the coordinator streams
   the same live figure headless.

## 6. Validation status

- 226 hardware-free tests pass (adds: se_gui 12, calibration 6, checkpath 5, device_operation 17),
  plus 2 live
  end-to-end tests on the real instruments (both units answer; the canonical reference pass runs
  source-tracked and returns well-formed acceptance rows).
- The full operator workflow was demonstrated composing end-to-end against the simulator
  (checkpath PATH-LIVE -> calibrate USABLE -> se-gui live SE(f) curve) and, on the golden two-VM
  live setup, the digital chain end-to-end (both units commanded concurrently, real per-point reads,
  SE computed, GUI painted).
- Committed at 6999fbf7 on branch perf/faster-panel-door-builds.

## 7. Open item (the ONLY blocker to a live SE number): transmit-path continuity

With the digital chain fully validated, the live in-enclosure measurement returns NO-COUPLING: the
transmit tone never reaches the RX. This is localized to the TRANSMIT side (source RF-OUT -> cable
-> LPDA feed), proven THREE independent ways:

1. Reversibility: the off/on/off tone bracket shows no reversible rise above the floor.
2. Steady-state toggle: RF on and RF off read the same level at a settled frequency.
3. Power-tracking (definitive): the RX level is flat (-59.3 dBm) across a 30 dB TX power swing
   (-10 to +20 dBm) at 1/2/6 GHz. A coupled tone would track TX power 1:1; it does not move.

Corroborating: the RX side IS live (it picks up a ~-53 dBm ambient signal, source-independent), so
the receive path is intact and the fault is on transmit. The source firmware reports leveled and
locked (status byte 0x00) and confirms its commanded frequency/level, so the fault is downstream of
the source output connector.

Resolution (physical, outside software): reseat the SMA connectors from the source RF-OUT through
the cable to the LPDA feed; confirm the source front-panel RF indicator is lit; re-run checkpath.
The instant it reads PATH-LIVE, calibrate and se-gui produce real live SE figures with NO code
change -- that path is already validated against both the simulator and the live instrument bus.

## 8. Traceability

- IEEE-299-2006 (reference/gov-standards/ieee-299-2006.md) -- substitution SE method. CAVEAT:
  its `local-pdf` is `not-acquired`; the distillation is RECONSTRUCTED from MIL-HDBK-1195,
  UFGS 13 49 20, and MIL-STD-188-125-1 (per its own frontmatter) -- the method is standards-anchored
  but the primary IEEE-299 text is not yet in-repo. Full standards reconciliation: sec 1a.
- Mission + standards charter: doc 172
  (`shielded-enclosure-setup/172-shielding-standards-and-mission-charter.md`, ADR-018) -- canonical
  Tier 1/2/3 hierarchy; sec 1a here restates the parts load-bearing for this measurement.
  RF-verification ownership: doc 147 (`shielded-enclosure-setup/147-rf-verification-ownership.md`)
  -- that doc references this one as the measurement-automation owner. Control loop + PC1-PC9
  integrity gates: doc 159
  (`shielded-enclosure-setup/159-rf-test-equipment-procurement-state-freeze.md` sec 4a / 4.2b).
- Analyzer, PRIMARY cite: reference/operator-manuals/agilent-8560e-users-guide.md (Agilent/HP 8560
  E-Series and EC-Series Spectrum Analyzers UG, 08560-90158). Corroborating cross-ref:
  reference/operator-manuals/hp-8560-e-series-programming.md (HP 8560 E-Series UG, 08560-90146).
- Source: reference/operator-manuals/anritsu-68000-series-operation.md (Anritsu 682XXB/683XXB
  Operation Manual, P/N 10370-10284, cross-checked to the MG369xB GPIB Programming Manual, P/N
  10370-10366) -- source native command set; every source mnemonic here is CONFIRMED against it and
  verified live via native `OF1`/`OL1`/`OSB` readback (sec 4).
- Networked/dual-instance operation: NETWORKED_OPERATION_SPEC.md. Operator commands: README.md.
