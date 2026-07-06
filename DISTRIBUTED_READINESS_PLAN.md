# se299 Distributed Readiness + Correction Plan

Status: ACTIVE (Wave 1 in flight). Created 2026-07-03. ASCII only.

Companion docs: CANONICAL_NETWORK_ARCHITECTURE.md (design), NETWORKED_OPERATION_SPEC.md (spec),
DISTRIBUTED_READINESS_PLAN.md (this -- the correction program to reach the target capability).

## Goal (the "consistent functionality" to achieve)

Multiple computers on one LAN, each connected to its own lab equipment, such that ANY client on the
network can run a coherent IEEE-299 substitution SE test over the network: discover the equipment,
take ATOMIC coherent exclusive control of instruments spread across hosts, observe live SE remotely,
and NEVER leave a transmitter keyed on a fault. Plus the single-machine GUI operator can run a
path-verified, persisted SE campaign.

## Verdict from two independent ensemble audits (2026-07-03)

- Course-correction audit: ON_COURSE_WITH_FIXES. Measurement core is correct (SE = ref - wall with
  TX held constant so it cancels, per-index RBW, SMP floors, EA8 dynamic-range gate, shield pause).
  Key correction: DO NOT build an operations.py action layer -- se-gui already holds a Coordinator
  exposing check_path/chain; the gap is GUI wiring, not missing/locked logic.
- Distributed-arch audit: UNSOUND today for multi-host any-client (canDoItToday = false). BUT the
  hard part is already right: per-point cross-host coherence is correct by construction (single-
  threaded sequential blocking round-trips; source settle read before the analyzer read; no wall-
  clock dependence, so latency only slows, never reorders), and the transport is host-agnostic
  (net:HOST:PORT:PAD local or remote, no code change). The gaps are in the distribution + safety +
  reachability layer -- fixable, not a wrong design.

## Definition of ready / consistent (acceptance criteria)

1. SAFETY: a client crash or client->source link partition CANNOT leave the source keyed -- the
   bridge de-keys the source on lease-drop / disconnect / keepalive lapse. Proven by test.
2. REACHABILITY: a client on host C reaches the analyzer on A and the source on B, and observes live
   SE, with no hand-editing of loopback binds.
3. DISCOVERY: `--discover` enumerates bridges on the LAN (a beacon actually runs).
4. COHERENCE: two-instrument control is atomic (all-or-nothing) across hosts; an asymmetric partition
   does not strand one instrument; a reconnecting client's held() reflects the bridge's truth
   (fence/epoch), and a dead peer does not block the next client for the full TTL.
5. CORRECTNESS: SE = ref - wall is protected across the calibrate->wall two-invocation flow (source
   identity + per-point level bound to the calibration).
6. TESTS: two-host integration tests (two separate bridge processes) assert 1-5.
7. GUI: an operator runs a path-verified (check_path pre-gate) and PERSISTED SE campaign from the GUI.

## Approach (how to tackle -- rationale)

- SEQUENTIAL WAVES, not concurrent edits. ni_gpib_server.py, coordinator.py, and cli.py are each
  touched by many items; concurrent agents editing one working tree race and break the board. Each
  wave is a sequential verify -> fix -> test -> full-board-green -> commit chain.
- Per item: reproduce the finding (cite file:line) BEFORE fixing; add a hardware-free test that FAILS
  before and PASSES after; keep the full board green; commit in a tight scoped batch.
- Order: SAFETY first, then REACHABILITY (unblocks all cross-host work), then COHERENCE/CORRECTNESS,
  then DISCOVERY (convenience) + HARDENING (DoS/auth), then the two-host ACCEPTANCE test layer.
- Two side tracks touch mostly-disjoint files and can run as their own waves: the SINGLETON
  provisioning fix (vm/provision) and the GUI course-correction (se_gui/drivers/loop). Exception:
  GUI P0-4 touches coordinator.py, so it follows Wave 1's atomic take_control change.
- HOLD the uncommitted Phase-2 singleton vm batch (cli.py/vm.py/provision.sh/test_vm.py/
  test_networked.py/NETWORKED_OPERATION_SPEC.md) until the live provisioning defect is fixed -- do
  not commit a bring-up that fails on real hardware.

## Work breakdown

Status key: [x] done + committed, [~] in flight, [ ] queued.

### Wave 1 -- safety + reachability (workflow wcgnd6c6u, sequential)
- [~] W1.1 SAFETY dead-man de-key: bridge sends a configured per-pad safe-state (RF-off) command on
      lease-drop / disconnect / keepalive lapse. Files: gpib_bridge/ni_gpib_server.py (+ args),
      tests/test_ni_gpib_server.py. Matters even single-machine (client crash leaves source keyed).
- [ ] W1.2 REACHABILITY routable bind: --vm can bind 0.0.0.0 (token-gated) + VmSpec.*_net_addr report
      LAN IP; default 127.0.0.1 unchanged. Files: gpib_bridge/vm.py, cli.py, tests/test_vm.py.
- [ ] W1.3 TELEMETRY bind-host: thread --telemetry-bind through CoordinatorRole into
      TelemetryHub(host=). Files: telemetry.py, roles.py, cli.py, tests.
- [ ] W1.4 ATOMIC checked take_control: all-or-nothing (adopt instrument_hub.acquire_both rollback);
      raise "TX controlled by <who>" before any bus op; callers stop discarding the result. Files:
      coordinator.py, cli.py, tests.

### Wave 2 -- coherence, discovery, correctness, hardening
- [ ] W2.1 Discovery beacon in the bridge (advertise routable host + pads) + harden discover()
      (bigger recv buffer, do not abort on first timeout, per-interface broadcast, routable-IP
      autodetect). Files: gpib_bridge/ni_gpib_server.py, discovery.py, tests.
- [ ] W2.2 Cross-host lease coherence: monotonic epoch returned on L/K and required on W/Q (reject
      stale); held() reflects bridge truth (demote on reconnect conflict); bind the two ControlLeases
      so losing/renew-failing one releases the sibling and aborts; keepalive gated on main-loop
      liveness; identity (u=) based ownership so a reconnecting client reclaims its lease; a takeover
      verb or shorter TTL so a dead peer does not block ~120 s. Files: ni_gpib_server.py, control_lease.py,
      coordinator.py, drivers.py, tests.
- [ ] W2.3 Substitution TX-identity/level guard: record source *IDN?/serial + per-point src power in
      the calibration; on the wall pass assert equality (or add src power to settings_key); optional
      live OL1 readback vs calibration. Files: loop.py, config.py, tests.
- [ ] W2.4 Bridge DoS/frame hardening: bounded readline, T-verb timeout cap, acquire worker slot
      before accept / refuse-fast, shorten idle reaping, graceful "! bad frame" (decode inside the
      per-verb try). Files: ni_gpib_server.py, protocol.py, tests.

### Two-host acceptance tests (the proof of "consistent across machines")
- [ ] TH.1 Spin up TWO separate bridge processes (analyzer + source) and assert: atomic dual-acquire;
      host-down rollback (no stuck lease past a short TTL); clean crash frees both AND de-keys the
      source; asymmetric partition does not permanently lock one host; two clients racing across hosts
      converge to exactly one controller. Files: tests/test_distributed.py (new).

### Singleton provisioning fix (side track -- unblocks the single-machine live function check)
- [ ] SV.1 Fix the section-4 two-board /etc/gpib.conf provisioning failure (provision.sh exits
      non-zero under set -e before the bridge service starts + before the readiness marker), and make
      await_guest_provisioned FAIL FAST on a provision error instead of a false 720 s timeout. Files:
      gpib_bridge/provision.sh, gpib_bridge/vm.py, tests/test_vm.py. Then re-run the live function
      check (both units respond on GPIB).

### GUI course-correction (side track -- from the first audit)
- [x] G.0 ControlLease.release() steal guard (3f9d9aee).
- [ ] G.1 P0-4: wire check_path (+ chain) into the se-gui campaign as a mandatory pre-Run gate via a
      uniform Coordinator control context (NO operations.py). Blocks the campaign on NO-COUPLING with
      a fault banner. Files: coordinator.py (a controlled() context), se_gui.py, tests. Follows W1.4.
- [ ] G.2 GUI campaign persistence: auto-write the SE table on completion via loop.write_run /
      write_calibration (se-gui currently discards reference/wall/se_figure). Files: se_gui.py, tests.
- [ ] G.3 set_detector normalization: route Agilent856xEC.set_detector through normalize_detector
      (drivers.py:931 writes raw DET -- a silent stale-detector trap). Files: drivers.py, tests.
- [ ] G.4 acquire_reference ambient bracketing: fold check_path's off/on/off reversibility into the
      reference floor/ref read so a mid-read ambient tone cannot inflate the 0 dB reference. Files:
      loop.py, tests.
- [ ] G.5 (later) Point-operation mode (large SE + PSD + arrow-key tune) -- AFTER G.3 (detector
      normalize is a prerequisite so a human label cannot corrupt point reads). Files: new mode, tests.

### Wave 3 -- auth hardening (needs the trust-model decision)
- [ ] W3.1 Auth/trust hardening for LAN exposure: TLS or mutual auth (token is cleartext + replayable
      today), observer-vs-controller capability split (a read-only credential cannot rf_on), signed /
      authenticated discovery beacons + do-not-send-token-to-unverified-host, admin force-preempt verb
      for a stuck/malicious BUS lease. Files: ni_gpib_server.py, drivers.py, discovery.py, protocol.py,
      docs. State + enforce the trusted-LAN boundary in CANONICAL_NETWORK_ARCHITECTURE.md.

## Open decisions

- TRUST MODEL (blocks Wave 3 scope): assumed TRUSTED ISOLATED LAB LAN -- require the token, forbid
  --insecure for any non-loopback bind, document the boundary. If it must be safe on a shared / Wi-Fi
  network, Wave 3 grows to full TLS + mutual auth + capability tokens. CONFIRM before Wave 3.

## Live-hardware notes

- Both NI adapters (GPIB-USB-HS + GPIB-USB-B) are present + ready. As of 2026-07-03 they were moved
  onto SEPARATE host USB controllers (HS -> EHCI hostbus 1, B -> XHCI hostbus 0), so the
  shared-controller reset race no longer applies (no _shared_controller_warning).
- PREMATURE-WEDGE FALSE POSITIVE (root cause of the golden live failures, fixed 2026-07-03):
  _poll_ready's R6 "early wedge verdict" fired the instant the QMP re-attach budget was spent
  (wedge_after_attempts=2, ~10-15 s), but a FRESH (reset=True) provision compiles ni_usb_gpib from
  source and only brings the board online + starts the bridge ~60-120 s in. Both golden guests
  reached "board 0 online" + "=== 5. bridge launcher ===" in their qemu.log at the exact moment the
  host declared them ADAPTER_WEDGED; the re-attaches were yanking the USB device out from under the
  guest driver (the "killed urb due to timeout"/"unexpected data" lines). It was NOT a hardware wedge
  and a physical replug would NOT have helped. Fix: gate the wedge verdict on the guest console
  (guest_boards_online reads qemu.log for the bridge-launch marker) + a wedge_grace_s window; before
  provisioning-complete the poller keeps waiting to the full timeout. Launcher per-unit timeout
  raised 240 -> 360 s so a cold compile has margin. Live re-run: run_live_two_client_e2e.py (golden).
- SHORT_READ FALSE POSITIVE (ni_gpib_server._status, fixed 2026-07-03): this guest's linux-gpib
  python build exposes ibsta()/ibcnt() but NOT iberr(). _status() called iberr() FIRST in one
  try/except returning (0,0,0) on failure, so the missing symbol ZEROED the real ibsta/ibcnt too;
  _read_is_short then saw ibsta=0 (END unset) and rejected EVERY valid reply as SHORT_READ. The
  8565EC answered "HP8565E,001,006,007,008" cleanly on a direct guest Gpib query but the bridge server
  discarded it -> reachable() always False -> analyzer wedged. Fix: _status fetches each field
  independently (missing iberr -> 0; the real ibsta END-bit + ibcnt survive). AFTER the fix the
  analyzer is reachable 5/5 over the network bridge (:5555). The guest runs ni_gpib_server.py directly
  from the read-only 9p mount (/opt/gpib_bridge), so a host edit + `systemctl restart ni-gpib-<role>`
  reloads it with NO VM rebuild.
- LIVE STATE 2026-07-03: 8565EC analyzer LIVE end-to-end through the full networked stack (ID? ->
  HP8565E,001,006,007,008). 68369A source ABSENT from the GPIB bus -- linux-gpib ENOL "no listeners
  currently addressed" at pad 5 on BOTH the HS bus and the analyzer's B bus; the source adapter itself
  is fine (interface_clear OK, USB settled). So the 68369A is powered off / uncabled / at a non-pad-5
  address; its bridge service correctly crash-loops "pad 5 not found on any board". The two-client E2E
  needs BOTH instruments -> BLOCKED on the 68369A being powered on + GPIB-cabled at pad 5. VMs left UP
  (se299-rx :5555, se299-tx :5556) so once the source answers the E2E runs with no rebuild.

## Status log

- 2026-07-03: two independent ensemble audits run (course-correction: ON_COURSE_WITH_FIXES;
  distributed-arch: UNSOUND today, core sound). Live function check attempted -> singleton bridge
  failed to provision (section-4 two-board gpib.conf). ControlLease steal-guard fixed + committed
  (3f9d9aee, board 487). Wave 1 correction ensemble launched (wcgnd6c6u).
