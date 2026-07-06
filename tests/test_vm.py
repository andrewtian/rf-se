"""Hardware-free tests for the QEMU USB-passthrough bridge (gpib_bridge/vm.py).

Covers the pure logic: host USB-GPIB detection (parsed from an ioreg plist fixture), the
qemu argv with USB passthrough + hostfwd, the cloud-init user-data, and the reachability
probe (against a REAL local fake bridge over TCP -- the production NetworkTransport path).
The actual qemu boot + firmware upload + USB grab need the real host + the NI adapter.

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_vm.py -q
"""
from __future__ import annotations

import os
import plistlib
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gpib_bridge import ni_gpib_server, vm


# ----------------------------------------------------------------- fixtures / helpers

def _usb_plist(*, ni_pid=0x702b, with_prologix=True):
    """An ioreg-style IOUSB plist with a hub, the LAN, the NI adapter nested behind the hub,
    and (optionally) a Prologix FTDI -- exercising the recursive walk + classification."""
    ni = {"idVendor": 0x3923, "idProduct": ni_pid, "locationID": 1310720}   # no strings (pre-fw)
    children = [ni]
    if with_prologix:
        children.append({"USB Product Name": "FT232R USB UART", "USB Vendor Name": "FTDI",
                         "idVendor": 0x0403, "idProduct": 0x6001, "locationID": 5})
    tree = {"IORegistryEntryChildren": [
        {"USB Product Name": "USB2.0 Hub", "idVendor": 0x05e3, "idProduct": 0x0608,
         "IORegistryEntryChildren": children},
        {"USB Product Name": "USB LAN", "idVendor": 0x0bda, "idProduct": 0x8156},
    ]}
    return plistlib.dumps(tree)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_fake(port, signal="moving"):
    srv = ni_gpib_server.listen("127.0.0.1", port)
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "signal": signal}, daemon=True).start()


# ----------------------------------------------------------------- detection

def test_detect_finds_ni_nested_and_prologix_skips_hub_and_lan():
    devs = vm.detect_gpib_usb(raw=_usb_plist())
    kinds = {d.kind for d in devs}
    assert "ni-gpib-b" in kinds and "prologix" in kinds     # 0x702b is a GPIB-USB-B
    assert "unknown" not in kinds                           # hub + LAN skipped
    ni = vm.pick_ni_device(devs)
    assert ni is not None and ni.vendor_id == 0x3923 and ni.valid is True
    assert "pre-firmware" in ni.note                        # 0x702b classified correctly


def test_detect_classifies_both_adapter_models():
    # the real setup: a GPIB-USB-B (8565EC) AND a GPIB-USB-HS (68369A) on one host
    tree = {"IORegistryEntryChildren": [
        {"idVendor": 0x3923, "idProduct": 0x702a, "USB Product Name": "GPIB-USB-B"},
        {"idVendor": 0x3923, "idProduct": 0x709b, "USB Product Name": "GPIB-USB-HS"},
    ]}
    devs = vm.detect_gpib_usb(raw=plistlib.dumps(tree))
    both = vm.pick_ni_devices(devs)
    assert len(both) == 2
    kinds = {d.kind for d in devs}
    assert kinds == {"ni-gpib-b", "ni-gpib-hs"}
    hs = next(d for d in devs if d.product_id == 0x709b)
    assert hs.model == "NI GPIB-USB-HS" and "onboard" in hs.note and hs.valid is True


def test_detect_ready_firmware_state():
    ni = vm.pick_ni_device(vm.detect_gpib_usb(raw=_usb_plist(ni_pid=0x702a)))
    assert ni.product_id == 0x702a and "ready" in ni.note


def test_pick_ni_none_when_only_prologix():
    # a Prologix is recognized but is NOT a valid VM-passthrough target (native macOS serial)
    tree = {"IORegistryEntryChildren": [
        {"idVendor": 0x0403, "idProduct": 0x6001, "USB Product Name": "FT232R USB UART"}]}
    devs = vm.detect_gpib_usb(raw=plistlib.dumps(tree))
    assert vm.pick_ni_device(devs) is None
    assert any(d.kind == "prologix" and d.valid is False for d in devs)


def test_usb_summary_lists_and_flags():
    s = vm.usb_summary(vm.detect_gpib_usb(raw=_usb_plist()))
    assert "0x3923:0x702b" in s and "VALID passthrough target" in s
    assert vm.usb_summary([]).startswith("USB-GPIB adapters detected: NONE")


# ----------------------------------------------------------------- qemu argv / gate

def test_qemu_available_gate(monkeypatch):
    monkeypatch.setattr(vm.shutil, "which", lambda x: None)
    ok, msg = vm.qemu_available()
    assert ok is False and "brew install qemu" in msg
    monkeypatch.setattr(vm.shutil, "which", lambda x: "/opt/homebrew/bin/" + x)
    assert vm.qemu_available()[0] is True


def test_build_qemu_argv_has_passthrough_and_hostfwd():
    argv = vm.build_qemu_argv(vm.VmSpec(), "/w/disk.qcow2", "/w/seed.iso", "/w/uefi.fd")
    joined = " ".join(argv)
    assert "-accel hvf" in joined                            # Apple Silicon acceleration
    # the GPIB-USB-B (8565EC) by VENDOR ONLY on XHCI (full-speed; survives 0x702b->0x702a)
    assert "usb-host,bus=xhci.0,vendorid=0x3923,id=ni_b" in joined
    # the GPIB-USB-HS (68369A) by its unique product id on EHCI (high-speed; avoids XhciDxe ASSERT)
    assert "usb-host,bus=ehci.0,vendorid=0x3923,productid=0x709b,id=ni_hs" in joined
    assert "qemu-xhci" in joined and "usb-ehci" in joined    # both controllers present
    assert "hostfwd=tcp:127.0.0.1:5555-:5555" in joined      # analyzer bridge port -> Mac loopback
    assert "hostfwd=tcp:127.0.0.1:5556-:5556" in joined      # source bridge port -> Mac loopback
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in joined        # ssh -> Mac loopback (diagnostics)
    assert "-qmp" in argv and any("qmp.sock" in a for a in argv)  # QMP for post-boot hot-plug
    assert "mount_tag=gpibbridge" in joined                  # the bridge folder over 9p
    assert "/w/disk.qcow2" in joined and "/w/seed.iso" in joined and "/w/uefi.fd" in joined


def test_build_qemu_argv_hs_listed_before_vendor_only_b():
    # ORDER matters: the 0x709b matcher must precede the vendor-only matcher so it claims the
    # HS and leaves the other 0x3923 device (the B) for the vendor-only grab.
    argv = vm.build_qemu_argv(vm.VmSpec(), "d", "s", "u")
    hs_i = next(i for i, a in enumerate(argv) if "productid=0x709b" in a)
    b_i = next(i for i, a in enumerate(argv) if a.endswith("vendorid=0x3923,id=ni_b"))
    assert hs_i < b_i


# ----------------------------------------------------------------- roles / per-instance identity

def test_build_qemu_argv_analyzer_role_only_the_b_on_xhci():
    argv = vm.build_qemu_argv(vm.VmSpec(name="rx", role=vm.ROLE_ANALYZER, port=6001), "d", "s", "u")
    joined = " ".join(argv)
    assert "usb-host,bus=xhci.0,vendorid=0x3923,id=ni_b" in joined    # the B only
    assert "productid=0x709b" not in joined and "usb-ehci" not in joined   # no HS, no ehci
    assert "hostfwd=tcp:127.0.0.1:6001-:6001" in joined              # its own bridge port
    assert "hostfwd=tcp:127.0.0.1:5556" not in joined                # no second (source) port


def test_build_qemu_argv_source_role_only_the_hs_on_ehci():
    argv = vm.build_qemu_argv(vm.VmSpec(name="tx", role=vm.ROLE_SOURCE, port=6002), "d", "s", "u")
    joined = " ".join(argv)
    assert "usb-host,bus=ehci.0,vendorid=0x3923,productid=0x709b,id=ni_hs" in joined  # the HS only
    assert "id=ni_b" not in joined and "qemu-xhci" not in joined      # no B, no xhci
    assert "hostfwd=tcp:127.0.0.1:6002-:6002" in joined              # source bridge on its port


def test_source_role_net_addr_uses_its_own_port():
    s = vm.VmSpec(name="tx", role=vm.ROLE_SOURCE, port=6002, source_addr=5)
    assert s.source_net_addr == "net:127.0.0.1:6002:5"
    assert s.net_addr == "net:127.0.0.1:6002:5"                       # primary = source for a TX VM


def test_per_instance_mac_is_stable_and_distinct():
    a = vm.VmSpec(name="se299-rx"); b = vm.VmSpec(name="se299-tx")
    assert a.nic_mac() == vm.VmSpec(name="se299-rx").nic_mac()        # stable per name
    assert a.nic_mac() != b.nic_mac()                                # distinct across instances
    assert a.nic_mac().startswith("52:54:00:")                       # locally-administered prefix
    assert vm.VmSpec(mac="52:54:00:aa:bb:cc").nic_mac() == "52:54:00:aa:bb:cc"  # explicit wins


def test_golden_pair_two_instances_fully_disjoint():
    ana, src = vm.golden_pair()
    assert ana.role == vm.ROLE_ANALYZER and src.role == vm.ROLE_SOURCE
    # every per-instance resource differs so the two qemus coexist
    assert ana.name != src.name
    assert ana.workdir() != src.workdir()
    assert ana.qmp_sock() != src.qmp_sock()
    assert ana.nic_mac() != src.nic_mac()
    assert ana.port != src.port and ana.ssh_port != src.ssh_port
    # each argv references only its own qmp/ports (no cross-talk)
    aargv = " ".join(vm.build_qemu_argv(ana, "d", "s", "u"))
    sargv = " ".join(vm.build_qemu_argv(src, "d", "s", "u"))
    assert ana.name in aargv and src.name in sargv
    assert f"hostfwd=tcp:127.0.0.1:{ana.port}-" in aargv
    assert f"hostfwd=tcp:127.0.0.1:{src.port}-" in sargv
    # the golden net: addresses -- analyzer RX + source TX, on separate ports
    assert ana.analyzer_net_addr == f"net:127.0.0.1:{ana.port}:18"
    assert src.source_net_addr == f"net:127.0.0.1:{src.port}:5"


def test_decode_location_id_and_hostport_match():
    # the two adapters on the host: B at 0x00140000 (port 1.4), HS at 0x00130000 (port 1.3)
    assert vm.decode_location_id(0x00140000) == (0, "1.4")
    assert vm.decode_location_id(0x00130000) == (0, "1.3")
    assert vm.decode_location_id(0x14120000) == (0x14, "1.2")        # nested behind a hub
    b = vm.UsbDevice(0x3923, 0x702a, "GPIB-USB-B", "NI", 0x00140000, "",
                     "ni-gpib-b", "NI GPIB-USB-B", "ready", True)
    assert vm.hostport_match(b) == "vendorid=0x3923,hostbus=0,hostport=1.4"


def test_golden_pair_pins_both_adapters_by_hostport_when_devices_given():
    # with the detected adapters, BOTH VMs are pinned to their PHYSICAL PORT: the analyzer to the
    # B (so it cannot race the source for the shared 0x3923 vendor id) and the source to the HS (so
    # its wedged-FX2 QMP re-attach knows the port -- reattach_source_hs).
    devs = [vm.UsbDevice(0x3923, 0x702a, "GPIB-USB-B", "NI", 0x00140000, "",
                         "ni-gpib-b", "NI GPIB-USB-B", "ready", True),
            vm.UsbDevice(0x3923, 0x709b, "GPIB-USB-HS", "NI", 0x00130000, "01D088F4",
                         "ni-gpib-hs", "NI GPIB-USB-HS", "ready", True)]
    ana, src = vm.golden_pair(devices=devs)
    aargv = " ".join(vm.build_qemu_argv(ana, "d", "s", "u"))
    assert "hostbus=0,hostport=1.4" in aargv                          # analyzer pinned to the B port
    assert "hostbus=0,hostport=1.3" in " ".join(vm.build_qemu_argv(src, "d", "s", "u"))  # HS port
    assert "hostport" in src.source_match()                           # source is port-pinned


def test_instance_reachable_per_role_against_fakes():
    # analyzer VM: only the analyzer needs to answer; source VM: only the source
    aport, sport = _free_port(), _free_port()
    _start_fake(aport); _start_fake(sport)
    ana = vm.VmSpec(name="rx", role=vm.ROLE_ANALYZER, port=aport, gpib_addr=18)
    src = vm.VmSpec(name="tx", role=vm.ROLE_SOURCE, port=sport, source_addr=5)
    assert vm.instance_reachable(ana) is True
    assert vm.instance_reachable(src) is True


def test_ensure_golden_pair_STAGGERS_launch_between_the_two_passthroughs():
    # P0 fix: launch the FIRST VM, SETTLE (so its 0x3923 FX2 fxload finishes / does not overlap),
    # THEN launch the SECOND -- never pass both FX2s through at the same instant (that wedges both
    # at -110). Ordering must be: launch-first, SETTLE, launch-second, then poll both.
    ev = []
    ana, src = vm.golden_pair()                                       # unpinned -> source-first
    a_addr, s_addr = vm.ensure_golden_pair(
        ana, src, reachable=lambda spec: False, detect=lambda: [],
        launch_one=lambda s, **kw: ev.append(("L", s.role)) or "launched",
        settle=lambda s, **kw: ev.append(("S", s.role)) or False,     # the STAGGER between launches
        poll_ready=lambda s, **kw: ev.append(("P", s.role)) or s.net_addr,
        run_parallel=lambda jobs, fn: {k: fn(a) for k, a in jobs},    # deterministic order for the assert
        log=lambda *a: None)
    # exactly one settle, strictly BETWEEN the two launches, before any poll
    assert [e[0] for e in ev][:3] == ["L", "S", "L"]
    assert ev[1][1] == ev[0][1]                                       # settle waits on the FIRST VM
    assert ("P", vm.ROLE_ANALYZER) in ev and ("P", vm.ROLE_SOURCE) in ev
    assert [e[0] for e in ev].count("S") == 1                         # NOT both passed through at once
    assert a_addr == ana.analyzer_net_addr and s_addr == src.source_net_addr


def test_ensure_golden_pair_degrades_to_healthy_unit_when_source_absent():
    # Fix 4 (preserved): one unit timing out does NOT abort the run -- return both addrs
    # (single-instrument verbs proceed) and DO NOT raise; only raise if NEITHER unit comes up.
    ana, src = vm.golden_pair()
    logs = []

    def fake_poll(s, **kw):
        if s.role == vm.ROLE_SOURCE:
            raise vm.BridgeUnavailable("the 68369A adapter (GPIB-USB-HS) FX2 is wedged")
        return s.net_addr

    a_addr, s_addr = vm.ensure_golden_pair(
        ana, src, reachable=lambda spec: False, detect=lambda: [],
        launch_one=lambda s, **kw: "launched", settle=lambda s, **kw: False, poll_ready=fake_poll,
        log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    assert a_addr == ana.analyzer_net_addr and s_addr == src.source_net_addr   # still returns both
    assert any("DEGRADED" in m and "source" in m for m in logs)               # marked ABSENT


def test_ensure_golden_pair_raises_only_when_both_units_absent():
    ana, src = vm.golden_pair()
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm.ensure_golden_pair(
            ana, src, reachable=lambda spec: False, detect=lambda: [],
            launch_one=lambda s, **kw: "launched", settle=lambda s, **kw: False,
            poll_ready=lambda s, **kw: (_ for _ in ()).throw(vm.BridgeUnavailable("wedged")),
            log=lambda *a: None)
    assert "NEITHER" in str(e.value)


def test_ensure_golden_pair_polls_units_in_parallel_independently():
    # R2: the two units are polled together (independent recovery), NOT a serial for-loop where a
    # wedged analyzer would delay the source's re-attach by the full timeout.
    ana, src = vm.golden_pair()
    submitted = {}

    def fake_parallel(jobs, fn):
        for key, spec in jobs:
            submitted[key] = spec.role                       # both units submitted together
        return {"analyzer": vm.BridgeUnavailable("analyzer wedged"), "source": None}

    a, s = vm.ensure_golden_pair(
        ana, src, reachable=lambda spec: False, detect=lambda: [],
        launch_one=lambda s, **kw: "launched", settle=lambda s, **kw: False,
        poll_ready=lambda s, **kw: s.net_addr, run_parallel=fake_parallel, log=lambda *a: None)
    assert set(submitted) == {"analyzer", "source"}          # BOTH polled in parallel
    assert a == ana.analyzer_net_addr and s == src.source_net_addr   # analyzer degraded, still returns


def test_run_parallel_captures_return_and_exception_per_key():
    def fn(x):
        if x == "boom":
            raise vm.BridgeUnavailable("wedged")
        return x
    out = vm._run_parallel([("a", "ok"), ("b", "boom")], fn)
    assert out["a"] == "ok" and isinstance(out["b"], vm.BridgeUnavailable)


def test_ensure_golden_pair_stops_first_if_it_fell_off_after_second_launch():
    # R1b RE-VERIFY: launching VM2 knocked VM1's adapter present->absent (a shared-controller reset
    # collision) -> STOP+report the first NOW; still DEGRADE to the second (returns both addrs).
    ana, src = vm.golden_pair()                              # unpinned -> first = source (HS)
    calls = {"n": 0}
    hs = vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "", "ni-gpib-hs", "HS", "r", True)

    def det():
        calls["n"] += 1
        return [hs] if calls["n"] == 1 else []               # present for the pre-snapshot, gone after

    logs = []
    a, s = vm.ensure_golden_pair(
        ana, src, reachable=lambda spec: False, detect=det,
        launch_one=lambda s, **kw: "launched", settle=lambda s, **kw: False,
        poll_ready=lambda s, **kw: s.net_addr, log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    assert a == ana.analyzer_net_addr and s == src.source_net_addr   # degraded, still returns both
    assert any("FELL OFF" in m and "source" in m for m in logs)      # STOP+report the collision


def test_await_host_settle_returns_on_stable_across_two_reads():
    # R1b: gate the second launch on REAL host state -- the first adapter STABLE across two reads
    # (its host-side claim/reset settled), NOT a blind timer.
    spec = vm.VmSpec(name="rx", role=vm.ROLE_ANALYZER)
    b = vm.UsbDevice(0x3923, 0x702b, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)
    slept = []
    ok = vm._await_host_settle(spec, detect=lambda: [b], reachable=lambda: False,
                               max_seconds=30.0, poll_interval=3.0,
                               sleep=lambda s: slept.append(s), log=lambda *a: None)
    assert ok is True and len(slept) <= 1        # settled on host state, not a blind 30s sleep


def test_await_host_settle_returns_immediately_when_reachable():
    ok = vm._await_host_settle(vm.VmSpec(name="rx", role=vm.ROLE_ANALYZER),
                               detect=lambda: [], reachable=lambda: True,
                               sleep=lambda s: (_ for _ in ()).throw(AssertionError("no sleep")),
                               log=lambda *a: None)
    assert ok is True


def test_await_host_settle_bounded_when_adapter_never_appears():
    slept = []
    ok = vm._await_host_settle(vm.VmSpec(name="rx", role=vm.ROLE_ANALYZER),
                               detect=lambda: [], reachable=lambda: False,
                               max_seconds=6.0, poll_interval=3.0,
                               sleep=lambda s: slept.append(s), log=lambda *a: None)
    assert ok is False and slept == [3.0, 3.0]   # bounded (6/3 steps), never forever


def test_adapters_share_controller_same_vs_different_bus():
    # R1a STRUCTURAL: same hostbus => shared controller (can race a shared reset); different => safe
    b = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)     # bus 0
    hs_same = vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "", "ni-gpib-hs", "HS", "r", True)  # bus 0
    hs_other = vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x14130000, "", "ni-gpib-hs", "HS", "r", True)  # bus 0x14
    assert vm.adapters_share_controller([b, hs_same]) == (True, 0)
    assert vm.adapters_share_controller([b, hs_other]) == (False, None)
    assert vm.adapters_share_controller([b]) == (False, None)          # one absent -> not shared


def test_shared_controller_warning_only_when_shared():
    b = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)
    hs = vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "", "ni-gpib-hs", "HS", "r", True)
    w = vm._shared_controller_warning([b, hs])
    assert w and "SAME host USB controller" in w and "different controller" in w.lower()
    assert vm._shared_controller_warning([b]) is None


def test_poll_ready_settles_between_two_device_adds_in_both_role():
    # a role=both VM (two adapters, ONE qemu): if both re-attaches fire in the SAME poll, a settle
    # is inserted BETWEEN the two device_adds so they don't race on the host controller.
    spec = vm.VmSpec(role=vm.ROLE_BOTH, port=6300,
                     analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4",
                     source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")
    seq = []
    n = {"i": 0}

    def reach():
        n["i"] += 1
        return n["i"] >= 2                              # down on the first poll -> both reattach

    vm._poll_ready(
        spec, reachable=reach, detect=lambda: [],       # empty detect -> no real ioreg, no drift
        reattach=lambda s, **kw: seq.append("reattach_A") or True,
        reattach_source=lambda s, **kw: seq.append("reattach_S") or True,
        settle_s=2.0, poll_interval=1.0, wait_timeout=100.0,
        sleep=lambda s: seq.append(("sleep", s)), log=lambda *a: None)
    ai, si = seq.index("reattach_A"), seq.index("reattach_S")
    assert ai < si and ("sleep", 2.0) in seq[ai + 1:si]            # settle strictly between the two


def test_both_role_keeps_hs_on_ehci_b_on_xhci():
    # the two adapters MUST stay on SEPARATE qemu controllers (HS -> ehci, B -> xhci). Golden runs
    # them in separate VMs; the both-role single VM keeps them split too.
    argv = " ".join(vm.build_qemu_argv(vm.VmSpec(role=vm.ROLE_BOTH), "d", "s", "u"))
    assert "usb-host,bus=ehci.0,vendorid=0x3923,productid=0x709b,id=ni_hs" in argv  # HS on EHCI
    assert "vendorid=0x3923,id=ni_b" in argv and "bus=xhci.0" in argv               # B on XHCI


def test_ensure_golden_pair_launches_source_first_when_analyzer_unpinned():
    # Fix 6: a vendor-only (unpinned) analyzer match could grab the HS -> launch the HS VM FIRST.
    ana, src = vm.golden_pair()                                       # no devices -> unpinned
    order = []
    vm.ensure_golden_pair(ana, src, reachable=lambda spec: True, detect=lambda: [],  # up -> no poll
                          launch_one=lambda s, **kw: order.append(s.role) or "up",
                          poll_ready=lambda s, **kw: s.net_addr, log=lambda *a: None)
    assert order[0] == vm.ROLE_SOURCE


def test_ensure_golden_pair_launches_analyzer_first_when_pinned():
    devs = [vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "NI GPIB-USB-B", "r", True),
            vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "01", "ni-gpib-hs", "NI GPIB-USB-HS", "r", True)]
    ana, src = vm.golden_pair(devices=devs)                          # analyzer pinned -> no wrong-grab
    order = []
    vm.ensure_golden_pair(ana, src, reachable=lambda spec: True, detect=lambda: [],
                          launch_one=lambda s, **kw: order.append(s.role) or "up",
                          poll_ready=lambda s, **kw: s.net_addr, log=lambda *a: None)
    assert order[0] == vm.ROLE_ANALYZER


def test_ensure_golden_pair_warns_when_both_adapters_share_one_controller():
    # R1a: both adapters pinned to the SAME hostbus -> warn the operator at bring-up
    devs = [vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True),   # bus 0
            vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "", "ni-gpib-hs", "HS", "r", True)]  # bus 0
    ana, src = vm.golden_pair(devices=devs)                          # both pinned, same hostbus 0
    logs = []
    vm.ensure_golden_pair(ana, src, reachable=lambda spec: True, detect=lambda: [],
                          launch_one=lambda s, **kw: "up", poll_ready=lambda s, **kw: s.net_addr,
                          log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    assert any("SAME host USB controller" in m for m in logs)


def test_prepare_assets_per_instance_isolation(tmp_path):
    # two instances -> two workdirs -> disjoint disk/seed; each cloud-init carries its own role
    ana, src = vm.golden_pair()
    aw, sw = str(tmp_path / "rx"), str(tmp_path / "tx")
    aa = vm.prepare_assets(ana, workdir=aw, run=lambda a: None)
    sa = vm.prepare_assets(src, workdir=sw, run=lambda a: None)
    assert aa.image != sa.image and aa.seed != sa.seed               # disjoint disk overlays
    assert "role analyzer" in (tmp_path / "rx" / "cidata" / "user-data").read_text().replace("=", " ")
    assert "provision.sh source" in (tmp_path / "tx" / "cidata" / "user-data").read_text()


class _FakeProc:
    def __init__(self): self._alive = True
    def poll(self): return None if self._alive else 0
    def terminate(self): self._alive = False


def test_qemuvm_lifecycle_is_running_then_stops():
    v = vm.QemuVm(vm.VmSpec(name="rx"))
    assert v.is_running() is False                                   # not launched yet
    v._proc = _FakeProc()
    assert v.is_running() is True
    v.stop()
    assert v.is_running() is False


def test_two_concurrent_instances_share_no_qemu_resource():
    # every collision-prone qemu resource is disjoint across two live instances
    ana, src = vm.golden_pair()
    a = vm.build_qemu_argv(ana, "da", "sa", "u")
    s = vm.build_qemu_argv(src, "db", "sb", "u")
    # qmp sockets, MACs, and every hostfwd host-port differ
    assert ana.qmp_sock() != src.qmp_sock()
    import re
    a_ports = set(re.findall(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-", " ".join(a)))
    s_ports = set(re.findall(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-", " ".join(s)))
    assert a_ports and s_ports and a_ports.isdisjoint(s_ports)       # no host-port collision
    a_mac = next(x.split("mac=")[1] for x in a if "mac=" in x)
    s_mac = next(x.split("mac=")[1] for x in s if "mac=" in x)
    assert a_mac != s_mac


def test_build_qemu_argv_respects_spec_port_and_vendor():
    argv = " ".join(vm.build_qemu_argv(vm.VmSpec(port=6000, usb_vendor_id=0x3923),
                                       "d", "s", "u"))
    assert "hostfwd=tcp:127.0.0.1:6000-:6000" in argv


def test_render_cloud_init_runs_provision_for_both_units():
    meta, user = vm.render_cloud_init(
        vm.VmSpec(gpib_addr=18, source_addr=5, port=5555, source_port=5556))
    assert "instance-id: se299-gpib" in meta
    assert "gpibbridge" in user and "/opt/gpib_bridge" in user
    # provisions BOTH boards (analyzer pad 18 on port 5555, source pad 5 on port 5556), binding
    # 0.0.0.0 --insecure so qemu's hostfwd (which targets the guest eth0 IP, not loopback) can
    # reach it -- the guest is NAT-isolated so only the host hostfwd sees the ports.
    assert "BIND_HOST=0.0.0.0 INSECURE=yes bash /opt/gpib_bridge/provision.sh both 18 5 5555 5556" in user
    assert "role=both" in user
    assert "ssh_authorized_keys" not in user                 # no key -> no ssh block


def test_render_cloud_init_injects_ssh_key_when_present():
    meta, user = vm.render_cloud_init(vm.VmSpec(ssh_pubkey="ssh-ed25519 AAAAtestkey user@mac"))
    assert "ssh_authorized_keys" in user and "ssh-ed25519 AAAAtestkey user@mac" in user
    assert "ssh_pwauth: false" in user


def test_vmspec_net_addr():
    assert vm.VmSpec().net_addr == "net:127.0.0.1:5555:18"
    assert vm.VmSpec(port=6000, gpib_addr=3).net_addr == "net:127.0.0.1:6000:3"


def test_vmspec_analyzer_and_source_addrs_on_separate_ports():
    # the two instruments are on SEPARATE NI adapters / boards => separate bridge ports
    s = vm.VmSpec(port=5555, source_port=5556, gpib_addr=18, source_addr=5)
    assert s.analyzer_net_addr == "net:127.0.0.1:5555:18"
    assert s.source_net_addr == "net:127.0.0.1:5556:5"
    assert s.analyzer_net_addr == s.net_addr


# ------------------------------------------------- STAGE 2 REACHABILITY: routable --vm-bind exposure

def test_default_bind_is_loopback_only_unchanged():
    # the single-machine default binds ONLY loopback -- today's behavior, unchanged
    joined = " ".join(vm.build_qemu_argv(vm.VmSpec(), "d", "s", "u"))
    assert "hostfwd=tcp:127.0.0.1:5555-:5555" in joined
    assert "hostfwd=tcp:127.0.0.1:5556-:5556" in joined
    assert "0.0.0.0" not in joined                                   # not exposed to the LAN


def test_routable_bind_exposes_bridge_ports_but_keeps_ssh_loopback():
    # --vm-bind 0.0.0.0 (with a token) exposes the BRIDGE ports on all interfaces so a client on
    # ANOTHER host can reach them; ssh (:2222) STAYS loopback (diagnostics, never LAN-exposed).
    spec = vm.VmSpec(bind_host="0.0.0.0", bridge_token="s3cret")     # role=both default
    joined = " ".join(vm.build_qemu_argv(spec, "d", "s", "u"))
    assert "hostfwd=tcp:0.0.0.0:5555-:5555" in joined               # analyzer bridge on the LAN
    assert "hostfwd=tcp:0.0.0.0:5556-:5556" in joined               # source bridge on the LAN
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in joined              # ssh stays loopback
    assert "hostfwd=tcp:0.0.0.0:2222" not in joined                # ssh never LAN-exposed


def test_routable_bind_without_token_is_refused_in_argv_and_guard():
    # a routable bind with NO token would publish an UNAUTHENTICATED instrument-control service --
    # build_qemu_argv REFUSES (the actual exposing point), mirroring ni_gpib_server.main.
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm.build_qemu_argv(vm.VmSpec(bind_host="0.0.0.0"), "d", "s", "u")
    assert "without a token" in str(e.value) and "0.0.0.0" in str(e.value)
    # the guard helper raises the same way and is a NO-OP for the loopback default
    with pytest.raises(vm.BridgeUnavailable):
        vm.guard_bind_auth(vm.VmSpec(bind_host="0.0.0.0"))
    vm.guard_bind_auth(vm.VmSpec())                                  # default loopback -> no raise
    vm.guard_bind_auth(vm.VmSpec(bind_host="0.0.0.0", bridge_token="ok"))  # routable + token -> ok


def test_net_addr_reports_routable_lan_ip_when_bound_routable(monkeypatch):
    # so coordinator/checkpath/sa/sg get a REACHABLE address, not the unreachable 127.0.0.1
    monkeypatch.setattr(vm, "host_lan_ip", lambda *a, **k: "192.168.7.20")
    spec = vm.VmSpec(bind_host="0.0.0.0", bridge_token="t", port=5555, source_port=5556,
                     gpib_addr=18, source_addr=5)                    # role=both default
    assert spec.advertised_host() == "192.168.7.20"
    assert spec.analyzer_net_addr == "net:192.168.7.20:5555:18"
    assert spec.source_net_addr == "net:192.168.7.20:5556:5"
    assert spec.net_addr == "net:192.168.7.20:5555:18"


def test_net_addr_stays_loopback_by_default():
    # the default single-machine path advertises loopback (existing callers unchanged)
    assert vm.VmSpec().analyzer_net_addr == "net:127.0.0.1:5555:18"
    assert vm.VmSpec().net_addr == "net:127.0.0.1:5555:18"


def test_advertised_host_uses_a_concrete_bind_ip_directly():
    # a concrete routable bind IP is dialable as-is (no LAN-IP resolution needed)
    spec = vm.VmSpec(bind_host="192.168.9.9", bridge_token="t")
    assert spec.advertised_host() == "192.168.9.9"
    assert spec.net_addr == "net:192.168.9.9:5555:18"


def test_host_lan_ip_returns_probe_address_else_loopback_fallback():
    class _Ok:
        def connect(self, *a): pass
        def getsockname(self): return ("10.1.2.3", 54321)
        def close(self): pass

    class _Boom:
        def connect(self, *a): raise OSError("no route to host")
        def getsockname(self): return ("x", 0)
        def close(self): pass

    assert vm.host_lan_ip(sock_factory=_Ok) == "10.1.2.3"            # learns the outbound iface addr
    assert vm.host_lan_ip(sock_factory=_Boom) == "127.0.0.1"        # unresolvable -> loopback fallback


def test_golden_pair_net_addrs_are_routable_when_bound(monkeypatch):
    # the golden two-VM addresses the coordinator receives are LAN-reachable when bound routable
    import dataclasses
    monkeypatch.setattr(vm, "host_lan_ip", lambda *a, **k: "192.168.5.5")
    ana, src = vm.golden_pair()
    ana = dataclasses.replace(ana, bind_host="0.0.0.0", bridge_token="t")
    src = dataclasses.replace(src, bind_host="0.0.0.0", bridge_token="t")
    assert ana.analyzer_net_addr == f"net:192.168.5.5:{ana.port}:18"
    assert src.source_net_addr == f"net:192.168.5.5:{src.port}:5"


# ----------------------------------------------------------------- reachability (real fake bridge)

def test_bridge_reachable_true_against_fake_bridge():
    port = _free_port()
    _start_fake(port)
    assert vm.bridge_reachable(vm.VmSpec(port=port)) is True


def test_bridge_reachable_false_when_nothing_listening():
    assert vm.bridge_reachable(vm.VmSpec(port=_free_port()), timeout_ms=500) is False


def test_both_instruments_reachable_through_two_bridges():
    # the core of "both units through one VM": TWO bridges (two fakes stand in for the two
    # boards behind the VM) -- the 8565EC on the analyzer port, the 68369A on the source port.
    aport, sport = _free_port(), _free_port()
    _start_fake(aport)                                       # analyzer board (answers pad 18)
    _start_fake(sport)                                       # source board (answers pad 5)
    spec = vm.VmSpec(port=aport, source_port=sport, gpib_addr=18, source_addr=5)
    assert vm.bridge_reachable(spec) is True                 # analyzer (RX)
    assert vm.source_reachable(spec) is True                 # source (TX)
    assert vm.both_reachable(spec) is True                   # coordinator readiness gate


def test_both_reachable_false_when_only_analyzer_up():
    # the analyzer bridge is up but the source bridge is not -> NOT ready (names the gap)
    aport, sport = _free_port(), _free_port()
    _start_fake(aport)
    spec = vm.VmSpec(port=aport, source_port=sport, gpib_addr=18, source_addr=5)
    assert vm.bridge_reachable(spec) is True
    assert vm.source_reachable(spec, timeout_ms=500) is False
    assert vm.both_reachable(spec, timeout_ms=500) is False


def test_both_reachable_false_when_nothing_listening():
    assert vm.both_reachable(vm.VmSpec(port=_free_port(), source_port=_free_port()),
                             timeout_ms=500) is False


# ----------------------------------------------------------------- asset prep

def test_asset_argv_builders():
    assert vm.download_argv("http://x/y.img", "/w/d.qcow2")[:2] == ["curl", "-L"]
    assert vm.resize_argv("/w/d.qcow2") == ["qemu-img", "resize", "/w/d.qcow2", "16G"]
    assert vm.overlay_argv("/w/base.qcow2", "/w/d.qcow2") == \
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", "/w/base.qcow2", "/w/d.qcow2"]
    s = vm.seed_argv("/w/cidata", "/w/seed.iso")
    assert s[0] == "hdiutil" and "cidata" in s and "/w/seed.iso" in s


def test_prepare_assets_writes_cloudinit_and_calls_tools(tmp_path):
    wd = tmp_path / "inst"                                # unique workdir -> per-test shared-cache root
    calls = []
    assets = vm.prepare_assets(vm.VmSpec(), workdir=str(wd),
                               run=lambda a: calls.append(a[0]))
    assert "provision.sh" in (wd / "cidata" / "user-data").read_text()
    assert (wd / "cidata" / "meta-data").exists()
    # base download (curl + qemu-img resize), the overlay (qemu-img create), and the seed
    assert "curl" in calls and "qemu-img" in calls and "hdiutil" in calls
    assert assets.image.endswith("disk.qcow2") and assets.seed.endswith("seed.iso")


def test_prepare_assets_is_idempotent(tmp_path):
    # base + overlay + seed all present -> nothing is re-downloaded or re-made
    wd = tmp_path / "inst"
    wd.mkdir()
    (wd / "disk-base.qcow2").write_text("base")
    (wd / "disk.qcow2").write_text("overlay")
    (wd / "seed.iso").write_text("x")
    calls = []
    vm.prepare_assets(vm.VmSpec(), workdir=str(wd), run=lambda a: calls.append(a[0]))
    assert calls == []                                   # nothing re-downloaded / re-made


def test_prepare_assets_reset_recreates_overlay_without_redownloading_base(tmp_path):
    # the failed-provision recovery: reset rebuilds the overlay + seed but KEEPS the base
    wd = tmp_path / "inst"
    wd.mkdir()
    (wd / "disk-base.qcow2").write_text("base")
    (wd / "disk.qcow2").write_text("stale-overlay")
    (wd / "seed.iso").write_text("stale")
    calls = []
    vm.prepare_assets(vm.VmSpec(), workdir=str(wd),
                      run=lambda a: calls.append(a[0]), reset=True)
    assert "curl" not in calls                           # base NOT re-downloaded
    assert "qemu-img" in calls and "hdiutil" in calls    # overlay + seed rebuilt
    assert not (wd / "disk.qcow2").exists()               # stale overlay removed (stub won't recreate)


def test_prepare_assets_shares_base_cache_across_instances(tmp_path):
    # the ~600 MB image is downloaded ONCE to a shared cache; a second instance reuses it (no re-download)
    downloads = {"n": 0}

    def run(a):
        if a[0] == "curl":                               # download_argv: [curl,-L,--fail,-o,dest,url]
            downloads["n"] += 1
            open(a[4], "w").close()                      # materialize the base so the 2nd call sees it

    vm.prepare_assets(vm.VmSpec(name="rx"), workdir=str(tmp_path / "rx"), run=run)
    vm.prepare_assets(vm.VmSpec(name="tx"), workdir=str(tmp_path / "tx"), run=run)
    assert downloads["n"] == 1                            # downloaded once, shared by both instances
    assert (tmp_path / "_base").is_dir()                  # shared cache lives beside the instances


def test_prepare_assets_seeds_shared_cache_from_existing_base_without_download(tmp_path):
    # a machine provisioned BEFORE the shared cache existed: seed it from the old per-instance base
    # (hardlink) instead of re-downloading.
    old = tmp_path / "se299-rx"
    old.mkdir()
    (old / "disk-base.qcow2").write_text("already-downloaded-base")
    calls = []
    vm.prepare_assets(vm.VmSpec(name="tx"), workdir=str(tmp_path / "se299-tx"),
                      run=lambda a: calls.append(a[0]))
    assert "curl" not in calls                            # reused the existing base -> no download
    assert (tmp_path / "_base" / "ubuntu-24.04-server-cloudimg-arm64.qcow2").exists()


# ----------------------------------------------------------------- ensure_bridge (NO fake)

def _ni_detect():
    return lambda: vm.detect_gpib_usb(raw=_usb_plist())


def test_ensure_bridge_already_up():
    assert vm.ensure_bridge(vm.VmSpec(), reachable=lambda: True,
                            log=lambda *a: None) == vm.VmSpec().net_addr


def test_ensure_bridge_qemu_missing_raises():
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm.ensure_bridge(vm.VmSpec(), reachable=lambda: False,
                         qemu_check=lambda: (False, "brew install qemu"), log=lambda *a: None)
    assert "brew install qemu" in str(e.value)


def test_ensure_bridge_no_ni_adapter_raises():
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm.ensure_bridge(vm.VmSpec(), reachable=lambda: False,
                         qemu_check=lambda: (True, "ok"), detect=lambda: [], log=lambda *a: None)
    assert "no NI GPIB adapter" in str(e.value)


def test_ensure_bridge_boots_then_becomes_reachable():
    n = {"i": 0}
    launched = {"n": 0}

    def reach():
        n["i"] += 1
        return n["i"] >= 3                               # False, False, True

    addr = vm.ensure_bridge(
        vm.VmSpec(), reachable=reach, qemu_check=lambda: (True, "ok"), detect=_ni_detect(),
        prepare=lambda s: vm.AssetPaths("i", "s", "u"),
        launch=lambda s, a: launched.__setitem__("n", launched["n"] + 1),
        already_booting=lambda s: False,
        sleep=lambda s: None, poll_interval=1.0, wait_timeout=100.0, log=lambda *a: None)
    assert addr == vm.VmSpec().net_addr and launched["n"] == 1


def test_ensure_bridge_times_out_and_points_at_the_log():
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm.ensure_bridge(
            vm.VmSpec(), reachable=lambda: False, qemu_check=lambda: (True, "ok"),
            detect=_ni_detect(), prepare=lambda s: vm.AssetPaths("i", "s", "u"),
            launch=lambda s, a: None, already_booting=lambda s: False, sleep=lambda s: None,
            poll_interval=1.0, wait_timeout=3.0, log=lambda *a: None)
    assert "qemu.log" in str(e.value) or "did not answer" in str(e.value)


# ----------------------------------------------------------- B QMP re-attach (FX2 re-enumeration)

def _b_dev(pid):
    return vm.UsbDevice(0x3923, pid, "GPIB-USB-B", "NI", 0x00140000, "", "ni-gpib-b",
                        "GPIB-USB-B", "", True)


def test_match_to_qmp_args_coerces_types():
    a = vm.match_to_qmp_args("vendorid=0x3923,hostbus=0,hostport=1.4")
    assert a == {"vendorid": 0x3923, "hostbus": 0, "hostport": "1.4"}   # hostport stays a string


def test_reattach_skips_when_not_hostport_pinned():
    # a vendor-only match -> we don't know the physical port -> never re-attach
    fired = []
    ok = vm.reattach_analyzer_b(vm.VmSpec(), detect=lambda: [_b_dev(0x702a)],
                                execute=lambda *a, **k: fired.append(a), log=lambda *a: None)
    assert ok is False and fired == []


def test_reattach_skips_until_host_b_reaches_0x702a():
    spec = vm.VmSpec(analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    fired = []
    ok = vm.reattach_analyzer_b(spec, detect=lambda: [_b_dev(0x702b)],   # still cold
                                execute=lambda *a, **k: fired.append(a), log=lambda *a: None)
    assert ok is False and fired == []


def test_reattach_fires_device_del_then_add_when_pinned_and_fxloaded():
    spec = vm.VmSpec(analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    cmds = []
    ok = vm.reattach_analyzer_b(
        spec, detect=lambda: [_b_dev(0x702a)],                           # fxloaded on host
        execute=lambda sock, c, **k: cmds.append(c[0]), sleep=lambda s: None,
        log=lambda *a: None)
    assert ok is True
    assert cmds[0]["execute"] == "device_del" and cmds[0]["arguments"]["id"] == "ni_b"
    add = cmds[1]
    assert add["execute"] == "device_add"
    assert add["arguments"]["driver"] == "usb-host" and add["arguments"]["bus"] == "xhci.0"
    assert add["arguments"]["hostport"] == "1.4" and add["arguments"]["id"] == "ni_b"


def test_reattach_never_raises_on_qmp_failure():
    spec = vm.VmSpec(analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    def boom(*a, **k):
        raise IOError("qmp socket closed")
    ok = vm.reattach_analyzer_b(spec, detect=lambda: [_b_dev(0x702a)],
                                execute=boom, sleep=lambda s: None, log=lambda *a: None)
    assert ok is False                                                   # swallowed, not raised


def test_ensure_bridge_reattaches_b_once_then_becomes_reachable():
    # analyzer down for the first polls; a single re-attach fires, then it comes up
    spec = vm.VmSpec(analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    reattach_calls = {"n": 0}
    n = {"i": 0}

    def reach():
        n["i"] += 1
        return n["i"] >= 3

    def fake_reattach(s, *, log=None, detect=None):
        reattach_calls["n"] += 1
        return True                                     # claims it re-attached

    addr = vm.ensure_bridge(
        spec, reachable=reach, qemu_check=lambda: (True, "ok"), detect=_ni_detect(),
        prepare=lambda s: vm.AssetPaths("i", "s", "u"), launch=lambda s, a: None,
        reattach=fake_reattach, already_booting=lambda s: False, sleep=lambda s: None,
        poll_interval=1.0, wait_timeout=100.0, log=lambda *a: None)
    assert addr == spec.net_addr
    assert reattach_calls["n"] == 1                     # fired exactly once (not every poll)


# ----------------------------------------------- HS (source) QMP re-attach (FX2 wedge, Fix 1)

def _hs_dev(pid=0x709b, loc=0x00130000):
    return vm.UsbDevice(0x3923, pid, "GPIB-USB-HS", "NI", loc, "01D088F4", "ni-gpib-hs",
                        "NI GPIB-USB-HS", "ready", True)


def test_reattach_source_skips_when_not_hostport_pinned():
    # default source_match is vendor+productid (no hostport) -> we don't know the port -> skip
    fired = []
    ok = vm.reattach_source_hs(vm.VmSpec(role=vm.ROLE_SOURCE), detect=lambda: [_hs_dev()],
                               execute=lambda *a, **k: fired.append(a), log=lambda *a: None)
    assert ok is False and fired == []


def test_reattach_source_skips_when_hs_absent_from_host():
    spec = vm.VmSpec(role=vm.ROLE_SOURCE, source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")
    fired = []
    ok = vm.reattach_source_hs(spec, detect=lambda: [],              # HS not on the bus
                               execute=lambda *a, **k: fired.append(a), log=lambda *a: None)
    assert ok is False and fired == []


def test_reattach_source_fires_device_del_then_add_on_ehci_when_pinned():
    spec = vm.VmSpec(role=vm.ROLE_SOURCE, source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")
    cmds = []
    ok = vm.reattach_source_hs(
        spec, detect=lambda: [_hs_dev()], sleep=lambda s: None,
        execute=lambda sock, c, **k: cmds.append(c[0]), log=lambda *a: None)
    assert ok is True
    assert cmds[0]["execute"] == "device_del" and cmds[0]["arguments"]["id"] == "ni_hs"
    add = cmds[1]
    assert add["execute"] == "device_add"
    assert add["arguments"]["driver"] == "usb-host" and add["arguments"]["bus"] == "ehci.0"
    assert add["arguments"]["hostport"] == "1.3" and add["arguments"]["id"] == "ni_hs"


def test_reattach_source_never_raises_on_qmp_failure():
    spec = vm.VmSpec(role=vm.ROLE_SOURCE, source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")

    def boom(*a, **k):
        raise IOError("qmp closed")
    assert vm.reattach_source_hs(spec, detect=lambda: [_hs_dev()], execute=boom,
                                 sleep=lambda s: None, log=lambda *a: None) is False


def test_ensure_bridge_source_role_reattaches_hs_once():
    # a SOURCE VM now gets its own wedge-recovery in the poll loop (previously NONE)
    spec = vm.VmSpec(role=vm.ROLE_SOURCE, port=6100,
                     source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")
    calls = {"n": 0}
    n = {"i": 0}

    def reach():
        n["i"] += 1
        return n["i"] >= 3

    def fake_src_reattach(s, *, log=None, detect=None):
        calls["n"] += 1
        return True

    addr = vm.ensure_bridge(
        spec, reachable=reach, qemu_check=lambda: (True, "ok"),
        detect=lambda: [_hs_dev()], prepare=lambda s: vm.AssetPaths("i", "s", "u"),
        launch=lambda s, a: None, reattach_source=fake_src_reattach,
        already_booting=lambda s: False, sleep=lambda s: None, poll_interval=1.0,
        wait_timeout=100.0, log=lambda *a: None)
    assert addr == spec.net_addr and calls["n"] == 1


# ----------------------------------------------- duplicate-launch guard + liveness (Fix 3)

def test_instance_is_live_true_if_any_probe_true():
    s = vm.VmSpec(name="probe")
    assert vm.instance_is_live(s, pidfile_live=lambda x: False,
                               qmp_live=lambda x: False, port_live=lambda x: True) is True
    assert vm.instance_is_live(s, pidfile_live=lambda x: True,
                               qmp_live=lambda x: False, port_live=lambda x: False) is True
    assert vm.instance_is_live(s, pidfile_live=lambda x: False,
                               qmp_live=lambda x: True, port_live=lambda x: False) is True


def test_instance_is_live_false_when_all_probes_false():
    s = vm.VmSpec(name="probe")
    assert vm.instance_is_live(s, pidfile_live=lambda x: False,
                               qmp_live=lambda x: False, port_live=lambda x: False) is False


def test_pidfile_roundtrip_and_dead_pid(tmp_path):
    p = str(tmp_path / "qemu.pid")
    assert vm.read_pid(p) is None                                    # missing file
    vm.write_pid(p, 4242)
    assert vm.read_pid(p, alive=lambda pid: pid == 4242) == 4242     # live
    assert vm.read_pid(p, alive=lambda pid: False) is None           # dead pid -> None


def test_port_bound_true_against_live_socket_false_against_free():
    port = _free_port()
    assert vm.port_bound(port, timeout=0.2) is False                 # nothing listening yet
    _start_fake(port)
    import time as _t; _t.sleep(0.05)
    assert vm.port_bound(port, timeout=0.5) is True                  # fake bridge accepts


def test_ensure_bridge_duplicate_guard_skips_launch_when_already_booting():
    # a re-run inside the boot window must NOT launch a second qemu; it joins the poll instead
    launched = {"n": 0}
    n = {"i": 0}

    def reach():
        n["i"] += 1
        return n["i"] >= 2

    addr = vm.ensure_bridge(
        vm.VmSpec(), reachable=reach, qemu_check=lambda: (True, "ok"), detect=_ni_detect(),
        prepare=lambda s: vm.AssetPaths("i", "s", "u"),
        launch=lambda s, a: launched.__setitem__("n", launched["n"] + 1),
        already_booting=lambda s: True,                             # a qemu is already booting
        sleep=lambda s: None, poll_interval=1.0, wait_timeout=100.0, log=lambda *a: None)
    assert addr == vm.VmSpec().net_addr and launched["n"] == 0       # NO second qemu launched


# ----------------------------------------------- hostport drift (Fix 7) + recovery text (Fix 2)

def test_hostport_drift_detects_moved_adapter():
    spec = vm.VmSpec(role=vm.ROLE_ANALYZER,
                     analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    moved = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00160000, "", "ni-gpib-b",
                         "NI GPIB-USB-B", "ready", True)                # now on port 1.6
    assert vm.hostport_drift(spec, "ni-gpib-b", detect=lambda: [moved]) == ("1.4", "1.6")
    same = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b",
                        "NI GPIB-USB-B", "ready", True)                 # still on 1.4
    assert vm.hostport_drift(spec, "ni-gpib-b", detect=lambda: [same]) is None
    # not pinned -> no drift concept
    assert vm.hostport_drift(vm.VmSpec(role=vm.ROLE_ANALYZER), "ni-gpib-b",
                             detect=lambda: [moved]) is None


def test_timeout_message_recovery_wedged_vs_absent_source_role():
    spec = vm.VmSpec(role=vm.ROLE_SOURCE, port=6200)
    # HS present on host but unit never answered -> wedged-FX2 replug recovery
    m1 = vm._timeout_message(spec, "the 68369A", 240.0, lambda: [_hs_dev()])
    assert "FX2 is wedged" in m1 and "SAME USB port" in m1 and "--vm-reset" in m1
    # HS absent from host -> 'not on the bus'
    m2 = vm._timeout_message(spec, "the 68369A", 240.0, lambda: [])
    assert "not on the USB bus" in m2


def test_analyzer_timeout_message_has_per_unit_detail():
    spec = vm.VmSpec(role=vm.ROLE_ANALYZER, port=6201)
    m = vm._timeout_message(spec, "the 8565EC", 240.0, lambda: [_b_dev(0x702a)])
    assert "analyzer pad" in m and "FX2 is wedged" in m


# ----------------------------------------------- vm-stop (Fix 9)

def test_stop_instance_health_aware_and_surgical(tmp_path, monkeypatch):
    # R5: never stop a HEALTHY instance without force; only unlink a pidfile actually stopped
    spec = vm.VmSpec(name="stopme")
    monkeypatch.setattr(spec.__class__, "workdir", lambda self: str(tmp_path))
    os.makedirs(str(tmp_path), exist_ok=True)
    # HEALTHY (reachable) + not forced -> SKIP, leave the live pidfile intact
    vm.write_pid(vm.pidfile_path(spec), os.getpid())
    st = vm.stop_instance(spec, reachable=lambda: True, force=False, log=lambda *a: None)
    assert st == "skipped-healthy"
    assert vm.read_pid(vm.pidfile_path(spec)) == os.getpid()          # NOT unlinked (never stopped it)
    # unhealthy -> SIGTERM the stored pid + unlink (kill injected so nothing dies)
    killed = []
    st = vm.stop_instance(spec, reachable=lambda: False,
                          kill=lambda pid, sig: killed.append((pid, sig)), log=lambda *a: None)
    assert st == "stopped" and killed and killed[0][0] == os.getpid()
    assert vm.read_pid(vm.pidfile_path(spec)) is None                # unlinked after a real stop
    # nothing left -> not-running (no pidfile, no qmp)
    assert vm.stop_instance(spec, reachable=lambda: False, log=lambda *a: None) == "not-running"


def test_stop_instance_force_stops_even_if_healthy(tmp_path, monkeypatch):
    spec = vm.VmSpec(name="forceme")
    monkeypatch.setattr(spec.__class__, "workdir", lambda self: str(tmp_path))
    os.makedirs(str(tmp_path), exist_ok=True)
    vm.write_pid(vm.pidfile_path(spec), os.getpid())
    killed = []
    st = vm.stop_instance(spec, force=True, reachable=lambda: True,   # healthy but --name forces it
                          kill=lambda pid, sig: killed.append(pid), log=lambda *a: None)
    assert st == "stopped" and killed == [os.getpid()]


def test_poll_ready_early_wedge_verdict_present_but_silent():
    # R6 (corrected): once the guest FINISHED provisioning (console shows the bridge-launch stage) yet
    # the adapter is PRESENT-but-SILENT past the grace, classify ADAPTER_WEDGED immediately (do not
    # grind to the full timeout). The provisioning gate (read_console) is what stops this from firing
    # on a still-booting guest; here we inject a provisioned console so the real-wedge verdict fires.
    spec = vm.VmSpec(role=vm.ROLE_ANALYZER, port=6400,
                     analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    b = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)  # present
    fires = {"n": 0}

    def fake_reattach(s, *, log=None, detect=None):
        fires["n"] += 1
        return True

    with pytest.raises(vm.BridgeUnavailable) as e:
        vm._poll_ready(spec, reachable=lambda: False, detect=lambda: [b],
                       reattach=fake_reattach, wedge_after_attempts=2, poll_interval=1.0,
                       wedge_grace_s=0.0, read_console=lambda s: "=== 5. bridge launcher ===",
                       wait_timeout=1000.0, sleep=lambda s: None, log=lambda *a: None)
    assert "ADAPTER_WEDGED" in str(e.value) and "re-plug" in str(e.value)
    assert fires["n"] == 2                        # spent the budget, then verdict NOW (not 1000s)


def test_poll_ready_waits_through_provisioning_no_premature_wedge():
    # REGRESSION (live-confirmed false positive): a FRESH-provision guest brings its board online and
    # answers only ~60-120s in. While the console has NOT yet reached the bridge-launch stage the
    # poller must KEEP WAITING even with the adapter present and the re-attach budget spent -- it must
    # NOT declare ADAPTER_WEDGED. Before the fix this raised at ~2 polls, killing a healthy bring-up.
    spec = vm.VmSpec(role=vm.ROLE_SOURCE, port=6402,
                     source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")
    hs = vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "01", "ni-gpib-hs", "HS", "r", True)
    st = {"i": 0}

    def reach():
        st["i"] += 1
        return st["i"] >= 18                       # bridge answers only at poll 18 (slow provision)

    def read_console(s):                            # guest reaches bridge-launch stage at ~poll 12
        return "=== 5. bridge launcher ===" if st["i"] >= 12 else ""

    addr = vm._poll_ready(
        spec, reachable=reach, detect=lambda: [hs],
        reattach_source=lambda s, **kw: True,       # budget spends on the first polls
        wedge_after_attempts=2, wedge_grace_s=10.0, read_console=read_console,
        poll_interval=1.0, wait_timeout=100.0, sleep=lambda s: None, log=lambda *a: None)
    assert addr == spec.net_addr                    # reached readiness; NEVER prematurely wedged


def test_poll_ready_no_wedge_while_unprovisioned_even_past_budget():
    # Tighter guard: adapter present + budget spent but the guest console NEVER reaches the bridge
    # stage within the window -> the poller must NOT wedge; it runs to the timeout message (a
    # genuinely-stuck guest is a timeout, not a false present-but-silent wedge).
    spec = vm.VmSpec(role=vm.ROLE_ANALYZER, port=6406,
                     analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    b = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm._poll_ready(spec, reachable=lambda: False, detect=lambda: [b],
                       reattach=lambda s, **kw: True, wedge_after_attempts=2,
                       wedge_grace_s=5.0, read_console=lambda s: "",  # never provisioned
                       poll_interval=1.0, wait_timeout=8.0, sleep=lambda s: None,
                       log=lambda *a: None)
    assert "ADAPTER_WEDGED" not in str(e.value)    # not a wedge -- the guest never provisioned
    assert "did not answer" in str(e.value)         # the timeout message instead


# =========================================== SINGLETON (Phase 2: one VM, serial hot-plug)

def test_singleton_argv_boots_empty_controllers_and_zero_usb_host():
    # ARGV SPLIT: hotplug_usb=True boots the two controllers EMPTY -- no usb-host at boot (dissolves
    # the UEFI XhciDxe ASSERT: nothing to enumerate). Both controllers present for the post-boot attach.
    spec = vm.VmSpec(role=vm.ROLE_BOTH, hotplug=True)
    argv = " ".join(vm.build_qemu_argv(spec, "d", "s", "u", hotplug_usb=True))
    assert "qemu-xhci,id=xhci" in argv and "usb-ehci,id=ehci" in argv    # both controllers, empty
    assert "usb-host" not in argv                                        # ZERO usb-host at boot


def test_golden_and_both_argv_unchanged_when_not_hotplug():
    # golden/both keep their at-boot usb-host passthrough (hotplug_usb defaults False)
    both = " ".join(vm.build_qemu_argv(vm.VmSpec(role=vm.ROLE_BOTH), "d", "s", "u"))
    assert "usb-host,bus=ehci.0,vendorid=0x3923,productid=0x709b,id=ni_hs" in both
    assert "usb-host,bus=xhci.0,vendorid=0x3923,id=ni_b" in both
    ana = " ".join(vm.build_qemu_argv(vm.VmSpec(role=vm.ROLE_ANALYZER), "d", "s", "u"))
    assert "usb-host,bus=xhci.0" in ana


def test_singleton_spec_is_role_both_hotplug_and_pins_both():
    devs = [vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True),
            vm.UsbDevice(0x3923, 0x709b, "HS", "NI", 0x00130000, "", "ni-gpib-hs", "HS", "r", True)]
    s = vm.singleton_spec(devices=devs)
    assert s.role == vm.ROLE_BOTH and s.hotplug is True and s.name == "se299-singleton"
    assert "hostport=1.4" in s.analyzer_match() and "hostport=1.3" in s.source_match()


def test_attach_adapter_hs_device_add_on_ehci_pinned():
    spec = vm.VmSpec(role=vm.ROLE_BOTH, source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")
    cmds = []
    ok = vm.attach_adapter(spec, "hs", detect=lambda: [], sleep=lambda s: None,
                           execute=lambda sock, c, **k: cmds.append(c[0]), log=lambda *a: None)
    assert ok is True
    assert cmds[0]["execute"] == "device_del" and cmds[0]["arguments"]["id"] == "ni_hs"  # del-prefix
    add = cmds[1]
    assert add["execute"] == "device_add" and add["arguments"]["bus"] == "ehci.0"
    assert add["arguments"]["id"] == "ni_hs" and add["arguments"]["hostport"] == "1.3"
    assert len([c for c in cmds if c["execute"] == "device_add"]) == 1                   # HS: no fxload re-add


def test_attach_adapter_b_waits_fxload_then_readds_on_xhci():
    spec = vm.VmSpec(role=vm.ROLE_BOTH, analyzer_usb_match="vendorid=0x3923,hostbus=0,hostport=1.4")
    b_cold = vm.UsbDevice(0x3923, 0x702b, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)
    b_hot = vm.UsbDevice(0x3923, 0x702a, "B", "NI", 0x00140000, "", "ni-gpib-b", "B", "r", True)
    reads = {"n": 0}

    def det():                                    # cold on the first read, fxloaded (0x702a) after
        reads["n"] += 1
        return [b_cold] if reads["n"] == 1 else [b_hot]

    cmds = []
    ok = vm.attach_adapter(spec, "b", detect=det, sleep=lambda s: None,
                           execute=lambda sock, c, **k: cmds.append(c[0]), log=lambda *a: None,
                           fxload_timeout=30.0, poll_interval=3.0)
    assert ok is True
    adds = [c for c in cmds if c["execute"] == "device_add"]
    assert len(adds) == 2                          # initial cold add + re-add after fxload (0x702a)
    assert all(a["arguments"]["bus"] == "xhci.0" and a["arguments"]["id"] == "ni_b"
               and a["arguments"]["hostport"] == "1.4" for a in adds)
    # ordering: device_del, device_add (cold), device_del, device_add (re-follow)
    assert [c["execute"] for c in cmds] == ["device_del", "device_add", "device_del", "device_add"]


def test_attach_adapter_never_raises_on_qmp_failure():
    spec = vm.VmSpec(role=vm.ROLE_BOTH, source_usb_match="vendorid=0x3923,hostbus=0,hostport=1.3")

    def boom(sock, c, **k):
        if c[0]["execute"] == "device_add":
            raise IOError("qmp closed")
    assert vm.attach_adapter(spec, "hs", detect=lambda: [], sleep=lambda s: None,
                             execute=boom, log=lambda *a: None) is False


def test_ensure_singleton_attaches_hs_and_verifies_before_b():
    # HEADLINE: HS attached AND verified BEFORE the B device_add is issued (serial, non-overlapping
    # host resets). Observed via ordered injected calls.
    spec = vm.singleton_spec()
    order = []
    state = {"hs": False, "b": False}

    def fake_attach(s, which, **kw):
        order.append(("attach", which))
        state["hs" if which in ("hs", "source") else "b"] = True   # answers right after its attach
        return True

    a, s = vm.ensure_singleton(
        spec, both_reachable_fn=lambda: False, live=lambda: False, provisioned=lambda: True,
        launch_one=lambda *a, **k: order.append(("boot",)), attach=fake_attach,
        hs_reachable_fn=lambda: state["hs"], b_reachable_fn=lambda: state["b"],
        detect=lambda: [], sleep=lambda s: None, log=lambda *a: None)
    assert order == [("boot",), ("attach", "hs"), ("attach", "b")]    # HS fully before B
    assert a == spec.analyzer_net_addr and s == spec.source_net_addr


def test_ensure_singleton_reuses_running_bridge_without_boot():
    spec = vm.singleton_spec()
    booted = {"n": 0}
    a, s = vm.ensure_singleton(
        spec, both_reachable_fn=lambda: True,                        # both already answer
        launch_one=lambda *a, **k: booted.__setitem__("n", booted["n"] + 1),
        attach=lambda *a, **k: booted.__setitem__("n", 99), log=lambda *a: None)
    assert booted["n"] == 0                                          # NO boot, NO attach
    assert a == spec.analyzer_net_addr and s == spec.source_net_addr


def test_ensure_singleton_join_attaches_only_the_missing_adapter():
    # a qemu is already live but only the HS answers -> JOIN (no reboot), attach only the B
    spec = vm.singleton_spec()
    order = []
    a, s = vm.ensure_singleton(
        spec, both_reachable_fn=lambda: False, live=lambda: True, provisioned=lambda: True,
        launch_one=lambda *a, **k: order.append("boot"),            # must NOT be called (no reboot)
        attach=lambda ss, which, **kw: order.append(which) or True,
        hs_reachable_fn=lambda: True,                               # HS already up
        b_reachable_fn=lambda: bool([o for o in order if o == "b"]),  # B up after it is attached
        detect=lambda: [], sleep=lambda s: None, log=lambda *a: None)
    assert "boot" not in order                                      # JOINED, never rebooted
    assert order == ["b"]                                           # only the MISSING adapter attached
    assert a == spec.analyzer_net_addr and s == spec.source_net_addr


def test_ensure_singleton_self_heals_unprovisioned_join_with_one_relaunch():
    spec = vm.singleton_spec()
    events = []
    prov = {"n": 0}

    def provisioned():
        prov["n"] += 1
        return prov["n"] >= 2                       # unprovisioned on the JOIN, provisioned after relaunch

    vm.ensure_singleton(
        spec, both_reachable_fn=lambda: False, live=lambda: True, provisioned=provisioned,
        stop=lambda ss, **kw: events.append("stop") or "stopped",
        relaunch=lambda ss, **kw: events.append("relaunch"),
        attach=lambda ss, which, **kw: events.append(("attach", which)) or True,
        hs_reachable_fn=lambda: True, b_reachable_fn=lambda: True,   # both up after heal -> no attach
        detect=lambda: [], sleep=lambda s: None, log=lambda *a: None)
    assert events[:2] == ["stop", "relaunch"]        # self-heal: stop once, relaunch once


def test_ensure_singleton_degrades_when_one_adapter_wedged():
    spec = vm.singleton_spec()

    def fake_av(ss, which, **kw):
        if which == "b":
            raise vm.BridgeUnavailable("ADAPTER_WEDGED: the 8565EC ... re-plug")
        return None

    logs = []
    a, s = vm.ensure_singleton(
        spec, both_reachable_fn=lambda: False, live=lambda: False, provisioned=lambda: True,
        launch_one=lambda *a, **k: None, attach_and_verify=fake_av,
        hs_reachable_fn=lambda: False, b_reachable_fn=lambda: False,
        detect=lambda: [], sleep=lambda s: None,
        log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    assert a == spec.analyzer_net_addr and s == spec.source_net_addr   # degraded, still returns both
    assert any("DEGRADED" in m and "analyzer" in m for m in logs)


def test_ensure_singleton_raises_when_never_provisioned_on_cold_boot():
    spec = vm.singleton_spec()
    with pytest.raises(vm.BridgeUnavailable) as e:
        vm.ensure_singleton(spec, both_reachable_fn=lambda: False, live=lambda: False,
                            provisioned=lambda: False, launch_one=lambda *a, **k: None,
                            detect=lambda: [], log=lambda *a: None)
    assert "never signalled provisioned" in str(e.value)


def test_await_guest_provisioned_true_when_marker_appears_and_false_on_bound():
    n = {"i": 0}

    def probe():
        n["i"] += 1
        return n["i"] >= 3

    assert vm.await_guest_provisioned(vm.VmSpec(name="s"), probe=probe, timeout=100.0,
                                      poll_interval=5.0, sleep=lambda s: None, log=lambda *a: None) is True
    assert vm.await_guest_provisioned(vm.VmSpec(name="s"), probe=lambda: False, timeout=6.0,
                                      poll_interval=3.0, sleep=lambda s: None, log=lambda *a: None) is False


# =========================================== CLI wiring (STAGE 2 reachability flags)

def test_cli_ensure_vm_addresses_refuses_routable_bind_without_token():
    # the --vm-bind/--vm-token wiring GUARDS a routable exposure UP FRONT (before any ioreg scan
    # or boot), so a client-reachable bind can never be published UNAUTHENTICATED. Loopback default
    # unchanged (this test exercises only the fail-fast refusal, which short-circuits before detect).
    import argparse
    import cli
    ns = argparse.Namespace(vm_mode="singleton", vm_port=5555, vm_source_port=5556,
                            vm_reset=False, vm_timeout=240.0, vm_stagger=25.0,
                            vm_bind="0.0.0.0", vm_token="")
    with pytest.raises(vm.BridgeUnavailable) as e:
        cli._ensure_vm_addresses(ns)
    assert "without a token" in str(e.value)
