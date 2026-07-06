# se299 network GPIB bridge (the M1 path to the 8565EC)

The NI GPIB-USB-HS **cannot** be driven natively on Apple Silicon macOS (NI-488.2 has
no Apple Silicon / macOS 13+ driver; pyvisa-py's GPIB backend is Linux/Windows only).
This bridge sidesteps that: run linux-gpib on a **Linux host that physically holds the
adapter** (a UTM/QEMU VM on the Mac, or a Raspberry Pi) and reach the 8565EC from macOS
over **TCP**. The Mac needs no GPIB driver at all - only a socket.

```
  macOS (M1)                          Linux guest (UTM/QEMU VM, or a Pi)
  ----------                          ----------------------------------
  se299 cli / tests                   ni_gpib_server.py  (this folder)
  drivers.NetworkTransport  --TCP-->  linux-gpib (Gpib)  --USB-->  NI GPIB-USB-HS
  (Agilent856xEC rides on top)                                          |
                                                                      GPIB
                                                                        |
                                                                     8565EC
```

Everything on the macOS side is done and hardware-free-tested (`tests/test_net_transport.py`).
The only manual, physical steps are: create the Linux VM, **pass the NI adapter through
to it**, and run `provision.sh`.

## Pieces

| File | Runs on | Role |
|---|---|---|
| `protocol.py` | both | wire protocol: `A`/`W`/`Q`/`T`/`L`/`K`/`U`/`R`/`C` -> `+`/`=`/`!`, base64 payloads (`L/K/U/R` = lease/renew/release/report) |
| `ni_gpib_server.py` | Linux guest | thread-per-connection TCP server (both instruments, one bus); `linux-gpib` backend (real) or `--fake` (canned per pad) |
| `provision.sh` | Linux guest | build linux-gpib, load NI firmware, gpib.conf, systemd unit |
| `drivers.NetworkTransport` | macOS | client transport; drop-in for `VisaTransport` |

## Bringing the real 8565EC to the Mac

The sweeper always talks to a `net:HOST:PORT:GPIBADDR` bridge; where that bridge runs is
the only choice, and there is **no fake fallback** -- if the real bridge is not reachable
the GUI reports it and stops (a labeled `--analyzer sim` DEMO is the only simulated path).

Pick the vehicle for the USB hop (see the matrix in the top-level answer):

- **Raspberry Pi / any Linux box (reliable):** plug the NI adapter in, run `provision.sh`,
  point the Mac at `--analyzer net:<pi-ip>:5555:18`. Native USB on Linux; nothing to pass
  through. This is the rock-solid path.
- **QEMU on the Mac (USB passthrough of the NI adapter):** `gpib_bridge/vm.py` builds the
  qemu-system-aarch64 command and provisions linux-gpib + `ni_usb_gpib` in the guest. See
  the plan (with the detected adapter) via:
  ```bash
  brew install qemu
  uv run python rf-se/se299/cli.py live --vm-plan
  ```
  Key design points: pass the adapter by `usb-host,vendorid=0x3923` (matches both `0x702b`
  and the post-firmware `0x702a`, so the `ni_usb_gpib` firmware load does not drop it);
  qemu grabs it via libusb (macOS has no driver claiming it, so the capture is clean); the
  bridge folder is shared over 9p and `provision.sh` runs at first boot, provisioning the
  whole bus so BOTH the 8565EC (pad 18) and the 68369A (pad 5) ride the one bridge; the
  bridge port is hostfwd'd to the Mac's `127.0.0.1` (loopback, no token). Then launch
  SEAMLESSLY -- provisioning + boot happen as part of the launch, waiting for both units:
  ```bash
  uv run python rf-se/se299/cli.py coordinator --vm   # boots the VM, waits for BOTH, runs the campaign
  uv run python rf-se/se299/cli.py live --vm          # analyzer-only live spectrum GUI
  ```
- **Prologix GPIB-USB (native, no VM):** an FTDI serial adapter that works directly on the
  M1 -- no VM, no passthrough. (A native `PrologixTransport` is the cleanest path if you
  buy one.)

`Lima cannot pass USB through` on Apple Silicon ([Lima #2224](https://github.com/lima-vm/lima/issues/2224)),
which is why the VM path uses raw qemu, not Lima. Commercial hypervisors (Parallels 20.3+,
VMware Fusion) have their own USB redirection and are the best VM bet if raw qemu is flaky.

## 0. Self-test first (no hardware, proves the whole software path)

On the Mac, in one terminal:
```bash
uv run python rf-se/se299/gpib_bridge/ni_gpib_server.py --fake --port 5599
```
In another:
```bash
uv run python rf-se/se299/cli.py detect --analyzer net:127.0.0.1:5599:18
# -> 8565EC: DETECTED + VALID   (a real TCP round-trip to the canned 8565E)
```
This is exactly the production path with the linux-gpib backend swapped for a fake. If
this works, the only remaining variable is the VM + USB passthrough.

## 1. Create the Linux VM (UTM on Apple Silicon)

1. Install [UTM](https://mac.getutm.app/) and download an **Ubuntu Server 24.04 ARM64**
   ISO (arm64/aarch64 - not x86).
2. New VM -> **Virtualize** (not Emulate) -> Linux -> attach the ISO. Give it 2 CPU / 2 GB.
3. **USB backend must be QEMU.** UTM's Apple-Virtualization backend does not do USB
   passthrough; only the **QEMU** backend does. (This is the one gamble in this path.)
4. Install Ubuntu, enable SSH, note the guest IP (`hostname -I`).

A Raspberry Pi (or any Linux box) skips all of this - just plug the adapter into it.

## 2. Pass the NI adapter through - the 0x702b -> 0x702a gotcha (read this)

The NI GPIB-USB-HS ships **blank**: it enumerates at USB PID **0x702b**, and linux-gpib
uploads FX2 firmware that makes it **re-enumerate at 0x702a**. If you pass it through by
`VID:PID` (`0x3923:0x702b`), the passthrough **drops the instant the PID changes** and the
firmware load fails in a loop.

- **Do:** pass it through by **USB port / bus location** (UTM: the USB device menu on the
  running VM; QEMU: `-device usb-host,hostbus=N,hostport=M`) so it survives re-enumeration.
- **Or:** authorize **both** PIDs (`0x3923:0x702b` and `0x3923:0x702a`) for the guest.
- Firmware upload happens **inside the guest** (macOS has no `fxload`), so the device must
  already be attached to the guest when `provision.sh` runs step 3.

Your attached unit reports PID **0x702b** (pre-firmware) - the expected starting state.

## 3. Provision the guest

Copy this `gpib_bridge/` folder into the guest (e.g. `scp -r`), then:
```bash
# ROLE ANALYZER_PAD SOURCE_PAD ANALYZER_PORT SOURCE_PORT
sudo ./provision.sh both 18 5 5555 5556   # both boards; 18=8565EC pad, 5=68369A pad
```
The two instruments are on SEPARATE NI adapters (the 8565EC on a GPIB-USB-B, the 68369A on a
GPIB-USB-HS) => two linux-gpib boards, each exposed on its own TCP port (analyzer 5555, source
5556). `provision.sh` builds linux-gpib, installs the NI firmware + udev rule, writes an
INTERFACE-ONLY `/etc/gpib.conf` (two `ni_usb_b` boards, minors 0/1 -- NO named device stanzas;
the bridge opens each pad ad hoc via `Gpib(board, pad=N)`), loads the module, and starts the
role's bridge service(s). Each stage prints a `VERIFY` line - the kernel build and the GPIB-USB-B
firmware upload are the usual failure points; fix a failed VERIFY before moving on. Validate GPIB
independently with linux-gpib's `ibtest` (open a board, write `ID?` at pad 18; `*IDN?` at pad 5).
`--vm-mode golden` runs each board in its OWN VM instead (analyzer VM + source VM); `both` is the
single-VM path above.

## 4. Drive it from the Mac

Point any se299 verb at `net:<guest-ip>:<port>:<gpib-addr>`. The analyzer is at the analyzer
pad, the source at the source pad, BOTH on the one bridge:
```bash
uv run python rf-se/se299/cli.py detect      --analyzer net:192.168.64.5:5555:18
uv run python rf-se/se299/cli.py sweep --mode stepped --analyzer net:192.168.64.5:5555:18
# both units at once (the SE coordinator) -- two boards, two ports (analyzer 5555, source 5556):
uv run python rf-se/se299/cli.py coordinator --analyzer net:192.168.64.5:5555:18 \
                                             --source   net:192.168.64.5:5556:5
```
`net:` addresses work anywhere an analyzer/source address is accepted (they route through
`drivers.make_transport`), so `capture`, `nf-sweep`, and `q` take them too. If the bridge is token-authenticated
(any non-loopback bind), export the token on the Mac first: `export NI_GPIB_TOKEN=...` (the
client sends it automatically; it is never placed in the `net:` address, so it stays out of
run manifests).

## Security (the bridge grants instrument control -- treat the port as privileged)

The bridge lets a client send arbitrary GPIB to whatever is on the bus -- including a
signal generator that can **transmit RF**. So it is not an open service:

- **Default bind is `127.0.0.1`** (loopback). The server **refuses** a non-loopback bind
  without a token (fail-closed) unless you pass `--insecure`.
- **Token auth:** start with `--token` / `NI_GPIB_TOKEN`; clients must send it (constant-time
  compared) before any bus op. `provision.sh` generates one, stores it mode-600 in
  `/etc/ni-gpib-bridge.env`, and wires it into the systemd unit.
- **Recommended (no shared secret):** keep the loopback bind and reach it from the Mac over
  an **SSH tunnel** -- `ssh -N -L 5555:127.0.0.1:5555 user@guest`, then
  `--analyzer net:127.0.0.1:5555:18`. SSH provides the auth + encryption; nothing listens
  on any LAN interface.
- **Firewall:** restrict the port to the Mac's host-only subnet
  (`ufw allow from 192.168.64.0/24 to any port 5555 proto tcp`). Never expose it to an
  untrusted network. `--insecure` (unauthenticated non-loopback) is only for a trusted,
  isolated bench LAN.

## Performance and reliability

- **Speed:** GPIB (and the 8565EC's own settling) is the bottleneck, not the VM. A
  601-point trace is ~5 KB read in tens of ms; USB passthrough adds single-digit ms per
  GPIB op. Campaign wall-clock is unchanged (see doc 171).
- **Reliability:** the risk is USB-passthrough stalls on Apple Silicon (the host can't
  cleanly reset a captured device). se299's `AnalyzerLink` reconnects the TCP socket, which
  recovers a dropped *connection* -- but a TCP reconnect does NOT revive a **wedged guest-side
  USB device** (an FX2 stuck in fxload-limbo / -110). For that, the bring-up issues a QMP
  device_del/device_add re-attach (analyzer AND source), and if that still fails the run
  reports the specific recovery: physically unplug + re-plug the adapter into the SAME USB port
  (a VM reboot will not clear it), then re-run; power-cycle the instrument and `--vm-reset` if it
  recurs. If passthrough proves flaky on your unit, moving the adapter to a Raspberry Pi makes
  this rock-solid (same bridge, same `net:` address).
