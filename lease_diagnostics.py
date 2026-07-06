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

# ---- #44 recovery tiers (auto-recover on unplug/replug): the reshaped wedge-vs-not-wedged decision.
# The bring-up path computes these signals but does not centralize the verdict; classify_recovery() is
# the single pure home so 44.2 (soft QMP-replug) and 44.3 (hard alert) act off ONE decision, not
# scattered booleans. Ordering encodes the R6 false-positive fix: SETTLING / PRE_FIRMWARE / BOOTING_GRACE
# are all "keep waiting, do NOT declare a wedge"; only present + boards-online + past-grace + silent is a
# real wedge, and only past the QMP-replug budget is it HARD.
SETTLING = "settling"                 # present but the adapter identity has not stabilized yet
PRE_FIRMWARE = "pre-firmware"         # B adapter present but its FX2 firmware is not loaded yet
BOOTING_GRACE = "booting-grace"       # guest GPIB boards not enumerated yet -- still booting (guards R6)
ANSWERING = "answering"               # the instrument responded -- no recovery needed
INSTRUMENT_SILENT = "instrument-silent"   # present + boards online + past grace, yet silent: a real wedge


@dataclass(frozen=True)
class RecoveryVerdict:
    label: str                # which instrument (e.g. "8565EC analyzer (RX)")
    tier: str                 # one of the recovery-tier constants above (or BRIDGE_DOWN)
    soft_recoverable: bool    # True -> a QMP virtual-replug is worth trying (44.2); False -> wait or HARD
    shared_controller: bool   # orthogonal warning: the two adapters share ONE USB controller (double -110)
    action: str               # one-line operator/daemon remedy

    def __str__(self) -> str:
        warn = "\n  ! shared USB controller: a double wedge needs one adapter moved to another controller" \
            if self.shared_controller else ""
        return f"{self.label}: {self.tier.upper()}\n  -> {self.action}{warn}"


def classify_recovery(label: str, *, host_present: bool, host_settled: bool, boards_online: bool,
                      instrument_answering: bool, grace_elapsed: bool, qmp_attempts_spent: bool,
                      is_b_role: bool = False, fxloaded: bool = True,
                      shared_controller: bool = False) -> RecoveryVerdict:
    """Pure decision table for auto-recovery: given the signals the bring-up path already computes, return
    the recovery tier + whether a QMP virtual-replug is worth trying (soft_recoverable). NO I/O -- the
    caller supplies the probed booleans (44.2/44.3), so this is 100% hardware-free testable.

    Ordering (each clause assumes the ones above it are False):
      not host_present            -> BRIDGE_DOWN     (the adapter/VM is not up on the host)
      not host_settled            -> SETTLING        (identity not stable yet -- keep waiting)
      B role and not fxloaded      -> PRE_FIRMWARE    (FX2 firmware still loading -- keep waiting)
      not boards_online            -> BOOTING_GRACE   (guest boards not enumerated -- keep waiting; guards
                                                       the R6 false positive EVEN past the attempt budget)
      instrument_answering         -> ANSWERING       (it came back -- no recovery needed)
      not grace_elapsed            -> SETTLING        (silent but still within the settle grace -- wait)
      not qmp_attempts_spent       -> INSTRUMENT_SILENT, soft_recoverable=True  (try a QMP virtual-replug)
      else                         -> INSTRUMENT_SILENT, soft_recoverable=False (HARD: physical replug)
    `shared_controller` rides on any verdict as an orthogonal warning."""
    def verdict(tier: str, soft: bool, action: str) -> RecoveryVerdict:
        return RecoveryVerdict(label, tier, soft, bool(shared_controller), action)

    if not host_present:
        return verdict(BRIDGE_DOWN, False,
            f"the {label} adapter/VM is not present on the host -- (re)start the bring-up (--vm) or check "
            "the host/port.")
    if not host_settled:
        return verdict(SETTLING, False,
            f"the {label} adapter is present but its identity has not settled -- keep waiting (do NOT "
            "declare a wedge).")
    if is_b_role and not fxloaded:
        return verdict(PRE_FIRMWARE, False,
            f"the {label} adapter is present but its FX2 firmware is not loaded yet -- keep waiting for "
            "fxload (do NOT declare a wedge).")
    if not boards_online:
        return verdict(BOOTING_GRACE, False,
            f"the {label} guest GPIB boards are not enumerated yet -- still booting; keep waiting (do NOT "
            "declare a wedge, even past the attempt budget).")
    if instrument_answering:
        return verdict(ANSWERING, False, f"the {label} instrument is answering -- no recovery needed.")
    if not grace_elapsed:
        return verdict(SETTLING, False,
            f"the {label} boards are up but the instrument has not answered yet -- within the settle "
            "grace; keep waiting.")
    if not qmp_attempts_spent:
        return verdict(INSTRUMENT_SILENT, True,
            f"the {label} boards are up but the instrument is silent past the grace -- try a QMP "
            "virtual-replug (SOFT recover), then revalidate.")
    return verdict(INSTRUMENT_SILENT, False,
        f"the {label} instrument is still silent after the QMP virtual-replug budget -- HARD wedge: "
        "physically unplug and replug the adapter (VBUS removal) or power-cycle the instrument, then "
        "re-run bring-up. A QMP/USB reset does NOT clear an FX2 -110 firmware wedge.")


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
