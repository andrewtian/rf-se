"""IEEE-299 substitution SE automation (Tier-1, real instruments) -- CLI entry point.

The no-cable 40 GHz / 100 dB acceptance test (doc 147 Path 2, doc 159): source +
TX horn OUTSIDE, analyzer + RX horn INSIDE, nothing across the shield. This is
the computer-automated control loop (doc 159 sec 4a / O3); the HackRF pipeline in
the parent dir is the separate Tier-0 sub-6-GHz screen.

    cd <repo root>
    uv run python rf-se/se299/cli.py demo                  # hardware-free end-to-end (sim)
    uv run python rf-se/se299/cli.py demo --gain 25        # model standard horns (EA8 fails @ 40 GHz)
    uv run python rf-se/se299/cli.py dryrun                # freq plan + budget + exact instrument cmds
    uv run python rf-se/se299/cli.py preflight             # open instruments + *IDN? (needs hardware/pyvisa)
    uv run python rf-se/se299/cli.py capture --phase reference --label run1
    uv run python rf-se/se299/cli.py capture --phase wall      --label run1   # computes SE vs stored reference

Run reference + wall with IDENTICAL settings; only the wall (open vs in place)
changes between them -- the settings_key guard enforces it.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg_mod
import connection
import coordinator
import discover
import drivers
import loop
import probe_sweep
from budget import fspl_db, se_capability_db

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "output")


def _build_cfg(args) -> cfg_mod.Campaign:
    bands = list(cfg_mod.DEFAULT_BANDS)
    if getattr(args, "gain", None) == 25:
        bands[-1] = cfg_mod.WR28_STANDARD_25DBI       # model the standard-horn case
    inst = cfg_mod.Instruments(
        source_addr=getattr(args, "source", "sim"),
        analyzer_addr=getattr(args, "analyzer", "sim"))
    an = cfg_mod.AnalyzerSettings(rbw_hz=getattr(args, "rbw", 1000.0))
    return cfg_mod.Campaign(instruments=inst, analyzer=an,
                            bands=tuple(bands), label=getattr(args, "label", "demo"))


def _print_summary(reference, wall, summary):
    print(f"  points: {summary['n_points']}   EA8 fails: {summary['ea8_fail_count']}"
          f"   verdicts: {summary['verdicts']}")
    print(f"  worst capability: {summary['worst_capability_db']} dB "
          f"at {summary['worst_capability_f_hz'] / 1e9:.2f} GHz")
    print(f"  CAMPAIGN PASS: {summary['campaign_pass']}")
    print(f"  {'f (GHz)':>9} {'cap dB':>7} {'SE dB':>7} {'fl':>3}  verdict")
    for k in sorted(wall):
        r, m = reference[k], wall[k]
        print(f"  {m['f_hz'] / 1e9:9.2f} {r['capability_db']:7.1f} {m['se_reported_db']:7.1f}"
              f" {'Y' if m['floor_limited'] else '-':>3}  {m['verdict']}")


def cmd_demo(args):
    cfg = _build_cfg(args)
    errs = cfg.validate()
    if errs:
        print("CONFIG ERRORS:", *errs, sep="\n  "); return 2
    source, analyzer, bench = drivers.open_instruments(cfg)
    print(f"DEMO (sim)  source={source.idn()}  analyzer={analyzer.idn()}")
    reference, wall = loop.run_demo(cfg, source, analyzer, bench)
    summary = loop.summarize(reference, wall)
    _print_summary(reference, wall, summary)
    out = loop.write_run(os.path.join(DEFAULT_OUT, cfg.label), cfg,
                         reference, wall, summary, note="hardware-free demo (sim)")
    print(f"  wrote {out}/")
    return 0


def cmd_localize(args):
    cfg = _build_cfg(args)
    source, analyzer, bench = drivers.open_instruments(cfg)
    if bench is not None and bench.leak_profile is None:
        bench.leak_profile = drivers.demo_seam_leak()      # sim: a hot seam at 1.2 m
    n = 49
    positions = [round(i * args.span / (n - 1), 4) for i in range(n)]
    rows, peak = loop.localize(cfg, source, analyzer, args.freq * 1e9, positions, bench=bench)
    print(f"LOCALIZE @ {args.freq} GHz  (digital level-vs-position over the bus; "
          f"{len(positions)} pts over {args.span} m)")
    print(f"  HOT SPOT: {peak['level_dbm']:.1f} dBm at {peak['position']:.2f} m")
    lo = min(r["level_dbm"] for r in rows)
    hi = max(r["level_dbm"] for r in rows)
    for r in rows[::2]:
        bar = "#" * int(46 * (r["level_dbm"] - lo) / (hi - lo + 1e-9))
        print(f"  {r['position']:5.2f} m |{bar} {r['level_dbm']:.0f}")
    return 0


def cmd_dryrun(args):
    cfg = _build_cfg(args)
    print("FREQUENCY PLAN + LINK BUDGET (modeled, doc 159 sec 4.2b):")
    print(f"  {'f (GHz)':>9} {'band':<26} {'G dBi':>6} {'Psrc':>5} {'DANL':>6} {'cap dB':>7}")
    for f_hz, b in cfg.frequencies():
        cap = se_capability_db(b.source_power_dbm, b.antenna_gain_dbi, b.antenna_gain_dbi,
                               f_hz, cfg.geometry.separation_m, b.danl_dbm_per_hz,
                               cfg.analyzer.rbw_hz, cfg.margin_db)
        flag = "" if cap >= b.target_se_db else "  <- below target (LNA / lower RBW)"
        print(f"  {f_hz / 1e9:9.2f} {b.name:<26} {b.antenna_gain_dbi:6.0f}"
              f" {b.source_power_dbm:5.0f} {b.danl_dbm_per_hz:6.0f} {cap:7.1f}{flag}")
    print(f"\n  FSPL(40 GHz, {cfg.geometry.separation_m} m) = {fspl_db(40e9, cfg.geometry.separation_m):.1f} dB")
    print("\nEXACT INSTRUMENT COMMANDS (68000-series native; CONFIRMED vs Anritsu 681XXC PM 10370-10334):")
    print("  source 683xx : RST IL1 AT0 | CF1 <GHz> GH | L1 <dBm> DM | RF1 | RF0 | OF1? OL1? OSB?")
    print("  analyzer 856x: SNGLS | RB <Hz>HZ | RL <dBm>DBM | DET POS | SP 0HZ | CF <Hz>HZ | TS | MKPK HI | MKA?")
    print(f"\n  PC3: analyzer link = {cfg.instruments.analyzer_link}")
    return 0


def cmd_preflight(args):
    cfg = _build_cfg(args)
    try:
        source, analyzer, _ = drivers.open_instruments(cfg)
    except Exception as e:  # pragma: no cover - hardware/pyvisa path
        print(f"OPEN FAILED: {e}"); return 1
    print(f"source   {cfg.instruments.source_addr:>28} -> {source.idn()}")
    print(f"analyzer {cfg.instruments.analyzer_addr:>28} -> {analyzer.idn()}")
    print(f"PC3 reminder: analyzer must reach the inside unit over FIBER only "
          f"({cfg.instruments.analyzer_link}).")
    try:
        opts = analyzer.query_options()
        print(f"analyzer options: {', '.join(opts) if opts else '(none reported)'}")
        for need in ("006", "008"):
            print(f"  Opt {need}: {'PRESENT' if need in opts else 'ABSENT (expected present)'}")
        print(f"  Opt 002: {'ABSENT (good; external mixing available)' if '002' not in opts else 'PRESENT (deletes external mixer)'}")
        errs = analyzer.query_errors()
        if errs:
            print(f"  ERROR QUEUE (PC2): {errs} -- resolve before a real run")
    except Exception as e:                               # pragma: no cover - hardware path
        print(f"  (options/errors query unavailable: {e})")
    source.close(); analyzer.close()
    return 0


def _print_sweep_frame(frame, kind):
    ramp = " .:-=+*#%@"
    width = 70
    freqs, levels = frame["freqs_hz"], frame["levels_dbm"]
    n = len(levels)
    cols = []
    for c in range(width):
        a = c * n // width
        b = max(a + 1, (c + 1) * n // width)
        cols.append(max(levels[a:b]))
    lo, hi = min(cols), max(cols)
    rng = (hi - lo) or 1.0
    bar = "".join(ramp[min(len(ramp) - 1, int((v - lo) / rng * (len(ramp) - 1)))] for v in cols)
    print(f"SWEEP  {kind}")
    print(f"  acq_mode={frame['acq_mode']}  purpose={frame['purpose']}  points={n}")
    print(f"  {freqs[0] / 1e9:.3f} [{bar}] {freqs[-1] / 1e9:.3f} GHz")
    print(f"  HOT {frame['hot_freq_hz'] / 1e9:.4f} GHz @ {frame['hot_level_dbm']:.1f} dBm")
    if frame.get("note"):
        print(f"  {frame['note']}")


def cmd_sweep(args):
    cfg = _build_cfg(args)
    source, analyzer, bench = drivers.open_instruments(cfg)
    sweep = cfg_mod.SweepSettings(
        mode=args.mode, span_lo_hz=args.span_lo * 1e9, span_hi_hz=args.span_hi * 1e9,
        n_points=args.points, sweep_time_s=args.sweep_time, attenuation_db=args.atten,
        aunits=args.aunits, video_avg=args.video_avg, max_hold=args.max_hold)
    if bench is not None and bench.se_model is None:
        bench.se_model = drivers.demo_enclosure_se()
    if args.mode == "stepped":
        n = max(2, args.points)
        freqs = [sweep.span_lo_hz + (sweep.span_hi_hz - sweep.span_lo_hz) * i / (n - 1)
                 for i in range(n)]
        analyzer.set_attenuation(db=sweep.attenuation_db)
        frame = loop.stepped_cw_sweep(cfg, source, analyzer, freqs, bench=bench)
        kind = "STEPPED-CW  (acceptance-grade: narrow RBW, high dynamic range)"
    else:
        if bench is not None:
            band = cfg.band_for((sweep.span_lo_hz + sweep.span_hi_hz) / 2)
            bench.gain, bench.danl = band.antenna_gain_dbi, band.danl_dbm_per_hz
            bench.src_rf_on, bench.wall_present = True, True
        try:
            frame = loop.swept_screen(analyzer, sweep, expect_points=601)
        except loop.AcquisitionRejected as e:
            print(f"SWEEP REJECTED (integrity guard): {e}")
            return 2
        kind = "SWEPT-SPAN SCREEN  (lower-bound only, blind to deep leaks)"
    _print_sweep_frame(frame, kind)
    print("  a sweep LOCALIZES; the SE acceptance verdict comes from `capture`.")
    return 0


def cmd_q(args):
    cfg = _build_cfg(args)
    source, analyzer, bench = drivers.open_instruments(cfg)
    if bench is not None and bench.resonance is None:        # sim: a demo resonance to read
        f0 = (args.span_lo + args.span_hi) / 2 * 1e9
        bench.resonance = drivers.demo_cavity_resonance(f0, 5000.0)
    cav = cfg_mod.CavitySettings(span_lo_hz=args.span_lo * 1e9, span_hi_hz=args.span_hi * 1e9,
                                 n_db_down=args.n_db, video_avg=args.video_avg)
    res = loop.cavity_q(analyzer, cav, bench=bench)
    print("CAVITY Q-FACTOR  (both units inside; NOT IEEE-299 -- reference/ holloway-2008)")
    print(f"  f0 = {res['f0_hz'] / 1e9:.4f} GHz    peak {res['peak_dbm']:.1f} dBm")
    print(f"  BW({res['n_db_down']:.0f} dB) = {res['bw_hz'] / 1e6:.4f} MHz")
    print(f"  Q  = {res['q']:.0f}    (linewidth Q = f0 / BW_{res['n_db_down']:.0f}dB)")
    analyzer.close()
    return 0


def cmd_capture(args):
    cfg = _build_cfg(args)
    out_dir = os.path.join(DEFAULT_OUT, cfg.label)
    os.makedirs(out_dir, exist_ok=True)
    ref_path = os.path.join(out_dir, "reference.json")
    keyf = os.path.join(out_dir, "settings_key.json")
    source, analyzer, bench = drivers.open_instruments(cfg)
    if args.phase == "reference":
        print("REFERENCE pass: place the TX/RX horns face-to-face (NO wall), aligned.")
        reference = loop.acquire_reference(cfg, source, analyzer, bench)
        with open(ref_path, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in reference.items()}, fh, indent=2)
        with open(keyf, "w", encoding="utf-8") as fh:
            json.dump(list(cfg.settings_key()[:5]), fh)
        ea8_fail = sum(1 for r in reference.values() if not r["ea8_ok"])
        print(f"  stored {len(reference)} ref points; EA8 fails: {ea8_fail}"
              f"  -> {ref_path}")
        if ea8_fail:
            print("  WARNING (PC6): EA8 gate fails -- the setup cannot SEE the target at "
                  "some frequencies. Lower RBW / add the RX LNA / use higher-gain horns.")
    else:  # wall
        if not os.path.exists(ref_path):
            print(f"NO REFERENCE at {ref_path}; run --phase reference first."); return 1
        if os.path.exists(keyf):
            with open(keyf, encoding="utf-8") as fh:
                if json.load(fh) != list(cfg.settings_key()[:5]):
                    print("ABORT: analyzer settings differ from the reference pass "
                          "(SE is a ratio -- settings must match)."); return 2
        with open(ref_path, encoding="utf-8") as fh:
            reference = {int(k): v for k, v in json.load(fh).items()}
        print("WALL pass: enclosure in place, same geometry.")
        wall = loop.measure_wall(cfg, source, analyzer, reference, bench)
        summary = loop.summarize(reference, wall)
        _print_summary(reference, wall, summary)
        loop.write_run(out_dir, cfg, reference, wall, summary, note="capture: reference+wall")
        print(f"  wrote {out_dir}/")
    source.close(); analyzer.close()
    return 0


# --------------------------------------------------- 8565EC near-field sweeper

def _sim_link(span, retries):
    return connection.AnalyzerLink(
        connection.DEFAULT_8565EC, span,
        discover_fn=discover.sim_inventory,
        open_fn=lambda dev: drivers.SimSpectrumAnalyzer(nf_model=drivers.demo_nearfield_spectrum()),
        retries=retries)


def _visa_link(span, retries, discover_fn):
    return connection.AnalyzerLink(
        connection.DEFAULT_8565EC, span,
        discover_fn=discover_fn,
        open_fn=lambda dev: drivers.Agilent856xEC(drivers.make_transport(dev.address)),
        retries=retries)


def build_analyzer_link(addr, span, retries=3):
    """Automatic connection support: pick the transport from `addr` and wire the
    lifecycle. Returns (link, simulated). 'sim' = forced simulator; a 'net:HOST:PORT:ADDR'
    bridge address or an explicit VISA string = that one address (honest
    ABSENT/INVALID/READY); 'auto'/None = scan the real bus, falling back to the
    simulator only when no instrument is found. A net: address routes through the
    network GPIB bridge (make_transport), so the M1 reaches the 8565EC over TCP."""
    if addr == "sim":
        return _sim_link(span, retries), True
    if addr in (None, "auto"):
        real = discover.discover_visa()
        if any("856" in (d.model or "") for d in real):
            return _visa_link(span, retries, discover_fn=lambda: real), False
        return _sim_link(span, retries), True            # no hardware -> simulate, labeled
    return _visa_link(span, retries,
                      discover_fn=lambda: discover.identify_addr(addr, drivers.make_transport)), False


def _print_link_status(st, simulated):
    flag = ("DETECTED + VALID" if st.valid
            else "DETECTED but INVALID" if st.detected else "NOT DETECTED")
    sim = "  [SIMULATED -- no hardware found]" if simulated else ""
    print(f"8565EC: {flag}{sim}")
    print(f"  state      {st.state}")
    print(f"  model      {st.model or '(none)'}    serial {st.serial or '(none)'}")
    print(f"  transport  {st.transport or '(none)'}    address {st.address or '(none)'}")
    if st.reason and not st.valid:
        print(f"  reason     {st.reason}")


def cmd_detect(args):
    span = (args.span_lo * 1e9, args.span_hi * 1e9)
    link, simulated = build_analyzer_link(args.analyzer, span, retries=args.retries)
    st = link.connect()
    _print_link_status(st, simulated)
    link.close()
    return 0 if st.valid else 1


def cmd_nf_sweep(args):
    span = (args.span_lo * 1e9, args.span_hi * 1e9)
    link, simulated = build_analyzer_link(args.analyzer, span, retries=args.retries)
    print(f"NEAR-FIELD PROBE SWEEP  {args.span_lo}-{args.span_hi} GHz  "
          f"{args.points} pts  (analyzer-as-sweeper, no source)")
    st = link.connect()
    _print_link_status(st, simulated)
    if not link.ensure():
        print("  -> link not usable; nothing to sweep. (Check the address / power / cabling.)")
        link.close()
        return 1
    sweeper = probe_sweep.ProbeSweeper(link, span, n_points=args.points, settle_s=args.interval)
    n = args.sweeps
    print()
    try:
        if n > 0:
            for f in sweeper.run(n):
                print(probe_sweep.render_frame(f, width=args.width)); print()
        else:                                            # continuous until Ctrl-C
            i = 0
            while True:
                f = sweeper.sweep_once(i); i += 1
                if f is not None:
                    print(probe_sweep.render_frame(f, width=args.width)); print()
                if args.interval:
                    import time; time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n(stopped)")
    if sweeper.reconnects:
        print(f"  link auto-reconnected {sweeper.reconnects}x during the run")
    link.close()
    return 0


def cmd_live(args):
    """LIVE 8565EC spectrum -- now the full-fidelity SA panel (alias of `sa`)."""
    return cmd_sa(args)


def _guard_vm_reset(reset, specs, vmmod):
    """--vm-reset recreates the disk overlay (prepare_assets removes disk.qcow2). Refuse to yank it
    out from under a LIVE qemu that still holds it open -- tell the operator to stop it first."""
    if not reset:
        return
    live = [s.name for s in specs if vmmod.instance_is_live(s)]
    if live:
        raise vmmod.BridgeUnavailable(
            "--vm-reset refused: a qemu instance is still live and holds its disk overlay open "
            f"({', '.join(live)}). Stop it first:  uv run python rf-se/se299/cli.py vm-stop  "
            "then re-run with --vm-reset.")


def _ensure_vm_addresses(args):
    """Boot+provision the qemu USB-passthrough VM(s) per --vm-mode and return
    (analyzer_addr, source_addr) as net: bridge addresses. Deployment shapes, ONE workflow:
      --vm-mode singleton : ONE VM, controllers booted EMPTY (no UEFI ASSERT), then HS then B
                            attached SERIALLY post-boot (DEFAULT -- the canonical single-machine
                            topology; degrades if one unit is absent).
      --vm-mode golden    : TWO VMs (analyzer + source), each 1 qemu / 1 adapter (remote / fault-
                            isolation; the two claims race a shared controller on one host).
      --vm-mode both      : ONE VM, at-boot passthrough of both adapters (SUPERSEDED -- UEFI ASSERT
                            + simultaneous-claim fragile).
    No fake. Raises vm.BridgeUnavailable on failure. Shared by the coordinator/se-gui/sa/sg verbs."""
    from gpib_bridge import vm as vmmod
    reset = getattr(args, "vm_reset", False)
    mode = getattr(args, "vm_mode", "singleton")
    timeout = float(getattr(args, "vm_timeout", 240.0))
    stagger = float(getattr(args, "vm_stagger", 25.0))
    # STAGE 2 REACHABILITY: --vm-bind exposes the forwarded bridge ports on a routable interface so a
    # client on ANOTHER host can reach the analyzer/source; the default 127.0.0.1 keeps the single-
    # machine (loopback) behavior. A routable bind REQUIRES --vm-token (or NI_GPIB_TOKEN) -- refuse
    # UP FRONT, before any ioreg scan or boot, mirroring ni_gpib_server.main.
    bind_host = getattr(args, "vm_bind", "127.0.0.1") or "127.0.0.1"
    token = getattr(args, "vm_token", "") or ""
    vmmod.guard_bind_auth(vmmod.VmSpec(bind_host=bind_host, bridge_token=token))

    def _expose(spec):
        return dataclasses.replace(spec, bind_host=bind_host, bridge_token=token)

    devs = vmmod.detect_gpib_usb()
    warn = vmmod._shared_controller_warning(devs)            # R1a: same host-controller reset race
    if warn:
        print(warn)
    if mode == "singleton":
        # ONE VM; boot controllers empty, then attach HS then B SERIALLY (verified, never overlapping
        # the two host-side resets). Both adapters pinned to their host port when detected.
        spec = _expose(vmmod.singleton_spec(base_port=args.vm_port, source_port=args.vm_source_port,
                                            ssh_pubkey=vmmod.default_ssh_pubkey(), devices=devs))
        _guard_vm_reset(reset, [spec], vmmod)
        return vmmod.ensure_singleton(spec, reset=reset, wait_timeout=timeout, log=print)
    if mode == "golden":
        # both VMs pinned to their adapter's physical port when detected (analyzer=B, source=HS) so
        # neither races the shared 0x3923 vendor id and the wedge re-attach knows each port. The
        # two VMs are STAGGERED (analyzer settles, then source launches) so the two 0x3923 FX2
        # fxloads never overlap -- passing both through at once wedges BOTH at -110.
        ana, src = vmmod.golden_pair(base_port=args.vm_port,
                                     ssh_pubkey=vmmod.default_ssh_pubkey(), devices=devs)
        ana, src = _expose(ana), _expose(src)
        _guard_vm_reset(reset, [ana, src], vmmod)
        return vmmod.ensure_golden_pair(ana, src, reset=reset, wait_timeout=timeout,
                                        stagger_seconds=stagger, log=print)
    # both mode: PIN each adapter to its PHYSICAL USB port so qemu FOLLOWS the B's 0x702b(cold)->
    # 0x702a(post-fxload) re-enumeration and so each FX2 wedge re-attach knows its port. A
    # vendor-only match does not re-attach the new-PID device (the guest stays stuck on the dead
    # 0x702b). hostport is stable across the re-enum. Falls back if an adapter is absent.
    b = next((d for d in devs if d.kind == "ni-gpib-b"), None)
    hs = next((d for d in devs if d.kind == "ni-gpib-hs"), None)
    b_match = vmmod.hostport_match(b) if b is not None else ""
    hs_match = vmmod.hostport_match(hs) if hs is not None else ""
    spec = _expose(vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port,
                                ssh_pubkey=vmmod.default_ssh_pubkey(),
                                analyzer_usb_match=b_match, source_usb_match=hs_match))
    _guard_vm_reset(reset, [spec], vmmod)
    if b_match:
        print(f"pinning 8565EC adapter (GPIB-USB-B) to its USB port: {b_match}")
    if hs_match:
        print(f"pinning 68369A adapter (GPIB-USB-HS) to its USB port: {hs_match}")
    vmmod.ensure_bridge(spec, require_both=True, reset=reset, wait_timeout=timeout, log=print)
    return spec.analyzer_net_addr, spec.source_net_addr


def cmd_vm_stop(args):
    """Stop running qemu bridge instance(s). R5 (surgical + health-aware): a BARE `vm-stop` NEVER
    tears down a HEALTHY (reachable) instance -- it only reaps wedged/stale qemus; pass an explicit
    --name to force-stop a specific instance even if healthy. Prints which were stopped vs skipped
    vs not-running; only unlinks a pidfile it actually stopped. Run before --vm-reset if it refuses."""
    from gpib_bridge import vm as vmmod
    explicit = getattr(args, "name", None)
    if explicit:
        specs = [vmmod.VmSpec(name=nm) for nm in explicit]   # surgical: force-stop the named ones
        force = True
    else:
        ana, src = vmmod.golden_pair()                       # correct per-instance roles/ports for the
        specs = [vmmod.singleton_spec(), ana, src,           # health check: singleton (default) first,
                 vmmod.VmSpec(name="se299-gpib")]            # then golden rx/tx, then both
        force = False
    buckets = {"stopped": [], "skipped-healthy": [], "not-running": []}
    for spec in specs:
        st = vmmod.stop_instance(spec, force=force, log=print)
        buckets.setdefault(st, []).append(spec.name)

    def _fmt(x):
        return ", ".join(x) if x else "-"
    print(f"vm-stop ({'surgical --name' if force else 'health-aware'}): "
          f"stopped [{_fmt(buckets['stopped'])}]; "
          f"skipped-healthy [{_fmt(buckets['skipped-healthy'])}]; "
          f"not-running [{_fmt(buckets['not-running'])}]")
    return 0


def cmd_coordinator(args):
    """SE-coordinator instance: own both units (rx+tx), run the substitution campaign, and
    publish the live SE figure over telemetry so dashboard instances can watch (R8/R9)."""
    import control_plane
    import discovery
    import roles
    cfg = _build_cfg(args)
    if getattr(args, "vm_plan", False):
        from gpib_bridge import vm as vmmod
        print(vmmod.launch_plan(vmmod.VmSpec(port=args.vm_port,
                                             source_port=args.vm_source_port)))
        return 0
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            args.analyzer, args.source = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    cid = drivers.client_id(role="coordinator")             # announced to each bridge (session registry)
    if args.source == "sim" and args.analyzer == "sim":
        cp = control_plane.simulated(cfg)
    elif getattr(args, "discover", False):
        beacons = discovery.discover(timeout_s=args.discover_timeout)
        cp = control_plane.from_beacons(cfg, beacons, client_id=cid)
        print(f"discovery: {len(beacons)} bridge(s) answered")
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=args.analyzer, tx_addr=args.source,
                                          client_id=cid)
    try:
        role = roles.CoordinatorRole(cp, telemetry_port=args.telemetry_port,
                                     telemetry_host=args.telemetry_bind).start()
    except control_plane.ControlPlaneError as e:
        print(f"cannot form an rx+tx pair: {e}")
        return 1
    print(f"coordinator: telemetry on {role.telemetry_host}:{role.telemetry_port}   roster:")
    for u in cp.roster():
        print(f"  [{u['kind']}] {u['label']:10s} {u['address']:26s} "
              f"caps={','.join(u['capabilities'])}")
    if getattr(args, "wait_subscribers", 0):
        print(f"  waiting for {args.wait_subscribers} dashboard subscriber(s)...")
        role.hub.wait_subscribers(args.wait_subscribers, timeout_s=args.wait_timeout)
    last = {"se": None}

    def on_se(fig, row):                                     # print only when the worst case tightens
        if fig["se_db"] != last["se"]:
            last["se"] = fig["se_db"]
            rel = ">=" if fig["lower_bound"] else "="
            print(f"  live worst SE {rel} {fig['se_db']:.1f} dB "
                  f"@ {row['f_hz'] / 1e9:.2f} GHz ({fig['points']} pts)")

    try:
        if not role.coord.ensure_ready():
            print("  instruments not READY; aborting (no fake). Point --source/--analyzer at "
                  "live instruments or a net:HOST:PORT:PAD bridge, or use sim.")
            return 1
        # arm floors BEFORE the sweep/campaign leases the bus (apply_now=False -> no bus write here;
        # the campaign's own configure()/set_attenuation applies the atten floor under its lease).
        _maybe_arm_direct_chain(args, role.coord, apply_now=False)
        if getattr(args, "sweep", False):
            # TWO-INSTANCE DRIVE: instance 1 (this coordinator, RX) sweeps DC-to-40-GHz; the TX
            # source on instance 2 FOLLOWS every point through the stack (source_tracked).
            import config as _cfgmod
            role.coord.cfg = dataclasses.replace(role.coord.cfg, bands=_cfgmod.DC_TO_40GHZ_BANDS)
            res = role.coord.sweep(bench=getattr(cp, "bench", None))
            fr = res["freqs_hz"]
            print(f"  DC-40 sweep: {len(fr)} pts  {min(fr) / 1e6:.1f} MHz -> {max(fr) / 1e9:.1f} GHz  "
                  f"TX source_tracked={res['source_tracked']} ({res['tracking']})")
            print(f"  hot bin: {res['hot_freq_hz'] / 1e9:.3f} GHz @ {res['hot_level_dbm']:.1f} dBm")
            return 0
        result = role.run_campaign(on_se_update=on_se)
    finally:
        role.stop()
    s = result["summary"]
    print(f"  CAMPAIGN PASS: {s['campaign_pass']}   worst SE figure: "
          f"{result['se_figure']['se_db']:.1f} dB")
    return 0


def _parse_session_line(line):
    """Parse one S-verb session line into a dict, or None if it is not a session line. The line
    is strictly field-tagged with space-free values:
      session <sid> client <cid|-> peer <ip:port|-> role <role|-> pad <pad|-> lease <scope|->
    so a flat split into key/value pairs recovers every field."""
    toks = line.split()
    d = {}
    i = 0
    while i + 1 < len(toks):
        d[toks[i]] = toks[i + 1]
        i += 2
    if "session" not in d:
        return None
    return {"sid": d.get("session", ""), "client_id": d.get("client", ""),
            "peer": d.get("peer", ""), "role": d.get("role", ""),
            "pad": d.get("pad", ""), "lease": d.get("lease", "")}


def _probe_device(kind, addr, our_cid=None):
    """Live-probe one network device (net:HOST:PORT:PAD): reachability, the bridge lease table
    (R verb) + session table (S verb -- every connected client), and the instrument IDN when the
    bus is free. `our_cid` (if given) is announced to the bridge so OUR OWN probe session appears
    in the S table (server-authoritative LOCAL marking). Returns a dict; never raises."""
    import drivers
    out = {"kind": kind, "addr": addr, "reachable": False, "idn": "", "leases": "",
           "holders": [], "sessions": [], "sessions_supported": False,
           "model": {"rx": "8565EC", "tx": "68367C"}.get(kind, "?")}
    if not str(addr).startswith("net:"):
        out["idn"] = "(non-network addr)"
        return out
    try:
        t = drivers.make_transport(addr, client_id=our_cid)
    except Exception as e:                                   # noqa: BLE001
        out["idn"] = f"(no transport: {e})"
        return out
    try:
        rep = t.lease_report()                              # R verb: who holds the lease (observer)
        out["reachable"] = True
        out["leases"] = rep.strip()
        for ln in rep.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[0] == "session":
                out["holders"].append(parts[1])
        try:                                                # S verb: every connected session
            srep = t.sessions_report()                      # raises IOError on a bridge predating S
            out["sessions_supported"] = True
            for ln in srep.splitlines():
                s = _parse_session_line(ln)
                if s is not None:
                    out["sessions"].append(s)
        except Exception:                                   # noqa: BLE001 -- old bridge / minimal fake
            out["sessions_supported"] = False
        if "no active leases" in rep:                       # bus free -> safe to read IDN
            try:
                drv = drivers.Agilent856xEC(t) if kind == "rx" else drivers.Anritsu68369(t)
                out["idn"] = drv.idn().strip()
            except Exception as e:                          # noqa: BLE001
                out["idn"] = f"(read err: {e})"
        else:
            out["idn"] = "(leased -- IDN skipped)"
    except Exception as e:                                  # noqa: BLE001
        out["idn"] = f"(unreachable: {e})"
    finally:
        try:
            t.close()
        except Exception:
            pass
    return out


def cmd_devices(args):
    """Show the network-connected DEVICES (instruments behind bridge instances) and the CLIENTS
    operating them. Canonical model: each device is reached over the network via a bridge instance
    (net:HOST:PORT:PAD); ANY client may discover + operate any device; exclusive control is
    arbitrated by the bridge lease table (the R verb) -- the authoritative source of 'who operates
    what'. Endpoints come from --analyzer/--source, a --device list, or --discover (UDP beacons)."""
    endpoints = []                                          # [(kind, addr)]
    if getattr(args, "analyzer", None):
        endpoints.append(("rx", args.analyzer))
    if getattr(args, "source", None):
        endpoints.append(("tx", args.source))
    for d in (getattr(args, "device", None) or []):
        endpoints.append(("?", d))
    if getattr(args, "discover", False):
        import discovery
        for b in discovery.discover(timeout_s=getattr(args, "discover_timeout", 1.0)):
            for inst in b.instruments:
                endpoints.append((inst.get("kind", "?"),
                                  f"net:{b.host}:{b.port}:{inst.get('pad', 0)}"))
    if not endpoints:
        print("no device endpoints. give --analyzer/--source net:HOST:PORT:PAD, --device ..., or --discover")
        return 1

    import identity
    our_cid = drivers.client_id(role="devices")             # announce our own probe (LOCAL marking)
    our_gk = identity.group_key(our_cid)
    devices = [_probe_device(k, a, our_cid=our_cid) for k, a in endpoints]

    print(f"\nNETWORK DEVICES ({len(devices)} instrument instance(s) on the bus):")
    print(f"  {'KIND':<4} {'MODEL':<8} {'ADDRESS':<26} {'STATUS':<10} {'CONTROL':<18} IDN")
    for d in devices:
        status = "REACHABLE" if d["reachable"] else "DOWN"
        control = ("FREE (open)" if not d["holders"]
                   else "LEASED by session " + ",".join(d["holders"]))
        print(f"  {d['kind']:<4} {d['model']:<8} {d['addr']:<26} {status:<10} {control:<18} {d['idn']}")

    _print_clients_section(devices, our_gk)
    return 0


def _print_clients_section(devices, our_gk):
    """Print the CLIENTS view, grouped by client. When any bridge exposes the session table (S
    verb), group EVERY connected session (controllers AND observers) by client identity and mark
    the LOCAL client. When no bridge does (all predate the session registry), fall back to the
    lease-table-only view (controllers by session id)."""
    any_sessions = any(d.get("sessions_supported") for d in devices)
    if not any_sessions:                                    # backward-compat: R-only (old bridges)
        holders_by_dev = {}
        for d in devices:
            for h in d["holders"]:
                holders_by_dev.setdefault(h, []).append(f"{d['kind']}:{d['model']}")
        print("\nCLIENTS (processes operating/observing the devices):")
        if holders_by_dev:
            for sess, devs in sorted(holders_by_dev.items()):
                print(f"  session {sess:<6} CONTROLLER  -- exclusive lease on {', '.join(devs)}")
        else:
            print("  (no controller -- the bus is FREE; any client may acquire a lease and operate)")
        print("  this client   OBSERVER    -- `devices` query (read the lease table; no bus lease held)")
        n_ctrl = len(holders_by_dev)
        print(f"\n  {len(devices)} device(s) | {n_ctrl} controller client(s) + 1 observer (this query)")
        return

    import identity
    # group every session across every device by its client identity (u= key). A session with no
    # announced identity ('-') can't be correlated across bridges, so it is its own group.
    groups = {}                                             # gk -> {cid, controls[], observes[]}
    for d in devices:
        dev_label = f"{d['kind']}:{d['model']}"
        for s in d.get("sessions", []):
            cid = s["client_id"]
            gk = identity.group_key(cid) if cid and cid != "-" else f"anon:{d['addr']}:{s['sid']}"
            g = groups.setdefault(gk, {"cid": cid, "controls": [], "observes": []})
            if (not g["cid"] or g["cid"] == "-") and cid and cid != "-":
                g["cid"] = cid                              # prefer a real id if any session has one
            bucket = "controls" if s["lease"] not in ("", "-") else "observes"
            g[bucket].append(dev_label)

    print("\nCLIENTS (all sessions on the devices, grouped by client):")
    n_ctrl = 0
    for gk in sorted(groups):
        g = groups[gk]
        cid = g["cid"]
        info = (identity.parse_client_id(cid) if cid and cid != "-"
                else {"role": "?", "host": "?", "pid": "?", "u": gk})
        is_local = bool(cid and cid != "-" and identity.group_key(cid) == our_gk)
        tag = "   (LOCAL -- this devices query)" if is_local else ""
        if g["controls"]:
            n_ctrl += 1
        print(f"  [{info['role'] or '?'}] host={info['host'] or '?'} pid={info['pid'] or '?'} "
              f"u={info['u'] or gk}{tag}")
        if g["controls"]:
            print(f"      CONTROLS: {', '.join(sorted(set(g['controls'])))}")
        if g["observes"]:
            print(f"      OBSERVES: {', '.join(sorted(set(g['observes'])))}")

    # devices whose bridge predates the session registry: fold their lease holders in (no identity)
    for d in devices:
        if not d.get("sessions_supported") and d["holders"]:
            print(f"  [unidentified] controller session(s) {', '.join(d['holders'])} on "
                  f"{d['kind']}:{d['model']} (bridge predates session registry -- restart to identify)")

    print("\n  NOTE: a telemetry-only observer (dashboard) opens no bridge socket, so it is not")
    print("  listed here; such observers are tracked via the coordinator's telemetry roster.")
    print(f"\n  {len(devices)} device(s) | {len(groups)} client(s) connected "
          f"({n_ctrl} controlling), incl. this LOCAL query")


def cmd_dashboard(args):
    """Observer instance: subscribe to a coordinator's telemetry and print the live SE figure
    as it is measured -- no bus contact (the coordinator owns the instruments)."""
    import roles
    import telemetry
    host, _, port = args.telemetry.rpartition(":")
    host = host or "127.0.0.1"
    dash = roles.DashboardRole()
    try:
        sub = telemetry.TelemetrySubscriber(host, int(port))
    except OSError as e:
        print(f"cannot reach coordinator telemetry at {host}:{port}: {e}")
        return 1
    print(f"dashboard: subscribed to {host}:{port}  (Ctrl-C to stop)")
    try:
        while True:
            msg = sub.recv_one(timeout_s=args.timeout)
            if msg is None:
                if dash.summary is not None:
                    break                                   # campaign finished
                continue
            dash.on_message(msg.get("topic"), msg.get("data"))
            topic = msg.get("topic")
            if topic == "roster":
                print("  roster: " + ", ".join(f"{u['kind']}:{u['label']}" for u in dash.roster))
            elif topic == "se":
                print("  " + dash.se_text())
            elif topic == "summary":
                print(f"  SUMMARY campaign_pass={dash.summary.get('campaign_pass')}")
    except KeyboardInterrupt:
        pass
    finally:
        sub.close()
    return 0


def _maybe_arm_direct_chain(args, coord, apply_now=True):
    """If --topology direct, arm 8565EC input protection for a TX cabled straight to the analyzer:
    cap the source and floor the input attenuation, PROVING connector <= +30 dBm (1 W) and first
    mixer <= +20 dBm before any tone. No-op for the default radiated topology (horns + path loss
    already protect the RX). Returns the armed envelope, or None."""
    if getattr(args, "topology", "radiated") != "direct":
        return None
    env = drivers.arm_direct_chain(coord.source, coord.analyzer, apply_now=apply_now)
    print(f"  [topology=direct] 8565EC PROTECTED: source cap {env['source_cap_dbm']:.1f} dBm, input "
          f"atten >= {env['rx_min_atten_db']:.0f} dB -> connector {env['connector_dbm']:.1f} dBm "
          f"(<= +30 dBm/1W), first mixer {env['mixer_dbm']:.1f} dBm (<= +20 dBm)")
    return env


def cmd_checkpath(args):
    """RF-path self-test: transmit a tone at a few frequencies and confirm the RX sees it rise
    above the noise floor. Run this FIRST -- it catches a dead/open RF path (loose source-out
    cable, disconnected antenna feed) before any SE number is trusted, instead of silently
    reporting SE = 0. Prints a PATH-LIVE / NO-COUPLING verdict + localization guidance."""
    drivers.set_client_role("checkpath")                    # process client role (session registry)
    import control_plane
    import loop
    cfg = _build_cfg(args)
    analyzer_addr, source_addr = args.analyzer, args.source
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    if source_addr == "sim" and analyzer_addr == "sim":
        cp = control_plane.simulated(cfg)
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                          client_id=drivers.client_id())  # role stamped at fn entry
    coord = cp.make_coordinator()
    if not coord.ensure_ready():
        print("  instruments not READY; aborting (no fake).")
        return 1
    lo, hi = args.span_lo * 1e9, args.span_hi * 1e9
    n = max(2, args.points)
    freqs = [lo + (hi - lo) * k / (n - 1) for k in range(n)]
    print(f"checkpath: analyzer={analyzer_addr}  source={source_addr}  "
          f"TX={cfg.bands[0].source_power_dbm:.0f} dBm  {n} pts {args.span_lo}-{args.span_hi} GHz")
    try:
        coord.take_control()                                # all-or-nothing; clean raise if a rival holds one
    except coordinator.ControlConflict as e:
        print(f"  cannot take exclusive control: {e}")
        return 1
    try:
        _maybe_arm_direct_chain(args, coord)                # protect the 8565EC BEFORE any tone (lease held)
        res = coord.check_path(freqs, bench=getattr(cp, "bench", None), guard_db=args.guard)
    finally:
        coord.release_control()
    print(f"\n  {'f (GHz)':>9} {'off1':>7} {'TX on':>7} {'off2':>7} {'delta':>7}  couples")
    for r in res["rows"]:
        print(f"  {r['f_hz'] / 1e9:9.2f} {r.get('tx_off1_dbm', r['tx_off_dbm']):7.1f} "
              f"{r['tx_on_dbm']:7.1f} {r.get('tx_off2_dbm', r['tx_off_dbm']):7.1f} "
              f"{r['delta_db']:7.1f}  {'YES' if r['couples'] else 'no'}")
    amb = res["max_ambient_dbm"]
    print(f"\n  VERDICT: {res['verdict']}  ({res['n_couple']}/{res['n']} freqs couple, "
          f"guard {res['guard_db']:.0f} dB)")
    print(f"  RX ambient (TX-off) max: {amb:.1f} dBm  "
          f"({'RX picks up ambient -> RX path likely live' if amb is not None and amb > -80 else 'RX at thermal floor'})")
    if res["verdict"] == "NO-COUPLING":
        print("  -> our tone never reaches the RX. Check, in order: source RF-OUT connector; "
              "cable to the TX (LPDA) feed; RX (horn) feed; analyzer RF-IN. Reseat each SMA; "
              "confirm the source front-panel RF indicator is lit.")
        return 2
    return 0


def cmd_chain(args):
    """Chain-continuity sweep: ramp both units UP the preset bands (low -> high), confirm the TX
    is transmitting AT setpoint before the RX reads each point, and validate emitted-vs-received
    across the whole plan. A fast go/no-go on the TX->RX signal chain that precedes an SE campaign
    (NOT an SE number). Prints a per-point + per-band roll-up and a CHAIN-LIVE/PARTIAL/NO-COUPLING
    verdict. --bands selects the preset plan; --topology direct arms 8565EC protection first."""
    drivers.set_client_role("chain")
    import control_plane
    import config as _cfgmod
    cfg = _build_cfg(args)
    if getattr(args, "bands", "dc-40") == "dc-40":
        cfg = dataclasses.replace(cfg, bands=_cfgmod.DC_TO_40GHZ_BANDS)
    elif args.bands == "1-40":
        cfg = dataclasses.replace(cfg, bands=_cfgmod.DEFAULT_BANDS)
    analyzer_addr, source_addr = args.analyzer, args.source
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    if source_addr == "sim" and analyzer_addr == "sim":
        cp = control_plane.simulated(cfg)
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                          client_id=drivers.client_id())
    coord = cp.make_coordinator()
    if not coord.ensure_ready():
        print("  instruments not READY; aborting (no fake).")
        return 1
    npts = len(cfg.frequencies())
    print(f"chain: analyzer={analyzer_addr}  source={source_addr}  {len(cfg.bands)} band(s), "
          f"{npts} pts low->high  guard {args.guard:.0f} dB")
    try:
        coord.take_control()                                # all-or-nothing; clean raise if a rival holds one
    except coordinator.ControlConflict as e:
        print(f"  cannot take exclusive control: {e}")
        return 1
    try:
        _maybe_arm_direct_chain(args, coord)                # protect the 8565EC BEFORE any tone
        res = coord.chain(bench=getattr(cp, "bench", None), guard_db=args.guard,
                          settle_s=(args.settle_ms / 1e3 if args.settle_ms else None))
    finally:
        coord.release_control()
    print(f"\n  {'f (GHz)':>9} {'TX dBm':>7} {'floor':>7} {'tone':>7} {'delta':>7} {'set?':>5}  carries")
    for r in res["rows"]:
        print(f"  {r['f_hz'] / 1e9:9.3f} {r['src_power_dbm']:7.1f} {r['floor_dbm']:7.1f} "
              f"{r['tone_dbm']:7.1f} {r['delta_db']:7.1f} {'OSB' if r['settle_confirmed'] else '.':>5}  "
              f"{'YES' if r['couples'] else 'no'}")
    print("\n  per band (emitted -> received):")
    for b in res["bands"]:
        print(f"    {b['band']:26s} {b['f_lo_hz'] / 1e9:6.3f}-{b['f_hi_hz'] / 1e9:6.3f} GHz  "
              f"{b['n_couple']}/{b['n']} carry")
    print(f"\n  VERDICT: {res['verdict']}  ({res['n_couple']}/{res['n']} points carry, "
          f"guard {res['guard_db']:.0f} dB)")
    if res["verdict"] == "NO-COUPLING":
        print("  -> the chain carries the tone nowhere. Check source RF-OUT, the TX/RX cabling, "
              "and the analyzer RF-IN; reseat each SMA and confirm the source RF indicator is lit.")
        return 2
    return 0 if res["chain_live"] else 3


def cmd_calibrate(args):
    """Capture a CALIBRATION: the reference pass in the CURRENT geometry (both antennas inside
    the enclosure), persisted + quality-graded, loadable later as the reference for a wall pass.

    Records per f the KNOWN TX power (calibrated source-output level -- see note), the RX-measured
    reference, the noise floor, the through-path coupling, and the EA8 capability, then grades the
    calibration USABLE / PARTIAL / FLOOR-LIMITED. A floor-limited cal (the expected result for a
    weak in-enclosure link) is still saved and still yields valid SE lower bounds -- TX cancels in
    SE = reference - wall, so the reference need only be STABLE, not strong."""
    drivers.set_client_role("calibrate")                    # process client role (session registry)
    import control_plane
    import loop
    cfg = _build_cfg(args)
    if getattr(args, "vm_plan", False):
        from gpib_bridge import vm as vmmod
        print(vmmod.launch_plan(vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port)))
        return 0
    analyzer_addr, source_addr = args.analyzer, args.source
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    if source_addr == "sim" and analyzer_addr == "sim":
        cp = control_plane.simulated(cfg)
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                          client_id=drivers.client_id())  # role stamped at fn entry
    coord = cp.make_coordinator()
    if not coord.ensure_ready():
        print("  instruments not READY; aborting (no fake). Point --source/--analyzer at live "
              "instruments or a net: bridge, or use sim.")
        return 1
    print(f"calibrate: analyzer={analyzer_addr}  source={source_addr}  (reference pass, "
          f"antennas in current geometry)")
    try:
        coord.take_control()                                # all-or-nothing; clean raise if a rival holds one
    except coordinator.ControlConflict as e:
        print(f"  cannot take exclusive control: {e}")
        return 1
    try:
        reference = coord.acquire_reference(bench=getattr(cp, "bench", None))
    finally:
        coord.release_control()
    summary = loop.calibration_summary(reference)
    # per-frequency table: KNOWN TX, RX reference, floor, coupling, capability, EA8
    print(f"\n  {'f (GHz)':>9} {'TX set':>7} {'RX ref':>8} {'floor':>8} {'coupl':>7} "
          f"{'cap':>6} {'EA8':>4}")
    for k in sorted(reference):
        r = reference[k]
        up = (r["ref_dbm"] - r["floor_dbm"]) >= summary["floor_guard_db"]
        print(f"  {r['f_hz'] / 1e9:9.2f} {r['src_power_dbm']:7.1f} {r['ref_dbm']:8.1f} "
              f"{r['floor_dbm']:8.1f} {r['coupling_db']:7.1f} {r['capability_db']:6.1f} "
              f"{'yes' if r['ea8_ok'] else 'no':>4}{'' if up else '   (at floor)'}")
    mc = summary["median_coupling_db"]
    print(f"\n  calibration STATUS: {summary['status']}  "
          f"({summary['n_above_floor']}/{summary['n_points']} points above floor)")
    print(f"  known TX (source-output level): {summary['src_power_dbm']:.1f} dBm  |  "
          f"median measured coupling: {mc if mc is None else f'{mc:.1f} dB'}")
    if summary["status"] == "FLOOR-LIMITED":
        print("  NOTE: reference is at the noise floor everywhere -- the in-enclosure link is not "
              "coupling. This cal yields SE LOWER BOUNDS only. Check the RF path (source-out / "
              "antenna feeds / analyzer-in); raise TX power or drop RBW to gain range.")
    out = getattr(args, "out", "") or f"calibration-{args.label}.json"
    loop.write_calibration(out, cfg, reference, summary, note=args.label)
    print(f"  saved -> {out}")
    return 0


def cmd_validate(args):
    """Executable VERIFICATION OF ADHERENCE: run the automatable per-device validation sequence
    (EQUIPMENT_VALIDATION.md) against the instruments and report each step PASS/FAIL/NA with its
    manual citation. Run before a campaign to confirm both units operate correctly. (The physical
    8565E 300 MHz CAL-OUT amplitude check + RF coupling remain operator steps.)"""
    drivers.set_client_role("validate")                     # process client role (session registry)
    import control_plane
    import loop
    cfg = _build_cfg(args)
    analyzer_addr, source_addr = args.analyzer, args.source
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    if source_addr == "sim" and analyzer_addr == "sim":
        cp = control_plane.simulated(cfg)
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                          client_id=drivers.client_id())  # role stamped at fn entry
    coord = cp.make_coordinator()
    if not coord.ensure_ready():
        print("  instruments not READY; aborting (no fake).")
        return 1
    print(f"validate: analyzer={analyzer_addr}  source={source_addr}  probe={args.probe} GHz")
    try:
        coord.take_control()                                # all-or-nothing; clean raise if a rival holds one
    except coordinator.ControlConflict as e:
        print(f"  cannot take exclusive control: {e}")
        return 1
    try:
        res = loop.validate_devices(cfg, coord.source, coord.analyzer,
                                    bench=getattr(cp, "bench", None), probe_hz=args.probe * 1e9)
    finally:
        coord.release_control()
    print(f"\n  {'step':<6} {'status':<5} {'check':<44} cite")
    for c in res["checks"]:
        mark = {"PASS": "ok ", "FAIL": "XX ", "NA": " - ", "WARN": "! "}[c["status"]]
        print(f"  {c['id']:<6} {mark:<5} {c['name'][:44]:<44} {c['cite']}"
              + (f"   [{c['detail']}]" if c["detail"] else ""))
    print(f"\n  ADHERENCE: {'ALL PASS' if res['all_pass'] else 'FAILURES PRESENT'}  "
          f"({res['n_pass']} pass / {res['n_fail']} fail / {res.get('n_warn', 0)} warn / "
          f"{res['n_na']} n/a)")
    return 0 if res["all_pass"] else 2


def cmd_wall(args):
    """Second half of the two-step campaign: LOAD a saved calibration (the reference pass) and run
    the WALL pass against it, computing SE(f) = reference(f) - wall(f). Use the SAME --gain as the
    calibration so the band plan + settings match (measure_wall asserts matched frequencies)."""
    drivers.set_client_role("wall")                          # process client role (session registry)
    import control_plane
    import loop
    cfg = _build_cfg(args)
    try:
        reference = loop.load_calibration(args.calibration)
    except (OSError, loop.AcquisitionRejected) as e:
        print(f"cannot load calibration {args.calibration!r}: {e}")
        return 1
    analyzer_addr, source_addr = args.analyzer, args.source
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    if source_addr == "sim" and analyzer_addr == "sim":
        cp = control_plane.simulated(cfg)
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                          client_id=drivers.client_id())  # role stamped at fn entry
    coord = cp.make_coordinator()
    if not coord.ensure_ready():
        print("  instruments not READY; aborting (no fake).")
        return 1
    print(f"wall: analyzer={analyzer_addr}  source={source_addr}  reference={args.calibration} "
          f"({len(reference)} pts)")
    try:
        coord.take_control()                                # all-or-nothing; clean raise if a rival holds one
    except coordinator.ControlConflict as e:
        print(f"  cannot take exclusive control: {e}")
        return 1
    try:
        wall = coord.measure_wall(reference, bench=getattr(cp, "bench", None))
    finally:
        coord.release_control()
    summary = loop.summarize(reference, wall)
    print(f"\n  {'f (GHz)':>9} {'ref':>8} {'wall':>8} {'SE':>8} {'verdict':>12}")
    for k in sorted(wall):
        r, w = reference[k], wall[k]
        rel = ">=" if w["floor_limited"] else "= "
        print(f"  {w['f_hz'] / 1e9:9.2f} {r['ref_dbm']:8.1f} {w['wall_dbm']:8.1f} "
              f"{rel}{w['se_reported_db']:6.1f} {w['verdict']:>12}")
    print(f"\n  CAMPAIGN PASS: {summary['campaign_pass']}   EA8 fails: {summary['ea8_fail_count']}"
          f"   worst capability: {summary['worst_capability_db']:.1f} dB @ "
          f"{summary['worst_capability_f_hz'] / 1e9:.2f} GHz")
    if getattr(args, "out_dir", ""):
        loop.write_run(args.out_dir, cfg, reference, wall, summary, note=args.label)
        print(f"  run written -> {args.out_dir}/")
    return 0


def cmd_walkaround(args):
    """Live NEAR-FIELD-PROBE WALKAROUND GUI: the operator walks the enclosure with a near-field
    probe on the 8565EC while the 68367C transmits a fixed CW tone; the GUI shows the probe level
    live, holds the peak, colours by heat, and logs marked hot spots. Both units networked
    (net:HOST:PORT:PAD | --vm/golden). Leak LOCALIZATION -- run after se-gui/wall shows a low-SE f."""
    import walkaround
    analyzer_addr, source_addr = args.analyzer, args.source
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    model, gui = walkaround.build_walkaround(analyzer_addr, source_addr, freq_hz=args.freq * 1e9,
                                             gain_dbi=args.gain, rbw_hz=args.rbw,
                                             client_id=drivers.client_id(role="walkaround"))
    print(f"walkaround: analyzer={analyzer_addr}  source={source_addr}  freq={args.freq} GHz")
    print("  press Start, then walk the near-field probe over seams/gaskets/penetrations; the "
          "readout goes RED at a leak. Mark logs the current spot; Reset peak restarts the max-hold.")
    gui.run(interval_ms=args.interval_ms)
    return 0


def cmd_se_gui(args):
    """Live SE figure GUI: watch the concurrent dual-unit substitution campaign paint the SE(f)
    curve in dB, with operator controls (Run/Stop, top-band gain, RBW) to conduct the test. The
    same source/analyzer resolution as coordinator: sim | net:HOST:PORT:PAD | the qemu --vm /
    --vm-mode golden bring-up. No fake in the runtime path."""
    import se_gui
    if getattr(args, "vm_plan", False):
        from gpib_bridge import vm as vmmod
        print(vmmod.launch_plan(vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port)))
        return 0
    analyzer_addr, source_addr = args.analyzer, args.source
    vm_spec = None                                       # LOCAL --vm only -> the owner wires 44.2 recovery
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            analyzer_addr, source_addr = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
        vm_spec = vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port)
    model, gui = se_gui.build_se_gui(analyzer_addr, source_addr,
                                     gain_dbi=args.gain, rbw_hz=args.rbw,
                                     client_id=drivers.client_id(role="se-gui"),
                                     out_dir=(getattr(args, "out_dir", "") or None),
                                     vm_spec=vm_spec)
    # seed the operator sweep-band + tone controls from the CLI (also settable in the GUI window)
    if getattr(args, "span_lo", None) is not None and getattr(args, "span_hi", None) is not None:
        gui.seed_span(args.span_lo, args.span_hi)
    if getattr(args, "power", None) is not None:
        gui.seed_power(args.power)
    print(f"se-gui: analyzer={analyzer_addr}  source={source_addr}")
    print("  set the sweep band + tone in the window (or --span-lo/--span-hi/--power), press Run; "
          "live SE(f) paints as it measures.")
    gui.run(interval_ms=args.interval_ms)
    return 0


def _run_panel_standalone(panel, title, interval_ms=200):
    """Wrap a panel widget in a QMainWindow, start it, and run the Qt loop (blocks until close)."""
    from PySide6 import QtWidgets
    import qt_common
    win = QtWidgets.QMainWindow()
    win.setWindowTitle(title)
    win.setCentralWidget(panel.widget)
    orig = win.closeEvent
    win.closeEvent = lambda ev: (panel.stop(), orig(ev))
    panel.start(interval_ms)
    return qt_common.run_live(win, lambda: None, 200)


def cmd_sa(args):
    """Standalone full-fidelity Spectrum Analyzer window (net:/sim). Supersedes `live`. Same
    --vm/--vm-plan qemu bring-up as se-gui/coordinator (Task 10 fix: this used to ignore it)."""
    import sa_gui
    import bench_gui
    if getattr(args, "vm_plan", False):
        from gpib_bridge import vm as vmmod
        print(vmmod.launch_plan(vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port)))
        return 0
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            args.analyzer, args.source = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    hub = bench_gui.build_bench(getattr(args, "analyzer", "sim"),
                                getattr(args, "source", "sim"),
                                client_id=drivers.client_id(role="sa")).hub
    panel = sa_gui.build_sa_panel(hub)
    print(f"sa: analyzer={getattr(args, 'analyzer', 'sim')} (Qt window; close to stop)")
    _run_panel_standalone(panel, "se299 spectrum analyzer", getattr(args, "interval_ms", 200))
    panel.stop()          # idempotent: real close already stopped it; guarantees the engine
    hub.shutdown()         # thread/timer are down before the hub releases leases (no orphan thread)
    return 0


def cmd_sg(args):
    """Standalone Signal Generator window (net:/sim). RF defaults off. Same --vm/--vm-plan qemu
    bring-up as se-gui/coordinator (Task 10 fix: this used to ignore it)."""
    import sg_gui
    import bench_gui
    if getattr(args, "vm_plan", False):
        from gpib_bridge import vm as vmmod
        print(vmmod.launch_plan(vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port)))
        return 0
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            args.analyzer, args.source = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    hub = bench_gui.build_bench(getattr(args, "analyzer", "sim"),
                                getattr(args, "source", "sim"),
                                client_id=drivers.client_id(role="sg")).hub
    panel = sg_gui.build_sg_panel(hub)
    print(f"sg: source={getattr(args, 'source', 'sim')}  RF defaults OFF (Qt window; close to stop)")
    _run_panel_standalone(panel, "se299 signal generator", getattr(args, "interval_ms", 200))
    panel.stop()          # idempotent: real close already stopped it (+ forced RF off); guarantees
    hub.shutdown()         # the engine thread/timer are down before the hub releases leases
    return 0


def cmd_bench(args):
    """The se299 bench: spectrum analyzer + signal generator in one window (independent operation).
    Same --vm/--vm-plan qemu bring-up as se-gui/coordinator (Task 10 fix: this used to ignore it)."""
    import bench_gui
    if getattr(args, "vm_plan", False):
        from gpib_bridge import vm as vmmod
        print(vmmod.launch_plan(vmmod.VmSpec(port=args.vm_port, source_port=args.vm_source_port)))
        return 0
    if getattr(args, "vm", False):
        from gpib_bridge import vm as vmmod
        try:
            args.analyzer, args.source = _ensure_vm_addresses(args)
        except vmmod.BridgeUnavailable as e:
            print(f"qemu bridge unavailable: {e}")
            return 1
    bench = bench_gui.build_bench(getattr(args, "analyzer", "sim"),
                                  getattr(args, "source", "sim"),
                                  client_id=drivers.client_id(role="bench"))
    import qt_common
    qt_common.install_exit_cleanup(bench.shutdown)   # Ctrl-C / kill frees the lease (else next launch blocks)
    print(f"bench: analyzer={getattr(args, 'analyzer', 'sim')}  source={getattr(args, 'source', 'sim')}"
          f"  (Qt window; close to stop)")
    try:
        bench.run(getattr(args, "interval_ms", 200))
    finally:
        bench.shutdown()  # idempotent: real close already ran this via closeEvent; guarantees both
    return 0              # engine threads are down (no orphan SA/SG threads) before returning


def _add_vm_bind_args(sp):
    """--vm-bind / --vm-token: STAGE 2 REACHABILITY. Expose the forwarded bridge ports on a routable
    interface so a client on ANOTHER host can reach the analyzer/source. The default 127.0.0.1 keeps
    today's single-machine (loopback) behavior. A routable bind REQUIRES a token (mirrors
    ni_gpib_server) -- it publishes an instrument-control service to the LAN."""
    sp.add_argument("--vm-bind", dest="vm_bind", default="127.0.0.1",
                    help="interface qemu's hostfwd binds the bridge ports on (default 127.0.0.1, "
                         "loopback/single-machine; 0.0.0.0 exposes them to the LAN for a remote "
                         "client -- requires --vm-token). ssh stays loopback.")
    sp.add_argument("--vm-token", dest="vm_token", default=os.environ.get("NI_GPIB_TOKEN", ""),
                    help="bridge auth token required for a routable --vm-bind (or set NI_GPIB_TOKEN)")


def _add_analyzer_args(sp, default_sweeps):
    sp.add_argument("--analyzer", default="auto",
                    help="'auto' (scan bus, sim if none) | 'sim' | 'net:HOST:PORT:GPIBADDR' "
                         "(network GPIB bridge, the M1 path) | a VISA resource string")
    sp.add_argument("--span-lo", dest="span_lo", type=float, default=1.0, help="sweep start (GHz)")
    sp.add_argument("--span-hi", dest="span_hi", type=float, default=6.0, help="sweep stop (GHz)")
    sp.add_argument("--points", type=int, default=601, help="trace points (601 on real hw)")
    sp.add_argument("--sweeps", type=int, default=default_sweeps,
                    help="number of sweeps (0 = continuous until Ctrl-C)")
    sp.add_argument("--interval", type=float, default=0.0, help="seconds between sweeps")
    sp.add_argument("--width", type=int, default=70, help="sparkline width (columns)")
    sp.add_argument("--retries", type=int, default=3, help="auto-reconnect attempts")


def main(argv=None):
    p = argparse.ArgumentParser(description="IEEE-299 substitution SE automation (Tier-1)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "dryrun", "preflight"):
        sp = sub.add_parser(name)
        sp.add_argument("--gain", type=int, choices=(25, 33), default=33,
                        help="top-band horn gain dBi (33=elite no-LNA, 25=standard+LNA)")
        sp.add_argument("--rbw", type=float, default=1000.0, help="analyzer RBW (Hz)")
        sp.add_argument("--source", default="sim")
        sp.add_argument("--analyzer", default="sim")
        sp.add_argument("--label", default=name)
    spl = sub.add_parser("localize")
    spl.add_argument("--freq", type=float, default=38.0, help="localization CW frequency (GHz)")
    spl.add_argument("--span", type=float, default=2.4, help="probe scan length (m)")
    spl.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spl.add_argument("--rbw", type=float, default=1000.0)
    spl.add_argument("--source", default="sim")
    spl.add_argument("--analyzer", default="sim")
    spl.add_argument("--label", default="localize")
    spc = sub.add_parser("capture")
    spc.add_argument("--phase", required=True, choices=("reference", "wall"))
    spc.add_argument("--label", required=True)
    spc.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spc.add_argument("--rbw", type=float, default=1000.0)
    spc.add_argument("--source", default="sim")
    spc.add_argument("--analyzer", default="sim")
    spd = sub.add_parser("detect", help="connect the 8565EC and show DETECTED + VALID status")
    _add_analyzer_args(spd, default_sweeps=0)
    spn = sub.add_parser("nf-sweep", help="8565EC as a near-field-probe sweeper (auto-connect)")
    _add_analyzer_args(spn, default_sweeps=4)
    spv = sub.add_parser("live", help="LIVE moving 8565EC spectrum GUI (PySide6 + pyqtgraph)")
    _add_analyzer_args(spv, default_sweeps=0)
    spv.add_argument("--vm", action="store_true",
                     help="seamless: boot the QEMU VM with USB passthrough of the NI adapter, "
                          "provision ni_usb_gpib, wait for the real 8565EC bridge, then render "
                          "it (needs: brew install qemu + the adapter attached). No fake.")
    spv.add_argument("--vm-plan", dest="vm_plan", action="store_true",
                     help="print the QEMU USB-passthrough bring-up plan (detected NI adapter "
                          "+ the qemu command + how to reach the bridge) and exit")
    spv.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton",
                     help="singleton (DEFAULT): ONE VM, serial hot-plug of both adapters (no UEFI ASSERT); "
                          "golden: TWO VMs (remote/fault-isolation); both: at-boot both (SUPERSEDED)")
    spv.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spv.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spv.add_argument("--vm-timeout", dest="vm_timeout", type=float, default=240.0,
                     help="per-unit readiness timeout seconds (default 240)")
    spv.add_argument("--vm-reset", dest="vm_reset", action="store_true",
                     help="re-provision from a clean overlay (keeps the downloaded base image)")
    _add_vm_bind_args(spv)
    spw = sub.add_parser("sweep", help="stepped-CW (acceptance-grade) or swept-span (screen) sweep")
    spw.add_argument("--mode", choices=("stepped", "swept"), default="stepped",
                     help="stepped = narrow-RBW dwell per point (high DR); swept = fast screen")
    spw.add_argument("--span-lo", dest="span_lo", type=float, default=1.0, help="start (GHz)")
    spw.add_argument("--span-hi", dest="span_hi", type=float, default=6.0, help="stop (GHz)")
    spw.add_argument("--points", type=int, default=201, help="stepped: number of source steps")
    spw.add_argument("--sweep-time", dest="sweep_time", type=float, default=0.0)
    spw.add_argument("--atten", type=float, default=0.0, help="input attenuation (dB)")
    spw.add_argument("--aunits", default="DBM")
    spw.add_argument("--video-avg", dest="video_avg", type=int, default=0)
    spw.add_argument("--max-hold", dest="max_hold", action="store_true")
    spw.add_argument("--rbw", type=float, default=1000.0)
    spw.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spw.add_argument("--source", default="sim")
    spw.add_argument("--analyzer", default="sim")
    spw.add_argument("--label", default="sweep")
    spco = sub.add_parser("coordinator", help="SE-coordinator instance: own both units, run "
                          "the campaign, publish the live SE figure over telemetry")
    spco.add_argument("--source", default="sim",
                      help="TX source addr (sim | net:HOST:PORT:PAD | VISA string)")
    spco.add_argument("--analyzer", default="sim",
                      help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA string)")
    spco.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spco.add_argument("--rbw", type=float, default=1000.0)
    spco.add_argument("--label", default="coordinator")
    spco.add_argument("--sweep", action="store_true",
                      help="TWO-INSTANCE DRIVE: instead of the SE campaign, sweep DC-to-40-GHz with "
                           "the TX source (instance 2) following the RX (instance 1) at every point")
    spco.add_argument("--telemetry-port", dest="telemetry_port", type=int, default=52998,
                      help="TCP port to publish live SE telemetry on (0 = ephemeral)")
    spco.add_argument("--telemetry-bind", dest="telemetry_bind", default="127.0.0.1",
                      help="interface the telemetry listener binds on (default 127.0.0.1 keeps it "
                           "loopback-only; set 0.0.0.0 or a LAN IP so dashboard instances on OTHER "
                           "hosts can subscribe and observe the live SE figure)")
    spco.add_argument("--topology", choices=("radiated", "direct"), default="radiated",
                      help="RF topology. 'radiated' (default): horns + path loss protect the RX. "
                           "'direct': the TX is CABLED straight to the 8565EC -- arm hardware "
                           "protection (cap the source, floor input atten >=10 dB) so the tone "
                           "cannot overdrive the analyzer front end.")
    spco.add_argument("--vm", action="store_true",
                      help="seamless: boot the QEMU VM with USB passthrough of BOTH NI adapters, "
                           "provision the patched ni_usb_gpib + two boards, wait for BOTH the "
                           "8565EC and 68369A, then run the campaign (needs: brew install qemu + "
                           "both adapters attached). No fake.")
    spco.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton",
                      help="singleton (DEFAULT): ONE VM, serial hot-plug of both adapters (no UEFI "
                           "ASSERT); golden: TWO VMs (remote/fault-isolation); both: SUPERSEDED")
    spco.add_argument("--vm-plan", dest="vm_plan", action="store_true",
                      help="print the QEMU USB-passthrough bring-up plan (both units) and exit")
    spco.add_argument("--vm-port", dest="vm_port", type=int, default=5555,
                      help="forwarded bridge port for the 8565EC analyzer board (default 5555; "
                           "golden mode also uses port+1 for the source VM)")
    spco.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556,
                      help="(both mode) forwarded bridge port for the 68369A source board")
    spco.add_argument("--vm-timeout", dest="vm_timeout", type=float, default=240.0,
                      help="per-unit readiness timeout seconds (default 240; golden degrades to "
                           "the healthy unit on timeout rather than aborting)")
    spco.add_argument("--vm-stagger", dest="vm_stagger", type=float, default=25.0,
                      help="golden: seconds to wait after launching the first VM before launching "
                           "the second, so the two NI FX2 fxloads never overlap (default 25; raise "
                           "if a cold boot still wedges both adapters at -110)")
    spco.add_argument("--vm-reset", dest="vm_reset", action="store_true",
                      help="re-provision from a clean overlay (use after a failed provision); "
                           "keeps the downloaded base image, so no re-download")
    _add_vm_bind_args(spco)
    spco.add_argument("--discover", action="store_true",
                      help="find bridges via UDP discovery instead of --source/--analyzer")
    spco.add_argument("--discover-timeout", dest="discover_timeout", type=float, default=1.0)
    spco.add_argument("--wait-subscribers", dest="wait_subscribers", type=int, default=0,
                      help="wait for N dashboard subscribers before running the campaign")
    spco.add_argument("--wait-timeout", dest="wait_timeout", type=float, default=30.0)
    spdev = sub.add_parser("devices", help="show the network-connected DEVICES (instrument "
                           "instances) + the CLIENTS operating them (via the bridge lease table)")
    spdev.add_argument("--analyzer", default="", help="RX device addr net:HOST:PORT:PAD")
    spdev.add_argument("--source", default="", help="TX device addr net:HOST:PORT:PAD")
    spdev.add_argument("--device", action="append", help="extra device net:HOST:PORT:PAD (repeatable)")
    spdev.add_argument("--discover", action="store_true", help="find devices via UDP discovery beacons")
    spdev.add_argument("--discover-timeout", dest="discover_timeout", type=float, default=1.0)
    spdb = sub.add_parser("dashboard", help="observer instance: subscribe to a coordinator's "
                          "live SE telemetry and print it")
    spdb.add_argument("--telemetry", default="127.0.0.1:52998",
                      help="coordinator telemetry HOST:PORT")
    spdb.add_argument("--timeout", type=float, default=1.0)
    spcp = sub.add_parser("checkpath", help="RF-path self-test: confirm the TX tone reaches the "
                          "RX above the noise floor BEFORE trusting any SE number (PATH-LIVE/NO-COUPLING)")
    spcp.add_argument("--source", default="sim", help="TX source addr (sim | net:HOST:PORT:PAD | VISA)")
    spcp.add_argument("--analyzer", default="sim", help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA)")
    spcp.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spcp.add_argument("--rbw", type=float, default=1000.0)
    spcp.add_argument("--span-lo", dest="span_lo", type=float, default=1.0, help="start (GHz)")
    spcp.add_argument("--span-hi", dest="span_hi", type=float, default=18.0, help="stop (GHz)")
    spcp.add_argument("--points", type=int, default=6, help="number of test frequencies")
    spcp.add_argument("--guard", type=float, default=6.0, help="min TX on-off delta to call it coupled (dB)")
    spcp.add_argument("--topology", choices=("radiated", "direct"), default="radiated",
                      help="'direct': the TX is CABLED straight to the 8565EC -- arm hardware "
                           "protection (cap the source, floor input atten >=10 dB) before any tone.")
    spcp.add_argument("--label", default="checkpath")
    spcp.add_argument("--vm", action="store_true", help="seamless qemu bring-up of BOTH adapters")
    spcp.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton")
    spcp.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spcp.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spcp.add_argument("--vm-reset", dest="vm_reset", action="store_true")
    _add_vm_bind_args(spcp)
    spch = sub.add_parser("chain", help="chain-continuity sweep: ramp both units UP the preset "
                          "bands (low->high), confirm TX-at-setpoint before each RX read, validate "
                          "emitted-vs-received across the whole plan (precedes an SE campaign)")
    spch.add_argument("--source", default="sim", help="TX source addr (sim | net:HOST:PORT:PAD | VISA)")
    spch.add_argument("--analyzer", default="sim", help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA)")
    spch.add_argument("--bands", choices=("dc-40", "1-40"), default="dc-40",
                      help="preset band plan: dc-40 (10 MHz-40 GHz, default) | 1-40 (1-40 GHz)")
    spch.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spch.add_argument("--rbw", type=float, default=1000.0)
    spch.add_argument("--guard", type=float, default=6.0,
                      help="min tone-minus-floor delta to call a point carried (dB)")
    spch.add_argument("--settle-ms", dest="settle_ms", type=float, default=0.0,
                      help="per-point source settle dwell (ms); 0 = use the campaign default")
    spch.add_argument("--topology", choices=("radiated", "direct"), default="radiated",
                      help="'direct': TX cabled straight to the 8565EC -- arm input protection first")
    spch.add_argument("--label", default="chain")
    spch.add_argument("--vm", action="store_true", help="seamless qemu bring-up of BOTH adapters")
    spch.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton")
    spch.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spch.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spch.add_argument("--vm-reset", dest="vm_reset", action="store_true")
    _add_vm_bind_args(spch)
    spcal = sub.add_parser("calibrate", help="capture a CALIBRATION (reference pass) in the "
                           "current antenna geometry; grade it + save it for a later wall pass")
    spcal.add_argument("--source", default="sim",
                       help="TX source addr (sim | net:HOST:PORT:PAD | VISA string)")
    spcal.add_argument("--analyzer", default="sim",
                       help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA string)")
    spcal.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spcal.add_argument("--rbw", type=float, default=1000.0, help="analyzer RBW (Hz)")
    spcal.add_argument("--out", default="", help="calibration JSON path (default calibration-<label>.json)")
    spcal.add_argument("--label", default="enclosure")
    spcal.add_argument("--vm", action="store_true",
                       help="seamless qemu bring-up of BOTH NI adapters, then run the reference pass")
    spcal.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton")
    spcal.add_argument("--vm-plan", dest="vm_plan", action="store_true")
    spcal.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spcal.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spcal.add_argument("--vm-reset", dest="vm_reset", action="store_true")
    _add_vm_bind_args(spcal)
    spv2 = sub.add_parser("validate", help="executable verification of adherence: run the "
                          "automatable per-device validation sequence (PASS/FAIL/NA + manual cite)")
    spv2.add_argument("--source", default="sim", help="TX source addr (sim | net:HOST:PORT:PAD | VISA)")
    spv2.add_argument("--analyzer", default="sim", help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA)")
    spv2.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spv2.add_argument("--rbw", type=float, default=1000.0)
    spv2.add_argument("--probe", type=float, default=5.0, help="probe frequency for the checks (GHz)")
    spv2.add_argument("--label", default="validate")
    spv2.add_argument("--vm", action="store_true", help="seamless qemu bring-up of BOTH adapters")
    spv2.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton")
    spv2.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spv2.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spv2.add_argument("--vm-reset", dest="vm_reset", action="store_true")
    _add_vm_bind_args(spv2)
    spw2 = sub.add_parser("wall", help="second half of the campaign: load a saved calibration "
                          "(reference pass) + run the WALL pass -> SE(f) = reference - wall")
    spw2.add_argument("--calibration", required=True, help="calibration JSON from `calibrate`")
    spw2.add_argument("--source", default="sim", help="TX source addr (sim | net:HOST:PORT:PAD | VISA)")
    spw2.add_argument("--analyzer", default="sim", help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA)")
    spw2.add_argument("--gain", type=int, choices=(25, 33), default=33,
                      help="MUST match the calibration's --gain (same band plan)")
    spw2.add_argument("--rbw", type=float, default=1000.0)
    spw2.add_argument("--out-dir", dest="out_dir", default="", help="write manifest/reference/wall/CSV here")
    spw2.add_argument("--label", default="wall")
    spw2.add_argument("--vm", action="store_true", help="seamless qemu bring-up of BOTH adapters")
    spw2.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton")
    spw2.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spw2.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spw2.add_argument("--vm-reset", dest="vm_reset", action="store_true")
    _add_vm_bind_args(spw2)
    spwk = sub.add_parser("walkaround", help="LIVE near-field-probe walkaround GUI: source-on CW "
                          "tone + live probe level + max-hold + hot-spot marks (leak localization)")
    spwk.add_argument("--source", default="sim", help="TX source addr (sim | net:HOST:PORT:PAD | VISA)")
    spwk.add_argument("--analyzer", default="sim", help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA)")
    spwk.add_argument("--freq", type=float, default=5.0, help="leak frequency to probe (GHz)")
    spwk.add_argument("--power", type=float, default=None, help="tone power dBm (default = band value)")
    spwk.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spwk.add_argument("--rbw", type=float, default=1000.0, help="analyzer RBW (Hz)")
    spwk.add_argument("--interval-ms", dest="interval_ms", type=int, default=200)
    spwk.add_argument("--vm", action="store_true", help="seamless qemu bring-up of BOTH adapters")
    spwk.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton")
    spwk.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spwk.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spwk.add_argument("--vm-reset", dest="vm_reset", action="store_true")
    _add_vm_bind_args(spwk)
    spg = sub.add_parser("se-gui", help="LIVE SE figure GUI: paint the SE(f) curve in dB with "
                         "operator controls (Run/Stop, gain, RBW); sim | net: | qemu --vm/golden")
    spg.add_argument("--source", default="sim",
                     help="TX source addr (sim | net:HOST:PORT:PAD | VISA string)")
    spg.add_argument("--analyzer", default="sim",
                     help="RX analyzer addr (sim | net:HOST:PORT:PAD | VISA string)")
    spg.add_argument("--gain", type=int, choices=(25, 33), default=33,
                     help="top-band horn gain dBi (33=elite no-LNA, 25=standard+LNA)")
    spg.add_argument("--rbw", type=float, default=1000.0, help="analyzer RBW (Hz)")
    spg.add_argument("--out-dir", dest="out_dir", default="",
                     help="save the completed campaign (manifest/reference/wall/CSV + calibration) here; "
                          "default output/<label>-<timestamp>")
    spg.add_argument("--span-lo", dest="span_lo", type=float, default=None,
                     help="operator sweep-band start (GHz); with --span-hi replaces the 1-40 GHz plan")
    spg.add_argument("--span-hi", dest="span_hi", type=float, default=None, help="sweep-band stop (GHz)")
    spg.add_argument("--power", type=float, default=None, help="tone power dBm (default = band value)")
    spg.add_argument("--interval-ms", dest="interval_ms", type=int, default=250,
                     help="GUI redraw interval (ms)")
    spg.add_argument("--vm", action="store_true",
                     help="seamless qemu bring-up of BOTH NI adapters, then drive the GUI live")
    spg.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton",
                     help="singleton (DEFAULT): ONE VM, serial hot-plug of both adapters (no UEFI ASSERT); "
                          "golden: TWO VMs (remote/fault-isolation); both: at-boot both (SUPERSEDED)")
    spg.add_argument("--vm-plan", dest="vm_plan", action="store_true",
                     help="print the QEMU USB-passthrough bring-up plan and exit")
    spg.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
    spg.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
    spg.add_argument("--vm-timeout", dest="vm_timeout", type=float, default=240.0,
                     help="per-unit readiness timeout seconds (default 240)")
    spg.add_argument("--vm-reset", dest="vm_reset", action="store_true",
                     help="re-provision from a clean overlay (keeps the downloaded base image)")
    _add_vm_bind_args(spg)
    for _name, _help in (("sa", "full-fidelity spectrum analyzer GUI (PySide6 + pyqtgraph)"),
                         ("sg", "signal generator GUI (CW + step-sweep; RF defaults off)"),
                         ("bench", "bench: spectrum analyzer + signal generator in one window")):
        _sp = sub.add_parser(_name, help=_help)
        _sp.add_argument("--analyzer", default="sim", help="'sim' | net:HOST:PORT:PAD | VISA")
        _sp.add_argument("--source", default="sim", help="'sim' | net:HOST:PORT:PAD | VISA")
        _sp.add_argument("--interval-ms", dest="interval_ms", type=int, default=200)
        _sp.add_argument("--vm", action="store_true",
                         help="seamless qemu bring-up of BOTH NI adapters, then drive live")
        _sp.add_argument("--vm-mode", dest="vm_mode", choices=("singleton", "golden", "both"), default="singleton",
                         help="singleton (DEFAULT): ONE VM, serial hot-plug of both adapters (no UEFI ASSERT); "
                          "golden: TWO VMs (remote/fault-isolation); both: at-boot both (SUPERSEDED)")
        _sp.add_argument("--vm-plan", dest="vm_plan", action="store_true",
                         help="print the QEMU USB-passthrough bring-up plan and exit")
        _sp.add_argument("--vm-port", dest="vm_port", type=int, default=5555)
        _sp.add_argument("--vm-source-port", dest="vm_source_port", type=int, default=5556)
        _sp.add_argument("--vm-timeout", dest="vm_timeout", type=float, default=240.0,
                         help="per-unit readiness timeout in seconds (default 240; golden degrades "
                              "to the healthy unit on timeout)")
        _sp.add_argument("--vm-reset", dest="vm_reset", action="store_true",
                         help="re-provision from a clean overlay (keeps the downloaded base image; "
                              "refused while a live qemu holds the overlay -- vm-stop first)")
        _add_vm_bind_args(_sp)
    spq = sub.add_parser("q", help="cavity Q-factor (both units inside; NOT IEEE-299)")
    spq.add_argument("--span-lo", dest="span_lo", type=float, default=9.98, help="start (GHz)")
    spq.add_argument("--span-hi", dest="span_hi", type=float, default=10.02, help="stop (GHz)")
    spq.add_argument("--n-db", dest="n_db", type=float, default=3.0, help="marker N-dB-down width")
    spq.add_argument("--video-avg", dest="video_avg", type=int, default=16)
    spq.add_argument("--rbw", type=float, default=1000.0)
    spq.add_argument("--gain", type=int, choices=(25, 33), default=33)
    spq.add_argument("--source", default="sim")
    spq.add_argument("--analyzer", default="sim")
    spq.add_argument("--label", default="q")
    spvs = sub.add_parser("vm-stop", help="stop running qemu bridge instance(s) (QMP quit / stored "
                          "pid); run before --vm-reset if it refuses (a live qemu holds the overlay)")
    spvs.add_argument("--name", action="append",
                      help="instance name to stop (repeatable; default: se299-rx, se299-tx, se299-gpib)")
    args = p.parse_args(argv)
    return {"demo": cmd_demo, "dryrun": cmd_dryrun, "preflight": cmd_preflight,
            "localize": cmd_localize, "capture": cmd_capture,
            "detect": cmd_detect, "nf-sweep": cmd_nf_sweep, "live": cmd_live,
            "sweep": cmd_sweep, "q": cmd_q, "se-gui": cmd_se_gui, "calibrate": cmd_calibrate,
            "checkpath": cmd_checkpath, "chain": cmd_chain, "wall": cmd_wall, "validate": cmd_validate,
            "walkaround": cmd_walkaround, "devices": cmd_devices,
            "coordinator": cmd_coordinator, "dashboard": cmd_dashboard,
            "sa": cmd_sa, "sg": cmd_sg, "bench": cmd_bench, "vm-stop": cmd_vm_stop}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
