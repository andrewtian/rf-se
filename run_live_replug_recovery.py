"""Live replug auto-recovery gate (#44.4) -- HARDWARE-REQUIRED; the definitive SOFT-vs-HARD proof.

The hardware-free suite proves the recovery MACHINERY: lease_diagnostics.classify_recovery (44.1, the
tier decision table), recovery.soft_recover (44.2, de-key-first budgeted QMP-replug + revalidate) and
its connection.AnalyzerLink(recover_fn=...) seam, and recovery.recover_power (44.3, the honest deferred
VBUS no-op). What NO fake can produce -- and what this script exists to obtain on the bench -- is the
PHYSICAL proof:

  * a CLEAN unplug/replug should SOFT-recover: the QMP virtual re-attach makes the adapter answer again.
  * a load-induced FX2 -110 firmware wedge should NOT recover via QMP and must land in the HARD alert
    (physical VBUS power-cycle) -- only the physical event tells the two apart.

Procedure (run at the bench, both NI adapters on SEPARATE USB controllers):

    uv run python rf-se/se299/run_live_replug_recovery.py [--role source|analyzer]

It brings up the real bridge (golden), leases + proves a live read, DE-KEYS the source (safety), then
prompts you to physically unplug and replug the chosen adapter and drives recovery.soft_recover against
the LIVE adapter via vm.attach_adapter -- reporting SOFT-recovered vs HARD (-> recover_power alert).

Exit: 0 = SOFT-recovered live; 1 = HARD (not cleared by QMP -- physical/VBUS remedy); 2 = bring-up
error; 3 = blocked (adapters share one controller -- move one, then re-run).

NOTE: production auto-recovery is NOW WIRED (2026-07-06) -- a LOCAL `se-gui --vm` owner threads the
VmSpec through control_plane into each link's recover_fn, so a mid-session wedge auto-recovers without
this script. This gate serves two purposes: (1) prove the recovery ENGINE on a real replug by driving
recovery.soft_recover directly (below), and (2) as the place to VALIDATE the wired se-gui path live --
run `uv run python rf-se/se299/cli.py se-gui --vm`, wedge/replug an adapter, and confirm the owner
auto-recovers (reconnects increments). The wiring's correctness (right spec/role/de-key) can only be
CONFIRMED with the real qemu + adapter present.
"""
import argparse
import os
import sys
from types import SimpleNamespace

SE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SE)

from gpib_bridge import vm as vmmod
import cli
import config as cfg_mod
import control_plane
import recovery


def main() -> int:
    ap = argparse.ArgumentParser(description="live replug auto-recovery gate (#44.4)")
    ap.add_argument("--role", choices=("source", "analyzer"), default="source",
                    help="which adapter to unplug/replug (source=HS/68369A, analyzer=B/8565EC)")
    ap.add_argument("--budget", type=int, default=2, help="QMP virtual-replug attempts before HARD")
    args = ap.parse_args()
    which = args.role

    devs = vmmod.detect_gpib_usb()
    warn = vmmod._shared_controller_warning(devs)
    if warn:
        print("BLOCKED: the two NI adapters share ONE USB controller -- a bring-up would wedge both.")
        print(warn)
        print("\nMove ONE adapter to a different controller, then re-run.")
        return 3

    print("bringing up the real bridge (golden) ...")
    vm_args = SimpleNamespace(vm=True, vm_mode="golden", vm_reset=True, vm_timeout=360.0,
                              vm_stagger=25.0, vm_port=5555, vm_source_port=5556,
                              vm_bind="127.0.0.1", vm_token="")
    try:
        rx_addr, tx_addr = cli._ensure_vm_addresses(vm_args)
    except BaseException as e:                                    # noqa: BLE001
        print(f"BRING-UP FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"live bridge up: RX={rx_addr}  TX={tx_addr}")

    spec = vmmod.VmSpec(port=vm_args.vm_port, source_port=vm_args.vm_source_port)
    cp = control_plane.from_addresses(cfg_mod.default(), rx_addr=rx_addr, tx_addr=tx_addr)
    coord = cp.make_coordinator()
    if not coord.ensure_ready():
        print("both links did not come READY -- cannot start the gate.")
        return 2

    link = coord.tx if which == "source" else coord.rx           # the faulted link to recover
    # SAFETY: de-key the source before ANY replug -- the source must not radiate across a re-enumeration.
    dekey = getattr(coord.source, "rf_off", lambda: None)

    def reachable():
        """LIVE liveness for the chosen role: a fresh connect that answers == READY."""
        return link.connect().state == "READY"

    input(f"\nPHYSICALLY UNPLUG the {which} adapter now, then press Enter ...")
    input(f"Now REPLUG the {which} adapter (same port), then press Enter ...")

    print(f"attempting SOFT recovery (de-key -> QMP virtual-replug x{args.budget} -> revalidate) ...")
    out = recovery.soft_recover(
        dekey_fn=dekey,
        replug_fn=lambda: vmmod.attach_adapter(spec, which),
        reachable_fn=reachable,
        budget=args.budget,
        on_event=lambda ev, n: print(f"  {ev} (attempt {n})"))

    if out.recovered:
        print(f"\nSOFT-RECOVERED live after {out.attempts} attempt(s): a clean replug is QMP-recoverable.")
        return 0
    hard = recovery.recover_power(which)
    print(f"\nHARD wedge: NOT cleared by {out.attempts} QMP virtual-replug(s). {hard.detail}.")
    print("This is the FX2 -110 case -- physically power-cycle the adapter/instrument (VBUS), then re-run.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
