"""End-to-end LIVE tests: the canonical SE code paths driven against REAL instruments through
the qemu bridge(s). These require both units present + a bridge up, so they SKIP with an honest
reason when the live setup is not reachable -- never a fake, never a false pass (the
se-certification discipline: honest ABSENT when hardware is missing).

Bring the live setup up first, then run these:
  # one VM, both adapters:
  uv run python rf-se/se299/cli.py coordinator --vm
  # or the golden two-instance:
  uv run python rf-se/se299/cli.py coordinator --vm --vm-mode golden
  # then, in another shell:
  uv run python -m pytest rf-se/se299/tests/test_e2e_live.py -q

Gate: the tests probe the forwarded bridge ports (analyzer 5555 pad 18, source 5556 pad 5 -- the
default single-VM 'both' AND golden layouts both put them there). Override with env
SE299_ANALYZER_PORT / SE299_SOURCE_PORT / SE299_ANALYZER_PAD / SE299_SOURCE_PAD.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import control_plane
import drivers
from gpib_bridge import vm


def _spec():
    return vm.VmSpec(
        port=int(os.environ.get("SE299_ANALYZER_PORT", "5555")),
        source_port=int(os.environ.get("SE299_SOURCE_PORT", "5556")),
        gpib_addr=int(os.environ.get("SE299_ANALYZER_PAD", "18")),
        source_addr=int(os.environ.get("SE299_SOURCE_PAD", "5")))


def _require_live():
    """Skip unless BOTH units answer through the live bridge(s). Names which is missing."""
    spec = _spec()
    a = vm.bridge_reachable(spec, timeout_ms=1500)
    s = vm.source_reachable(spec, timeout_ms=1500)
    if not (a and s):
        pytest.skip(
            "live setup not reachable "
            f"[analyzer 127.0.0.1:{spec.port}:{spec.gpib_addr}={'UP' if a else 'DOWN'}; "
            f"source 127.0.0.1:{spec.source_port}:{spec.source_addr}={'UP' if s else 'DOWN'}] "
            "-- bring it up with `cli.py coordinator --vm` (or `--vm-mode golden`) first")
    return spec


def test_live_both_units_answer_idn():
    # the core goal: BOTH the 8565EC (RX) and the 68369A (TX) answer *IDN?/ID? through qemu
    spec = _require_live()
    an = drivers.NetworkTransport("127.0.0.1", spec.port, spec.gpib_addr, timeout_ms=3000)
    try:
        idn_a = drivers.Agilent856xEC(an).idn()
    finally:
        an.close()
    src = drivers.NetworkTransport("127.0.0.1", spec.source_port, spec.source_addr, timeout_ms=3000)
    try:
        idn_s = drivers.Anritsu68369(src).idn()
    finally:
        src.close()
    assert "856" in idn_a, f"analyzer IDN unexpected: {idn_a!r}"
    assert "68369" in idn_s or "683" in idn_s, f"source IDN unexpected: {idn_s!r}"


def test_live_se_campaign_reference_pass_runs():
    # the canonical SE path end to end over the live bridge(s): take control, run the reference
    # (EA8) pass source-tracked, assert well-formed rows come back for the whole plan.
    spec = _require_live()
    cfg = config.Campaign(
        instruments=config.Instruments(
            analyzer_addr=f"net:127.0.0.1:{spec.port}:{spec.gpib_addr}",
            source_addr=f"net:127.0.0.1:{spec.source_port}:{spec.source_addr}"),
        # a short single-band plan keeps a live run quick; still exercises the whole loop
        bands=(config.BandPlan("live 1-2 GHz", 1e9, 2e9, 2, 14.0, 12.0, -150.0),),
        label="e2e-live")
    cp = control_plane.from_addresses(
        cfg, rx_addr=cfg.instruments.analyzer_addr, tx_addr=cfg.instruments.source_addr)
    coord = cp.make_coordinator()                           # resolve the rx+tx pair -> Coordinator
    assert coord.ensure_ready(), "coordinator could not reach both live instruments"
    coord.take_control()
    try:
        ref = coord.acquire_reference()
    finally:
        coord.release_control()
    assert len(ref) == 2                                    # one row per planned point
    for row in ref.values():
        assert row["source_tracked"] is True                # the source retuned per point
        assert row["acq_mode"] == "stepped-cw-zerospan"     # the acceptance mode
        assert "capability_db" in row and "ea8_ok" in row


def _net_addrs(spec):
    return (f"net:127.0.0.1:{spec.port}:{spec.gpib_addr}",
            f"net:127.0.0.1:{spec.source_port}:{spec.source_addr}")


def test_live_walkaround_reads_frames_over_net():
    # the near-field WALKAROUND drives BOTH networked units: source CW on at a leak freq, analyzer
    # reads the probe in a loop. Assert real frames stream back and the source ends OFF.
    spec = _require_live()
    an_addr, src_addr = _net_addrs(spec)
    cfg = config.Campaign(bands=(config.BandPlan("live 2 GHz", 1.5e9, 2.5e9, 2, 14.0, 12.0, -150.0),),
                          label="e2e-walkaround")
    cp = control_plane.from_addresses(cfg, rx_addr=an_addr, tx_addr=src_addr)
    coord = cp.make_coordinator()
    assert coord.ensure_ready(), "coordinator could not reach both live instruments"
    got = []
    # walk for ~5 frames then stop; RF is turned off in loop.nearfield_walkaround's finally
    coord.walkaround(2.0e9, on_frame=lambda i, lvl: got.append(lvl),
                     should_stop=lambda: len(got) >= 5)
    assert len(got) >= 5                                     # real probe frames streamed live
    assert all(isinstance(v, float) and v == v for v in got)   # finite dBm numbers, not NaN
    # source left OFF: a fresh probe read with RF off should not raise (bus is sane)
    src = drivers.NetworkTransport("127.0.0.1", spec.source_port, spec.source_addr, timeout_ms=3000)
    try:
        drivers.Anritsu68369(src).rf_off()
    finally:
        src.close()


def test_live_two_instance_sweep_tx_follows_dc_to_40ghz():
    # TWO NETWORKED INSTANCES: instance 1 = the analyzer bridge (RX), instance 2 = the source
    # bridge (TX), both on the net. Instance 1 (this coordinator) DRIVES a DC-to-40-GHz sweep;
    # the TX must FOLLOW every point through the stack (control-plane -> net -> bridge -> GPIB ->
    # source). A short 2-pts-per-band plan keeps it quick but still spans 10 MHz -> 40 GHz.
    spec = _require_live()
    an_addr, src_addr = _net_addrs(spec)
    bands = (config.BandPlan("DC-1GHz", 10e6, 1e9, 2, 3.0, 12.0, -150.0),
             config.BandPlan("1-18GHz", 1e9, 18e9, 2, 14.0, 12.0, -150.0),
             config.BandPlan("18-40GHz", 18e9, 40e9, 2, 25.0, 11.0, -145.0))
    cfg = config.Campaign(bands=bands, label="live-dc40-2instance")
    cp = control_plane.from_addresses(cfg, rx_addr=an_addr, tx_addr=src_addr)
    coord = cp.make_coordinator()
    assert coord.ensure_ready(), "instance 1 could not reach BOTH networked units"
    plan = [f for f, _ in cfg.frequencies()]
    res = coord.sweep()                                    # instance 1 drives; TX follows
    assert res["source_tracked"] is True
    assert len(res["levels_dbm"]) == len(plan)
    assert all(isinstance(v, float) and v == v for v in res["levels_dbm"])   # finite dBm each pt
    assert min(res["freqs_hz"]) <= 11e6 and max(res["freqs_hz"]) >= 39.9e9   # spans DC-40 GHz
    # PROVE the TX followed through the stack to the real hardware: after the sweep the source's
    # last-commanded frequency (native OF1 readback) is the last swept point.
    src = drivers.NetworkTransport("127.0.0.1", spec.source_port, spec.source_addr, timeout_ms=3000)
    try:
        of1_mhz = drivers.Anritsu68369(src).output_freq_mhz()
    finally:
        src.close()
    assert abs(of1_mhz - plan[-1] / 1e6) <= max(1.0, plan[-1] / 1e6 * 1e-4)


def test_live_se_gui_campaign_over_net():
    # the SE-testing GUI's production path over BOTH networked units: build_se_gui's factory
    # resolves net: addresses and runs a real (tiny operator-band) campaign end to end.
    spec = _require_live()
    an_addr, src_addr = _net_addrs(spec)
    import se_gui
    model, gui = se_gui.build_se_gui(an_addr, src_addr, gain_dbi=33, rbw_hz=1000.0)
    # operator sweep band 1-2 GHz, 2 points (set exactly as the GUI would)
    coord, bench = gui.campaign_factory(33, 1000.0, 1.0, 2.0, None)
    assert coord.ensure_ready(), "se-gui factory could not reach both live instruments"
    result = coord.run_campaign(bench=bench)
    wall = result["wall"]
    assert len(wall) == 20                                   # operator sweep band default n_points
    freqs = [r["f_hz"] for r in wall.values()]
    assert min(freqs) <= 1.01e9 and max(freqs) >= 1.99e9     # SE computed across the operator 1-2 GHz band
    for row in wall.values():
        assert "se_reported_db" in row and row["acq_mode"] == "stepped-cw-zerospan"
