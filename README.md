<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/hero-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/hero-light.svg">
    <img alt="se299 -- IEEE-299 substitution shielding-effectiveness automation, DC-40 GHz, SE >= 100 dB. Anritsu 68369A source + TX horn OUTSIDE, HP 8565EC analyzer + RX horn INSIDE; SE(f) = reference(f) - wall(f). A computer drives each instrument over GPIB: the outside controller, and the inside Pi reached over the fiber-optic link -- the only shield penetration, no copper through the shield. Every reading is logged over the digital bus, never read off the instrument display." src="assets/hero-light.svg" width="920">
  </picture>
</p>

# se299 -- IEEE-299 substitution SE automation (Tier-1, real instruments)

Computer-automated, no-cable shielding-effectiveness measurement for the HMEMC
enclosure: the 40 GHz / 100 dB acceptance test (doc 147 Path 2, doc 159). This
is the **Tier-1** rig (real Anritsu 68369 source + HP/Agilent 856x analyzer);
the parent `rf-se/` HackRF pipeline is the **Tier-0** sub-6-GHz screen.

Method (doc 147 sec 4.1): source + TX horn OUTSIDE, analyzer + RX horn INSIDE,
**nothing crosses the shield**. SE(f) = reference(f) - wall(f) by substitution.
A reading at the noise floor yields only a LOWER BOUND on SE.

**All acquisition is over the DIGITAL bus (GPIB / 10GbE), logged programmatically --
never by reading the instrument display** (doc 159 sec 14). A sweep is a logged
level-vs-freq array; `localize` is a logged level-vs-position map.

## Setup on a new machine (portability)

The library is self-contained: no hardcoded paths, no hardcoded instrument IPs, and every host-
specific location (qemu assets, SSH keys) resolves under `~`. What a fresh machine needs depends
only on WHICH of the three run modes you want.

**Prerequisite (all modes):** [`uv`](https://docs.astral.sh/uv/) + Python 3.13 (uv fetches it).
From the repo root, `uv sync` builds `.venv/` from `pyproject.toml` + `uv.lock`. Everything below is
run with `uv run python rf-se/se299/...` from the repo root, so it works from any checkout location.

| Run mode | Extra install | Host requirement | Portable? |
|---|---|---|---|
| **Simulator** (default -- `--source sim --analyzer sim`, all `demo`/tests) | `uv sync` | any OS with Python 3.13 | fully |
| **Operator GUIs** (`se-gui`/`bench`/`sa`/`sg`/`walkaround`) | `uv sync --group se299-gui` | any OS with a display (or `QT_QPA_PLATFORM=offscreen` for tests) | fully |
| **Networked instruments** (`net:HOST:PORT:PAD`) | `uv sync` (client needs NO VISA) | a Linux host holds the adapter + runs `gpib_bridge/ni_gpib_server.py` (a Pi, or any Linux box); see `gpib_bridge/README.md` | fully (any client OS) |
| **Direct VISA** (`GPIB0::18::INSTR`, `TCPIP0::...`) | `uv sync --group se299-hw` | a VISA-capable OS (Linux/Windows; NOT Apple-Silicon macOS for USB-GPIB) | mode-limited by VISA |
| **Seamless qemu bring-up** (`--vm`) | `brew install qemu` | Apple-Silicon macOS + the physical NI GPIB-USB adapter | host-specific (macOS only) |

Only the `--vm` seamless bring-up is machine-specific (it drives qemu USB-passthrough on Apple-Silicon
macOS and downloads an Ubuntu cloud image to `~/.se299-vm/`). Every other mode -- including live
instruments via the network bridge -- runs on any OS: the client speaks plain TCP to the bridge, so a
Linux/Windows/Intel-Mac box drives the same 8565EC + 68369A a Mac would, just by pointing `net:` at the
bridge host.

**Configuration is environment variables (all optional, sane defaults).** The tools and live tests read
these so nothing is hardcoded to one bench:

| Variable | Default | Meaning |
|---|---|---|
| `SE299_ANALYZER_PORT` | `5555` | TCP port of the bridge serving the 8565EC (RX) |
| `SE299_ANALYZER_PAD` | `18` | GPIB primary address of the analyzer |
| `SE299_SOURCE_PORT` | `5556` | TCP port of the bridge serving the 68369A (TX) |
| `SE299_SOURCE_PAD` | `5` | GPIB primary address of the source |
| `SE299_LIVE_RX` / `SE299_LIVE_TX` | unset | `net:` addresses that opt the two-client live E2E (`run_live_two_client_e2e.py`, `test_e2e_two_client_local.py`) onto the REAL bridge instead of a local fake |
| `NI_GPIB_TOKEN` | unset | shared secret the bridge requires for bus writes (set on both the server and every client when auth is enabled) |

CLI verbs take addresses directly (`--analyzer net:HOST:PORT:PAD --source net:...`); the standalone
`tools/` scripts (`diagnose_8565ec.py`, `live_se_figure_sweep.py`, `characterize_bands.py`) read the
`SE299_*` env vars so they need no arguments on a correctly-pointed bench.

**Verify a fresh checkout (no hardware):**

```bash
uv sync
uv run python rf-se/se299/cli.py demo                       # SE sweep end-to-end on the simulator
QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest tests/ -q  # full board
```

The full board is the single command above. `tests/conftest.py` drains Qt objects (DeferredDelete +
closeAllWindows + gc) after every test so the co-located Qt + non-Qt suite does not trip an
intermittent shiboken teardown SIGSEGV at interpreter finalization; it is a pure no-op for tests that
never load Qt (so the board still collects and runs WITHOUT the `se299-gui` group). If that segfault
ever recurs on a machine, fall back to the SPLIT board (both green): run the non-Qt files first
(`--ignore=` the Qt files: test_se_gui / test_bench / test_cli_gui_verbs / test_live / test_point_op_* /
test_range_mode / test_sa_panel / test_sg_panel / test_walkaround / test_agent), then those Qt files on
their own.

## Quick start (hardware-free -- runs today, no pyvisa, no instruments)

```bash
cd <repo root>
uv run python rf-se/se299/cli.py demo            # SE sweep end-to-end against the simulator
uv run python rf-se/se299/cli.py demo --gain 25  # standard 25 dBi horns: EA8 fails @ 40 GHz
uv run python rf-se/se299/cli.py localize --freq 38   # fixed-freq seam scan: find the hot seam (digital)
uv run python rf-se/se299/cli.py dryrun          # freq plan + link budget + exact instrument cmds
uv run python rf-se/se299/cli.py detect          # is the 8565EC DETECTED + VALID? (auto-connect)
uv run python rf-se/se299/cli.py nf-sweep        # 8565EC as a near-field-probe SWEEPER (auto-connect)
uv run python rf-se/se299/cli.py sweep --mode stepped   # acceptance-grade stepped-CW sweep (high DR)
uv run python rf-se/se299/cli.py sweep --mode swept     # fast swept-span SCREEN (blind to deep leaks)
uv run python rf-se/se299/cli.py q               # cavity Q-factor (both units inside; NOT IEEE-299)
uv run python -m pytest rf-se/se299/tests/ -q    # hardware-free tests (drivers, GUI, networking, calibration, checkpath, device-operation, sweep, ...)
```

## 8565EC as a near-field-probe sweeper (analyzer-as-sweeper, no source)

A near-field probe on the 8565EC input; the analyzer sweeps a span and the whole
level-vs-freq trace is pulled over the bus each sweep (never screen-read). The hot
bin is the seam/gasket leak the probe is localizing. The connection LIFECYCLE is
automatic: discover the bus -> open -> identify (`*IDN?`/`ID?`) -> validate (model
+ span within range) -> auto-reconnect on a dropped link.

```bash
uv run python rf-se/se299/cli.py detect                          # auto: scan bus, sim if none
uv run python rf-se/se299/cli.py detect   --analyzer GPIB0::18::INSTR
uv run python rf-se/se299/cli.py nf-sweep --span-lo 1 --span-hi 6 --sweeps 4
uv run python rf-se/se299/cli.py nf-sweep --analyzer sim --sweeps 0   # continuous until Ctrl-C
```

`--analyzer` is `auto` (scan the real bus; fall back to the simulator only when no
instrument is found, clearly labeled), `sim` (forced simulator), or an explicit
VISA resource string (honest DETECTED/INVALID/ABSENT). The verbs exit non-zero
when the 8565EC is not DETECTED + VALID, so they gate a real run.

## Operator runbook: how to perform an SE test (the workflow)

The canonical order. Each step gates the next; do not trust an SE number until step 1 passes.
`ADDR` is `sim` | `net:HOST:PORT:PAD` | a VISA string; add `--vm` / `--vm-mode golden` to bring
the qemu bridge(s) up seamlessly instead.

```bash
# 1. VERIFY THE RF PATH FIRST. Transmit a tone; confirm the RX sees it rise above the floor and
#    fall back (reversible). Catches an open/dead RF path instead of silently reporting SE = 0.
uv run python rf-se/se299/cli.py checkpath --source ADDR --analyzer ADDR
#    PATH-LIVE  -> proceed.  NO-COUPLING -> our tone never reaches the RX: it prints the connector
#    checklist (source RF-OUT, TX/LPDA feed, RX/horn feed, analyzer RF-IN). The reported RX-ambient
#    tells you which side is live (RX picks up ambient => RX path good, suspect the TX side).

# 2. CALIBRATE. Capture the reference pass in the current geometry; it is graded + saved, and
#    records the KNOWN TX power (calibrated source-output level) per point for audit.
uv run python rf-se/se299/cli.py calibrate --source ADDR --analyzer ADDR --out cal.json
#    STATUS USABLE -> a strong reference.  FLOOR-LIMITED/PARTIAL -> the link is weak; SE will be a
#    LOWER BOUND (fine -- TX cancels in SE = ref - wall, so the reference need only be STABLE).

# 3. RUN THE CAMPAIGN with the live SE figure + operator controls (Run/Stop, gain, RBW):
#    The three operator windows (se-gui / walkaround / live) use PySide6 + pyqtgraph, which live in
#    an OPTIONAL dependency group so the shared CAD venv stays lean. Install it once:
uv sync --group se299-gui
uv run python rf-se/se299/cli.py se-gui --source ADDR --analyzer ADDR      # GUI: live SE(f) curve
uv run python rf-se/se299/cli.py coordinator --source ADDR --analyzer ADDR # headless: streams SE
#    both run the reference pass then the wall pass and compute SE(f) = reference(f) - wall(f).

# INSTRUMENT BENCH (Phase 1): the analyzer + generator as first-class GUIs, independent or together.
uv run python rf-se/se299/cli.py bench --analyzer ADDR --source ADDR   # SA + SG in one window
uv run python rf-se/se299/cli.py sa    --analyzer ADDR                 # spectrum analyzer alone
uv run python rf-se/se299/cli.py sg    --source ADDR                   # signal generator alone
#    SA and SG run independently (different instruments); the bench shares both via an InstrumentHub
#    (lease-on-demand), and `cli.py live` is now an alias for `sa`. `--vm`/`--vm-mode golden` boots
#    the qemu bridges first, same as se-gui.
```

## With instruments (single-shot capture)

```bash
uv sync --group se299-hw                 # VISA backend (only needed for DIRECT VISA hardware)
uv run python rf-se/se299/cli.py preflight \
    --source GPIB0::5::INSTR --analyzer TCPIP0::pi-inside.local::gpib0,18::INSTR
uv run python rf-se/se299/cli.py capture --phase reference --label run1 --source ... --analyzer ...
# (move horns face-to-face for reference, then put the enclosure between them)
uv run python rf-se/se299/cli.py capture --phase wall      --label run1 --source ... --analyzer ...
# -> output/run1/{manifest.json, reference.json, wall.json, se_results.csv}
```

**Instrument command sets (confirmed):** source is the Anritsu 68000-series native GPIB language
(`CF1 <GHz> GH` sets CW mode + frequency -- NOT `CW1`; `L1 <dBm> DM`; `RF1`/`RF0`; `OF1`/`OL1`/`OSB`
readback), confirmed vs the Anritsu 681XXC PM (10370-10334) and verified live on the 68367C.
Analyzer is the HP 8560-series language; a campaign presets the analyzer (`IP`) and flushes the
stale first sweep before reading -- without a clean preset the marker returns non-repeatable values.

### Driving GPIB from an Apple Silicon Mac (network bridge)

An NI GPIB-USB-HS cannot be driven natively on Apple Silicon macOS (no NI-488.2 driver;
pyvisa-py GPIB is Linux/Windows only). Use the **network GPIB bridge** in `gpib_bridge/`:
run `ni_gpib_server.py` (linux-gpib) on a Linux host that holds the adapter -- a UTM/QEMU
VM on the Mac, or a Raspberry Pi -- and address the instrument as `net:HOST:PORT:GPIBADDR`.
It routes through `drivers.NetworkTransport` (a drop-in for `VisaTransport`), so every verb
takes a `net:` address:

```bash
# hardware-free self-test of the whole path (canned 8565EC):
uv run python rf-se/se299/gpib_bridge/ni_gpib_server.py --fake --port 5599 &
uv run python rf-se/se299/cli.py detect --analyzer net:127.0.0.1:5599:18   # DETECTED + VALID
# real bridge (linux-gpib in the VM/Pi): swap host/port/addr
uv run python rf-se/se299/cli.py capture --phase reference --label run1 \
    --analyzer net:192.168.64.5:5555:18 --source net:192.168.64.5:5555:5
```

See `gpib_bridge/README.md` for the VM bring-up (`provision.sh`) and the USB-passthrough
gotcha (the NI adapter re-enumerates 0x702b -> 0x702a on firmware load; pass it through by
USB port, not VID:PID).

## Networked multi-instance operation (both units, live control plane)

The 8565EC (RX) and 68369A (TX) can both be network-based behind one bridge, driven by
MULTIPLE instances that each serve a role. Full determination + design in
`NETWORKED_OPERATION_SPEC.md`. The layers:

- **One bridge, many clients.** `ni_gpib_server.py` is thread-per-connection: several
  instances connect to one bus concurrently. A process-wide mutex serializes each
  transaction, and one FakeBackend answers per bound pad (pad 5 -> 68369A, else 8565EC), so
  one server serves both instruments.
- **Arbitration (lease/lock).** A VXI-11-style lease (`L/U/K/R` verbs;
  `NetworkTransport.lease/renew_lease/release_lease/lease_report`) gives ONE controller
  exclusive bus access to a device pad or the whole bus for a TTL; released on `U`, TTL
  expiry, or disconnect. Non-holders are observers, refused bus ops on the leased scope.
- **Links.** `connection.AnalyzerLink` (RX) and `connection.SourceLink` (TX) each
  self-manage discover -> open -> validate -> auto-reconnect.
- **Coordinator + live SE figure (R8).** `coordinator.Coordinator` owns an rx+tx pair, takes
  exclusive control, runs the substitution campaign source-tracked, and streams a running
  WORST-CASE SE figure per wall point -- the SE is known WHILE both units operate.
- **Discovery.** `discovery.py` -- a UDP `Beacon` on the bridge host answers a broadcast
  `discover()` probe, so instances find bridges with no hand-configured address (pure
  stdlib, not zeroconf).
- **Control plane (R9).** `control_plane.py` -- a driver REGISTRY maps model/idn -> driver +
  role (`register_driver`; a new instrument plugs in with zero Coordinator change); a
  `ControlPlane` holds the live roster and resolves rx/tx by CAPABILITY, then composes any
  pair into a Coordinator (`simulated` / `from_beacons` / `from_addresses`).
- **Roles + telemetry.** `roles.CoordinatorRole` publishes the live SE figure / roster /
  summary over `telemetry.TelemetryHub`; `roles.DashboardRole` observers subscribe and render
  without touching the bus.

```bash
# SEAMLESS through qemu: boot the VM (USB passthrough of the NI adapter), provision the whole
# bus, wait until BOTH the 8565EC and 68369A answer on the one bridge, then run the campaign:
uv run python rf-se/se299/cli.py coordinator --vm
# or point at an already-running bridge (Pi / VM), both units off one host:port, two pads:
uv run python rf-se/se299/cli.py coordinator \
    --analyzer net:192.168.64.5:5555:18 --source net:192.168.64.5:5555:5 \
    --telemetry-port 52998 --wait-subscribers 1
# instance 2 -- a live dashboard observer (no bus contact; sees the SE figure as measured):
uv run python rf-se/se299/cli.py dashboard --telemetry 127.0.0.1:52998
# hardware-free: swap both --analyzer/--source for 'sim' (one shared bench)
uv run python rf-se/se299/cli.py coordinator --source sim --analyzer sim --telemetry-port 0
```

Tests: `tests/test_networked.py` (39) covers concurrency, arbitration, both links, the
coordinator + live SE, discovery, the control plane, and the coordinator/dashboard pub/sub.

## Sweep modes and the acceptance boundary

Driving the 8565EC over GPIB, there are THREE acquisitions and ONE verdict path. The
canonical control surface (`drivers.Agilent856xEC`) is confirmed against the HP 8560
E-series programming manual (`reference/operator-manuals/hp-8560-e-series-programming.md`).

- **stepped-CW synthetic sweep** (`sweep --mode stepped`): the source is stepped across
  the band and the analyzer reads a zero-span, narrow-RBW, positive-peak dwell at each
  point. This is the ONLY acquisition with the dynamic range to see a deep leak, and the
  ONLY one whose levels may feed an SE acceptance verdict (via `capture`).
- **swept-span screen** (`sweep --mode swept`): one fast analyzer sweep over a span. Its
  span-coupled RBW floor is too high to see deep leaks -- it is "blind" and yields only a
  lower bound + a spur/hot-bin map. NEVER a pass/fail verdict.
- **near-field probe survey** (`nf-sweep`): analyzer-as-sweeper, no source -- localizes
  WHERE a leak is, produces no SE number.

**Source-tracks-sweep requirement.** A swept SE measurement is valid ONLY if the source
frequency TRACKS the analyzer's measurement frequency at every point -- a tone must be present
at the bin being measured. A swept trace taken against a parked or absent source is a screen
(lower bound), never an SE result. Every acquisition carries `source_tracked`; `loop.summarize`
requires it, and `loop.require_source_tracked(frame)` gates any swept-frame -> verdict promotion.
There is no true tracking generator at 40 GHz, so tracking is realized two ways (both
`source_tracked=True`, via `loop.tracked_sweep`): **software lockstep** (the controller sets
source AND analyzer to each f -- the default, robust, one GPIB round-trip/point; this is
`stepped_cw_sweep` / `capture`) or **hardware list-sweep** (`hardware=True`: the 68369 runs a
preloaded list advanced by one external trigger per point via `set_list_sweep`/`arm_sweep`/
`trigger_point` -- faster, needs the 8560-trig-out -> 683xx-trig-in wire and the 683xx
list-sweep commands, VERIFY against the 683xx manual).

Every acquisition is typed `acq_mode` (stepped-cw-zerospan / swept-span / probe-survey) and
`purpose` (acceptance / screening). The verdict engine (`loop.summarize`) is AFFIRMATIVE: a
campaign passes only if EVERY row is a stepped-CW acceptance PASS with `ea8_ok` and
`source_tracked` -- a screening row can never certify. Swept acquisitions run integrity guards (`loop.swept_screen`) that
raise `AcquisitionRejected` (never a silent wrong number) on UNCAL, non-DBM units, a short
trace, or a point spacing too coarse to resolve the expected leak notch.

**Cavity Q** (`q`) is a separate mode (both units inside, NOT IEEE-299): linewidth
Q = f0 / BW_3dB from the pulled trace (no MKBW mnemonic needed), with `composite_q()` for the
overmoded reverberation formula (`reference/` holloway-2008 + iec-61000-4-21).

## Files

| File | Role |
|---|---|
| `budget.py` | link-budget math (FSPL, reference, floor, SE capability) -- reproduces doc 159 sec 4.2b |
| `config.py` | Campaign config: instruments, analyzer, per-band plan + budget params, target, margin |
| `drivers.py` | instrument interfaces + real drivers (68369 / 856x full control surface: sweep/param/marker/detector/averaging/state, mnemonics confirmed vs the 8560 manual) + the hardware-free simulator (near-field, swept-substitution, and cavity-resonance models) |
| `discover.py` | cross-transport discovery: VISA scan + `*IDN?`/`ID?` parse + `sim_inventory` (never raises) |
| `connection.py` | `AnalyzerLink` (RX) + `SourceLink` (TX) automatic lifecycle: discover -> open -> identify -> validate -> auto-reconnect; `LinkStatus` (detected/valid) |
| `probe_sweep.py` | `ProbeSweeper` (8565EC near-field-probe sweeper) + ASCII `render_frame` |
| `loop.py` | the control loop: EA8 reference pass, through-wall pass, SE + verdicts, `localize` (level-vs-position seam scan), PC8 logging |
| `coordinator.py` | `Coordinator` owns an rx+tx pair, takes exclusive control, runs the campaign, streams the live worst-case SE figure (R8) |
| `discovery.py` | zero-dep UDP `Beacon` + `discover()` -- find bridges with no hand-configured address |
| `control_plane.py` | driver `register_driver`/`resolve_driver` + `ControlPlane` live roster + `make_coordinator` (resolve any rx/tx by capability -- R9) |
| `telemetry.py` | `TelemetryHub` / `TelemetrySubscriber` pub/sub -- the coordinator publishes the live SE figure to observers |
| `roles.py` | `CoordinatorRole` (owns the bus, publishes) + `DashboardRole` (observer, subscribes; no bus contact) |
| `cli.py` | `checkpath` / `calibrate` / `se-gui` / `demo` / `localize` / `dryrun` / `preflight` / `capture` / `detect` / `nf-sweep` / `sweep` / `q` / `coordinator` / `dashboard` |
| `se_gui.py` | live SE-figure GUI: `SEFigureModel` (pure) + `SELiveGUI` (PySide6 + pyqtgraph view + Run/Stop/gain/RBW/sweep-band/tone operator controls) painting SE(f) coloured by verdict |
| `walkaround.py` | near-field-probe GUI: `NearFieldModel` (pure) + `NearFieldGUI` (PySide6 + pyqtgraph heat meter + rolling trace + max-hold + marks) for leak localization |
| `live.py` | live 8565EC spectrum GUI: `LiveSpectrumModel` (pure) + `LiveSpectrumGUI` (PySide6 + pyqtgraph moving-spectrum view) |
| `qt_common.py` | shared Qt scaffolding: `ensure_app`, `ControlPanel` (reusable operator controls incl. `add_checkbox`), `OptionalDoubleSpin` (auto/None fields), `new_plot`, `run_live` (QTimer loop) |
| `instrument_hub.py` | bench instrument sharing: pure `Arbiter` (rx/tx ownership + one-level suspend/resume) + `InstrumentHub` (wraps the Coordinator, lease-on-demand, opens links via `ensure`, two-phase `acquire_both`) |
| `sa_gui.py` | full-fidelity Spectrum Analyzer: `SpectrumModel` (pure) + `SpectrumEngine` (command-queue sweep loop) + `SpectrumAnalyzerPanel` (pyqtgraph trace + max-hold + marker + native controls); supersedes `live.py`'s GUI |
| `sg_gui.py` | Signal Generator: `SourceModel` (pure, RF-off default) + `SourceEngine` (CW + step-sweep, `rf_off_safe`) + `SignalGeneratorPanel` |
| `bench_gui.py` | `BenchWindow` composing the SA + SG panels over one `InstrumentHub`, ordered shutdown (RF off before lease release); `build_bench` + `cli.py bench` |
| `tests/test_se299.py` | hardware-free regression tests (substitution rig) |
| `tests/test_probe_sweep.py` | hardware-free tests (discovery, validity, lifecycle, auto-reconnect, sweep) |
| `tests/test_sweep.py` | hardware-free tests: FakeTransport command strings + sweep/floor/detector/averaging + integrity guards + cavity Q |
| `tests/test_networked.py` | networked multi-instance: concurrency, lease arbitration, SourceLink, Coordinator + live SE, discovery, control plane, roles/telemetry |

## PC1-PC9 coverage (doc 159 sec 4a)

| # | Ensurement | Where |
|---|---|---|
| PC1 | remote interface present | `preflight` (open + idn both instruments) |
| PC2 | command coverage | `drivers.py` real drivers; 856x mnemonics CONFIRMED vs the HP 8560 manual (`reference/operator-manuals/hp-8560-e-series-programming.md`); **68369 mnemonics still VERIFY vs the 683xx GPIB manual before a real run** |
| PC3 | no copper through the shield | `config.Instruments.analyzer_link` (fiber-only); printed by `preflight`/`dryrun` (operational invariant, not enforceable in code) |
| PC4 | disciplined timebase, no ref cable | operational (lock-then-holdover); recorded in the manifest |
| PC5 | bus-read amplitude fidelity | `config.settings_key()` guard: reference and wall MUST share settings (SE is a ratio) |
| PC6 | automated EA8 system-check | `loop.acquire_reference` computes capability = ref - floor - margin; `ea8_ok` gate; `capture` warns on failure |
| PC7 | programmatic external-mixer path | not in this scaffold (40 GHz native); the 50-110 GHz mixer path is doc 105g / doc 165 sec 3.2 |
| PC8 | reproducible logging | `loop.write_run`: manifest (config + git + summary) + per-f JSON + CSV |
| PC9 | native-vs-mixer band map | `config.BandPlan` per-band records (native to 40 GHz here) |

## What it is / is not

The structure, the simulator, the EA8/verdict logic, the full 8565EC control surface,
and the tests are complete and runnable now. The 856x mnemonics are CONFIRMED against
the HP 8560 manual. Before a real campaign: (1) **verify the 68369 native GPIB
mnemonics** in `drivers.Anritsu68369` against the 683xx programming manual (PC2 --
still the one open command-coverage item); (2) confirm GPIB-over-fiber on the inside
Pi (PC3); (3) the simulator's enclosure curve (`drivers.demo_enclosure_se`) is
illustrative, not a prediction.

Default config uses the elite **33 dBi WR-28 horns** (Multipath LHA-WR28-33) ->
no RX LNA needed (doc 159 sec 4.2b). `--gain 25` models the standard-horn case,
where the EA8 gate fails at 40 GHz at 1 kHz RBW (needs the LNA or <=100 Hz RBW).
The 1-18 GHz mid-band is also tight at 1 kHz (low-gain broadband horns), cleared
by narrowing RBW -- the demo surfaces both.
