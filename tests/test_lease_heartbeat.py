"""Unattended-safety heartbeat: a HUNG main loop (its keepalive thread still fine) must STOP renewing
so the lease lapses + the socket idles out -> the bridge's dead-man de-keys the source. Opt-in; default
disabled = renew unconditionally (unchanged). All hardware-free (fake transport records renew/lease).

Run:  uv run python -m pytest rf-se/se299/tests/test_lease_heartbeat.py -q
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import control_lease
import coordinator


class FakeT:
    def __init__(self):
        self.renews = 0
        self.leases = 0

    def lease(self, scope, ttl_s):
        self.leases += 1

    def renew_lease(self, ttl_s):
        self.renews += 1

    def release_lease(self):
        pass


class FakeLink:
    def __init__(self, t):
        self.analyzer = type("A", (), {"t": t})()          # _transport(link) -> link.analyzer.t


# ---- ControlLease heartbeat mechanism -------------------------------------------------------

def test_no_heartbeat_always_renews():
    t = FakeT()
    cl = control_lease.ControlLease(FakeLink(t))            # default: heartbeat disabled
    cl._renew_once()
    assert t.renews == 1 and not cl._stalled               # unchanged behavior


def test_fresh_heartbeat_renews():
    t = FakeT()
    cl = control_lease.ControlLease(FakeLink(t), heartbeat=lambda: time.monotonic(),
                                    heartbeat_timeout_s=1.0)
    cl._renew_once()
    assert t.renews == 1 and not cl._stalled


def test_stale_heartbeat_suppresses_renew():
    # main loop hung ~1000 s -> STOP renewing so the lease lapses -> dead-man de-keys the source
    t = FakeT()
    cl = control_lease.ControlLease(FakeLink(t), heartbeat=lambda: time.monotonic() - 1000.0,
                                    heartbeat_timeout_s=1.0)
    cl._renew_once()
    assert t.renews == 0 and cl._stalled


def test_none_heartbeat_is_paused_not_stalled():
    # a None last-tick = PAUSED (e.g. shield insertion) -> keep control, never de-key a paused campaign
    t = FakeT()
    cl = control_lease.ControlLease(FakeLink(t), heartbeat=lambda: None, heartbeat_timeout_s=1.0)
    cl._renew_once()
    assert t.renews == 1 and not cl._stalled


def test_heartbeat_recovers_after_transient_stall():
    # a stall that recovers before de-key resumes renewing (the loop un-hangs)
    box = {"ts": time.monotonic() - 1000.0}                # stale
    t = FakeT()
    cl = control_lease.ControlLease(FakeLink(t), heartbeat=lambda: box["ts"], heartbeat_timeout_s=1.0)
    cl._renew_once()
    assert t.renews == 0 and cl._stalled
    box["ts"] = time.monotonic()                           # loop resumes
    cl._renew_once()
    assert t.renews == 1 and not cl._stalled


def test_releasing_beats_heartbeat_check():
    # release-in-progress still short-circuits BEFORE the stall check (ordering unchanged)
    t = FakeT()
    cl = control_lease.ControlLease(FakeLink(t), heartbeat=lambda: time.monotonic(),
                                    heartbeat_timeout_s=1.0)
    cl._releasing = True
    cl._renew_once()
    assert t.renews == 0                                    # releasing -> no renew, no re-take


# ---- Coordinator beat wiring ----------------------------------------------------------------

def test_coordinator_beat_and_pause_when_enabled():
    cfg = config.default()
    coord = coordinator.Coordinator(cfg, object(), object(), heartbeat_timeout_s=90.0)
    assert coord._last_beat is None                        # not yet beaten
    coord.beat()
    assert isinstance(coord._last_beat, float)             # liveness stamped
    coord._pause_beat()
    assert coord._last_beat is None                        # paused (shield insertion)
    # the heartbeat is wired into BOTH leases with the same timeout
    assert coord._rx_lease._hb_timeout == 90.0 and coord._tx_lease._hb_timeout == 90.0
    assert coord._rx_lease._heartbeat is not None and coord._tx_lease._heartbeat is not None


def test_coordinator_heartbeat_disabled_by_default():
    cfg = config.default()
    coord = coordinator.Coordinator(cfg, object(), object())   # no heartbeat_timeout_s
    coord.beat()
    assert coord._last_beat is None                        # beat() is a no-op when disabled
    assert coord._rx_lease._hb_timeout is None and coord._rx_lease._heartbeat is None
