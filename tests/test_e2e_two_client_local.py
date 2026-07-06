"""LOCAL end-to-end: TWO CLIENTS over the network, TWO namespaced bridges, on ONE M1.

Proves the multi-computer any-client capability WITHOUT real hardware or VMs, using two SEPARATE
fake ni_gpib_server processes on two localhost ports -- each port is one host / one VM namespace:
one bridge serves the ANALYZER (pad 18), the other serves the SOURCE (pad 5). On top of that
two-namespace topology:

  * a COORDINATOR client atomically controls BOTH bridges and runs a coherent IEEE-299 substitution
    campaign while publishing live SE (client 1);
  * a second OBSERVER client watches the live SE figure over telemetry without touching the bus
    (client 2);
  * a CONTENDER client is correctly REFUSED control on both bridges while the coordinator holds it;
  * take_control is ATOMIC: if a rival already holds the source, the coordinator refuses cleanly and
    leaves NO analyzer lease stranded;
  * a dropped controlling session frees both leases (no stranding) -- the bridge's dead-man safe-state
    (source pad 5 -> RF0) is unit-tested in test_ni_gpib_server.

Real remote-host operation differs ONLY in the addresses (the transport is host-agnostic), so this
one-machine E2E is the repeatable proof that the networked two-client topology + cross-bridge
arbitration are correct.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
        rf-se/se299/tests/test_e2e_two_client_local.py -q
"""
from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import control_plane
import coordinator
import drivers
import roles
from gpib_bridge import ni_gpib_server


def _bridge(signal="flat"):
    """Start ONE fake bridge on an ephemeral localhost port and return the port. Each bridge is a
    distinct process-global namespace (its own lease registry) -- two of them stand in for two hosts
    / two VM namespaces on this single M1."""
    srv = ni_gpib_server.listen("127.0.0.1", 0)
    port = srv.getsockname()[1]
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "signal": signal}, daemon=True).start()
    return port


def _mini_cfg():
    """A 2-frequency campaign so an E2E over real TCP bridges is FAST (the full DC-40 GHz plan is
    ~76s over the network). Two points still exercise the reference + wall passes and the live
    per-point SE telemetry the observer must receive."""
    return dataclasses.replace(
        cfg_mod.default(),
        bands=(cfg_mod.BandPlan("e2e-mini", 1e9, 2e9, 2, 14.0, 12.0, -150.0),))


def _two_bridge_cp():
    """A control plane whose analyzer and source live on TWO SEPARATE bridges (two namespaces).

    Default: two FAKE bridges on localhost (hardware-free regression of the networked two-client
    topology + lease/telemetry logic). If SE299_LIVE_RX and SE299_LIVE_TX are set to the real
    bridge's net:HOST:PORT:PAD, this targets LIVE equipment instead -- the SAME two-client topology
    driving the real 8565EC + 68369A. That env-var path is how the capstone is run against live
    hardware once the bridge is up; returns (cp, rx_host_port, tx_host_port)."""
    live_rx, live_tx = os.environ.get("SE299_LIVE_RX"), os.environ.get("SE299_LIVE_TX")
    if live_rx and live_tx:
        cp = control_plane.from_addresses(_mini_cfg(), rx_addr=live_rx, tx_addr=live_tx)
        _hp = lambda a: (a.split(":")[1], int(a.split(":")[2]), int(a.split(":")[3]))
        return cp, _hp(live_rx), _hp(live_tx)
    aport, sport = _bridge(), _bridge()
    cp = control_plane.from_addresses(
        _mini_cfg(),
        rx_addr=f"net:127.0.0.1:{aport}:18",
        tx_addr=f"net:127.0.0.1:{sport}:5")
    return cp, ("127.0.0.1", aport, 18), ("127.0.0.1", sport, 5)


# ----------------------------------------------------------------- capstone: controller + observer

def test_two_client_networked_campaign_over_two_bridges():
    # CLIENT 1 (coordinator) runs the full substitution campaign atomically across TWO bridges while
    # CLIENT 2 (observer) watches the live SE figure over telemetry -- the whole networked two-client
    # operation on one M1, over real TCP, two namespaces.
    cp, _aport, _sport = _two_bridge_cp()
    role = roles.CoordinatorRole(cp).start()
    try:
        dash = roles.DashboardRole().connect("127.0.0.1", role.telemetry_port)
        assert role.hub.wait_subscribers(1)
        result = role.run_campaign()
        n = result["se_figure"]["points"]
        deadline = time.monotonic() + 3.0                    # let the observer's async reader drain
        while time.monotonic() < deadline and (dash.summary is None or len(dash.se_history) < n):
            time.sleep(0.02)
        dash.close()
    finally:
        role.stop()
    # the observer KNEW the roster, the live worst-case SE, and the verdict -- without touching the bus
    assert dash.roster and {u["kind"] for u in dash.roster} == {"rx", "tx"}
    assert dash.latest_se["se_db"] == pytest.approx(result["se_figure"]["se_db"])
    assert dash.summary is not None and "campaign_pass" in dash.summary
    hist = [h for h in dash.se_history if h is not None]
    assert hist == sorted(hist, reverse=True)                # running worst-case SE, monotone down


# ----------------------------------------------------------------- exclusive cross-bridge control

def test_coordinator_locks_both_bridges_against_a_contender():
    # CLIENT 1 takes atomic control across the two namespaces; CLIENT 2 (a contender) is refused BOTH
    # pads until release, then may control -- coherent exclusivity spanning two independent bridges.
    cp, rx_hp, tx_hp = _two_bridge_cp()
    coord = cp.make_coordinator()
    assert coord.ensure_ready() is True
    coord.take_control()
    c_ana = drivers.NetworkTransport(*rx_hp)
    c_src = drivers.NetworkTransport(*tx_hp)
    try:
        with pytest.raises(IOError):
            c_ana.query("ID?")                               # analyzer bridge locked
        with pytest.raises(IOError):
            c_src.query("*IDN?")                             # source bridge locked
        coord.release_control()
        # 683xx FAMILY match (the bench unit is a 68367C, not a 68369A) -- mirrors drivers.Anritsu68369.idn
        # and vm.source_reachable, which both key on "683", not the exact "68369".
        assert "8565" in c_ana.query("ID?") and "683" in c_src.query("*IDN?")
    finally:
        c_ana.close()
        c_src.close()
        cp.resolve(kind="rx").close()
        cp.resolve(kind="tx").close()


# ----------------------------------------------------------------- atomic all-or-nothing acquire

def test_take_control_is_atomic_no_stranded_lease_when_source_contended():
    # A rival CLIENT already controls the SOURCE bridge. take_control must refuse CLEANLY (raise,
    # naming who holds it) BEFORE any bus op, and must NOT strand the analyzer lease -- a fresh client
    # can immediately take the analyzer. (Requires atomic all-or-nothing take_control -- Wave 1 W1.4.)
    cp, rx_hp, tx_hp = _two_bridge_cp()
    rival_src = drivers.NetworkTransport(*tx_hp)
    rival_src.lease(scope="device", ttl_s=30)                # rival owns the source
    try:
        coord = cp.make_coordinator()
        assert coord.ensure_ready() is True
        with pytest.raises(coordinator.ControlConflict) as ei:  # must RAISE, not half-acquire
            coord.take_control()
        assert ei.value.instrument == "TX"                   # names the CONTENDED instrument...
        assert "TX controlled by" in str(ei.value) and ei.value.who    # ...and WHO holds it
        # the analyzer must NOT be stranded: a fresh client leases it with no wait
        fresh_ana = drivers.NetworkTransport(*rx_hp)
        try:
            assert "scope" in fresh_ana.lease(scope="device", ttl_s=30)
            assert "8565" in fresh_ana.query("ID?")
        finally:
            fresh_ana.close()
    finally:
        rival_src.close()
        cp.resolve(kind="rx").close()
        cp.resolve(kind="tx").close()


# ----------------------------------------------------------------- no stranding on a hard drop

def test_dropped_controller_frees_both_bridges_no_stranding():
    # If the controlling client vanishes (socket death), BOTH bridges must free their leases so the
    # next client takes coherent control -- no host left permanently locked. The source-side dead-man
    # RF-off (safe-state pad 5 -> RF0) is asserted in test_ni_gpib_server; here we prove liveness.
    cp, rx_hp, tx_hp = _two_bridge_cp()
    coord = cp.make_coordinator()
    assert coord.ensure_ready() is True
    coord.take_control()
    # simulate the controlling client vanishing WITHOUT a clean release: drop the underlying sockets
    cp.resolve(kind="rx").close()
    cp.resolve(kind="tx").close()
    # the bridges free the leases on disconnect; a fresh client on each bridge takes control
    nxt_ana = drivers.NetworkTransport(*rx_hp)
    nxt_src = drivers.NetworkTransport(*tx_hp)
    try:
        deadline = time.monotonic() + 3.0
        got_a = got_s = False
        while time.monotonic() < deadline and not (got_a and got_s):
            try:
                if not got_a:
                    nxt_ana.lease(scope="device", ttl_s=30); got_a = True
                if not got_s:
                    nxt_src.lease(scope="device", ttl_s=30); got_s = True
            except IOError:
                time.sleep(0.05)                             # lease not freed yet -> retry
        assert got_a and got_s                               # both bridges freed -> no stranding
        # 683xx FAMILY match (bench unit is a 68367C) -- see the note in the exclusive-control test
        assert "8565" in nxt_ana.query("ID?") and "683" in nxt_src.query("*IDN?")
    finally:
        nxt_ana.close()
        nxt_src.close()


# ----------------------------------------------------------------- two clients race -> ONE controller

def test_two_clients_racing_converge_to_exactly_one_controller():
    # TWO clients race to lease BOTH bridges (RX then TX -- the coordinator's acquire order) at the
    # same instant. The bridges' lease arbitration must let EXACTLY ONE win both; the other is refused
    # on the first contended pad (loses RX -> never reaches TX) -- coherent exclusivity, never split
    # control across the two hosts. (Raced at the LEASE level: the analyzer health-gate, which
    # concurrent UNLEASED sweep-liveness probing can trip on the flat fake, is orthogonal to the
    # arbitration under test and would only confound it.)
    cp, rx_hp, tx_hp = _two_bridge_cp()
    results = {}
    socks = {}
    start = threading.Barrier(2)

    def race(name):
        rx = drivers.NetworkTransport(*rx_hp)
        tx = drivers.NetworkTransport(*tx_hp)
        socks[name] = (rx, tx)
        start.wait()                                         # fire both at the same instant
        try:
            rx.lease(scope="device", ttl_s=30)               # RX first...
            tx.lease(scope="device", ttl_s=30)               # ...then TX
            results[name] = "won"
        except IOError:
            results[name] = "lost"                           # refused on the contended pad

    t1 = threading.Thread(target=race, args=("a",))
    t2 = threading.Thread(target=race, args=("b",))
    t1.start(); t2.start(); t1.join(5); t2.join(5)
    try:
        assert sorted(results) == ["a", "b"]                 # both finished (neither hung)
        assert list(results.values()).count("won") == 1      # EXACTLY one controller, never two
        assert list(results.values()).count("lost") == 1     # the other cleanly refused
    finally:
        for rx, tx in socks.values():
            rx.close(); tx.close()
        cp.resolve(kind="rx").close(); cp.resolve(kind="tx").close()


# ----------------------------------------------------------------- host-down: short-TTL rollback

def test_short_ttl_lease_frees_for_next_client_when_holder_goes_silent():
    # A client leases a bridge with a SHORT ttl then goes SILENT (a dead / partitioned peer: no
    # keepalive, no release). The lease must EXPIRE so the next client acquires without waiting
    # forever -- a dead peer cannot lock the instrument past its TTL (host-down rollback).
    cp, rx_hp, tx_hp = _two_bridge_cp()
    dead = drivers.NetworkTransport(*tx_hp)
    dead.lease(scope="device", ttl_s=1.5)                    # short TTL; never renewed (silent)
    nxt = drivers.NetworkTransport(*tx_hp)
    try:
        with pytest.raises(IOError):
            nxt.lease(scope="device", ttl_s=30)              # still held (well within the 1.5 s TTL)
        deadline = time.monotonic() + 5.0
        got = False
        while time.monotonic() < deadline and not got:
            try:
                nxt.lease(scope="device", ttl_s=30); got = True
            except IOError:
                time.sleep(0.1)                              # not expired yet -> retry
        assert got                                           # TTL lapsed -> next client acquired; no lock
    finally:
        dead.close(); nxt.close()
        cp.resolve(kind="rx").close(); cp.resolve(kind="tx").close()


# ----------------------------------------------------------------- asymmetric partition frees one side

def test_asymmetric_partition_frees_only_the_partitioned_host():
    # The coordinator controls BOTH bridges, then ONLY the SOURCE link partitions (its socket drops)
    # while the ANALYZER link stays up. The source bridge must free its lease (+ dead-man de-key) so a
    # fresh client takes the source, WHILE the analyzer stays coherently held (still refused to others).
    # An asymmetric partition frees the affected host without stranding it or dropping the healthy one.
    cp, rx_hp, tx_hp = _two_bridge_cp()
    coord = cp.make_coordinator()
    assert coord.ensure_ready() is True
    coord.take_control()
    cp.resolve(kind="tx").close()                            # partition ONLY the source
    nxt_src = drivers.NetworkTransport(*tx_hp)
    other_ana = drivers.NetworkTransport(*rx_hp)
    try:
        deadline = time.monotonic() + 3.0
        got_src = False
        while time.monotonic() < deadline and not got_src:
            try:
                nxt_src.lease(scope="device", ttl_s=30); got_src = True
            except IOError:
                time.sleep(0.05)
        assert got_src                                       # partitioned host freed -> reclaimable
        with pytest.raises(IOError):
            other_ana.query("ID?")                           # analyzer STILL held -> coherence intact
    finally:
        nxt_src.close(); other_ana.close()
        coord.release_control()
        cp.resolve(kind="rx").close()                        # tx link already closed above
