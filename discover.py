"""Cross-transport instrument discovery for the SE rig.

Enumerates whatever is connected and identifies each device (model + serial) so
the connection lifecycle (connection.py) can strict-match it to an expected role.
VISA discovery is best-effort and NEVER raises: pyvisa absent -> [] + a note, so
the whole pipeline keeps running hardware-free against sim_inventory().

PC2 (VERIFY-before-real-run): the 8565EC answers `ID?` (HP 8560 language) and may
answer `*IDN?` on newer firmware; the 68369 answers `*IDN?`. The model+serial
parse here is the primary match key -- confirm the exact response strings against
the programming manuals before trusting a real bus scan.
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    import pyvisa
except ImportError:  # pragma: no cover - pyvisa optional; sim is the default
    pyvisa = None


@dataclass
class DiscoveredDevice:
    transport: str                  # "usb" | "visa" | "sim"
    address: str                    # VISA resource string, hackrf serial, or "sim"
    model: str
    serial: str
    options: tuple = ()
    firmware: str = ""
    raw_idn: str = ""


def parse_idn(raw: str) -> dict:
    """Parse a model/serial/firmware out of an identity response.

    Handles the IEEE-488.2 4-field form 'Mfr,Model,Serial,Firmware' AND a bare
    model token (e.g. the 8565EC `ID?` reply 'HP8565E'). Unknown fields -> ''."""
    raw = (raw or "").strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 4:
        return {"model": parts[1], "serial": parts[2], "firmware": parts[3]}
    if len(parts) == 2:
        return {"model": parts[1] or parts[0], "serial": "", "firmware": ""}
    return {"model": raw, "serial": "", "firmware": ""}


def _identity_query(inst) -> str:
    """Try *IDN? then ID? (8560-language). Return the raw reply or ''."""
    for cmd in ("*IDN?", "ID?"):
        try:
            r = inst.query(cmd).strip()
            if r:
                return r
        except Exception:
            continue
    return ""


def discover_visa(rm=None) -> list:
    """Enumerate VISA resources, identify each. pyvisa absent -> [] (never raises)."""
    if pyvisa is None and rm is None:
        return []
    out = []
    try:
        rm = rm or pyvisa.ResourceManager()
        for addr in rm.list_resources():
            try:
                inst = rm.open_resource(addr)
                inst.timeout = 5000
                raw = _identity_query(inst)
                idn = parse_idn(raw)
                out.append(DiscoveredDevice(
                    transport="visa", address=addr, model=idn["model"],
                    serial=idn["serial"], firmware=idn["firmware"], raw_idn=raw))
                try:
                    inst.close()
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        return out
    return out


def identify_addr(addr: str, transport_factory) -> list:
    """Open ONE explicit VISA address, identify it. transport_factory(addr) -> a
    transport with .query()/.close(). Returns [] if it cannot be opened/identified."""
    try:
        t = transport_factory(addr)
    except Exception:
        return []
    try:
        raw = _identity_query(t)
        idn = parse_idn(raw)
        return [DiscoveredDevice(transport="visa", address=addr, model=idn["model"],
                                 serial=idn["serial"], firmware=idn["firmware"], raw_idn=raw)]
    finally:
        try:
            t.close()
        except Exception:
            pass


def sim_inventory() -> list:
    """A synthetic inventory for the hardware-free path: an 8565EC analyzer (and the
    68369 source) at address 'sim', so a sim run reports DETECTED + VALID."""
    return [
        DiscoveredDevice("sim", "sim", "HP8565EC", "SIM-SA-0001",
                         options=("001", "006", "007", "008"), firmware="sim", raw_idn="SIM,HP8565EC"),
        DiscoveredDevice("sim", "sim", "Anritsu 68369A/NV", "SIM-SG-0001",
                         options=("2B",), firmware="sim", raw_idn="SIM,68369A/NV"),
    ]
