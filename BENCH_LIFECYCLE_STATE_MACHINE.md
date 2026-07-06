# se299 Bench Lifecycle + Per-Unit State Machine -- Audit and Improvements

Purpose: ensure the bench GUI drives BOTH instruments (8565EC analyzer "rx", 68367C source "tx")
through a correct, race-free lifecycle, and that at any instant we KNOW what state each unit is in so
invariants and inconsistencies can be checked. Triggered by a live defect: Point Op frequently showed
no PSD feed. This doc records the audit, the root cause, the fixes applied, the per-unit state
machines, the invariants now enforced, and the ranked remaining improvements.

Scope: `bench_gui.py` (tabbed mode host), `instrument_hub.py` (InstrumentHub + Arbiter),
`control_lease.py` (per-instrument bridge lease), `sa_gui.py` / `sg_gui.py` (engines), and the mode
panels `point_op_mode.py` / `range_mode.py`. All findings reproduced live over the qemu GPIB bridge,
single consumer (no fakes in the runtime path).

Legend: FIXED = corrected this pass; OPEN = recommended, not yet implemented; OP = operational
discipline (correct in code, honored by the caller).

---

## 1. The reproduced defect (evidence)

Symptom (operator): launching the bench and selecting the Point Op tab often shows a blank PSD (no
feed). Sometimes it works.

Reproduced headless against the live bridge, exercising the exact operator flow (start bench with tab
0 active, then select the Point Op tab), 3 launches:

```
iter 0: after start (tab0 active) -> analyzer owned by a NON-ACTIVE mode
        after switch to Point Op  -> NO FEED  (bridge: "pad 18 locked by session 294")
iter 1: ...                       -> FEED (601 pts)
iter 2: after start               -> analyzer owned by a NON-ACTIVE mode
        after switch to Point Op  -> NO FEED  (bridge: "pad 18 locked by session 300")
```

2 of 3 launches failed. The failure is nondeterministic, which is why it "sometimes" worked. The
bridge error ("pad locked by session N") is the tell: the analyzer lease was already held by a stale
session when Point Op tried to acquire it.

The state after `start()` was also internally torn (observed): the ACTIVE tab suspended and owning
nothing, while NON-active tabs were unsuspended and holding the leases; within a single mode one
engine suspended and its sibling not -- a state a clean tab handoff can never produce.

---

## 2. Current architecture (as audited)

Three ownership mechanisms cooperate:

1. **ControlLease (bridge lease), one per instrument** -- `instrument_hub.py` holds a single
   `control_lease.ControlLease` for "rx" and one for "tx", leased on first acquire and held (with a
   ttl/3 keepalive) until `shutdown()`. This is the SERVER-side single-consumer lock at the bridge.

2. **Arbiter (internal owner), pure state machine** -- `instrument_hub.py:Arbiter` tracks which
   ENGINE currently drives each instrument, with one level of preemption (acquiring an owned
   instrument suspends the prior owner and remembers it; releasing restores it). This arbitrates
   between engines that share the one lease.

3. **Tab handoff** -- `BenchWindow._on_tab(idx)` calls `resume()` on the active mode and `suspend()`
   on the rest. A mode's `suspend()` releases its engines' Arbiter ownership and forces RF off; its
   `resume()` re-acquires and re-applies settings. Each engine runs its own background thread
   (`run()` loops `step_once()`), and `step_once()` lazily calls `hub.acquire()` on the first
   iteration after resume.

Intended invariant: exactly ONE mode (the active tab) drives rx and tx; all others are suspended and
hold nothing.

---

## 3. Root causes (why the intended invariant was violated)

- **R1 (FIXED) -- startup race.** `BenchWindow.start()` started ALL three modes' engine threads
  unsuspended, THEN suspended the non-active ones. In the window before the suspend, every mode's
  thread raced to `hub.acquire()`. A non-active mode could win the analyzer lease. When the operator
  later selected Point Op, the lease was still held by that mode's session -> Point Op's acquire
  failed -> no feed. Root of the reproduced defect.

- **R2 (FIXED) -- lock-free Arbiter mutated from two thread contexts.** `acquire()` runs on an
  engine's background thread; `release()` can run on the main (Qt) thread via a mode `suspend()`.
  The owner map had no lock, so concurrent handoffs could tear (explains the observed within-mode
  inconsistency).

- **R3 (OPEN, mitigated) -- one-level preemption cannot represent 3 competing modes.** The Arbiter
  remembers a single suspended prior. With three modes racing (the pre-fix world), a deeper prior
  was forgotten and left suspended forever. After R1, only ONE mode's engines ever acquire, so this
  path is no longer exercised cross-mode; it remains only for the intended in-mode case (an SE engine
  preempting a manual engine). Documented so it is not mistaken for general N-way arbitration.

- **R4 (FIXED) -- no authoritative "who owns what" + no invariant check.** Ownership was emergent
  from thread races; nothing could assert or report the intended single-owner invariant, so the
  torn state was silent.

- **R5 (FIXED) -- unclean exit strands the bridge lease.** A NetworkTransport auto-leases its pad on
  connect, and the bench holds each lease (keepalive-renewed) until `shutdown()`. `shutdown()` runs
  on the window-close (closeEvent) path, but a Ctrl-C / kill of the process (Qt's `app.exec()`
  swallows SIGINT) never fires it -- so the lease was left held for the full 120 s TTL. The operator
  closes/kills the GUI and relaunches within the TTL, the analyzer is still "locked by session N",
  and the new launch shows no feed. This is a SECOND, independent cause of the reported symptom
  (distinct from R1): reproduced by killing a running bench and observing the stranded lease block
  the next launch.

- **R6 (FIXED) -- SpectrumEngine ran the analyzer in ZERO SPAN, so a tone never showed as a peak.**
  `Agilent856xEC.configure()` ends with `SP 0HZ` (its zero-span CW-read heritage for the SE point
  power measurement -- measure_peak/measure_floor). `SpectrumEngine._apply` called `set_frequency`
  (which sets the real span) BEFORE `configure`, so the span was immediately wiped: every SA / Range /
  Point-Op sweep ran at ZERO SPAN (power vs time at CF), not a spectrum. A spectral tone could not
  appear as a peak -- it showed as a raised flat line, and the noise floor collapsed to a flat trace.
  This is the primary "the tone does not appear in Point Op" cause. Fix: reorder so `set_frequency`
  runs AFTER `configure` (F5). Live-proven: bench Point Op read `SP?=0` before, `SP?=5MHz` after; a
  -30 dBm 2.45 GHz source then reads as a -34 dBm spectral PEAK ~44 dB above a -78 dBm floor on a
  direct read. `configure()` itself is unchanged, so the SE-coordinator zero-span measure path keeps
  its `SP 0HZ`.

- **R7 (FIXED) -- `arm_and_wait` read a PARTIAL/blank sweep -> Point Op "NO TONE".** `TS; DONE?` does
  not block for the new sweep over the networked bridge; `arm_and_wait` had no dwell, so `read_trace`
  read mid-sweep. Benign until `CLRW TRA` (issued by `configure()` and `set_max_hold(False)` on every
  apply) cleared the trace -- which then never refilled, railing the read flat at the bottom
  graticule. Deterministic isolation: +`CLRW TRA` alone = 12/12 -> 0/12 tone; + a real per-sweep dwell
  = back to 12/12. Fix F6 (dwell in `arm_and_wait`). This -- not a marginal reference -- is the true
  cause of "NO TONE"; the analyzer was confirmed healthy (live sweep, ERR?=111) throughout.

### Refuted hypotheses (recorded so they are not re-chased)

- The "marginal 8565EC / power-cycle needed" reading (item 7.0 as first written) was WRONG. The RX
  rail was fully deterministic and software-caused (R7, `CLRW TRA` + no read dwell); the "TX tone
  suppressed ~-73 dBm" was the same partial-trace read, not a source-level defect (the source emits
  -30 dBm and reads back -34 dBm once the sweep completes). No power-cycle was needed.
- An "8565EC is WEDGED/frozen" reading during this audit was a FALSE POSITIVE of a too-fast
  two-snapshot sweep-alive check: `TS; DONE?` does NOT block over the networked bridge, so two
  back-to-back `TRA?` reads return the same held trace (0 of 601 changed). With a real ~0.5 s dwell
  between sweeps the trace advances on ~590/601 points -- the sweep is LIVE (ERR?=111, no
  reference-unlock codes). Lesson: over the bridge, verify sweep-advance with a real dwell, never by
  the raw TS/DONE? handshake alone.

An "immediate tab-switch causes concurrent connect() churn on the shared link" theory was
investigated and REFUTED: on a CLEAN bridge, an immediate switch to Point Op (no dwell) feeds 3/3
(601 pts). The intermediate no-feed observations that suggested churn were all caused by a stranded
lease from a prior test process that had exited uncleanly (R5) -- including the dwell case, which the
churn theory could not explain. Lesson: check the bridge lease table (`lease_report`) before
diagnosing a bench-side ownership bug.

---

## 4. Fixes applied this pass

- **F1 (R1) -- pre-suspend before threads spawn.** `BenchWindow.start()` now suspends EVERY mode
  before starting its engine threads, then resumes ONLY the active tab. Non-active engines spawn
  already parked and never acquire. Only the active mode ever drives the shared units.
  Live result: 3/3 launches feed; active tab is always the sole owner.

- **F2 (R2) -- thread-safe Arbiter.** `Arbiter` guards `acquire`/`release`/`owner`/`snapshot` with a
  re-entrant lock (`RLock`, because `acquire()` -> prior `suspend()` -> `release()` re-enters on the
  same thread; a plain `Lock` would self-deadlock).

- **F3 (R4) -- per-unit introspection + invariant checker.**
  - `Arbiter.snapshot()` -> owner + suspended-prior name per instrument.
  - `InstrumentHub.state_snapshot()` -> per unit: owner, lease_held, link health state.
  - `BenchWindow.invariant_violations(active=None)` -> [] when the active tab is the sole driver and
    all non-active modes are suspended; otherwise names each violation (identity-based, so it is not
    fooled by engines sharing a class name).
  - `BenchWindow.state_report()` -> units snapshot + active tab + violations, safe to poll on a timer.
  - Live result: 0 violations across repeated tab switches; each unit reports owner + lease + READY.

- **F4 (R5) -- release the lease on kill.** `qt_common.install_exit_cleanup(cleanup)` installs
  SIGINT/SIGTERM + atexit handlers that run the bench shutdown (release both leases, RF off) exactly
  once, wired into the CLI `bench` entry point; `BenchWindow.shutdown()` is hardened to ALWAYS reach
  the lease release even if a mode teardown raises; the entry point runs shutdown in a `finally`.
  Live result: killing a running bench with SIGTERM frees the lease immediately (was: held for the
  120 s TTL), and an immediate relaunch feeds (601 pts). SIGKILL still cannot be caught -- there the
  TTL is the only backstop, and the startup absent-surfacing ("RX leased by session N") tells the
  operator to wait or retry.

- **F5 (R6) -- SpectrumEngine reasserts the span after configure().** `SpectrumEngine._apply` now
  calls `an.configure(...)` FIRST (it ends with `SP 0HZ`) and `an.set_frequency(center, span)` AFTER,
  so the intended span survives. Applies to SA / Range / Point Op (all share SpectrumEngine).
  `configure()` is unchanged -- the SE-coordinator zero-span point-power path (measure_peak) still
  gets its `SP 0HZ`. Commit 2da2de03; board green (599 passed). Live: `SP?=0 -> SP?=5MHz`, tone
  appears as a peak on a direct read.

- **F6 (R7) -- `arm_and_wait` dwells for sweep completion.** After each of its two `TS`, it now sleeps
  `max(_SWEEP_LIVE_DWELL_S, ST? * 1.5)` so the sweep COMPLETES before `read_trace()` over the bridge
  (`DONE?` does not block). This refills a `CLRW`-cleared trace, so the read is a real completed sweep
  instead of a blank railed one. Kept CONTS (SNGLS is stale over the bridge; CONTS keeps the front
  panel live on release). The sim analyzer has its own `arm_and_wait`, so hardware-free tests are
  untouched. Commit f14723d0 + regression 121efd18. Live: bench Point Op reads TONE OK (-34.3 dBm,
  floor -77.7) every sample; board green (600 passed).

Tests: `tests/test_bench.py` (pre-suspend ordering; invariant detects torn ownership; Arbiter
concurrent-safe; exit-cleanup runs once + hooks signals), plus a live SIGTERM kill test. Full GUI +
hub board green.

---

## 5. Per-unit operational state machines

These formalize "what state each unit is in" so invariants are checkable. They are the OPERATING
states of each instrument as driven by its engine; they compose with the ownership lifecycle above.

### 5.1 RX analyzer (8565EC), driven by SpectrumEngine

```
OFFLINE ---acquire OK---> LEASED ---apply settings---> CONFIGURED ---arm+read---> SWEEPING
   ^  \                                                                             |
   |   \--acquire fail (lease/bridge)--> ABSENT(reason)                             |
   |                                                                                |
   +----------------------- suspend (release lease, RF n/a) <----------------------+
                                                                                    |
                          wedge (frozen sweep / ref-unlock codes) --> WEDGED <------+
```

- OFFLINE: engine suspended or not yet acquired.
- LEASED: bridge lease held (single consumer) but not yet configured.
- CONFIGURED: center/span/RBW/detector/sweep applied.
- SWEEPING: continuously arming + reading traces (the live feed).
- ABSENT(reason): acquire failed; reason surfaced (lease held by session N / bridge unreachable /
  FAULT power-cycle). This is the state the operator saw as "no feed"; it is now surfaced, never
  silent.
- WEDGED: fresh-sweep guard or reference-unlock error codes detected (see DEVICE_OPERATION_AUDIT and
  the 8565EC health gate); halt rather than measure garbage.

Observable today: `SpectrumEngine.suspended`, `_have_rx`, the applied settings (via
`read_state`/`_emit_state` -> CF/span/RBW/detector/errors), and `hub.state_snapshot()["rx"]`.

### 5.2 TX source (68367C), driven by SourceEngine

```
OFFLINE ---acquire OK---> LEASED ---RF off (default)---> RF_OFF
                                                            |
                                     set freq/level + RF on |
                                                            v
                                        SETTLING ---await_settled (OSB)---> LEVELED
                                            ^                                   |
                                            |            retune-while-hot       |
                                            +-----------------------------------+
                            suspend / stop / leave-mode ==> RF_OFF (safety invariant)
```

- RF_OFF: leased, output disabled (the power-on and leave-mode default; safety invariant).
- SETTLING: freq/level commanded, waiting for OSB leveled+locked + analog settle (POINT_SETTLE_S in
  Point Op). Reading the tone here reads it suppressed.
- LEVELED: OSB reports leveled+locked and the settle dwell elapsed; tone is trustworthy.
- Known hardware caveat (separate item): post-retune leveling near the 2.0 GHz band switch can
  degrade from <0.5 s to 2-3 s; OSB still reports leveled. Surfaced as a hardware difficulty, not a
  driving defect (see the source-settle finding). Awaits a power-cycle + cooldown to confirm.

Observable today: `SourceEngine.suspended`, `_have_tx`, and `read_state`/`_emit_state` -> OF1
(freq readback), OL1 (level), OSB (status byte: leveled/locked bits), plus `settled` events.

---

## 6. Invariants (enforced / recommended)

Enforced now (via `invariant_violations()`), [] = healthy:

- INV-1: for each instrument, the Arbiter owner (if any) is an engine belonging to the ACTIVE tab.
- INV-2: every non-active mode's engines are suspended.
- INV-3 (ownership set with lease not yet acquired = handoff in flight) is NOT flagged: it is an
  expected transient, so the checker keys on WRONG owner, not on the `_have_*` flag.

Recommended (OPEN):

- INV-4 (partially covered by F4): exactly one bridge session holds each pad while the bench runs,
  and it is released on exit (clean OR killed). SIGKILL still relies on the TTL; a startup stale-lease
  reclaim (force-preempt a provably-dead session) is the remaining gap -- a trust decision.
- INV-5: TX is in RF_OFF whenever no mode intends a tone (leave-mode / suspend). Partially covered by
  `rf_off_safe()` on suspend/stop; add an assertion.
- INV-6: RX is never reported as measuring while WEDGED (already gated in the coordinator health
  path; wire the same gate into the Point Op reading-validity chip).

---

## 7. Ranked remaining improvements

0. **(FIXED, R7/F6) The RX read railed BLANK -> Point Op "NO TONE".** The earlier "marginal 8565EC"
   read of this was WRONG (a probing artifact + the wedged/frozen false positive). Root cause, found
   deterministically: over the networked GPIB bridge `TS; DONE?` does NOT block for the new sweep, and
   `arm_and_wait` took two TS with NO dwell, so `read_trace()` got a PARTIAL trace. That was harmless
   while a stale prior sweep still held valid data -- but `configure()` and `set_max_hold(False)` both
   issue `CLRW TRA` on every apply, which CLEARS the trace, and it then never refilled -> a permanently
   BLANK trace railed at the bottom graticule (all 601 pts at ref-100, std 0). Isolation was clean:
   adding ONLY `CLRW TRA` to a working raw setup took it from 12/12 tone to 0/12; adding a real dwell
   after each TS took the CLRW case back to 12/12. Fix F6: `arm_and_wait` now dwells `>= ST? * 1.5`
   (floored at `_SWEEP_LIVE_DWELL_S=0.3`) after each TS so the sweep COMPLETES before the read (kept
   CONTS -- SNGLS reads stale over the bridge, and CONTS keeps the front panel live on release). Commit
   f14723d0 + regression test 121efd18. Live: bench Point Op now reads "peak -34.3 @ 2.4500 GHz, floor
   -77.7, TONE OK" every sample (was NO TONE / flat -110). Board green (600 passed). NOTE the "TX tone
   suppressed ~-73 dBm" observations were the SAME blank-read (the -73/-110 partial traces), not a real
   source-level problem: the source emits -30 dBm and reads back as a -34 dBm peak once the sweep
   completes. The remaining rare single-read miss right after a retune is the source ALC ramp
   (POINT_SETTLE_S), which the reading-validity streak already rides out.

1. **(OPEN, high) Watchdog surfacing.** Poll `state_report()` on the existing GUI timer; on a
   SUSTAINED violation (2 consecutive ticks, to ignore the handoff transient) show a status-strip
   warning + log. Turns the invariant checker from a test tool into an operator-visible guard.

2. **(OPEN, medium) Couple lease release to leave-mode, or make handoff explicit.** Today the hub
   holds both leases until `shutdown()` and hands off internally via the Arbiter; that is correct for
   the single-owner bench but means a leaked/killed process holds the pad until TTL. Consider an
   explicit `release_on_suspend` option or shortening the TTL for the bench role.

3. **(OPEN, medium) Collapse the 3-mechanism model.** With F1 (only the active mode drives), the
   Arbiter's cross-mode preemption is unused. Simplify to: BenchWindow owns "active index"; on a
   transition, atomically suspend outgoing (confirm released) then resume incoming (confirm
   acquired). Keep in-mode preemption only if an SE-preempts-manual case actually exists.

4. **(OPEN, low) Surface the per-unit state machine in the Point Op debug pane.** The pane shows raw
   OF1/OSB/CF/span already; add the derived state name (RF_OFF/SETTLING/LEVELED; SWEEPING/ABSENT/
   WEDGED) so the operator reads the state, not just the registers.

5. **(OPEN, low) Fold the R3 one-level limitation out** once #3 lands.

---

## 8. How to check state / reproduce

- Invariant + per-unit state (any running bench object): `bench.state_report()`.
- Live lifecycle reproduction + verification scripts used this pass are ad hoc (headless, offscreen,
  single consumer over `net:127.0.0.1:5555:18` / `:5556:5`): build the bench, `start()`, drive
  `tabs.setCurrentIndex(...)`, pump the Qt event loop, and read `state_report()` + the Point Op
  `rx_model.curve()` point count (601 = feed, 0 = no feed).
- Unit tests: `tests/test_bench.py::test_start_presuspends_every_mode_then_resumes_only_active`,
  `::test_bench_invariant_violations_detects_torn_ownership`,
  `::test_arbiter_thread_safe_under_concurrent_acquire_release`.

---

## 9. Changelog

- 2026-07-04: Initial audit. Reproduced the flaky no-PSD-feed live (2/3 launches). Root-caused to the
  startup ownership race (R1) + lock-free Arbiter (R2) + no invariant (R4). Applied F1 (pre-suspend
  before threads), F2 (RLock Arbiter), F3 (introspection + invariant checker). Live: 3/3 feed, 0
  invariant violations across tab switches. Documented the RX/TX operational state machines and the
  ranked OPEN improvements.
- 2026-07-04 (follow-up): Found a SECOND, independent cause of the same symptom -- an unclean exit
  strands the bridge lease for the TTL and blocks the next launch (R5). Refuted the immediate-switch
  connect-churn theory (immediate switch feeds 3/3 on a clean bridge). Applied F4 (SIGINT/SIGTERM +
  atexit lease release, hardened shutdown). Live: SIGTERM frees the lease immediately, relaunch feeds.
- 2026-07-04 ("tone does not appear in Point Op" audit): Root-caused to R6 -- SpectrumEngine ran the
  analyzer in ZERO SPAN because `configure()` (ends with `SP 0HZ`) was called AFTER `set_frequency`,
  wiping the span. Applied F5 (reorder set_frequency after configure in `SpectrumEngine._apply`);
  commit 2da2de03. Live-proven `SP?=0 -> SP?=5MHz`; direct read then shows the -34 dBm tone as a peak
  ~44 dB above the -78 dBm floor. Board green (599 passed). Refuted a "wedged/frozen" reading (a
  too-fast TS/DONE? sweep-alive false positive; the sweep is LIVE with a real dwell). SURFACED a
  remaining difficulty (item 7.0): through the threaded bench the RX read intermittently returns a
  blank railed trace (B) and the TX tone reads suppressed ~39 dB low (C) while every direct drive
  works -- consistent with heavy probing pushing the load-sensitive 8565EC marginal; needs an 8565EC
  power-cycle + one fresh launch on a rested unit to confirm before any further bench-engine change.
- 2026-07-04 (true root cause -- SUPERSEDES the line above): The RX rail was NOT a marginal analyzer;
  it was deterministic and software-caused (R7). `CLRW TRA` (issued by `configure()` +
  `set_max_hold(False)` every apply) clears the trace, and `arm_and_wait` read it back before any
  sweep refilled it because `TS; DONE?` does not block over the bridge and it had no dwell -> a blank
  railed read. Isolated by adding ONLY `CLRW TRA` to a working setup (12/12 -> 0/12) and a per-sweep
  dwell restoring it (0/12 -> 12/12). Applied F6 (dwell in `arm_and_wait`; commit f14723d0) +
  regression (121efd18). Live: bench Point Op reads TONE OK (-34.3 dBm over a -77.7 dBm floor) every
  sample. RETRACTED "marginal 8565EC / power-cycle" and "TX suppressed ~39 dB" -- both were the same
  partial-trace artifact; the unit was healthy (live sweep, ERR?=111) throughout. Board green (600).
