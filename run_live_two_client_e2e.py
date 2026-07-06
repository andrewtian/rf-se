"""Live two-client E2E launcher -- the one command that completes the live proof.

If the two NI GPIB-USB adapters are on SEPARATE host USB controllers, this brings up the real
bridge (singleton VM) and runs tests/test_e2e_two_client_local.py against the LIVE 8565EC + 68369A
(SE299_LIVE_RX / SE299_LIVE_TX pointed at the real bridge) -- no fakes.

If the two adapters still share ONE controller (hostbus 0), it does NOT attempt a bring-up (that
resets the shared controller and wedges both adapters -- proven across singleton / golden /
single-adapter topologies). It prints the required physical move and exits 3.

    uv run python rf-se/se299/run_live_two_client_e2e.py

Exit: 0 = live E2E passed; 1 = live E2E ran but failed; 2 = bring-up error; 3 = blocked (adapters
still share a controller -- move one to a different controller, then re-run).
"""
import os
import subprocess
import sys
from types import SimpleNamespace

SE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SE)

from gpib_bridge import vm as vmmod
import cli


def main() -> int:
    devs = vmmod.detect_gpib_usb()
    warn = vmmod._shared_controller_warning(devs)
    if warn:
        print("BLOCKED: the two NI adapters still share ONE USB controller -- a bridge bring-up would")
        print("wedge both (confirmed across singleton / golden / single-adapter). Physical move needed:")
        print(warn)
        print("\nMove ONE adapter to a USB port on a DIFFERENT controller (a powered hub on a separate")
        print("root port), then re-run this script -- it will bring the bridge up and run the LIVE E2E.")
        return 3

    print("adapters are on SEPARATE controllers -- bringing up the real bridge (singleton) ...")
    # clean any stale qemu first (health-aware: skips a healthy bridge)
    subprocess.run([sys.executable, os.path.join(SE, "cli.py"), "vm-stop"], check=False)
    # golden (two VMs, at-boot passthrough) not singleton (post-boot QMP hot-plug): the ni_usb_gpib
    # driver desyncs on a device that appears mid-flight ("unexpected data" + URB timeout), so the
    # adapter must be present when the guest boots. golden ran the bench Jul 1-2 for this reason.
    # 360s per-unit timeout: with the premature-wedge false positive fixed (the readiness poll now
    # waits for the guest to reach its bridge-launch stage before any ADAPTER_WEDGED verdict), the
    # per-unit timeout is the binding gate -- give a FRESH provision (compiles ni_usb_gpib from
    # source) real margin. A genuinely wedged adapter still fails fast after provisioning + grace.
    args = SimpleNamespace(vm=True, vm_mode="golden", vm_reset=True, vm_timeout=360.0,
                           vm_stagger=25.0, vm_port=5555, vm_source_port=5556,
                           vm_bind="127.0.0.1", vm_token="")
    try:
        rx_addr, tx_addr = cli._ensure_vm_addresses(args)
    except BaseException as e:                                    # noqa: BLE001
        print(f"BRING-UP FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"live bridge up: RX={rx_addr}  TX={tx_addr}")

    env = dict(os.environ, SE299_LIVE_RX=rx_addr, SE299_LIVE_TX=tx_addr,
               QT_QPA_PLATFORM="offscreen")
    print("running the two-client E2E against the LIVE 8565EC + 68369A ...")
    rc = subprocess.run(
        [sys.executable, "-m", "pytest",
         os.path.join(SE, "tests", "test_e2e_two_client_local.py"), "-v"],
        env=env).returncode
    print("\nLIVE E2E: " + ("PASSED -- two-client networked operation works on real equipment"
                            if rc == 0 else "FAILED (see output above)"))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
