"""control_lease.py -- the ONE client-side control primitive over a Link (Phase 3, client lane).

A ControlLease is the SINGLE home of lease ACQUISITION (L) + KEEPALIVE renewal (K at ttl/3) +
RELEASE (U) for one auto-reconnecting Link. It replaces the bespoke lease+keepalive code that
previously lived, DUPLICATED, in Coordinator and InstrumentHub -- fixing the silent lease lapse
(the hub leased once and never renewed, so control was lost after the TTL) and giving both call
sites IDENTICAL L/K/U wire behavior.

Phase-1 canonical design: exclusive control is a bus lease when the instrument is NETWORKED
(NetworkTransport.lease / renew_lease / release_lease). It is a full NO-OP for a SIM / local-VISA
link, which has no lease surface -- so the same object runs hardware-free in tests and arbitrated
on the real bench. An OBSERVER never constructs a ControlLease (it holds no lease -> reads only).

Transport RE-RESOLUTION: the transport is looked up from the link on EVERY tick (never captured),
so a link auto-reconnect -- which swaps in a fresh NetworkTransport (that itself re-sends L in
_connect) -- is followed, and the keepalive renews the CURRENT socket, never a dead one.
"""
from __future__ import annotations

import threading
import time


def _transport(link):
    """The bus transport under a link's opened driver, or None. Real drivers store it as `.t`;
    sim / local-VISA drivers have none. Re-resolved on every use so a reconnect swap is followed."""
    return getattr(getattr(link, "analyzer", None), "t", None)


# Default exclusive-lease TTL. Sized for a SINGLE operator + the dead-man de-key: short enough that a
# crashed/partitioned controller's lease frees (and the operator can reclaim) quickly, long enough that
# a HEALTHY holder never lapses mid-op. The keepalive renews at TTL/3 (= 20 s here), and a renew K can
# be delayed behind the longest blocking bus op (peak_preselector ~30 s), so TTL must exceed
# keepalive + ~30 s + LAN jitter -- 60 s clears that with margin. (Was 120 s.) NOTE: the de-key LATENCY
# on a silent partition is governed by the bridge's idle_s, NOT this TTL (a TTL lapse alone never
# de-keys); see ni_gpib_server._DEFAULT_IDLE_S.
DEFAULT_LEASE_TTL_S = 60.0


class ControlLease:
    """Exclusive control of ONE Link's instrument: acquire (L) + keepalive (K at ttl/3) + release
    (U). The single home of lease renewal. Sim/VISA link (no transport / no .lease) -> full no-op:
    held() True, no thread, no wire traffic."""

    # bounded keepalive-join ceiling: ample to join a daemon renew thread that wakes on
    # stop.wait(interval); NOT scaled to the (up to 60 s) TTL, so a slow release never hangs.
    JOIN_TIMEOUT_S = 5.0

    def __init__(self, link, scope: str = "device", ttl_s: float = DEFAULT_LEASE_TTL_S,
                 heartbeat=None, heartbeat_timeout_s=None):
        self.link = link
        self.scope = scope
        self.ttl_s = float(ttl_s)
        self._interval = max(0.05, self.ttl_s / 3.0)   # keepalive renews at ttl/3
        self._held = False
        self._stop = None
        self._thread = None
        # UNATTENDED SAFETY (opt-in; default disabled = renew unconditionally, unchanged behavior):
        # `heartbeat` is a 0-arg callable returning the monotonic timestamp of the last main-loop tick
        # (or None), and `heartbeat_timeout_s` the max stale gap. When the loop goes stale the keepalive
        # STOPS renewing, so a HUNG client (main loop wedged while its keepalive thread is still fine)
        # lets the lease lapse + the socket idle out -> the bridge's dead-man de-keys the source instead
        # of holding it keyed forever. A None heartbeat return PAUSES the check (a legitimate operator
        # wait, e.g. shield insertion) so it never de-keys a deliberately-paused campaign.
        self._heartbeat = heartbeat
        self._hb_timeout = heartbeat_timeout_s
        self._stalled = False                           # observable: keepalive suppressed by a stale loop
        # set while release() is tearing down: an in-flight renew tick that outlives the join must
        # NOT re-acquire (L) after we send U, or it steals the lease back from the next taker.
        self._releasing = False

    # -- state ------------------------------------------------------------------
    def held(self) -> bool:
        return self._held

    # -- acquire / release ------------------------------------------------------
    def acquire(self):
        """Take the lease (L) + start the keepalive. Returns (ok, who|None); NEVER raises.
        Idempotent: a second acquire while held is a no-op (True, None). A sim/VISA link (no lease
        surface) is a no-op that reports control held. On a conflict returns (False, <who-holds-it>)."""
        if self._held:
            return (True, None)
        t = _transport(self.link)
        if t is None or not hasattr(t, "lease"):       # sim / local-VISA: no lease surface -> no-op
            self._held = True
            return (True, None)
        try:
            t.lease(scope=self.scope, ttl_s=self.ttl_s)
        except IOError as e:                            # conflict: another controller holds it
            return (False, self._who(e))
        self._held = True
        self._releasing = False                         # a fresh hold: renews may re-acquire again
        self._start_keepalive()
        return (True, None)

    def release(self):
        """Release the lease (U). Idempotent. STOP+JOIN the keepalive BEFORE sending U so a
        finishing renew can never re-grab the lease AFTER release and steal control back from the
        next taker (preserves the coordinator's original correct ordering)."""
        if not self._held:
            return
        self._releasing = True                          # gate any in-flight renew off re-acquiring
        self._stop_keepalive()                          # ordering: join the renewer BEFORE U
        t = _transport(self.link)
        try:
            if t is not None and hasattr(t, "release_lease"):
                t.release_lease()
        except Exception:                               # noqa: BLE001 -- best-effort
            pass
        self._held = False

    # -- keepalive --------------------------------------------------------------
    def _loop_stalled(self) -> bool:
        """True iff a heartbeat is configured AND the main loop has not ticked within
        heartbeat_timeout_s. An unset heartbeat/timeout or a None last-tick -> False (disabled, or
        PAUSED for a legitimate operator wait). When True the keepalive stops renewing so the
        dead-man can fire."""
        if self._heartbeat is None or self._hb_timeout is None:
            return False
        last = self._heartbeat()
        if last is None:
            return False
        return (time.monotonic() - last) > float(self._hb_timeout)

    def _renew_once(self) -> None:
        """One keepalive tick: renew (K) the lease on the RE-RESOLVED transport; if the renew
        raises (e.g. the lease lapsed while a bus read blocked past the TTL, or the transport just
        reconnected), RE-ACQUIRE it (L). Best-effort; a rival that already holds it leaves W/Q
        arbitration the enforcer. Directly callable (no thread) so tests drive it with no real sleep."""
        if self._releasing:                             # release() has begun -> do not renew/re-take
            return
        if self._loop_stalled():                        # HUNG main loop: STOP renewing so the lease
            self._stalled = True                        # lapses + the socket idles out -> the bridge's
            return                                       # dead-man de-keys the source (unattended safety)
        self._stalled = False
        t = _transport(self.link)
        if t is None:                                   # a mid-reconnect link has no transport yet
            return
        try:
            t.renew_lease(self.ttl_s)
        except Exception:                               # noqa: BLE001 -- lapsed/reconnected -> re-take
            if self._releasing:                         # release began WHILE we were renewing: an L
                return                                  # here would re-grab the lease AFTER our U
            try:
                t.lease(scope=self.scope, ttl_s=self.ttl_s)
            except Exception:                           # noqa: BLE001
                pass

    def _start_keepalive(self) -> None:
        self._stop_keepalive()
        t = _transport(self.link)
        if getattr(t, "renew_lease", None) is None:     # sim/VISA: nothing to keep alive -> no thread
            return
        stop = threading.Event()

        def run():
            while not stop.wait(self._interval):        # wake early when stop is set
                self._renew_once()

        self._stop = stop
        self._thread = threading.Thread(target=run, name="se299-lease-keepalive", daemon=True)
        self._thread.start()

    def _stop_keepalive(self) -> None:
        stop, thread = self._stop, self._thread
        self._stop = self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=self.JOIN_TIMEOUT_S)

    # -- who --------------------------------------------------------------------
    def _who(self, exc=None):
        """Best-effort human string for who currently holds the instrument (from the bridge lease
        table), else the conflict exception text, else a generic string."""
        t = _transport(self.link)
        try:
            if t is not None and hasattr(t, "lease_report"):
                rep = t.lease_report().strip()
                if rep and "no active leases" not in rep:
                    return rep.splitlines()[0]
        except Exception:                               # noqa: BLE001
            pass
        return (str(exc) if exc else "") or "controlled by another client"
