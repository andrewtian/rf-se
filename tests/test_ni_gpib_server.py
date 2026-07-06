"""Server-side unit tests for the networked GPIB bridge (gpib_bridge/ni_gpib_server.py).

The real LinuxGpibBackend cannot run here (no linux-gpib on this Mac), so the ``gpib`` module
and the ``Gpib`` device class are INJECTED as a fake (`_FakeGpib`) and the pure logic is
exercised with no hardware:

  * short-read guard        -- END-bit / ibcnt inspection of a query readback
  * error classification    -- iberr -> DEVICE_SILENT / BUS_WEDGED / ADAPTER_WEDGED
  * recover state machine   -- ibclr -> ibonl off/on -> fresh handle -> probe -> verdict
  * ping liveness read      -- serial poll -> status byte or classified fault

Dispatcher-level tests drive the REAL serve_connection over a socket.socketpair() so the
wire framing is proven end to end (a short read returns '!' not '=', the additive Z verb
round-trips, the idle timeout ends a parked session).

Run:  uv run python -m pytest rf-se/se299/tests/test_ni_gpib_server.py -q
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gpib_bridge import ni_gpib_server, protocol


# --------------------------------------------------------------------------- fake gpib backend

class _FakeGpib:
    """Stand-in for the linux-gpib ``gpib`` C module AND the ``Gpib`` device class -- one
    instance plays both injection seams. Scripted by a terminal fault so every recover /
    short-read / classification path is reachable with no hardware."""

    ERR, TIMO, END = 0x8000, 0x4000, 0x2000

    def __init__(self, *, read=b"-90.0", read_ibsta=None, read_ibcnt=None,
                 poll=None, fault=None):
        self._read = read
        self._read_ibsta = self.END if read_ibsta is None else read_ibsta
        self._read_ibcnt = len(read) if read_ibcnt is None else read_ibcnt
        self._poll = poll                       # int -> device answers; None -> raise `fault`
        self._fault = fault or (0, 0, 0)        # (iberr, ibsta, ibcnt) applied on a failure
        self.iv, self.sv, self.cv = 0, 0, 0     # iberr / ibsta / ibcnt registers
        self.id = 7                             # device descriptor
        self.opens, self.clears, self.onlines, self.tmo = [], 0, [], None
        self.write_fault = False

    # --- module seam: gpib.iberr()/ibsta()/ibcnt()/clear()/online()/serial_poll() ---
    def iberr(self):
        return self.iv

    def ibsta(self):
        return self.sv

    def ibcnt(self):
        return self.cv

    def clear(self, ud):
        self.clears += 1

    def online(self, ud, val):
        self.onlines.append(val)

    def serial_poll(self, ud):
        if self._poll is not None:
            self.iv, self.sv, self.cv = 0, self.END, 1
            return self._poll
        self.iv, self.sv, self.cv = self._fault
        raise RuntimeError("serial poll failed")

    # --- device (Gpib class) seam: gpib_cls(board, pad=..) -> handle with write/read/... ---
    def __call__(self, board, pad=None):
        self.opens.append((board, pad))
        return self

    def write(self, data):
        if self.write_fault:
            self.iv, self.sv, self.cv = self._fault
            raise RuntimeError("write failed")
        self._last = data

    def read(self, n):
        self.iv, self.sv, self.cv = 0, self._read_ibsta, self._read_ibcnt
        return self._read

    def timeout(self, code):
        self.tmo = code

    def close(self):
        pass


def _linux_backend(**kw):
    """A LinuxGpibBackend wired to a fresh _FakeGpib, already bound to pad 18."""
    g = _FakeGpib(**kw)
    be = ni_gpib_server.LinuxGpibBackend(board=0, gpib=g, gpib_cls=g)
    be.bind(18)
    return be, g


# --------------------------------------------------------------------------- dispatcher driver

def _drive(backend, requests, token=None, idle_s=None):
    """Run the REAL serve_connection on one end of a socketpair; send framed `requests`
    (list of (verb, payload_bytes)); return the decoded (status, data) replies (one per
    request -- every verb used here answers with exactly one line)."""
    cli, srv = socket.socketpair()
    cli.settimeout(3.0)                                   # never hang the test on a missing reply
    kwargs = {"token": token}
    if idle_s is not None:
        kwargs["idle_s"] = idle_s
    th = threading.Thread(target=ni_gpib_server.serve_connection,
                          args=(srv, backend), kwargs=kwargs, daemon=True)
    th.start()
    f = cli.makefile("rwb")
    replies = []
    try:
        for verb, payload in requests:
            f.write(protocol.encode_request(verb, payload))
            f.flush()
            replies.append(protocol.decode_reply(f.readline()))
    finally:
        f.close()
        cli.close()
        th.join(timeout=2.0)
    return replies


# =========================================================================== pure logic

def test_read_is_short_pure():
    E = ni_gpib_server._IBSTA_END
    assert ni_gpib_server._read_is_short(E, 0) is True       # END set but zero bytes -> corrupt
    assert ni_gpib_server._read_is_short(0, 10) is True      # bytes but END never asserted
    assert ni_gpib_server._read_is_short(E, 10) is False     # END + data -> a complete readback


def test_classify_fault_pure():
    c = ni_gpib_server._classify_fault
    assert c(6, 0, 0) == "DEVICE_SILENT"                     # EABO
    assert c(2, 0, 0) == "BUS_WEDGED"                        # ENOL
    assert c(0, 0, 110) == "ADAPTER_WEDGED"                  # EDVR (errno in ibcnt)
    assert c(99, 0, 0) == "FAULT"                            # anything else


def test_classify_fault_without_iberr_uses_ibsta_ibcnt():
    # LIVE REGRESSION: this guest's linux-gpib build has NO gpib.iberr(), so _status reports iberr=0
    # for EVERY fault. iberr==0 also == EDVR, so the iberr-only map collapsed every fault to
    # ADAPTER_WEDGED -- a powered-off instrument then told the operator to power-cycle the NI adapter.
    # With iberr unavailable (0), classify from the ibsta/ibcnt bits that ARE reported.
    c = ni_gpib_server._classify_fault
    E, T = ni_gpib_server._IBSTA_ERR, ni_gpib_server._IBSTA_TIMO
    # no-listener (instrument OFF/absent): live-observed ibsta=0x8000 (ERR, no TIMO), ibcnt=0
    assert c(0, E, 0) == "BUS_WEDGED"                        # ENOL, NOT ADAPTER_WEDGED
    # timeout (device present, silent): TIMO set
    assert c(0, E | T, 0) == "DEVICE_SILENT"                # EABO-like
    assert c(0, T, 0) == "DEVICE_SILENT"
    # genuine adapter-gone: a real OS errno rides in ibcnt even with iberr==0
    assert c(0, E, 19) == "ADAPTER_WEDGED"                  # ENODEV
    assert c(0, E, 110) == "ADAPTER_WEDGED"                 # ETIMEDOUT
    # bare EDVR with no diagnostic bits -> still adapter (unchanged conservative default)
    assert c(0, 0, 0) == "ADAPTER_WEDGED"


# =========================================================================== short-read guard (fix 1)

def test_query_short_read_no_end_bit_raises():
    be, g = _linux_backend(read=b"garbage", read_ibsta=0, read_ibcnt=7)   # bytes, but END unset
    with pytest.raises(ni_gpib_server.GpibFault) as ei:
        be.query(b"TRA?")
    assert ei.value.cls == "SHORT_READ"


def test_query_empty_read_raises():
    be, g = _linux_backend(read=b"", read_ibsta=_FakeGpib.END, read_ibcnt=0)   # END set, 0 bytes
    with pytest.raises(ni_gpib_server.GpibFault):
        be.query(b"TRA?")


def test_query_good_read_returns_bytes():
    be, g = _linux_backend(read=b"-90.0,-90.0", read_ibsta=_FakeGpib.END, read_ibcnt=11)
    assert be.query(b"TRA?") == b"-90.0,-90.0"


def test_query_survives_linux_gpib_build_without_iberr():
    # LIVE REGRESSION: this guest's linux-gpib python build exposes ibsta()/ibcnt() but NOT iberr().
    # The old _status() called iberr() FIRST inside one try/except that returned (0,0,0) on failure,
    # so the missing symbol ZEROED the real ibsta/ibcnt too -- a valid END-terminated reply then
    # reported ibsta=0 (END unset) and was FALSELY rejected as SHORT_READ, so the 8565EC never came
    # up though it answered "HP8565E,001,006,007,008" cleanly. _status must fetch each field
    # independently: a missing iberr degrades to 0 while ibsta (END) + ibcnt survive.
    be, g = _linux_backend(read=b"HP8565E,001,006,007,008",
                           read_ibsta=_FakeGpib.END, read_ibcnt=23)
    g.iberr = None                                      # build without gpib.iberr()
    assert be.query(b"ID?") == b"HP8565E,001,006,007,008"   # valid reply NOT rejected as SHORT_READ
    assert be._status() == (0, _FakeGpib.END, 23)          # real ibsta/ibcnt preserved, iberr -> 0


# =========================================================================== error classification (fix 5)

def test_write_fault_raises_structured_gpibfault():
    be, g = _linux_backend(fault=(2, _FakeGpib.ERR, 0))
    g.write_fault = True
    with pytest.raises(ni_gpib_server.GpibFault) as ei:
        be.write(b"SNGLS")
    assert ei.value.iberr == 2 and ei.value.cls == "BUS_WEDGED"


# =========================================================================== ping (fix 2)

def test_ping_ok_returns_status_byte():
    be, g = _linux_backend(poll=0x40)
    assert be.ping() == 0x40


def test_ping_fault_is_classified():
    be, g = _linux_backend(poll=None, fault=(6, _FakeGpib.ERR | _FakeGpib.TIMO, 0))
    with pytest.raises(ni_gpib_server.GpibFault) as ei:
        be.ping()
    assert ei.value.cls == "DEVICE_SILENT"


# =========================================================================== recover state machine (fix 2)

def test_recover_runs_full_state_machine_and_reports_ok():
    be, g = _linux_backend(poll=0x40)
    res = be.recover()
    assert res.verdict == "OK"
    assert g.clears == 1 and g.onlines == [0, 1]            # ibclr + ibonl off/on happened
    assert len(g.opens) == 2                               # one open at bind, one at reopen
    assert [step for step, *_ in res.trail] == ["ibclr", "ibonl", "reopen", "probe"]


@pytest.mark.parametrize("fault,verdict", [
    ((6, _FakeGpib.ERR | _FakeGpib.TIMO, 0), "DEVICE_SILENT"),   # EABO
    ((2, _FakeGpib.ERR, 0), "BUS_WEDGED"),                        # ENOL
    ((0, _FakeGpib.ERR, 110), "ADAPTER_WEDGED"),                  # EDVR + ETIMEDOUT
])
def test_recover_classifies_terminal_state(fault, verdict):
    be, g = _linux_backend(poll=None, fault=fault)
    res = be.recover()
    assert res.verdict == verdict


def test_recover_adapter_wedged_detail_names_errno():
    be, g = _linux_backend(poll=None, fault=(0, _FakeGpib.ERR, 110))
    assert "ETIMEDOUT" in be.recover().detail


# =========================================================================== bind default timeout (fix 4)

def test_bind_sets_a_default_timeout():
    be, g = _linux_backend()
    assert g.tmo is not None                               # bind() -> set_timeout(3000) applied


# =========================================================================== dispatcher framing

def test_dispatcher_happy_path_unchanged():
    be = ni_gpib_server.FakeBackend()
    r = _drive(be, [("A", b"18"), ("W", b"SNGLS"), ("Q", b"ID?"), ("C", b"")])
    assert r[0] == ("+", b"") and r[1] == ("+", b"")       # A, W
    assert r[2][0] == "=" and b"8565E" in r[2][1]          # Q
    assert r[3] == ("+", b"")                              # C


def test_dispatcher_short_read_returns_error_not_data():
    be, g = _linux_backend(read=b"", read_ibsta=0, read_ibcnt=0)
    status, data = _drive(be, [("Q", b"TRA?")])[0]
    assert status == "!"                                   # NOT '=' -- the corruption is caught
    assert data.startswith(b"SHORT_READ ")
    assert b"ibcnt=0" in data                              # structured, backward-compatible '!' line


def test_dispatcher_z_ping_and_recover_fake():
    be = ni_gpib_server.FakeBackend()
    r = _drive(be, [("A", b"18"), ("Z", b"ping"), ("Z", b"recover"), ("Z", b"bogus")])
    assert r[1] == ("=", b"OK 0")                          # ping liveness
    assert r[2][0] == "=" and r[2][1].startswith(b"OK ")   # recover verdict
    assert r[3][0] == "!"                                  # unknown subcommand -> error


def test_dispatcher_z_ping_fault_is_structured():
    be, g = _linux_backend(poll=None, fault=(6, _FakeGpib.ERR | _FakeGpib.TIMO, 0))
    status, data = _drive(be, [("Z", b"ping")])[0]
    assert status == "!"
    assert data.startswith(b"DEVICE_SILENT ") and b"iberr=6" in data


def test_error_path_emits_structured_stderr_log(capsys):
    # fix 5: every fault path journals sid=..pad=..verb=..iberr=..ibsta=0x..ibcnt=..msg=..
    be, g = _linux_backend(read=b"", read_ibsta=0, read_ibcnt=0)   # short read -> a fault
    _drive(be, [("Q", b"TRA?")])
    err = capsys.readouterr().err
    assert "sid=" in err and "pad=18" in err and "verb=Q" in err
    assert "iberr=" in err and "ibsta=0x" in err and "ibcnt=" in err and "msg=" in err


@pytest.mark.parametrize("fault,verdict", [
    ((6, _FakeGpib.ERR | _FakeGpib.TIMO, 0), b"DEVICE_SILENT"),
    ((2, _FakeGpib.ERR, 0), b"BUS_WEDGED"),
    ((0, _FakeGpib.ERR, 110), b"ADAPTER_WEDGED"),
])
def test_dispatcher_z_recover_classifications(fault, verdict):
    be, g = _linux_backend(poll=None, fault=fault)
    status, data = _drive(be, [("Z", b"recover")])[0]
    assert status == "="                                   # recover always answers '=' with a verdict
    assert data.startswith(verdict)


def test_dispatcher_z_is_backward_compatible_old_clients_unaffected():
    # an old client never sends Z; the ordinary W/Q path is byte-identical to before.
    be = ni_gpib_server.FakeBackend()
    r = _drive(be, [("A", b"5"), ("Q", b"*IDN?")])         # pad 5 -> the source persona
    assert r[1][0] == "=" and b"68369A" in r[1][1]


# =========================================================================== idle timeout (fix 3)

def test_idle_timeout_ends_parked_session():
    be = ni_gpib_server.FakeBackend()
    cli, srv = socket.socketpair()
    th = threading.Thread(target=ni_gpib_server.serve_connection,
                          args=(srv, be), kwargs={"idle_s": 0.2}, daemon=True)
    th.start()
    th.join(timeout=3.0)                                   # send nothing: the read must time out
    assert not th.is_alive()                               # session ended, thread not parked forever
    cli.close()
    srv.close()


# =========================================================================== dead-man safe-state (STAGE 1 SAFETY)

def _lease_then_crash(backend, requests, safe_state=None):
    """Drive `requests` on a REAL serve_connection, then ABRUPTLY close the client socket -- a client
    CRASH: no U (release), no C (close verb) -- and wait for the server thread's teardown `finally` to
    run. Inspect `backend` afterwards for the dead-man de-key. Returns the thread (already joined)."""
    cli, srv = socket.socketpair()
    cli.settimeout(3.0)
    kwargs = {"idle_s": 1.0}
    if safe_state is not None:
        kwargs["safe_state"] = safe_state
    th = threading.Thread(target=ni_gpib_server.serve_connection,
                          args=(srv, backend), kwargs=kwargs, daemon=True)
    th.start()
    f = cli.makefile("rwb")
    try:
        for verb, payload in requests:
            f.write(protocol.encode_request(verb, payload))
            f.flush()
            protocol.decode_reply(f.readline())           # drain the reply for each request
    finally:
        f.close()
        cli.close()                                       # abrupt disconnect == client crash / partition
        th.join(timeout=2.0)
    return th


def test_lease_holder_disconnect_sends_safe_state_to_pad():
    # THE FIX: a controlling client leases the SOURCE pad and then CRASHES (no U, no C). The bridge
    # must de-key the transmitter on teardown -- send the configured per-pad safe-state command
    # (default 5:RF0 = 68369A RF output OFF) to the pad it controlled, so a dead client cannot leave
    # a 40 GHz emitter radiating.
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        th = _lease_then_crash(be, [("A", b"5"), ("L", b"5 30")], safe_state={5: b"RF0"})
        assert not th.is_alive()
        assert be.address == 5                             # bridge addressed the source pad
        assert be._last == b"RF0"                          # ... and sent RF output OFF (dead-man de-key)
    finally:
        ni_gpib_server._LEASES.reset()


def test_lease_holder_disconnect_uses_default_safe_state():
    # safety BY DEFAULT: no explicit safe_state -> the built-in {5: RF0} still de-keys the source pad.
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        _lease_then_crash(be, [("A", b"5"), ("L", b"5 30")])   # no safe_state kwarg -> default
        assert be._last == b"RF0"
    finally:
        ni_gpib_server._LEASES.reset()


def test_observer_disconnect_does_not_de_key():
    # a session that BINDS the source pad but NEVER leases it is an OBSERVER, not a controller; its
    # disconnect must NOT de-key a source another client may be driving.
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        _lease_then_crash(be, [("A", b"5"), ("Q", b"*IDN?")], safe_state={5: b"RF0"})
        assert be._last == b""                             # no lease -> no safe-state command written
    finally:
        ni_gpib_server._LEASES.reset()


def test_unleased_keying_write_de_keys_on_crash():
    # ISSUE 5 (safety gap): keying needs NO lease -- an unleased pad accepts W. A client that WRITES
    # RF1 to the source pad WITHOUT leasing must STILL be de-keyed on crash, else a dead client leaves
    # a 40 GHz emitter radiating. A write to a safe-state pad arms the dead-man for that pad.
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        _lease_then_crash(be, [("A", b"5"), ("W", b"RF1")], safe_state={5: b"RF0"})
        assert be.address == 5 and be._last == b"RF0"      # keyed-without-lease -> still de-keyed
    finally:
        ni_gpib_server._LEASES.reset()


def test_unleased_keying_then_clean_release_does_not_de_key():
    # symmetry with the leased case: a clean U handoff clears the keyed set too, so an intentional
    # release after keying does NOT fire the dead-man (de-key is reserved for a crash/partition).
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        _lease_then_crash(be, [("A", b"5"), ("W", b"RF1"), ("U", b"")], safe_state={5: b"RF0"})
        assert be._last == b"RF1"                           # U cleared keyed -> no RF0 de-key
    finally:
        ni_gpib_server._LEASES.reset()


def test_clean_release_then_disconnect_does_not_de_key():
    # U is an explicit CLEAN handoff: the client relinquished control on purpose, so a later
    # disconnect must NOT fire the dead-man de-key (that is reserved for a crash / partition).
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        _lease_then_crash(be, [("A", b"5"), ("L", b"5 30"), ("U", b"")], safe_state={5: b"RF0"})
        assert be._last == b""                             # controlled cleared on U -> no de-key
    finally:
        ni_gpib_server._LEASES.reset()


def test_safe_state_de_keys_a_free_pad():
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        ni_gpib_server._send_safe_state(1, be, {5}, {5: b"RF0"})
        assert be.address == 5 and be._last == b"RF0"      # free pad -> de-keyed
    finally:
        ni_gpib_server._LEASES.reset()


def test_safe_state_skips_pad_held_by_a_successor_controller():
    # if a SUCCESSOR controller now holds the pad, a dying prior session must NOT de-key it (that
    # would disrupt the live controller's source).
    ni_gpib_server._LEASES.reset()
    try:
        ni_gpib_server._LEASES.acquire(5, holder=999, ttl=30)   # a successor controls pad 5
        be = ni_gpib_server.FakeBackend()
        be.bind(18)
        ni_gpib_server._send_safe_state(1, be, {5}, {5: b"RF0"})
        assert be._last == b""                             # successor holds pad 5 -> NOT de-keyed
        assert be.address == 18                            # backend was not rebound to the source pad
    finally:
        ni_gpib_server._LEASES.reset()


def test_bus_controller_de_keys_configured_pad():
    ni_gpib_server._LEASES.reset()
    try:
        be = ni_gpib_server.FakeBackend()
        ni_gpib_server._send_safe_state(1, be, {"BUS"}, {5: b"RF0"})
        assert be.address == 5 and be._last == b"RF0"      # BUS controller safes every configured pad
    finally:
        ni_gpib_server._LEASES.reset()


def test_parse_safe_state_default_and_overrides():
    assert ni_gpib_server._parse_safe_state([]) == {5: b"RF0"}         # default protects the source
    assert ni_gpib_server._parse_safe_state(["5:RF0", "7:OUTP0"]) == {5: b"RF0", 7: b"OUTP0"}
    assert ni_gpib_server._parse_safe_state(["5:"]) == {}              # 'PAD:' opts a pad out
    with pytest.raises(ValueError):
        ni_gpib_server._parse_safe_state(["nope"])                     # malformed -> ValueError


def test_empty_host_is_not_loopback():
    # GAP-A regression: bind("") is INADDR_ANY (all interfaces), so "" must NOT read as loopback,
    # else `--host ""` skips the auth refusal and exposes unauthenticated control on the LAN.
    assert ni_gpib_server._is_loopback("127.0.0.1")
    assert ni_gpib_server._is_loopback("localhost")
    assert not ni_gpib_server._is_loopback("")                        # all-interfaces, NOT loopback
    assert not ni_gpib_server._is_loopback("0.0.0.0")


@pytest.mark.parametrize("host", ["", "0.0.0.0", "192.168.1.50"])
def test_main_refuses_all_interfaces_bind_without_auth(host):
    # The refusal must fire for every non-loopback bind incl. "" -- and does so (p.error -> SystemExit)
    # BEFORE serve_forever opens any socket, so no client/accept is involved.
    with pytest.raises(SystemExit):
        ni_gpib_server.main(["--host", host, "--port", "5599"])       # no token, no --insecure
