"""Pure unit tests for the bridge SessionRegistry (no socket, no bridge): register a session,
announce its identity (X), bind a pad (A), acquire a lease (L), and confirm the joined S-verb
report line; unregister empties it; reset() isolates. Mirrors the LeaseRegistry test style.

Run:  uv run python -m pytest rf-se/se299/tests/test_sessions_registry.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gpib_bridge.ni_gpib_server import SessionRegistry, LeaseRegistry


def test_register_sets_defaults():
    s = SessionRegistry()
    s.register(1, "127.0.0.1:5140", now=100.0)
    rep = s.report()
    assert rep == "session 1 client - peer 127.0.0.1:5140 role - pad - lease -"


def test_set_client_parses_role_and_pad():
    s = SessionRegistry()
    s.register(1, "127.0.0.1:5140")
    s.set_client(1, "coordinator|host=h|pid=9|u=aaaa")
    s.set_pad(1, 18)
    line = s.report()
    assert "client coordinator|host=h|pid=9|u=aaaa" in line
    assert "role coordinator" in line and "pad 18" in line and "lease -" in line


def test_report_joins_lease_scope():
    s, leases = SessionRegistry(), LeaseRegistry()
    s.register(4, "127.0.0.1:5140")
    s.set_client(4, "coordinator|host=h|pid=9|u=aaaa")
    s.set_pad(4, 18)
    ok, _ = leases.acquire("BUS", 4, ttl=30.0)              # live lease (real monotonic clock)
    assert ok
    line = s.report(leases)                                 # report joins with real-time scope_for
    assert "session 4 " in line and "lease BUS" in line     # CONTROLLER: holds a lease


def test_observer_has_no_lease():
    s, leases = SessionRegistry(), LeaseRegistry()
    s.register(5, "127.0.0.1:5141")
    s.set_client(5, "devices|host=h|pid=1|u=bbbb")
    s.set_pad(5, 18)                                          # bound, but never leased
    line = s.report(leases)
    assert "role devices" in line and "lease -" in line      # OBSERVER: no lease


def test_multiple_sessions_sorted_by_sid():
    s = SessionRegistry()
    s.register(7, "b:2"); s.register(3, "a:1")
    lines = s.report().splitlines()
    assert lines[0].startswith("session 3 ") and lines[1].startswith("session 7 ")


def test_unregister_and_empty_report():
    s = SessionRegistry()
    assert s.report() == "no active sessions"
    s.register(1, "x:1")
    s.unregister(1)
    assert s.report() == "no active sessions"


def test_reset_clears():
    s = SessionRegistry()
    s.register(1, "x:1"); s.register(2, "y:2")
    s.reset()
    assert s.report() == "no active sessions"


def test_scope_for_join_key():
    leases = LeaseRegistry()
    assert leases.scope_for(9) is None
    leases.acquire(18, 9, ttl=30.0, now=0.0)
    assert leases.scope_for(9, now=0.0) == 18
    assert leases.scope_for(9, now=100.0) is None            # expired
