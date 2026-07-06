#!/usr/bin/env python3
"""se299 network GPIB bridge -- runs on the Linux host (a UTM/QEMU VM on Apple Silicon,
or a Raspberry Pi) that physically holds the NI GPIB-USB-HS, and exposes the 8565EC
over TCP so a macOS client (se299 drivers.NetworkTransport) can control it WITHOUT any
local GPIB driver (NI-488.2 has no Apple Silicon support; this sidesteps it entirely).

Backends:
  linux-gpib (default): the real ``Gpib`` Python binding -> NI HS -> 8565EC.
  --fake:               a canned 8565EC so the bridge + protocol can be self-tested
                        with no hardware (also what the macOS test suite spins up).

Serves MANY clients concurrently (thread-per-connection) over one GPIB bus: a process-wide
bus mutex serializes each transaction and a VXI-11-style lease table (L/U/K/R) arbitrates
exclusive control. Protocol: gpib_bridge/protocol.py.

Run on the VM/Pi:
    python3 ni_gpib_server.py --host 0.0.0.0 --port 5555 --board 0
Self-test (no hardware):
    python3 ni_gpib_server.py --fake --port 5555
"""
from __future__ import annotations

import argparse
import hmac
import math
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocol  # noqa: E402  (sibling import so the folder is copyable to the VM)


# linux-gpib read-timeout codes (ibtmo): index -> seconds. set_timeout picks the
# smallest code whose value is >= the requested seconds.
_TMO_SECONDS = [0.0, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3,
                300e-3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0]

# One physical GPIB bus behind one adapter -> one process-wide mutex so concurrent sessions
# (thread-per-connection) never interleave a query's write-then-read at the bus level.
_BUS_LOCK = threading.Lock()


@dataclass
class Lease:
    scope: object          # "BUS" (whole bus) or an int GPIB pad (one device)
    holder: int            # owning session id
    expiry: float          # time.monotonic() deadline


def _scopes_conflict(a, b) -> bool:
    """Two lease/target scopes collide? BUS conflicts with everything; a device pad
    conflicts only with the SAME pad or a BUS lease. Two different pads never collide, so
    the analyzer (pad 18) and the source (pad 5) can be controlled independently."""
    if a == "BUS" or b == "BUS":
        return True
    return a == b


class LeaseRegistry:
    """Process-wide VXI-11-style lease table shared by every connection thread. A lease is
    EXCLUSIVE control of a scope -- one device pad or the whole BUS -- held by a single
    session for a TTL. The holder is the CONTROLLER; every other session is an OBSERVER,
    refused BUS ops (write/query) on the leased scope until the lease is released (U verb),
    lapses (TTL), or its session disconnects. With no lease held the bus is open, so this is
    backward compatible: a client that never leases behaves exactly as before.

    Pure + thread-safe; `now` is injectable so the arbitration logic is unit-testable
    without sleeping on the wall clock."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_holder = {}                          # holder(session id) -> Lease

    def _expire(self, now):
        for h in [h for h, l in self._by_holder.items() if now >= l.expiry]:
            del self._by_holder[h]

    def acquire(self, scope, holder, ttl, now=None):
        """Grant `holder` an exclusive lease on `scope` for `ttl` seconds. Returns
        (ok, reason). Refused iff a live lease on a CONFLICTING scope is held by a DIFFERENT
        session. Re-acquiring your own lease just replaces it (idempotent)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            for h, l in self._by_holder.items():
                if h != holder and _scopes_conflict(scope, l.scope):
                    return (False, f"locked by session {h} (scope {l.scope})")
            self._by_holder[holder] = Lease(scope, holder, now + max(0.0, float(ttl)))
            return (True, f"scope {scope}")

    def renew(self, holder, ttl, now=None):
        """Extend the caller's lease TTL (keepalive). (ok, reason)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            l = self._by_holder.get(holder)
            if l is None:
                return (False, "no lease held to renew")
            l.expiry = now + max(0.0, float(ttl))
            return (True, f"scope {l.scope}")

    def release(self, holder):
        with self._lock:
            self._by_holder.pop(holder, None)

    def check(self, pad, holder, now=None):
        """May `holder` do a bus op on device `pad` (or None) right now? Blocked iff a
        conflicting lease is held by another session. (ok, reason)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            for h, l in self._by_holder.items():
                if h != holder and _scopes_conflict(pad, l.scope):
                    return (False, f"pad {pad} locked by session {h} (scope {l.scope})")
            return (True, "ok")

    def scope_for(self, holder, now=None):
        """The live lease scope this session holds ('BUS' or an int pad), or None. Used to
        JOIN the session table with the lease table for the S report (which session, if any,
        is a CONTROLLER)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            l = self._by_holder.get(holder)
            return None if l is None else l.scope

    def report(self, now=None):
        """Observer-readable snapshot of live leases (one per line) -- the R verb payload."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            if not self._by_holder:
                return "no active leases"
            return "\n".join(
                f"session {l.holder} scope {l.scope} ttl {max(0.0, l.expiry - now):.1f}s"
                for l in self._by_holder.values())

    def reset(self):
        """Drop all leases (test isolation)."""
        with self._lock:
            self._by_holder.clear()


@dataclass
class Session:
    sid: int                        # session id (the same counter used for leases)
    peer: str                       # "ip:port" of the connecting client (or "-")
    client_id: str = ""             # announced identity (X verb): role|host=..|pid=..|u=..
    role: str = ""                  # role field of client_id (first '|'-segment)
    pad: object = None              # bound GPIB pad (A verb), or None until bound
    connect_time: float = 0.0       # time.monotonic() at register (for age, if wanted)


class SessionRegistry:
    """Process-wide table of EVERY connected session (not just lease holders), so a client
    can ask any device 'who is connected to you?' -- controllers AND observers alike. A
    session appears on connect (register), gains its identity on the X verb (set_client),
    its bound pad on the A verb (set_pad), and disappears on disconnect (unregister). The S
    verb joins this table with the LeaseRegistry so each session shows whether it CONTROLS a
    scope (holds a lease) or merely OBSERVES (connected/bound, no lease).

    Independent of the lease table (a session with no lease is still listed). Thread-safe;
    `now` is injectable so register/report are unit-testable without the wall clock."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_sid = {}                             # session id -> Session

    def register(self, sid, peer, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._by_sid[sid] = Session(sid=sid, peer=(peer or "-"), connect_time=now)

    def set_client(self, sid, client_id):
        """Record a session's announced identity (X verb) and parse its role out of it."""
        with self._lock:
            s = self._by_sid.get(sid)
            if s is not None:
                s.client_id = client_id or ""
                s.role = (client_id or "").split("|", 1)[0]

    def set_pad(self, sid, pad):
        with self._lock:
            s = self._by_sid.get(sid)
            if s is not None:
                s.pad = pad

    def unregister(self, sid):
        with self._lock:
            self._by_sid.pop(sid, None)

    def report(self, leases=None):
        """Observer-readable snapshot of live sessions -- the S verb payload. One field-tagged
        line per session, joined with `leases` (a LeaseRegistry) by session id so each line
        carries the scope it controls (or '-'):

            session <sid> client <cid|-> peer <ip:port|-> role <role|-> pad <pad|-> lease <scope|->

        All values are space-free tokens (the client id is space-free by construction), so the
        client can split each line on whitespace."""
        with self._lock:
            sessions = list(self._by_sid.values())
        if not sessions:
            return "no active sessions"
        lines = []
        for s in sorted(sessions, key=lambda x: x.sid):
            scope = leases.scope_for(s.sid) if leases is not None else None
            lines.append(
                f"session {s.sid} client {s.client_id or '-'} peer {s.peer or '-'} "
                f"role {s.role or '-'} pad {'-' if s.pad is None else s.pad} "
                f"lease {'-' if scope is None else scope}")
        return "\n".join(lines)

    def reset(self):
        """Drop all sessions (test isolation)."""
        with self._lock:
            self._by_sid.clear()


_LEASES = LeaseRegistry()                             # shared by all connection threads
_SESSIONS = SessionRegistry()                         # every connected session (controllers + observers)
_SESSION_LOCK = threading.Lock()
_SESSION_SEQ = [0]


def _next_session_id() -> int:
    with _SESSION_LOCK:
        _SESSION_SEQ[0] += 1
        return _SESSION_SEQ[0]


def _parse_lease_arg(payload: bytes):
    """'BUS <ttl>' or '<pad> <ttl>' -> (scope, ttl_seconds). TTL defaults to 30 s."""
    parts = payload.decode("ascii", "replace").split()
    if not parts:
        raise ValueError("lease requires 'BUS <ttl>' or '<pad> <ttl>'")
    ttl = float(parts[1]) if len(parts) > 1 else 30.0
    scope = "BUS" if parts[0].upper() == "BUS" else int(parts[0])
    return scope, ttl


def _timeout_code(ms: float) -> int:
    secs = max(0.0, float(ms) / 1000.0)
    for code in range(1, len(_TMO_SECONDS)):
        if _TMO_SECONDS[code] >= secs:
            return code
    return len(_TMO_SECONDS) - 1


# linux-gpib ibsta status bits (ib.h) and iberr codes -- defined locally so the short-read
# guard, the recover state machine, and the fault classifier are unit-testable with NO
# linux-gpib module present (the real module exposes these same values).
_IBSTA_ERR = 0x8000      # ERR : an error occurred on the last call
_IBSTA_TIMO = 0x4000     # TIMO: the operation timed out
_IBSTA_END = 0x2000      # END : EOI/EOS terminated the last read (the readback is complete)
_IBERR_EDVR = 0          # system/driver error; the OS errno rides in ibcnt (FX2/USB gone)
_IBERR_ENOL = 2          # no listener on the bus: wedged bus / stale IBGTS addressing state
_IBERR_EABO = 6          # I/O aborted (timeout): device present but not answering
_ERRNO_NAMES = {19: "ENODEV", 110: "ETIMEDOUT"}   # the EDVR ibcnt values the adapter reports


class GpibFault(Exception):
    """A GPIB op failed. Carries the terminal linux-gpib registers (iberr/ibsta/ibcnt) and a
    classification so the dispatcher can emit ONE structured '!' line + a journalled log,
    while staying backward compatible (an old client just reads it as an error string)."""

    def __init__(self, msg, iberr=0, ibsta=0, ibcnt=0, cls=None):
        super().__init__(msg)
        self.iberr = int(iberr)
        self.ibsta = int(ibsta)
        self.ibcnt = int(ibcnt)
        self.cls = cls or _classify_fault(self.iberr, self.ibsta, self.ibcnt)


@dataclass
class RecoverResult:
    """Outcome of LinuxGpibBackend.recover(): the classified terminal verdict, a human detail
    string, and the per-step (step, iberr, ibsta, ibcnt) trail (journalled by the dispatcher)."""
    verdict: str
    detail: str
    trail: list
    iberr: int
    ibsta: int
    ibcnt: int


def _read_is_short(ibsta: int, ibcnt: int) -> bool:
    """A read is SHORT/CORRUPT -- must be an error, never a '=' success -- when the END bit
    never asserted or nothing came back. The HS adapter's documented quirk hands back a
    zeroed/truncated register readback WITHOUT setting the ERR bit; iberr does not catch it,
    so this END/ibcnt check is the only guard against returning a silent wrong number."""
    if int(ibcnt) <= 0:
        return True
    return not (int(ibsta) & _IBSTA_END)


def _classify_fault(iberr: int, ibsta: int, ibcnt: int) -> str:
    """Map a terminal linux-gpib error state to an operator-facing verdict class:
        DEVICE_SILENT  -- EABO: bus + adapter fine, the instrument is not answering
        BUS_WEDGED     -- ENOL: no listener persists after ibclr + ibonl
        ADAPTER_WEDGED -- EDVR: the FX2/USB is gone (errno in ibcnt) -> power-cycle needed
        FAULT          -- any other iberr.

    CRITICAL on the live guest's linux-gpib build: gpib.iberr() is ABSENT, so _status() reports
    iberr=0 for EVERY fault. iberr==0 also == _IBERR_EDVR, so the naive iberr-only mapping collapsed
    EVERY fault to ADAPTER_WEDGED -- a merely powered-off instrument then told the operator to
    power-cycle the NI adapter (connection.py escalates ADAPTER_WEDGED to a terminal FAULT). When
    iberr is 0 we therefore DISAMBIGUATE from ibsta/ibcnt, whose bits ARE reported: a real OS errno
    riding in ibcnt is a genuine EDVR (FX2/USB gone); TIMO set is a timeout (device present, silent);
    ERR without TIMO and no errno is a no-listener (ENOL, instrument off/absent). Live-confirmed on
    the empty source bus: a no-listener write yields ibsta=0x8000 (ERR, no TIMO), ibcnt=0."""
    iberr, ibsta, ibcnt = int(iberr), int(ibsta), int(ibcnt)
    if iberr == _IBERR_EABO:
        return "DEVICE_SILENT"
    if iberr == _IBERR_ENOL:
        return "BUS_WEDGED"
    if iberr != _IBERR_EDVR:                          # a known-nonzero, non-EABO/ENOL code
        return "FAULT"
    # iberr == 0: genuine EDVR OR this build has no gpib.iberr(). Read the verdict off ibsta/ibcnt.
    if ibcnt in _ERRNO_NAMES:                         # an OS errno rode in ibcnt -> FX2/USB gone
        return "ADAPTER_WEDGED"
    if ibsta & _IBSTA_TIMO:                           # timed out -> device present but not answering
        return "DEVICE_SILENT"
    if ibsta & _IBSTA_ERR:                            # ERR w/o TIMO, no errno -> no-listener (ENOL)
        return "BUS_WEDGED"
    return "ADAPTER_WEDGED"                           # bare EDVR, no diagnostic bits -> adapter


class LinuxGpibBackend:
    """Real backend: linux-gpib ``Gpib`` binding. One device, re-bound per connection.

    Requires linux-gpib + hardware to run for real (in the VM), but the ``gpib`` module and
    the ``Gpib`` device class are INJECTABLE (``gpib=`` / ``gpib_cls=``) so the short-read
    guard, the recover state machine, and the fault classifier are unit-tested against a fake
    gpib backend with no hardware (see tests/test_ni_gpib_server.py)."""

    def __init__(self, board: int = 0, read_bytes: int = 65536, gpib=None, gpib_cls=None):
        # Default to the real linux-gpib binding; tests inject a fake module + device class.
        if gpib is None or gpib_cls is None:
            import gpib as _gpib_mod     # C ext: iberr()/ibsta()/ibcnt()/clear()/online()/serial_poll()
            import Gpib as _Gpib_mod     # Gpib(board, pad=...) device class
            gpib = _gpib_mod if gpib is None else gpib
            gpib_cls = _Gpib_mod.Gpib if gpib_cls is None else gpib_cls
        self._gpib = gpib
        self._gpib_cls = gpib_cls
        self._board = board
        self._read_bytes = read_bytes
        self._dev = None
        self.address = None                             # bound pad (for lease scoping)

    def bind(self, address: int) -> None:
        # NOTE: no per-bind interface_clear -- with thread-per-connection that would reset
        # the whole bus under another session. IFC (if needed) happens once at board init.
        self.address = int(address)
        self._dev = self._gpib_cls(self._board, pad=int(address))
        self.set_timeout(3000)                          # sane default op timeout even if no T verb

    def _status(self):
        """The linux-gpib (iberr, ibsta, ibcnt) after the last call (non-destructive reads). Fetch
        each field INDEPENDENTLY: some linux-gpib python builds omit gpib.iberr() while exposing
        ibsta()/ibcnt() (confirmed live on this guest's build). A single try/except that returns
        (0,0,0) when iberr is missing would ALSO zero the real ibsta/ibcnt, so a valid END-terminated
        read reports ibsta=0 (END unset) and _read_is_short FALSELY rejects every good reply as
        SHORT_READ -- the analyzer then never comes up though the instrument answers cleanly. Per-field
        defaulting preserves the real ibsta (END bit) + ibcnt, which is all _read_is_short needs; a
        missing iberr degrades to 0 (fault classification leans on ibsta's ERR/TIMO bits and the
        GpibError message instead)."""
        def _one(name):
            fn = getattr(self._gpib, name, None)
            if not callable(fn):
                return 0
            try:
                return int(fn())
            except Exception:
                return 0
        return (_one("iberr"), _one("ibsta"), _one("ibcnt"))

    def _fault(self, msg, exc=None):
        iberr, ibsta, ibcnt = self._status()
        text = msg if exc is None else f"{msg}: {exc}"
        return GpibFault(text, iberr=iberr, ibsta=ibsta, ibcnt=ibcnt)

    def write(self, data: bytes) -> None:
        with _BUS_LOCK:                                 # per-transaction bus mutex (safety floor)
            try:
                self._dev.write(data)
            except Exception as e:
                raise self._fault("write failed", e)

    def query(self, data: bytes) -> bytes:
        with _BUS_LOCK:                                 # write+read atomic vs other sessions
            try:
                self._dev.write(data)
                out = bytes(self._dev.read(self._read_bytes))
            except Exception as e:
                raise self._fault("query failed", e)
            iberr, ibsta, ibcnt = self._status()
            if _read_is_short(ibsta, ibcnt):            # END unset / empty -> corrupt, NOT a '='
                end = "set" if ibsta & _IBSTA_END else "unset"
                raise GpibFault(f"short/no-END read (ibcnt={ibcnt}, END={end})",
                                iberr=iberr, ibsta=ibsta, ibcnt=ibcnt, cls="SHORT_READ")
            return out

    def _probe_locked(self) -> int:
        """Serial-poll the bound device for its status byte (assumes _BUS_LOCK held).
        Returns the status byte int; raises GpibFault (classified) on failure."""
        try:
            return int(self._gpib.serial_poll(self._dev.id))
        except Exception as e:
            raise self._fault("serial poll failed", e)

    def ping(self) -> int:
        """Cheap bounded liveness read: serial-poll the bound device for its status byte."""
        with _BUS_LOCK:
            return self._probe_locked()

    def recover(self) -> RecoverResult:
        """Escalating self-heal, reading iberr/ibsta/ibcnt after EACH step, then classifying
        the terminal state: (1) ibclr, (2) ibonl offline/online (the documented fix for a
        stale IBGTS/ENOL addressing state), (3) a fresh Gpib handle, (4) a serial-poll probe."""
        with _BUS_LOCK:
            trail = []

            def _rec(step):
                iberr, ibsta, ibcnt = self._status()
                trail.append((step, iberr, ibsta, ibcnt))

            try:
                self._gpib.clear(self._dev.id)          # (1) device clear (ibclr)
            except Exception:
                pass
            _rec("ibclr")
            try:                                        # (2) offline then online (ibonl)
                self._gpib.online(self._dev.id, 0)
                self._gpib.online(self._dev.id, 1)
            except Exception:
                pass
            _rec("ibonl")
            try:                                        # (3) fresh handle
                try:
                    self._dev.close()
                except Exception:
                    pass
                self._dev = self._gpib_cls(self._board, pad=int(self.address))
            except Exception:
                pass
            _rec("reopen")
            answered, sb = False, None                  # (4) probe: does the device answer?
            try:
                sb = int(self._gpib.serial_poll(self._dev.id))
                answered = True
            except Exception:
                pass
            _rec("probe")
            iberr, ibsta, ibcnt = self._status()
            if answered:
                verdict = "OK"
                detail = f"statusbyte={sb} iberr={iberr} ibsta=0x{ibsta:04x} ibcnt={ibcnt}"
            else:
                verdict = _classify_fault(iberr, ibsta, ibcnt)
                note = ""
                if verdict == "ADAPTER_WEDGED":
                    note = " " + _ERRNO_NAMES.get(ibcnt, f"errno={ibcnt}")
                detail = f"iberr={iberr} ibsta=0x{ibsta:04x} ibcnt={ibcnt}{note}"
            return RecoverResult(verdict, detail, trail, iberr, ibsta, ibcnt)

    def set_timeout(self, ms: int) -> None:
        if self._dev is not None:
            self._dev.timeout(_timeout_code(ms))

    def close(self) -> None:
        try:
            if self._dev is not None:
                self._dev.close()
        except Exception:
            pass


class FakeBackend:
    """Hardware-free canned 8565EC -- enough of the 8560 command surface that the
    bridge, the protocol, and Agilent856xEC ride end-to-end with no instrument.

    signal selects the TRA? trace shape:
      "flat"   (default): 601 points near -90.0 dBm with a small DETERMINISTIC per-sweep
                          dither -- a healthy, TONE-LESS noise floor. Successive sweeps DIFFER
                          (a real floor is never byte-identical), so Agilent856xEC._sweep_is_live()
                          reads it as a LIVE analyzer. (A byte-identical trace models a FROZEN /
                          wedged analyzer -- which _sweep_is_live correctly rejects.)
      "moving"          : 601 points = a noise floor near -90 dBm plus a Gaussian
                          peak (~ -20 dBm, ~15 bins wide) whose CENTER BIN advances
                          every TRA? query so a real (moving) live signal sweeps
                          across the span. Deterministic (no RNG); each successive
                          sweep differs. Still exactly 601 comma-separated values."""

    _POINTS = 601
    _FLOOR_DBM = -90.0
    _PEAK_DBM = -20.0
    _PEAK_SIGMA_BINS = 15.0
    _STEP_BINS = 23                     # peak center advance per sweep (co-prime-ish)
    SOURCE_PADS = (5,)                  # GPIB pads answering as the 68369A source; else 8565EC

    def __init__(self, signal: str = "flat"):
        if signal not in ("flat", "moving"):
            raise ValueError(f"signal must be 'flat' or 'moving', got {signal!r}")
        self.address = None
        self.signal = signal
        self._last = b""
        self._sweep = 0                 # per-instance TRA? counter (a live signal moves)

    def _is_source(self) -> bool:
        """The bound pad determines which instrument this session answers as -- so one
        multiplexing server serves BOTH the 68369A (source) and the 8565EC (analyzer)."""
        return self.address in self.SOURCE_PADS

    def bind(self, address: int) -> None:
        self.address = int(address)

    def write(self, data: bytes) -> None:
        self._last = data

    def _moving_trace(self) -> bytes:
        """601 points: a -90 dBm floor + a Gaussian peak whose center bin advances
        each call, so successive sweeps differ (the peak marches across the span)."""
        center = (self._sweep * self._STEP_BINS) % self._POINTS
        self._sweep += 1
        amp = self._PEAK_DBM - self._FLOOR_DBM        # peak height above the floor (dB)
        two_sigma_sq = 2.0 * self._PEAK_SIGMA_BINS * self._PEAK_SIGMA_BINS
        vals = []
        for i in range(self._POINTS):
            d = i - center
            peak = amp * math.exp(-(d * d) / two_sigma_sq)
            ripple = 0.5 * math.sin(i * 0.1)          # tiny fixed ripple (deterministic)
            vals.append(f"{self._FLOOR_DBM + ripple + peak:.2f}")
        return ",".join(vals).encode("ascii")

    def _flat_trace(self) -> bytes:
        """601-point noise floor near -90 dBm with a small DETERMINISTIC per-sweep dither (no tone),
        so successive sweeps DIFFER -- a real healthy floor is never byte-identical, and
        Agilent856xEC._sweep_is_live() reads it as a LIVE analyzer. No RNG (resume-safe)."""
        s = self._sweep
        self._sweep += 1
        # amplitude 0.5 dB + a ~1.1 rad per-sweep phase step -> consecutive sweeps differ well above
        # the 0.05 dB _sweep_is_live threshold in >>3 points (a real noise floor varies tenths of a dB).
        vals = [f"{self._FLOOR_DBM + 0.5 * math.sin(0.7 * i + 1.1 * s):.2f}"
                for i in range(self._POINTS)]
        return ",".join(vals).encode("ascii")

    def query(self, data: bytes) -> bytes:
        d = data.decode("ascii", "replace").strip().upper()
        if "IDN" in d or d.endswith("ID?"):
            return (b"ANRITSU,68369A/NV,0,4.0" if self._is_source()
                    else b"HEWLETT-PACKARD,8565E,3702A00874,A.03.06")
        if self._is_source():
            return b"0"                 # the 68369A is write-driven; the driver only queries *IDN?
        if d.startswith("FA?"):
            return b"1000000000"
        if d.startswith("FB?"):
            return b"6000000000"
        if d.startswith("TRA?"):
            if self.signal == "moving":
                return self._moving_trace()
            return self._flat_trace()
        if d.startswith("MKA?"):
            return b"-42.5"
        if d.startswith("MKF?"):
            return b"2450000000"
        if d.startswith("DONE?"):
            return b"1"
        if d.startswith("ERR?"):
            return b"0"
        return b"0"

    def set_timeout(self, ms: int) -> None:
        pass

    def ping(self) -> int:
        """The canned instrument always answers -- status byte 0 (no faults to simulate)."""
        return 0

    def recover(self) -> RecoverResult:
        """Nothing to heal: the fake is always live -> a trivial OK verdict."""
        return RecoverResult("OK", "statusbyte=0 (fake)", [("fake", 0, 0, 0)], 0, 0, 0)

    def close(self) -> None:
        pass


def _fmt_peer(peer) -> str:
    """Normalize an accept() peer (an (ip, port) tuple, a string, or None) to 'ip:port'."""
    if peer is None:
        return "-"
    if isinstance(peer, (tuple, list)) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer)


# per-socket idle READ timeout. This bounds the DE-KEY LATENCY on a SILENT partition: the dead-man
# safe-state (RF off) fires on connection teardown, and a half-open socket is torn down only when this
# idle read times out (a lease-TTL lapse alone does NOT de-key). 45 s (was 300 s) shrinks a keyed-source
# window from ~5 min to ~45 s. Safe: idle_s fires only BETWEEN frames -- never during a bus op, which the
# bridge executes without reading the socket -- and sits above the ~20 s lease keepalive (TTL/3).
_DEFAULT_IDLE_S = 45.0
_DEFAULT_MAX_CONNS = 128       # cap on concurrent worker threads (backpressure, not refusal)
_DEFAULT_MAX_CONNS = 128       # cap on concurrent worker threads (backpressure, not refusal)

# Per-pad DEAD-MAN SAFE-STATE (de-key) commands. The bridge is instrument-agnostic, so the byte
# string sent to safe a pad is CONFIGURED PER GPIB PAD, never hardcoded to one instrument. The
# DEFAULT protects the RF SOURCE: Anritsu 68000-series RF-output-OFF is "RF0" -- the exact mnemonic
# drivers.Anritsu68369.rf_off writes -- sent to the source pad (5). So if the controlling client
# CRASHES or its link PARTITIONS, the bridge de-keys the transmitter on session teardown instead of
# leaving a 40 GHz emitter radiating. Extend/override with --safe-state PAD:CMD (repeatable).
_DEFAULT_SAFE_STATE = {5: b"RF0"}      # source pad 5 -> RF output OFF (68369A rf_off mnemonic)


def _parse_safe_state(entries, base=None):
    """Build the {pad(int): command(bytes)} safe-state map from '--safe-state PAD:CMD' CLI entries,
    layered over `base` (defaults to _DEFAULT_SAFE_STATE so the source is protected out of the box).
    An entry 'PAD:' with an EMPTY command REMOVES that pad (opt a pad out of de-keying). Raises
    ValueError on a malformed entry."""
    out = dict(_DEFAULT_SAFE_STATE if base is None else base)
    for e in entries or []:
        if ":" not in e:
            raise ValueError(f"--safe-state expects PAD:CMD (e.g. 5:RF0), got {e!r}")
        pad_s, cmd = e.split(":", 1)
        pad = int(pad_s.strip())
        if cmd == "":
            out.pop(pad, None)
        else:
            out[pad] = cmd.encode("ascii", "replace")
    return out


def _log_event(sid, pad, verb, iberr, ibsta, ibcnt, msg) -> None:
    """One structured stderr line per error / recover step (systemd captures it to the
    journal): ``sid=<n> pad=<p> verb=<v> iberr=<n> ibsta=0x<h> ibcnt=<n> msg=<text>``."""
    try:
        sys.stderr.write(
            f"sid={sid} pad={'-' if pad is None else pad} verb={verb} iberr={int(iberr)} "
            f"ibsta=0x{int(ibsta):04x} ibcnt={int(ibcnt)} msg={msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _send_safe_state(session_id, backend, controlled, safe_state, keyed=None) -> None:
    """DEAD-MAN de-key. On session teardown, send each configured per-pad SAFE-STATE command for the
    pads this session CONTROLLED (took an L lease on and did NOT cleanly release) OR KEYED (wrote to
    without a lease -- keying needs no lease, so this closes the gap where a client writes RF1 to the
    source un-leased and would otherwise slip past the leased-pad dead-man). A crashed or partitioned
    client thus cannot leave its instrument (e.g. the RF source) hot. This teardown `finally` is the
    single convergence point for a clean EOF, a dropped/half-open socket (reaped by the idle-read
    timeout), and a lapsed keepalive -- so all triggers de-key through this one path.

    A pad is de-keyed ONLY if no OTHER session now controls it (LeaseRegistry.check): if a SUCCESSOR
    controller has taken the pad over, its live source must not be disturbed. Best-effort and fully
    isolated -- a bind/write failure is logged, never raised past teardown. A BUS controller safes
    every configured pad; a device controller safes its own leased pads plus any it keyed."""
    keyed = keyed or set()
    if not safe_state:
        return
    if "BUS" in controlled:
        pads = sorted(safe_state)
    else:
        pads = sorted((set(p for p in controlled if p in safe_state)) | (set(keyed) & set(safe_state)))
    if not pads:
        return
    for pad in pads:
        cmd = safe_state.get(pad)
        if not cmd:
            continue
        ok, _why = _LEASES.check(pad, session_id)          # skip if a SUCCESSOR now controls this pad
        if not ok:
            continue
        try:
            backend.bind(pad)                              # address the pad (local; no bus-wide IFC)
            backend.write(cmd)                             # e.g. RF0 -> RF output OFF
            _log_event(session_id, pad, "SAFE", 0, 0, 0,
                       f"dead-man safe-state sent {cmd.decode('ascii', 'replace')}")
        except Exception as e:                             # noqa: BLE001 -- teardown must never raise
            _log_event(session_id, pad, "SAFE", 0, 0, 0, f"safe-state send FAILED: {e}")


def _handle_health(session_id, backend, payload):
    """The Z verb. 'ping' -> a bounded serial-poll liveness read (reply '= OK <statusbyte>';
    a fault raises GpibFault, turned into a structured '!' by the dispatcher). 'recover' ->
    escalating self-heal + a classified verdict ('= <VERDICT> <detail>'), journalling each
    escalation step. Un-arbitrated on purpose: recover is the escape hatch for a wedged bus,
    so it must run even while another session holds a lease."""
    sub = payload.decode("ascii", "replace").strip().lower()
    pad = getattr(backend, "address", None)
    if sub == "ping":
        sb = backend.ping()                            # GpibFault -> structured '!' upstream
        return protocol.encode_reply("=", f"OK {sb}".encode("ascii", "replace"))
    if sub == "recover":
        res = backend.recover()
        for step, iberr, ibsta, ibcnt in res.trail:    # journal every escalation step
            _log_event(session_id, pad, f"Z:{step}", iberr, ibsta, ibcnt, "recover step")
        _log_event(session_id, pad, "Z:verdict", res.iberr, res.ibsta, res.ibcnt, res.verdict)
        return protocol.encode_reply(
            "=", f"{res.verdict} {res.detail}".encode("ascii", "replace"))
    return protocol.encode_reply("!", b"Z requires 'ping' or 'recover'")


def serve_connection(conn, backend, token=None, session_id=None, peer=None,
                     idle_s=_DEFAULT_IDLE_S, safe_state=None) -> None:
    """Handle one client: dispatch framed requests to the backend.

    If `token` is set, the client MUST authenticate first with `H <token>` (constant-time
    compared); any non-auth verb before a successful `H` is refused and the connection
    dropped. With no token (loopback dev default) the bus is open.

    Arbitration (L/U/K/R): a session may take an EXCLUSIVE lease on its device pad or the
    whole BUS. While another session holds a conflicting lease, this session's bus ops
    (W/Q) are refused -- it is an OBSERVER. A/T stay open (bind + this-session read timeout
    are local, not bus transactions, so an observer can still connect and watch). The lease
    is released on U, on TTL expiry, or here in `finally` when the session disconnects.

    The accepted socket gets SO_KEEPALIVE + an idle read timeout so a half-open client never
    parks this thread in readline forever -- a timeout ends the session (falls through to the
    finally cleanup below, exactly like a clean disconnect)."""
    if session_id is None:
        session_id = _next_session_id()
    if safe_state is None:                             # safety by default: source pad de-keyed on teardown
        safe_state = _DEFAULT_SAFE_STATE
    controlled = set()                                 # scopes ('BUS'/pad) leased -> dead-man de-key on teardown
    keyed = set()                                       # safe-state pads WRITTEN-to (keyed) without a lease ->
    #                                                     also de-keyed on an UNCLEAN teardown. Keying needs no
    #                                                     lease (an unleased pad accepts W), so a client that
    #                                                     writes RF1 to the source without leasing would slip
    #                                                     past the leased-pad dead-man and leave it hot on crash.
    try:                                               # reap a half-open peer (TCP keepalive)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        pass                                           # e.g. AF_UNIX (socketpair) in tests
    try:
        if idle_s:
            conn.settimeout(float(idle_s))             # bound each blocking read
    except (OSError, AttributeError):
        pass
    _SESSIONS.register(session_id, _fmt_peer(peer))    # list EVERY session, not just lease holders
    f = conn.makefile("rwb")
    authed = token is None
    try:
        while True:
            try:
                line = f.readline()
            except (socket.timeout, OSError):          # idle timeout / half-open -> end session
                break
            if not line:                               # EOF: client closed
                break
            verb, payload = protocol.decode_request(line)
            try:
                if verb == "H":                          # authenticate
                    authed = token is None or hmac.compare_digest(
                        payload.decode("ascii", "replace"), token)
                    f.write(protocol.encode_reply("+" if authed else "!",
                                                  b"" if authed else b"auth failed"))
                    f.flush()
                    if not authed:
                        break
                    continue
                if verb == "":
                    continue
                if not authed:                           # fail-closed: no auth, no bus
                    f.write(protocol.encode_reply("!", b"authentication required"))
                    f.flush()
                    break
                if verb == "L":                          # acquire lease: "BUS <ttl>" | "<pad> <ttl>"
                    scope, ttl = _parse_lease_arg(payload)
                    ok, msg = _LEASES.acquire(scope, session_id, ttl)
                    if ok:                               # remember what we CONTROL -> dead-man de-key on teardown
                        controlled.add(scope)
                    reply = protocol.encode_reply("+" if ok else "!",
                                                  msg.encode("ascii", "replace"))
                elif verb == "K":                        # keepalive: renew the caller's lease
                    ttl = float(payload.decode("ascii") or "30")
                    ok, msg = _LEASES.renew(session_id, ttl)
                    reply = protocol.encode_reply("+" if ok else "!",
                                                  msg.encode("ascii", "replace"))
                elif verb == "U":                        # release the caller's lease
                    _LEASES.release(session_id)
                    controlled.clear()                   # a CLEAN handoff -> no dead-man de-key on teardown
                    keyed.clear()                        # ...including any pads keyed under this lease
                    reply = protocol.encode_reply("+")
                elif verb == "R":                        # report live leases (observer view)
                    reply = protocol.encode_reply("=",
                                                  _LEASES.report().encode("ascii", "replace"))
                elif verb == "X":                        # announce client identity (session registry)
                    _SESSIONS.set_client(session_id,
                                         payload.decode("ascii", "replace"))
                    reply = protocol.encode_reply("+")
                elif verb == "S":                        # report live sessions (observer view)
                    reply = protocol.encode_reply(
                        "=", _SESSIONS.report(_LEASES).encode("ascii", "replace"))
                elif verb == "A":                        # bind pad (local; not a bus op)
                    backend.bind(int(payload.decode("ascii")))
                    _SESSIONS.set_pad(session_id, getattr(backend, "address", None))
                    reply = protocol.encode_reply("+")
                elif verb == "T":                        # this-session read timeout (local)
                    backend.set_timeout(int(payload.decode("ascii")))
                    reply = protocol.encode_reply("+")
                elif verb in ("W", "Q"):                 # bus ops -- arbitrated by the lease
                    ok, why = _LEASES.check(getattr(backend, "address", None), session_id)
                    if not ok:
                        reply = protocol.encode_reply("!", why.encode("ascii", "replace"))
                    elif verb == "W":
                        backend.write(payload)           # raises GpibFault -> structured '!' below
                        _pad = getattr(backend, "address", None)
                        if _pad in safe_state:           # wrote to a safe-state pad (maybe keyed RF on) ->
                            keyed.add(_pad)              # arm the dead-man for it even without a lease
                        reply = protocol.encode_reply("+")
                    else:
                        reply = protocol.encode_reply("=", backend.query(payload))
                elif verb == "Z":                        # health/heal endpoint (additive; un-leased)
                    reply = _handle_health(session_id, backend, payload)
                elif verb == "C":
                    f.write(protocol.encode_reply("+"))
                    f.flush()
                    break
                else:
                    reply = protocol.encode_reply("!", f"unknown verb {verb!r}".encode())
            except GpibFault as gf:                     # structured, classified, journalled '!'
                _log_event(session_id, getattr(backend, "address", None), verb,
                           gf.iberr, gf.ibsta, gf.ibcnt, str(gf))
                reply = protocol.encode_reply(
                    "!", (f"{gf.cls} iberr={gf.iberr} ibsta=0x{gf.ibsta:04x} "
                          f"ibcnt={gf.ibcnt} {gf}").encode("ascii", "replace"))
            except Exception as e:                      # never crash the bridge on a bad op
                _log_event(session_id, getattr(backend, "address", None), verb, 0, 0, 0, str(e))
                reply = protocol.encode_reply("!", str(e).encode("ascii", "replace"))
            f.write(reply)
            f.flush()
    finally:
        # DEAD-MAN de-key BEFORE releasing the lease: if this session was the CONTROLLER of a
        # safe-state pad (e.g. the RF source) and its client crashed / partitioned, send the
        # configured safe-state command so the transmitter cannot stay keyed past teardown.
        _send_safe_state(session_id, backend, controlled, safe_state, keyed)
        _LEASES.release(session_id)                     # release lease on disconnect
        _SESSIONS.unregister(session_id)                # drop the session on disconnect
        try:
            f.close()
        except Exception:
            pass
        try:
            backend.close()
        except Exception:
            pass


def make_backend(fake: bool = False, board: int = 0, signal: str = "flat"):
    return FakeBackend(signal=signal) if fake else LinuxGpibBackend(board=board)


def listen(host: str, port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, int(port)))
    srv.listen(4)
    return srv


def _serve_and_close(conn, backend, token, peer=None, idle_s=_DEFAULT_IDLE_S, sem=None,
                     safe_state=None):
    try:
        serve_connection(conn, backend, token=token, peer=peer, idle_s=idle_s,
                         safe_state=safe_state)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if sem is not None:                               # free a worker-thread slot
            try:
                sem.release()
            except Exception:
                pass


def serve(srv: socket.socket, fake: bool = False, board: int = 0,
          one_shot: bool = False, token=None, signal: str = "flat",
          idle_s: float = _DEFAULT_IDLE_S, max_conns: int = _DEFAULT_MAX_CONNS,
          safe_state=None) -> None:
    """Accept loop: THREAD-PER-CONNECTION so multiple role instances (analyzer view, source
    control, SE coordinator) connect concurrently. Each connection is an independent session
    with its own bound pad; the shared GPIB bus is arbitrated inside the backend (a real
    LinuxGpibBackend serializes bus ops under a module lock; the fake is per-session).

    `max_conns` caps concurrent worker threads (backpressure via a bounded semaphore, not
    refusal) so a flood of half-open clients cannot exhaust the process; `idle_s` bounds each
    session's blocking reads."""
    sem = threading.BoundedSemaphore(max_conns) if max_conns and max_conns > 0 else None
    try:
        while True:
            conn, addr = srv.accept()
            if sem is not None:
                sem.acquire()                             # block until a worker slot frees
            backend = make_backend(fake=fake, board=board, signal=signal)
            t = threading.Thread(target=_serve_and_close, args=(conn, backend, token, addr),
                                 kwargs={"idle_s": idle_s, "sem": sem, "safe_state": safe_state},
                                 daemon=True)
            t.start()
            if one_shot:
                t.join()                                  # serve exactly one connection
                break
    finally:
        try:
            srv.close()
        except Exception:
            pass


def serve_forever(host: str, port: int, fake: bool = False, board: int = 0,
                  one_shot: bool = False, token=None, signal: str = "flat",
                  idle_s: float = _DEFAULT_IDLE_S, max_conns: int = _DEFAULT_MAX_CONNS,
                  safe_state=None) -> None:
    srv = listen(host, port)
    bh, bp = srv.getsockname()
    sig = f"  signal={signal}" if fake else ""
    safe = safe_state if safe_state is not None else _DEFAULT_SAFE_STATE
    safe_txt = ",".join(f"{p}:{c.decode('ascii', 'replace')}" for p, c in sorted(safe.items())) or "none"
    print(f"ni_gpib_server: listening on {bh}:{bp}  "
          f"backend={'fake' if fake else 'linux-gpib(board %d)' % board}  "
          f"auth={'token' if token else 'NONE'}  safe-state={safe_txt}{sig}", flush=True)
    serve(srv, fake=fake, board=board, one_shot=one_shot, token=token, signal=signal,
          idle_s=idle_s, max_conns=max_conns, safe_state=safe)


def _is_loopback(host: str) -> bool:
    # NOTE: "" is NOT loopback -- socket.bind(("", port)) binds INADDR_ANY (ALL interfaces),
    # exactly like "0.0.0.0". Treating it as loopback let `--host ""` skip the auth refusal below
    # and expose an UNAUTHENTICATED control service on the LAN. It must be gated like any routable bind.
    return host in ("127.0.0.1", "::1", "localhost")


def main(argv=None):
    p = argparse.ArgumentParser(description="se299 network GPIB bridge (linux-gpib -> TCP)")
    # Secure default: bind loopback only. Reach it from the Mac over an SSH tunnel
    # (ssh -N -L 5555:127.0.0.1:5555 user@guest), or bind a LAN interface WITH --token.
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--board", type=int, default=0, help="linux-gpib board index (gpib0)")
    p.add_argument("--token", default=os.environ.get("NI_GPIB_TOKEN"),
                   help="shared secret required from clients (default: env NI_GPIB_TOKEN). "
                        "Required to bind a non-loopback interface unless --insecure.")
    p.add_argument("--insecure", action="store_true",
                   help="allow a non-loopback bind with NO token (only on a trusted, "
                        "isolated network -- exposes unauthenticated instrument control)")
    p.add_argument("--fake", action="store_true", help="hardware-free canned 8565EC")
    p.add_argument("--signal", choices=("flat", "moving"), default="flat",
                   help="--fake trace shape: 'flat' static -90 dBm floor (default) or "
                        "'moving' a live signal whose peak sweeps across the span")
    p.add_argument("--one-shot", action="store_true",
                   help="serve a single connection then exit (used by tests)")
    p.add_argument("--idle-s", type=float, default=_DEFAULT_IDLE_S,
                   help="per-socket idle read timeout in seconds (reap half-open clients); "
                        "0 disables")
    p.add_argument("--max-conns", type=int, default=_DEFAULT_MAX_CONNS,
                   help="cap on concurrent worker threads (backpressure, not refusal)")
    p.add_argument("--safe-state", action="append", default=[], metavar="PAD:CMD",
                   help="per-pad DEAD-MAN safe-state command sent when a controlling client's "
                        "session ends (crash / partition / idle timeout / lapsed keepalive) so a "
                        "dead client cannot leave an instrument keyed. Repeatable. Default 5:RF0 "
                        "(RF source OFF); 'PAD:' with an empty command opts a pad out.")
    a = p.parse_args(argv)
    try:
        safe_state = _parse_safe_state(a.safe_state)
    except ValueError as e:
        p.error(str(e))
    if not _is_loopback(a.host) and not a.token and not a.insecure:
        p.error(f"refusing to bind non-loopback {a.host!r} without authentication: this "
                f"exposes an UNAUTHENTICATED instrument-control service. Set --token / "
                f"NI_GPIB_TOKEN, use the default 127.0.0.1 bind + an SSH tunnel, or pass "
                f"--insecure to override on a trusted isolated network.")
    serve_forever(a.host, a.port, fake=a.fake, board=a.board,
                  one_shot=a.one_shot, token=a.token, signal=a.signal,
                  idle_s=a.idle_s, max_conns=a.max_conns, safe_state=safe_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
