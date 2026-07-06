"""End-to-end HEADLESS test of Point Operation driven through the REAL bench lifecycle:
build -> activate the Point Op tab -> start() (pre-suspend, resume active) -> engine background
threads sweep -> main-thread QTimer drains the queue + renders. This is the integration path the
flaky no-PSD-feed bug lived in; the panel unit tests (test_point_op_mode.py) call _tick()/render()
synchronously and never exercise the thread + timer + lease handoff, so they could not have caught
it. Here we pump the Qt event loop exactly as the operator GUI does and assert the feed arrives.

Sim path runs in the normal board (no hardware). A LIVE path runs opt-in when SE299_LIVE_RX and
SE299_LIVE_TX are set to net:HOST:PORT:PAD addresses -- so the 'no fakes' hardware verification is
one env var away and uses the identical lifecycle:
    SE299_LIVE_RX=net:127.0.0.1:5555:18 SE299_LIVE_TX=net:127.0.0.1:5556:5 \
      QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
      rf-se/se299/tests/test_point_op_e2e.py -q -n0
"""
from __future__ import annotations

import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bench_gui


def _app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump_until(app, cond, timeout_s=8.0, tick_s=0.03):
    """Drive the Qt event loop (main-thread timers) while the engine threads run, until `cond()` is
    true or the timeout elapses. Bounded, so it is reliable in a test rather than a fixed sleep."""
    end = time.time() + timeout_s
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(tick_s)
    app.processEvents()
    return cond()


def _addrs():
    rx, tx = os.environ.get("SE299_LIVE_RX"), os.environ.get("SE299_LIVE_TX")
    return (rx, tx, True) if rx and tx else ("sim", "sim", False)


def test_point_op_feeds_through_the_bench_lifecycle():
    """Point Op, selected and started like the real GUI, must produce a live PSD sweep through the
    engine-thread + drain-timer path, be the SOLE owner (no lifecycle violation), and tear down clean."""
    pytest.importorskip("PySide6")
    app = _app()
    rx, tx, live = _addrs()
    bench = bench_gui.build_bench(rx, tx)
    try:
        point_idx = bench.modes.index(bench.point)
        bench.tabs.setCurrentIndex(point_idx)              # Point Op is the active tab from the first tick
        bench.start(120)
        got = _pump_until(app, lambda: len(bench.point.rx_model.curve()[0]) > 0,
                          timeout_s=10.0 if live else 8.0)
        assert got, "Point Op produced no PSD feed through the bench lifecycle"
        fg, levels, _ = bench.point.rx_model.curve()
        assert len(fg) == len(levels) and len(fg) >= 100    # a real sweep, not an empty trace
        assert bench.invariant_violations() == []           # active tab is the sole driver
        # the render path (SE readout, TX/ref lines, validity) runs without raising on live data
        bench.point.render()
    finally:
        bench.shutdown()


def test_point_op_survives_tab_handoff_both_directions():
    """Leaving Point Op and coming back must restore the feed (the resume path re-acquires + re-applies),
    and Point Op must be the sole owner each time it is active -- the repeated-handoff guard."""
    pytest.importorskip("PySide6")
    app = _app()
    rx, tx, live = _addrs()
    bench = bench_gui.build_bench(rx, tx)
    try:
        pi = bench.modes.index(bench.point)
        fi = bench.modes.index(bench.full)
        bench.tabs.setCurrentIndex(pi)
        bench.start(120)
        assert _pump_until(app, lambda: len(bench.point.rx_model.curve()[0]) > 0, 8.0), "no initial feed"
        bench.point.rx_model.reset_traces()                 # clear so we prove the feed comes BACK
        bench.tabs.setCurrentIndex(fi)                       # leave Point Op
        _pump_until(app, lambda: False, 1.0)
        assert pi != fi
        bench.tabs.setCurrentIndex(pi)                       # return to Point Op
        assert _pump_until(app, lambda: len(bench.point.rx_model.curve()[0]) > 0, 8.0), "feed did not resume"
        assert bench.invariant_violations() == []
    finally:
        bench.shutdown()


@pytest.mark.skipif(not (os.environ.get("SE299_LIVE_RX") and os.environ.get("SE299_LIVE_TX")),
                    reason="live bridge addresses not set (SE299_LIVE_RX / SE299_LIVE_TX)")
def test_point_op_live_reference_and_se():
    """LIVE only: with a real tone, capturing a reference then reading a (weaker) live level yields a
    finite SE. Exercises the full substitution loop end-to-end on hardware."""
    app = _app()
    rx, tx, _ = _addrs()
    bench = bench_gui.build_bench(rx, tx)
    try:
        pi = bench.modes.index(bench.point)
        bench.tabs.setCurrentIndex(pi)
        p = bench.point
        p.spin_freq.setValue(2.45)
        p.spin_power.setValue(0.0)
        p.chk_rf.setChecked(True)
        p._apply()
        bench.start(120)
        assert _pump_until(app, lambda: len(p.rx_model.curve()[0]) > 0, 12.0), "no live feed"
        # Require a STABLY valid tone across consecutive sweeps before referencing -- a transient
        # ambient blip (WiFi at 2.45 GHz) must NOT be mistaken for a source tone (no fakes). If the
        # physical TX->RX path is open (analyzer sees only ambient / a flat trace), no stable tone
        # appears -> skip and surface the setup gap. The substitution MATH is covered with a real tone
        # in test_point_op_mode.py; the live feed + lifecycle are verified by the tests above.
        valid_streak = 0
        deadline = time.time() + 12.0
        while time.time() < deadline and valid_streak < 4:
            _pump_until(app, lambda: False, 0.35)           # one ~sweep window
            valid_streak = valid_streak + 1 if p.model.reading_status()[1] else 0
        if valid_streak < 4:
            pytest.skip(f"no STABLE on-freq tone at the analyzer ({p.model.reading_status()[0]}); the "
                        f"TX->RX path is not presenting a steady source tone (ambient-only)")
        p._set_reference()
        assert p.model.reference_dbm is not None
        _pump_until(app, lambda: p.model.se_db() is not None, 4.0)
        assert p.model.se_db() is not None                  # SE = ref - live peak, finite
    finally:
        bench.shutdown()
