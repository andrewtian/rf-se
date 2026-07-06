"""Instrument drivers for the SE substitution rig, plus a hardware-free simulator.

The no-cable method needs TWO self-contained instruments (doc 147 sec 4.1):
  - SignalGenerator   : Anritsu 68369A/NV, OUTSIDE, drives the TX horn.
  - SpectrumAnalyzer  : HP/Agilent 8564EC/8565EC, INSIDE (controlled over fiber,
                        PC3), reads the RX horn.

Design: the loop depends on the ABSTRACT interfaces (SignalGenerator /
SpectrumAnalyzer). A real driver emits instrument command strings over VISA; a
Sim* driver implements the physics against a shared SimBench. So the loop is
hardware-agnostic and runs today with addr="sim" (no pyvisa, no hardware).

PC2: the 856x mnemonics are CONFIRMED against the HP 8560 E-Series User's Guide (HP
08560-90146, distilled in reference/operator-manuals/hp-8560-e-series-programming.md),
with the Agilent/HP 8560 E-Series and EC-Series Spectrum Analyzers User's Guide
(08560-90158, distilled in reference/operator-manuals/agilent-8560e-users-guide.md) as
the PRIMARY cite for the EC-series unit actually on the bench. The 68369/68367C
mnemonics are CONFIRMED against the Anritsu 682XXB/683XXB Operation Manual (P/N
10370-10284), cross-checked to the MG369xB GPIB Programming Manual (P/N 10370-10366 --
identical 68000-series native language), both distilled in
reference/operator-manuals/anritsu-68000-series-operation.md, and live-verified via
native OF1/OL1/OSB readback on the bench 68367C. The simulator does not depend on any
of these strings.
"""
from __future__ import annotations

import math
import os
import random
import re
import socket
import struct
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Callable, Optional

import device_state                             # canonical ABSOLUTE-state records (pure, no hardware)

try:
    import pyvisa
except ImportError:  # pragma: no cover - pyvisa optional; sim is the default
    pyvisa = None

try:
    from gpib_bridge import protocol as _bridge  # network GPIB bridge wire protocol
except Exception:  # pragma: no cover - only if the se299 dir isn't on sys.path
    _bridge = None

# per-process client identity (announced to bridges via the X verb); re-exported so call
# sites use drivers.client_id(role=...) / drivers.set_client_role(...) alongside the transports.
from identity import client_id, set_client_role  # noqa: F401

from budget import reference_amp_dbm, noise_floor_dbm, db_power_sum


def _fit_linear(xs, ys):
    """Least-squares y = a*x + b over paired samples; returns (a, b, rms_residual) or None if x has no
    spread (a flat trace can't fit a slope). Used to self-calibrate the binary trace's measurement-units
    -> dBm map from a paired ASCII+binary read of the same sweep."""
    n = len(xs)
    if n == 0:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    a = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / sxx
    b = my - a * mx
    rms = (sum((ys[i] - (a * xs[i] + b)) ** 2 for i in range(n)) / n) ** 0.5
    return (a, b, rms)


# ============================================================== VISA transport

class VisaTransport:  # pragma: no cover - requires hardware + pyvisa
    """Thin wrapper over a pyvisa resource (write / query / close)."""

    def __init__(self, addr: str, timeout_ms: int = 10000):
        if pyvisa is None:
            raise RuntimeError(
                "pyvisa not installed -- use addr='sim' (or a net: bridge address), or install the "
                "VISA backend: `uv sync --group se299-hw`")
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(addr)
        self._inst.timeout = timeout_ms

    def write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def query_raw(self, cmd: str) -> bytes:
        # RAW readback (no decode/strip) for a binary reply such as the Anritsu OSB status byte.
        self._inst.write(cmd)
        return bytes(self._inst.read_raw())

    def set_timeout(self, ms: int) -> None:
        # narrow-RBW zero-span dwells and slow swept-span sweeps can exceed the
        # fixed 10 s VISA timeout; the sweep loop raises this before arm_and_wait.
        self._inst.timeout = int(ms)

    def close(self) -> None:
        try:
            self._inst.close()
        finally:
            self._rm.close()


# ============================================================== network transport

class AdapterNotAnswering(IOError):
    """A wedged/absent device surfaced linux-gpib ENOL ("no listeners currently addressed")
    that SURVIVED one re-address (and, where the bridge supports it, a `Z recover`). Distinct
    from a transient socket timeout so the link's FAULT classifier (connection.py) can escalate
    a genuinely wedged adapter instead of retrying forever. `verdict` carries the `Z recover`
    classification when known (OK / DEVICE_SILENT / BUS_WEDGED / ADAPTER_WEDGED); adapter_wedged
    is the terminal case that means the NI GPIB-USB-HS itself must be power-cycled."""

    def __init__(self, message, verdict: str = ""):
        super().__init__(message)
        self.verdict = verdict
        self.adapter_wedged = (verdict == "ADAPTER_WEDGED")


class AnalyzerNotSweeping(RuntimeError):
    """The 8565EC trace is NOT re-acquiring (frozen / halted acquisition -- the reference/LO wedge),
    detected on the NOISY FLOOR (RF off) where a live sweep must vary. This is the "graph not moving on
    the screen while the frequency changes" symptom. Raised by _require_live_sweep, which a caller runs
    on the floor BEFORE keying a tone -- NOT during a tone read (a strong stable CW tone is a near-
    constant zero-span trace, indistinguishable from frozen, so a per-read liveness test would false-
    positive). Recovery is a power-cycle (GPIB cannot clear the wedge); diagnose with
    tools/diagnose_8565ec.py."""


# codes that indicate the shared reference/timebase + cal-oscillator PLLs have UNLOCKED (the wedge):
# 333 600-MHz-ref UNLK, 335 sampler UNLK, 337 fractional-N UNLK, 499 cal-oscillator UNLK. Canonical
# single definition reused by the driver health check, the coordinator gate, the diagnose tool, and
# the defect test suite (do not re-inline the literal set).
REFERENCE_UNLOCK_CODES = frozenset({333, 335, 337, 499})


def _is_enol(exc) -> bool:
    """True if a bridge error is a linux-gpib ENOL (the addressed device is not answering as a
    listener) -- the wedged/absent-device signature, surfaced as a framed '!' error string."""
    s = str(exc).lower()
    return "no listener" in s or "enol" in s


class NetworkTransport:
    """Client transport to a network GPIB bridge (a linux-gpib server running on a
    Linux host that physically holds the NI GPIB-USB-HS -- a UTM/QEMU VM on the Mac,
    or a Raspberry Pi). Matches the VisaTransport interface (write / query /
    set_timeout / close) so the existing Agilent856xEC / Anritsu68369 drivers ride on
    top unchanged. This is the M1 path: no local GPIB driver -- NI-488.2 has no Apple
    Silicon support -- only a TCP socket to the bridge. See gpib_bridge/ for the server
    and its wire protocol (A/W/Q/T/C -> +/=/!)."""

    # A slow bus op (prepare 15 s, peak_preselector 30 s) raises the BRIDGE read timeout via the
    # T verb; the CLIENT socket deadline must sit ABOVE it (set_timeout adds this margin) so the
    # bridge always wins the race and returns a clean framed error instead of the client abandoning
    # a HEALTHY slow read and poisoning the socket. The CONNECT deadline stays short + separate.
    SOCKET_MARGIN_S = 5.0
    CONNECT_TIMEOUT_S = 5.0

    def __init__(self, host: str, port: int, address: int, timeout_ms: int = 10000,
                 token: Optional[str] = None, client_id: Optional[str] = None):
        if _bridge is None:  # pragma: no cover - only if gpib_bridge isn't importable
            raise RuntimeError("gpib_bridge.protocol not importable (se299 dir not on sys.path)")
        self.host, self.port, self.address = host, int(port), int(address)
        self.timeout_ms = int(timeout_ms)
        self._token = token if token is not None else os.environ.get("NI_GPIB_TOKEN")
        # client identity announced to the bridge (X verb) so the device-side session
        # registry can attribute + group this session. None -> announce nothing (byte-for-byte
        # the same traffic as before, for callers that don't opt in).
        self._client_id = client_id
        # ACTIVE LEASE memory: lease() records the held scope+ttl so _connect() can RE-ASSERT the
        # lease after a reconnect -- otherwise an auto-reconnect mid-campaign silently drops
        # exclusivity (the bridge frees a session's lease when its socket dies). None = no lease.
        self._lease_scope = None
        self._lease_ttl_s = None
        self._sock = None
        self._f = None
        # one request/reply in flight per transport: the lease keepalive renews from a
        # background thread on the SAME socket the campaign transacts on, so transactions
        # must serialize or the line framing desyncs (a reply lands in the wrong thread).
        # RLock (not Lock) so reconnect() can hold it across the whole close+_connect swap while
        # _connect's own H/A/T/L transactions re-enter it on the same thread.
        self._txn_lock = threading.RLock()
        self._connect()

    def _connect(self) -> None:
        # a SHORT, separate connect deadline (not the potentially-elevated bus-op timeout): a dead
        # bridge should fail fast, while a live one connects instantly.
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.CONNECT_TIMEOUT_S)
        self._f = self._sock.makefile("rwb")
        # authenticate first if a token is configured. The secret is NEVER put in the net:
        # address, so it stays out of CLI args and run manifests.
        if self._token:
            self._txn("H", self._token.encode("ascii"))
        self._txn("A", str(int(self.address)).encode("ascii"))   # bind the GPIB address
        self.set_timeout(self.timeout_ms)                        # sets bridge T + socket deadline
        if self._client_id:
            # best-effort identity announce. An OLD (un-restarted) bridge replies
            # "! unknown verb 'X'", which _txn raises as IOError -- swallow it so X is a
            # harmless no-op there. Re-sent automatically on reconnect() (it calls _connect).
            try:
                self._txn("X", self._client_id.encode("ascii"))
            except IOError:
                pass
        if self._lease_scope is not None:
            # RE-ASSERT a lease held before the drop so an auto-reconnect keeps exclusivity: the
            # bridge freed our lease when the old socket died, and a fresh session must re-take it.
            # Best-effort -- a rival that grabbed it in the gap leaves W/Q arbitration the enforcer.
            try:
                self._send_lease(self._lease_scope, self._lease_ttl_s)
            except IOError:
                pass

    def reconnect(self) -> None:
        """Drop and re-establish the bridge connection (re-auth, re-bind, re-set timeout, re-assert
        any held lease). A socket that has TIMED OUT mid-read is poisoned -- Python raises "cannot
        read from timed out object" on every subsequent read, and a partial/late bridge reply would
        desync the next transaction anyway. A fresh socket is the clean recovery. Used after a query
        the instrument did not answer (e.g. an older synth that ignores *OPC?). The socket swap runs
        UNDER the txn lock so a concurrent keepalive renew can never read/write a half-swapped socket."""
        with self._txn_lock:
            try:
                self.close()
            except Exception:
                pass
            self._connect()

    def _txn(self, token: str, payload: bytes = b"") -> bytes:
        with self._txn_lock:
            self._f.write(_bridge.encode_request(token, payload))
            self._f.flush()
            line = self._f.readline()
        if not line:
            raise IOError("gpib bridge closed the connection")
        status, data = _bridge.decode_reply(line)
        if status == "!":
            raise IOError(f"gpib bridge error: {data.decode('ascii', 'replace')}")
        if status not in ("+", "="):
            raise IOError(f"gpib bridge protocol error: unexpected status {status!r}")
        return data

    def write(self, cmd: str) -> None:
        self._bus_op("W", cmd.encode("ascii"))

    def query(self, cmd: str) -> str:
        return self._bus_op("Q", cmd.encode("ascii")).decode("ascii", "replace").strip()

    def query_raw(self, cmd: str) -> bytes:
        """Query returning the RAW response bytes -- NO ascii-decode, NO strip. Required for a binary
        readback (e.g. the Anritsu OSB status byte): query()'s .decode('ascii','replace').strip()
        corrupts byte values that are ascii whitespace (0x09/0x0a/0x0b/0x0c/0x0d/0x20 -> stripped to
        EMPTY) or >=0x80 (-> U+FFFD). Live example: OSB=0x0C (RF-unleveled + lock-error, the worst
        case) is form-feed -> stripped to '' -> read as status 0 -> the leveled/locked interlock
        falsely passes. The wire protocol base64-frames the payload so the bytes arrive intact; only
        query()'s text post-processing mangles them, so a raw path is the fix."""
        return self._bus_op("Q", cmd.encode("ascii"))

    def _bus_op(self, token: str, payload: bytes) -> bytes:
        """A W/Q bus transaction with ENOL recovery. A wedged/absent device surfaces linux-gpib
        ENOL ("no listeners currently addressed") as a framed '!' error. On ENOL: RE-ADDRESS once
        (re-send A, which rebuilds the server-side Gpib handle) and retry the op a single time; if
        it still ENOLs, ask the bridge to classify/recover via `Z recover` (when supported) and
        raise a TYPED AdapterNotAnswering carrying the verdict -- distinct from a transient timeout,
        so the link's FAULT classifier escalates a wedged adapter. Non-ENOL errors propagate as-is."""
        try:
            return self._txn(token, payload)
        except IOError as e:
            if not _is_enol(e):
                raise
        try:
            self._txn("A", str(int(self.address)).encode("ascii"))   # one re-address (rebuild handle)
            return self._txn(token, payload)                         # ... then retry the op once
        except IOError as e2:
            if not _is_enol(e2):
                raise
        verdict, detail = "", ""
        try:
            verdict, detail = self.recover()      # `Z recover`; old bridge -> IOError (unsupported)
        except IOError:
            pass
        raise AdapterNotAnswering(
            f"gpib bridge ENOL: pad {self.address} not answering after one re-address"
            + (f"; recover={verdict} {detail}".rstrip() if verdict else ""),
            verdict=verdict or "DEVICE_SILENT")

    def set_timeout(self, ms: int) -> None:
        # set the BRIDGE-side instrument read timeout (T) AND raise the CLIENT socket read deadline
        # above it (bridge wins the race; a healthy slow op is never abandoned -> no poisoned socket).
        ms = int(ms)
        self._txn("T", str(ms).encode("ascii"))
        self.timeout_ms = ms
        if self._sock is not None:
            try:
                self._sock.settimeout(ms / 1000.0 + self.SOCKET_MARGIN_S)
            except Exception:
                pass

    # -- arbitration (VXI-11-style lease) ---------------------------------------
    # A CONTROLLER takes an exclusive lease so concurrent role instances (dashboard,
    # SE coordinator) don't fight over the bus. An OBSERVER never leases -- it can still
    # read the lease table (lease_report) but its write/query is refused while a
    # conflicting lease is held. The lease frees on release_lease(), TTL expiry, or when
    # this transport disconnects.

    def _send_lease(self, scope: str, ttl_s: float) -> str:
        """Raw L transaction (used by lease() AND by _connect()'s reconnect re-assert)."""
        target = "BUS" if str(scope).upper() == "BUS" else self.address
        return self._txn("L", f"{target} {ttl_s}".encode("ascii")).decode("ascii", "replace")

    def lease(self, scope: str = "device", ttl_s: float = 30.0) -> str:
        """Acquire an exclusive control lease. scope='device' locks just this transport's
        GPIB pad; scope='BUS' locks the whole bus. Returns the grant message; raises
        IOError if a conflicting lease is already held by another controller."""
        grant = self._send_lease(scope, ttl_s)
        self._lease_scope, self._lease_ttl_s = scope, ttl_s   # remember for reconnect re-assert
        return grant

    def renew_lease(self, ttl_s: float = 30.0) -> None:
        """Keepalive: extend this session's lease TTL. Raises IOError if none is held."""
        self._txn("K", str(ttl_s).encode("ascii"))
        if self._lease_scope is not None:                     # keep the remembered TTL current
            self._lease_ttl_s = ttl_s

    def release_lease(self) -> None:
        try:
            self._txn("U")
        finally:
            self._lease_scope = self._lease_ttl_s = None      # forget it: no re-assert on reconnect

    # -- shared recovery-verb contract (Z) --------------------------------------
    # The bridge server exposes `Z ping` (liveness) and `Z recover` (diagnose/recover a wedged
    # bus). An OLD bridge predating the Z verb replies "! unknown verb 'Z'" -> _txn raises IOError,
    # which callers catch and treat as "recover/ping unsupported" (falling back to an IDN-readback
    # liveness probe + the link's consecutive-failure counter).
    RECOVER_VERDICTS = ("OK", "DEVICE_SILENT", "BUS_WEDGED", "ADAPTER_WEDGED")

    def ping(self) -> str:
        """`Z ping` -- one bounded liveness round-trip. Returns the reply payload (may be empty).
        RAISES IOError against an old bridge without the Z verb."""
        return self._txn("Z", b"ping").decode("ascii", "replace").strip()

    def recover(self) -> tuple:
        """`Z recover` -- ask the bridge to diagnose/recover a wedged bus. Parses the reply
        `= <VERDICT> <detail>` (VERDICT in RECOVER_VERDICTS) -> (verdict, detail). RAISES IOError
        against an old bridge without the Z verb."""
        raw = self._txn("Z", b"recover").decode("ascii", "replace").strip()
        parts = raw.split(None, 1)
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")

    def lease_report(self) -> str:
        """The bridge's live lease table (who controls what) -- readable by any session."""
        return self._txn("R").decode("ascii", "replace")

    def sessions_report(self) -> str:
        """The bridge's live SESSION table -- every connected client, with its identity, bound
        pad, and lease. Mirror of lease_report() (the S verb). RAISES IOError against an old
        bridge that predates the S verb; callers catch that and treat it as 'S unsupported'
        (falling back to lease_report)."""
        return self._txn("S").decode("ascii", "replace")

    def close(self) -> None:
        try:
            self._txn("C")
        except Exception:
            pass
        for c in (getattr(self, "_f", None), getattr(self, "_sock", None)):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass


def parse_net_addr(addr: str) -> tuple:
    """Parse a bridge address 'net:HOST:PORT:GPIBADDR' -> (host, port, gpib_addr).
    HOST is a hostname or IPv4 (no colons). Case-insensitive scheme."""
    if not (isinstance(addr, str) and addr.lower().startswith("net:")):
        raise ValueError(f"not a net: address: {addr!r}")
    parts = addr.split(":")
    if len(parts) != 4 or not parts[1]:
        raise ValueError(f"net address must be net:HOST:PORT:GPIBADDR, got {addr!r}")
    return (parts[1], int(parts[2]), int(parts[3]))


def make_transport(addr: str, timeout_ms: int = 10000, client_id: Optional[str] = None):
    """Transport factory: a 'net:HOST:PORT:GPIBADDR' address -> NetworkTransport (the
    linux-gpib bridge in a VM/Pi); any other string -> VisaTransport (local VISA).
    Used by open_instruments and the CLI so a bridge address is a drop-in for a VISA
    resource string everywhere the drivers open a transport. `client_id` (if given) is
    announced to a network bridge so the device-side session registry can attribute the
    session; it is ignored for a local VISA resource (no bridge to announce to)."""
    if isinstance(addr, str) and addr.lower().startswith("net:"):
        host, port, gaddr = parse_net_addr(addr)
        return NetworkTransport(host, port, gaddr, timeout_ms, client_id=client_id)
    return VisaTransport(addr, timeout_ms)


# ============================================================== interfaces

class SignalGenerator:
    """TX source interface (set CW freq/power, RF on/off)."""

    def idn(self) -> str: raise NotImplementedError
    def prepare(self) -> None:
        """Bring the source to a known-good CW output state before a campaign. Base: no-op."""
    def settled_ok(self) -> bool:
        """True if the source reports RF leveled + locked (native OSB bits clear). Base: True."""
        return True
    def set_freq(self, f_hz: float) -> None: raise NotImplementedError
    def set_power(self, p_dbm: float) -> None: raise NotImplementedError
    def rf_on(self) -> None: raise NotImplementedError
    def rf_off(self) -> None: raise NotImplementedError

    def read_state(self) -> "device_state.SourceState":
        """ABSOLUTE device state queried FROM the source (OF1/OL1/OSB) as a SourceState -- the ground
        truth for reconciliation against the commanded model. Base raises; Sim + real implement it."""
        raise NotImplementedError

    def await_settled(self, settle_s: float = 0.05, use_opc: bool = True) -> None:
        """Block until the source has FINISHED retuning and its output has settled at the new
        CW freq/level -- the handshake the coordinator runs AFTER set_freq/rf_on and BEFORE the
        analyzer reads, so a measurement is never taken during a frequency transition. This is
        the cross-instance synchronization point: with the source and analyzer on two separate
        networked instances, the coordinator's sequential blocking calls already guarantee
        ordering; await_settled adds the source-settle the ordering alone does not. Base default:
        just dwell settle_s (a source with no completion query)."""
        if settle_s > 0:
            time.sleep(settle_s)

    def settle(self, settle_s: float = 0.05) -> None:
        """Fixed analog settle dwell ONLY -- no completion (OSB) query. Use per-point WITHIN a
        band after a CW retune: the 683xx settles physically in < 40 ms (datasheet 11410-00175),
        so once await_settled has confirmed leveled+locked at the band start, each point in the
        band pays only this dwell, not the ~85 ms OSB round-trip -- the source-bus speed lever
        (chain sweep). Base: dwell settle_s."""
        if settle_s > 0:
            time.sleep(settle_s)

    # -- source-tracks-sweep (hardware list/step sweep) -------------------------
    # For a swept measurement the source frequency MUST track the analyzer's
    # measurement frequency. The software path just calls set_freq per point; these
    # primitives are the optional hardware path: the source steps a preloaded list,
    # one point per external trigger, so a tone tracks the analyzer bin with no
    # per-point GPIB round-trip.
    def set_list_sweep(self, freqs_hz, dwell_s: float = 0.0) -> None:
        raise NotImplementedError
    def arm_sweep(self) -> None:
        """Arm the loaded list sweep at its first point (external-trigger stepping)."""
        raise NotImplementedError
    def trigger_point(self) -> None:
        """Advance the list sweep one point (or the controller pulses the trigger line)."""
        raise NotImplementedError

    def close(self) -> None: pass


class SpectrumAnalyzer:
    """RX analyzer interface (zero-span CW power read at the source frequency)."""

    def idn(self) -> str: raise NotImplementedError
    def prepare(self) -> None:
        """Establish a clean, known analyzer state before a campaign (preset + flush the stale
        first sweep). Base: no-op. Verified necessary live: without a preset, a dirty state left
        by prior marker/trace/mode commands makes marker reads return non-physical, non-repeatable
        values (identical configure() calls returned -80 then -1.7 dBm)."""
    def configure(self, rbw_hz: float, vbw_hz: float, ref_dbm: float, detector: str) -> None:
        raise NotImplementedError
    def measure_peak(self, f_hz: float, settle_s: float) -> tuple:
        """Tune to f_hz (zero span), take one sweep, return (marker_freq_hz, amp_dbm)."""
        raise NotImplementedError
    def peak_preselector(self, f_hz: float, span_hz: float = 50e6, rbw_hz: float = 1e3):
        """Peak the YIG preselector above 2.9 GHz for correct amplitude; None below/unsupported."""
        return None
    def set_preselector_dac(self, dac) -> None:
        """Reuse a recorded preselector peak DAC (no-op where there is no preselector)."""
    def measure_average(self, f_hz: float, settle_s: float, sweeps: int = 1) -> tuple:
        """Masker-robust averaged power read; base defaults to the peak read."""
        return self.measure_peak(f_hz, settle_s)

    def _sweep_is_live(self) -> bool:
        """True if the trace is actively re-acquiring (a live sweep). Base/sim: always True (the sim
        has no frozen-sweep failure mode). Agilent856xEC overrides with the real two-snapshot check."""
        return True

    def _require_live_sweep(self, f_hz: float = 0.0, tries: int = 3) -> None:
        """FLOOR-ONLY (RF off) hard liveness check: raise AnalyzerNotSweeping if the trace will not
        re-acquire (frozen/wedged). Call this on the NOISY floor BEFORE keying a tone -- NOT during a
        tone read, where a strong stable CW tone is a near-constant trace that would false-positive.
        No-op wherever _sweep_is_live is True (sim). Bounded retries ride out a first-sweep transient."""
        for _ in range(max(1, tries)):
            if self._sweep_is_live():
                return
        raise AnalyzerNotSweeping(
            f"8565EC trace is FROZEN at {f_hz/1e9:.4f} GHz -- acquisition halted (reference/LO wedge). "
            "Power-cycle the analyzer and confirm with tools/diagnose_8565ec.py before measuring.")

    def measure_tracked_peak(self, f_hz: float, search_span_hz: float = 0.0,
                             settle_s: float = 0.0) -> tuple:
        """Find + level-read a tone NEAR f_hz (search + preselector-peak above 2.9 GHz on the real
        8565EC). Base/sim: no source<->analyzer offset and no preselector, so the tone is exactly at
        f_hz -> delegate to the zero-span read (settle_s dwell honored there)."""
        return self.measure_peak(f_hz, settle_s)

    def snapshot_error_baseline(self) -> list:
        """Record the CHRONIC error codes that RE-ENTER on their own (persistent hardware/cal
        conditions): clear the queue, then re-read what comes back unaided. A healthy 8565EC
        baselines to [] (or a benign 111); a sick unit to its chronic LO/IF set. query_new_errors()
        then flags only codes NOT in this baseline, so a genuinely NEW fault raised by a measurement
        is separable from the chronic nag -- audit F12: query_errors() always returned the full
        13-code baseline, so the loop A-V7 self-check perpetually FAILED and could never spot a new
        code. Relies on self.query_errors() (sim + real implement it)."""
        self.query_errors()                          # clear the queue
        self._error_baseline = self.query_errors()   # codes that re-enter unaided = chronic baseline
        return self._error_baseline

    def query_new_errors(self) -> list:
        """Error codes present now that are NOT in the chronic baseline (snapshot_error_baseline).
        With no baseline snapshot taken, every code counts as new (== query_errors)."""
        base = set(getattr(self, "_error_baseline", None) or [])
        return [e for e in self.query_errors() if e not in base]
    def sweep_trace(self, f_lo_hz: float, f_hi_hz: float, n_points: int,
                    settle_s: float = 0.0) -> tuple:
        """Swept span acquisition: take one sweep over [f_lo, f_hi] and return the
        whole level-vs-freq trace (freqs[], levels_dbm[]) read over the bus. This is
        the analyzer-as-sweeper path used by the near-field-probe survey (no source)."""
        raise NotImplementedError

    # -- canonical 8565EC control surface (se299 sweep-mode plan) ---------------
    # Additive: base raises so a driver that omits one fails loudly; Sim + real
    # implement them. marker_bandwidth has a concrete from-trace default shared by
    # both, so no driver needs the unverified native MKBW mnemonic.
    def set_frequency(self, *, center_hz: Optional[float] = None,
                      span_hz: Optional[float] = None,
                      start_hz: Optional[float] = None,
                      stop_hz: Optional[float] = None) -> None:
        """Center/span (SP 0 = zero span) OR start/stop; supplying both is rejected."""
        raise NotImplementedError
    def set_sweep_time(self, seconds: Optional[float] = None, auto: bool = False) -> None:
        raise NotImplementedError
    def set_resolution_bandwidth(self, rbw_hz: Optional[float] = None, auto: bool = False) -> None:
        """Re-assert RBW (rbw_hz<=0 or auto -> coupled AUTO). Used to RESTORE the parked RBW after
        peak_preselector zooms to 300 kHz -- otherwise the feed keeps sweeping at 300 kHz while the
        readout says 'auto'. Base: no-op (a driver without a distinct RBW control is unaffected)."""
    def set_continuous(self, continuous: bool) -> None:
        raise NotImplementedError
    def arm_and_wait(self, timeout_s: float = 10.0, fresh: bool = True) -> None:
        """Trigger a sweep and block until it completes. fresh=True takes an extra flush sweep first
        (after a settings change); fresh=False assumes a parked free-running sweep is already current."""
        raise NotImplementedError
    def read_trace(self, trace: str = "A", calibrate: bool = False) -> tuple:
        """Pull the current trace as (freqs_hz[], levels_dbm[]) over the bus, with the frequency axis
        reconstructed from the INSTRUMENT's start/stop, not cached args. calibrate is an optional hint
        (used by the 8560 binary fast-path) that this read follows a settings change; base ignores it."""
        raise NotImplementedError
    def set_attenuation(self, db: Optional[float] = None, auto: bool = False) -> None:
        raise NotImplementedError
    def set_amplitude_units(self, units: str = "DBM",
                            scale_db_div: Optional[float] = None) -> None:
        raise NotImplementedError
    def set_detector(self, mode: str) -> None:
        """POS (positive-peak, a CW tone) or SMP (sample, the true noise floor)."""
        raise NotImplementedError
    def set_video_average(self, count: Optional[int] = None) -> None:
        raise NotImplementedError
    def set_max_hold(self, on: bool, trace: str = "A") -> None:
        raise NotImplementedError
    def marker_peak(self) -> tuple:
        """(marker_freq_hz, amp_dbm) at the highest peak of the current trace."""
        raise NotImplementedError
    def marker_bandwidth(self, n_db: float = 3.0, from_trace: bool = True) -> float:
        """N-dB-down bandwidth (Hz) about the peak. DEFAULT computes it from the pulled
        trace (identical in sim + real, zero mnemonic risk) -- used for the cavity-Q
        linewidth Q = f0 / BW_3dB. from_trace=False uses the native marker-bandwidth
        command (real driver only)."""
        if from_trace:
            freqs, levels = self.read_trace("A")
            return bandwidth_from_trace(freqs, levels, n_db)
        raise NotImplementedError
    def query_options(self) -> tuple:
        raise NotImplementedError
    def query_errors(self) -> list:
        """Drain the instrument error queue; [] means clean."""
        raise NotImplementedError
    def query_status(self) -> int:
        raise NotImplementedError
    def read_state(self) -> "device_state.AnalyzerState":
        """ABSOLUTE device state queried FROM the analyzer (CF?/SP?/RB?/VB?/RL?/AT?/DET?/LG?/AUNITS?/ST?)
        as an AnalyzerState -- the ground truth for reconciliation against the commanded model. Base
        raises; Sim + real implement it."""
        raise NotImplementedError
    def invalidate_calibration(self) -> None:
        """Drop any cached fast-read (binary MU->dBm) calibration so the next read RE-DERIVES it. Called
        when reconciliation detects an out-of-band reference-level / scale change, so the feed can never
        keep publishing a wrong amplitude from a stale map. Base: no-op (no cached calibration)."""
    def measurement_uncalibrated(self) -> bool:
        """True if the last sweep was flagged UNCAL/UNCOR (sweep-time too short for the
        span/RBW so an amplitude read is depressed). Base = never; drivers override."""
        return False
    def save_state(self, reg: int) -> None:
        raise NotImplementedError
    def recall_state(self, reg: int) -> None:
        raise NotImplementedError

    def close(self) -> None: pass


def bandwidth_from_trace(freqs, levels, n_db: float = 3.0) -> float:
    """N-dB-down bandwidth (Hz) of the tallest peak in a (freqs, levels_dbm) trace.

    Walk left and right from the peak bin to the first sample crossing peak - n_db,
    linearly interpolate each crossing, and return f_right - f_left. Returns 0.0 if
    the trace never drops n_db on a side (peak at an edge / span too narrow). This is
    the analyzer-independent linewidth used for cavity Q -- no MKBW mnemonic needed."""
    if not levels or len(levels) < 3:
        return 0.0
    pk = max(range(len(levels)), key=lambda i: levels[i])
    thr = levels[pk] - n_db

    def crossing(order) -> Optional[float]:
        prev = pk
        for i in order:
            if levels[i] <= thr:
                span = levels[prev] - levels[i]
                if span <= 0:
                    return freqs[i]
                frac = (levels[prev] - thr) / span
                return freqs[prev] + (freqs[i] - freqs[prev]) * frac
            prev = i
        return None

    f_lo = crossing(range(pk - 1, -1, -1))
    f_hi = crossing(range(pk + 1, len(levels)))
    if f_lo is None or f_hi is None:
        return 0.0
    return abs(f_hi - f_lo)


# ============================================================== real drivers

class Anritsu68369(SignalGenerator):  # pragma: no cover - requires hardware
    """Anritsu 68000-series synthesized signal generator (68369A/NV, 683xxB/C ...). Native GPIB
    mnemonics CONFIRMED against the Anritsu 682XXB/683XXB Operation Manual (P/N 10370-10284) and
    the MG369xB GPIB PM (P/N 10370-10366) -- identical 68000-series native language -- and VERIFIED
    LIVE on the bench 68367C (fw 2.35) via OF1/OL1 readback. Distilled in
    reference/operator-manuals/anritsu-68000-series-operation.md (PC2 / task #41, complete).

    KEY FIX: CW output is `CFn` ("set CW mode at Fn"), NOT `CW1`/`Fn` alone. `CW1` is not a valid
    mnemonic (Syntax Error, discarded); `F1 2 GH` only LOADS register F1 and -- per the manual --
    "does not affect the current output", so the unit stays in its power-up sweep mode emitting no
    CW tone. `CF1 2 GH` sets CW mode AND the value in one command (verified: after `CF1 2 GH`,
    `OF1` returns 2000 MHz)."""

    # HARD upper bound on the commandable output cap. max_output_dbm may be LOWERED per instance
    # (e.g. to 0.0 for a bare loopback, via drivers.arm_direct_chain) but can NEVER be raised above
    # this -- so no caller can defeat the clamp by writing a huge cap (audit finding F2). 17.0 dBm is
    # the 683xx family leveled-power ceiling and is itself 13 dB BELOW the 8565EC +30 dBm / 1 W
    # absolute-max input, so on a direct cable the TX cannot, by construction, deliver more than the
    # analyzer's rated connector maximum. (8565EC rating: agilent-8560e-users-guide.md input-damage-limits.)
    HARD_MAX_OUTPUT_DBM = 17.0

    def __init__(self, transport: VisaTransport):
        self.t = transport
        # SAFETY GUARDS (protect the TX 68367C and a directly-connected analyzer). set_power never
        # commands above max_output_dbm; set_freq is clamped into the rated band. For a bare LOOPBACK
        # into the 8565EC, LOWER max_output_dbm (call drivers.arm_direct_chain) so a strong tone
        # cannot overdrive the analyzer front end. The cap is itself CLAMPED to HARD_MAX_OUTPUT_DBM.
        self.max_output_dbm = 17.0        # 683xx family max leveled power (<= HARD ceiling)
        self.min_freq_hz = 10e6           # 68367C rated floor (OI: 0.01-40 GHz)
        self.max_freq_hz = 40e9           # rated ceiling

    @property
    def max_output_dbm(self) -> float:
        return self._max_output_dbm

    @max_output_dbm.setter
    def max_output_dbm(self, value) -> None:
        value = float(value)
        if value > self.HARD_MAX_OUTPUT_DBM:              # F2: the cap can be lowered but NEVER raised
            warnings.warn(f"max_output_dbm {value:.1f} dBm > hard ceiling "
                          f"{self.HARD_MAX_OUTPUT_DBM:.1f} dBm; clamped (cannot exceed the 8565EC input max)")
            value = self.HARD_MAX_OUTPUT_DBM
        self._max_output_dbm = value

    def idn(self) -> str:
        # NON-DESTRUCTIVE identity (idn() is used as a LIVENESS probe by control_plane). IEEE-488.2
        # *IDN? is Option 19: a native-mode / legacy 683xx does NOT answer it -> the query times out
        # and POISONS the socket (every later read fails "cannot read from timed out object"). This
        # bench unit (fw 2.35) DOES answer *IDN? and it is the richer, "683"-matchable string, so try
        # it first; on ANY failure clear the poisoned socket (reconnect) and fall back to native OI
        # (model/serial/limits/firmware -- guaranteed on all 683xx). LIVE: OI ->
        # "6867 0.0140.00-120.0 3.02.35070326C3"; *IDN? -> "ANRITSU,68367C,070326,2.35".
        try:
            r = self.t.query("*IDN?")
            if r and r.strip():
                return r.strip()
        except Exception:
            try:
                self.t.reconnect()
            except Exception:
                pass
        try:
            return self.t.query("OI").strip()     # native fallback; never poisons
        except Exception:
            return ""

    def prepare(self) -> None:
        # Force a known-good CW output state (once, before a campaign), ruling out ALL software
        # causes of "leveled/locked but no RF at the connector". Sequence + mnemonics confirmed vs
        # the Anritsu 682XXB/683XXB Operation Manual (P/N 10370-10284) + MG369xB GPIB PM
        # (10370-10366), distilled in reference/operator-manuals/anritsu-68000-series-operation.md:
        #   RST   native reset
        #   IL1   INTERNAL ALC leveling (default; DL1/PL1 external-detector modes collapse the
        #         output when nothing is on EXT ALC IN)
        #   AT0   re-COUPLE the step attenuator to the ALC (undo a stale AT1 decouple)
        #   ATT00 zero the step-attenuator value (undo a stale ATT08..ATT11 pad -- up to 110 dB is
        #         subtracted AFTER the ALC detector, so the box still reports leveled)
        #   TR0   0 dB (not 40 dB) attenuation when RF is switched off (we toggle RF each point)
        #   LO0   no level offset;  LOG  dBm level mode
        for cmd in ("RST", "IL1", "AT0", "ATT00", "TR0", "LO0", "LOG"):
            self.t.write(cmd)

    def set_freq(self, f_hz: float) -> None:
        # CFn = set CW mode at Fn + load value (GH/MH/KH/HZ terminators). CONFIRMED (see class doc).
        # SAFETY: clamp into the source's rated band -- an out-of-range CW command unlevels/faults the
        # synth and can leave RF in an undefined state.
        f_hz = float(f_hz)
        # An edge tolerance keeps a legitimate band-edge point (e.g. a 40 GHz sweep endpoint whose
        # log-spacing rounds to 40e9 + sub-Hz) from tripping a false "out of band" warning; the hard
        # clamp below still snaps it to the exact rated edge (a sub-Hz no-op) either way.
        tol_hz = max(1.0, self.max_freq_hz * 1e-9)
        if f_hz < self.min_freq_hz - tol_hz or f_hz > self.max_freq_hz + tol_hz:
            warnings.warn(f"source freq {f_hz / 1e9:.4f} GHz outside rated "
                          f"{self.min_freq_hz / 1e6:.0f} MHz-{self.max_freq_hz / 1e9:.0f} GHz; clamped")
        f_hz = min(max(f_hz, self.min_freq_hz), self.max_freq_hz)
        self.t.write(f"CF1 {f_hz / 1e9:.9f} GH")
        self.last_freq_cmd = f"CF1 {f_hz / 1e9:.9f} GH"

    def set_power(self, p_dbm: float) -> None:
        # Ln = set/select CW level register n (DM = dBm). CONFIRMED. opt-2B attenuator sets range.
        # SAFETY: hard-cap the commanded output at max_output_dbm -- protects the TX (never above its
        # rated leveled power) AND a directly-connected 8565EC input (lower max_output_dbm, e.g. to
        # 0 dBm, for a bare loopback so a strong tone cannot damage/compress the analyzer front end).
        p_dbm = float(p_dbm)
        if p_dbm > self.max_output_dbm:
            warnings.warn(f"source power {p_dbm:.1f} dBm > safety cap {self.max_output_dbm:.1f} dBm; clamped")
            p_dbm = self.max_output_dbm
        self.t.write(f"L1 {p_dbm:.2f} DM")

    def rf_on(self) -> None:
        self.t.write("RF1")                       # RF output ON (power-on default). CONFIRMED.

    def rf_off(self) -> None:
        self.t.write("RF0")                       # RF output OFF. CONFIRMED.

    # -- native readback (ASCII, newline-terminated -- safe on the *OPC?-poisoning 68367C) -------
    def output_freq_mhz(self) -> float:
        """Read back the F1 CW frequency register in MHz (native OFn). After `CF1 2 GH` -> 2000."""
        return float(self.t.query("OF1"))

    def output_level_dbm(self) -> float:
        """Read back the L1 CW level register in dBm (native OLn, log mode)."""
        return float(self.t.query("OL1"))

    def status_byte(self) -> int:
        """Primary GPIB status byte (native OSB, one raw byte). bit2=RF-unleveled, bit3=lock-error,
        bit5=syntax-error; 0 = clean. RAW binary, not ASCII: MUST use query_raw -- the text query()
        path decode/strips the byte, silently zeroing whitespace-valued status bytes (e.g. 0x0C =
        RF-unleveled+lock-error -> form-feed -> stripped -> read as 0 -> settled_ok() falsely True)."""
        raw = self.t.query_raw("OSB")             # raw bytes; raw[0] is already an int
        return raw[0] if raw else 0

    def settled_ok(self) -> bool:
        """True iff the source reports RF leveled AND locked: OSB bit2 (RF Unleveled) and bit3
        (Lock Error) both clear (Anritsu 68000-series; reading OSB also clears latched bits)."""
        try:
            b = self.status_byte()
        except Exception:
            return False
        return (b & 0x04) == 0 and (b & 0x08) == 0

    def read_state(self) -> "device_state.SourceState":
        """ABSOLUTE source state: OF1 (freq register) + OL1 (level register) + OSB (status byte). The
        registers confirm COMMAND ACCEPTANCE (not physical emission -- see the class note on RF truth);
        OSB gives leveled/locked/syntax. One OSB read (clears latched bits, like settled_ok)."""
        b = self.status_byte()
        return device_state.SourceState(
            freq_hz=self.output_freq_mhz() * 1e6,
            level_dbm=self.output_level_dbm(),
            leveled=(b & 0x04) == 0,             # bit2 clear
            locked=(b & 0x08) == 0,              # bit3 clear
            syntax_ok=(b & 0x20) == 0)           # bit5 clear -- a rejected command sets this

    def await_settled(self, settle_s: float = 0.05, use_opc: bool = False) -> None:
        # Completion HANDSHAKE via the native OSB status read (settled_ok), THEN the analog settle
        # dwell. OSB always answers on the 683xx family (a raw status byte), so it confirms the
        # source drained its command queue and reports leveled+locked WITHOUT the *OPC? failure
        # mode: IEEE-488.2 *OPC? is NOT answered by the bench 68367C (fw 2.35) -- the query times
        # out and POISONS the transport socket (every later read raises "cannot read from timed
        # out object"). So the default handshake polls OSB a few times for leveled+locked
        # (settled_ok is self-safe: returns False on a read error), then dwells. `use_opc` (opt-in,
        # default False) ADDITIONALLY issues *OPC? for a genuine 683xxB/C that supports it, with a
        # reconnect-on-timeout fallback so a non-answering unit degrades to the dwell, never a
        # poisoned socket.
        for _ in range(3):                       # native OSB completion handshake (leveled+locked)
            if self.settled_ok():
                break
            time.sleep(0.005)
        if use_opc:                              # opt-in IEEE-488.2 *OPC? (genuine 683xxB/C only)
            try:
                self.t.query("*OPC?")
            except Exception:
                try:
                    self.t.reconnect()
                except Exception:
                    pass
        if settle_s > 0:
            time.sleep(settle_s)

    def set_list_sweep(self, freqs_hz, dwell_s: float = 0.0) -> None:
        # Native 68000-series LIST SWEEP, CONFIRMED vs Anritsu MG369xB GPIB PM (P/N 10370-10366,
        # Ch.2-3; identical Native dictionary), distilled in reference/operator-manuals/
        # anritsu-68000-series-operation.md. NOTE the corrected mnemonics: the old LSP/DWL SC/
        # SWP LST/TRG EXT/*TRG were all WRONG (SWP = ANALOG sweep; SC is not a valid terminator;
        # *TRG is IEEE-488.2 which the native 68367C does not answer). Per-point advance = UP.
        freqs = list(freqs_hz)
        self.t.write("LST")                       # list-sweep mode (NOT SWP, which is analog)
        self.t.write("ELN1")                      # select volatile list 1 (ELN0 = nonvolatile)
        self.t.write("ELI0")                      # set the list index to 0 (load start address)
        pts = ", ".join(f"{f / 1e9:.9f} GH" for f in freqs)
        self.t.write(f"LF {pts}")                 # load the list frequencies from the index
        if dwell_s:
            self.t.write(f"LDT {dwell_s * 1e3:.3f} MS")   # per-step dwell (ms; SEC/MS, NOT SC)
        self.t.write("LEA")                       # learn list: precompute so sweep 1 is not slow
        self.t.write("LIB0")                      # list START index
        self.t.write(f"LIE{max(0, len(freqs) - 1)}")      # list STOP index
        self.t.write("MNT")                       # manual-step trigger: index advances one per UP

    def arm_sweep(self) -> None:
        self.t.write("RSS")                       # reset the sweep to the start index (native "arm")

    def trigger_point(self) -> None:
        self.t.write("UP")                        # advance the list index one step (native; the
        #                                           correct replacement for the unsupported *TRG)

    def close(self) -> None:
        self.t.close()


# 8560-series detector mnemonics. The INTERACTIVE GUI uses human labels; the campaign passes the
# mnemonic directly (config.detector = "POS"). Both must reach the instrument as a VALID mnemonic --
# a bad DET string (e.g. "DET peak") is SILENTLY IGNORED by the 8565EC (no fault, stale detector),
# so translate here once. The 8565EC has NO RMS detector, so "rms" is not offered in the GUI.
_DETECTOR_MNEMONIC = {
    "pos": "POS", "peak": "POS", "positive": "POS", "positive-peak": "POS", "pos-peak": "POS",
    "smp": "SMP", "sample": "SMP",
    "neg": "NEG", "neg-peak": "NEG", "negative": "NEG", "negative-peak": "NEG",
    "nrm": "NRM", "normal": "NRM", "norm": "NRM",
}


def normalize_detector(detector) -> str:
    """Map a human label OR an 8560 mnemonic to a valid 8560 mnemonic (POS/SMP/NEG/NRM). Unknown ->
    POS (positive-peak: never under-reads a CW tone -- the safe default for a substitution read)."""
    return _DETECTOR_MNEMONIC.get(str(detector).strip().lower(), "POS")


_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _leading_float(raw) -> float:
    """Parse the first numeric token from an instrument query reply (tolerates a units suffix or extra
    whitespace, e.g. '2450000000 HZ' -> 2.45e9). Raises ValueError if there is no number."""
    m = _NUM_RE.search(str(raw))
    if m is None:
        raise ValueError(f"no number in instrument reply {raw!r}")
    return float(m.group(0))


class Agilent856xEC(SpectrumAnalyzer):  # pragma: no cover - requires hardware
    """HP/Agilent 8564EC / 8565EC. HP 8560-series remote language."""

    # 8565EC INPUT DAMAGE LIMITS -- distilled in reference/operator-manuals/agilent-8560e-users-guide.md
    # (Ch.1 p.37 CAUTION + Table 1-2; Ch.2 p.109,118; Ch.4 p.218 AT-key CAUTION). These anchor the
    # direct-chain protection math in drivers.arm_direct_chain and the guard tests.
    RX_ABS_MAX_INPUT_DBM = 30.0     # +30 dBm (1 W) absolute max at INPUT 50 Ohm
    RX_RATING_MIN_ATTEN_DB = 10.0   # the +30 dBm rating is valid ONLY with >= 10 dB input attenuation
    RX_MIXER_MAX_DBM = 20.0         # first-mixer ceiling = 30 - 10 (0-dB-atten bare-mixer inference)

    def __init__(self, transport: VisaTransport):
        self.t = transport
        self._detector = "POS"                    # default detector (overwritten by configure())
        # SAFETY GUARD (protect the RX 8565EC input): a FLOOR on input attenuation. 0.0 = full
        # sensitivity (correct for low-level SHIELDED reads). RAISE (call drivers.arm_direct_chain) for
        # a DIRECTLY-CONNECTED / loopback input so the 8565EC +30 dBm (>=10 dB atten) handling rating
        # applies and the first mixer is not overdriven. Enforced in configure() AND set_attenuation()
        # -- including the AUTO path, which is pinned to the floor rather than allowed to couple to 0 dB.
        self.min_atten_db = 0.0
        self._bin_cal = None                      # (a, b) map from binary measurement units -> dBm, or
        #                                           None = read ASCII. Set by read_trace(calibrate=True),
        #                                           cleared by configure() (RL/scale may change).

    def idn(self) -> str:
        return self.t.query("ID?")                # 8560-series identity query

    def prepare(self) -> None:
        # Instrument preset -> a clean, known state, BEFORE configure(). Verified necessary live:
        # a dirty state (leftover marker/trace/mode from prior commands) makes MKA? return
        # non-physical, non-repeatable values -- identical configure() calls returned -80 then
        # -1.7 dBm until an IP was issued, after which reads were real and reproducible. The FIRST
        # sweep after preset is stale, so take one throwaway sweep here.
        self.t.set_timeout(15000)                 # IP self-cal/settle can take ~1 s
        self.t.write("IP")                        # instrument preset
        time.sleep(1.0)
        # CONTINUOUS sweep, NOT single-sweep. LIVE-PROVEN on the bench 8565EC over the networked GPIB
        # bridge: SNGLS + TS + DONE? returns a STALE trace (the DONE? sync does not block for the new
        # sweep across the bridge), so every marker read reported the PRIOR input state (a source RF
        # toggle read 0 dB delta -- tone == floor). In CONTS free-run the trace updates every sweep and
        # reads are reliable (57-58 dB tone/floor delta, 4/4). measure_peak takes 2 sweeps to land on a
        # fresh one. (The 8560 manual's SNGLS sequence assumes direct GPIB, not a qemu-passthrough bus.)
        self.t.write("CONTS")                     # continuous sweep (bridge-reliable; see note)
        self.t.write("TS")                        # flush the stale post-preset sweep
        self.t.query("DONE?")

    def configure(self, rbw_hz: float, vbw_hz: float, ref_dbm: float, detector: str) -> None:
        self.t.write("CONTS")                     # continuous sweep -- NOT SNGLS: single-sweep + TS +
        #                                           DONE? returns a STALE trace over the networked GPIB
        #                                           bridge (live-proven; see prepare() note). CONTS
        #                                           free-run + measure_peak's 2-sweep read is reliable.
        self.t.write("CLRW TRA")                  # CLEAR-WRITE trace A: clears MAX/min-hold + video
        #                                           averaging (8560 manual). MXMH TRA is a PERSISTENT
        #                                           instrument state -- the range/paint mode leaves it
        #                                           ON (range_mode.py sets max_hold=True) and nothing
        #                                           resets it, so a later measurement inherits a HELD
        #                                           max-hold trace that looks EXACTLY like a wedge
        #                                           (0/601 change, RF-toggle 0 dB delta). Asserting
        #                                           clear-write here makes every SE read start from a
        #                                           known live trace, so a stale max-hold can never
        #                                           masquerade as an instrument fault (audit 2026-07-04).
        self.t.write("AUNITS DBM")                # PIN amplitude units to dBm: MKA?/TRA? reads assume
        #                                           dBm, but AUNITS is a persistent instrument state --
        #                                           a leaked V/W from a prior session gives silent wrong
        #                                           numbers. Assert it once so every read is unambiguous.
        # rbw/vbw <= 0 (or None) = AUTO: the GUI's "RBW auto" sends 0; a literal "RB 0HZ" is an
        # invalid RBW the 8565EC rejects (RX dead on arrival), so emit the coupled-auto "RB AUTO".
        self.t.write("RB AUTO" if not rbw_hz or rbw_hz <= 0 else f"RB {rbw_hz:.0f}HZ")
        self.t.write("VB AUTO" if not vbw_hz or vbw_hz <= 0 else f"VB {vbw_hz:.0f}HZ")
        self.t.write(f"RL {ref_dbm:.1f}DBM")
        det = normalize_detector(detector)        # human label or mnemonic -> valid 8560 mnemonic
        self.t.write(f"DET {det}")
        self.t.write("SP 0HZ")                    # zero span: CW power vs time at CF
        if self.min_atten_db > 0:                 # SAFETY: enforce the input-attenuation floor so
            self.t.write(f"AT {self.min_atten_db:.0f}DB")   # BOTH read paths (tone + floor) are protected
        self._detector = det                      # remembered (as a mnemonic) so measure_floor restores it
        self._bin_cal = None                      # RL/scale just changed -> re-derive the binary map

    def read_state(self) -> "device_state.AnalyzerState":
        """ABSOLUTE analyzer state queried FROM the device via the 8560 interrogate form (manual-confirmed:
        CF?/SP?/FA?/FB?/RB?/VB?/RL?/AT?/DET?/AUNITS?/LG?/ST? -- see hp-8560-e-series-programming.md). This
        is the RX ground truth that did not exist before: the model can now be RECONCILED to the device
        rather than trusted blind. Pure reads -- takes no lease and no control; the caller owns the bus.
        LG? returns 0.0 in linear mode (== scale_db_div 0). Detector/AUNITS are returned as the raw
        mnemonics."""
        q = self.t.query
        return device_state.AnalyzerState(
            center_hz=_leading_float(q("CF?")),
            span_hz=_leading_float(q("SP?")),
            rbw_hz=_leading_float(q("RB?")),
            vbw_hz=_leading_float(q("VB?")),
            ref_level_dbm=_leading_float(q("RL?")),
            atten_db=_leading_float(q("AT?")),
            detector=str(q("DET?")).strip().upper(),
            scale_db_div=_leading_float(q("LG?")),
            aunits=str(q("AUNITS?")).strip().upper(),
            sweep_time_s=_leading_float(q("ST?")))

    def invalidate_calibration(self) -> None:
        """Force the binary MU->dBm map to re-derive on the next calibrated read (RL/scale drifted)."""
        self._bin_cal = None

    def set_resolution_bandwidth(self, rbw_hz=None, auto: bool = False) -> None:
        """Re-assert RBW without a full configure() -- RESTORE the parked RBW after a preselector zoom.
        rbw_hz<=0/None or auto -> the coupled 'RB AUTO' (a literal 'RB 0HZ' is rejected by the 8565EC)."""
        self.t.write("RB AUTO" if auto or not rbw_hz or rbw_hz <= 0 else f"RB {rbw_hz:.0f}HZ")

    # how many sweeps measure_peak will take waiting for two consecutive marker reads to agree
    # before returning the last value anyway. GENTLE OPERATION (Task 3): a real settle_s dwell after
    # the retune (below) now covers the LO/source transition, so the stabilize loop no longer has to
    # burn ~10 sweeps riding it out -- 4 is enough to confirm two consecutive agree, and fewer sweeps
    # = fewer LO re-lock stress events on the marginal reference. Overridable per instance.
    _READ_STABLE_TRIES = 4
    _READ_STABLE_TOL_DB = 0.5

    # peak_preselector real-tone guard: the found peak must rise at least this far above the trace floor
    # (median) to be treated as a tone worth peaking the YIG onto. Below it, PP would mis-tune onto noise
    # and BLANK the display, so peak_preselector returns None and leaves the preselector untouched.
    _PRESEL_MIN_TONE_DB = 8.0

    # dwell after each TS+DONE? before reading the trace in the liveness check. REQUIRED over the qemu
    # GPIB bridge: DONE? does NOT block for the new sweep across the passthrough bus, so back-to-back
    # TRA? reads ALIAS to the SAME sweep -> byte-identical -> a healthy analyzer FALSELY reads FROZEN
    # (LIVE-PROVEN: 0/601 pts change at dwell=0 vs ~590/601 at dwell>=0.1 s on a confirmed-healthy
    # 8565EC). 0.3 s gives margin over the one-sweep settle. The sim overrides _sweep_is_live -> no dwell.
    _SWEEP_LIVE_DWELL_S = 0.3

    # dwell after each TS in arm_and_wait() before read_trace(). Same bridge race as above, but for the
    # actual trace read: it must be >= the sweep time so the sweep COMPLETES (DONE? does not block). At a
    # 5 MHz span / auto RBW the sweep is ~50 ms, so 0.12 s is a ~2.4x margin. LIVE-measured: 0.30 -> 0.12
    # gave no further speed (the read is bus-limited by the 601-point TRA? transfer, not the dwell) while
    # holding 8/8 tone + 0 blanks, so 0.12 keeps margin at no cost. Separate from _SWEEP_LIVE_DWELL_S,
    # which stays 0.3 for the (reliability-over-speed) liveness check.
    _ARM_DWELL_S = 0.12

    # max RMS residual (dB) of the binary measurement-units -> dBm linear fit for the calibration to be
    # TRUSTED. The map is exact and linear (LIVE: dBm = RL - (600-MU)/6, rms 0.003 dB), so a real fit is
    # far below this; a residual above it means the binary parse is wrong (byte order / scale) -> discard
    # the calibration and read ASCII, so binary can never ship a wrong amplitude.
    _BIN_CAL_MAX_RMS_DB = 0.5

    # how many (dwell + paired binary read) attempts to confirm the SNGLS hold is FROZEN before the
    # calibration's paired read. Two agreeing binary reads = settled; a still-sweeping trace never agrees,
    # so retry. If it never settles the calibration is skipped (ASCII fallback), never wrong.
    _BIN_CAL_SETTLE_TRIES = 4

    # the 8560/8565 trace is a FIXED 601 points. A binary (TDF B) read that returns any other count is a
    # truncated / desynced transfer over the bridge -> reject it and fall back to ASCII (a short binary
    # trace would otherwise render as a partial PSD). ASCII reads self-size (len), so this guards binary.
    _TRACE_POINTS = 601

    def _sweep_is_live(self) -> bool:
        """True if trace A is actively re-acquiring, False if FROZEN (the reference/LO wedge -- halted
        acquisition). Two TRA? snapshots, each a FRESH completed sweep (TS + DONE? + a real dwell so the
        sweep finishes over the bridge before TRA?): a live sweep changes many points (fresh noise every
        sweep), a frozen acquisition returns an IDENTICAL trace. Mirrors tools/diagnose_8565ec.py
        Rx.sweep_alive (which dwells 0.4 s between snapshots for the same reason)."""
        def _snap():
            self.t.write("TDF P"); self.t.write("TS"); self.t.query("DONE?")
            if self._SWEEP_LIVE_DWELL_S:
                time.sleep(self._SWEEP_LIVE_DWELL_S)      # let the sweep COMPLETE (DONE? races over the bridge)
            raw = self.t.query("TRA?")
            return [float(x) for x in raw.replace(";", ",").split(",")
                    if x.strip() and any(c.isdigit() for c in x)]
        a = _snap(); b = _snap()
        n = min(len(a), len(b))
        if n == 0:
            return False
        return sum(1 for i in range(n) if abs(a[i] - b[i]) > 0.05) > 3

    def measure_peak(self, f_hz: float, settle_s: float) -> tuple:
        self.t.write(f"CF {f_hz:.0f}HZ")
        # GENTLE OPERATION (Task 3): a real dwell after the retune lets the first LO settle BEFORE the
        # first sweep, so the read is not taken mid-relock (and the stabilize loop below can be short).
        # settle_s = cfg.analyzer.settle_s (documented "dwell after retune before reading the marker"),
        # previously a dead parameter here. The sim override does not sleep, so tests stay fast.
        if settle_s and settle_s > 0:
            time.sleep(settle_s)
        # NB: liveness/wedge detection is NOT done here. A read may land on a strong STABLE CW tone,
        # whose zero-span trace is near-constant sweep-to-sweep -- indistinguishable from a frozen
        # (halted) trace by any trace-change test. Liveness is therefore checked at the HEALTH GATE on
        # the NOISY floor (RF off): Coordinator.analyzer_health / the tools' floor health check, which
        # call _sweep_is_live where noise actually varies. See _sweep_is_live's docstring.
        # STABILIZE, don't assume one sweep is fresh. The 8560 trace memory holds the PRIOR sweep
        # until a new one completes, and over the networked GPIB bridge the read lags a further
        # sweep -- so a single (or even double) TS can return the trace from BEFORE this point's
        # input state (LIVE-PROVEN on the bench 8565EC: a source RF toggle read one sweep behind,
        # reporting the tone as the floor and +11.7 dBm "floors"). Instead take sweeps until TWO
        # CONSECUTIVE marker reads agree within tol: a stale value differs from the next fresh one,
        # so it is rejected; once the reading settles it repeats. This ALSO rides out the source's
        # ~0.5-1 s RF on/off transition (the level keeps changing until the source settles, so the
        # loop keeps going until it is stable) -- one mechanism for both the bridge lag and the RF
        # settle. A genuinely wedged analyzer returns a CONSTANT stuck value (consecutive reads agree
        # immediately); callers detect that as tone == floor (no coupling) rather than a false pass.
        self.t.write("TS"); self.t.query("DONE?")
        self.t.write("MKPK HI"); prev = float(self.t.query("MKA?"))
        for _ in range(self._READ_STABLE_TRIES):
            self.t.write("TS"); self.t.query("DONE?")     # next fresh sweep
            self.t.write("MKPK HI")
            cur = float(self.t.query("MKA?"))
            if abs(cur - prev) <= self._READ_STABLE_TOL_DB:
                return (f_hz, cur)                # two consecutive agree -> settled, trustworthy
            prev = cur
        return (f_hz, prev)                       # settle budget spent -> last read (best effort)
        # MKF? (marker frequency readback) is NOT queried: we commanded CF f_hz above, so the
        # marker sits exactly there; every caller discards the first element anyway (_, amp).

    # -- source-off noise-floor read (SAMPLE detector, not POS) ---------------------------------
    def measure_floor(self, f_hz: float, settle_s: float) -> tuple:
        """Source-off floor read with the SAMPLE detector. POS positive-peak detection inflates a
        noise floor by ~+2.5 dB (it reports the peak of the noise, not its average), understating
        the true dynamic-range capability -- and that bias does NOT cancel in SE = ref - wall
        because the floor only gates capability/floor_limited, never the ref-wall tone
        difference itself. Switches to DET SMP for the read, then restores whatever detector
        configure() last set (default POS) so the following tone read is unaffected."""
        self.t.write("DET SMP")
        _, floor = self.measure_peak(f_hz, settle_s)
        self.t.write(f"DET {self._detector}")
        return (f_hz, floor)

    def measure_tracked_peak(self, f_hz: float, search_span_hz: float = 0.0,
                             settle_s: float = 0.0) -> tuple:
        """FIND the tone near f_hz and return (tone_freq_hz, amp_dbm). measure_peak() reads zero-span
        at the EXACT commanded CF -- fine in band 0, but it FAILS above 2.9 GHz where (a) the YIG
        preselector must be peaked or the tone reads low / is missed, and (b) the source<->analyzer
        frequency offset (unshared 10 MHz references, harmonic-multiplied at high band -- LIVE-measured
        +0.33 MHz @ 10 GHz = +33 ppm) puts the tone off the commanded CF, so a plain zero-span read lands
        on noise or a spur. This searches a WIDE span, peaks the preselector
        ON the tone above 2.9 GHz (peak_preselector centers via MKCF + peaks the YIG), then stabilize-
        reads the peak LEVEL (the SE-figure amplitude, which does not need the tone's absolute frequency
        to be correct). NOTE: the returned tone_freq is only as accurate as the analyzer reference --
        the LEVEL is trustworthy, the FREQUENCY is not until the analyzer 10 MHz reference is resolved."""
        hi = f_hz > 2.9e9
        self.t.write("CONTS")
        if hi:
            # WIDE search + MATCHED coarse RBW so the tone is resolved, NOT undersampled into a spur
            # lock (LIVE-proven: 200 MHz / 300 kHz finds the real tone where 50 MHz / 1 kHz missed it).
            # peak_preselector finds + MKCF-centers + peaks the YIG on the tone; read at the centered tone.
            span = search_span_hz or 200e6
            self.peak_preselector(f_hz, span_hz=span, rbw_hz=300e3)
        else:
            span = search_span_hz or 5e6
            self.t.write(f"CF {f_hz:.0f}HZ"); self.t.write(f"SP {span:.0f}HZ")
            self.t.write("RB 1MHZ"); self.t.write("VB 1MHZ")
        # GENTLE OPERATION (Task 3): dwell after the retune / preselector move so the LO settles
        # before the marker read (settle_s = cfg.analyzer.settle_s when driven from the loop).
        if settle_s and settle_s > 0:
            time.sleep(settle_s)
        # NB: no per-read liveness guard here -- a strong stable tone reads as a near-constant trace,
        # indistinguishable from frozen. Wedge detection is the HEALTH GATE's job, on the floor (RF off).
        # stabilize-read the peak (two consecutive marker reads agree -> defeats the bridge stale-lag)
        self.t.write("TS"); self.t.query("DONE?")
        self.t.write("MKPK HI"); prev = float(self.t.query("MKA?"))
        for _ in range(self._READ_STABLE_TRIES):
            self.t.write("TS"); self.t.query("DONE?")
            self.t.write("MKPK HI")
            cur = float(self.t.query("MKA?"))
            if abs(cur - prev) <= self._READ_STABLE_TOL_DB:
                prev = cur; break
            prev = cur
        try:
            ftone = float(self.t.query("MKF?"))
        except Exception:
            ftone = f_hz
        # Leave the DISPLAY centered on the actual peak (MKCF) so the live feed shows a centered tone
        # instead of one sitting f*(reference ppm) off the commanded CF. That offset is the REAL, un-
        # correctable analyzer-vs-source reference error (the two units share no 10 MHz reference); it is
        # still RETURNED as ftone. Cosmetic only -- the LEVEL was already peak-searched (MKPK), so the SE
        # amplitude is unaffected by where the tone sits in the span.
        self.t.write("MKCF"); self.t.write("TS"); self.t.query("DONE?")
        return (ftone, prev)

    # -- preselector peaking (correct amplitude above the 2.9 GHz band-0/1 crossover) -----------
    # Above 2.9 GHz the YIG preselector must be peaked or a real tone reads low / is missed (8560
    # E-series UG 08560-90158, PP p.560; distilled in reference/operator-manuals/
    # agilent-8560e-users-guide.md). Below 2.9 GHz (band 0) there is no preselector -> no-op.
    def peak_preselector(self, f_hz: float, span_hz: float = 200e6, rbw_hz: float = 300e3):
        """Peak the preselector on the tone at f_hz (a signal MUST be present). PP needs RBW > 100
        Hz and a nonzero span entirely in high band. Returns the preselector peak DAC (0-255) for
        reuse, or None below 2.9 GHz / on failure.

        Span/RBW MATTER: the trace is 601 points, so RBW must be >= span/601 or the sweep UNDERSAMPLES
        and MKPK HI locks a between-bin SPUR instead of the tone (LIVE-PROVEN: a 50 MHz span at RB 1 kHz
        missed the real 10 GHz tone and peaked a -63 dBm spur +15.9 MHz off; 200 MHz at RB 300 kHz found
        the true -7.67 dBm tone +0.33 MHz off). Defaults are that proven wide-span / matched-RBW recipe;
        the wide span also spans the harmonic-multiplied reference offset (MHz at high band)."""
        if f_hz <= 2.9e9:
            return None
        self.t.set_timeout(30000)                 # PP zooms + peaks; can take seconds
        self.t.write(f"CF {f_hz:.0f}HZ")
        self.t.write(f"SP {span_hz:.0f}HZ")
        self.t.write(f"RB {max(rbw_hz, 1e3):.0f}HZ")   # PP unavailable at RBW <= 100 Hz
        self.t.write("TS"); self.t.query("DONE?")
        # REAL-TONE GUARD: PP (peak preselector) tunes the YIG onto whatever MKPK HI lands on. If NO
        # tone is present (source not settled / not emitting / path open), MKPK locks a noise bin and PP
        # mis-tunes the YIG to REJECT that frequency -> the display goes BLANK and stays blank until the
        # next retune (LIVE-proven: peaking before the 68367C settled blanked the 5 GHz read). So only
        # peak if the found peak rises clearly above the trace floor; otherwise leave CF + the preselector
        # UNTOUCHED and return None -- the caller reads whatever is there (a low/absent tone), never a
        # self-inflicted blank. A weak-but-real tone still clears the margin and gets peaked.
        try:
            _, levels = self.read_trace("A")
            floor = sorted(levels)[len(levels) // 2]           # median = noise floor
            if (max(levels) - floor) < self._PRESEL_MIN_TONE_DB:
                return None                                     # no real tone -> do NOT mis-tune the YIG
        except Exception:
            pass                                                # read failed -> fall through, best effort
        self.t.write("MKPK HI"); self.t.write("MKCF")  # MKCF = marker-to-center (8560 UG p.7-114)
        self.t.write("TS"); self.t.query("DONE?")
        self.t.write("PP"); self.t.query("DONE?")      # peak the preselector (blocks until done)
        # PP changed the preselector hardware; the trace is stale until a fresh sweep completes
        # (8560 UG: a second TS after a hardware change before reading trace/PSDAC).
        self.t.write("TS"); self.t.query("DONE?")
        try:
            return int(float(self.t.query("PSDAC?")))
        except Exception:
            return None

    def set_preselector_dac(self, dac) -> None:
        """Reuse a recorded preselector peak (PSDAC 0-255) so the reference and wall reads at a
        frequency share an IDENTICAL preselector state. Hardware applies at end of sweep -> TS."""
        self.t.write(f"PSDAC {int(dac)}")
        self.t.write("TS"); self.t.query("DONE?")

    # -- masker-robust averaged read (recover a CW tone from under a pulsed masker) -------------
    def measure_average(self, f_hz: float, settle_s: float, sweeps: int = 1) -> tuple:
        """Zero-span linear-power average with the SAMPLE detector. A continuous CW tone
        contributes fully to the average while a LOW-DUTY-CYCLE pulsed masker averages toward its
        duty-cycle mean -- so this recovers a tone that positive-peak detection buries under the
        masker's pulse peaks (8560 UG DET SMP/VAVG). Returns (f_hz, avg_dbm)."""
        import math
        self.t.write(f"CF {f_hz:.0f}HZ")
        self.t.write("DET SMP")                   # sample detector (average power, not peak)
        if sweeps and sweeps > 1:
            self.t.write(f"VAVG {int(sweeps)}")
        self.t.write("TS"); self.t.query("DONE?")
        self.t.write("TDF P")
        raw = self.t.query("TRA?")
        vals = [float(x) for x in raw.replace(";", ",").split(",") if x.strip()]
        if not vals:
            return (f_hz, float("nan"))
        lin = sum(10 ** (v / 10.0) for v in vals) / len(vals)
        return (f_hz, 10 * math.log10(lin))

    def sweep_trace(self, f_lo_hz: float, f_hi_hz: float, n_points: int = 601,
                    settle_s: float = 0.0) -> tuple:
        # Retained for AnalyzerLink.read_sweep back-compat: delegate to the new
        # set_frequency + arm_and_wait + read_trace path (n_points is fixed at 601 by
        # the instrument; the arg is honored only by the simulator).
        self.set_frequency(start_hz=f_lo_hz, stop_hz=f_hi_hz)
        self.arm_and_wait(timeout_s=max(10.0, settle_s))
        return self.read_trace("A")

    # -- canonical control surface (8560 E-series remote language) --------------
    # Mnemonics CONFIRMED against the HP 8560 E-Series User's Guide (HP 08560-90146,
    # Ch. 5-7), distilled in reference/operator-manuals/hp-8560-e-series-programming.md.
    # Notes flag the 8560-specific forms that differ from the 8566/8568 family.
    def set_frequency(self, *, center_hz=None, span_hz=None,
                      start_hz=None, stop_hz=None) -> None:
        cs = center_hz is not None or span_hz is not None
        ss = start_hz is not None or stop_hz is not None
        if cs and ss:
            raise ValueError("set_frequency: use center/span OR start/stop, not both")
        if ss:
            if start_hz is not None:
                self.t.write(f"FA {start_hz:.0f}HZ")   # start freq
            if stop_hz is not None:
                self.t.write(f"FB {stop_hz:.0f}HZ")    # stop freq
        else:
            if center_hz is not None:
                self.t.write(f"CF {center_hz:.0f}HZ")  # center freq
            if span_hz is not None:
                self.t.write(f"SP {span_hz:.0f}HZ")    # span (SP 0HZ = zero span)

    def set_sweep_time(self, seconds=None, auto=False) -> None:
        if auto:
            self.t.write("ST AUTO")                    # auto-couple sweep time
        elif seconds is not None:
            self.t.write(f"ST {seconds:.6f}SC")        # SC = seconds (abbrev table)

    def set_continuous(self, continuous: bool) -> None:
        self.t.write("CONTS" if continuous else "SNGLS")  # continuous / single sweep

    def arm_and_wait(self, timeout_s: float = 10.0, fresh: bool = True) -> None:
        # Take a complete sweep and DWELL for it to actually COMPLETE before read_trace(). The 8560 has
        # no *OPC?, and over the networked GPIB bridge the TS;DONE? handshake does NOT block for the new
        # sweep (the same race _sweep_is_live documents) -- so a real dwell >= the sweep time is what
        # guarantees completion. Without it the next TS re-triggers before the sweep finishes and
        # read_trace() returns a PARTIAL trace: harmless while a stale prior sweep still holds valid
        # data, but a CLRW-cleared trace (configure() and set_max_hold(False) both clear it on every
        # apply) then NEVER refills -> a permanently BLANK trace railed at the bottom graticule.
        # LIVE-PROVEN root cause of Point Op "NO TONE": CLRW + no dwell = 0/12 tone; dwell = 12/12.
        #
        # fresh=True (DEFAULT, after any CF/span/CLRW/preselector change): take TWO sweeps -- the first
        # FLUSHES the stale one-behind trace, the second is the fresh one read_trace() reads. fresh=False
        # (PARKED, no state change since the last read, e.g. the steady-state live feed): the analyzer is
        # already free-running in CONTS so ONE completed sweep is current -- skip the flush. LIVE-measured:
        # single sweep ~halves the per-read wall time (0.6 -> 0.8-0.9 reads/s) with 8/8 tone, 0 blanks at
        # 2.45 + 10 GHz; the remaining floor is the 601-point TRA? bus transfer, not the dwell.
        # CONTINUOUS (not SNGLS): a single-sweep SNGLS TS returns a stale trace over the bridge (see
        # prepare()), and CONTS also leaves the front panel live-sweeping for the operator on release.
        self.t.set_timeout(int(max(1.0, timeout_s) * 1000))
        try:
            sweep_s = max(0.0, float(self.t.query("ST?")))     # actual (auto-coupled) sweep time
        except Exception:                                      # noqa: BLE001 -- ST? unreadable -> floor
            sweep_s = 0.0
        dwell = max(self._ARM_DWELL_S, sweep_s * 1.5)          # dwell >= sweep time = completion guarantee
        self.t.write("CONTS")                          # continuous sweep (bridge-reliable)
        if fresh:
            self.t.write("TS"); self.t.query("DONE?"); time.sleep(dwell)   # flush the stale one-behind
        self.t.write("TS"); self.t.query("DONE?"); time.sleep(dwell)       # fresh COMPLETED sweep to read

    def read_trace(self, trace: str = "A", calibrate: bool = False) -> tuple:
        """Return (freqs_hz[], levels_dbm[]). Uses the fast BINARY transfer when a self-calibration is
        cached, else ASCII. calibrate=True (the caller uses it on the FRESH tick after a settings change)
        reads the SAME frozen sweep in both ASCII (dBm) and binary (measurement units), fits dBm=a*MU+b,
        and caches (a,b) ONLY if the fit is tight -- so parked reads then use the ~3x-smaller binary
        transfer. A loose fit (wrong binary parse / unexpected scale) leaves the cache empty and every
        read stays ASCII, so binary can never ship a wrong amplitude. configure() clears the cache."""
        if calibrate:
            return self._read_and_calibrate(trace)
        if self._bin_cal is not None:
            try:
                return self._read_trace_binary(trace)
            except Exception:                          # noqa: BLE001 -- any binary hiccup -> safe ASCII
                self._bin_cal = None
        return self._read_trace_ascii(trace)

    def _freq_axis(self, n: int) -> list:
        """Reconstruct the frequency axis from the instrument's start/stop over n points."""
        fa = float(self.t.query("FA?"))
        fb = float(self.t.query("FB?"))
        if fb == fa or n <= 1:                          # zero span: level-vs-time at CF
            return [fa] * n
        return [fa + (fb - fa) * i / (n - 1) for i in range(n)]

    def _read_trace_ascii(self, trace: str = "A") -> tuple:
        self.t.write("TDF P")                          # trace data format = parameter (dBm)
        raw = self.t.query("TRA?" if trace == "A" else "TRB?")   # 601 fixed points, ASCII
        levels = [float(x) for x in raw.replace(";", ",").split(",") if x.strip()]
        if not levels:
            return ([], [])
        if len(levels) != self._TRACE_POINTS:          # truncated / desynced ASCII reply -- do NOT stretch
            raise ValueError(f"ASCII trace has {len(levels)} points, expected {self._TRACE_POINTS} "
                             "(truncated/desynced) -- refusing to publish a short trace")
        return (self._freq_axis(len(levels)), levels)

    def _read_binary_mu(self, trace: str = "A") -> list:
        """Read the trace as TDF B binary measurement units: 601 big-endian uint16 (LIVE-confirmed on the
        8565EC -- 1202 bytes, MSB-first, aligned, MU 0..600 spanning the 100 dB log display). ~3x smaller
        on the wire than the ASCII dBm transfer, which is the live-feed bottleneck."""
        self.t.write("TDF B")
        raw = self.t.query_raw("TRA?" if trace == "A" else "TRB?")
        n = len(raw) // 2
        return list(struct.unpack(f">{n}H", raw[:n * 2])) if n else []

    def _read_trace_binary(self, trace: str = "A") -> tuple:
        a, b = self._bin_cal
        mu = self._read_binary_mu(trace)
        self.t.write("TDF P")                          # restore ASCII default for any other reader
        if len(mu) != self._TRACE_POINTS:              # empty OR truncated/desynced -> caller falls back to ASCII
            raise ValueError(f"binary trace has {len(mu)} points, expected {self._TRACE_POINTS}")
        levels = [a * m + b for m in mu]
        return (self._freq_axis(len(levels)), levels)

    def _read_and_calibrate(self, trace: str = "A") -> tuple:
        """FREEZE a sweep with SNGLS, then read the SAME held trace in ASCII (dBm) and binary (MU), fit
        dBm=a*MU+b, and cache (a,b) iff the RMS residual is tight. Restore ASCII + CONTS. Returns the
        ASCII (freqs, levels) so the caller publishes the exact calibrated sweep. Any failure or a loose
        fit leaves the cache empty (ASCII-only reads) -- never a wrong amplitude.

        The ASCII and binary reads MUST land on the same sweep. Issuing SNGLS alone is not enough: if it
        arrives mid-sweep the analyzer finishes the in-flight sweep, so the first read can catch the OLD
        held trace while the second catches the just-completed one -> a two-sweep mismatch that fails the
        fit (LIVE-observed as an INTERMITTENT spurious reject). Do NOT force a fresh sweep with TS either
        -- SNGLS+TS returns a STALE trace over the bridge (documented quirk), which desyncs the read the
        same way. Instead CONFIRM the hold has settled: two back-to-back binary reads that agree prove the
        single-sweep hold is frozen (a still-sweeping trace changes point-by-point between reads); then the
        ASCII read for the fit is the SAME frozen trace. The stability check uses the cheap binary transfer
        so the extra reads are ~130 ms, not ~1 s each.

        STATE CONSISTENCY: the ASCII read is the DELIVERABLE; the binary stability loop + fit are a
        best-effort OPTIMIZATION. A hiccup in the optional machinery MUST NOT blank the trace. So the
        binary work is isolated in its own try/except that only touches the cache; the ASCII read is taken
        OUTSIDE it, so a genuine read failure PROPAGATES (the engine surfaces 'absent') instead of silently
        returning ([], []) and erasing the PSD -- the regression the pre-binary one-query path never had."""
        self._bin_cal = None
        self.t.write("SNGLS")                          # single-sweep hold (the in-flight sweep completes)
        try:
            try:
                sweep_s = max(0.0, float(self.t.query("ST?")))
            except Exception:                          # noqa: BLE001 -- ST? unreadable -> floor
                sweep_s = 0.0
            dwell = max(self._ARM_DWELL_S, sweep_s * 1.5)
            mu = None
            try:                                       # best-effort: a binary hiccup only skips calibration
                for _ in range(self._BIN_CAL_SETTLE_TRIES):
                    time.sleep(dwell)
                    m1 = self._read_binary_mu(trace)
                    m2 = self._read_binary_mu(trace)
                    if m1 and m1 == m2:                # hold is frozen (no point changed between reads)
                        mu = m2
                        break
            except Exception:                          # noqa: BLE001 -- optional machinery, never fatal
                mu = None
            freqs, levels = self._read_trace_ascii(trace)   # DELIVERABLE (raises -> engine 'absent', not blank)
            if mu is not None and len(mu) == len(levels) and len(levels) >= 8:
                cal = _fit_linear(mu, levels)
                if cal is not None and cal[2] <= self._BIN_CAL_MAX_RMS_DB:
                    self._bin_cal = (cal[0], cal[1])
        finally:
            self.t.write("TDF P")
            self.t.write("CONTS")
        return (freqs, levels)

    def set_attenuation(self, db=None, auto=False) -> None:
        if auto:
            if self.min_atten_db > 0:
                # F3: AUTO couples attenuation to the reference level and can select 0 dB at a low RL,
                # dropping below the input-protection floor. When a floor is armed, pin a FIXED
                # attenuation at the floor instead so a directly-connected front end stays protected.
                warnings.warn(f"AT AUTO overridden: input-protection floor {self.min_atten_db:.0f} dB in force")
                self.t.write(f"AT {self.min_atten_db:.0f}DB")
            else:
                self.t.write("AT AUTO")                # auto input attenuation (no floor armed)
        elif db is not None:
            db = max(float(db), self.min_atten_db)     # SAFETY: never below the input-protection floor
            self.t.write(f"AT {db:.0f}DB")             # 0-70 dB (0-60 on 8564/65E), decade

    def set_amplitude_units(self, units: str = "DBM", scale_db_div=None) -> None:
        self.t.write(f"AUNITS {units}")                # DBM/DBMV/DBUV/V/W
        if scale_db_div is not None:
            self.t.write(f"LG {scale_db_div:.0f}DB")   # log scale dB/div (1/2/5/10)

    def set_detector(self, mode: str) -> None:
        # normalize a human label ("sample"/"peak") OR mnemonic to a valid 8560 mnemonic -- a raw bad
        # string ("DET peak") is SILENTLY IGNORED by the 8565EC (stale detector -> wrong number), so
        # route through the SAME map configure() uses instead of writing the caller's string verbatim.
        self.t.write(f"DET {normalize_detector(mode)}")   # POS / NEG / NRM / SMP

    def set_video_average(self, count=None) -> None:
        if not count:
            self.t.write("VAVG OFF")                   # video averaging off
        else:
            self.t.write(f"VAVG {int(count):d}")       # 1-999 sweeps

    def set_max_hold(self, on: bool, trace: str = "A") -> None:
        tr = "TRA" if trace == "A" else "TRB"          # MXMH/CLRW require the trace arg
        self.t.write(f"MXMH {tr}" if on else f"CLRW {tr}")

    def marker_peak(self) -> tuple:
        self.t.write("MKPK HI")                        # marker -> highest peak
        amp = float(self.t.query("MKA?"))              # marker amplitude, dBm
        mkf = float(self.t.query("MKF?"))              # marker frequency, Hz
        return (mkf, amp)

    def marker_bandwidth(self, n_db: float = 3.0, from_trace: bool = True) -> float:
        if from_trace:                                  # default: compute from the trace
            return SpectrumAnalyzer.marker_bandwidth(self, n_db, from_trace=True)
        # 08560-90146: MKBW takes a NEGATIVE dB-down int with ",?" to query -> Hz;
        # there is no standalone MKBW? and no DB suffix. Needs a peak marker first.
        self.t.write("MKPK HI")
        return float(self.t.query(f"MKBW -{abs(int(round(n_db)))},?"))

    def query_options(self) -> tuple:
        # 08560-90146: the 8560 has NO *OPT?; ID? returns the model plus the installed
        # options. Parse the options out of the ID? reply (model is the first field).
        raw = self.t.query("ID?")
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        return tuple(parts[1:]) if len(parts) > 1 else ()

    def query_errors(self) -> list:
        raw = self.t.query("ERR?")                     # native error queue; read clears it
        out = []
        for p in raw.replace(";", ",").split(","):
            p = p.strip()
            if not p:
                continue
            try:
                out.append(int(float(p)))
            except ValueError:
                pass
        return [e for e in out if e != 0]

    def query_status(self) -> int:
        return int(float(self.t.query("STB?")))        # status byte (see 08560-90146)

    def measurement_uncalibrated(self) -> bool:
        # 08560-90146: the 8560 has NO dedicated MEAS-UNCAL status bit (STB 0x02 is
        # "message occurred", any message); UNCAL is a front-panel annotation, not a
        # reliable bus flag. Use ST AUTO to avoid UNCAL; the sim models the condition
        # for the guardrail test. Real-hw detection is left False (do not over-trust).
        return False

    def save_state(self, reg: int) -> None:
        self.t.write(f"SAVES {int(reg):d}")            # 8560 = SAVES (not SAV); reg 0-7 user

    def recall_state(self, reg: int) -> None:
        self.t.write(f"RCLS {int(reg):d}")             # 8560 = RCLS (not RCL); n | LAST | PWRON

    def close(self) -> None:
        self.t.close()


class SingleConsumerConflict(RuntimeError):
    """A standalone tool tried to lease an instrument another consumer already holds. Only ONE
    consumer may drive the analyzer at a time -- concurrent/interleaved probing forces more frequent,
    less-settled first-LO re-locks, which is the biggest operational contributor to the marginal
    reference wedging (Task 3). `label` names the instrument; `report` carries the bridge lease table
    so the operator sees who holds it and can stop that consumer (or wait)."""

    def __init__(self, label, report):
        self.label, self.report = label, report
        super().__init__(
            f"{label} is already leased by ANOTHER consumer -- only one consumer may drive it at a "
            f"time (concurrent probing stresses the analyzer reference). Stop the other consumer or "
            f"wait for its lease to expire, then retry.\nBridge lease table:\n{report}")


def lease_exclusive(t, label: str, ttl_s: float = 400.0) -> str:
    """Acquire an EXCLUSIVE device lease on transport t, or raise SingleConsumerConflict (with the
    live bridge lease table) if a rival already holds it -- so a standalone tool REFUSES to run
    rather than adding a second concurrent consumer. NetworkTransport.lease already raises IOError on
    a conflicting lease; this converts that into an actionable single-consumer refusal."""
    try:
        return t.lease(scope="device", ttl_s=ttl_s)
    except IOError:
        try:
            report = t.lease_report().strip() or "(empty lease table)"
        except Exception:                                 # noqa: BLE001 -- best-effort context
            report = "(lease table unavailable)"
        # ATTRIBUTE the failure: classify bridge-down / adapter-wedged / leased-by-<holder> so the
        # operator sees WHY and WHAT holds it, not a bare conflict. Only for a real network transport
        # (has host/port); a duck-typed/fake transport keeps the plain lease-table report unchanged.
        host, port = getattr(t, "host", None), getattr(t, "port", None)
        if host and port:
            try:
                import lease_diagnostics
                report = str(lease_diagnostics.diagnose(
                    host, port, label, lease_report_fn=getattr(t, "lease_report", None))) + "\n" + report
            except Exception:                             # noqa: BLE001 -- diagnosis is best-effort
                pass
        raise SingleConsumerConflict(label, report)


def arm_direct_chain(source, analyzer, source_cap_dbm: float = 0.0,
                     rx_min_atten_db: float = 20.0, cable_loss_db: float = 0.0,
                     apply_now: bool = True) -> dict:
    """Arm hardware protection for a SOURCE cabled DIRECTLY to the analyzer input (no antenna, no
    path loss). Establishes BY CONSTRUCTION that the TX cannot deliver more than the 8565EC can
    handle: it caps the source output and floors the analyzer input attenuation, then PROVES the
    resulting connector level and first-mixer level sit under the 8565EC damage limits before it
    lets any tone flow. Raises AssertionError if the requested envelope cannot be made safe.

    Worst-case model: cable_loss_db defaults to 0 (a lossless cable delivers ALL source power to the
    analyzer). Limits (agilent-8560e-users-guide.md input-damage-limits): connector <= +30 dBm (1 W),
    input attenuation >= 10 dB (precondition of the +30 dBm rating), first mixer <= +20 dBm.

    Returns the armed envelope: {source_cap_dbm, rx_min_atten_db, connector_dbm, mixer_dbm}."""
    abs_max = Agilent856xEC.RX_ABS_MAX_INPUT_DBM
    min_atten_req = Agilent856xEC.RX_RATING_MIN_ATTEN_DB
    mixer_max = Agilent856xEC.RX_MIXER_MAX_DBM

    # The source cap is itself clamped to the source's hard ceiling; the atten floor is at least the
    # rating's required minimum. Neither can be argued down below a safe value.
    cap = min(float(source_cap_dbm), Anritsu68369.HARD_MAX_OUTPUT_DBM)
    atten = max(float(rx_min_atten_db), min_atten_req)
    connector_dbm = cap - float(cable_loss_db)          # worst-case power reaching the RX connector
    mixer_dbm = connector_dbm - atten                   # power reaching the first mixer

    assert connector_dbm <= abs_max, (
        f"unsafe direct chain: connector {connector_dbm:.1f} dBm > 8565EC max {abs_max:.1f} dBm (1 W)")
    assert atten >= min_atten_req, (
        f"unsafe direct chain: {atten:.0f} dB atten < {min_atten_req:.0f} dB required for the +30 dBm rating")
    assert mixer_dbm <= mixer_max, (
        f"unsafe direct chain: first mixer {mixer_dbm:.1f} dBm > ceiling {mixer_max:.1f} dBm")

    source.max_output_dbm = cap                         # arm the source cap (property re-clamps to HARD)
    analyzer.min_atten_db = atten                       # arm the atten floor (enforced everywhere)
    if apply_now:
        analyzer.set_attenuation(db=atten)              # write it to the instrument NOW (needs a lease);
        #                                                 else the atten floor is applied by the next
        #                                                 configure()/set_attenuation under the campaign lease.
    return {"source_cap_dbm": cap, "rx_min_atten_db": atten,
            "connector_dbm": connector_dbm, "mixer_dbm": mixer_dbm}


# ============================================================== simulator

@dataclass
class SimBench:
    """Shared physical state for the simulator: the source, the wall, the floor.

    The sim SG mutates the source state here; the sim SA reads it to synthesize a
    marker amplitude via the doc-159 link budget. Deterministic (seeded) so tests
    are stable. In a REAL run there is no se_model -- the wall's SE is what we measure.
    """
    separation_m: float = 0.6
    wall_present: bool = False
    seed: int = 1234
    # source state (written by the sim SG)
    src_freq_hz: float = 1e9
    src_power_dbm: float = 0.0
    src_rf_on: bool = False
    rbw_hz: float = 1000.0
    # current band params, set by the loop per measurement (NOT re-derived from f:
    # adjacent bands abut at 18 GHz and a float-fragile freq lookup misassigns the
    # boundary point's horn gain). se_model is frequency-based (smooth, no boundary).
    gain: float = 14.0                                      # antenna gain dBi (per horn)
    danl: float = -150.0                                    # DANL dBm/Hz
    se_model: Optional[Callable[[float], float]] = None     # f -> TRUE enclosure SE dB
    # localization state (set by loop.localize): a fixed-freq near-field probe scan
    localize_mode: bool = False
    probe_position: float = 0.0                             # probe position (m or label)
    leak_profile: Optional[Callable[[float], float]] = None  # position -> leaked level dBm at probe
    # cavity-Q state (both units inside): a resonance spectrum f -> level dBm that
    # the swept trace reads. When set, it takes priority over the substitution model.
    resonance: Optional[Callable[[float], float]] = None
    # hardware source-tracked sweep: the source steps this list one point per trigger,
    # so a tone tracks the analyzer bin without a per-point GPIB round-trip.
    sweep_list: Optional[list] = None
    sweep_index: int = 0

    def amplitude_dbm(self) -> float:
        f = self.src_freq_hz
        floor = noise_floor_dbm(self.danl, self.rbw_hz)
        if self.localize_mode and self.leak_profile is not None:
            rng = random.Random(hash((self.seed, "loc", round(self.probe_position * 1000))))
            if not self.src_rf_on:
                return floor + rng.gauss(0.0, 0.5)
            return db_power_sum(self.leak_profile(self.probe_position), floor) + rng.gauss(0.0, 0.3)
        rng = random.Random(hash((self.seed, round(f))))
        floor_noise = floor + rng.gauss(0.0, 0.5)
        if not self.src_rf_on:
            return floor_noise
        ref = reference_amp_dbm(self.src_power_dbm, self.gain, self.gain, f, self.separation_m)
        sig = ref - self.se_model(f) if self.wall_present else ref
        # the analyzer reads the incoherent sum of the signal and the floor
        return db_power_sum(sig, floor_noise)


class SimSignalGenerator(SignalGenerator):
    HARD_MAX_OUTPUT_DBM = Anritsu68369.HARD_MAX_OUTPUT_DBM   # parity: same un-raisable ceiling

    def __init__(self, bench: SimBench):
        self.b = bench
        self.max_output_dbm = 17.0        # SAFETY: mirror the real driver's output cap (parity)

    @property
    def max_output_dbm(self) -> float:
        return self._max_output_dbm

    @max_output_dbm.setter
    def max_output_dbm(self, value) -> None:
        value = float(value)
        if value > self.HARD_MAX_OUTPUT_DBM:              # F2 parity: cap can be lowered, never raised
            warnings.warn(f"max_output_dbm {value:.1f} dBm > hard ceiling "
                          f"{self.HARD_MAX_OUTPUT_DBM:.1f} dBm; clamped")
            value = self.HARD_MAX_OUTPUT_DBM
        self._max_output_dbm = value

    def idn(self) -> str:
        return "SIM,Anritsu-68369A/NV,0,sim"

    def prepare(self) -> None:
        self.b.src_rf_on = False                  # sim: reset to a known off/leveled state

    def output_freq_mhz(self) -> float:
        return getattr(self.b, "src_freq_hz", 0.0) / 1e6

    def output_level_dbm(self) -> float:
        return getattr(self.b, "src_power_dbm", 0.0)

    def status_byte(self) -> int:
        return 0                                  # sim: always clean (leveled, locked, no error)

    def set_freq(self, f_hz: float) -> None:
        self.b.src_freq_hz = f_hz

    def set_power(self, p_dbm: float) -> None:
        self.b.src_power_dbm = min(float(p_dbm), self.max_output_dbm)   # SAFETY: same cap as real

    def rf_on(self) -> None:
        self.b.src_rf_on = True

    def rf_off(self) -> None:
        self.b.src_rf_on = False

    def read_state(self) -> "device_state.SourceState":
        """Sim ABSOLUTE source state (status always clean: leveled/locked/syntax-ok) for hardware-free
        reconciliation tests."""
        return device_state.SourceState(
            freq_hz=self.output_freq_mhz() * 1e6,
            level_dbm=self.output_level_dbm(),
            leveled=True, locked=True, syntax_ok=True)

    def await_settled(self, settle_s: float = 0.05, use_opc: bool = True) -> None:
        # sim: instantaneous + deterministic (no sleep) -- the bench state is already applied.
        self.b.settled_count = getattr(self.b, "settled_count", 0) + 1

    def settle(self, settle_s: float = 0.05) -> None:
        # sim: dwell-only fast path, instantaneous (counted separately from OSB-confirmed settles)
        self.b.dwell_count = getattr(self.b, "dwell_count", 0) + 1

    def set_list_sweep(self, freqs_hz, dwell_s: float = 0.0) -> None:
        self.b.sweep_list = list(freqs_hz)
        self.b.sweep_index = 0

    def arm_sweep(self) -> None:
        self.b.sweep_index = 0
        if self.b.sweep_list:
            self.b.src_freq_hz = self.b.sweep_list[0]

    def trigger_point(self) -> None:
        if self.b.sweep_list:
            self.b.sweep_index = min(self.b.sweep_index + 1, len(self.b.sweep_list) - 1)
            self.b.src_freq_hz = self.b.sweep_list[self.b.sweep_index]


class SimSpectrumAnalyzer(SpectrumAnalyzer):
    """Hardware-free 8565EC. Analyzer CONTROL state lives on the instance; the
    physical world (source, wall, DANL, cavity resonance) lives on the shared
    SimBench. A trace is synthesized from whichever model applies, in priority
    order: cavity resonance > near-field spectrum > swept SUBSTITUTION."""

    def __init__(self, bench: Optional[SimBench] = None,
                 nf_model: Optional[Callable[[float], float]] = None, seed: int = 4242):
        self.b = bench                # SE-substitution / cavity bench (measure_peak + physics)
        self.nf = nf_model            # near-field probe spectrum (analyzer-as-sweeper survey)
        self.seed = seed
        # analyzer control state (mutated by configure / set_*)
        self.detector = "POS"
        self.atten_db = 0.0
        self.min_atten_db = 0.0       # SAFETY: input-attenuation floor (parity with the real driver)
        self.aunits = "DBM"
        self.ref_level_dbm = 0.0      # tracked so read_state() returns a faithful RL (set by configure)
        self.scale_db_div = 10.0      # log 10 dB/div (parity with the real default); read_state reports it
        self.video_avg = 0
        self.max_hold = False
        self.span_lo_hz = 1e9
        self.span_hi_hz = 6e9
        self.options = ("001", "006", "007", "008")   # our owned unit (30 Hz-50 GHz)
        self.uncal = False
        self._sweep_i = 0
        self._maxhold: Optional[list] = None
        self._regs: dict = {}

    def idn(self) -> str:
        return "SIM,Agilent-8564EC,0,sim"

    def prepare(self) -> None:
        self.uncal = False                        # sim: preset clears any UNCAL / stale state
        self._maxhold = None

    def configure(self, rbw_hz: float, vbw_hz: float, ref_dbm: float, detector: str) -> None:
        # rbw_hz <= 0 (or None) = AUTO: keep a valid RBW (a literal 0 would make noise_floor_dbm do
        # log10(0) and crash -- which the panel then mis-renders as "analyzer ABSENT"). Mirror the
        # real driver, which sends RB AUTO.
        if self.b is not None and rbw_hz and rbw_hz > 0:
            self.b.rbw_hz = rbw_hz
        self.detector = normalize_detector(detector) if detector else self.detector
        self.ref_level_dbm = float(ref_dbm)       # tracked so read_state() reports a faithful RL

    def measure_peak(self, f_hz: float, settle_s: float) -> tuple:
        self.b.src_freq_hz = f_hz  # the sim's "tune" tracks the source freq (CW substitution)
        return (f_hz, self.b.amplitude_dbm() - self.atten_db)

    def measure_floor(self, f_hz: float, settle_s: float) -> tuple:
        """Source-off floor read with the SAMPLE detector -- mirrors Agilent856xEC.measure_floor.
        VERIFIED: SimBench.amplitude_dbm() (what measure_peak reads) has no POS/SMP-dependent
        bias -- that +2.5 dB positive-peak bias is only modeled in _subst_base (the swept-trace
        path used by read_trace/sweep_trace), not here. So this is numerically identical to
        measure_peak on this bench; the detector is still toggled for consistency with the real
        driver's DET SMP -> read -> restore sequence and so a fake bench that DOES key floor
        behavior off self.detector stays correct."""
        prev = self.detector
        self.detector = "SMP"
        try:
            return self.measure_peak(f_hz, settle_s)
        finally:
            self.detector = prev

    # -- control surface --------------------------------------------------------
    def set_frequency(self, *, center_hz=None, span_hz=None,
                      start_hz=None, stop_hz=None) -> None:
        cs = center_hz is not None or span_hz is not None
        ss = start_hz is not None or stop_hz is not None
        if cs and ss:
            raise ValueError("set_frequency: use center/span OR start/stop, not both")
        if ss:
            if start_hz is not None:
                self.span_lo_hz = start_hz
            if stop_hz is not None:
                self.span_hi_hz = stop_hz
        else:
            c = center_hz if center_hz is not None else (self.span_lo_hz + self.span_hi_hz) / 2
            s = span_hz if span_hz is not None else (self.span_hi_hz - self.span_lo_hz)
            self.span_lo_hz, self.span_hi_hz = c - s / 2, c + s / 2

    def set_sweep_time(self, seconds=None, auto=False) -> None:
        pass                          # timing not modeled; UNCAL is set explicitly in tests

    def set_continuous(self, continuous: bool) -> None:
        pass

    def arm_and_wait(self, timeout_s: float = 10.0, fresh: bool = True) -> None:
        pass                          # sim sweeps are instantaneous

    def read_trace(self, trace: str = "A", calibrate: bool = False) -> tuple:
        return self._synth(self.span_lo_hz, self.span_hi_hz, 601)   # sim has no binary path; calibrate no-op

    def read_state(self) -> "device_state.AnalyzerState":
        """Sim ABSOLUTE state from the tracked control state (parity with the real read_state, so
        reconciliation tests run hardware-free). RBW comes from the shared bench; VBW is unmodeled (0=AUTO)."""
        return device_state.AnalyzerState(
            center_hz=(self.span_lo_hz + self.span_hi_hz) / 2.0,
            span_hz=(self.span_hi_hz - self.span_lo_hz),
            rbw_hz=float(getattr(self.b, "rbw_hz", 0.0) or 0.0),
            vbw_hz=0.0,
            ref_level_dbm=self.ref_level_dbm,
            atten_db=self.atten_db,
            detector=str(self.detector).strip().upper(),
            scale_db_div=self.scale_db_div,
            aunits=str(self.aunits).strip().upper(),
            sweep_time_s=0.0)

    def set_resolution_bandwidth(self, rbw_hz=None, auto: bool = False) -> None:
        if self.b is not None and rbw_hz and rbw_hz > 0 and not auto:
            self.b.rbw_hz = rbw_hz             # restore the parked RBW after a preselector zoom (parity)

    def set_attenuation(self, db=None, auto=False) -> None:
        if db is not None:
            self.atten_db = max(float(db), self.min_atten_db)   # SAFETY: same floor as real
        elif auto:
            self.atten_db = max(0.0, self.min_atten_db)         # F3 parity: AUTO honors the floor

    def set_amplitude_units(self, units: str = "DBM", scale_db_div=None) -> None:
        self.aunits = units

    def set_detector(self, mode: str) -> None:
        self.detector = mode

    def set_video_average(self, count=None) -> None:
        self.video_avg = int(count or 0)

    def set_max_hold(self, on: bool, trace: str = "A") -> None:
        self.max_hold = bool(on)
        if not on:
            self._maxhold = None

    def marker_peak(self) -> tuple:
        freqs, levels = self.read_trace("A")
        i = max(range(len(levels)), key=lambda k: levels[k])
        return (freqs[i], levels[i])

    def query_options(self) -> tuple:
        return tuple(self.options)

    def query_errors(self) -> list:
        return []

    def query_status(self) -> int:
        return 0

    def measurement_uncalibrated(self) -> bool:
        return bool(self.uncal)

    def save_state(self, reg: int) -> None:
        self._regs[int(reg)] = (self.detector, self.atten_db, self.aunits,
                                self.video_avg, self.max_hold,
                                self.span_lo_hz, self.span_hi_hz)

    def recall_state(self, reg: int) -> None:
        s = self._regs.get(int(reg))
        if s is not None:
            (self.detector, self.atten_db, self.aunits, self.video_avg,
             self.max_hold, self.span_lo_hz, self.span_hi_hz) = s

    def sweep_trace(self, f_lo_hz: float, f_hi_hz: float, n_points: int = 601,
                    settle_s: float = 0.0) -> tuple:
        return self._synth(f_lo_hz, f_hi_hz, n_points)

    # -- trace synthesis: cavity resonance > near-field > swept substitution -----
    def _synth(self, lo: float, hi: float, n_points: int) -> tuple:
        n = max(1, int(n_points))
        b = self.b
        if b is not None and b.resonance is not None:
            model = b.resonance
        elif self.nf is not None:
            model = self.nf
        elif b is not None:
            model = lambda f: self._subst_base(f, b)
        else:
            model = demo_nearfield_spectrum()
        freqs = ([lo + (hi - lo) * i / (n - 1) for i in range(n)]
                 if (n > 1 and hi > lo) else [lo] * n)
        sigma = 0.4 / math.sqrt(self.video_avg) if self.video_avg else 0.4
        self._sweep_i += 1
        levels = []
        for f in freqs:
            rng = random.Random(hash((self.seed, round(f), self._sweep_i)))
            levels.append(model(f) + rng.gauss(0.0, sigma))
        if self.max_hold:
            if self._maxhold is None or len(self._maxhold) != n:
                self._maxhold = levels[:]
            else:
                self._maxhold = [max(a, c) for a, c in zip(self._maxhold, levels)]
            levels = self._maxhold[:]
        return (freqs, levels)

    def _subst_base(self, f: float, b: SimBench) -> float:
        """Noiseless swept SUBSTITUTION level at f: reference - SE(f), power-summed
        with the RBW-dependent floor, minus input attenuation (a display offset that
        cancels in the SE ratio). Positive-peak biases the floor up vs sample -- so a
        wide-RBW swept trace is BLIND to a deep leak the narrow-RBW dwell would catch."""
        floor = noise_floor_dbm(b.danl, b.rbw_hz) + (2.5 if self.detector == "POS" else 0.0)
        if not b.src_rf_on:
            return floor - self.atten_db
        ref = reference_amp_dbm(b.src_power_dbm, b.gain, b.gain, f, b.separation_m)
        sig = ref - b.se_model(f) if (b.wall_present and b.se_model is not None) else ref
        return db_power_sum(sig, floor) - self.atten_db


# ============================================================== factory + models

def demo_enclosure_se(target_db: float = 105.0):
    """A plausible TRUE enclosure SE curve for the simulator: ~target with two
    injected leaks -- a door-seal dip near 2.4 GHz and a bolt-pitch slot dip near
    38 GHz (doc 29: inter-bolt apertures become electrically large at 40 GHz)."""
    import math

    def se(f_hz: float) -> float:
        f_ghz = f_hz / 1e9
        val = target_db
        val -= 22.0 * math.exp(-((f_ghz - 2.4) ** 2) / (2 * 0.4 ** 2))   # door-seal leak
        val -= 35.0 * math.exp(-((f_ghz - 38.0) ** 2) / (2 * 2.5 ** 2))  # bolt-pitch slot
        return val
    return se


def demo_seam_leak(hot_position_m: float = 1.2, peak_dbm: float = -70.0,
                   width_m: float = 0.06, floor_dbm: float = -110.0):
    """Demo near-field leak profile for the simulator: a hot seam (Gaussian) at
    hot_position_m above a quiet baseline -- the level a fixed-freq RX probe reads
    vs position along a wall. Returns position(m) -> leaked level dBm."""
    import math

    def level(pos_m: float) -> float:
        return floor_dbm + (peak_dbm - floor_dbm) * math.exp(
            -((pos_m - hot_position_m) ** 2) / (2 * width_m ** 2))
    return level


def demo_nearfield_spectrum(floor_dbm: float = -100.0, peaks=None):
    """A plausible near-field-probe spectrum for the simulator: a quiet floor with
    a couple of LEAK peaks the probe is meant to find -- a hot seam at 2.45 GHz and
    a weaker door-seal leak at 0.9 GHz. Returns f_hz -> level_dbm (noiseless; the
    sim analyzer adds seeded noise). Each peak is a Gaussian bump above the floor."""
    import math
    pk = peaks if peaks is not None else [(2.45e9, -45.0, 0.05e9), (0.9e9, -60.0, 0.03e9)]

    def level(f_hz: float) -> float:
        v = floor_dbm
        for center, peak_dbm, width in pk:
            v += (peak_dbm - floor_dbm) * math.exp(-((f_hz - center) ** 2) / (2 * width ** 2))
        return v
    return level


def demo_cavity_resonance(f0_hz: float, q: float, peak_dbm: float = -20.0,
                          floor_dbm: float = -90.0):
    """A single cavity resonance for the simulator: a power Lorentzian whose 3-dB
    FULL width is exactly f0/q, so `marker_bandwidth(from_trace=True)` on a swept
    trace recovers Q = f0 / BW_3dB. Returns f_hz -> level dBm (the sim adds noise)."""
    bw = f0_hz / q
    def level(f_hz: float) -> float:
        x = 2.0 * (f_hz - f0_hz) / bw
        lor = peak_dbm - 10.0 * math.log10(1.0 + x * x)     # -3.01 dB at x = +/-1
        return max(floor_dbm, lor)
    return level


def install_bench_models(bench: SimBench, cfg) -> None:
    """Install the simulator's enclosure SE model (gain/DANL are set per-measurement
    by the loop from the active band -- see SimBench.gain/danl)."""
    if bench.se_model is None:
        bench.se_model = demo_enclosure_se()


def open_instruments(cfg):
    """Return (source, analyzer, bench). Both 'sim' share one SimBench; bench is
    None when both are real hardware."""
    src_sim = cfg.instruments.source_addr == "sim"
    sa_sim = cfg.instruments.analyzer_addr == "sim"
    bench = None
    if src_sim or sa_sim:
        bench = SimBench(separation_m=cfg.geometry.separation_m)
        install_bench_models(bench, cfg)
    source = (SimSignalGenerator(bench) if src_sim
              else Anritsu68369(make_transport(cfg.instruments.source_addr)))
    analyzer = (SimSpectrumAnalyzer(bench) if sa_sim
                else Agilent856xEC(make_transport(cfg.instruments.analyzer_addr)))
    return source, analyzer, bench
