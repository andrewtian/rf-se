"""Control plane (R9): discover, type-by-capability, and resolve ANY TX/RX on the network.

Separates the CONTROL plane (which units exist, their roles/capabilities, how to open one)
from the DATA plane (the GPIB transactions). Two abstract roles: rx = analyzer
(SpectrumAnalyzer contract: sweep / zero-span / marker / floor) and tx = source
(SignalGenerator contract: cw / rf-onoff / list-sweep). A driver REGISTRY maps a model/idn to
a concrete driver class and its role; a new instrument model plugs in by registering a driver,
with ZERO change to the Coordinator or the GUIs -- they speak only the abstract RX/TX
contract. The ControlPlane holds the live ROSTER of discovered units and RESOLVES an rx/tx
handle (an auto-reconnecting Link) by capability, not by address, then composes any (tx, rx)
pair into a Coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import connection as conn
import coordinator
import discover as disc
import drivers


# ----------------------------------------------------------------- capability contract
# A unit's role determines its capability set; roles/GUIs depend on these, not on a model.
CAPABILITIES = {
    "rx": ("sweep", "zero-span", "marker", "floor"),
    "tx": ("cw", "rf-onoff", "list-sweep"),
}


class ControlPlaneError(RuntimeError):
    """A control-plane operation could not be satisfied (e.g. no rx/tx pair available)."""


# ----------------------------------------------------------------- driver registry (plugin)

@dataclass(frozen=True)
class DriverSpec:
    match: str                    # case-insensitive model/idn substring
    kind: str                     # "rx" | "tx"
    driver: type                  # concrete driver class: driver(transport) -> instrument
    freq_lo_hz: float             # instrument capability window (for link validation)
    freq_hi_hz: float
    label: str = ""
    family: str = ""              # looser token: a near-miss model reports DETECTED-INVALID


_REGISTRY = []                    # list[DriverSpec]; resolve scans most-recent-first


def register_driver(match, kind, driver, freq_lo_hz, freq_hi_hz, label="", family=""):
    """Register a driver so the control plane can operate that instrument model. Later
    registrations for the same token take precedence, so a caller can override a seed."""
    if kind not in ("rx", "tx"):
        raise ValueError(f"kind must be 'rx' or 'tx', got {kind!r}")
    _REGISTRY.append(DriverSpec(match.lower(), kind, driver, float(freq_lo_hz),
                                float(freq_hi_hz), label or match, family.lower()))


def resolve_driver(model_or_idn):
    """The DriverSpec whose token matches this model/idn, or None (unknown -> honest skip)."""
    m = (model_or_idn or "").lower()
    for spec in reversed(_REGISTRY):
        if spec.match in m:
            return spec
    return None


def default_spec(kind):
    """The first-registered driver spec for a role (the campaign default: 8565EC rx /
    68369A tx). Used when an address is given without discovery, so the model is assumed."""
    for spec in _REGISTRY:
        if spec.kind == kind:
            return spec
    return None


# Seed the two campaign instruments. Any other analyzer/source plugs in via register_driver.
register_driver("8565", "rx", drivers.Agilent856xEC, 30.0, 50e9, "8565EC", "856")
register_driver("68369", "tx", drivers.Anritsu68369, 10e6, 40e9, "68369A", "683")


def _expected(spec):
    """Build the link's ExpectedAnalyzer/Source validation record from a DriverSpec."""
    return conn.ExpectedAnalyzer(spec.match, spec.freq_lo_hz, spec.freq_hi_hz,
                                 label=spec.label, family_token=spec.family)


def _link_class(kind):
    return conn.AnalyzerLink if kind == "rx" else conn.SourceLink


# ----------------------------------------------------------------- liveness probe (networked open)
# A network open_fn only proves the SOCKET connected -- a wedged NI adapter accepts the TCP
# connection while its bus is dead, so connect() would reach READY on socket-open alone. The probe
# below makes opening ALSO prove the device ANSWERS (a bounded `Z ping`, or an IDN readback against
# an older bridge), so a silent device fails the open -> the link stays out of READY and escalates
# to terminal FAULT after K consecutive failures instead of masquerading as healthy.
_PROBE_TIMEOUT_MS = 4000


def _probe_liveness(driver, probe_ms=_PROBE_TIMEOUT_MS):
    """Prove the opened driver's instrument answers on the bus. Bounds the deadline (the bridge
    wins the race -> a wedged device returns a clean framed error, never a poisoned socket), then
    prefers the bridge `Z ping` liveness verb, falling back to an IDN readback against an old
    bridge that lacks it. Raises (propagating a typed AdapterNotAnswering on ENOL) if silent."""
    t = getattr(driver, "t", None)
    prior_ms = getattr(t, "timeout_ms", None)
    if t is not None and hasattr(t, "set_timeout"):
        try:
            t.set_timeout(int(probe_ms))
        except Exception:
            pass
    try:
        if t is not None and hasattr(t, "ping"):
            try:
                t.ping()                          # `Z ping` -- one bounded liveness round-trip
                return
            except IOError:
                pass                              # old bridge (no Z verb) -> IDN-readback fallback
        driver.idn()                              # raises on a silent/wedged device
    finally:
        if t is not None and prior_ms is not None and hasattr(t, "set_timeout"):
            try:
                t.set_timeout(int(prior_ms))      # restore the campaign read timeout
            except Exception:
                pass


def _open_with_probe(open_fn):
    """Wrap a network open_fn so opening also runs the liveness probe: READY then requires the
    device to ANSWER, not merely the socket to connect. On a silent device the half-open driver is
    closed and the error propagates, so the Link records a real bus-op failure (and can FAULT)."""
    def _open(dev):
        driver = open_fn(dev)
        try:
            _probe_liveness(driver)
        except Exception:
            try:
                driver.close()
            except Exception:
                pass
            raise
        return driver
    return _open


# ----------------------------------------------------------------- live roster

@dataclass
class Unit:
    """One discovered instrument in the roster. `build_link` lazily constructs its
    auto-reconnecting Link (AnalyzerLink for rx, SourceLink for tx); the Link is cached so
    every resolve() of the same unit shares one connection."""
    kind: str
    model: str
    label: str
    address: str
    capabilities: tuple
    instance_id: str
    build_link: object                        # callable () -> Link
    _link: object = field(default=None, repr=False)

    def link(self):
        if self._link is None:
            self._link = self.build_link()
        return self._link


class ControlPlane:
    """The live roster of TX/RX units plus capability-based resolution. Build one from a
    discovery beacon list (networked) or the simulator; then resolve rx/tx handles or a
    Coordinator over any registered pair."""

    def __init__(self, cfg, span=(1e9, 6e9)):
        self.cfg = cfg
        self.span = tuple(span)
        self.units = []
        self.bench = None                     # set by simulated(); None for real hardware

    def add_unit(self, unit):
        self.units.append(unit)
        return unit

    def available(self, kind=None):
        return [u for u in self.units if kind is None or u.kind == kind]

    def available_rx(self):
        return self.available("rx")

    def available_tx(self):
        return self.available("tx")

    def resolve(self, kind=None, instance_id=None):
        """A Link for the requested unit -- by instance_id, else the first of `kind`. Returns
        None if there is no such unit."""
        if instance_id is not None:
            u = next((x for x in self.units if x.instance_id == instance_id), None)
        elif kind is not None:
            u = next(iter(self.available(kind)), None)
        else:
            u = None
        return u.link() if u is not None else None

    def roster(self):
        """A dashboard snapshot: one dict per unit (no bus contact)."""
        return [{"instance_id": u.instance_id, "kind": u.kind, "model": u.model,
                 "label": u.label, "address": u.address,
                 "capabilities": list(u.capabilities)} for u in self.units]

    def make_coordinator(self, lease_ttl_s=60.0):   # = control_lease.DEFAULT_LEASE_TTL_S
        """Resolve one rx and one tx by capability and return a Coordinator over the pair.
        Raises ControlPlaneError if either role is missing -- honest, never a fake."""
        rx, tx = self.resolve(kind="rx"), self.resolve(kind="tx")
        if rx is None or tx is None:
            raise ControlPlaneError(
                f"a coordinator needs one rx and one tx; have "
                f"{len(self.available_rx())} rx / {len(self.available_tx())} tx")
        return coordinator.Coordinator(self.cfg, rx, tx, lease_ttl_s=lease_ttl_s)


# ----------------------------------------------------------------- builders

def from_beacons(cfg, beacons, span=(1e9, 6e9), token=None, client_id=None):
    """A ControlPlane over networked bridges advertised by discovery beacons. Each advertised
    instrument is classified by the registry (unknown models skipped, honestly) and gets a
    Link that opens the concrete driver over a NetworkTransport to that bridge/pad. `client_id`
    (if given) is announced to each bridge so the device-side session registry attributes it."""
    cp = ControlPlane(cfg, span)
    for b in beacons:
        for inst in b.instruments:
            model, pad = inst.get("model", ""), int(inst.get("pad"))
            spec = resolve_driver(model)
            if spec is None:
                continue
            address = f"net:{b.host}:{b.port}:{pad}"
            dev = disc.DiscoveredDevice("net", address, model, "", (), "", model)

            def build(spec=spec, dev=dev, host=b.host, bport=b.port, pad=pad):
                return _link_class(spec.kind)(
                    expected=_expected(spec), span=span,
                    discover_fn=lambda dev=dev: [dev],
                    open_fn=_open_with_probe(
                        lambda d, spec=spec, host=host, bport=bport, pad=pad:
                        spec.driver(drivers.NetworkTransport(host, bport, pad, token=token,
                                                             client_id=client_id))))

            cp.add_unit(Unit(spec.kind, model, spec.label, address,
                             CAPABILITIES[spec.kind], f"{b.host}:{b.port}:{pad}", build))
    return cp


def from_addresses(cfg, rx_addr=None, tx_addr=None, span=(1e9, 6e9), client_id=None):
    """A ControlPlane from EXPLICIT instrument addresses (net:HOST:PORT:PAD or a VISA
    string), one per role, when no discovery beacon is running. The model is assumed to be
    the role's seeded default (8565EC rx / 68369A tx) -- the user asserts what the address
    points at. make_transport routes net: -> NetworkTransport, else -> VISA. `client_id` (if
    given) is announced to a network bridge so its session registry attributes the session."""
    cp = ControlPlane(cfg, span)
    for kind, addr in (("rx", rx_addr), ("tx", tx_addr)):
        if not addr:
            continue
        spec = default_spec(kind)
        if spec is None:
            continue
        dev = disc.DiscoveredDevice("addr", addr, spec.label, "", (), "", spec.label)

        def build(spec=spec, dev=dev, addr=addr):
            return _link_class(spec.kind)(
                expected=_expected(spec), span=span,
                discover_fn=lambda dev=dev: [dev],
                open_fn=_open_with_probe(
                    lambda d, spec=spec, addr=addr:
                    spec.driver(drivers.make_transport(addr, client_id=client_id))))

        cp.add_unit(Unit(spec.kind, spec.label, spec.label, addr,
                         CAPABILITIES[spec.kind], f"addr:{addr}", build))
    return cp


def simulated(cfg, inventory=None, span=(1e9, 6e9)):
    """A ControlPlane over the simulator: rx + tx Sim drivers SHARE one SimBench, classified
    by the registry from their model names -- the modular path exercised hardware-free."""
    inventory = inventory or disc.sim_inventory()
    bench = drivers.SimBench(separation_m=cfg.geometry.separation_m)
    drivers.install_bench_models(bench, cfg)
    cp = ControlPlane(cfg, span)
    cp.bench = bench
    for dev in inventory:
        spec = resolve_driver(dev.model)
        if spec is None:
            continue

        def build(spec=spec, dev=dev):
            open_fn = ((lambda d: drivers.SimSpectrumAnalyzer(bench)) if spec.kind == "rx"
                       else (lambda d: drivers.SimSignalGenerator(bench)))
            return _link_class(spec.kind)(
                expected=_expected(spec), span=span,
                discover_fn=lambda dev=dev: [dev], open_fn=open_fn)

        cp.add_unit(Unit(spec.kind, dev.model, spec.label, "sim",
                         CAPABILITIES[spec.kind], f"sim:{dev.model}", build))
    return cp
