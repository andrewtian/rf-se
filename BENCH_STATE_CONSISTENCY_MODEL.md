# Bench State Operating Model + Consistency Gate Register (se299)

Canonical converged model and the tracking register for making the bench's live feed and control
**performant, correct, and fully consistent** -- i.e. the software's model of each instrument stays in
agreement with the device's ABSOLUTE (device-queried) state, with reliable command send/receive.

This doc is the single source of truth for this work product. It supersedes nothing; it makes the
prose per-unit operating states in `BENCH_LIFECYCLE_STATE_MACHINE.md` concrete and adds the missing
reconciliation axis. Governing hard constraints (from the operator): **NO breaking changes** (every
change is additive; existing signatures/behavior preserved) and **FULL verification** (hardware-free
board green + live confirmation at each gate).

Status legend: OPEN / IN-PROGRESS / VERIFIED (hardware-free) / LIVE-VERIFIED / DEFERRED.

---

## 1. Mission

Two GPIB instruments driven together over a networked qemu bridge:
- RX = HP/Agilent 8565EC spectrum analyzer (net 127.0.0.1:5555 pad 18)
- TX = Anritsu 68367C synthesized source (net 127.0.0.1:5556 pad 5)

The GUI must (a) run a fast live PSD feed, (b) never display a numerically wrong or stale trace, (c)
retune/level both units on operator control, and (d) at all times be able to answer "what is each device
ACTUALLY set to" from the device, and flag when that disagrees with what the model intends.

Primary goal restated as an invariant: **for every load-bearing field, DESIRED == ACTUAL within a
declared tolerance, or the disagreement is surfaced.** Feed performance target: keep the parked binary
feed at its established ~3 fps (span-independent); reconciliation adds ~0 fps cost.

---

## 2. Audit findings (Phase 0, four parallel audits)

Root finding, on which all four audits agree: **the bench has a solid connection state machine, a solid
ownership/lease layer with an invariant watchdog, and per-op device-driving correctness -- but the entire
RECONCILIATION axis is absent.** Every state-changing write is fire-and-forget; the "instrument truth"
panes show cached intent (RX literally "no bus read"); nothing compares device readback to intent.

### 2.1 Analyzer (RX)
- Zero state-changing writes are read back/verified. No absolute-state snapshot method exists.
- Fields with NO device readback used today: CF, SP, RBW, VBW, RL, AT, DET, AUNITS, LG/scale,
  CONTS/SNGLS, TDF, VAVG. Only `ST?`/`FA?`/`FB?`/`ERR?`/`STB?`/`PSDAC?`/`MKA?`/`MKF?` are queried, all
  for function (dwell/axis/marker), never for verification.
- The programming manual (`reference/operator-manuals/hp-8560-e-series-programming.md`) CONFIRMS the
  interrogate form for every load-bearing field: `CF? SP? FA? FB? RB? VB? RL? AT? DET? AUNITS? LG? ST?`.
  So an absolute-state read IS buildable. (`TDF` and `SNGLS/CONTS` have no query form but the driver sets
  them fresh on every read, so they need no reconciliation.)
- Correctness hazard: the TDF-B binary MU->dBm cal `(a,b)` encodes RL and dB/div at fit time and is
  cleared ONLY by `configure()`; `configure()` never writes `LG`. An out-of-band RL or LG change
  mis-offsets / mis-slopes every parked binary point with no cross-check.

### 2.2 Source (TX)
- freq (`OF1`), level (`OL1`), leveled/locked (`OSB` raw byte) are all queryable and the GUI engine
  already reads them -- but display-only, never reconciled against the commanded `SourceModel`.
- `settled_ok()` checks OSB bit2 (unleveled) + bit3 (lock-error) but IGNORES bit5 (syntax error): a
  silently-rejected command is undetected.
- RF ON/OFF has NO source-side truth; `RF1`/`RF0` are fire-and-forget and OSB=0x00 does not prove RF is
  present. RF-present is confirmable only downstream by the analyzer measuring the tone.
- Device quirks correctly handled: `CF1 <GHz> GH` CW rule (not `CW1`), `*IDN?`/`*OPC?` poison-and-
  reconnect, raw-`OSB` decode. Good.

### 2.3 Feed correctness + performance
- One parked frame (~0.33 s -> ~3 fps): drain/conflate queue -> `arm_and_wait` (`ST?`, `CONTS`, `TS`,
  `DONE?`, sleep dwell ~0.12 s) -> binary `read_trace` (`TDF B`, `TRA?` ~130 ms, `TDF P`, `FA?`, `FB?`)
  -> keep-last publish. `DONE?` does not block over the bridge; the dwell is the completion guarantee.
- Ranked correctness gaps:
  1. Stale binary cal on RL/LG change without `configure()` -> WRONG amplitude (whole trace). Primary
     wrong-trace path.
  2. Fresh-sweep verify is time-based (dwell on `ST?`), not proven by a readback.
  3. **Preselector peak leaves RBW at 300 kHz un-restored** -- `step_once` restores only frequency, so
     the parked feed then sweeps at 300 kHz RBW while the readout says "auto". Fires on every high-band
     Point Op retune. Concrete bug.
  4. **ASCII read has no 601-point guard** (only the binary path guards count) -> a truncated-but-
     nonempty ASCII read is published as a short trace stretched over `FA?..FB?` = wrong-frequency PSD.
  5. Binary-cal engagement intermittent (~3/4). Safe (falls back to ASCII, never wrong amplitude) but
     the fps floor is intermittent.
- Performance hooks (measured-implied):
  - Per-tick full-state query (~8 x 20 ms) = +160 ms/tick -> ~2 fps. NOT worth it.
  - On-change verify folded into the already-~2-3 s apply/calibrate tick = <5 % overhead, invisible.
  - **Sweet spot: piggyback the existing throttled `read_state` tick (~1 Hz, already reads `ERR?`)** and
    add `RL?`/`LG?`/`RB?` there -> ~1 Hz consistency probe at ~0 fps cost, catches stale-cache drift
    within ~1 s.
  - Free win: `FA?`/`FB?` are re-read every parked tick though CF/SP only change on apply -> cache
    on-change, drop ~40 ms/tick.
- Control-accept: RX has NO accept confirmation (dwell/time-based); TX confirms via OSB. Point Op has an
  emergent end-to-end check (`reading_status` gates tone-above-floor AND on-frequency); plain SA has none.

### 2.4 State model + transport + reconciliation
Prioritized gap list (the spine of this work product):

| # | Gap | Partial today? |
|---|---|---|
| G1 | Canonical per-device state record (link + operating state + desired + last-verified-actual) | Link-state formal; operating states are PROSE-only in `BENCH_LIFECYCLE_STATE_MACHINE.md`; desired settings duplicated across per-mode models + engine |
| G2 | Write-then-readback verify discipline | TX verifies leveled/locked + not-wedged only; no CF/SP/RBW (RX) or OF1==commanded (TX) verify |
| G3 | Absolute-state query per device | TX partial (queries device, display-only); **RX none** (echoes applied settings) |
| G4 | Reconciliation cadence (startup/apply/reconnect/periodic) | NONE for state |
| G5 | Model-vs-device drift detection + surfacing | Only ownership/wedge/measurement drift surfaced; no state drift |
| G6 | Transport reply correlation + stale/reorder detection + general retry | Positional pairing via strict serialization; ENOL-only retry; binary-length guard only |
| G7 | Startup stale-lease reclaim (SIGKILL leaks lease until 120 s TTL) | Clean/SIGINT/SIGTERM/atexit release only |

---

## 3. The converged state operating model

Each device has ONE canonical **DeviceState** record with three layers per field:
- **DESIRED** -- operator/model intent.
- **COMMANDED** -- the last value written to the device.
- **ACTUAL** -- the last value read back FROM the device (device truth), with a timestamp.

Consistency := for each load-bearing field, `ACTUAL` agrees with `DESIRED` within the field tolerance.
Drift := disagreement -> surfaced, and (for amplitude fields) triggers a cal-cache clear.

### 3.1 Canonical state schema + query commands + tolerance

RX (8565EC):

| Field | Desired source | Query | Tolerance / note |
|---|---|---|---|
| center_hz | model.freq/span | `CF?` | max(1 Hz, 1e-6 * f) |
| span_hz | model.span | `SP?` | max(1 Hz, 1e-6 * span) |
| rbw_hz | AUTO or set | `RB?` | if DESIRED=AUTO, record ACTUAL, never flag; else exact |
| vbw_hz | AUTO or set | `VB?` | as RBW |
| ref_level_dbm | configure RL | `RL?` | 0.05 dB; **drift here clears the binary cal cache** |
| atten_db | AUTO or set | `AT?` | exact (dB); enforce min-atten floor |
| detector | model | `DET?` | enum equal |
| scale_db_div | log 1/2/5/10 | `LG?` | exact; 0 => linear mode; **drift clears cal cache** |
| aunits | DBM | `AUNITS?` | enum equal (expect DBM) |
| sweep_time_s | AUTO or set | `ST?` | informational (already read for dwell) |

TX (68367C):

| Field | Desired source | Query | Tolerance / note |
|---|---|---|---|
| freq_hz | model.freq | `OF1` (MHz register) | max(1 kHz, 1e-6 * f) |
| level_dbm | model.power | `OL1` (dBm register) | 0.1 dB |
| leveled | (status) | `OSB` bit2 clear | boolean |
| locked | (status) | `OSB` bit3 clear | boolean |
| syntax_ok | (status) | `OSB` bit5 clear | boolean; NEW check (rejected-command detector) |
| rf_commanded | model.rf_on | (no device query) | see 3.2 |

`OF1`/`OL1` read the F1/L1 registers set at command-parse, so they confirm **command acceptance**, not
physical emission -- exactly what verify needs.

### 3.2 RF-on truth (structural)

The source cannot confirm RF presence. Model it as two fields: `rf_commanded` (what we wrote) and
`rf_confirmed` (analyzer sees tone above floor at the commanded freq). `rf_confirmed` is owned by the
analyzer read path (Point Op `reading_status` already computes tone-above-floor AND on-frequency). The
state pane shows both; a `rf_commanded=ON` with `rf_confirmed=False` is the annunciated inconsistency.

### 3.3 Reconciliation cadence (the perf-safe schedule)

- **On apply / settings-change tick** (already ~2-3 s): after `configure()`, fold an absolute-state read
  and confirm CF/SP/RL/DET landed (control-accept confirmation). Stamp RL + LG onto the cal cache so
  later drift is detectable. <5 % overhead on a tick that already freezes a sweep.
- **Periodic ~1 Hz** (piggyback the existing throttled `read_state`/`_emit_state` tick that already reads
  `ERR?`): read RX `RL?`/`LG?`/`RB?` (+ TX `OF1`/`OSB`) and compare to DESIRED. On RL/LG drift: clear the
  binary cal cache (forces recalibration -> correct amplitude) and annunciate. ~0 fps cost.
- **On reconnect / preemption handoff**: re-read absolute state before trusting the next trace.
- The parked binary hot path is otherwise untouched (except the FA?/FB? on-change cache free-win).

### 3.4 Command send/receive discipline

- Every state-changing write remains a normal write; VERIFY happens at the reconciliation points above
  (not per command -> preserves fps).
- Add the OSB bit5 (syntax) check to `settled_ok` semantics via a new `syntax_ok`/`command_rejected`
  signal so a rejected source command is detectable (does not change existing `settled_ok` callers).
- Transport (G6): full reply-correlation IDs are a large, risky change to the load-bearing single-socket
  serialization and are **DEFERRED** (see 5). This pass adds only cheap, non-breaking **sanity guards**
  on scalar replies (a `CF?` reply must parse as a number in the plausible range; `OSB` must be one
  byte), so a framing desync is caught rather than ingested as truth.

---

## 4. Gate register (tracking core)

Each gate is additive, independently testable, and regression-checked. "Board" = the hardware-free suite
(command in section 6). Live verification is gated on a healthy bench.

| Gate | Deliverable | Maps to | Acceptance (hardware-free) | Live acceptance | Status |
|---|---|---|---|---|---|
| C1 | RX `read_state()` on `Agilent856xEC`: query CF?/SP?/RB?/VB?/RL?/AT?/DET?/AUNITS?/LG?/ST? -> `AnalyzerState`; sim returns its configured state | G3 | fake-transport test parses each query; sim snapshot matches configured | live `read_state()` returns plausible values matching a known configure | VERIFIED (hw-free); LIVE pending |
| C2 | TX `read_state()` on `Anritsu68369`: aggregate OF1/OL1/OSB -> `SourceState` incl. `syntax_ok` (bit5) | G3, G2 | fake-transport test; bit5 surfaced | live snapshot matches a known set_freq/set_power | VERIFIED (hw-free); LIVE pending |
| C3 | Canonical `AnalyzerState`/`SourceState` records (device_state.py) | G1 | unit test of the records + reconciliation | n/a | VERIFIED (hw-free) |
| C4 | Reconciliation compare (DESIRED vs ACTUAL, per-field tolerance incl. AUTO handling) + `Drift` result | G4, G5 | unit test: matching -> no drift; injected mismatch -> drift on the right field | n/a | VERIFIED (hw-free) |
| C5 | Amplitude-drift -> clear binary cal cache (`invalidate_calibration`) | G5, feed#1 | test: RL/scale drift clears cache -> next read recalibrates (never wrong amplitude) | live: change RL out-of-band -> feed recalibrates, no wrong offset | VERIFIED (hw-free); LIVE pending |
| C6 | Feed bug: restore `RB AUTO` after `peak_preselector` (`set_resolution_bandwidth`; frequency + RBW both restored) | feed#3 | test: after preselector peak, parked read re-asserts AUTO RBW, not 300 kHz | live high-band retune: RBW returns to auto | VERIFIED (hw-free); LIVE pending |
| C7 | Feed bug: 601-point guard on the ASCII path (truncated ASCII -> raise, not publish short; empty=keep-last) | feed#4 | test: short ASCII read raises; empty -> ([],[]); full 601 ok | live: n/a (defensive) | VERIFIED (hw-free) |
| C8 | Reconciliation wired at ~1 Hz (piggyback throttled `_emit_state` tick) -- reads absolute state, compares to intent, publishes drift | G4, perf | test: reconcile runs on the throttled tick, drift published, amplitude drift clears cal | live: fps unchanged (~3 parked); drift annunciates within ~1 s | VERIFIED (hw-free); LIVE pending |
| C9 | State pane surfaces the model-vs-device drift annunciation (rf_confirmed already = Point Op `reading_status`) | G5, RF-truth | offscreen test: drift renders; no drift -> no annunciation | live: eyeball the pane during a retune | VERIFIED (hw-free); LIVE pending |
| C10 | E2E: model==device across a control sweep + feed; full board green; regression check | all | full board green (690 passed) | live e2e: reconcile clean on both units, injected out-of-band RL drift caught | VERIFIED LIVE 2026-07-05 (real 8565EC + 68367C) |

FA?/FB? on-change caching (a per-tick ~40 ms free perf win noted in the audit) is a separate, optional
optimization deferred out of this pass -- it touches the hot binary read path and is a pure speed win, not
a consistency requirement; recorded here so it is not lost.

Deferred (documented, out of this pass -- see 5): G6 full reply-correlation redesign; G7 startup
stale-lease reclaim.

---

## 5. Deferred with rationale (no-breaking-change boundary)

- **G6 transport correlation IDs.** The bus is a single TCP socket with strict one-in-flight
  serialization; correctness today rests on "never pipeline." Adding sequence/correlation tags touches
  the load-bearing `_txn`/keepalive path and the bridge protocol -- high blast radius, real risk of a
  breaking change to every consumer. This pass adds cheap scalar sanity guards (C-series) that catch a
  desync without a protocol change. A full correlated-transport redesign is a separate, spec-first work
  item.
- **G7 startup stale-lease reclaim.** Lease lifecycle, not device-state consistency; a SIGKILL leaks the
  lease until the 120 s TTL. Tracked in `BENCH_LIFECYCLE_STATE_MACHINE.md` (INV-4). Not required for
  model==device consistency and left to the lease work stream.

---

## 6. Verification protocol (binding)

- **No breaking changes:** every gate is additive -- new methods (`read_state`), a new record, a new
  reconciliation step, new optional signals. No existing method signature or behavior changes. Each gate
  re-runs the covering tests AND the full hardware-free board.
- **Hardware-free board (regression gate), run after every gate:**
  `QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest rf-se/se299/tests/ -q -n0
  --ignore=tests/test_e2e_live.py --ignore=tests/test_e2e_coupling_live.py
  --ignore=tests/test_e2e_single_unit_live.py --ignore=tests/test_defect_8565ec_reference_live.py`
- **Live verification (gated on a healthy bench):** single consumer, RF off on exit, source capped. Per
  gate: run `read_state()` and confirm it matches a known configure; inject an out-of-band RL change and
  confirm the feed recalibrates (no wrong amplitude); confirm parked fps stays ~3; confirm a retune
  reconciles clean and an injected drift is annunciated. If the analyzer is wedged, HALT and surface --
  do not measure through it.

---

## 7a. Lease/lock acquisition attribution (task #43)

When the bench is waiting on or cannot take RX/TX, it must say WHY and WHAT holds it. The lease-conflict
case was already handled (`SingleConsumerConflict` carries the bridge lease table); the live C10 attempt
exposed the gap -- a WEDGED adapter surfaced only as a bare `timed out`, indistinguishable from "held by
someone". `lease_diagnostics.py` closes it with a four-way classifier:

- `bridge-down` -- the bridge TCP port refuses connections -> start/restart the bridge VM.
- `adapter-wedged` -- TCP open but the bridge/board transaction hangs -> physically replug the adapter
  (software recovery does NOT clear the FX2LP firmware wedge).
- `leased` -- another consumer holds the device lease -> named holder (from the lease table); stop it/wait.
- `board-busy` -- bridge up, unleased, but the board did not answer this attempt -> retry.

`classify()` is a pure decision table; `diagnose(host, port, label, lease_report_fn=...)` composes a TCP
probe + the lease-table read; wired into `lease_exclusive` so a real-transport conflict now carries the
classified attribution + holder. Hardware-free tests in test_lease_diagnostics.py (13). GUI hub
surfacing + live confirmation are live-gated (same as C10).

## 7. Changelog

- 2026-07-05: Created. Phase 0 (four audits) complete; converged model + gate register C1-C10 defined.
  G6/G7 deferred with rationale.
- 2026-07-05: C1-C4 landed (commit 0ca66fa6) -- device_state.py (records + reconciliation), read_state()
  on all four drivers, sim state tracking. Additive; full board 669 passed.
- 2026-07-05: C5-C9 landed -- invalidate_calibration + amplitude-drift cache clear, set_resolution_
  bandwidth RBW-restore after preselector, ASCII 601-guard, reconciliation wired into the throttled
  _emit_state tick, drift annunciation in the RX state pane. Additive; hardware-free verified.
- 2026-07-05: C10 live attempt BLOCKED. Bridge VM up + ports listening + RX adapter enumerated
  (3923:702a), but the instrument connection times out and `gpib_config --minor 0` returns "failed to
  configure board / Connection timed out" -- the NI GPIB-USB-B FX2LP firmware wedge. Software recovery
  (gpib_config + service restart) does NOT clear it (confirmed); a PHYSICAL replug of the RX adapter is
  required. C10 will run once the bench is healthy. All logic is proven hardware-free (677 passed); C10
  only re-confirms it on the metal.
- 2026-07-05: after the physical replug, C10 STILL blocked -- diagnosis via the host qemu QMP monitors:
  the TX/source adapter (GPIB-USB-HS 3923:709b) is present + attached to its VM, but the RX/analyzer
  adapter (GPIB-USB-B 3923:702a) is ABSENT from the host USB bus entirely (qemu `info usbhost` shows only
  709b + unrelated devices; the RX passthrough is pinned to hostbus=0/hostport=1.4 where nothing NI now
  sits). The replug did not re-enumerate the RX adapter on the host, so there is nothing for qemu to
  re-attach. Needs re-seating / a full power cycle of the RX adapter (or the fxload firmware download to
  bring it up as 702a). An independent background audit of software/virtual-replug recovery (qemu QMP
  device_del/device_add) is running. Task #43 lease_diagnostics + attribution landed meanwhile.
- 2026-07-05: C10 VERIFIED LIVE. Recovery sequence that worked: user replugged the RX/B into the SAME
  host port -> host re-enumerated it cold and fxload brought it to 3923:702a at hostbus=0/hostport=1.4
  (confirmed via ioreg idProduct 28714) -> the running qemu still showed the guest placeholder, so a QMP
  `device_del ni_b` + `device_add usb-host,bus=xhci.0,vendorid=0x3923,hostbus=0,hostport=1.4,id=ni_b` on
  ~/.se299-vm/se299-rx/qmp.sock handed the guest the real device (guest lsusb: GPIB-USB-B; dmesg: attached
  to gpib0) -> `gpib_config --minor 0` + analyzer-bridge restart -> the 8565EC answered. C10 probe then
  passed: BEFORE-lease holder attribution read clean (no active leases); RX healthy (sweep LIVE, no ref
  codes); RX read_state == known configure (CF 2.45G / SP 5M / RL -10 / AT 20 / POS / 10 dB-div) reconcile
  CLEAN; TX read_state (2.45G / -10 dBm / leveled / locked / syntax-ok) reconcile CLEAN; injected
  out-of-band RL -5 (desired -10) CAUGHT as amplitude drift -> would clear the cal cache. Leases released
  + RF off on exit. This confirms the audit's two-tier recovery doctrine on the metal: the QMP virtual
  re-attach is the software step; the physical same-port replug (true VBUS cycle) was the prerequisite the
  hard FX2 wedge required. C1-C10 all VERIFIED (C1-C9 hw-free, C10 live). Phase 5 complete.
