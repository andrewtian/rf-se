# se299 Field Readiness Audit -- REAL SE Measurement Gating

Status: AUDIT (read-only), 2026-07-05. ASCII only. Scope: everything still gating a REAL field
shielding-effectiveness (SE) measurement over the networked two-host bench, INSIDE the enclosure
(substitution SE(f) = reference - wall) and OUTSIDE (near-field walkaround / neighborhood RF).
Separates HARD BLOCKERS (no valid measurement without) from DEGRADERS (measurable but caveated),
each traced to the owning doc/task.

Companion docs: CANONICAL_SE_BASIS.md (the method), EQUIPMENT_VALIDATION.md (chain + coupling
audit), DISTRIBUTED_READINESS_PLAN.md (the distribution correction program),
NETWORKED_OPERATION_SPEC.md (the networked spec), audit/2026-07-04-issue-register-and-trace-plan.md
(the live-hardware issue register), BENCH_LIFECYCLE_STATE_MACHINE.md (states + invariants), and
shielded-enclosure-setup/147-rf-verification-ownership.md + 105p-master-procurement-buying-tracker.md
(equipment + procurement).

---

## Summary

The DIGITAL chain is proven live end-to-end: the owned Agilent 8565EC (RX, pad 18 / :5555) and
Anritsu 68367C (TX, pad 5 / :5556) both answer over the golden two-VM qemu bridge; every command
string is traced to the operator manuals and confirmed on hardware (CANONICAL_SE_BASIS.md sec 4a;
EQUIPMENT_VALIDATION.md sec 2). The measurement software, the EA8/PC6 dynamic-range gate, the
affirmative campaign_pass verdict, and the checkpath/calibrate/wall/se-gui operator flow are
complete and hardware-free-tested (226 SE tests plus the wider ~486/677 board).

What is NOT ready is the PHYSICAL RF measurement. Five things gate a real inside SE number and a
sixth gates going outside/multi-host:

1. The TX RF path is OPEN -- the transmit tone never reaches the RX (checkpath reads NO-COUPLING),
   proven three independent ways; localized to source RF-OUT -> cable -> antenna feed.
2. The committed RF cable set is ORDERED (2026-06-17) but not confirmed received; the source->antenna
   TX cable may not exist in on-hand stock.
3. No committed metrology antenna is physically on-hand (all matched pairs LOCKED / pending-order /
   OPEN-pool); the LPDA-TX/horn-RX used in the live diagnosis is a screening-grade ad-hoc config,
   not the IEEE-299 matched-pair metrology chain.
4. The 8565EC reference/timebase is marginal (load-triggered wedge, power-cycle-only recovery) and
   corrupts high-band (>2.9 GHz) frequency accuracy -- so the 18-40 GHz band (where the 100 dB
   target bites) is not yet trustworthy.
5. Substitution rigor: the calibrate->wall source-identity/level guard is still open (a silent
   source drift between passes is not yet asserted against).
6. Cross-machine any-client operation (needed to run inside AND outside across two hosts) is unsound
   today: the two-host acceptance layer is absent and reachability/discovery/telemetry are
   loopback-only.

METROLOGY NOTE (closes the noise-source question directly): a calibrated 346B/346C/NC346 noise
source is NOT in the substitution SE chain and is neither a blocker nor a degrader for an SE number.
Substitution SE = reference - wall cancels TX absolutely; the only "noise" needed is the analyzer's
own source-off floor (loop.measure_floor). A calibrated source matters only for absolute noise-power
/ ENR claims in the separate jammer/emanation program (project_bg7tbl_noise_source_owned.md:14;
105j). The owned BG7TBL rebadge is "uncalibrated, for bench/tracking use only."

Near-term achievable: a low-band (<2.9 GHz), gentle, single-consumer, single-machine inside SE
lower-bound -- which was already demonstrated as a clean band-0 figure (10 MHz-2.5 GHz, 1.33-4.33 dB)
-- once the TX path is physically closed. A certified DC-40 GHz / 100 dB number needs the committed
antennas + RX LNA received and the timebase serviced.

Doc-consistency note: 105p Sec 0a still lists the 8565EC/68369B/antennas as `[ ]` "LOCKED, not
ordered," while doc 147/159 and the live GPIB traces confirm the 8565EC and a 68367C source are
PHYSICALLY on the bench and answering. This audit treats the two instruments as present (established
by live traces) and the antennas as not-received (no receipt marker anywhere).

---

## BLOCKERS (no valid measurement without)

| # | Item | Why it blocks | Owning doc / task | What resolves it |
|---|------|---------------|-------------------|------------------|
| B1 | TX RF path OPEN (checkpath NO-COUPLING) | The transmit tone never reaches the RX. Proven 3 ways: off/on/off non-reversibility; steady-state on==off; DEFINITIVE power-tracking (RX flat at -59.3 dBm across a 30 dB TX swing at 1/2/6 GHz). RX path IS live (picks up ~-53 dBm ambient); source reports leveled/locked (OSB 0x00). Any SE number is untrustworthy until checkpath reads PATH-LIVE. | CANONICAL_SE_BASIS.md sec 7; audit/2026-07-04-issue-register (bench state); memory project_se299_live_se_testing_toolchain (2026-06-30 diagnosis); cli.py cmd_checkpath | Physically reseat/verify source RF-OUT -> cable -> antenna feed; confirm the source front-panel RF indicator is lit; re-run `checkpath` until PATH-LIVE with a reversible, power-tracking tone. No code change needed (path already validated vs sim + live bus). |
| B2 | Committed RF cables ordered but NOT confirmed received | The source->antenna TX run (INERGEK XH400T N-M/N-M 4.0 m + SMA-F/N-F LPDA pigtail) was ORDERED 2026-06-17 but no `RCV`/receipt marker exists anywhere; only the $199 adapter/saver set (PI-20260602-01, 13 pcs, 2x 2.4mm savers on hand) is confirmed on-hand. If the order has not arrived, B1 cannot be closed from on-hand stock. | 105p Sec 0a (`:40`, `:322`, changelog `:597`) + Sec 2d; 105u-cable-assembly-index.md; memory project_cable_set_resolved / project_inergek_cable_committed_build | Physically verify receipt of the 2026-06-17 INERGEK order; OR fall back to a known-good on-hand cable/pigtail for the first low-band run (a direct 2.4mm cable is already on the bench per the issue-register bench state). |
| B3 | No committed metrology antenna on-hand | The committed IEEE-299 chain is four matched pairs (AL-130R loop RX-only, TBMA1B biconical, DRH 1-18 GHz, SGH 18-40 GHz) -- ALL "LOCKED, not ordered" / OPEN-pool, none received. The DRH is quote-locked, not paid/received. A metrology-grade inside SE number cannot be produced without them. | 105p Sec 0a (`:44`); 105n-antenna-selection-reference sec 13.1; 147 R-OI-3 (WR-28 SGH OPEN) + R-OI-4 (loop/biconical pending order) | Order + receive the loop / biconical / DRH / SGH matched pairs. Until then, any inside number is screening-grade (see D4). |
| B4 | High-band (>2.9 GHz) SE not trustworthy -- marginal 8565EC timebase | The 8565EC reference/timebase wedges under load (codes 333/335/337/499; NOT GPIB-recoverable, power-cycle only) and its LO error is harmonic-multiplied above 2.9 GHz, corrupting high-band frequency accuracy (5 GHz tone 15 MHz off; a fixed spur locks the marker). The high-band read path (PP + MKF re-center + per-band leveled power + shared 10 MHz ref) is not yet built. This blocks 18-40 GHz -- the band where the 100 dB target bites. Low band (<2.9 GHz) is unaffected. | audit/2026-07-04-issue-register Issue 1 (timebase, tasks #16/#17) + Issue 3 (high-band read path); CANONICAL_SE_BASIS.md sec 4a | Service the reference/synth section (run the DECISIVE external-10MHz-ref-on-J9 localizer first to tell A21-OCXO vs downstream); build the high-band read path in drivers/loop; share a 10 MHz reference between source and analyzer. |
| B5 | Cross-machine any-client operation UNSOUND (gates OUTSIDE + multi-host) | To run coherently across two hosts (analyzer on A, source on B, observe from C) the distribution layer is not ready: two-host acceptance test (tests/test_distributed.py / TH.1) ABSENT; routable bind (W1.2, 0.0.0.0), telemetry bind-host (W1.3), discovery beacon (W2.1), cross-host lease coherence/epoch-fence/identity-reclaim/shorter-TTL (W2.2) all OPEN; telemetry + discovery are loopback-only; auth token is cleartext/replayable (W3). Distributed-arch audit verdict: canDoItToday = false. Single-machine loopback (golden two-VM) is NOT gated by this. | DISTRIBUTED_READINESS_PLAN.md (Wave 1 W1.2/W1.3, Wave 2 W2.1/W2.2, TH.1, Wave 3 W3.1; tasks #10/#11/#12); NETWORKED_OPERATION_SPEC.md | Complete Wave 1 reachability (routable bind + telemetry bind + discovery beacon), Wave 2 lease coherence (monotonic epoch/fence, identity-based reclaim, shorter TTL / takeover verb), and land the two-host acceptance test (atomic dual-acquire, host-down rollback, partition, two-clients-converge). Decide the trust model before Wave 3. |

Landed since the DISTRIBUTED plan was written (verified in code, NOT blockers): W1.1 dead-man de-key
(gpib_bridge/ni_gpib_server.py:604-673, RF0 on session teardown for LEASED and KEYED pads, so a
crashed/partitioned client cannot leave the source hot); W1.4 atomic all-or-nothing take_control
(coordinator.py:178-203, ControlConflict names RX/TX); Issue 5 keyed-pad de-key gap CLOSED in code.

---

## DEGRADERS (measurable but caveated)

| # | Item | Caveat | Owning doc / task |
|---|------|--------|-------------------|
| D1 | FX2LP (GPIB-USB-B, 8565EC) -110 wedge under load | The full-speed B adapter (fxload firmware) can wedge into fxload-limbo / a -110 hang under load; it survives VM reboot AND qemu USB reset -- a PHYSICAL power-cycle/replug (same USB port) is the only full recovery. A field/roaming or unattended session that wedges mid-run needs hands on the adapter. Mitigated single-machine by gentle operation + the HS QMP reattach recovery, but the B adapter needs physical intervention. | NETWORKED_OPERATION_SPEC.md Pass 13/14 + R6; memory project_se299_networked_multi_instance + project_se299_live_se_testing_toolchain (physical replug cleared -110 cleanly; discard the first post-recovery read) |
| D2 | RX 18-40 GHz LNA not ordered | Without the top-band preamp the 18-40 GHz link fields ~80 dB dynamic range, not the 100 dB headline target (mandatory for 100 dB @18-40). The LNA is OPTIONAL/`[ ]` in the tracker. | 105p L1 (`:145`); 147 sec 4.1; 159 |
| D3 | 100 dB solid pre-LNA stubs + metrology 2.4mm F-F saver + 10 MHz ref jumpers not ordered | The solid-Cu ISR-086/ISR-141 pre-LNA stubs (100 dB tier), a metrology-grade 2.4mm F-F saver for the 8565EC input, and the RG-316 10 MHz reference jumpers are STILL OUTSTANDING. Without the stubs + at-horn LNAs the kit is an 80 dB kit. RAD000203 (2.92 union) shows a ledger-vs-Sec2d inconsistency to confirm at receipt. | 105p Sec 2d (`:41`, `:225-226`, `:233`, `:363`, `:367`) |
| D4 | Live-diagnosis antenna config is screening-grade, not committed metrology | The LPDA-TX / 1-18 GHz-horn-RX used in the live bench diagnosis is a screening/leak-hunt config; the corpus states a log-periodic-TX vs different-RX is "NOT IEEE 299 substitution method." A number produced with it is a relative / lower-bound SCREEN, not a certified metrology SE. (The reciprocal biconical A2 would be valid; the AL-130R loop is active RX-only and needs its NIST AF -- neither on-hand.) | 105k-lf-to-vhf-antenna-procurement.md:64; 105n sec 2/sec 13.3; datasheets/rfv-loop-al130r-... |
| D5 | Substitution TX-identity/level guard (W2.3) OPEN | The calibrate->wall two-invocation flow does not yet record source *IDN?/serial + per-point level in the calibration and assert equality on the wall pass; a silent source swap or per-point level drift between passes could corrupt SE = ref - wall. Partial mitigation: the baseline-drift re-check IS implemented (coordinator.reference_drift / loop.reference_drift). | DISTRIBUTED_READINESS_PLAN.md W2.3; coordinator.py:279-286 |
| D6 | Dead-man de-key landed but not live-witnessed | The RF0-on-teardown safety is in code (leased + keyed pads) but the analyzer-as-witness confirmation (write RF1 un-leased, crash client, confirm the tone drops on the 8565EC) has not been run live. Safety-critical -- verify before trusting an unattended keyed run. | audit/2026-07-04-issue-register Issue 5 |
| D7 | IEEE-299-2006 primary PDF not-acquired | The method is standards-ANCHORED (reconstructed from three acquired cross-consistent sources: MIL-HDBK-1195, UFGS 13 49 20, MIL-STD-188-125-1) but the primary IEEE-299-2006 text is not in-repo. Traceability caveat only; does not affect a measured number. | CANONICAL_SE_BASIS.md sec 1a + sec 8; reference/gov-standards/ieee-299-2006.md |
| D8 | Calibrated noise source (346B/346C/NC346) not owned | OUT OF THE SUBSTITUTION CHAIN -- listed only to close the metrology question: it is neither required nor a degrader for an SE number (TX cancels; the floor is read source-off on the analyzer). It matters only for absolute noise-power/ENR claims in the jammer program. Owned BG7TBL rebadge is uncalibrated/bench-only. | memory project_bg7tbl_noise_source_owned:3,14 + project_noise_source_pick_audit:17; 105j |
| D9 | Analyzer ERR? baseline-delta + startup stale-lease reclaim | (Issue 6) the chronic healthy baseline ERR?=[111] is treated as new by the A-V7 self-check -> perpetual false failure until it snapshots the baseline and flags only deltas (code fix, no hardware). (INV-4) a SIGKILL'd client's lease relies on TTL expiry; a startup stale-lease force-preempt of a provably-dead session is an open trust decision. | audit/2026-07-04-issue-register Issue 6; BENCH_LIFECYCLE_STATE_MACHINE.md INV-4 (`:261-263`) |
| D10 | Source high-band leveling uncharacterized | At 5 GHz / 0 dBm the source read OSB=0x04 (unleveled) and the tone was ~15 MHz off; the per-band leveled-power envelope (sweep L1 until OSB bit2 clears at 5/10/20/40 GHz) is not yet recorded. Folds into B4 for high-band trust. | audit/2026-07-04-issue-register Issue 3 |

---

## Inside vs Outside delta

INSIDE (substitution SE, single-machine golden two-VM -- the near-ready case):
- Topology: analyzer + RX antenna inside, source + TX antenna outside, nothing crossing the shield;
  the digital/control link is fiber-only (PC3). The golden two-VM runs on loopback with no code
  change. Cross-machine reachability (B5) is NOT required for this.
- Gating set: B1 (TX path), B2 (TX cable receipt), B3 (metrology antennas), B4 (high-band only).
  Low-band (<2.9 GHz) gentle single-consumer is achievable first; high-band waits on B4.
- Safety posture is strong: single-consumer enforced server-side by the VXI-11 lease (one controller,
  many observers); take_control all-or-nothing; RF-off-on-exit in every loop/GUI finally
  (loop.py, walkaround nearfield_walkaround, sg_gui.rf_off_safe, bench_gui ordered shutdown);
  dead-man de-key on teardown; 8565EC health gate halts on WEDGED rather than measuring garbage
  (BENCH_LIFECYCLE_STATE_MACHINE.md INV-6). Health-gate = the WEDGED-state halt + invariant_violations().

OUTSIDE (near-field walkaround / neighborhood RF) adds, on top of the inside blockers:
- B5 in full: an operator roaming with the analyzer while the source transmits requires routable
  binds (not loopback), a telemetry stream reachable off-host, discovery without hand-typed
  addresses, and cross-host lease coherence -- all OPEN. No two-host acceptance test exists.
- Portable power for the analyzer + the adapter host (Pi/laptop): NOT specified or procured anywhere
  in the corpus (UNKNOWN -- resolve by a field-power plan; none found).
- A second roaming controller/host: the golden topology is two loopback VMs on one Mac; a physically
  separate roaming controller is not yet a supported deployment (only addresses differ in principle,
  but B5 reachability must land first).
- D1 (FX2LP wedge) risk is amplified: a wedge during a walkaround needs a physical replug.
- walkaround.py is leak LOCALIZATION (find WHERE it leaks after a low-SE frequency is found), NOT an
  SE(f) number. Neighborhood / broadband OUTSIDE survey is the SEPARATE top-level rf-se/ HackRF
  Tier-0 pipeline (1-6 GHz, 8-bit, screening + change-detection only; does NOT extrapolate to 40 GHz);
  its tools are not installed on the dev box and no hardware is attached (memory rf-se-hackrf-screening).

Net: inside single-machine is one physical fix (B1/B2) + gentle-low-band away from a first valid
lower-bound number; outside/multi-host is a whole distribution + logistics program away (B5 + portable
power + roaming host), plus the timebase (B4) and antennas (B3) for any certified figure.

---

## Recommended minimal path to a first valid INSIDE measurement

Target: a HONEST low-band (<2.9 GHz) inside SE lower-bound on a single machine, gentle + single-
consumer. This is the shortest path to a number that is real (not a sim, not SE=0-from-a-dead-path).

1. CLOSE THE RF PATH (B1/B2). Verify the 2026-06-17 INERGEK cable order arrived, or use a known-good
   on-hand cable/pigtail for the source->antenna TX run. Physically reseat source RF-OUT -> cable ->
   antenna feed; confirm the source front-panel RF indicator is lit.
2. GATE ON checkpath. Run `cli.py checkpath --source <addr> --analyzer <addr>` and require PATH-LIVE
   (a reversible off/on/off tone that also tracks TX power). Do NOT trust any SE number until this
   passes. NO-COUPLING prints the connector checklist and which side is live.
3. STAY LOW-BAND + GENTLE. Restrict the first run to <2.9 GHz, single consumer, stepwise retune,
   per-point settle, minimal RF toggling -- the envelope that already produced a clean band-0 figure
   (10 MHz-2.5 GHz). Defer >2.9 GHz until B4 (timebase service + high-band read path) is done.
4. CALIBRATE -> WALL. `cli.py calibrate` (reference pass, current geometry; graded USABLE/PARTIAL/
   FLOOR-LIMITED, saved JSON) then the wall pass (insert the shield) via se-gui/coordinator; SE(f) =
   reference - wall. Expect FLOOR-LIMITED lower bounds where the in-enclosure link is weak -- these
   are valid as SE >= capability (never a smaller certified number).
5. PROVE SUBSTITUTION VALIDITY. After the wall pass run coordinator.reference_drift to confirm the
   baseline held (a DRIFTED verdict means repeat). RF-off-on-exit + the dead-man de-key protect the
   source throughout. (Absent W2.3, manually confirm the same source unit + per-point level across
   the two passes.)
6. LABEL HONESTLY. Report it as a screening-grade, low-band, lower-bound SE taken with the ad-hoc
   LPDA/biconical config -- explicitly NOT a certified DC-40 GHz / 100 dB metrology number, which
   requires the committed matched-pair antennas (B3) + RX LNA (D2) received and the reference/timebase
   serviced for the high band (B4).

---

## Traceability

Software + method: CANONICAL_SE_BASIS.md, EQUIPMENT_VALIDATION.md, DEVICE_OPERATION_AUDIT.md,
README.md (operator runbook), cli.py (checkpath/calibrate/chain/wall/coordinator/se-gui),
coordinator.py, loop.py, gpib_bridge/ni_gpib_server.py, BENCH_LIFECYCLE_STATE_MACHINE.md.
Distribution: DISTRIBUTED_READINESS_PLAN.md, NETWORKED_OPERATION_SPEC.md,
CANONICAL_NETWORK_ARCHITECTURE.md. Live-hardware issues: audit/2026-07-04-issue-register-and-trace-
plan.md. Equipment + procurement: shielded-enclosure-setup/147-rf-verification-ownership.md,
159-rf-test-equipment-procurement-state-freeze.md, 105p-master-procurement-buying-tracker.md,
105n-antenna-selection-reference.md, 105u-cable-assembly-index.md. Memory topics:
project_se299_live_se_testing_toolchain, project_se299_networked_multi_instance,
project_cable_set_resolved, project_bg7tbl_noise_source_owned, rf-se-hackrf-screening.
