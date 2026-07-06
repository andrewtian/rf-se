"""Session-level SOFT recovery of a wedged GPIB adapter, MID-RUN (#44.2/44.3).

Distinct from the BRING-UP recovery (vm._attach_and_verify), which runs once at start: this runs when a
link the owner ALREADY holds faults mid-campaign. It is a SUPERVISED step of the single owner's control
loop (task #27 single-active-owner + task #47 dead-man): the owner that holds the leases is the only
actor allowed to trigger a replug, so recovery can never race the operator or a rival consumer.

SAFETY (task #47): de-key the source BEFORE any replug -- the source must not radiate while an adapter
re-enumerates. All I/O is INJECTED (dekey_fn / replug_fn / reachable_fn), so the state machine is 100%
hardware-free testable. The LIVE proof -- that a REAL replug re-enumerates, and that an FX2 -110 firmware
wedge is NOT cleared by a QMP/USB reset (the SOFT-vs-HARD dichotomy) -- is HARDWARE-GATED: only a physical
unplug/replug on the bench can produce it (see run_live_replug_recovery.py, 44.4).

The activation seam is connection.AnalyzerLink(recover_fn=...): a LOCAL --vm owner injects a closure that
binds dekey_fn = source.rf_off, replug_fn = vm.attach_adapter(spec, role), reachable_fn = the link's
liveness probe, budget = wedge_after_attempts. A REMOTE net:HOST:PORT:PAD owner injects NOTHING (the
in-guest bridge cannot reach the host QMP socket) -> that role is HARD-alert only (44.3).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryOutcome:
    recovered: bool           # True -> the instrument answered again after a virtual-replug
    attempts: int             # how many replug attempts were spent
    detail: str = ""          # a HARD/unsupported note for the operator alert (44.3), else ""


def soft_recover(*, dekey_fn, replug_fn, reachable_fn, budget, on_event=None) -> RecoveryOutcome:
    """Attempt a supervised SOFT recovery of a wedged adapter mid-run: de-key the source, then QMP
    virtual-replug + revalidate up to `budget` attempts, returning as soon as the instrument answers.

    de-key runs FIRST and ALWAYS (even if budget <= 0) -- the source is never left radiating across a
    replug. Returns RecoveryOutcome(recovered, attempts): recovered=False means the budget was spent and
    the caller must escalate to the HARD alert (44.3), because a QMP/USB reset does NOT clear a real FX2
    -110 firmware wedge -- only a physical replug / VBUS power-cycle does."""
    dekey_fn()                                          # SAFETY: source de-keyed before ANY replug
    n = 0
    for n in range(1, int(budget) + 1):
        if on_event:
            on_event("replug", n)
        replug_fn()
        if reachable_fn():
            if on_event:
                on_event("recovered", n)
            return RecoveryOutcome(True, n)
    if on_event:
        on_event("exhausted", n)
    return RecoveryOutcome(False, n)


def recover_power(role: str) -> RecoveryOutcome:
    """44.3 deferred HARD-tier seam: a true VBUS power-cycle -- the ONLY thing that clears an FX2 -110
    wedge that a QMP/USB reset cannot -- needs a uhubctl-capable per-port-power USB hub that is NOT
    procured (none in the corpus). This is an HONEST no-op: it NEVER claims success. A future uhubctl
    driver (hardware) implements it. Returns a non-recovered outcome so callers ALERT the operator to
    physically replug / power-cycle rather than pretend a recovery happened."""
    return RecoveryOutcome(False, 0,
                           detail=f"VBUS power-cycle of the {role} adapter is unsupported "
                                  "(no per-port-power uhubctl USB hub attached) -- replug physically")
