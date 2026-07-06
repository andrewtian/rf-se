# Master Spec -- Networked, Multi-Instance Operation of the 8565EC + 68369A

Status: PHASE 1 COMPLETE (canonical feature set + architecture determined by the elite
ensemble); PHASE 2 (implementation) IN PROGRESS. Iterated across passes until the
deliverable is thoroughly produced. Tracks scope, feature set, architecture, plan, tests,
and state.

## 1. Intent (parsed from the user's directive -- recursed for full understanding)

Build the canonical, end-to-end networked control system for the two SE-campaign
instruments -- the HP/Agilent **8565EC** (RX / spectrum analyzer) and the Anritsu
**68369A** (TX / signal generator):

- **R1. Both instruments network-based** -- each reachable + controllable over the network.
- **R2. Multiple GUI instances, role-based** -- several instances at once, each a role.
- **R3. Elite networking with broadcast / discovery** -- instances auto-discover instrument
  services + peer roles; no hand-typed addresses.
- **R4. Core SE testing preserved** -- the substitution measurement (RX+TX coordinated),
  source-tracks-sweep, and the se299 measurement-integrity guardrails.
- **R5. Instanceable + testable** -- multiple instances, each connecting to the hardware
  attached to the host.
- **R6. E2E impl with valid tests; NETWORKING TESTS MUST BE PRESENT** -- multi-instance,
  discovery, concurrent operation, on the live network (+ hardware-free where possible).
- **R7. Elite/SOTA architecture**, delivered through this spec, iterated to completion.
- **R8. LIVE SE figure during concurrent operation** -- the SE coordinator KNOWS and
  displays the shielding-effectiveness figure SE(f) in REAL TIME while BOTH units operate
  concurrently and validly. Per-point `SE(f) = reference(f) - wall(f)` (against a stored/prior
  reference) is computed and shown as the coordinated source-tracks-sweep runs, with every
  validity invariant (I1-I8) holding -- the SE figure is continuously available DURING
  operation, not only at campaign end. A point within margin of the floor shows `SE >= X`
  (lower bound); the running worst-case SE + `campaign_pass`-so-far update live.

Non-negotiable (carried): NO fake/mock in the runtime (fakes are TEST DOUBLES only); honest
failure when hardware is absent; reuse the `net:` transport + the existing `LiveSpectrumGUI`.

## 2. Current architecture + gaps

Have: `drivers` (Agilent856xEC RX + Anritsu68369 TX over VisaTransport/NetworkTransport;
`make_transport("net:HOST:PORT:PAD")`); `gpib_bridge/ni_gpib_server` (ONE client at a time,
ONE pad per connection; protocol A/W/Q/T/C/H; token/loopback auth); `connection.AnalyzerLink`
(analyzer only); `probe_sweep.ProbeSweeper`; `live` (LiveSpectrumModel/GUI); `loop` (SE
substitution, `tracked_sweep`, `require_source_tracked`, affirmative `summarize`, PC8).
Gaps: server single-client (2nd instance blocks in the kernel backlog) + no bus arbitration;
no source lifecycle (`SourceLink`); no discovery/broadcast; GUI is analyzer-only (no source /
coordinator / dashboard roles); no multi-instance/discovery tests.

## 3. Canonical feature set (ensemble determination)

### Roles (each a launchable GUI instance)
- **A. Analyzer view (RX/8565EC) [REQ]** -- reuse `LiveSpectrumGUI`. Live spectrum + hot-bin
  marker + link/identity readout; two data sources behind the SAME `SweepFrame` contract:
  **OBSERVER** (subscribes to the coordinator's published stream, issues ZERO bus writes when
  a coordinator holds the analyzer lease) and **STANDALONE** near-field sweeper (`ProbeSweeper`,
  takes the analyzer lease itself when no coordinator is active).
- **B. Source control (TX/68369A) [REQ]** -- set CW freq/power (power clamped to a safety
  max), RF ON/OFF with a prominent RF-ON indicator, list/step sweep (`set_list_sweep`/
  `arm_sweep`/`trigger_point`), IDN + `SourceLink` status. **Lease interlock**: while the
  coordinator holds the TX write-lease, manual controls are observe-only.
- **C. SE-coordinator [REQ]** -- the ONLY role that owns BOTH transports. Campaign config
  (`config.Campaign`), phase (reference/wall), tracking mode; runs `acquire_reference` then
  `measure_wall`; renders `summarize` (SE(f), capability/EA8 floor, verdicts, `floor_limited`,
  `campaign_pass`); settings_key match indicator; holds the exclusive both-instrument lease;
  publishes the per-point/trace stream to observers; writes the run (PC8). **LIVE SE figure
  (R8)**: as the coordinated sweep runs (both units operating), each completed point emits
  `SE(f)=reference(f)-wall(f)` (stored/prior reference minus the live wall read); the GUI
  updates the SE(f) curve, running worst-case SE, per-point verdict, and `campaign_pass`-so-
  far in real time -- the SE figure is known DURING operation, honoring floor-limited lower
  bounds and the `source_tracked` gate.
- **R9. Modular LIVE CONTROL PLANE for ANY TX/RX units.** The architecture operates on
  ABSTRACT capability-typed units (any RX/analyzer, any TX/source discovered on the network),
  not hardcoded to the 8565EC/68369A. A control plane (discovery + capability registry +
  arbitration + coordination) is SEPARATE from the data plane (the per-instrument GPIB
  transactions); it tracks the LIVE roster of units (join/leave, role, capabilities, lease,
  health) and lets any role resolve + operate a TX/RX by capability. New instrument models
  plug in via a driver registry with zero coordinator change.
- **D. Discovery / dashboard [REQ]** -- enumerate instrument services on the LAN (analyzer +
  source), show role token / IDN / health / which client holds the coordinator lease + the
  live roster of role instances; launch buttons.
- **E. Localize / leak-hunt [REC]** -- a coordinator sub-mode over `loop.localize`.

### Networked service model
ONE instrument SERVICE per physical bus (in the SE topology the source is OUTSIDE and the
analyzer INSIDE over fiber per PC3 -> TWO services; on a single-adapter bench -> one service
serving two pads). A service owns its `/dev/gpib0`, serves MANY sessions, binds a pad per
session (`A`), and arbitrates the bus (below). The coordinator opens a transport per service.

### Discovery/broadcast (zero-dependency stdlib UDP multicast beacon)
NOT zeroconf (its hard-coded 224.0.0.251:5353 collides across parallel xdist workers + the
macOS mDNSResponder). A stdlib UDP beacon on an admin-scoped 239.255/16 group with
**injectable group/port** (xdist-safe), `IP_MULTICAST_LOOP=1` (services + GUIs share a host),
periodic ANNOUNCE + directed unicast QUERY->ANNOUNCE (the deterministic test path). One JSON
datagram per record: `{schema, kind: instrument|role, instance_id, host, ttl_s, ...}`;
instrument record carries `instrument_kind` (analyzer|source), model/serial/options,
port/gpib_address, **`transport_addr` = the ready `net:HOST:PORT:PAD` string** (browse->connect
is a pass-through through `parse_net_addr`/`make_transport`), freq envelope, capabilities, and
`auth: none|token` (advertises that auth is required, NEVER the secret). `zeroconf` is an
optional adapter behind the same `Discoverer` interface for real-LXI interop.

## 4. Architecture

### Concurrency + arbitration (two layers, both required)
- **Thread-per-connection** replaces the sequential accept loop (the real multi-instance
  blocker). Each connection = an independent session with its own bound pad.
- **Shared-board refactor**: one `Board` owns `/dev/gpib0` + a `threading.Lock` (the bus
  mutex) + one device handle per pad; `bind()` stops doing a bus-wide `interface_clear` per
  call. A per-session view forwards write/query under the mutex.
- **(a) Per-transaction bus mutex** (always on): every W/Q runs under the board lock so a
  query's write-then-read is atomic vs any other session -- the safety floor.
- **(b) Application lock/lease** (VXI-11 `device_lock` analogue): scope a device pad OR `BUS`
  (whole bus -- required for the coordinated RX+TX pass), mode EXCL; grant returns a lease
  token + TTL; held-by-another -> fail-fast `! locked <holder-role>` (optional bounded wait);
  auto-release on TCP disconnect OR TTL expiry (crash-safe); renew on any op + explicit
  keepalive. Controller may W/Q; while an EXCL lease is held, non-holders' W (and Q on the
  locked device) are refused -> observers render the published stream instead.
- **Pub/sub fan-out** [phase]: the lease holder's reads are published to observer sessions.

### Protocol additions (additive; A/W/Q/T/C/H preserved)
`L <scope> <mode> <ttl_ms> [wait_ms]` (acquire -> `= <token>` | `! locked ...`);
`U [token]` (release; also implicit on C/disconnect); `K [token]` (keepalive/renew);
`R <role> <client-id>` (role/identity hello; ties a shared BUS lease to a client-id).
Lease is connection-scoped; a shared client-id across the coordinator's two connections lets a
BUS lock on one authorize the other. Nice-to-have: `Wa/Qa` (address-per-request multiplexing).

### Client + coordination
Two `NetworkTransport`s (one per service/pad); the real drivers ride unchanged.
`NetworkTransport.lock/unlock/renew` + a shared `client_id` (lock lives on the transport, NOT
the SCPI driver). `SourceLink` mirrors `AnalyzerLink` for the 68369 (ExpectedSource
model_token "68369"/family "683", discover/open/validate/auto-reconnect). A `Coordinator`
owns `(SourceLink, AnalyzerLink)` + both leases, runs the existing loop, publishes the stream.

### Control plane (R9): capability-typed, plugin-modular, live roster
Separate the **control plane** (discovery + registry + arbitration + coordination + live
state) from the **data plane** (per-instrument GPIB transactions). Canonical functionality:
- **Capability contract**: two abstract roles -- **RX** (analyzer: sweep / zero-span / marker /
  floor; the existing `SpectrumAnalyzer` interface) and **TX** (source: CW / RF on-off /
  list-sweep; the existing `SignalGenerator` interface). Any unit satisfying the contract is
  operable; the SE core already depends only on these interfaces, never on a concrete model.
- **Driver registry (plugin)**: `register_driver(idn_pattern, driver_class)` / `resolve_driver
  (idn) -> class`. Seed entries: `8565`/`856x` -> `Agilent856xEC` (RX), `68369`/`683` ->
  `Anritsu68369` (TX). A new analyzer/source model plugs in by registering a driver -- ZERO
  coordinator change.
- **Networked instrument handle**: `(discovered record) -> NetworkTransport -> resolved driver`
  exposing the RX or TX capability; the control plane builds handles from discovery records.
- **ControlPlane object (live)**: consumes the discovery beacon; maintains the live roster of
  units (kind/capabilities/role/lease-holder/health, updated as units join/leave/lease); offers
  `available_rx()` / `available_tx()` / `resolve(kind|role|instance_id) -> handle`. Roles and the
  coordinator ask the control plane for units by CAPABILITY, not address.
- **Modular coordinator**: composes ANY `(TX, RX)` pair the control plane resolves and runs the
  existing SE loop over them -- swap either instrument (any registered model) and it works,
  because the loop and the GUIs speak only the abstract RX/TX contract.

### RX+TX coordination for a VALID networked SE result (integrity)
The per-point handshake IS `acquire_reference`/`measure_wall` -- unchanged: `set_power`->
`set_freq` (source) -> settle -> `measure_peak` (analyzer: CF/TS/DONE?/MKPK HI). Both bridge
ops are synchronous request/reply, so each ACK/`=` is a happens-before barrier; the coordinator
sequences them single-threaded per point. **I7 barrier**: analyzer_read(i) happens-after
source_set+ACK+settle(i); FORBID any pipelined read that races/uses a stale trace. Every
published point carries `(campaign_id, phase, point_index, f_hz, src_freq, src_rf_on)` to prove
correspondence. Network latency only lengthens `settle_s`; it never corrupts the ratio. PC4:
independent disciplined refs (GPSDO out / OCXO-Rb in), frequency accuracy not phase; assert/log
`|f_src - f_center| < RBW/2`; `MKPK HI` absorbs the residual offset within the RBW bin.

### Measurement-integrity invariants (MUST hold over the network)
I1 NO-FAKE (real published frames or honest stop). I2 SETTINGS-KEY (ref==wall or refuse).
I3 FLOOR-LIMITED -> lower bound only. I4 SOURCE_TRACKED gates any verdict. I5 EA8/PC6 floor
below target. I6 SINGLE-WRITER LEASE during a pass. I7 HAPPENS-BEFORE barrier + point-token.
I8 PC3/PC4 recorded (fiber-only analyzer path + independent timebases; freq offset logged).

## 5. Implementation plan (ordered, smallest safe, each hardware-free + green)

1. **Server concurrency + bus mutex + per-pad fake.** Thread-per-connection; shared `Board`
   + mutex; `bind()` no bus-wide `interface_clear`; `FakeBackend` answers per bound pad
   (pad 5 -> 68369 IDN, pad 18 -> 8565 IDN). Test: 2 concurrent `NetworkTransport`s (pad 5,
   pad 18) operate without corruption; per-pad IDN; existing tests still pass.
2. **`SourceLink`** (mirror `AnalyzerLink` for the 68369) + `ExpectedSource`. Test: discover
   -> validate -> READY -> auto-reconnect against `sim_inventory`'s 68369.
3. **Lease/lock core** (`L/U/K/R` verbs + TTL + release-on-disconnect + controller/observer
   gating). Test: grant, `! locked` conflict, release, TTL expiry, non-holder write refused.
4. **Client lock API + `Coordinator`.** `NetworkTransport.lock/unlock/renew` + shared
   `client_id`; `Coordinator` owns both transports + a BUS lease; runs `acquire_reference`/
   `measure_wall` over two fake bridges. Tests (headline R6): network SE result == in-proc
   sim SE (campaign PASS); I7 barrier (op-order: every analyzer read for i preceded by a
   source set+RF-on for i, even with injected latency); observer write refused during a pass.
5. **Discovery/broadcast** (UDP beacon module: `announce` + `Discoverer.browse`; record
   schema; wire into the bridge host + `cli.build_analyzer_link`/`SourceLink` `discover`
   path + a `discover` verb). Tests: multi-service loopback (2 announcers -> 2 records whose
   `transport_addr` round-trips `parse_net_addr`, no token in datagram); honest-empty ->
   ABSENT -> stop; browse-as-discover_fn -> READY against a fake bridge.
6. **Control plane (R9)** -- capability contract + `register_driver`/`resolve_driver` registry
   (seed 8565->RX, 68369->TX), networked instrument HANDLE, `ControlPlane` (live roster from
   discovery; `available_rx/tx`, `resolve(kind|role)`), and a modular `Coordinator` that runs
   the SE loop over any resolved `(TX,RX)`. Tests: registry resolves each model; ControlPlane
   builds the live roster from beacon records + resolves an RX and a TX handle; the coordinator
   runs a campaign over control-plane-resolved handles == the address-wired result; an
   unregistered model is reported honestly (no fake). LIVE SE figure (R8) streamed per point.
7. **Role GUIs + pub/sub** (`SourceControlGUI`, `SECoordinatorGUI`, `DashboardGUI`;
   `LiveSpectrumGUI` observer mode subscribing to the coordinator stream). Test (R6 e2e):
   analyzer-view + source-control + coordinator against the two fake services -> coordinator
   completes a campaign PASS while the analyzer-view observes and source-control is lease-
   locked-out.

## 6. Test plan (networking tests REQUIRED -- R6)

Mirror the proven `tests/test_net_transport.py` daemon-thread + ephemeral-port pattern.
Hardware-free unless marked live. T1 SourceLink lifecycle. T2 lease grant/conflict/release/
TTL. T3 (headline) coordinator per-point handshake over two fake bridges == in-proc SE.
T4 serialization/barrier (op-order under injected latency). T5 observer replication (zero bus
writes). T6 discovery multi-service loopback + honest-empty + browse->READY. T7 multi-instance
role launch (concurrent). **T8 (R8) LIVE SE figure**: with a stored reference, run the wall
pass over the two fake bridges and assert a per-point SE(f) is emitted DURING the sweep
(streamed as points complete, not only at the end), matches `reference-wall`, floor-limited
points report `SE >= capability`, and the running worst-case/`campaign_pass`-so-far update
live and equal the final `summarize`. Plus an opt-in `live`-marked integration test announcing
on the default group + browsing on the real LAN. Every increment keeps the full suite green.

## 7. Iteration log / state

- Pass 0: created spec; parsed intent (sec 1); mapped current state (sec 2); dispatched the
  elite ensemble (3 lenses).
- Pass 1 (Phase 1 COMPLETE): synthesized the ensemble into the canonical feature set (sec 3),
  architecture (sec 4), plan (sec 5), tests (sec 6). Determinations mutually consistent:
  one-gateway-per-bus + thread-per-conn + bus-mutex + lease/lock; zero-dep UDP discovery beacon
  carrying `transport_addr`; roles A-E; SE core reused unchanged behind a Coordinator holding a
  BUS lease; invariants I1-I8.
- Pass 2: added R8 (live SE figure during concurrent operation) + R9 (modular live control
  plane for any TX/RX -- capability contract, driver registry, control-plane vs data-plane,
  live roster); folded into roles/architecture/plan/tests. Plan is now 7 increments.
- Pass 3 (Phase 2 IN PROGRESS): **Increment 1 DONE + committed (822848c7)** -- gateway is
  thread-per-connection + bus mutex + per-pad fake (68369A/8565EC on one server); 4 networking
  tests (per-pad identity, 2 concurrent instances, no cross-talk, analyzer trace); 119 pass.
  Next: increment 2 (SourceLink), then 3 (lease/lock), 4 (client lock + Coordinator), 5
  (discovery), 6 (control plane R9), 7 (role GUIs + pub/sub).
- Pass 4 (Increment 2 DONE): `SourceLink` (connection.py) mirrors `AnalyzerLink` for the
  68369A TX -- subclasses it to reuse the whole discover->open->validate->auto-reconnect
  lifecycle (symmetric RX/TX control plane), driven by write_via (set_freq/set_power/
  rf_on/rf_off/list-sweep); read_sweep/read_point disabled (a source can't pull a trace).
  `DEFAULT_68369A` (10 MHz-40 GHz, family 683). +7 networked tests: discover/validate READY,
  ABSENT when only the analyzer is present, INVALID on a wrong 683xx unit, write ops drive
  the bench, read_sweep rejected, auto-reconnect after a dropped write, and a control-plane
  RX+TX pair both READY from one inventory. 126 tests pass. Next: increment 3 (lease/lock).
- Pass 5 (Increment 3 DONE): lease/lock arbitration. `LeaseRegistry` (process-wide, shared
  by all connection threads; pure + now-injectable) grants ONE session an exclusive lease on
  a device pad or the whole BUS for a TTL; conflicting scopes refuse each other; released on
  U / TTL / disconnect. Protocol verbs L/K/U/R added; W/Q are arbitrated (A/T stay open so an
  observer can still connect + watch). `NetworkTransport.lease/renew_lease/release_lease/
  lease_report` on the client; `LinuxGpibBackend` now tracks its bound pad for scoping. +8
  tests (registry exclusivity/BUS-scope/renew/release + TCP: second-controller block, BUS
  blocks other pad, two device leases coexist, TTL expiry, release-on-hard-disconnect).
  134 tests pass. Next: increment 4 (client-side lock helper + Coordinator over RX+TX).
- Pass 6 (Increment 4 DONE): `coordinator.py`. `Coordinator` owns an RX AnalyzerLink + a TX
  SourceLink, ensures both READY (auto-reconnect), takes exclusive control (leases both when
  networked; no-op in sim), and runs the substitution campaign over the pair via loop.py --
  reference pass then wall pass, source-tracked. `LiveSEFigure` maintains the running
  WORST-CASE SE (a running min, so monotonically non-increasing) with floor-limited lower
  bounds; `run_campaign(on_se_update=...)` streams it per wall point so the SE figure is
  known DURING concurrent operation (R8). Added an additive `on_point` hook to
  loop.acquire_reference/measure_wall. +3 tests: sim campaign with a live monotonic SE figure
  == wall min, CoordinatorNotReady when a link is absent, and (networked) take_control leases
  BOTH pads so a concurrent observer is refused until release. 137 tests pass. Next:
  increment 5 (discovery beacon) then 6 (control plane R9: registry + ControlPlane roster).
- Pass 7 (Increment 5 DONE): `discovery.py`. Zero-dep UDP discovery so role instances find
  bridges with no hand-configured address: a `Beacon` on the bridge host answers a broadcast
  PROBE with a `BeaconInfo` (bridge host/port + the instruments behind it, service-tagged);
  `discover()` broadcasts one probe and collects replies for a window, de-duped by (host,
  port). Pure stdlib (socket + json) -- NOT zeroconf, which needs a fixed well-known port and
  collides across xdist workers; the port is injectable and tests run on loopback with
  ephemeral ports. +4 tests: encode/decode roundtrip, foreign-UDP rejected, discover finds a
  local beacon (both rx+tx advertised), discover empty when nothing listens. 141 tests pass.
  Next: increment 6 (control plane R9: driver registry + ControlPlane roster + modular resolve).
- Pass 8 (Increment 6 DONE): `control_plane.py` (R9). Driver REGISTRY maps model/idn -> a
  concrete driver + role (rx/tx) + freq window; `register_driver`/`resolve_driver` (seeded
  8565->rx, 68369->tx); a new model plugs in with zero Coordinator change. `ControlPlane`
  holds the live roster of `Unit`s (role + capability set + address + a lazily-built cached
  Link); `available_rx/tx`, `resolve(kind|instance_id)`, `roster()`, and `make_coordinator()`
  which resolves an rx+tx pair by capability (raises ControlPlaneError if either is missing).
  Builders: `simulated()` (rx+tx Sim drivers share one bench, classified by the registry) and
  `from_beacons()` (networked units over NetworkTransport per advertised pad; unknown models
  skipped honestly). +7 tests: registry resolve/plug-in, sim roster + capability resolve, sim
  make_coordinator campaign, raises without a pair, from_beacons skips unknowns, and
  from_beacons builds a networked pair whose coordinator leases both pads. 148 tests pass.
  Next: increment 7 (role GUIs + pub/sub telemetry: SourceControl / SECoordinator / Dashboard).
- Pass 9 (Increment 7 DONE -- PLAN COMPLETE): `telemetry.py` (TelemetryHub/TelemetrySubscriber:
  line-JSON TCP pub/sub, best-effort fan-out) + `roles.py` (CoordinatorRole owns the bus and
  publishes the live SE figure / roster / summary; DashboardRole observer subscribes and holds
  the model, no bus contact). CLI `coordinator` + `dashboard` verbs wire real launchable role
  instances; control_plane gained `from_addresses` (explicit net:/VISA addresses without a
  beacon). +6 tests incl. the capstone: a CoordinatorRole runs a sim campaign while a
  DashboardRole observer receives the live worst-case SE (monotone) + roster + summary over
  telemetry (R8, two concurrent instances, no observer bus contact), plus CLI smoke tests.
  154 tests pass. All 7 increments delivered; R1-R9 satisfied end to end with valid networking
  tests. Remaining for a REAL bench run: confirm the 683xx GPIB mnemonics vs the manual (PC2)
  and wire an optional `--beacon` flag on ni_gpib_server so a real bridge advertises itself.
- Pass 10 (qemu BOTH units): the qemu path was 8565EC-only; extended it so ONE NI adapter /
  ONE bridge serves BOTH instruments. **SUPERSEDED by Pass 13 + the two-adapter golden topology:
  the 68369A is NOT on the 8565EC's bus -- it is on a SECOND NI adapter (GPIB-USB-HS), so the two
  instruments ride TWO boards on separate TCP ports (analyzer 5555, source 5556), and the intended
  live deployment is the GOLDEN two-VM pair (one qemu per adapter), now the `--vm-mode` default.
  The single-adapter "one bridge serves both" note below is retained only for history.**
  `VmSpec.source_addr` (pad 5) + `analyzer_net_addr` /
  `source_net_addr`; `provision.sh` takes `ANALYZER_PAD SOURCE_PAD PORT` and writes gpib.conf
  for both (`sa8565` + `sg68369`); `render_cloud_init` passes both pads; `vm.source_reachable`
  + `vm.both_reachable`; `ensure_bridge(require_both=True)` gates readiness on BOTH answering.
  Provisioning/bring-up is now PART OF LAUNCH: `cli.py coordinator --vm` boots the VM, waits
  for both units, wires both net: addresses off the one bridge, and runs the campaign (also
  `--vm-plan`, `--vm-port`). +4 tests (both-reachable through one fake bridge, per-role net
  addresses, provision-both cloud-init, and the coordinator --vm wiring). 158 tests pass. The
  live `--vm-plan` detects a real NI GPIB-USB-HS attached (0x702b), so the passthrough path is
  real; a full qemu boot needs the image + the two instruments on the bus.
- Pass 11 (qemu LIVE bring-up -- real hardware): booted the qemu VM against the attached NI
  adapter + a real 8565EC and fixed every provisioning bug found in order: linux-gpib git URL
  (/code 404 -> /git); NI firmware (dead fmhess/gpib_firmware-2008 -> fmhess/linux_gpib_firmware
  + a deterministic two-stage fxload, 0x702b -> 0x702a, passthrough survives via vendorid);
  gpib.conf (dropped device stanzas with invalid eos_flags -> interface-only, ad-hoc pads);
  gpib_config non-fatal; and THE reachability bug -- bind 0.0.0.0 --insecure not 127.0.0.1,
  because qemu hostfwd targets the guest eth0 IP not loopback (a loopback bind RSTs every
  forwarded connection). prepare_assets now uses a pristine base + disposable COW overlay so
  re-provisioning re-runs cloud-init with no re-download (cli --vm-reset). RESULT: the real
  8565EC answers `ID? -> HP8565E,001,006,007,008` over the qemu bridge -- passthrough ->
  firmware -> linux-gpib -> bridge -> real GPIB, end to end. The 68369A was silent on every
  scanned pad (powered off / not cabled); its path is identical (same bridge, same pad access),
  so it works once present. 159 tests pass.
- Pass 12 (68369A presence DEFINITIVELY resolved + hardening): added guest SSH (hostfwd
  2222->:22, cloud-init key inject) and ran linux-gpib's reliable full-bus enumeration (fast
  write-probe pads 1-30, no timeout jamming). Result: ONLY pad 18 present (the 8565EC). The
  68369A is at NO GPIB address -> physically powered off / uncabled, NOT a software gap (a
  powered GPIB device always participates in the handshake and would be detected). Hardening:
  ensure_bridge names WHICH unit is missing and no longer boots a 2nd VM when the bridge is
  already up; provision.sh self-heals the board via ExecStartPre (modprobe + gpib_config on
  every start -- validated live) and clones linux-gpib over HTTPS only (dropped the git://
  plaintext fallback, a supply-chain finding). 160 tests pass. STATUS: 8565EC fully working
  through qemu (real ID? repeatable); 68369A blocked ONLY on being physically connected +
  powered at pad 5 -- identical path, works the instant it is present.
- Pass 13 (TWO-ADAPTER truth + per-instance/role refactor; 180 tests, 2 live-skip): the 68369A
  is NOT on the 8565EC's bus -- it is on a SECOND NI adapter (GPIB-USB-HS 0x709b). Passed BOTH
  adapters through ONE qemu VM: HS(0x709b, high-speed) -> usb-ehci (UEFI XhciDxe ASSERTs on its
  interrupt endpoint; EhciDxe tolerates it), GPIB-USB-B(8565EC, full-speed) -> qemu-xhci (bare
  EHCI drops a full-speed device). Confirmed 0 UEFI ASSERTs, both PIDs enumerated in-guest.
  Root-caused the 68369A: NOT absence -- a linux-gpib 4.3.7 driver bug (the IBGTS after every
  send_setup wipes recent-HS-firmware addressing -> ENOL). provision.sh now patches
  ni_usb_go_to_standby() to a no-op + sets gpib.conf master/set-reos/set-eot. The GPIB-USB-B got
  stuck in fxload-limbo (0x702a bcdDevice 0.01) that survives VM reboot AND qemu USB reset -> a
  PHYSICAL power-cycle is required to clear it (the one remaining live-validation blocker).
  Refactor per the adjusted goal: 1 instance == 1 qemu. vm.py gained VmSpec.role
  (analyzer|source|both) with fully per-instance identity (workdir/qmp/mac/ports) so MULTIPLE
  qemus coexist; golden_pair() = the GOLDEN two-instance deployment (analyzer VM + source VM on
  loopback, analyzer pinned to the B's hostbus/hostport -- the race-free matching; serial is
  unsupported by usb-host); ensure_golden_pair; cli `coordinator --vm --vm-mode {both,golden}`.
  provision.sh is role-aware (enables only that role's bridge service, self-maps board->pad,
  hardens /boot nofail). Cross-instance sync: the coordinator is ONE process, so sequential
  blocking net: transactions guarantee ordering across the two instances; added
  SignalGenerator.await_settled(*OPC? + settle) after rf_on, before the analyzer read (loop.py,
  cfg.source). tests/test_sync.py (ordering) + tests/test_e2e_live.py (hardware-gated, skip
  honest when no live bridge). STATUS: full architecture + hardware-free tests landed; LIVE
  validation of both units awaits a one-time physical replug of the two NI adapters.
- Pass 14 (golden RUN-FLOW hardening; hardware-free-tested, LIVE-validation pending): the golden
  topology + anti-swap pinning were audited CORRECT; the run flow around them got hardened.
  (1) The SOURCE HS gained wedge-recovery: reattach_source_hs (QMP device_del/device_add id=ni_hs
  on ehci.0), the HS pinned by hostbus/hostport in golden_pair, fired in the poll loop for
  SOURCE/BOTH -- previously a golden source VM had NONE. (2) Operator recovery guidance: a unit
  DETECTED on the host that never answers now reports the specific wedged-FX2 replug step (same
  USB port; a reboot won't clear it; power-cycle + --vm-reset if it recurs), per-unit for every
  role, and in launch_plan. (3) Duplicate-launch guard: instance_is_live (pidfile / QMP / bound
  port) makes a re-run inside the boot window JOIN the in-progress boot instead of spawning a
  rival qemu; QemuVm.launch writes a pidfile. (4) Golden now LAUNCHES both VMs then polls both,
  DEGRADED per-instance (one timeout returns the healthy unit + recovery, not a whole-run abort);
  default per-unit timeout cut to 240s (--vm-timeout). (5) `--vm-mode` DEFAULT flipped to golden
  (both is explicit opt-in). (6) When the analyzer is not port-pinned, the source (HS) VM launches
  FIRST so the vendor-only 0x3923 match cannot grab the HS. (7) A stale hostport pin surfaces
  "re-plug into port X (was Y)" (hostport_drift). (8) New `vm-stop` verb (QMP quit / SIGTERM
  stored pid) + --vm-reset refuses while a live qemu holds the overlay open. Docs corrected
  (provision.sh `both 18 5 5555 5556`; interface-only gpib.conf; source net:...:5556:5; a TCP
  reconnect does NOT revive a wedged guest USB device). +26 vm tests (89 hardware-free vm/CLI
  golden tests total pass). LIVE qemu validation still pending (no live qemu in this environment).
- Pass 14b (R1a-R6 durability): the Pass-14 stagger was a blind timer working for a mis-stated
  reason; hardened to a DURABLE, verified guarantee. R1a structural detection (adapters_share_
  controller: same-hostbus warning at bring-up/plan/timeout -- LIVE-CONFIRMED both adapters are on
  hostbus 0, so the PHYSICAL move to a second controller stays the primary race fix). R1b host-state
  settle (_await_host_settle: gate the second golden launch on the first adapter STABLE across two
  detect reads / 0x702a, not a fixed sleep; re-verify the first didn't fall off after the second
  claim -> STOP+degrade). R1c docstring names the real invariant (serialize the host-side FX2
  claim/RESET). R2 the two golden units poll INDEPENDENTLY (threads). R5 vm-stop is health-aware
  (never stop a reachable instance without --name; only unlink a pidfile it actually stopped). R6
  early ADAPTER_WEDGED verdict (present-but-silent after the re-attach budget -> physical-replug NOW,
  not a 240s grind).
- Pass 15 (SINGLETON refactor -- the SINGLE-MACHINE CANONICAL topology): a SINGLE VM claiming both
  adapters serial-and-verified is strictly more controllable than two VMs racing one host controller
  (it FULLY settles adapter 1 before touching adapter 2). Additive on the R1a-c/R2/R5/R6 primitives.
  (1) ARGV SPLIT: _usb_controller_argv + build_qemu_argv(hotplug_usb=True) boot the two controllers
  EMPTY -- ZERO usb-host at boot -> the UEFI XhciDxe ASSERT dissolves (nothing to enumerate). (2)
  attach_adapter(spec, which) = the P5-Attach PRIMARY primitive (generalizes reattach_*): device_add
  on the pinned controller (HS->ehci.0, B->xhci.0) with a device_del PREFIX unifying initial+recovery;
  the B waits host 0x702a (fxload) then del+add so the guest re-follows the live PID. (3)
  ensure_singleton: REUSE (both answer -> no boot); JOIN a live qemu (attach only the MISSING
  adapter, never reboot; self-heal an unprovisioned join = stop+one relaunch); await_guest_provisioned
  gate (the /run/se299/provisioned marker over ssh :2222) BEFORE any attach; SERIAL attach HS+VERIFY
  THEN B+VERIFY (host resets never overlap); per-unit DEGRADE. (4) provision.sh touches
  /run/se299/provisioned as its FINAL step. (5) cli `--vm-mode singleton` is the single-machine
  DEFAULT; golden kept for remote/fault-isolation; `both` flagged SUPERSEDED; vm-stop default reaps
  the singleton too. +14 tests (103 hardware-free vm/CLI tests pass; full suite 486). LIVE-PENDING:
  real host claim/reset timing + the fxload/attach sequence on the physical bench.
