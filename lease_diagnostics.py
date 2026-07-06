"""Attribute a device lock/lease acquisition failure: when the bench is waiting on or cannot take RX or
TX, say WHY and WHAT holds it, distinguishing the four real causes so the operator knows the remedy:

  bridge-down     the GPIB bridge process/VM is not accepting connections (TCP refused) -> start it
  adapter-wedged  the bridge is up (TCP open) but the GPIB board will not answer -> physically replug
  leased          another consumer holds the device lease -> named holder; stop it or wait
  board-busy      bridge up, unleased, but the board did not answer this attempt -> retry

This is the gap the live probe exposed: a wedged adapter surfaced only as a bare 'timed out', with no
attribution. The lease-conflict case was already handled (SingleConsumerConflict carries the lease table);
this adds the classification + the holder name across all failure modes.

Pure logic + a tiny TCP probe; no GUI, no driver import -- `classify()` is a decision table so it is
fully unit-testable, and `diagnose()` composes the probes so tools/GUI get one clear message.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass

BRIDGE_DOWN = "bridge-down"
ADAPTER_WEDGED = "adapter-wedged"
LEASED = "leased"
BOARD_BUSY = "board-busy"


@dataclass(frozen=True)
class AcquireDiagnosis:
    label: str            # which instrument (e.g. "8565EC analyzer (RX)")
    reason: str           # one of the constants above
    holder: str           # who holds the lease (LEASED only; else "")
    action: str           # one-line operator remedy

    def __str__(self) -> str:
        who = f" held by: {self.holder}" if self.holder else ""
        return f"{self.label}: {self.reason.upper()}{who}\n  -> {self.action}"


def classify(label: str, *, tcp_reachable: bool, bridge_responsive, holder: str = "") -> AcquireDiagnosis:
    """The decision table. `tcp_reachable`: the bridge TCP port accepts connections. `bridge_responsive`:
    a bridge-level READ-ONLY command (the lease table) returned -- True/False, or None when it was not
    probed (e.g. the transport never finished constructing). `holder`: who holds THIS device's lease
    ('' = nobody). Pure; no I/O."""
    holder = (holder or "").strip()
    if not tcp_reachable:
        return AcquireDiagnosis(label, BRIDGE_DOWN, "",
            "the GPIB bridge is not accepting connections -- start/restart the bridge VM (or the "
            "host/port is wrong).")
    if bridge_responsive is False:
        return AcquireDiagnosis(label, ADAPTER_WEDGED, "",
            f"the bridge is up but the GPIB board will not answer -- the {label} adapter firmware is "
            "wedged. Physically unplug and replug the adapter (VBUS removal), then retry. A software "
            "restart / gpib_config does NOT clear this.")
    if holder:
        return AcquireDiagnosis(label, LEASED, holder,
            f"{label} is leased by another consumer. Stop that consumer or wait for its lease to "
            "expire, then retry.")
    if bridge_responsive is None:
        # TCP open but the transaction/handshake never completed and we could not read the lease table:
        # the board is not answering -- same remedy as a confirmed wedge.
        return AcquireDiagnosis(label, ADAPTER_WEDGED, "",
            f"the bridge accepted the connection but the {label} board did not answer -- the adapter is "
            "likely wedged. Physically replug it, then retry.")
    return AcquireDiagnosis(label, BOARD_BUSY, "",
        f"the bridge is up and no rival holds the lease, but the {label} board did not answer this "
        "attempt -- retry; if it persists, replug the adapter.")


def tcp_open(host: str, port: int, timeout_s: float = 2.0) -> bool:
    """True if a TCP connection to host:port succeeds within timeout_s (the bridge process is up)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def diagnose(host: str, port: int, label: str, *, lease_report_fn=None, holder="",
             probe_tcp=tcp_open) -> AcquireDiagnosis:
    """Compose the probes into a classified diagnosis. `lease_report_fn` (optional, zero-arg -> str) reads
    the bridge lease table; if it raises/times out the board is treated as not answering (wedged). If it
    returns text and no explicit `holder` was given, the table itself is used as the holder attribution
    when non-empty. Call this whenever an acquire fails (construction timeout OR lease conflict)."""
    if not probe_tcp(host, port):
        return classify(label, tcp_reachable=False, bridge_responsive=None)
    responsive, table = None, ""
    if lease_report_fn is not None:
        try:
            table = (lease_report_fn() or "").strip()
            responsive = True
        except Exception:                                  # noqa: BLE001 -- a timeout here IS the signal
            responsive = False
    holder = holder or (table if (responsive and table and table != "(empty lease table)") else "")
    return classify(label, tcp_reachable=True, bridge_responsive=responsive, holder=holder)
