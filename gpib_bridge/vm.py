"""Bring up the GPIB bridge in a QEMU Linux VM on Apple Silicon with USB PASSTHROUGH of
the NI GPIB-USB-HS, so ni_usb_gpib (linux-gpib) in the guest drives the 8565EC and the
Mac sweeper reaches it over the net: bridge. This replaces the Lima path, which cannot
pass USB through on Apple Silicon.

Why qemu works here where Lima cannot: qemu's `usb-host` device is libusb underneath, and
macOS has NO driver claiming the NI adapter (verified: libusb can claim it + do control
transfers without root), so qemu can capture it cleanly. Passing by `vendorid=0x3923`
alone (no productid) matches the adapter both before and after its 0x702b -> 0x702a
firmware re-enumeration, so the ni_usb_gpib firmware load in the guest does not drop it.

Data path (device -> sweeper):
  8565EC --GPIB--> NI HS --USB(qemu usb-host, vendorid 0x3923)--> guest ni_usb_gpib
  (linux-gpib) --> ni_gpib_server (guest 127.0.0.1:5555) --qemu hostfwd--> Mac
  127.0.0.1:5555 --> drivers.NetworkTransport --> Agilent856xEC --> LiveSpectrumGUI.

Pure parts (device detection, the qemu argv, the cloud-init user-data) are hardware-free
testable; the actual boot + firmware upload + USB grab are the real-environment steps.
"""
from __future__ import annotations

import glob
import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace

HERE = os.path.dirname(os.path.abspath(__file__))          # gpib_bridge/
SE299 = os.path.dirname(HERE)                              # rf-se/se299/
UBUNTU_ARM64_CLOUDIMG = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/"
    "ubuntu-24.04-server-cloudimg-arm64.img")
NI_VENDOR_ID = 0x3923


class BridgeUnavailable(RuntimeError):
    """The qemu VM bridge could not be brought up (qemu missing, no NI adapter, boot/USB
    failure). We NEVER fall back to a fake -- the caller reports honestly."""


# =============================================================== host USB detection

@dataclass(frozen=True)
class UsbDevice:
    vendor_id: int
    product_id: int
    name: str
    vendor_name: str
    location_id: int
    serial: str
    kind: str                 # ni-gpib | ni-gpib-plus | prologix | keysight-82357 | unknown
    model: str
    note: str
    valid: bool               # a valid target for the qemu + ni_usb_gpib passthrough path

    @property
    def vidpid(self) -> str:
        return f"0x{self.vendor_id:04x}:0x{self.product_id:04x}"


def _classify(vid: int, pid: int, name: str):
    """(kind, model, note, valid) for a USB (vendor, product). valid = drivable by the
    qemu + linux-gpib ni_usb_gpib path in the guest (i.e. an NI GPIB adapter)."""
    if vid == 0x3923:                                      # National Instruments
        if pid in (0x702a, 0x702b):
            # GPIB-USB-B: FULL-speed, needs an fxload firmware upload (0x702b cold -> 0x702a).
            state = "ready" if pid == 0x702a else "pre-firmware (fxload uploads it -> 0x702a)"
            return ("ni-gpib-b", "NI GPIB-USB-B", state, True)
        if pid == 0x709b:
            # GPIB-USB-HS: HIGH-speed, ONBOARD firmware (no upload). Recent firmware (bcdDevice
            # 1.01) trips a linux-gpib IBGTS quirk -> the provisioner patches ni_usb_go_to_standby.
            return ("ni-gpib-hs", "NI GPIB-USB-HS", "ready (onboard firmware)", True)
        if pid in (0x7618, 0x761e):
            state = "ready" if pid == 0x7618 else "pre-firmware (HS+ two-stage load)"
            return ("ni-gpib-plus", "NI GPIB-USB-HS+", state, True)
        return ("ni-gpib", "NI GPIB adapter", f"unrecognized NI PID 0x{pid:04x}", True)
    if vid == 0x0957:                                     # Agilent/Keysight
        return ("keysight-82357", "Keysight/Agilent 82357B",
                "not the ni_usb_gpib path; use its own driver", False)
    if vid == 0x0403:                                     # FTDI (Prologix-class)
        return ("prologix", "FTDI serial (Prologix-class)",
                "serial USB-GPIB -- works NATIVELY on macOS, no VM/passthrough needed", False)
    return ("unknown", name or "USB device", "", False)


def _ioreg_usb_plist() -> bytes:  # pragma: no cover - shells out to ioreg
    return subprocess.run(["ioreg", "-a", "-l", "-p", "IOUSB"],
                          capture_output=True).stdout


def detect_gpib_usb(raw: bytes = None) -> list:
    """Detect attached USB-GPIB adapters on the Mac by parsing `ioreg -a -l -p IOUSB` (the
    full USB plane, so devices nested behind a hub are found). raw = plist bytes (tests).
    Returns only recognized GPIB adapters (NI / Prologix / Keysight); hubs/LAN are skipped."""
    if raw is None:
        raw = _ioreg_usb_plist()
    try:
        tree = plistlib.loads(raw)
    except Exception:
        return []
    out = []

    def walk(node):
        if isinstance(node, dict):
            vid = node.get("idVendor")
            if vid is not None:
                pid = node.get("idProduct") or 0
                kind, model, note, valid = _classify(vid, pid, node.get("USB Product Name"))
                if kind != "unknown":
                    out.append(UsbDevice(vid, pid, node.get("USB Product Name") or "",
                                         node.get("USB Vendor Name") or "",
                                         node.get("locationID") or 0,
                                         node.get("USB Serial Number") or "",
                                         kind, model, note, valid))
            for ch in node.get("IORegistryEntryChildren", []) or []:
                walk(ch)
        elif isinstance(node, list):
            for n in node:
                walk(n)

    walk(tree)
    return out


_NI_KINDS = ("ni-gpib", "ni-gpib-b", "ni-gpib-hs", "ni-gpib-plus")


def pick_ni_device(devices: list):
    """The first NI GPIB adapter (a qemu-passthrough target), or None."""
    for d in devices:
        if d.kind in _NI_KINDS:
            return d
    return None


def pick_ni_devices(devices: list) -> list:
    """ALL NI GPIB adapters -- the two-adapter setup passes BOTH through one VM (the
    GPIB-USB-B carrying the 8565EC and the GPIB-USB-HS carrying the 68369A)."""
    return [d for d in devices if d.kind in _NI_KINDS]


def usb_summary(devices: list) -> str:
    if not devices:
        return ("USB-GPIB adapters detected: NONE. Attach the NI GPIB-USB-HS (or use a "
                "Prologix natively / a Pi). The VM has nothing to pass through.")
    lines = ["USB-GPIB adapters detected:"]
    for d in devices:
        tag = "VALID passthrough target" if d.valid else "NOT the VM path"
        lines.append(f"  {d.vidpid}  {d.model}  [{tag}] -- {d.note}")
    return "\n".join(lines)


# =============================================================== qemu VM spec + argv

ROLE_ANALYZER = "analyzer"       # a VM that passes ONLY the 8565EC's GPIB-USB-B (RX)
ROLE_SOURCE = "source"           # a VM that passes ONLY the 68369A's GPIB-USB-HS (TX)
ROLE_BOTH = "both"               # ONE VM that passes BOTH adapters (RX+TX on two boards)
ROLES = (ROLE_ANALYZER, ROLE_SOURCE, ROLE_BOTH)


def _is_loopback(host: str) -> bool:
    """A loopback / non-routable bind host for the qemu HOSTFWD. Intentionally treats "" as loopback
    (UNLIKE ni_gpib_server._is_loopback, which must NOT, because socket.bind(("",p)) is INADDR_ANY):
    here "" is SAFE only because _hostfwd() coerces bind_host `or "127.0.0.1"` before building the
    hostfwd string, so an empty bind_host forwards on loopback, not all-interfaces. GUARD: if that
    coercion is ever removed, drop "" from this set too, or an empty bind_host silently exposes the LAN."""
    return host in ("127.0.0.1", "::1", "localhost", "")


def host_lan_ip(fallback: str = "127.0.0.1", *, sock_factory=None) -> str:
    """Best-effort routable LAN IP of THIS host -- the address a remote client dials to reach the
    forwarded bridge ports. Learns the outbound-interface address via a UDP socket to a public IP
    WITHOUT sending a packet (connect on SOCK_DGRAM just selects the route); returns `fallback`
    (loopback) if it cannot resolve. sock_factory is injectable for tests. Never raises."""
    s = (sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))()
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return fallback
    finally:
        s.close()


@dataclass(frozen=True)
class VmSpec:
    """A single qemu VM instance (1 instance == 1 qemu). `role` selects what it passes through
    and serves:
      * analyzer -> the GPIB-USB-B (8565EC), one board, bridge on `port` (pad gpib_addr)
      * source   -> the GPIB-USB-HS (68369A), one board, bridge on `port` (pad source_addr)
      * both     -> both adapters, two boards, analyzer on `port` + source on `source_port`
    Every mutable-per-instance resource (workdir, disk overlay, qmp socket, MAC, forwarded
    ports) derives from `name`/explicit fields so MULTIPLE instances run concurrently on one
    Mac without collision -- the golden deployment is two instances (see golden_pair())."""
    name: str = "se299-gpib"
    role: str = ROLE_BOTH
    port: int = 5555                    # this instance's primary bridge port (analyzer, or the
                                        # source's own port in a source-only instance)
    source_port: int = 5556             # role=both ONLY: the second (source) bridge port
    gpib_addr: int = 18                 # 8565EC analyzer (RX) primary address
    source_addr: int = 5                # 68369A source (TX) primary address
    ssh_port: int = 2222                # host port forwarded to the guest's :22 (diagnostics)
    ssh_pubkey: str = ""                # if set, injected as an authorized key (keyless SSH in)
    mac: str = ""                       # per-instance NIC MAC (blank -> derived from name)
    cpus: int = 2
    memory_mb: int = 2048
    image_location: str = UBUNTU_ARM64_CLOUDIMG
    usb_vendor_id: int = NI_VENDOR_ID   # NI vendor id; the GPIB-USB-B is matched by vendor ONLY
                                        # so it survives its 0x702b->0x702a firmware re-enumeration
    hs_product_id: int = 0x709b         # GPIB-USB-HS product id (unique -> its own match/board)
    analyzer_usb_match: str = ""        # override the analyzer adapter's usb-host match (blank ->
                                        # vendor-only, follows the B's re-enumeration)
    source_usb_match: str = ""          # override the source adapter's usb-host match (blank ->
                                        # vendor+productid=0x709b, unique to the HS)
    bridge_dir: str = HERE
    guest_mount: str = "/opt/gpib_bridge"
    hotplug: bool = False               # SINGLETON: boot the two USB controllers EMPTY (no usb-host
                                        # at boot -> no UEFI XhciDxe ASSERT) and attach both adapters
                                        # SERIALLY post-boot via QMP (attach_adapter). role=both.
    bind_host: str = "127.0.0.1"        # interface qemu's hostfwd binds the BRIDGE PORTS on. Loopback
                                        # (default) = single-machine (today's behavior, unchanged); a
                                        # routable value (e.g. 0.0.0.0) exposes them so a client on
                                        # ANOTHER host can reach the analyzer/source. ssh (:2222) stays
                                        # loopback regardless. A routable bind REQUIRES bridge_token.
    bridge_token: str = ""              # auth token GATING a routable bind (mirrors ni_gpib_server.main:
                                        # refuse to publish an UNAUTHENTICATED instrument-control
                                        # service to the LAN). Empty is fine for the loopback default.

    # ---- USB matches (overridable so launch code can pin by hostbus/hostport if needed) ----
    def analyzer_match(self) -> str:
        return self.analyzer_usb_match or f"vendorid=0x{self.usb_vendor_id:04x}"

    def source_match(self) -> str:
        return (self.source_usb_match
                or f"vendorid=0x{self.usb_vendor_id:04x},productid=0x{self.hs_product_id:04x}")

    # ---- net: addresses (role-aware) --------------------------------------------------------
    def advertised_host(self) -> str:
        """The host a CLIENT dials to reach this instance's forwarded bridge ports. Loopback for the
        default single-machine bind; the host's routable LAN IP (resolved; loopback fallback) when the
        bridge ports are bound on a routable interface, so coordinator/checkpath/sa/sg (possibly on
        ANOTHER host) get a reachable address instead of the unreachable 127.0.0.1. A concrete routable
        bind IP is advertised as-is; a wildcard (0.0.0.0/::) resolves to the real LAN IP."""
        if _is_loopback(self.bind_host):
            return "127.0.0.1"
        if self.bind_host in ("0.0.0.0", "::"):
            return host_lan_ip()
        return self.bind_host

    @property
    def net_addr(self) -> str:
        """This instance's PRIMARY bridge address: the analyzer for analyzer/both roles, the
        source for a source-only role."""
        h = self.advertised_host()
        if self.role == ROLE_SOURCE:
            return f"net:{h}:{self.port}:{self.source_addr}"
        return f"net:{h}:{self.port}:{self.gpib_addr}"

    @property
    def analyzer_net_addr(self) -> str:
        return f"net:{self.advertised_host()}:{self.port}:{self.gpib_addr}"

    @property
    def source_net_addr(self) -> str:
        # role=both keeps the source on its own second port; a source-only instance serves it
        # on that instance's primary port.
        p = self.source_port if self.role == ROLE_BOTH else self.port
        return f"net:{self.advertised_host()}:{p}:{self.source_addr}"

    def nic_mac(self) -> str:
        """A stable per-instance MAC (blank field -> derived from the name) so two concurrent
        VMs on the same host do not share the qemu default 52:54:00:12:34:56."""
        if self.mac:
            return self.mac
        h = 0
        for ch in self.name:
            h = (h * 131 + ord(ch)) & 0xFFFF
        return f"52:54:00:5e:{(h >> 8) & 0xFF:02x}:{h & 0xFF:02x}"

    def workdir(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".se299-vm", self.name)

    def qmp_sock(self) -> str:
        return os.path.join(self.workdir(), "qmp.sock")


def decode_location_id(loc: int) -> tuple:
    """(hostbus, hostport) from an Apple ioreg locationID: hostbus = the high byte; hostport =
    the successive nibbles (USB-tree tiers) below it, up to the first zero, joined by '.'. This
    is the DEFAULT decode -- on macOS the value libusb (hence qemu) actually reports can differ
    (a documented getPortNumbers regression), so a live run must confirm it. Pure/testable."""
    bus = (loc >> 24) & 0xFF
    nibbles = []
    for shift in (20, 16, 12, 8, 4, 0):
        n = (loc >> shift) & 0xF
        if n == 0:
            break
        nibbles.append(str(n))
    return (bus, ".".join(nibbles))


def hostport_match(dev: "UsbDevice") -> str:
    """A qemu usb-host match string pinned to this adapter's PHYSICAL PORT (survives the B's
    0x702b->0x702a re-enumeration; provably disjoint between concurrent qemus), with vendorid as
    a redundant assertion. Returns '' if the port cannot be decoded (caller then keeps its
    default match)."""
    bus, port = decode_location_id(dev.location_id)
    if not port:
        return ""
    return f"vendorid=0x{dev.vendor_id:04x},hostbus={bus},hostport={port}"


def golden_pair(base_port: int = 5555, base_ssh_port: int = 2222,
                ssh_pubkey: str = "", devices: list = None) -> tuple:
    """The GOLDEN deployment: TWO instances (two qemus), one per instrument, on the local
    loopback -- (analyzer_spec, source_spec). Disjoint name/workdir/qmp/mac/ports so both run
    at once. The coordinator drives them over the net: as RX + TX.

    If `devices` (detect_gpib_usb output) is given, BOTH VMs are PINNED to their adapter's physical
    port (hostbus/hostport): the analyzer to the GPIB-USB-B so it cannot race the source VM for the
    shared 0x3923 vendor id, and the source to the GPIB-USB-HS so a wedged-FX2 QMP re-attach knows
    its port (reattach_source_hs). Without `devices`, the analyzer falls back to a vendor-only match
    (needs source-VM-first launch -- see ensure_golden_pair) and the source keeps its unique
    productid=0x709b match."""
    ana = VmSpec(name="se299-rx", role=ROLE_ANALYZER, port=base_port,
                 ssh_port=base_ssh_port, ssh_pubkey=ssh_pubkey)
    src = VmSpec(name="se299-tx", role=ROLE_SOURCE, port=base_port + 1,
                 ssh_port=base_ssh_port + 1, ssh_pubkey=ssh_pubkey)
    if devices:
        b = next((d for d in devices if d.kind == "ni-gpib-b"), None)
        if b is not None:
            m = hostport_match(b)
            if m:
                ana = replace(ana, analyzer_usb_match=m)
        hs = next((d for d in devices if d.kind == "ni-gpib-hs"), None)
        if hs is not None:
            m = hostport_match(hs)
            if m:
                src = replace(src, source_usb_match=m)
    return (ana, src)


def default_ssh_pubkey() -> str:
    """The Mac's SSH public key (for keyless guest login / diagnostics), or '' if none."""
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        p = os.path.expanduser(os.path.join("~", ".ssh", name))
        if os.path.exists(p):
            with open(p) as fh:
                return fh.read().strip()
    return ""


def qemu_available(qemu: str = "qemu-system-aarch64") -> tuple:
    """(ok, message). qemu is the vehicle for USB passthrough; Lima/vz cannot do it."""
    if shutil.which(qemu) is None:
        return (False, "qemu not installed -- install it with:  brew install qemu   (it is "
                       "the only backend that can pass the NI adapter through on Apple Silicon).")
    return (True, "ok")


def _usb_device_argv(spec: VmSpec) -> list:
    """The USB-controller + usb-host passthrough args for this instance's role.

    Speed/controller mapping is load-bearing:
      * The GPIB-USB-HS (0x709b, 68369A) is HIGH-speed -> EHCI. UEFI's XhciDxe ASSERTs on its
        interrupt-endpoint descriptor during boot (EhciDxe does not); EHCI carries it fine.
      * The GPIB-USB-B (0x702a/0x702b, 8565EC) is FULL-speed -> XHCI. A bare EHCI controller is
        high-speed-only and SILENTLY DROPS a full-speed device.
    Matching: the HS is matched uniquely by product id; the B is matched by VENDOR ONLY so the
    match survives its 0x702b(cold)->0x702a(post-fxload) re-enumeration (a productid match would
    drop the passthrough the instant the guest loads firmware). In role=both the HS device is
    listed FIRST so it claims 0x709b and the vendor-only matcher grabs the remaining B."""
    if spec.role == ROLE_ANALYZER:
        return ["-device", "qemu-xhci,id=xhci",
                "-device", f"usb-host,bus=xhci.0,{spec.analyzer_match()},id=ni_b"]
    if spec.role == ROLE_SOURCE:
        return ["-device", "usb-ehci,id=ehci",
                "-device", f"usb-host,bus=ehci.0,{spec.source_match()},id=ni_hs"]
    # ROLE_BOTH: two controllers, both adapters (HS listed first).
    return ["-device", "qemu-xhci,id=xhci",
            "-device", "usb-ehci,id=ehci",
            "-device", f"usb-host,bus=ehci.0,{spec.source_match()},id=ni_hs",
            "-device", f"usb-host,bus=xhci.0,{spec.analyzer_match()},id=ni_b"]


def _usb_controller_argv(spec: VmSpec) -> list:
    """The two EMPTY USB controllers for the SINGLETON hot-plug boot: qemu-xhci (full-speed B) +
    usb-ehci (high-speed HS), with NO usb-host at boot. Booting the controllers with NOTHING attached
    dissolves the UEFI XhciDxe ASSERT (there is no interrupt-endpoint descriptor to choke on during
    boot); both adapters are then hot-plugged SERIALLY post-boot -- HS onto ehci.0, B onto xhci.0
    (attach_adapter) -- so the two host-side resets never overlap."""
    return ["-device", "qemu-xhci,id=xhci", "-device", "usb-ehci,id=ehci"]


def guard_bind_auth(spec: VmSpec) -> None:
    """Refuse to expose the bridge PORTS on a non-loopback interface without a token -- mirrors
    ni_gpib_server.main's rule (an unauthenticated instrument-control service on the LAN is a
    hazard). No-op for the default loopback bind; raises BridgeUnavailable on a routable bind with
    no bridge_token."""
    if not _is_loopback(spec.bind_host) and not spec.bridge_token:
        raise BridgeUnavailable(
            f"refusing to expose the bridge ports on non-loopback {spec.bind_host!r} without a "
            f"token: that publishes an UNAUTHENTICATED instrument-control service to the LAN. Set "
            f"--vm-token / NI_GPIB_TOKEN to acknowledge + gate the exposure, or keep the default "
            f"127.0.0.1 (single-machine, loopback) bind.")


def _hostfwd(spec: VmSpec) -> str:
    """The user-net hostfwd string. The bridge port(s) bind spec.bind_host -- loopback by default
    (single-machine, unchanged); a routable bind (e.g. 0.0.0.0) exposes them so a client on ANOTHER
    host can reach the analyzer/source. ssh (:2222) STAYS on loopback (diagnostics only, never
    LAN-exposed)."""
    bh = spec.bind_host or "127.0.0.1"
    fwd = [f"hostfwd=tcp:{bh}:{spec.port}-:{spec.port}"]
    if spec.role == ROLE_BOTH:
        fwd.append(f"hostfwd=tcp:{bh}:{spec.source_port}-:{spec.source_port}")
    fwd.append(f"hostfwd=tcp:127.0.0.1:{spec.ssh_port}-:22")
    return "user,id=n0," + ",".join(fwd)


def build_qemu_argv(spec: VmSpec, image_path: str, seed_path: str, uefi_path: str,
                    qemu: str = "qemu-system-aarch64", hotplug_usb: bool = False) -> list:
    """The qemu-system-aarch64 command for ONE instance: hvf-accelerated ARM64 virt, the Ubuntu
    cloud image + a cloud-init seed, the bridge folder over 9p virtfs, USB, hostfwd of this
    instance's port(s) + ssh, a per-instance MAC + QMP socket. Pure -- no side effects; tested by
    asserting the flags. All per-instance paths/ports/MAC derive from `spec` so multiple instances
    coexist.

    hotplug_usb=True (the SINGLETON): boot the two USB controllers EMPTY (no usb-host at boot -> no
    UEFI XhciDxe ASSERT); both adapters are attached SERIALLY post-boot via QMP. hotplug_usb=False
    (golden/both): the current at-boot usb-host passthrough for the spec's role (unchanged).

    Precondition: a routable (non-loopback) bind_host REQUIRES a bridge_token (guard_bind_auth) so
    build_qemu_argv can never assemble an argv that publishes the bridge ports UNAUTHENTICATED."""
    guard_bind_auth(spec)
    usb = _usb_controller_argv(spec) if hotplug_usb else _usb_device_argv(spec)
    return [
        qemu,
        "-machine", "virt", "-accel", "hvf", "-cpu", "host",
        "-smp", str(spec.cpus), "-m", str(spec.memory_mb),
        "-drive", f"if=pflash,format=raw,readonly=on,file={uefi_path}",
        "-drive", f"if=virtio,format=qcow2,file={image_path}",
        "-drive", f"if=virtio,format=raw,file={seed_path}",         # cloud-init seed
        "-fsdev", f"local,id=fsbridge,path={spec.bridge_dir},security_model=none",
        "-device", "virtio-9p-pci,fsdev=fsbridge,mount_tag=gpibbridge",
        *usb,
        "-netdev", _hostfwd(spec),
        "-device", f"virtio-net-pci,netdev=n0,mac={spec.nic_mac()}",
        # QMP monitor (unix socket) for post-boot USB hot-plug / device reset -- lets a running
        # VM add/reset hardware connections without a reboot.
        "-qmp", f"unix:{spec.qmp_sock()},server=on,wait=off",
        "-nographic",
    ]


def render_cloud_init(spec: VmSpec) -> tuple:
    """(meta_data, user_data) for the cloud-init seed. On first boot the guest mounts the
    9p bridge share and runs the mounted provision.sh (build linux-gpib + ni_usb_gpib, load
    the NI firmware, gpib.conf for BOTH the 8565EC analyzer and the 68369A source, launch
    ni_gpib_server). One adapter, one bridge, both instruments. It binds 0.0.0.0 --insecure
    (no token): qemu user-mode hostfwd delivers to the guest's eth0 IP, NOT guest loopback, so
    a loopback bind would be unreachable; the guest is NAT-isolated (no LAN presence), so only
    the host's 127.0.0.1 hostfwd can reach the port."""
    meta_data = f"instance-id: {spec.name}\nlocal-hostname: {spec.name}\n"
    ssh_block = ""
    if spec.ssh_pubkey:
        # keyless SSH for the ubuntu user (diagnostics via the forwarded :22). The guest is
        # NAT-isolated; only the host's 127.0.0.1:ssh_port hostfwd can reach it.
        ssh_block = f"ssh_pwauth: false\nssh_authorized_keys:\n  - {spec.ssh_pubkey}\n"
    ap = spec.port                                          # analyzer bridge port
    sp = spec.source_port if spec.role == ROLE_BOTH else spec.port   # source bridge port
    user_data = f"""#cloud-config
package_update: true
{ssh_block}mounts:
  - [ "gpibbridge", "{spec.guest_mount}", "9p", "trans=virtio,version=9p2000.L,ro", "0", "0" ]
runcmd:
  - [ mkdir, -p, "{spec.guest_mount}" ]
  - [ mount, -a ]
  - [ bash, -lc, "BIND_HOST=0.0.0.0 INSECURE=yes bash {spec.guest_mount}/provision.sh {spec.role} {spec.gpib_addr} {spec.source_addr} {ap} {sp}" ]
final_message: "se299 GPIB bridge provisioned (role={spec.role}); 8565EC pad {spec.gpib_addr}, 68369A pad {spec.source_addr}."
"""
    return (meta_data, user_data)


def uefi_firmware_path(qemu: str = "qemu-system-aarch64") -> str:
    """Best-effort path to qemu's edk2 aarch64 UEFI code (brew layout)."""
    exe = shutil.which(qemu)
    if exe:
        share = os.path.join(os.path.dirname(os.path.dirname(exe)), "share", "qemu")
        for cand in ("edk2-aarch64-code.fd", "edk2-arm-code.fd"):
            p = os.path.join(share, cand)
            if os.path.exists(p):
                return p
    return "edk2-aarch64-code.fd"      # let the user point QEMU at it if not found


# =============================================================== reachability

def _idn_at(port: int, pad: int, timeout_ms: int):
    """Query the identity at a pad over the forwarded bridge port. Returns the idn string,
    or "" on any failure. Never raises. Uses *IDN? (both instruments answer it -- the 8565EC
    on A.03+ firmware, the 68369A/NV as a 488.2 device)."""
    sys.path.insert(0, SE299)
    try:
        import drivers
        t = drivers.NetworkTransport("127.0.0.1", port, pad, timeout_ms=timeout_ms)
        try:
            return t.query("*IDN?") or ""
        finally:
            t.close()
    except Exception:
        return ""


def bridge_reachable(spec: VmSpec, timeout_ms: int = 2000) -> bool:
    """True iff a valid 8565-class analyzer answers at the forwarded port (a real detect
    handshake over the production NetworkTransport path). Never raises."""
    sys.path.insert(0, SE299)
    try:
        import drivers
        t = drivers.NetworkTransport("127.0.0.1", spec.port, spec.gpib_addr, timeout_ms=timeout_ms)
        try:
            idn = drivers.Agilent856xEC(t).idn()
        finally:
            t.close()
        return "856" in (idn or "")
    except Exception:
        return False


def source_reachable(spec: VmSpec, timeout_ms: int = 2000) -> bool:
    """True iff an Anritsu 683xx-family source answers at its pad over the bridge port serving it
    (the second port in a role=both VM, or the instance's own port in a source-only VM). Matches
    the whole 683xx synthesizer family ("683" -- 68367C, 68369A/B/NV, ...), not one model, exactly
    as drivers.Anritsu68369.idn() accepts. Never raises."""
    p = spec.source_port if spec.role == ROLE_BOTH else spec.port
    return "683" in _idn_at(p, spec.source_addr, timeout_ms)


def both_reachable(spec: VmSpec, timeout_ms: int = 2000) -> bool:
    """True iff BOTH instruments answer -- the analyzer (RX) AND the source (TX). For a role=both
    VM they are two boards behind one VM; this is the readiness gate for driving both."""
    return bridge_reachable(spec, timeout_ms) and source_reachable(spec, timeout_ms)


def instance_reachable(spec: VmSpec, timeout_ms: int = 2000) -> bool:
    """True iff THIS instance's instrument(s) answer, per its role: the analyzer for an
    analyzer VM, the source for a source VM, BOTH for a role=both VM."""
    if spec.role == ROLE_ANALYZER:
        return bridge_reachable(spec, timeout_ms)
    if spec.role == ROLE_SOURCE:
        return source_reachable(spec, timeout_ms)
    return both_reachable(spec, timeout_ms)


# =============================================================== asset prep + lifecycle

@dataclass(frozen=True)
class AssetPaths:
    image: str
    seed: str
    uefi: str


def download_argv(url: str, dest: str) -> list:
    return ["curl", "-L", "--fail", "-o", dest, url]


def resize_argv(image: str, size: str = "16G") -> list:
    return ["qemu-img", "resize", image, size]


def overlay_argv(base: str, overlay: str) -> list:
    # a fresh copy-on-write overlay backed by the pristine base image. Booting the overlay
    # runs cloud-init from a clean slate, so provisioning is REPEATABLE (recreate the overlay
    # to re-provision) without re-downloading the base.
    return ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base, overlay]


def seed_argv(seed_dir: str, out_iso: str) -> list:
    # a cloud-init NoCloud seed: an ISO labeled "cidata" holding meta-data + user-data.
    # hdiutil is always present on macOS (no mkisofs/xorriso needed).
    return ["hdiutil", "makehybrid", "-iso", "-joliet",
            "-default-volume-name", "cidata", "-o", out_iso, seed_dir]


def _run(argv):  # pragma: no cover - shells out
    subprocess.run(argv, check=True)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _base_cache_name(spec: VmSpec) -> str:
    """The shared-cache filename for this spec's cloud image, keyed by the image basename so a
    different image URL never reuses a stale cached base."""
    stem = os.path.basename(spec.image_location).rsplit(".", 1)[0] or "cloudimg"
    return f"{stem}.qcow2"


def _find_existing_base(vm_root: str) -> str:
    """Any already-downloaded per-instance disk-base.qcow2 under vm_root, to seed the shared cache
    from (a hardlink) instead of re-downloading. Returns '' if none exists."""
    for p in sorted(glob.glob(os.path.join(vm_root, "*", "disk-base.qcow2"))):
        if os.path.exists(p):
            return p
    return ""


def prepare_assets(spec: VmSpec, workdir: str = None, run=None,
                   reset: bool = False) -> AssetPaths:
    """Download + prep the VM assets (idempotent). Returns AssetPaths. run is the subprocess
    runner (injectable for tests); the cloud-init files are always written so a test can
    assert their content without shelling out.

    The pristine cloud image is downloaded + resized ONCE into a SHARED, machine-level cache
    (~/.se299-vm/_base/<image>.qcow2) and NEVER booted; every instance's disposable copy-on-write
    overlay (disk.qcow2) backs onto that one base, so a new instance/role never re-downloads the
    ~600 MB image. The shared cache is seeded by hardlink from any pre-existing per-instance
    disk-base.qcow2 (no re-download on machines provisioned before this cache existed); an existing
    per-instance base is still honored so overlays created before the change keep a valid backing
    chain. reset=True (or a missing overlay) recreates the overlay + seed WITHOUT a re-download."""
    run = run or _run
    wd = workdir or spec.workdir()
    os.makedirs(wd, exist_ok=True)
    vm_root = os.path.dirname(wd)                              # ~/.se299-vm (tmp parent in tests)
    legacy_base = os.path.join(wd, "disk-base.qcow2")         # pre-shared-cache per-instance base
    shared_base = os.path.join(vm_root, "_base", _base_cache_name(spec))
    if not os.path.exists(shared_base):
        os.makedirs(os.path.dirname(shared_base), exist_ok=True)
        donor = legacy_base if os.path.exists(legacy_base) else _find_existing_base(vm_root)
        if donor and os.path.exists(donor):                   # reuse an already-downloaded base
            try:
                os.link(donor, shared_base)                   # read-only base -> safe to share one inode
            except OSError:
                shutil.copyfile(donor, shared_base)           # cross-filesystem fallback
        else:
            run(download_argv(spec.image_location, shared_base))
            run(resize_argv(shared_base))
    # honor an existing per-instance base (keeps a pre-existing overlay's backing chain valid);
    # otherwise back the overlay onto the shared cache.
    base = legacy_base if os.path.exists(legacy_base) else shared_base
    image = os.path.join(wd, "disk.qcow2")
    if reset and os.path.exists(image):
        os.remove(image)
    if not os.path.exists(image):
        run(overlay_argv(base, image))
    seed = os.path.join(wd, "seed.iso")
    sdir = os.path.join(wd, "cidata")
    os.makedirs(sdir, exist_ok=True)
    meta, user = render_cloud_init(spec)
    _write(os.path.join(sdir, "meta-data"), meta)
    _write(os.path.join(sdir, "user-data"), user)
    # regenerate the seed on reset, or if it is missing/incomplete (hdiutil makehybrid
    # refuses to overwrite an existing file, so remove a stale/zero-byte seed first).
    if reset or not (os.path.exists(seed) and os.path.getsize(seed) > 0):
        if os.path.exists(seed):
            os.remove(seed)
        run(seed_argv(sdir, seed))
    return AssetPaths(image, seed, uefi_firmware_path())


class QemuVm:
    """A background qemu-system-aarch64 instance for the bridge. Persists across GUI
    sessions so later launches find the bridge already up."""

    def __init__(self, spec: VmSpec):
        self.spec = spec
        self._proc = None

    def launch(self, assets: AssetPaths) -> None:  # pragma: no cover - boots a real VM
        os.makedirs(self.spec.workdir(), exist_ok=True)
        # Remove a STALE qmp.sock left by a prior qemu that was killed uncleanly. A leftover
        # socket file makes QMP connections to the fresh qemu refuse (observed: the post-boot
        # device_del/device_add reattach could not run -> "connection refused"), so the running
        # VM cannot recover a re-enumerated adapter. Unlink before launch so QMP binds clean.
        try:
            os.unlink(self.spec.qmp_sock())
        except FileNotFoundError:
            pass
        log = open(os.path.join(self.spec.workdir(), "qemu.log"), "ab")
        self._proc = subprocess.Popen(
            build_qemu_argv(self.spec, assets.image, assets.seed, assets.uefi,
                            hotplug_usb=self.spec.hotplug),   # singleton boots controllers-only
            stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        # record the pid so a re-run inside the boot window ADOPTS this instance (instance_is_live)
        # instead of spawning a rival qemu, and so `vm-stop` can signal it.
        try:
            write_pid(pidfile_path(self.spec), self._proc.pid)
        except OSError:
            pass

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:  # pragma: no cover
        if self.is_running():
            self._proc.terminate()
        try:
            os.unlink(pidfile_path(self.spec))
        except FileNotFoundError:
            pass


def _default_launch(spec: VmSpec, assets: AssetPaths):  # pragma: no cover
    vm = QemuVm(spec)
    vm.launch(assets)
    return vm


def match_to_qmp_args(match: str) -> dict:
    """A qemu usb-host match string -> QMP device_add argument dict.
    'vendorid=0x3923,hostbus=0,hostport=1.4' -> {'vendorid':14627,'hostbus':0,'hostport':'1.4'}.
    vendorid/productid/hostbus/hostaddr coerce to int (hex ok); hostport stays a string (the USB
    tree path like '1.4'). Pure/testable."""
    out = {}
    for tok in match.split(","):
        if "=" not in tok:
            continue
        k, v = (x.strip() for x in tok.split("=", 1))
        if k in ("vendorid", "productid", "hostbus", "hostaddr"):
            out[k] = int(v, 0)
        elif k == "hostport":
            out[k] = v
    return out


def qmp_execute(sock_path: str, commands: list, timeout: float = 10.0) -> list:
    """Run a list of QMP command dicts over the qemu QMP unix socket (after the mandatory
    qmp_capabilities handshake) and return their reply dicts. Raises on socket/protocol error --
    the caller decides whether that is fatal. Used to hot-plug/reset passed-through USB devices on
    a RUNNING qemu instance."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    f = s.makefile("rwb", buffering=0)

    def _reply():
        while True:
            line = f.readline()
            if not line:
                raise IOError("qmp socket closed")
            o = json.loads(line.decode())
            if "return" in o or "error" in o:      # skip async events
                return o

    try:
        f.readline()                                # QMP greeting
        f.write((json.dumps({"execute": "qmp_capabilities"}) + "\n").encode()); _reply()
        out = []
        for c in commands:
            f.write((json.dumps(c) + "\n").encode())
            out.append(_reply())
        return out
    finally:
        s.close()


# =============================================================== duplicate-launch guard (pidfile)

def pidfile_path(spec: VmSpec) -> str:
    return os.path.join(spec.workdir(), "qemu.pid")


def _pid_alive(pid: int) -> bool:
    """True iff a process with this pid currently exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                     # exists but owned by another user
        return True
    except (ValueError, OverflowError, OSError):
        return False
    return True


def read_pid(path: str, *, alive=_pid_alive):
    """The LIVE pid recorded in a pidfile, or None (no file, unparsable, or the pid is dead)."""
    try:
        with open(path) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    return pid if alive(pid) else None


def write_pid(path: str, pid: int) -> None:
    with open(path, "w") as fh:
        fh.write(str(pid))


def port_bound(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """True if something already ACCEPTS a TCP connection on host:port (a prior qemu's hostfwd or
    a live bridge). One signal that a qemu instance is already up so a re-run need not launch a
    second one. Never raises."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def qmp_alive(sock_path: str, *, execute=qmp_execute) -> bool:
    """True if a qemu QMP monitor answers on this unix socket (a query-status handshake). Signals a
    live/booting instance so a re-run joins it instead of spawning a rival qemu. Never raises."""
    try:
        execute(sock_path, [{"execute": "query-status"}], timeout=2.0)
        return True
    except Exception:                           # noqa: BLE001 -- probe only
        return False


def instance_is_live(spec: VmSpec, *, pidfile_live=None, qmp_live=None, port_live=None) -> bool:
    """Decide whether a qemu instance for THIS spec is already up or mid-boot, so a re-run inside
    the (multi-minute) boot window does NOT spawn a duplicate qemu. True if ANY of: the pidfile
    names a live pid, the QMP monitor answers, or the primary bridge port is already bound. The
    three probes are injectable so the decision is unit-testable without a real qemu. Pure logic."""
    if pidfile_live is None:
        pidfile_live = lambda s: read_pid(pidfile_path(s)) is not None
    if qmp_live is None:
        qmp_live = lambda s: os.path.exists(s.qmp_sock()) and qmp_alive(s.qmp_sock())
    if port_live is None:
        port_live = lambda s: port_bound(s.port)
    return bool(pidfile_live(spec) or qmp_live(spec) or port_live(spec))


def stop_instance(spec: VmSpec, *, force: bool = False, reachable=None, execute=qmp_execute,
                  kill=os.kill, log=print) -> str:
    """Stop a running qemu instance for this spec and return a STATUS: 'stopped' | 'skipped-healthy'
    | 'not-running'. R5 (surgical): unless `force`, a REACHABLE (healthy) instance is SKIPPED -- a
    bare `vm-stop` must never tear down a working bridge (that is what killed the analyzer); an
    explicit --name sets force=True. QMP `quit` if the monitor answers, else SIGTERM the stored pid.
    ONLY unlinks the pidfile if it ACTUALLY stopped something (a skip leaves the live pid intact).
    NEVER raises -- best-effort teardown used by `vm-stop` and the --vm-reset guard."""
    reachable = reachable or (lambda: instance_reachable(spec))
    if not force and reachable():
        log(f"vm-stop: SKIPPING '{spec.name}' -- it is HEALTHY (its instrument answers); pass "
            f"--name {spec.name} to force-stop it")
        return "skipped-healthy"
    stopped = False
    sock = spec.qmp_sock()
    if os.path.exists(sock):
        try:
            execute(sock, [{"execute": "quit"}], timeout=2.0)
            stopped = True
            log(f"stopped qemu instance '{spec.name}' via QMP quit")
        except Exception as e:                  # noqa: BLE001
            log(f"QMP quit failed for '{spec.name}' ({e}); trying the stored pid")
    if not stopped:
        pid = read_pid(pidfile_path(spec))
        if pid is not None:
            try:
                kill(pid, signal.SIGTERM)
                stopped = True
                log(f"stopped qemu instance '{spec.name}' (SIGTERM pid {pid})")
            except Exception as e:              # noqa: BLE001
                log(f"could not signal pid {pid} for '{spec.name}': {e}")
    if not stopped:
        return "not-running"                    # do NOT unlink a pidfile we did not actually stop
    try:
        os.unlink(pidfile_path(spec))
    except FileNotFoundError:
        pass
    return "stopped"


def host_b_fxloaded(detect=None) -> bool:
    """True if the GPIB-USB-B is present on the HOST at 0x702a (its post-fxload product id)."""
    return any(d.kind == "ni-gpib-b" and d.product_id == 0x702a
               for d in (detect or detect_gpib_usb)())


def host_hs_present(detect=None) -> bool:
    """True if the GPIB-USB-HS (68369A source adapter, 0x709b) is present on the HOST USB bus."""
    return any(d.kind == "ni-gpib-hs" and d.product_id == 0x709b
               for d in (detect or detect_gpib_usb)())


def adapters_share_controller(devices: list) -> tuple:
    """R1a STRUCTURAL DETECTION: (shared, hostbus). True iff the GPIB-USB-B and the GPIB-USB-HS sit
    on the SAME host USB controller (same hostbus decoded from locationID). Same controller => the
    two 0x3923 devices can race a shared USB reset and BOTH wedge at once (the -110 double-wedge);
    DIFFERENT controllers physically cannot -- that is the true structural guarantee the stagger is
    a stand-in for. Returns (False, None) if either adapter is absent. Pure/testable."""
    b = next((d for d in devices if d.kind == "ni-gpib-b"), None)
    hs = next((d for d in devices if d.kind == "ni-gpib-hs"), None)
    if b is None or hs is None:
        return (False, None)
    bus_b, _ = decode_location_id(b.location_id)
    bus_hs, _ = decode_location_id(hs.location_id)
    return (bus_b == bus_hs, bus_b if bus_b == bus_hs else None)


def _shared_controller_warning(devices: list):
    """The operator warning string when both NI adapters share one host USB controller, else None."""
    shared, bus = adapters_share_controller(devices)
    if not shared:
        return None
    return (f"WARNING: both NI adapters are on the SAME host USB controller (hostbus {bus}) -- they "
            f"can race a shared USB reset and BOTH wedge at -110 on a cold boot. Move ONE adapter "
            f"to a USB port on a DIFFERENT controller (e.g. a powered hub on another root port); "
            f"different controllers cannot race a shared reset.")


def _target_kind(spec: VmSpec) -> str:
    """The host adapter KIND this instance's role depends on: the GPIB-USB-B for an analyzer VM,
    the GPIB-USB-HS for a source VM (a role=both VM depends on both -> returns 'both')."""
    if spec.role == ROLE_ANALYZER:
        return "ni-gpib-b"
    if spec.role == ROLE_SOURCE:
        return "ni-gpib-hs"
    return "both"


def _target_present(spec: VmSpec, detect=None) -> bool:
    """True iff THIS instance's target adapter is present on the host USB bus right now (the B for
    an analyzer VM, the HS for a source VM, either for a role=both VM). Used for the present-but-
    silent wedge verdict (R6) and the post-launch re-verify (R1b)."""
    devs = (detect or detect_gpib_usb)()
    b = any(d.kind == "ni-gpib-b" for d in devs)
    hs = any(d.kind == "ni-gpib-hs" for d in devs)
    if spec.role == ROLE_ANALYZER:
        return b
    if spec.role == ROLE_SOURCE:
        return hs
    return b or hs


def _adapter_identity(spec: VmSpec, detect=None):
    """A hashable identity of THIS instance's target adapter as the host sees it now -- (kind,
    product_id, hostport) -- or None if absent. Two consecutive reads with the SAME identity mean
    the host-side enumeration has SETTLED (the qemu usb-host claim/reset finished without dropping
    or re-enumerating it), which is what gates the second golden launch (R1b)."""
    kind = _target_kind(spec)
    for d in (detect or detect_gpib_usb)():
        if kind == "both" and d.kind in ("ni-gpib-b", "ni-gpib-hs"):
            pass
        elif d.kind != kind:
            continue
        _, hp = decode_location_id(d.location_id)
        return (d.kind, d.product_id, hp)
    return None


def reattach_analyzer_b(spec: VmSpec, *, log=print, detect=None, execute=qmp_execute,
                        sleep=time.sleep) -> bool:
    """Force the GUEST to re-attach the GPIB-USB-B after its in-guest fxload re-enumerated it
    0x702b->0x702a on the HOST. qemu's usb-host does NOT propagate that FX2 re-enumeration to the
    guest (verified live: host reaches 0x702a, guest stays stuck on the dead 0x702b for BOTH a
    vendor-only and a hostport-pinned match), so the 8565EC never comes up. Fix: on the RUNNING
    qemu, QMP device_del the B then device_add it pinned to its PHYSICAL port -- the guest then
    enumerates the live 0x702a device and ni_usb_gpib attaches it.

    Acts ONLY when the B is hostport-pinned (so we know its port) AND has reached 0x702a on the
    host (fxload done). Returns True iff it issued the re-attach. NEVER raises -- a QMP failure
    just logs and returns False so the bring-up keeps polling."""
    match = spec.analyzer_match()
    if "hostport" not in match:                     # not pinned -> we don't know the port; skip
        return False
    if not host_b_fxloaded(detect):                 # fxload not done yet -> nothing to re-attach
        return False
    args = match_to_qmp_args(match)
    args.update(driver="usb-host", bus="xhci.0", id="ni_b")
    try:
        execute(spec.qmp_sock(), [{"execute": "device_del", "arguments": {"id": "ni_b"}}])
        sleep(1.0)                                  # let the guest process the USB unplug first
        execute(spec.qmp_sock(), [{"execute": "device_add", "arguments": args}])
        log(f"re-attached the 8565EC adapter (GPIB-USB-B) at 0x702a via QMP ({match}) -- "
            f"qemu does not follow the FX2 re-enumeration on its own")
        return True
    except Exception as e:                          # noqa: BLE001 -- never fatal to the bring-up
        log(f"QMP re-attach of the GPIB-USB-B failed ({e}); will keep polling")
        return False


def reattach_source_hs(spec: VmSpec, *, log=print, detect=None, execute=qmp_execute,
                       sleep=time.sleep) -> bool:
    """Force the GUEST to re-attach the GPIB-USB-HS (68369A source) after its FX2 wedged
    (fxload-limbo / -110): a stuck guest-side USB device that a VM reboot does NOT clear. On the
    RUNNING qemu, QMP device_del the HS then device_add it pinned to its PHYSICAL port so the guest
    re-enumerates the live device and ni_usb_gpib re-attaches it. This mirrors reattach_analyzer_b
    for the SOURCE role -- without it a golden source VM had NO wedge recovery at all.

    Acts ONLY when the HS is hostport-pinned (so we know its port) AND is present on the host at
    0x709b. Returns True iff it issued the re-attach. NEVER raises -- a QMP failure just logs and
    returns False so the bring-up keeps polling."""
    match = spec.source_match()
    if "hostport" not in match:                     # not pinned -> we don't know the port; skip
        return False
    if not host_hs_present(detect):                 # HS not on the host bus -> nothing to re-attach
        return False
    args = match_to_qmp_args(match)
    args.update(driver="usb-host", bus="ehci.0", id="ni_hs")
    try:
        execute(spec.qmp_sock(), [{"execute": "device_del", "arguments": {"id": "ni_hs"}}])
        sleep(1.0)                                  # let the guest process the USB unplug first
        execute(spec.qmp_sock(), [{"execute": "device_add", "arguments": args}])
        log(f"re-attached the 68369A adapter (GPIB-USB-HS) via QMP ({match}) -- clears an FX2 "
            f"wedge the guest cannot recover on its own")
        return True
    except Exception as e:                          # noqa: BLE001 -- never fatal to the bring-up
        log(f"QMP re-attach of the GPIB-USB-HS failed ({e}); will keep polling")
        return False


def hostport_drift(spec: VmSpec, kind: str, *, detect=None):
    """If THIS instance's adapter (kind 'ni-gpib-b' analyzer | 'ni-gpib-hs' source) is now on a
    DIFFERENT host USB port than its launch-time pin, return (pinned_port, current_port); else None.
    A stale pin (the operator re-plugged into another port) makes the QMP re-attach target a dead
    port and poll forever -- surfacing the drift lets the caller tell the operator where to re-plug.
    Pure/testable given an injected detect."""
    match = spec.analyzer_match() if kind == "ni-gpib-b" else spec.source_match()
    pin = match_to_qmp_args(match).get("hostport")
    if not pin:
        return None
    dev = next((d for d in (detect or detect_gpib_usb)() if d.kind == kind), None)
    if dev is None:
        return None
    _, now = decode_location_id(dev.location_id)
    if now and now != pin:
        return (pin, now)
    return None


def _wedged_adapter_recovery(which: str) -> str:
    """The operator recovery for an adapter that is present on the USB bus but never answers -- a
    wedged FX2 (fxload-limbo / -110) that a VM reboot cannot clear."""
    return (f"{which} is on the USB bus but its FX2 is wedged (fxload-limbo / -110); a VM reboot "
            f"will NOT clear it -- physically unplug and re-plug it into the SAME USB port, then "
            f"re-run; if it recurs, power-cycle the instrument and re-run with --vm-reset.")


def _not_on_bus(which: str) -> str:
    return (f"{which} is not on the USB bus -- attach + power it (and confirm the instrument's "
            f"GPIB address), then re-run.")


def _who(spec: VmSpec) -> str:
    return {ROLE_ANALYZER: "the 8565EC", ROLE_SOURCE: "the 68369A",
            ROLE_BOTH: "the 8565EC + 68369A"}[spec.role]


def _boot_instance(spec: VmSpec, *, prepare=None, launch=None, already_booting=None,
                   reset: bool = False, log=print) -> None:
    """Prepare assets + boot ONE qemu (no readiness poll). The duplicate-launch guard makes a
    re-run inside the boot window JOIN the in-progress boot instead of spawning a rival qemu."""
    if (already_booting or instance_is_live)(spec):
        log(f"a qemu instance for '{spec.name}' is already up/booting -- joining its readiness "
            f"poll instead of launching a second qemu")
        return
    log("preparing qemu VM assets (first run downloads the Ubuntu ARM64 image; minutes)...")
    assets = prepare(spec) if prepare is not None else prepare_assets(spec, reset=reset)
    log(f"booting qemu instance '{spec.name}' (role={spec.role}); cloud-init builds patched "
        f"ni_usb_gpib + starts the bridge(s)...")
    (launch or _default_launch)(spec, assets)


def _warn_hostport_drift(spec: VmSpec, has_analyzer: bool, has_source: bool, detect, log) -> bool:
    """Log once if this instance's adapter drifted to a different USB port than its pin. Fix 7:
    a stale pin never re-attaches -- tell the operator which port to re-plug into."""
    checks = []
    if has_analyzer:
        checks.append(("ni-gpib-b", "the 8565EC adapter (GPIB-USB-B)"))
    if has_source:
        checks.append(("ni-gpib-hs", "the 68369A adapter (GPIB-USB-HS)"))
    for knd, nm in checks:
        d = hostport_drift(spec, knd, detect=detect)
        if d:
            log(f"{nm} is now on USB port {d[1]} but its pin expects {d[0]} -- re-plug it into "
                f"port {d[0]}, or re-run to re-pin (a stale pin never re-attaches, just polls).")
            return True
    return False


def _timeout_message(spec: VmSpec, who: str, wait_timeout: float, detect) -> str:
    """The BridgeUnavailable message when an instance never answers: name which unit is missing and
    emit the specific recovery -- a wedged FX2 for an adapter that IS on the USB bus (a re-plug, not
    a reboot), or 'not on the bus' for one that is absent. Fix 2: per-unit detail for every role."""
    both_role = spec.role == ROLE_BOTH
    host_devs = (detect or detect_gpib_usb)()
    b_on_host = any(d.kind == "ni-gpib-b" for d in host_devs)
    hs_on_host = any(d.kind == "ni-gpib-hs" for d in host_devs)
    if both_role:
        a_up = bridge_reachable(spec, timeout_ms=1500)
        s_up = source_reachable(spec, timeout_ms=1500)
        detail = (f" [analyzer pad {spec.gpib_addr}: {'UP' if a_up else 'NOT FOUND'}; "
                  f"source pad {spec.source_addr}: {'UP' if s_up else 'NOT FOUND'}].")
        if not a_up:
            detail += " " + (_wedged_adapter_recovery("the 8565EC adapter (GPIB-USB-B)")
                             if b_on_host else _not_on_bus("the 8565EC's GPIB-USB-B"))
        if not s_up:
            detail += " " + (_wedged_adapter_recovery("the 68369A adapter (GPIB-USB-HS)")
                             if hs_on_host else _not_on_bus("the 68369A's GPIB-USB-HS"))
    elif spec.role == ROLE_SOURCE:
        detail = f" [source pad {spec.source_addr}: NOT FOUND]. " + (
            _wedged_adapter_recovery("the 68369A adapter (GPIB-USB-HS)")
            if hs_on_host else _not_on_bus("the 68369A's GPIB-USB-HS"))
    else:  # ROLE_ANALYZER
        detail = f" [analyzer pad {spec.gpib_addr}: NOT FOUND]. " + (
            _wedged_adapter_recovery("the 8565EC adapter (GPIB-USB-B)")
            if b_on_host else _not_on_bus("the 8565EC's GPIB-USB-B"))
    warn = _shared_controller_warning(host_devs)             # R1a: surface a same-controller race
    if warn:
        detail += " " + warn
    return (f"qemu booted but {who} did not answer within {wait_timeout:.0f}s.{detail} Check the "
            f"GPIB address(es), that the adapter passed through, and that ni_usb_gpib loaded -- "
            f"see {os.path.join(spec.workdir(), 'qemu.log')}.")


def _wedge_verdict_message(spec: VmSpec, who: str, attempts: int, detect) -> str:
    """R6 EARLY WEDGE VERDICT: {who}'s adapter is PRESENT on the USB bus but stayed SILENT after
    `attempts` QMP re-attach(es) -- classify ADAPTER_WEDGED and surface the physical-replug recovery
    NOW instead of grinding to the full timeout. Mentions qemu.log + any same-controller race."""
    host_devs = (detect or detect_gpib_usb)()
    b_on = any(d.kind == "ni-gpib-b" for d in host_devs)
    hs_on = any(d.kind == "ni-gpib-hs" for d in host_devs)
    recov = []
    if spec.role in (ROLE_ANALYZER, ROLE_BOTH) and b_on:
        recov.append(_wedged_adapter_recovery("the 8565EC adapter (GPIB-USB-B)"))
    if spec.role in (ROLE_SOURCE, ROLE_BOTH) and hs_on:
        recov.append(_wedged_adapter_recovery("the 68369A adapter (GPIB-USB-HS)"))
    warn = _shared_controller_warning(host_devs)
    if warn:
        recov.append(warn)
    return (f"ADAPTER_WEDGED: {who} is present on the USB bus but stayed SILENT after {attempts} "
            f"QMP re-attach attempt(s) -- not waiting the full timeout. " + " ".join(recov)
            + f" (did not answer; see {os.path.join(spec.workdir(), 'qemu.log')}).")


def _read_console(spec: VmSpec) -> str:  # pragma: no cover - reads the qemu serial capture
    """Read the guest serial console that qemu captured to qemu.log (host side). provision.sh's
    milestones -- boards online, the bridge-launch section header -- land here, so the host can tell
    a still-provisioning guest from a genuinely wedged adapter without touching the guest."""
    with open(os.path.join(spec.workdir(), "qemu.log"), "r", errors="replace") as f:
        return f.read()


def guest_boards_online(spec: VmSpec, *, read=None) -> bool:
    """True once the guest console shows provisioning REACHED the bridge-launch stage: the GPIB board
    bring-up finished and the bridge systemd services are starting. BEFORE this the guest is still
    booting/compiling ni_usb_gpib (a fresh provision takes ~60-120s) and a still-silent instrument is
    NORMAL, not a wedge; AFTER it a still-silent instrument is a REAL fault (a wedged FX2 / a board
    that never onlined), so the ADAPTER_WEDGED verdict may fire. Reads spec.workdir()/qemu.log; never
    raises -- False if the log is not there yet."""
    try:
        text = (read or _read_console)(spec)
    except OSError:
        return False
    return ("5. bridge launcher" in text) or ("attached boards online" in text)


def _poll_ready(spec: VmSpec, *, reachable, detect=None, reattach=None, reattach_source=None,
                wait_timeout: float = 900.0, poll_interval: float = 5.0, settle_s: float = 2.0,
                wedge_after_attempts: int = 2, wedge_grace_s: float = 60.0, read_console=None,
                log=print, sleep=time.sleep) -> str:
    """Poll a LAUNCHED instance to readiness and return spec.net_addr, or raise BridgeUnavailable.
    Drives BOTH QMP wedge recoveries -- the GPIB-USB-B FX2 re-enumeration (analyzer) and the
    GPIB-USB-HS FX2 wedge (source) -- each re-attach retried up to `wedge_after_attempts` times, plus
    the hostport-drift warning. In a role=both VM a settle_s delay is inserted BETWEEN the two
    device_adds so they do not race on one host controller.

    R6 WEDGE VERDICT (corrected -- live false-positive fix): a FRESH-provision guest compiles
    ni_usb_gpib from source and only brings its board online + starts the bridge ~60-120s in. The old
    verdict fired the instant the re-attach budget was spent (~10-15s), so it killed HEALTHY bring-ups
    mid-provision -- observed live: both golden guests reached 'board online' + '5. bridge launcher'
    at the exact moment the host declared them wedged, and the QMP re-attaches were yanking the USB
    device out from under the guest driver. So classify ADAPTER_WEDGED only once the guest console
    (qemu.log) shows provisioning REACHED the bridge-launch stage AND `wedge_grace_s` has elapsed with
    the instrument still silent (grace for the just-started bridge service to bind). Before that, keep
    polling to the full timeout. Goes straight to the poll loop (the caller did the up-front reachable
    check); NO fake."""
    both_role = spec.role == ROLE_BOTH
    who = _who(spec)
    reattach = reattach or reattach_analyzer_b
    reattach_src = reattach_source or reattach_source_hs
    drift_warned = False
    attempts_a = attempts_s = 0
    provisioned_polls = 0                                     # polls seen AFTER the guest provisioned
    grace_polls = max(1, int(wedge_grace_s / poll_interval)) if poll_interval > 0 else 1
    has_analyzer = spec.role in (ROLE_ANALYZER, ROLE_BOTH)
    has_source = spec.role in (ROLE_SOURCE, ROLE_BOTH)
    for _ in range(max(1, int(wait_timeout / poll_interval))):
        sleep(poll_interval)
        if reachable():
            log(f"bridge up at {spec.net_addr} -- ni_usb_gpib is driving {who}"
                + (f" (source at {spec.source_net_addr})" if both_role else ""))
            return spec.net_addr
        did_a = (has_analyzer and attempts_a < wedge_after_attempts
                 and reattach(spec, log=log, detect=detect))
        if did_a:
            attempts_a += 1
        # settle between two device_adds on the same host controller (role=both only): a source
        # re-attach fired in the SAME poll as an analyzer re-attach would race it -> both wedge.
        if did_a and has_source and attempts_s < wedge_after_attempts:
            sleep(settle_s)
        if (has_source and attempts_s < wedge_after_attempts
                and reattach_src(spec, log=log, detect=detect)):
            attempts_s += 1
        if (attempts_a or attempts_s) and not drift_warned:
            drift_warned = _warn_hostport_drift(spec, has_analyzer, has_source, detect, log)
        # R6 (corrected): only a PROVISIONED-but-still-silent adapter is a wedge. Wait for the guest
        # to reach the bridge-launch stage, then a grace period, before the present-but-silent verdict.
        if guest_boards_online(spec, read=read_console):
            provisioned_polls += 1
        tried = attempts_a + attempts_s
        if (tried >= wedge_after_attempts and provisioned_polls >= grace_polls
                and _target_present(spec, detect)):
            raise BridgeUnavailable(_wedge_verdict_message(spec, who, tried, detect))
    raise BridgeUnavailable(_timeout_message(spec, who, wait_timeout, detect))


def _await_host_settle(spec: VmSpec, *, detect=None, reachable=None, max_seconds: float = 25.0,
                       poll_interval: float = 5.0, sleep=time.sleep, log=print) -> bool:
    """HOST-STATE SETTLE between the two golden launches (replaces the blind timer).

    THE REAL INVARIANT (R1c): SERIALIZE the host-side FX2 claim/RESET -- the two 0x3923 devices'
    resets must NEVER overlap, or both wedge at -110. What matters is the ~1-2s host-side usb-host
    claim+reset each qemu does right after Popen, NOT the in-guest fxload minutes later; so this MUST
    NOT be optimized back into 'launch both' or a fixed sleep. R1b: gate the SECOND launch on REAL
    host state -- poll detect_gpib_usb() until the FIRST VM's adapter is STABLE across two consecutive
    reads (its claim/reset finished without dropping or re-enumerating it), or the GPIB-USB-B has
    reached 0x702a, or the instance is already reachable. Bounded by max_seconds; returns True once
    settled, False on the bound (the caller proceeds -- the poll phase owns the real timeout). NEVER
    raises."""
    reachable = reachable or (lambda: instance_reachable(spec))
    dfn = detect or detect_gpib_usb
    wants_b = _target_kind(spec) in ("ni-gpib-b", "both")
    prev = _adapter_identity(spec, dfn)
    steps = max(1, int(max_seconds / poll_interval)) if poll_interval > 0 else 1
    for _ in range(steps):
        if reachable():
            log(f"first VM '{spec.name}' is up -- host-side claim settled; launching the second now")
            return True
        if wants_b and host_b_fxloaded(dfn):
            log(f"first VM '{spec.name}' adapter reached 0x702a (fxload done) -- launching the second")
            return True
        cur = _adapter_identity(spec, dfn)
        if cur is not None and cur == prev:
            log(f"first VM '{spec.name}' adapter STABLE on the host {cur} across two reads -- its "
                f"usb-host claim/reset settled; launching the second (the resets will not overlap)")
            return True
        prev = cur
        sleep(poll_interval)
    log(f"first VM '{spec.name}' host state not settled after ~{max_seconds:.0f}s -- launching the "
        f"second anyway (the poll phase owns the real timeout)")
    return False


def _launch_one(spec: VmSpec, *, reachable=None, qemu_check=None, detect=None, prepare=None,
                launch=None, already_booting=None, reset: bool = False, log=print) -> str:
    """Launch ONE golden qemu instance UNLESS it is already up. Returns 'up' (already reachable, no
    boot), or 'launched'/'booting'. Raises BridgeUnavailable if qemu is missing or no NI adapter is
    attached. Used by ensure_golden_pair to boot both VMs BEFORE polling either (concurrent-capable,
    not serial shared-fate)."""
    reachable = reachable or (lambda: instance_reachable(spec))
    if reachable():
        log(f"bridge already up at {spec.net_addr}")
        return "up"
    ok, msg = (qemu_check or qemu_available)()
    if not ok:
        raise BridgeUnavailable(msg)
    devices = (detect or detect_gpib_usb)()
    if pick_ni_device(devices) is None:
        raise BridgeUnavailable("no NI GPIB adapter attached -- nothing to pass through. "
                                + usb_summary(devices))
    booting = (already_booting or instance_is_live)(spec)
    _boot_instance(spec, prepare=prepare, launch=launch,
                   already_booting=(lambda s: booting), reset=reset, log=log)
    return "booting" if booting else "launched"


def ensure_bridge(spec: VmSpec, *, reachable=None, detect=None, qemu_check=None,
                  prepare=None, launch=None, reattach=None, reattach_source=None,
                  already_booting=None, require_both: bool = False, reset: bool = False,
                  wait_timeout: float = 900.0, poll_interval: float = 5.0, settle_s: float = 2.0,
                  log=print, sleep=time.sleep) -> str:
    """Bring up the qemu USB-passthrough bridge and return spec.net_addr, or raise
    BridgeUnavailable. There is NO fake fallback anywhere in this path. If the bridge is
    already up, return at once. Steps are injectable for hardware-free testing.

    Readiness is per this instance's ROLE: an analyzer VM needs the 8565EC to answer, a source
    VM needs the 68369A, a role=both VM needs BOTH. require_both is retained for callers but a
    role=both spec already gates on both units via instance_reachable. A re-run inside the boot
    window does NOT spawn a duplicate qemu (instance_is_live guard); the poll drives BOTH the
    analyzer (GPIB-USB-B) and source (GPIB-USB-HS) FX2 wedge recoveries."""
    reachable_injected = reachable is not None
    both_role = spec.role == ROLE_BOTH
    who = _who(spec)
    reachable = reachable or (lambda: instance_reachable(spec))
    if reachable():
        log(f"bridge already up at {spec.net_addr} ({who})")
        return spec.net_addr
    # role=both, bridge up but a unit missing: don't boot a SECOND VM -- report what's absent.
    if both_role and not reachable_injected and bridge_reachable(spec, timeout_ms=1500):
        raise BridgeUnavailable(
            f"the qemu bridge is UP and the 8565EC answers (pad {spec.gpib_addr}), but the "
            f"68369A source was not found (pad {spec.source_addr}) -- it is powered off, "
            f"uncabled, at a different GPIB address, or its board is not configured. Fix, re-run.")
    ok, msg = (qemu_check or qemu_available)()
    if not ok:
        raise BridgeUnavailable(msg)
    devices = (detect or detect_gpib_usb)()
    if pick_ni_device(devices) is None:
        raise BridgeUnavailable("no NI GPIB adapter attached -- nothing to pass through. "
                                + usb_summary(devices))
    # only the DEFAULT prepare takes reset; an injected prepare keeps its one-arg contract.
    _boot_instance(spec, prepare=prepare, launch=launch, already_booting=already_booting,
                   reset=reset, log=log)
    return _poll_ready(spec, reachable=reachable, detect=detect, reattach=reattach,
                       reattach_source=reattach_source, wait_timeout=wait_timeout,
                       poll_interval=poll_interval, settle_s=settle_s, log=log, sleep=sleep)


def _run_parallel(jobs: list, fn) -> dict:
    """Run fn(arg) for each (key, arg) CONCURRENTLY (one daemon thread each) and return
    {key: return_value_or_exception}. R2: the two golden units recover INDEPENDENTLY -- a wedged
    analyzer must NOT delay the source's re-attach by up to the full per-unit timeout (the old serial
    for-loop's worst case was ~2x the timeout). Each thread writes only its own key (GIL-atomic)."""
    results = {}

    def _worker(key, arg):
        try:
            results[key] = fn(arg)
        except Exception as e:                      # noqa: BLE001 -- captured per job; caller surfaces
            results[key] = e

    threads = [threading.Thread(target=_worker, args=(k, a), daemon=True) for k, a in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def ensure_golden_pair(ana: VmSpec = None, src: VmSpec = None, *, launch_one=None,
                       poll_ready=None, settle=None, reachable=None, run_parallel=None, log=print,
                       wait_timeout: float = 240.0, poll_interval: float = 5.0,
                       stagger_seconds: float = 25.0, settle_between_s: float = 2.0,
                       reset: bool = False, detect=None, qemu_check=None, prepare=None,
                       launch=None, reattach=None, reattach_source=None, already_booting=None,
                       sleep=time.sleep) -> tuple:
    """Bring up the GOLDEN two-instance deployment and return (analyzer_net_addr, source_net_addr).

    STAGGERED, HOST-STATE-GATED LAUNCH: boot the FIRST VM, wait until its adapter's host-side
    usb-host claim/RESET has SETTLED (stable across two detect reads, or 0x702a, or reachable --
    bounded by stagger_seconds), THEN boot the SECOND. The two 0x3923 devices' resets must NEVER
    overlap or BOTH wedge at -110 (this is a host-side claim/reset serialization, NOT the in-guest
    fxload and NOT a blind timer). After the second launch we RE-VERIFY the first adapter did not
    fall off the host (a shared-controller reset collision) and STOP+report it if so. R1a: warns if
    both adapters are pinned to the same host controller (the structural race). R2: the two units are
    polled INDEPENDENTLY (threads) so a wedged one does not delay the other's recovery.

    DEGRADED behavior preserved: a per-unit timeout returns the other's address (single-instrument
    verbs proceed) with the wedged-adapter recovery; raises ONLY if NEITHER answers. Launch ORDER:
    analyzer-first when port-pinned; SOURCE-first when the analyzer is only vendor-pinned (Fix 6).
    `launch_one`/`poll_ready`/`settle`/`reachable`/`run_parallel` are injectable for testing."""
    if ana is None or src is None:
        ga, gs = golden_pair(ssh_pubkey=default_ssh_pubkey())
        ana, src = ana or ga, src or gs
    launch_one = launch_one or _launch_one
    poll_ready = poll_ready or _poll_ready
    settle = settle or _await_host_settle
    run_parallel = run_parallel or _run_parallel
    reach_of = reachable or (lambda spec: instance_reachable(spec))
    log(f"golden pair: analyzer instance '{ana.name}' + source instance '{src.name}' "
        f"(host-state-gated STAGGERED launch, then poll both INDEPENDENTLY; per-unit timeout "
        f"{wait_timeout:.0f}s)")
    # R1a STRUCTURAL: both adapters pinned to the SAME host controller can race a shared reset.
    hb_a = match_to_qmp_args(ana.analyzer_match()).get("hostbus")
    hb_s = match_to_qmp_args(src.source_match()).get("hostbus")
    if hb_a is not None and hb_s is not None and hb_a == hb_s:
        log(f"WARNING: both NI adapters are pinned to the SAME host USB controller (hostbus {hb_a}) "
            f"-- they can race a shared USB reset and BOTH wedge at -110. Move ONE adapter to a USB "
            f"port on a DIFFERENT controller (a powered hub on another root port) for cold-boot "
            f"reliability; different controllers physically cannot race a shared reset.")
    first, second = ana, src
    if "hostport" not in ana.analyzer_match():
        first, second = src, ana
        log("analyzer adapter is not port-pinned -> launching the source (GPIB-USB-HS) VM FIRST "
            "so the vendor-only analyzer match cannot grab the HS (0x3923 is shared)")

    def _boot(spec):
        launch_one(spec, reachable=(lambda s=spec: reach_of(s)), qemu_check=qemu_check,
                   detect=detect, prepare=prepare, launch=launch,
                   already_booting=already_booting, reset=reset, log=log)

    # 1) STAGGERED, HOST-STATE-GATED LAUNCH -- never pass both FX2s through at the same instant.
    _boot(first)
    settle(first, detect=detect, reachable=(lambda s=first: reach_of(s)),
           max_seconds=stagger_seconds, poll_interval=poll_interval, sleep=sleep, log=log)
    first_present_before = _target_present(first, detect)
    _boot(second)
    # R1b RE-VERIFY: did the second launch knock the first adapter present->absent (a shared-
    # controller reset collision)? If so, STOP+report the first NOW rather than grind a full poll.
    pre_absent = {}
    if first_present_before and not _target_present(first, detect):
        fkey = "analyzer" if first.role == ROLE_ANALYZER else "source"
        note = _shared_controller_warning((detect or detect_gpib_usb)()) or (
            "move the two adapters to different USB controllers / a powered hub, then re-run.")
        pre_absent[fkey] = (f"the {fkey} adapter FELL OFF the host bus the instant the second VM "
                            f"claimed its adapter -- a shared-controller USB reset collision. " + note)
        log(f"golden: STOP -- {pre_absent[fkey]}")

    # 2) POLL both INDEPENDENTLY (R2 threads); DEGRADE per-instance.
    def _poll(spec):
        if reach_of(spec):
            return None
        poll_ready(spec, reachable=(lambda: reach_of(spec)), detect=detect, reattach=reattach,
                   reattach_source=reattach_source, wait_timeout=wait_timeout,
                   poll_interval=poll_interval, settle_s=settle_between_s, log=log, sleep=sleep)
        return None

    jobs = [(key, spec) for spec, key in ((ana, "analyzer"), (src, "source"))
            if key not in pre_absent]
    results = run_parallel(jobs, _poll)
    absent = list(pre_absent.keys())
    for key in ("analyzer", "source"):
        if key in pre_absent:
            continue
        r = results.get(key)
        if isinstance(r, BridgeUnavailable):
            absent.append(key)
            log(f"golden: the {key} instance is ABSENT -- {r}")
        elif isinstance(r, Exception):
            raise r                                 # an unexpected error is not a graceful degrade
    absent = sorted(set(absent))
    if len(absent) == 2:
        raise BridgeUnavailable("golden pair: NEITHER unit answered -- see the per-unit recovery "
                                "above. Stop any stuck qemu (cli.py vm-stop), replug, then re-run.")
    if absent:
        log(f"golden pair DEGRADED: the {absent[0]} unit is ABSENT; the other is up so "
            f"single-instrument verbs (sa/sg) proceed. The coordinator needs BOTH and will report "
            f"not-ready until the absent unit is recovered.")
    return (ana.analyzer_net_addr, src.source_net_addr)


# =============================================================== SINGLETON (one VM, serial hot-plug)

def singleton_spec(base_port: int = 5555, source_port: int = 5556, ssh_port: int = 2222,
                   ssh_pubkey: str = "", devices: list = None) -> VmSpec:
    """The SINGLETON deployment: ONE VM (role=both, hotplug) that claims BOTH adapters via SERIAL,
    verified, post-boot QMP hot-plug -- the canonical SINGLE-MACHINE topology. Boots the two USB
    controllers EMPTY (no UEFI ASSERT), then attaches HS then B one at a time so the two host-side
    resets never overlap. Both adapters are pinned to their host port when detected (so attach_adapter
    targets the right physical port and can re-follow the B's fxload re-enumeration)."""
    spec = VmSpec(name="se299-singleton", role=ROLE_BOTH, hotplug=True, port=base_port,
                  source_port=source_port, ssh_port=ssh_port, ssh_pubkey=ssh_pubkey)
    if devices:
        b = next((d for d in devices if d.kind == "ni-gpib-b"), None)
        hs = next((d for d in devices if d.kind == "ni-gpib-hs"), None)
        if b is not None and hostport_match(b):
            spec = replace(spec, analyzer_usb_match=hostport_match(b))
        if hs is not None and hostport_match(hs):
            spec = replace(spec, source_usb_match=hostport_match(hs))
    return spec


def _which_present(which: str, detect=None) -> bool:
    """True iff the adapter for `which` ('b'/'analyzer' -> GPIB-USB-B, 'hs'/'source' -> GPIB-USB-HS)
    is present on the host USB bus right now."""
    devs = (detect or detect_gpib_usb)()
    if which in ("b", "analyzer"):
        return any(d.kind == "ni-gpib-b" for d in devs)
    return any(d.kind == "ni-gpib-hs" for d in devs)


def attach_adapter(spec: VmSpec, which: str, *, log=print, detect=None, execute=qmp_execute,
                   sleep=time.sleep, fxload_timeout: float = 90.0, poll_interval: float = 3.0) -> bool:
    """The P5-Attach PRIMARY primitive (generalizes reattach_analyzer_b/reattach_source_hs). QMP
    device_add the pinned adapter on its guest controller -- HS onto ehci.0 (id=ni_hs), B onto xhci.0
    (id=ni_b). A device_del PREFIX makes the INITIAL attach and a RECOVERY attach ONE code path (the
    del is TOLERANT -- it just fails harmlessly when nothing is attached yet). For the B: after the
    add the guest runs fxload (0x702b->0x702a); qemu does not follow that re-enumeration, so wait for
    the host to reach 0x702a (bounded fxload_timeout), then device_del+add so the guest re-enumerates
    the LIVE PID. For the HS: onboard firmware, no fxload wait. NEVER raises; returns True iff it
    issued the attach (False if not pinned/known or a QMP error)."""
    if which in ("b", "analyzer"):
        bus, dev_id, match, is_b = "xhci.0", "ni_b", spec.analyzer_match(), True
    else:
        bus, dev_id, match, is_b = "ehci.0", "ni_hs", spec.source_match(), False
    add_args = match_to_qmp_args(match)
    add_args.update(driver="usb-host", bus=bus, id=dev_id)
    sock = spec.qmp_sock()

    def _del():
        try:                                    # tolerant: no-op when nothing is attached yet
            execute(sock, [{"execute": "device_del", "arguments": {"id": dev_id}}])
        except Exception:                       # noqa: BLE001
            pass

    def _add():
        execute(sock, [{"execute": "device_add", "arguments": dict(add_args)}])

    try:
        _del()
        sleep(1.0)                              # let the guest process any prior unplug first
        _add()
        log(f"attached {dev_id} on {bus} via QMP ({match})")
        if is_b:
            for _ in range(max(1, int(fxload_timeout / poll_interval))):
                if host_b_fxloaded(detect):
                    _del()
                    sleep(1.0)
                    _add()                      # re-follow the LIVE 0x702a PID after fxload
                    log(f"B reached 0x702a (fxload done) -- re-added {dev_id} so the guest follows "
                        f"the live device")
                    return True
                sleep(poll_interval)
            log(f"B fxload did not reach 0x702a within {fxload_timeout:.0f}s -- left {dev_id} added; "
                f"the readiness poll will retry the re-attach")
        return True
    except Exception as e:                      # noqa: BLE001 -- never fatal to the bring-up
        log(f"attach of {dev_id} failed ({e}); the readiness poll will retry")
        return False


def _attach_and_verify(spec: VmSpec, which: str, *, reachable_fn, attach=None, detect=None,
                       wedge_after_attempts: int = 2, wait_timeout: float = 240.0,
                       poll_interval: float = 5.0, sleep=time.sleep, log=print) -> None:
    """SERIAL per-adapter attach + VERIFY for the singleton. attach_adapter(which) [initial], then
    poll reachable_fn until the instrument answers, RE-attaching (recovery = the SAME primitive, its
    device_del prefix makes it idempotent) up to wedge_after_attempts. R6: once the adapter is
    PRESENT-but-SILENT after the budget, raise ADAPTER_WEDGED immediately (do not grind the full
    timeout). Raises BridgeUnavailable if it never answers; returns None on success."""
    attach = attach or attach_adapter
    who = {"b": "the 8565EC", "analyzer": "the 8565EC",
           "hs": "the 68369A", "source": "the 68369A"}[which]
    attach(spec, which, log=log, detect=detect)             # initial attach (P5-Attach PRIMARY)
    attempts = 1
    for _ in range(max(1, int(wait_timeout / poll_interval))):
        if reachable_fn():
            log(f"{who} answered after {attempts} attach attempt(s)")
            return
        if attempts >= wedge_after_attempts and _which_present(which, detect):
            raise BridgeUnavailable(_wedge_verdict_message(spec, who, attempts, detect))
        sleep(poll_interval)
        if attempts < wedge_after_attempts:
            attach(spec, which, log=log, detect=detect)     # recovery = same primitive (del-prefix)
            attempts += 1
    raise BridgeUnavailable(_wedge_verdict_message(spec, who, attempts, detect))


def _guest_marker_present(spec: VmSpec) -> bool:  # pragma: no cover - shells out to ssh
    """True iff the guest's /run/se299/provisioned marker exists (checked over the SSH hostfwd)."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=4",
             "-o", "BatchMode=yes", "-p", str(spec.ssh_port), "ubuntu@127.0.0.1",
             "test -f /run/se299/provisioned"],
            capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def await_guest_provisioned(spec: VmSpec, *, timeout: float = 720.0, poll_interval: float = 5.0,
                            probe=None, sleep=time.sleep, log=print) -> bool:
    """Gate BEFORE hot-plugging adapters: the B's fxload rule + ni_usb_gpib exist ONLY after cloud-
    init runs provision.sh, so attaching before that races provisioning. Poll for the guest's
    /run/se299/provisioned marker (provision.sh's FINAL step) over the SSH hostfwd (:2222), bounded
    by `timeout` (~12 min first boot / ~90 s warm). With NO ssh key, fall back to a QMP-status check
    (confirms the VM is running -- weaker, but better than nothing). NEVER raises; returns True once
    provisioned (or the QMP fallback confirms a live guest)."""
    if probe is None:
        probe = ((lambda: _guest_marker_present(spec)) if spec.ssh_pubkey
                 else (lambda: qmp_alive(spec.qmp_sock())))
    for _ in range(max(1, int(timeout / poll_interval))):
        if probe():
            log(f"guest '{spec.name}' signalled provisioned -- safe to hot-plug the adapters")
            return True
        sleep(poll_interval)
    log(f"guest '{spec.name}' did not signal provisioned within {timeout:.0f}s")
    return False


def ensure_singleton(spec: VmSpec = None, *, both_reachable_fn=None, hs_reachable_fn=None,
                     b_reachable_fn=None, live=None, provisioned=None, launch_one=None,
                     attach_and_verify=None, attach=None, stop=None, relaunch=None, detect=None,
                     qemu_check=None, prepare=None, launch=None, already_booting=None,
                     reset: bool = False, wait_timeout: float = 240.0, poll_interval: float = 5.0,
                     provision_timeout: float = 720.0, log=print, sleep=time.sleep) -> tuple:
    """SINGLETON bring-up: ONE VM claims BOTH adapters via SERIAL, verified, post-boot hot-plug.
    Returns (analyzer_net_addr, source_net_addr); raises BridgeUnavailable only if NEITHER unit comes
    up. Steps injectable for hardware-free testing.

      * REUSE      -- both instruments already answer -> return at once (NO boot).
      * JOIN       -- a qemu is already live (pidfile/QMP) but a unit is missing -> DO NOT reboot;
                      attach only the MISSING adapter(s) onto the running VM (self-heal: if the
                      joined VM is not provisioned, stop it once + relaunch).
      * COLD BOOT  -- boot the controllers EMPTY (no UEFI ASSERT), GATE on the guest being
                      provisioned, THEN SERIAL attach: HS attach+VERIFY, THEN B attach+VERIFY -- the
                      two host resets NEVER overlap. Per-unit DEGRADE (return the healthy address,
                      mark the other absent with the wedged-adapter recovery)."""
    if spec is None:
        spec = singleton_spec(ssh_pubkey=default_ssh_pubkey())
    both_reachable_fn = both_reachable_fn or (lambda: instance_reachable(spec))
    hs_reachable_fn = hs_reachable_fn or (lambda: source_reachable(spec))
    b_reachable_fn = b_reachable_fn or (lambda: bridge_reachable(spec))
    live = live or (lambda: instance_is_live(spec))
    provisioned = provisioned or (lambda: await_guest_provisioned(spec, timeout=provision_timeout,
                                                                  log=log))
    launch_one = launch_one or _launch_one
    attach_and_verify = attach_and_verify or _attach_and_verify
    stop = stop or stop_instance
    addrs = (spec.analyzer_net_addr, spec.source_net_addr)

    # REUSE: both already answer.
    if both_reachable_fn():
        log(f"singleton '{spec.name}' already up -- both instruments answer "
            f"({spec.analyzer_net_addr} + {spec.source_net_addr})")
        return addrs

    if live():
        # JOIN a running qemu -- never reboot; attach only what's missing. Self-heal if unprovisioned.
        log(f"a singleton qemu '{spec.name}' is already live -- JOINING it (no reboot); attaching "
            f"only the missing adapter(s)")
        if not provisioned():
            log(f"joined singleton '{spec.name}' is not provisioned -- self-heal: stop once + relaunch")
            stop(spec, force=True, log=log)
            (relaunch or launch_one)(spec, reachable=both_reachable_fn, qemu_check=qemu_check,
                                     detect=detect, prepare=prepare, launch=launch,
                                     already_booting=(lambda s: False), reset=reset, log=log)
            provisioned()
    else:
        # COLD BOOT controllers-only, then gate on provisioning BEFORE any attach.
        launch_one(spec, reachable=both_reachable_fn, qemu_check=qemu_check, detect=detect,
                   prepare=prepare, launch=launch, already_booting=already_booting, reset=reset,
                   log=log)
        if not provisioned():
            raise BridgeUnavailable(
                f"singleton guest '{spec.name}' never signalled provisioned within "
                f"{provision_timeout:.0f}s -- cloud-init/provision.sh did not finish; see "
                f"{os.path.join(spec.workdir(), 'qemu.log')} (ssh -p {spec.ssh_port} ubuntu@127.0.0.1).")

    # SERIAL attach + verify: HS FIRST (fully), THEN B -- the two host resets never overlap. Skip a
    # unit already answering (the JOIN case attaches only the missing one). Per-unit DEGRADE.
    absent = []
    for which, key, reach_one in (("hs", "source", hs_reachable_fn),
                                  ("b", "analyzer", b_reachable_fn)):
        if reach_one():
            continue
        try:
            attach_and_verify(spec, which, reachable_fn=reach_one, attach=attach, detect=detect,
                              wait_timeout=wait_timeout, poll_interval=poll_interval,
                              sleep=sleep, log=log)
        except BridgeUnavailable as e:
            absent.append(key)
            log(f"singleton: the {key} unit is ABSENT -- {e}")
    if len(absent) == 2:
        raise BridgeUnavailable("singleton: NEITHER unit answered after serial attach -- see the "
                                "per-unit recovery above. cli.py vm-stop, replug, then re-run.")
    if absent:
        log(f"singleton DEGRADED: the {absent[0]} unit is ABSENT; the other is up so single-"
            f"instrument verbs (sa/sg) proceed. The coordinator needs BOTH and reports not-ready "
            f"until the absent unit is recovered.")
    return addrs


def launch_plan(spec: VmSpec) -> str:
    """A human-readable plan: what qemu will run and the manual prep. Printed by the CLI so
    the user can see (and run) the exact command; no fake, no silent boot."""
    ok, msg = qemu_available()
    devices = detect_gpib_usb()
    ni = pick_ni_device(devices)
    lines = ["QEMU GPIB-bridge VM plan (USB passthrough + ni_usb_gpib):",
             "  " + usb_summary(devices).replace("\n", "\n  ")]
    if ni is None:
        lines.append("  ** no NI adapter attached -- nothing to pass through.")
    if not ok:
        lines.append(f"  ** {msg}")
    warn = _shared_controller_warning(devices)               # R1a: same-controller reset race
    if warn:
        lines.append("  ** " + warn)
    wd = spec.workdir()
    lines += [
        f"  prep (once): mkdir -p {wd}; download {os.path.basename(spec.image_location)} -> disk.qcow2;",
        f"               qemu-img resize disk.qcow2 16G; make a cloud-init seed.iso (hdiutil makehybrid).",
        f"  passthrough: HS 0x709b -> EHCI (68369A); GPIB-USB-B by vendor 0x{spec.usb_vendor_id:04x} -> XHCI (8565EC).",
        f"  singleton (SINGLE-MACHINE DEFAULT): ONE VM, controllers booted EMPTY (no UEFI ASSERT), "
        f"then HS then B attached SERIALLY -- analyzer {spec.analyzer_net_addr} + source {spec.source_net_addr}.",
        f"  golden mode (remote / fault-isolation): TWO VMs -- one qemu / one adapter each.",
        f"  both mode  : ONE VM, at-boot passthrough of both adapters (SUPERSEDED -- UEFI ASSERT + simultaneous-claim fragile).",
        f"  analyzer GUI:  uv run python rf-se/se299/cli.py live --analyzer {spec.analyzer_net_addr}",
        f"  both units:    uv run python rf-se/se299/cli.py coordinator --vm   (launches both, waits, runs the campaign)",
        f"  stop / reset:  uv run python rf-se/se299/cli.py vm-stop   (before --vm-reset, which refuses while a qemu holds the overlay open)",
        "  wedged adapter (detected on USB but never answers): "
        + _wedged_adapter_recovery("that adapter"),
    ]
    return "\n".join(lines)
