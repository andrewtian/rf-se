"""Automatic connection lifecycle for the SE analyzer (8565EC by default).

One class, AnalyzerLink, drives the full lifecycle so the caller never opens a bus
by hand:

    DISCONNECTED --discover--> (ABSENT | found)
                 --open------> (FAILED | opened)
                 --identify+validate--> (INVALID | READY)

and AUTO-RECONNECTS: a transport error during use drops the link to DISCONNECTED,
and the next ensure() re-runs discover+open+validate (bounded by `retries`). All
dependencies (discover_fn, open_fn) are injected so the whole thing runs
hardware-free against the simulator and is fully unit-testable.

`status()` is what the UI displays: detected? valid? which model/serial/address.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


# lifecycle states
DISCONNECTED = "DISCONNECTED"   # initial / after a clean close
ABSENT = "ABSENT"               # discovery found no matching-model device (not on the bus)
FAILED = "FAILED"               # a matching device was found but open/identify/liveness errored
INVALID = "INVALID"             # device present but wrong model / span out of range
READY = "READY"                 # identified + valid + ANSWERING -> usable as a sweeper
FAULT = "FAULT"                 # TERMINAL: a matching device is on the bus but stopped answering
                                # after K consecutive drops/timeouts (or an ADAPTER_WEDGED verdict)
                                # -- distinct from ABSENT; ensure() will NOT auto-clear it. Carries
                                # an actionable message (power-cycle the adapter).


@dataclass(frozen=True)
class ExpectedAnalyzer:
    model_token: str             # case-insensitive substring required in the IDN model
    freq_lo_hz: float            # instrument capability low edge
    freq_hi_hz: float            # instrument capability high edge
    label: str = "8565EC"
    family_token: str = ""       # looser token: a near-miss model (same family, wrong
                                 # unit) is reported DETECTED-but-INVALID, not ABSENT


# The 8565EC: 9 kHz - 50 GHz (Opt 006 drops the low end to 30 Hz). Canonical RX
# analyzer (doc 105d / doc 159). model_token matches '8565E'/'HP8565EC'/'8565EC';
# family_token '856' catches a wrong 856x analyzer so the UI can say so.
DEFAULT_8565EC = ExpectedAnalyzer("8565", 30.0, 50e9, label="8565EC", family_token="856")


@dataclass
class LinkStatus:
    state: str
    detected: bool
    valid: bool
    model: str = ""
    serial: str = ""
    address: str = ""
    transport: str = ""
    reason: str = ""
    reconnects: int = 0


class LinkNotReady(RuntimeError):
    """read_sweep() called while the link is not READY."""


class LinkDropped(RuntimeError):
    """The transport raised mid-acquisition; the link has dropped to DISCONNECTED."""


def validate_analyzer(device, expected: ExpectedAnalyzer, span) -> tuple:
    """Is this discovered device the CORRECT analyzer, able to sweep `span`?

    Returns (ok, reason). ok iff the model token matches AND the requested span
    [f_lo, f_hi] lies within the instrument's frequency capability."""
    if expected.model_token.lower() not in (device.model or "").lower():
        return (False, f"model '{device.model}' does not contain '{expected.model_token}'")
    lo, hi = span
    if lo < expected.freq_lo_hz or hi > expected.freq_hi_hz:
        return (False, f"span {lo/1e9:.3f}-{hi/1e9:.3f} GHz outside instrument range "
                       f"{expected.freq_lo_hz/1e9:.3f}-{expected.freq_hi_hz/1e9:.0f} GHz")
    return (True, "ok")


class AnalyzerLink:
    """Self-managing link to the SE analyzer. See module docstring for the states."""

    role = "rx"                             # this side's role (rx analyzer); SourceLink overrides tx

    def __init__(self, expected: ExpectedAnalyzer, span, discover_fn, open_fn,
                 retries: int = 3, backoff_s: float = 0.25, fault_after: int = 3, recover_fn=None):
        self.expected = expected
        self.span = tuple(span)
        self._discover = discover_fn        # () -> list[DiscoveredDevice]
        self._open = open_fn                # (DiscoveredDevice) -> analyzer (has sweep_trace/close)
        self.retries = max(1, int(retries))
        # a small default backoff so ensure() does not HAMMER a hard-down bridge between retries.
        self.backoff_s = backoff_s
        self.state = DISCONNECTED
        self.device = None
        self.analyzer = None
        self.reason = ""
        self.reconnects = 0
        # a REAL reconnect = the link came back to READY after an unexpected DROP (not the first
        # connect, and not a recovery from an explicit close). Set by the drop handlers.
        self._dropped = False
        # CONSECUTIVE bus-op failures (drops/timeouts/open-liveness failures). Reset on a genuine
        # successful bus op (a probed READY or a successful read/write), NOT on socket-open. After
        # fault_after in a row (or an ADAPTER_WEDGED verdict) the link goes terminal FAULT.
        self._consec_fail = 0
        self.fault_after = max(1, int(fault_after))
        # 44.2 SOFT RECOVER (opt-in): a closure recover_fn(exc) -> bool that de-keys the source and QMP
        # virtual-replugs this adapter up to its budget, returning True iff the instrument answered again.
        # Injected ONLY for a LOCAL --vm owner (a remote net: address gets None -> straight to FAULT /
        # 44.3 HARD alert). Default None = the exact prior behavior. _recovering guards re-entrancy so a
        # bus op inside recover_fn cannot recurse into recovery.
        self._recover_fn = recover_fn
        self._recovering = False

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> LinkStatus:
        """Run one full discover -> open -> validate(+liveness) attempt; set state; return status.

        The open path may run a bounded LIVENESS PROBE (the control plane wraps the network open_fn
        so opening also proves the device ANSWERS, not just that the socket connected) -- so an open
        failure here counts as a real bus-op failure toward the FAULT escalation."""
        if self.state == FAULT:                       # terminal: do not silently re-run and clear it
            return self.status()
        self._teardown_analyzer()
        try:
            devices = self._discover() or []
        except Exception as e:
            self.state, self.reason, self.device = FAILED, f"discovery error: {e}", None
            self._bump_failure(e)
            return self.status()
        match = next((d for d in devices
                      if self.expected.model_token.lower() in (d.model or "").lower()), None)
        if match is None and self.expected.family_token:
            # a same-family but wrong unit -> DETECTED, validation will fail -> INVALID
            match = next((d for d in devices
                          if self.expected.family_token.lower() in (d.model or "").lower()), None)
        if match is None:
            # ABSENT (not on the bus) is NOT a wedged-adapter fault: leave the failure counter alone.
            self.state, self.reason, self.device = ABSENT, "no matching-model device on the bus", None
            return self.status()
        self.device = match
        ok, reason = validate_analyzer(match, self.expected, self.span)
        if not ok:
            self.state, self.reason = INVALID, reason
            return self.status()
        try:
            self.analyzer = self._open(match)
        except Exception as e:
            self.state, self.reason = FAILED, f"open failed: {e}"
            self._bump_failure(e)                     # a wedged-device open/liveness failure counts
            return self.status()
        if self.analyzer is None:
            self.state, self.reason = FAILED, "open returned no analyzer"
            self._bump_failure()
            return self.status()
        self.state, self.reason = READY, "ok"
        if self._dropped:                             # a genuine reconnect: came back after a DROP
            self.reconnects += 1
            self._dropped = False
        self._consec_fail = 0                         # a probed READY IS a successful bus op -> reset
        return self.status()

    def probe_alive(self) -> bool:
        """SIDE-EFFECT-FREE liveness probe: attempt discover -> match -> open (the control plane's open
        is liveness-wrapped, so a successful open proves the instrument ANSWERS) and return whether it
        answered -- WITHOUT mutating the FAULT accounting or link state. Used by the 44.2 soft-recover
        loop to test whether a QMP virtual-replug brought the adapter back, so the probe itself can never
        push the link toward FAULT (which link.connect() would)."""
        try:
            devices = self._discover() or []
        except Exception:                             # noqa: BLE001 -- a probe never raises
            return False
        match = next((d for d in devices
                      if self.expected.model_token.lower() in (d.model or "").lower()), None)
        if match is None:
            return False
        try:
            return self._open(match) is not None
        except Exception:                             # noqa: BLE001 -- not answering yet
            return False

    def ensure(self) -> bool:
        """Guarantee READY if possible. Idempotent: returns immediately when already READY;
        otherwise retries connect() up to `retries` times. A terminal FAULT is NOT auto-cleared
        (returns False immediately, without retrying) -- the operator must power-cycle the adapter
        and rebuild the link. Returns READY?"""
        if self.state == FAULT:
            return False
        if self.state == READY and self.analyzer is not None:
            return True
        for attempt in range(self.retries):
            if attempt and self.backoff_s:
                time.sleep(self.backoff_s)
            self.connect()
            if self.state == READY:
                return True
            if self.state == FAULT:                   # escalated mid-retry -> stop, stay terminal
                return False
        return False

    def read_sweep(self, n_points: int = 601, settle_s: float = 0.0) -> tuple:
        """Pull one swept trace. Raises LinkNotReady if not READY; on a transport
        error drops the link to DISCONNECTED and raises LinkDropped (the caller
        ensure()s to auto-reconnect)."""
        if self.state != READY or self.analyzer is None:
            raise LinkNotReady(f"link not READY (state={self.state}: {self.reason})")
        try:
            trace = self.analyzer.sweep_trace(self.span[0], self.span[1], n_points, settle_s)
        except Exception as e:
            self._on_drop(e)
            raise LinkDropped(str(e))
        self._consec_fail = 0                   # a successful bus op clears the failure streak
        return trace

    def read_via(self, fn):
        """Run fn(analyzer) under the SAME ready-check + drop-to-DISCONNECTED envelope
        as read_sweep, so any bus op (a stepped-CW point read, an option query) gets
        transparent auto-reconnect on the next ensure(). Raises LinkNotReady if not
        READY; on a transport error drops the link and raises LinkDropped."""
        if self.state != READY or self.analyzer is None:
            raise LinkNotReady(f"link not READY (state={self.state}: {self.reason})")
        try:
            result = fn(self.analyzer)
        except LinkDropped:
            raise
        except Exception as e:
            self._on_drop(e)
            raise LinkDropped(str(e))
        self._consec_fail = 0                   # a successful bus op clears the failure streak
        return result

    def read_point(self, f_hz: float, settle_s: float = 0.0) -> tuple:
        """One zero-span CW power read (marker peak) with auto-reconnect -- the
        stepped-CW analogue of read_sweep."""
        return self.read_via(lambda a: a.measure_peak(f_hz, settle_s))

    def status(self) -> LinkStatus:
        d = self.device
        return LinkStatus(
            state=self.state,
            detected=self.state in (READY, INVALID, FAILED, FAULT) and d is not None,
            valid=self.state == READY,
            model=(d.model if d else ""),
            serial=(d.serial if d else ""),
            address=(d.address if d else ""),
            transport=(d.transport if d else ""),
            reason=self.reason,
            reconnects=self.reconnects)

    def close(self) -> None:
        self._teardown_analyzer()
        self.state, self.device, self.reason = DISCONNECTED, None, "closed"

    def _teardown_analyzer(self) -> None:
        if self.analyzer is not None:
            try:
                self.analyzer.close()
            except Exception:
                pass
        self.analyzer = None

    # -- failure accounting + terminal FAULT ------------------------------------

    def _on_drop(self, exc) -> None:
        """A bus op raised mid-acquisition: drop to DISCONNECTED, tear the driver down, mark this
        as a real drop (so the next successful connect counts as a reconnect), and account the
        failure -- which may escalate the link to terminal FAULT after fault_after in a row."""
        self.state, self.reason = DISCONNECTED, f"link dropped: {exc}"
        self._teardown_analyzer()
        self._dropped = True
        self._bump_failure(exc)

    def _bump_failure(self, exc=None) -> None:
        """Count one consecutive bus-op failure. Escalate to terminal FAULT on an ADAPTER_WEDGED
        verdict immediately, or after fault_after consecutive failures. A FAULT carries an
        actionable message and overrides the just-set intermediate (DISCONNECTED/FAILED) state."""
        self._consec_fail += 1
        if not (getattr(exc, "adapter_wedged", False) or self._consec_fail >= self.fault_after):
            return
        # About to declare terminal FAULT. 44.2: if a local-qemu soft-recover hook is wired, attempt ONE
        # supervised recovery first (it de-keys the source, then QMP virtual-replugs up to its budget and
        # revalidates). On success, clear the failure streak and drop to DISCONNECTED so the next ensure()
        # reconnects (counted as a reconnect); on failure -> terminal FAULT (44.3 HARD alert). _recovering
        # blocks re-entrancy so a bus op inside recover_fn cannot recurse.
        if self._recover_fn is not None and not self._recovering:
            self._recovering = True
            try:
                recovered = bool(self._recover_fn(exc))
            except Exception:                             # noqa: BLE001 -- a failed recovery -> FAULT
                recovered = False
            finally:
                self._recovering = False
            if recovered:
                self._consec_fail = 0
                self._dropped = True                      # the next READY counts as a reconnect
                self.state = DISCONNECTED
                self.reason = "soft-recovered (QMP virtual-replug) -- revalidating"
                return
        self.state = FAULT
        self.reason = self._fault_message(self._consec_fail, exc)

    def _fault_message(self, tries, exc=None) -> str:
        """Actionable FAULT reason: which role/model/pad stopped answering, and what to do."""
        d = self.device
        model = (d.model if d and d.model else self.expected.label)
        addr = (d.address if d else "") or ""
        pad = addr.rsplit(":", 1)[-1] if addr.lower().startswith("net:") else addr
        where = f"pad {pad}" if pad else ""
        verdict = getattr(exc, "verdict", "") or ""
        tag = f" [{verdict}]" if verdict else ""
        msg = (f"{self.role.upper()} {model} {where} not answering after {tries} tries{tag}: "
               f"power-cycle the {self.role} adapter (NI GPIB-USB-HS)")
        return " ".join(msg.split())              # collapse any doubled spaces from an empty pad


# The 68369A: 10 MHz - 40 GHz synthesized signal generator (NV = non-volatile front
# panel). Canonical TX source (doc 147 Path-1). model_token '68369' matches
# 'Anritsu 68369A/NV'; family_token '683' catches a wrong 683xx source so the UI can
# report DETECTED-but-INVALID rather than ABSENT. ExpectedAnalyzer's shape (model token +
# frequency capability window) is instrument-agnostic, so the source reuses it.
DEFAULT_68369A = ExpectedAnalyzer("68369", 10e6, 40e9, label="68369A", family_token="683")


class SourceLink(AnalyzerLink):
    """Self-managing link to the TX source (68369A) -- the SourceLink mirror of AnalyzerLink.

    The control plane treats RX and TX symmetrically, so a source reuses the IDENTICAL
    discover -> open -> validate -> auto-reconnect lifecycle; only the operations differ.
    read_sweep/read_point are analyzer-only (a source cannot pull a trace) and are disabled
    here. A source is driven by WRITES -- set_freq / set_power / rf_on / rf_off and the
    hardware list-sweep primitives -- each wrapped in write_via, which is the same
    ready-check + drop-to-DISCONNECTED envelope as read_via, so a bus error on any source
    op auto-reconnects on the next ensure()."""

    role = "tx"                             # this side's role (tx source) -> FAULT message wording

    @property
    def source(self):
        """The opened SignalGenerator driver (aliases the generic .analyzer slot)."""
        return self.analyzer

    def write_via(self, fn):
        """Run fn(source) under the ready-check + auto-reconnect envelope -- the source
        analogue of read_via. Raises LinkNotReady if not READY; on a transport error drops
        the link to DISCONNECTED and raises LinkDropped."""
        return self.read_via(fn)

    def idn(self):
        return self.write_via(lambda s: s.idn())

    def set_freq(self, f_hz: float):
        return self.write_via(lambda s: s.set_freq(f_hz))

    def set_power(self, p_dbm: float):
        return self.write_via(lambda s: s.set_power(p_dbm))

    def rf_on(self):
        return self.write_via(lambda s: s.rf_on())

    def rf_off(self):
        return self.write_via(lambda s: s.rf_off())

    def set_list_sweep(self, freqs_hz, dwell_s: float = 0.0):
        return self.write_via(lambda s: s.set_list_sweep(freqs_hz, dwell_s))

    def arm_sweep(self):
        return self.write_via(lambda s: s.arm_sweep())

    def trigger_point(self):
        return self.write_via(lambda s: s.trigger_point())

    def read_sweep(self, *a, **k):
        raise LinkNotReady("SourceLink is a TX source: use set_freq/set_power/rf_on, "
                           "not read_sweep (a source cannot pull a trace)")

    def read_point(self, *a, **k):
        raise LinkNotReady("SourceLink is a TX source: use set_freq/set_power, "
                           "not read_point")
