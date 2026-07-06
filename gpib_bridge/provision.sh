#!/usr/bin/env bash
# Provision an Ubuntu ARM64 guest (a QEMU VM on Apple Silicon, or a Raspberry Pi) as the
# se299 GPIB bridge for BOTH instruments, each on its OWN NI USB-GPIB adapter / GPIB bus:
#   * 8565EC analyzer (RX) on an NI GPIB-USB-B  (0x702a; full-speed; needs an fxload upload)
#   * 68369A source   (TX) on an NI GPIB-USB-HS (0x709b; high-speed; ONBOARD firmware)
# Two adapters => two linux-gpib boards behind ONE VM; each board is exposed on its own TCP
# port by an ni_gpib_server instance. The macOS half is se299 drivers.NetworkTransport.
#
#   sudo ./provision.sh [ROLE] [ANALYZER_PAD] [SOURCE_PAD] [ANALYZER_PORT] [SOURCE_PORT]
#   sudo ./provision.sh both     18 5 5555 5556   # one VM, both adapters (two boards)
#   sudo ./provision.sh analyzer 18 5 5555 5556   # RX-only VM (8565EC on the GPIB-USB-B)
#   sudo ./provision.sh source   18 5 5555 5556   # TX-only VM (68369A on the GPIB-USB-HS)
#
# ROLE selects which bridge service(s) run: analyzer -> the 8565EC on ANALYZER_PORT; source ->
# the 68369A on SOURCE_PORT; both -> both. 1 instance == 1 qemu; the GOLDEN deployment is a
# separate analyzer VM + source VM (see vm.golden_pair).
#
# Run INSIDE the guest, as root, AFTER the adapter(s) for this role have been USB-passed through.
# Each stage prints a VERIFY line; the linux-gpib build + the GPIB-USB-B firmware upload are the
# usual sticking points.
set -euo pipefail

ROLE="${1:-both}"             # analyzer | source | both
ANALYZER_PAD="${2:-18}"       # 8565EC analyzer GPIB primary address (front-panel setting)
SOURCE_PAD="${3:-5}"         # 68369A source GPIB primary address
ANALYZER_PORT="${4:-5555}"    # TCP port for the analyzer bridge
SOURCE_PORT="${5:-5556}"     # TCP port for the source bridge
case "$ROLE" in analyzer|source|both) : ;; *) echo "bad ROLE '$ROLE' (analyzer|source|both)"; exit 2;; esac
WANT_ANALYZER=no; WANT_SOURCE=no
case "$ROLE" in analyzer) WANT_ANALYZER=yes;; source) WANT_SOURCE=yes;; both) WANT_ANALYZER=yes; WANT_SOURCE=yes;; esac
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD=/usr/local/src

log() { printf '\n=== %s ===\n' "$*"; }

# --------------------------------------------------------------- reboot survival
# The Ubuntu cloud image mounts /boot + /boot/efi by label; a udev label race after an
# unclean shutdown can fail that mount and drop the WHOLE boot to emergency mode (SSH never
# comes up). We never boot FROM /boot at runtime, so make those mounts non-fatal.
harden_fstab() {
    local changed=0
    if [ -f /etc/fstab ]; then
        # add nofail + a short device timeout to any /boot or /boot/efi entry lacking nofail
        if awk '$2=="/boot"||$2=="/boot/efi"{if($4 !~ /nofail/) exit 1}' /etc/fstab; then :; else
            python3 - <<'PY' && changed=1
import re
p="/etc/fstab"; L=open(p).read().splitlines(); out=[]
for ln in L:
    f=ln.split()
    if len(f)>=4 and f[1] in ("/boot","/boot/efi") and "nofail" not in f[3]:
        f[3]=f[3]+",nofail,x-systemd.device-timeout=5s"
        ln="\t".join(f)
    out.append(ln)
open(p,"w").write("\n".join(out)+"\n")
PY
        fi
    fi
    [ "$changed" = 1 ] && { systemctl daemon-reload 2>/dev/null || true; echo "VERIFY: /boot made nofail (reboot-safe)"; } || true
}
harden_fstab

# --------------------------------------------------------------- idempotent short-circuit
# If linux-gpib + the bridge units already exist (a re-provision on a later boot), just
# reconfigure the boards and restart this role's services -- skip the slow apt + build.
if python3 -c "import Gpib" >/dev/null 2>&1 && [ -f /etc/systemd/system/ni-gpib-boards.service ]; then
    log "already provisioned (role=$ROLE) -- reconfiguring boards + restarting bridge(s)"
    modprobe ni_usb_gpib 2>/dev/null || true
    # NO direct gpib_config here: a config on an adapterless board HANGS (D-state). The boards
    # service (se299_gpib_boards.sh) configs ONLY attached boards, timeout-bounded -- use it.
    systemctl restart ni-gpib-boards.service 2>/dev/null || true
    [ "$WANT_ANALYZER" = yes ] && systemctl restart ni-gpib-analyzer.service 2>/dev/null || true
    [ "$WANT_SOURCE" = yes ] && systemctl restart ni-gpib-source.service 2>/dev/null || true
    exit 0
fi

# --------------------------------------------------------------- deps
log "1. build dependencies"
apt-get update
apt-get install -y build-essential autoconf automake libtool flex bison texinfo \
    tk-dev python3 python3-dev python3-pip python3-setuptools \
    "linux-headers-$(uname -r)" libusb-1.0-0-dev fxload git usbutils
# VERIFY: both NI adapters visible to the guest. The B may show as 0x702b (pre-firmware) or
# 0x702a; the HS shows as 0x709b. If nothing shows, USB passthrough is not working.
lsusb -d 3923: || echo "WARN: no NI (0x3923) device on the guest USB bus -- fix passthrough first"

# --------------------------------------------------------------- linux-gpib (+ HS quirk patch)
log "2. linux-gpib (kernel module + userspace + Python binding)"
mkdir -p "$BUILD"; cd "$BUILD"
# SourceForge git is at /p/linux-gpib/git. Shallow clone over HTTPS ONLY (never git://: the
# git protocol is unauthenticated + unencrypted -- a supply-chain MITM risk for code we build
# as root). One HTTPS retry covers a transient blip. (Re-clone if a prior attempt was partial.)
if [ ! -d linux-gpib/linux-gpib-kernel ]; then
    rm -rf linux-gpib
    git clone --depth 1 https://git.code.sf.net/p/linux-gpib/git linux-gpib \
      || { sleep 3; git clone --depth 1 https://git.code.sf.net/p/linux-gpib/git linux-gpib; }
fi
# HS firmware quirks (recent GPIB-USB-HS 0x709b, bcdDevice 1.01): mainline linux-gpib 4.3.7
# mishandles SEVEN newer-firmware behaviors. Most visibly (quirk 3) an IBGTS after send_setup
# wipes the addressing engine (addressed writes ENOL), and the READ-path quirks (1 orphaned/
# desynchronized bulk-in pipe, 2 malformed/short status blocks, 4 garbage byte counts) make a
# board READ return a zeroed register-readback -- NIUSB_NO_BUS_ERROR / retval=-5, the 68369A
# read failure. Fix: drop in the Gersoft-lab `driver/modern` ni_usb_gpib.{c,h}, a validated
# replacement for linux-gpib 4.3.7's ni_usb that fixes all seven (addressed_transfer_lock +
# stop-and-drain pipe resync, status-block parser, byte-count clamp). HTTPS ONLY (built as root).
# If the clone/copy is unavailable OR the drop-in does not compile, fall back to the minimal
# in-place IBGTS no-op so addressed WRITES at least work. (gpib.conf already sets set-reos=no +
# set-eot=yes + master=yes, as the Gersoft docs require.) The GPIB-USB-B path is unaffected.
NIUSB_C="$(find "$BUILD/linux-gpib" -path '*ni_usb*/ni_usb_gpib.c' | head -1)"
NIUSB_DIR="$(dirname "$NIUSB_C" 2>/dev/null)"
apply_ibgts_fallback() {
    [ -f "$NIUSB_DIR/ni_usb_gpib.c" ] || { echo "PATCH-WARN: ni_usb_gpib.c not found"; return; }
    python3 - "$NIUSB_DIR/ni_usb_gpib.c" <<'PY'
import sys, re
p = sys.argv[1]; s = open(p).read()
if "se299 IBGTS neutralized" in s:
    print("PATCH: ni_usb_go_to_standby already neutralized"); sys.exit(0)
m = re.search(r'ni_usb_go_to_standby\s*\([^;{)]*\)\s*\{', s)
if not m:
    print("PATCH-WARN: ni_usb_go_to_standby definition not found -- HS addressed writes may ENOL")
    sys.exit(0)
ins = m.end()
s = s[:ins] + "\n\treturn 0; /* se299 IBGTS neutralized (fallback) */\n" + s[ins:]
open(p, "w").write(s)
print("PATCH: ni_usb_go_to_standby neutralized (IBGTS fallback)")
PY
}
USED_GERSOFT=0
if [ -n "$NIUSB_DIR" ] && [ -d "$NIUSB_DIR" ]; then
    cp -f "$NIUSB_DIR/ni_usb_gpib.c" "$NIUSB_DIR/ni_usb_gpib.c.orig"
    cp -f "$NIUSB_DIR/ni_usb_gpib.h" "$NIUSB_DIR/ni_usb_gpib.h.orig"
    rm -rf ni-gpib-hs
    git clone --depth 1 https://github.com/Gersoft-lab/NI-GPIB-USB-HS ni-gpib-hs \
      || { sleep 3; git clone --depth 1 https://github.com/Gersoft-lab/NI-GPIB-USB-HS ni-gpib-hs; } || true
    if [ -f ni-gpib-hs/driver/modern/ni_usb_gpib.c ] && [ -f ni-gpib-hs/driver/modern/ni_usb_gpib.h ]; then
        cp -f ni-gpib-hs/driver/modern/ni_usb_gpib.c "$NIUSB_DIR/ni_usb_gpib.c"
        cp -f ni-gpib-hs/driver/modern/ni_usb_gpib.h "$NIUSB_DIR/ni_usb_gpib.h"
        # Compat: the Gersoft "modern" driver uses Linux 6.16+ idioms (timer_container_of,
        # kzalloc_obj, kmalloc_objs); shim them (#ifndef-guarded, so a newer kernel wins) so it
        # ALSO builds on the 6.8 (Ubuntu 24.04) guest. VALIDATED: compiles + the 683xx read works.
        python3 - "$NIUSB_DIR/ni_usb_gpib.c" <<'PY'
import sys, re
p = sys.argv[1]; s = open(p).read()
MARK = "se299 kernel-API compat"
if MARK not in s:
    shim = ("\n/* " + MARK + ": Gersoft modern targets Linux 6.16+; shim newer idioms for 6.8. */\n"
            "#ifndef timer_container_of\n"
            "#define timer_container_of(var, cb_timer, field) from_timer(var, cb_timer, field)\n#endif\n"
            "#ifndef kzalloc_obj\n#define kzalloc_obj(T) kzalloc(sizeof(T), GFP_KERNEL)\n#endif\n"
            "#ifndef kmalloc_objs\n#define kmalloc_objs(obj, n) kmalloc_array((n), sizeof(obj), GFP_KERNEL)\n#endif\n")
    m = re.search(r'#include\s+"ni_usb_gpib\.h".*\n', s)
    idx = m.end() if m else 0
    open(p, "w").write(s[:idx] + shim + s[idx:])
    print("DRIVER: applied <6.16 kernel compat shim to Gersoft ni_usb_gpib.c")
PY
        USED_GERSOFT=1
        echo "DRIVER: Gersoft-lab modern ni_usb_gpib.{c,h} dropped in (fixes all 7 HS firmware quirks)"
    else
        echo "DRIVER-WARN: Gersoft modern files unavailable -- applying IBGTS fallback"
        apply_ibgts_fallback
    fi
else
    echo "DRIVER-WARN: linux-gpib ni_usb dir not found -- cannot apply HS quirk fix"
fi
cd linux-gpib/linux-gpib-kernel
if ! make; then
    if [ "$USED_GERSOFT" = 1 ] && [ -f "$NIUSB_DIR/ni_usb_gpib.c.orig" ]; then
        echo "DRIVER-WARN: Gersoft driver did not compile -- reverting to stock ni_usb + IBGTS fallback"
        cp -f "$NIUSB_DIR/ni_usb_gpib.c.orig" "$NIUSB_DIR/ni_usb_gpib.c"
        cp -f "$NIUSB_DIR/ni_usb_gpib.h.orig" "$NIUSB_DIR/ni_usb_gpib.h"
        apply_ibgts_fallback
        make
    else
        echo "DRIVER-ERROR: linux-gpib kernel module build failed"; exit 1
    fi
fi
make install
depmod -a
cd ../linux-gpib-user
./bootstrap
./configure --sysconfdir=/etc     # put gpib.conf in /etc
make
make install
ldconfig
if [ -d language/python ]; then
    ( cd language/python && python3 setup.py install )
fi
modinfo ni_usb_gpib >/dev/null && echo "VERIFY ni_usb_gpib module: OK"
python3 -c "import Gpib; print('VERIFY Python Gpib import: OK')"

# --------------------------------------------------------------- NI GPIB-USB-B firmware (fxload)
log "3. NI GPIB-USB-B firmware (fxload) -- the 8565EC adapter only"
# A blank GPIB-USB-B enumerates at 0x702b; linux-gpib's udev rule runs fxload to upload the FX2
# firmware, after which it re-enumerates at 0x702a and attaches as a board. The GPIB-USB-HS
# (0x709b) has ONBOARD firmware -- no upload. Firmware images come from fmhess's repo (HTTPS)
# and go where the fxloader looks: /usr/local/share/usb/<device>/. BEST-EFFORT + non-fatal.
apt-get install -y fxload || true
cd "$BUILD"
[ -d linux_gpib_firmware ] || git clone --depth 1 \
    https://github.com/fmhess/linux_gpib_firmware.git 2>/dev/null || true
if [ -d linux_gpib_firmware ]; then
    for sub in ni_gpib_usb_b ni_usb_gpib; do
        if [ -d "linux_gpib_firmware/$sub" ]; then
            install -d "/usr/local/share/usb/$sub"
            cp -v linux_gpib_firmware/$sub/*.hex "/usr/local/share/usb/$sub/" 2>/dev/null || true
        fi
    done
fi
# CRITICAL: the udev auto-fxloader (gpib_udev_fxloader) looks for the NI GPIB-USB-B firmware under
# /usr/local/share/usb/ni_usb_gpib/, but fmhess's repo ships it ONLY as ni_gpib_usb_b/. Without
# mirroring it, the DEVICE-ADD auto-fxload fails ("missing firmware .../ni_usb_gpib/...") and the
# adapter stays stuck at 0x702b after any power-cycle / re-plug (the one-time manual fxload below
# masks this at first provision but NOT on re-enumeration). Mirror into the fxloader's path.
if [ -f /usr/local/share/usb/ni_gpib_usb_b/niusbb_firmware.hex ]; then
    install -d /usr/local/share/usb/ni_usb_gpib
    cp -f /usr/local/share/usb/ni_gpib_usb_b/*.hex /usr/local/share/usb/ni_usb_gpib/ 2>/dev/null || true
fi
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true
sleep 2
# DETERMINISTIC manual fxload if the B is still blank at 0x702b (the udev auto-load is
# path/version-sensitive). After a successful load it re-enumerates 0x702b -> 0x702a; qemu
# passes the B by vendor id so the re-enumeration does not drop the passthrough.
FWB=/usr/local/share/usb/ni_gpib_usb_b
if lsusb -d 3923:702b >/dev/null 2>&1 && [ -f "$FWB/niusbb_firmware.hex" ]; then
    BUSDEV=$(lsusb -d 3923:702b | sed -nE 's#^Bus ([0-9]+) Device ([0-9]+).*#/dev/bus/usb/\1/\2#p' | head -1)
    echo "GPIB-USB-B blank at 0x702b; fxload -> ${BUSDEV}"
    fxload -t fx -D "$BUSDEV" -I "$FWB/niusbb_firmware.hex" -s "$FWB/niusbb_loader.hex" \
        && { echo "fxload OK; waiting for re-enumeration to 0x702a..."; sleep 4; } \
        || echo "WARN: fxload failed (check firmware files / device path)"
fi
echo "current NI USB state:"; lsusb -d 3923: || true
echo "VERIFY: 'lsusb -d 3923:' should show 0x702a (the B) AND 0x709b (the HS)."

# --------------------------------------------------------------- gpib.conf (TWO boards)
log "4. /etc/gpib.conf -- two boards (minor 0 + minor 1), both ni_usb_b"
# TWO adapters => two linux-gpib boards. Board numbering is probe-order dependent, so which
# minor is the analyzer vs the source is decided at bridge-start (bridge_launch.sh probes each
# board for the target pad). master=yes: be system controller (else ibsic/clear -> "not the
# system controller"). set-reos=no + set-eot=yes: recommended for the GPIB-USB-HS firmware
# (set-eot works around its EOI/write-completion quirk). Interface stanzas only -- the bridge
# opens each pad ad hoc via Gpib(board, pad=N), so no named device stanzas are needed.
cat > /etc/gpib.conf <<EOF
interface {
        minor        = 0
        board_type   = "ni_usb_b"
        name         = "gpib0"
        pad          = 0
        sad          = 0
        timeout      = T3s
        master       = yes
        set-reos     = no
        set-eot      = yes
}
interface {
        minor        = 1
        board_type   = "ni_usb_b"
        name         = "gpib1"
        pad          = 0
        sad          = 0
        timeout      = T3s
        master       = yes
        set-reos     = no
        set-eot      = yes
}
EOF
modprobe ni_usb_gpib || true
sleep 2
# Config ONLY boards with an attached adapter -- gpib_config on an adapterless board HANGS the
# driver (uninterruptible), which wedged an earlier boot. Timeout-bounded as a second guard.
# SINGLETON: the VM boots with EMPTY USB controllers and the adapters are HOT-PLUGGED post-boot,
# so at provision time dmesg has NO 'attached to gpib' lines -> the grep pipe returns 1. Under
# `set -euo pipefail` that non-zero would kill provision.sh here (before the bridge service starts
# or the readiness marker is written), which is exactly the singleton false-720s-timeout. `|| true`
# makes an empty result non-fatal; the boards are configured later on hot-plug by ni-gpib-boards.
ATTACHED=$(dmesg 2>/dev/null | grep -oE 'attached to gpib[0-9]+' | grep -oE '[0-9]+$' | sort -un || true)
echo "attached gpib boards: ${ATTACHED:-none}"
for b in ${ATTACHED:-}; do
    timeout 20 gpib_config --minor "$b" && echo "board $b online" \
        || echo "WARN: gpib_config --minor $b failed/timed out"
done
echo "VERIFY: attached boards online; the analyzer (pad ${ANALYZER_PAD}) and source (pad ${SOURCE_PAD}) are reached by pad."

# --------------------------------------------------------------- bridge launcher (self-mapping)
log "5. bridge launcher + two systemd services"
# bridge_launch.sh finds WHICH board carries a target pad (probe-order is non-deterministic)
# and execs ni_gpib_server on it. A bounded write-probe: an absent pad fast-fails ENOL, so the
# scan never hangs on an empty board; a wedged board is time-boxed away.
LAUNCH=/usr/local/sbin/se299_bridge_launch.sh
cat > "$LAUNCH" <<'LAUNCHEOF'
#!/usr/bin/env bash
# se299_bridge_launch.sh TARGET_PAD PORT [--insecure]
# Discover the linux-gpib board carrying TARGET_PAD, then exec the bridge on it. The analyzer and
# source bridges share the SAME GPIB boards, so discovery runs under an EXCLUSIVE flock and each
# bridge CLAIMS the board carrying its pad. Without this the two bridges concurrently probe (and,
# formerly, re-configured) the shared boards, thrashing the fragile GPIB-USB-HS -> intermittent
# "pad not found" races + wedged reads. Boards are configured ONCE by ni-gpib-boards.service;
# this launcher does NOT gpib_config them (a re-config resets board state out from under an
# already-running peer bridge). Only a bounded write-probe, serialized.
set -uo pipefail
TARGET_PAD="$1"; PORT="$2"; EXTRA="${3:-}"
modprobe ni_usb_gpib 2>/dev/null || true
CLAIMDIR=/run/se299; mkdir -p "$CLAIMDIR"
GC="$(command -v gpib_config || echo /usr/local/sbin/gpib_config)"
CANDIDATES="0 1"        # the minors defined in /etc/gpib.conf (two NI adapters -> two boards)
# count of BINDABLE NI adapters on the USB bus (0x702a = fxloaded B, 0x709b = HS) vs boards already
# attached. A gpib_config on a minor with NO unbound adapter D-states (uninterruptible); a config
# is therefore issued ONLY while an unbound adapter still exists.
attached_boards() { dmesg 2>/dev/null | grep -oE 'attached to gpib[0-9]+' | grep -oE '[0-9]+$' | sort -un; }
board_attached()  { attached_boards | grep -qx "$1"; }
FOUND=""
# serialize discovery across both bridges (they must never probe the shared boards at once).
# -w 45: never block forever -- if the peer's discovery wedged, proceed rather than hang the unit
# (Restart=on-failure then retries). Losing the lock is safe: the claim files still de-conflict.
exec 9>"$CLAIMDIR/discovery.lock"
flock -w 45 9 || echo "se299_bridge_launch: lock wait timed out, proceeding (claims de-conflict)" >&2
for b in $CANDIDATES; do
    # skip a board another bridge already claimed for a DIFFERENT pad (never touch its board)
    CLAIM="$CLAIMDIR/board${b}.pad"
    if [ -f "$CLAIM" ] && [ "$(cat "$CLAIM" 2>/dev/null || true)" != "$TARGET_PAD" ]; then
        continue
    fi
    # bring THIS board online if it is not attached yet -- e.g. the GPIB-USB-B, which qemu only
    # exposes once ensure_bridge QMP-re-attaches it post-fxload, so the boot-time boards oneshot
    # never configured it. Config ONLY while an UNBOUND adapter exists (#adapters > #attached),
    # else the config would D-state on an adapterless minor. We hold the lock + the board is
    # unclaimed, so this never disturbs a board a peer is serving.
    if ! board_attached "$b"; then
        NADAPT=$(lsusb 2>/dev/null | grep -icE '3923:(702a|709b)')
        NATTACHED=$(attached_boards | grep -c .)
        if [ "${NADAPT:-0}" -gt "${NATTACHED:-0}" ]; then
            timeout 20 "$GC" --minor "$b" 2>/dev/null || true
        else
            continue        # no unbound adapter for this minor -> configuring it would hang
        fi
    fi
    [ -e "/dev/gpib${b}" ] || continue
    # bounded (timeout) write-probe: present pad accepts the write, absent pad ENOLs fast.
    if timeout 8 python3 - "$b" "$TARGET_PAD" <<'PY'
import sys, gpib
board=int(sys.argv[1]); pad=int(sys.argv[2])
try:
    h=gpib.dev(board,pad); gpib.timeout(h,9)   # ~100ms
    gpib.write(h,b"*CLS\n")                     # present -> ok; absent -> ENOL (raises)
    print("FOUND"); sys.exit(0)
except Exception:
    sys.exit(1)
PY
    then FOUND="$b"; echo "$TARGET_PAD" > "$CLAIM"; break; fi
done
flock -u 9
if [ -z "$FOUND" ]; then
    echo "se299_bridge_launch: pad ${TARGET_PAD} not found on any board -- instrument off/uncabled?" >&2
    exit 1
fi
echo "se299_bridge_launch: pad ${TARGET_PAD} is on gpib${FOUND}; starting bridge on :${PORT}"
exec /usr/bin/python3 "SRC_DIR_PLACEHOLDER/ni_gpib_server.py" \
     --host "BIND_HOST_PLACEHOLDER" --port "$PORT" --board "$FOUND" $EXTRA
LAUNCHEOF
sed -i "s#SRC_DIR_PLACEHOLDER#${SRC_DIR}#g; s#BIND_HOST_PLACEHOLDER#${BIND_HOST:-0.0.0.0}#g" "$LAUNCH"
chmod 755 "$LAUNCH"

# Security note: this VM binds 0.0.0.0 --insecure (no token) ONLY because qemu user-mode NAT
# forwards the host's 127.0.0.1 hostfwd to the guest eth0 IP (not loopback) and the guest has
# NO LAN presence -- so only the host's loopback hostfwd can reach these ports. On a Pi / LAN,
# bind 127.0.0.1 + SSH-tunnel, or set a token (the server fails closed off loopback otherwise).
SERVER_EXTRA=""
if [ "${INSECURE:-no}" = yes ]; then SERVER_EXTRA="--insecure"; fi

# oneshot: bring online ONLY the boards that actually have an adapter attached (a
# gpib_config on a board with NO attached USB adapter HANGS the driver, wedging boot). The
# attached-board list comes from the ni_usb_gpib "attached to gpibN" kernel log; each config
# is timeout-bounded as a secondary guard. Installed to /usr/local/sbin/se299_gpib_boards.sh.
BOARDS=/usr/local/sbin/se299_gpib_boards.sh
cat > "$BOARDS" <<'BEOF'
#!/bin/sh
# bring online only ni_usb_gpib boards that have an attached adapter
modprobe ni_usb_gpib 2>/dev/null || true
sleep 2
GC="$(command -v gpib_config || echo /usr/local/sbin/gpib_config)"
BRDS=$(dmesg 2>/dev/null | grep -oE 'attached to gpib[0-9]+' | grep -oE '[0-9]+$' | sort -un)
[ -n "$BRDS" ] || BRDS=0
for b in $BRDS; do
    echo "se299_gpib_boards: gpib_config --minor $b"
    timeout 20 "$GC" --minor "$b" || echo "se299_gpib_boards: WARN minor $b config failed"
done
BEOF
chmod 755 "$BOARDS"
cat > /etc/systemd/system/ni-gpib-boards.service <<EOF
[Unit]
Description=se299 GPIB boards online (only boards with an attached adapter)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${BOARDS}

[Install]
WantedBy=multi-user.target
EOF

# analyzer bridge (8565EC): self-maps its board, serves ANALYZER_PORT. (role analyzer|both)
if [ "$WANT_ANALYZER" = yes ]; then
cat > /etc/systemd/system/ni-gpib-analyzer.service <<EOF
[Unit]
Description=se299 GPIB bridge -- 8565EC analyzer (pad ${ANALYZER_PAD}) on :${ANALYZER_PORT}
After=ni-gpib-boards.service
Wants=ni-gpib-boards.service

[Service]
ExecStart=${LAUNCH} ${ANALYZER_PAD} ${ANALYZER_PORT} ${SERVER_EXTRA}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

# source bridge (68369A): self-maps its board, serves SOURCE_PORT. (role source|both)
if [ "$WANT_SOURCE" = yes ]; then
cat > /etc/systemd/system/ni-gpib-source.service <<EOF
[Unit]
Description=se299 GPIB bridge -- 68369A source (pad ${SOURCE_PAD}) on :${SOURCE_PORT}
After=ni-gpib-boards.service
Wants=ni-gpib-boards.service

[Service]
ExecStart=${LAUNCH} ${SOURCE_PAD} ${SOURCE_PORT} ${SERVER_EXTRA}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
systemctl enable --now ni-gpib-boards.service
[ "$WANT_ANALYZER" = yes ] && systemctl enable --now ni-gpib-analyzer.service || true
[ "$WANT_SOURCE" = yes ] && systemctl enable --now ni-gpib-source.service || true
systemctl --no-pager status ni-gpib-analyzer.service ni-gpib-source.service 2>/dev/null | head -20 || true

# READINESS MARKER (singleton gate): the LAST thing provision.sh does. await_guest_provisioned on
# the Mac polls /run/se299/provisioned over the SSH hostfwd (:2222) BEFORE it hot-plugs adapters --
# the B's fxload udev rule + ni_usb_gpib exist ONLY after this ran, so attaching earlier races
# provisioning. /run is tmpfs (cleared on reboot), so a warm reboot re-runs provision.sh (the
# already-provisioned fast path) and re-touches the marker. The bridge services already tolerate a
# post-boot USB hot-plug (ExecStartPre modprobe + gpib_config on every start).
mkdir -p /run/se299 && touch /run/se299/provisioned
echo "  readiness marker written: /run/se299/provisioned"

log "DONE -- role=$ROLE"
[ "$WANT_ANALYZER" = yes ] && echo "  analyzer (8565EC): 127.0.0.1:${ANALYZER_PORT} pad ${ANALYZER_PAD}"
[ "$WANT_SOURCE" = yes ] && echo "  source   (68369A): 127.0.0.1:${SOURCE_PORT} pad ${SOURCE_PAD}"
echo "  From the Mac, coordinator wiring (qemu forwards each port to 127.0.0.1):"
if [ "$ROLE" = both ]; then
  echo "  uv run python rf-se/se299/cli.py coordinator \\"
  echo "     --analyzer net:127.0.0.1:${ANALYZER_PORT}:${ANALYZER_PAD} \\"
  echo "     --source   net:127.0.0.1:${SOURCE_PORT}:${SOURCE_PAD}"
fi
