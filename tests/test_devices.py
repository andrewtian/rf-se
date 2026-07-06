"""Hardware-free tests for the `devices` verb: probing a network device (reachability + IDN via
the bridge lease table's R verb + the session table's S verb) and rendering the CLIENTS view
grouped by client (with the LOCAL client marked). Fake transports stand in for NetworkTransport,
so no bridge/hardware is needed.

Run:  uv run python -m pytest rf-se/se299/tests/test_devices.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cli
import drivers


class _FakeT:
    """A fake transport. `sessions` is the canned S-verb report; when None (the default) the fake
    behaves like an OLD bridge with no S verb -- sessions_report() raises IOError."""

    def __init__(self, report, idn="HP8565E,001,006,007,008", sessions=None):
        self._report, self._idn, self._sessions = report, idn, sessions
        self.closed = False

    def lease_report(self):
        return self._report

    def sessions_report(self):
        if self._sessions is None:
            raise IOError("gpib bridge error: unknown verb 'S'")   # old bridge
        return self._sessions

    def query(self, cmd):
        return self._idn

    def write(self, cmd):
        pass

    def close(self):
        self.closed = True


# ------------------------------------------------------------------ _probe_device (R + S)

def test_probe_free_bus_reads_idn(monkeypatch):
    ft = _FakeT("no active leases")
    monkeypatch.setattr(drivers, "make_transport", lambda a, **k: ft)
    d = cli._probe_device("rx", "net:host:5555:18")
    assert d["reachable"] is True
    assert d["holders"] == []                                # bus FREE
    assert "HP8565E" in d["idn"]                             # IDN read when free
    assert ft.closed is True                                 # transport always closed
    assert d["sessions_supported"] is False                 # old-bridge fake has no S verb


def test_probe_leased_bus_reports_holder_skips_idn(monkeypatch):
    monkeypatch.setattr(drivers, "make_transport",
                        lambda a, **k: _FakeT("session 29 scope BUS ttl 80.0s"))
    d = cli._probe_device("rx", "net:host:5555:18")
    assert d["reachable"] is True
    assert d["holders"] == ["29"]                            # controller session parsed
    assert "leased" in d["idn"].lower()                      # IDN skipped while leased


def test_probe_multiple_leases_parsed(monkeypatch):
    rep = "session 3 scope BUS ttl 60.0s\nsession 7 scope 18 ttl 30.0s"
    monkeypatch.setattr(drivers, "make_transport", lambda a, **k: _FakeT(rep))
    d = cli._probe_device("tx", "net:host:5556:5")
    assert d["holders"] == ["3", "7"]


def test_probe_parses_session_table(monkeypatch):
    srep = ("session 4 client coordinator|host=h|pid=9|u=aaaa peer 127.0.0.1:5140 "
            "role coordinator pad 18 lease BUS\n"
            "session 5 client devices|host=h|pid=1|u=bbbb peer 127.0.0.1:5141 "
            "role devices pad 18 lease -")
    monkeypatch.setattr(drivers, "make_transport",
                        lambda a, **k: _FakeT("session 4 scope BUS ttl 80.0s", sessions=srep))
    d = cli._probe_device("rx", "net:host:5555:18")
    assert d["sessions_supported"] is True
    assert [s["role"] for s in d["sessions"]] == ["coordinator", "devices"]
    assert d["sessions"][0]["lease"] == "BUS" and d["sessions"][1]["lease"] == "-"
    assert d["sessions"][0]["client_id"].endswith("u=aaaa")


def test_probe_non_network_addr():
    d = cli._probe_device("rx", "sim")                       # sim/VISA are not network devices
    assert d["reachable"] is False and "non-network" in d["idn"]


def test_parse_session_line_fields():
    s = cli._parse_session_line(
        "session 7 client se-gui|host=h|pid=3|u=cccc peer 10.0.0.2:6000 role se-gui pad 5 lease 5")
    assert s == {"sid": "7", "client_id": "se-gui|host=h|pid=3|u=cccc", "peer": "10.0.0.2:6000",
                 "role": "se-gui", "pad": "5", "lease": "5"}
    assert cli._parse_session_line("no active sessions") is None


# ------------------------------------------------------------------ cmd_devices (grouped)

def test_devices_groups_sessions_by_client_and_marks_local(monkeypatch, capsys):
    # a coordinator client (u=aaaa) controls BOTH devices; our devices query (u=OURU) observes both.
    def sess(cid, role, pad, lease, sid, peer="127.0.0.1:1"):
        return {"sid": sid, "client_id": cid, "peer": peer, "role": role, "pad": pad, "lease": lease}

    coord = "coordinator|host=h|pid=9|u=aaaa"
    ours = "devices|host=h|pid=1|u=OURU"

    def fake_probe(kind, addr, our_cid=None):
        if kind == "rx":
            return {"kind": "rx", "addr": addr, "reachable": True, "idn": "(leased -- IDN skipped)",
                    "leases": "session 4 scope BUS ttl 80s", "holders": ["4"], "model": "8565EC",
                    "sessions_supported": True,
                    "sessions": [sess(coord, "coordinator", "18", "BUS", "4"),
                                 sess(ours, "devices", "18", "-", "9")]}
        return {"kind": "tx", "addr": addr, "reachable": True, "idn": "(leased -- IDN skipped)",
                "leases": "session 6 scope BUS ttl 80s", "holders": ["6"], "model": "68367C",
                "sessions_supported": True,
                "sessions": [sess(coord, "coordinator", "5", "BUS", "6"),
                             sess(ours, "devices", "5", "-", "11")]}
    monkeypatch.setattr(cli, "_probe_device", fake_probe)
    monkeypatch.setattr(drivers, "client_id", lambda role=None: ours)

    class A:
        analyzer = "net:h:5555:18"; source = "net:h:5556:5"; device = None; discover = False
    rc = cli.cmd_devices(A())
    out = capsys.readouterr().out
    assert rc == 0
    assert "NETWORK DEVICES (2" in out
    # grouped-by-client view (not the old per-session view)
    assert "grouped by client" in out
    # coordinator appears ONCE, controlling both instruments
    assert out.count("[coordinator]") == 1
    assert "CONTROLS: rx:8565EC, tx:68367C" in out
    # our own query is marked LOCAL and observes both
    assert "[devices]" in out and "(LOCAL -- this devices query)" in out
    assert "OBSERVES: rx:8565EC, tx:68367C" in out
    assert "2 client(s) connected (1 controlling)" in out


def test_devices_falls_back_to_lease_only_when_no_session_support(monkeypatch, capsys):
    # regression guard: bridges with no S verb -> the old R-only CLIENTS view (unchanged)
    def fake_probe(kind, addr, our_cid=None):
        if kind == "rx":
            return {"kind": "rx", "addr": addr, "reachable": True, "idn": "(leased -- IDN skipped)",
                    "leases": "session 29 scope BUS ttl 80s", "holders": ["29"], "model": "8565EC",
                    "sessions_supported": False, "sessions": []}
        return {"kind": "tx", "addr": addr, "reachable": True, "idn": "ANRITSU,68367C", "leases": "",
                "holders": [], "model": "68367C", "sessions_supported": False, "sessions": []}
    monkeypatch.setattr(cli, "_probe_device", fake_probe)

    class A:
        analyzer = "net:h:5555:18"; source = "net:h:5556:5"; device = None; discover = False
    rc = cli.cmd_devices(A())
    out = capsys.readouterr().out
    assert rc == 0
    assert "NETWORK DEVICES (2" in out
    assert "LEASED by session 29" in out and "FREE (open)" in out
    assert "session 29" in out and "CONTROLLER" in out and "OBSERVER" in out
    assert "1 controller client(s) + 1 observer" in out
