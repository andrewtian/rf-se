"""Hardware-free tests for the network GPIB bridge (the M1 client-server path).

Spins up ni_gpib_server in --fake mode in a background thread and drives it end to end
through drivers.NetworkTransport and the real Agilent856xEC driver -- proving the wire
protocol, the transport, and the driver stack all round-trip over TCP with no GPIB
hardware. The only thing NOT exercised here is the linux-gpib backend + USB passthrough,
which live in the VM (see gpib_bridge/README.md).

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_net_transport.py -q
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drivers
from gpib_bridge import ni_gpib_server, protocol


def _start_fake_server(token=None):
    """A fake-backend bridge on an ephemeral port; returns the port. Daemon thread
    keeps serving sequential connections until the test process exits."""
    srv = ni_gpib_server.listen("127.0.0.1", 0)
    port = srv.getsockname()[1]
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "token": token}, daemon=True).start()
    return port


# ----------------------------------------------------------------- protocol

def test_protocol_roundtrip_all_verbs_and_binary():
    cases = [("A", b"18"), ("W", b"SNGLS"), ("Q", b"TRA?"), ("T", b"5000"),
             ("C", b""), ("+", b""), ("=", b"-90.0,-91.5,-88.2"), ("!", b"boom"),
             ("W", b",;\n\r ")]                          # bytes that would break naive framing
    for token, payload in cases:
        line = protocol.encode(token, payload)
        assert line.endswith(b"\n")
        assert protocol.decode(line) == (token, payload)


def test_protocol_blank_line():
    assert protocol.decode(b"\n") == ("", b"")


# ----------------------------------------------------------------- address parsing

def test_parse_net_addr():
    assert drivers.parse_net_addr("net:192.168.64.5:5555:18") == ("192.168.64.5", 5555, 18)
    assert drivers.parse_net_addr("NET:ubuntu.local:5025:3") == ("ubuntu.local", 5025, 3)
    for bad in ("GPIB0::18::INSTR", "net:host:port", "net::5555:18", "sim"):
        with pytest.raises(ValueError):
            drivers.parse_net_addr(bad)


def test_make_transport_routes_net_to_network_transport():
    port = _start_fake_server()
    t = drivers.make_transport(f"net:127.0.0.1:{port}:18")
    assert isinstance(t, drivers.NetworkTransport)
    t.close()


def test_make_transport_non_net_uses_visa(monkeypatch=None):
    # pyvisa is absent in this env, so the VISA path raises RuntimeError (never returns
    # a NetworkTransport) -- proving a plain VISA string does NOT route to the bridge.
    with pytest.raises(Exception):
        drivers.make_transport("GPIB0::18::INSTR")


# ----------------------------------------------------------------- transport over TCP

def test_network_transport_query_write_timeout_close():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18, timeout_ms=3000)
    assert "8565E" in t.query("ID?")
    t.write("SNGLS")                                     # no exception -> server acked +
    t.set_timeout(5000)                                  # no exception
    t.close()


def test_agilent_driver_over_network_bridge_idn_and_trace():
    port = _start_fake_server()
    an = drivers.Agilent856xEC(drivers.NetworkTransport("127.0.0.1", port, 18))
    assert "8565E" in an.idn()
    freqs, levels = an.read_trace("A")                   # TDF P + TRA? + FA?/FB? over TCP
    assert len(levels) == 601 == len(freqs)
    assert freqs[0] == pytest.approx(1e9) and freqs[-1] == pytest.approx(6e9)
    mkf, amp = an.marker_peak()                          # MKPK HI + MKA? + MKF?
    assert amp == pytest.approx(-42.5) and mkf == pytest.approx(2.45e9)
    an.close()


def test_bridge_error_reply_becomes_ioerror():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    # reach into the socket and send an unknown verb -> server replies '!' -> IOError
    with pytest.raises(IOError):
        t._txn("Z", b"x")
    t.close()


def test_server_one_client_at_a_time_sequential_connections():
    port = _start_fake_server()
    for _ in range(3):                                   # sequential clients all succeed
        t = drivers.NetworkTransport("127.0.0.1", port, 18)
        assert "8565E" in t.query("*IDN?")
        t.close()


def test_close_is_idempotent_and_survives_dead_socket():
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)
    t.close()
    t.close()                                            # second close must not raise


# ----------------------------------------------------------------- authentication

def test_auth_required_rejects_client_without_token(monkeypatch):
    monkeypatch.delenv("NI_GPIB_TOKEN", raising=False)
    port = _start_fake_server(token="s3cret")
    with pytest.raises(IOError):                          # bus op before auth -> refused
        drivers.NetworkTransport("127.0.0.1", port, 18)


def test_auth_accepts_correct_token():
    port = _start_fake_server(token="s3cret")
    t = drivers.NetworkTransport("127.0.0.1", port, 18, token="s3cret")
    assert "8565E" in t.query("ID?")
    t.close()


def test_auth_rejects_wrong_token():
    port = _start_fake_server(token="s3cret")
    with pytest.raises(IOError):
        drivers.NetworkTransport("127.0.0.1", port, 18, token="not-it")


def test_token_from_env_is_used(monkeypatch):
    monkeypatch.setenv("NI_GPIB_TOKEN", "envtoken")
    port = _start_fake_server(token="envtoken")
    t = drivers.NetworkTransport("127.0.0.1", port, 18)   # no token arg -> reads env
    assert "8565E" in t.query("*IDN?")
    t.close()


def test_open_server_accepts_client_that_offers_a_token():
    # a loopback dev server with no token still accepts a client that sends H (no-op)
    port = _start_fake_server(token=None)
    t = drivers.NetworkTransport("127.0.0.1", port, 18, token="whatever")
    assert "8565E" in t.query("ID?")
    t.close()


def test_server_refuses_nonloopback_bind_without_token(monkeypatch):
    monkeypatch.delenv("NI_GPIB_TOKEN", raising=False)
    with pytest.raises(SystemExit):                       # fail-closed argparse guard
        ni_gpib_server.main(["--host", "0.0.0.0", "--port", "0", "--fake", "--one-shot"])


# ----------------------------------------------------- client identity + session table (X/S)

def test_client_id_appears_in_session_report():
    port = _start_fake_server()
    cid = "coordinator|host=h|pid=9|u=aaaa"
    t = drivers.NetworkTransport("127.0.0.1", port, 18, client_id=cid)   # X announced on connect
    rep = t.sessions_report()                            # S verb over the real bridge
    assert f"client {cid}" in rep
    assert "pad 18" in rep and "lease -" in rep          # bound observer, no lease yet
    t.lease()                                            # become a CONTROLLER
    rep2 = t.sessions_report()
    assert "lease 18" in rep2                            # the same session now holds a device lease
    t.close()


def test_two_clients_distinct_sessions():
    port = _start_fake_server()
    a = drivers.NetworkTransport("127.0.0.1", port, 18, client_id="se-gui|host=h|pid=1|u=aaaa")
    b = drivers.NetworkTransport("127.0.0.1", port, 18, client_id="devices|host=h|pid=2|u=bbbb")
    rep = b.sessions_report()
    assert "u=aaaa" in rep and "u=bbbb" in rep           # both sessions visible to either client
    assert rep.count("session ") == 2
    a.close(); b.close()


def test_session_drops_on_disconnect():
    port = _start_fake_server()
    a = drivers.NetworkTransport("127.0.0.1", port, 18, client_id="wall|host=h|pid=1|u=aaaa")
    b = drivers.NetworkTransport("127.0.0.1", port, 18, client_id="devices|host=h|pid=2|u=bbbb")
    assert "u=aaaa" in b.sessions_report()
    a.close()
    # a's session must disappear from the table (bridge unregisters on disconnect)
    import time
    for _ in range(50):
        if "u=aaaa" not in b.sessions_report():
            break
        time.sleep(0.02)
    assert "u=aaaa" not in b.sessions_report()
    b.close()


def test_x_verb_is_backward_compatible_no_client_id():
    # a transport with NO client_id sends no X and never calls S -> identical to legacy traffic
    port = _start_fake_server()
    t = drivers.NetworkTransport("127.0.0.1", port, 18)  # client_id defaults None
    assert "8565E" in t.query("ID?")                     # ordinary ops unaffected
    # but S is still answerable by the (new) fake bridge, listing the anonymous session
    rep = t.sessions_report()
    assert "client -" in rep and "pad 18" in rep
    t.close()
