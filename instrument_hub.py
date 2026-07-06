"""The InstrumentHub and its pure Arbiter for the se299 bench GUI.

The Arbiter is a thread-free, I/O-free state machine that tracks which ENGINE internally drives
each instrument (rx analyzer / tx source) and performs one-level suspend/resume handoff. The hub
(added in a later task) wraps the existing Coordinator, leases each instrument on demand, keeps a
separate observer socket per device, and uses the Arbiter to arbitrate which engine drives.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import control_lease

INSTRUMENTS = ("rx", "tx")


class Arbiter:
    """Pure rx/tx internal-ownership state machine. An `engine` has suspend()/resume()/name.
    Acquiring an owned instrument suspends the prior owner and remembers it; releasing restores
    and resumes it (one level of preemption -- a manual engine preempted by the SE engine).

    Thread-safe: acquire() runs on an engine's background thread while release() can be driven from
    the main (Qt) thread via a mode suspend, so the owner map is guarded by a re-entrant lock. RLock
    (not Lock) because acquire() calls the prior owner's suspend(), which re-enters release() on the
    same thread -- a plain Lock would self-deadlock there."""

    def __init__(self):
        self._owner = {i: None for i in INSTRUMENTS}
        self._suspended = {i: None for i in INSTRUMENTS}
        self._lock = threading.RLock()

    def owner(self, instrument):
        with self._lock:
            return self._owner[instrument]

    def acquire(self, instrument, engine):
        with self._lock:
            cur = self._owner[instrument]
            if cur is engine:
                return
            if cur is not None:
                cur.suspend()
                self._suspended[instrument] = cur
            self._owner[instrument] = engine

    def release(self, instrument, engine):
        with self._lock:
            if self._owner[instrument] is not engine:
                return
            prior = self._suspended[instrument]
            self._suspended[instrument] = None
            self._owner[instrument] = prior
            if prior is not None:
                prior.resume()

    def snapshot(self):
        """Owner + suspended-prior engine NAME per instrument (None = free). For a debug pane / an
        invariant check; a pure read under the lock so it can't tear against a concurrent handoff."""
        with self._lock:
            return {i: {"owner": getattr(self._owner[i], "name", None),
                        "suspended_prior": getattr(self._suspended[i], "name", None)}
                    for i in INSTRUMENTS}


def _transport(link):
    """The bus transport under a link's opened driver (see control_lease._transport)."""
    return getattr(getattr(link, "analyzer", None), "t", None)


class InstrumentHub:
    """Single owner of the two instrument connections for the bench. Wraps a Coordinator (its
    rx/tx Links), leases each instrument ON DEMAND (first acquire) and holds until shutdown(),
    and arbitrates which engine drives via an Arbiter. Lease failures (external client holds the
    instrument) are reported as a human string, never raised past acquire()."""

    def __init__(self, coord, lease_ttl_s=control_lease.DEFAULT_LEASE_TTL_S):
        self._coord = coord
        self._ttl = float(lease_ttl_s)
        self._arb = Arbiter()
        self._link = {"rx": coord.rx, "tx": coord.tx}
        # ONE ControlLease per instrument (Phase 3): the SINGLE home of lease + keepalive + release.
        # This FIXES the silent lease lapse -- the old hub leased once and NEVER renewed, so control
        # was lost after the TTL; a ControlLease renews at ttl/3 for as long as the hub holds it.
        self._lease = {i: control_lease.ControlLease(self._link[i], ttl_s=self._ttl)
                       for i in INSTRUMENTS}

    @property
    def analyzer(self):
        return self._coord.analyzer

    @property
    def source(self):
        return self._coord.source

    def ensure_ready(self):
        return self._coord.ensure_ready()

    def _t(self, instrument):
        return _transport(self._link[instrument])

    def _link_reason(self, instrument):
        """The link's actionable reason string (FAULT "power-cycle the <role> adapter" vs ABSENT),
        or "" if the link does not expose a status()."""
        st = getattr(self._link[instrument], "status", None)
        if callable(st):
            try:
                return st().reason or ""
            except Exception:
                return ""
        return ""

    def acquire(self, instrument, engine):
        if not self._link[instrument].ensure():
            # surface the LINK's actionable reason (e.g. terminal FAULT "power-cycle the <role>
            # adapter") instead of a generic string, so the operator sees WHAT is wrong + what to do.
            return (False, self._link_reason(instrument) or f"{instrument} not ready (bridge unreachable)")
        ok, who = self._lease[instrument].acquire()      # ControlLease: leases + keeps it alive
        if not ok:
            return (False, who)
        self._arb.acquire(instrument, engine)
        return (True, None)

    def link_status(self, instrument):
        """The current LinkStatus (state + reason) for a proactive status probe -- READY / ABSENT /
        FAULT / INVALID / DISCONNECTED. Does NOT lease or take control: safe to poll on a timer so
        the GUI status strip can show both units live/absent/fault at open, before any acquire.
        Returns None for a minimal duck-typed link with no status()."""
        st = getattr(self._link[instrument], "status", None)
        return st() if callable(st) else None

    def acquire_both(self, engine, instruments=INSTRUMENTS):
        for inst in instruments:
            if not self._link[inst].ensure():
                return (False, f"{inst} not ready (bridge unreachable)")
        newly = []
        for inst in instruments:
            was = self._lease[inst].held()
            ok, who = self._lease[inst].acquire()
            if not ok:
                for n in newly:                          # all-or-nothing: roll back what THIS call took
                    self._lease[n].release()
                return (False, who)
            if not was:
                newly.append(inst)
        for inst in instruments:
            self._arb.acquire(inst, engine)
        return (True, None)

    def release(self, instrument, engine):
        self._arb.release(instrument, engine)

    def owner(self, instrument):
        """The engine object currently driving `instrument` (None = free). Identity lets a caller
        that knows the modes map ownership back to a specific tab for an invariant check."""
        return self._arb.owner(instrument)

    def state_snapshot(self):
        """Per-unit lifecycle state for invariant checks and a debug pane: which engine drives it,
        whether the bridge lease is held, and the link's health state. Pure reads -- takes no lease
        and no control, so it is safe to poll on a timer."""
        snap = {}
        for inst in INSTRUMENTS:
            ls = self.link_status(inst)
            snap[inst] = {"owner": getattr(self._arb.owner(inst), "name", None),
                          "lease_held": self._lease[inst].held(),
                          "link_state": getattr(ls, "state", None)}
        return snap

    def sessions_report(self, instrument):
        t = self._t(instrument)
        return t.sessions_report() if t is not None and hasattr(t, "sessions_report") else ""

    def lease_report(self, instrument):
        t = self._t(instrument)
        return t.lease_report() if t is not None and hasattr(t, "lease_report") else ""

    def shutdown(self):
        for inst in INSTRUMENTS:
            self._lease[inst].release()                  # stops each keepalive, then releases (U)
