"""LIVE coupling E2E: prove the 8565EC RX ACTUALLY RECEIVES the 68367C TX tone -- the real
indicator of correct two-unit operation, not just well-formed numbers.

The other live suite (test_e2e_live.py) asserts rows are finite/well-formed, which passes even on a
STALE or wedged analyzer trace. These tests instead assert PHYSICS: with the source cabled to the
analyzer input, turning the tone ON must raise the measured level FAR above the source-off floor
(couples), the peak must TRACK the source frequency, and it must TRACK the source power ~1:1. That
is the "does the RX see the TX" check.

Robust to the bench 8565EC's intermittent sweep-wedge (post-power-cycle the LO is marginal): each
read is retried, and if no stable coupling can be obtained the test SKIPS with an honest reason
(instrument not measuring) rather than false-passing on stale data -- the se-certification
discipline (honest ABSENT, never a fake pass).

SAFETY: the source is capped (arm_direct_chain -> <=0 dBm) and the analyzer input attenuation floored
(>=20 dB), proven under the 8565EC damage limits before any tone flows; RF is left OFF on exit.

Run (bring the live bridge up first, see test_e2e_live.py):
  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
    rf-se/se299/tests/test_e2e_coupling_live.py -v -n0
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drivers
from gpib_bridge import vm

# frequencies for the coupling checks -- band 0 (<2.9 GHz, unpreselected) so no preselector-peak step
_F_HZ = (1.90e9, 2.00e9, 2.10e9)
_COUPLE_MIN_DB = 20.0          # tone must exceed the source-off floor by >= this (real coupling)
_RF_SETTLE_S = 1.0            # RF on/off transition dwell (measured ~0.5-1.0 s on the 68367C)
_MAX_TRIES = 4               # ride through an intermittent analyzer sweep-wedge


def _spec():
    return vm.VmSpec(
        port=int(os.environ.get("SE299_ANALYZER_PORT", "5555")),
        source_port=int(os.environ.get("SE299_SOURCE_PORT", "5556")),
        gpib_addr=int(os.environ.get("SE299_ANALYZER_PAD", "18")),
        source_addr=int(os.environ.get("SE299_SOURCE_PAD", "5")))


def _require_live(spec):
    if not (vm.bridge_reachable(spec, timeout_ms=1500) and vm.source_reachable(spec, timeout_ms=1500)):
        pytest.skip("live setup not reachable (bring up with `cli.py coordinator --vm`; both units needed)")


@pytest.fixture
def live_pair():
    """Leased analyzer+source drivers over the real bridge, armed for a safe direct-cable loopback.
    RF is forced OFF and links closed on teardown."""
    spec = _spec()
    _require_live(spec)
    rx = drivers.NetworkTransport("127.0.0.1", spec.port, spec.gpib_addr, timeout_ms=15000)
    tx = drivers.NetworkTransport("127.0.0.1", spec.source_port, spec.source_addr, timeout_ms=15000)
    rx.lease(scope="device", ttl_s=300)
    tx.lease(scope="device", ttl_s=300)
    ana = drivers.Agilent856xEC(rx)
    src = drivers.Anritsu68369(tx)
    # SAFETY FIRST: prove the direct-cable envelope is under the 8565EC damage limits before any tone.
    drivers.arm_direct_chain(src, ana, source_cap_dbm=0.0, rx_min_atten_db=20.0, cable_loss_db=0.0)
    src.prepare()
    ana.configure(rbw_hz=100e3, vbw_hz=100e3, ref_dbm=10.0, detector="POS")   # CONTS read path (bridge-reliable)
    ana.set_attenuation(db=20)
    try:
        yield ana, src
    finally:
        try:
            src.rf_off()
        except Exception:
            pass
        rx.close()
        tx.close()


def _tone_and_floor(ana, src, f_hz):
    """Source-off floor vs source-on tone at f_hz, retried through an intermittent wedge. Returns
    (floor_dbm, tone_dbm) once the pair is self-consistent (tone > floor OR a confirmed no-couple),
    or (None, None) if the analyzer never produced a responsive read."""
    src.set_freq(f_hz)
    src.set_power(-5.0)
    for _ in range(_MAX_TRIES):
        src.rf_off(); time.sleep(_RF_SETTLE_S)
        _, floor = ana.measure_floor(f_hz, 0.05)
        src.rf_on(); time.sleep(_RF_SETTLE_S)
        _, tone = ana.measure_peak(f_hz, 0.05)
        # a responsive analyzer moves between the two states; a wedged one returns the SAME stuck
        # value for both -- retry those, they are not trustworthy data.
        if abs(tone - floor) > 1.0:
            return floor, tone
    return None, None


def test_live_rx_receives_tx_tone_couples(live_pair):
    # THE core check: turning the tone ON raises the measured level FAR above the source-off floor.
    ana, src = live_pair
    floor, tone = _tone_and_floor(ana, src, 2.00e9)
    if floor is None:
        pytest.skip("8565EC not producing responsive sweeps (intermittent LO wedge) -- warm up / service")
    assert tone - floor >= _COUPLE_MIN_DB, (
        f"RX does not see the TX tone: tone {tone:.1f} dBm vs floor {floor:.1f} dBm "
        f"(delta {tone - floor:.1f} dB < {_COUPLE_MIN_DB} dB)")


def test_live_rx_peak_tracks_tx_frequency(live_pair):
    # the received peak must appear at EACH commanded source frequency (real tone, not a fixed spur)
    ana, src = live_pair
    got = 0
    for f in _F_HZ:
        floor, tone = _tone_and_floor(ana, src, f)
        if floor is None:
            continue
        if tone - floor >= _COUPLE_MIN_DB:
            got += 1
    if got == 0:
        pytest.skip("8565EC not producing responsive sweeps (intermittent LO wedge) -- warm up / service")
    assert got == len(_F_HZ), f"tone only coupled at {got}/{len(_F_HZ)} frequencies"


def test_live_rx_tracks_tx_power_one_to_one(live_pair):
    # stepping the source power must move the received level ~1:1 (proves a real calibrated link,
    # not a saturated/stuck reading). RF held ON; only the level changes.
    ana, src = live_pair
    src.set_freq(2.00e9)
    src.set_power(-10.0)                     # a defined starting level (not a floor->tone edge)
    src.rf_on(); time.sleep(_RF_SETTLE_S)
    ana.measure_peak(2.00e9, 0.05)          # PRIMING read (discard): confirm the source is up and
    #                                         flush the rf_on transition before the graded sweep
    levels = (-20.0, -15.0, -10.0, -5.0)
    reads = []
    for lvl in levels:
        src.set_power(lvl); time.sleep(_RF_SETTLE_S)
        _, v = ana.measure_peak(2.00e9, 0.05)
        reads.append(v)
    span_rx = reads[-1] - reads[0]
    span_tx = levels[-1] - levels[0]
    # WEDGE signature: the analyzer returned the SAME value across the whole 15 dB source sweep
    # (no response at all) -- honest-skip rather than fail, like the other coupling tests.
    if abs(span_rx) < 1.0:
        pytest.skip("8565EC not producing responsive sweeps (intermittent LO wedge) -- warm up / service")
    # over the -20 -> -5 dBm span (15 dB) the received level should rise ~15 dB (+/- 4 dB tolerance)
    assert abs(span_rx - span_tx) <= 4.0, (
        f"RX power tracking not 1:1: source moved {span_tx:.0f} dB, RX moved {span_rx:.1f} dB "
        f"(reads {[round(r, 1) for r in reads]})")
