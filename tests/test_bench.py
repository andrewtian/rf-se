"""Headless test for the bench composition: SA + SG panels present, ordered shutdown forces RF off
and shuts the hub. Uses fake panels/hub via build over a sim control plane.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui \
        python -m pytest rf-se/se299/tests/test_bench.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bench_gui


def test_bench_builds_sim_and_shutdown_is_ordered(monkeypatch):
    pytest.importorskip("PySide6")
    bench = bench_gui.build_bench("sim", "sim")
    calls = []
    monkeypatch.setattr(bench.sa, "stop", lambda: calls.append("sa_stop"))
    monkeypatch.setattr(bench.sg, "stop", lambda: calls.append("sg_stop"))
    monkeypatch.setattr(bench.hub, "shutdown", lambda: calls.append("hub_shutdown"))
    bench.shutdown()
    assert calls == ["sa_stop", "sg_stop", "hub_shutdown"]   # stop panels (rf off) before hub


def test_bench_has_both_panels():
    pytest.importorskip("PySide6")
    bench = bench_gui.build_bench("sim", "sim")
    assert bench.sa is not None and bench.sg is not None
    assert bench.window is not None


def test_start_presuspends_every_mode_then_resumes_only_active(monkeypatch):
    """Lifecycle invariant: start() parks EVERY mode (suspend before its threads spawn) and then
    resumes ONLY the active tab -- so non-active modes never race for the shared rx/tx leases. This
    is the fix for the flaky no-PSD-feed: previously all modes started unsuspended and a non-active
    mode could win the analyzer, blocking the active tab."""
    pytest.importorskip("PySide6")
    bench = bench_gui.build_bench("sim", "sim")       # default active tab = 0
    events = []
    for i, m in enumerate(bench.modes):
        monkeypatch.setattr(m, "suspend", (lambda i: lambda: events.append((i, "suspend")))(i))
        monkeypatch.setattr(m, "resume", (lambda i: lambda: events.append((i, "resume")))(i))
        monkeypatch.setattr(m, "start", (lambda i: lambda ms=200: events.append((i, "start")))(i))
    bench.start(200)
    for i in range(len(bench.modes)):                 # every mode: suspended BEFORE it is started
        verbs = [v for (j, v) in events if j == i]
        assert verbs[0] == "suspend"
        assert verbs.index("suspend") < verbs.index("start")
    assert (0, "resume") in events                    # only the active tab is resumed
    assert (1, "resume") not in events and (2, "resume") not in events


def test_bench_invariant_violations_detects_torn_ownership():
    """The bench can always answer 'is the active tab the sole driver?' -- healthy when it is,
    and it names the violation when a non-active mode owns an instrument."""
    pytest.importorskip("PySide6")
    bench = bench_gui.build_bench("sim", "sim")
    point = bench.modes[2]
    bench.hub._arb._owner["rx"] = point.rx_engine       # Point Op = sole owner of both units
    bench.hub._arb._owner["tx"] = point.tx_engine
    for i, m in enumerate(bench.modes):
        m.rx_engine.suspended = (i != 2)
        m.tx_engine.suspended = (i != 2)
    assert bench.invariant_violations(active=2) == []    # healthy
    bench.hub._arb._owner["rx"] = bench.modes[0].rx_engine   # torn: Full Bench owns rx while PointOp active
    v = bench.invariant_violations(active=2)
    assert v and any(s.startswith("rx:") for s in v)


def test_bench_watchdog_tolerates_transient_flags_sustained():
    """The status-strip watchdog ignores a single-tick handoff transient but surfaces a SUSTAINED
    (2+ tick) invariant violation, so a torn-ownership state can never present as a silent blank."""
    pytest.importorskip("PySide6")
    bench = bench_gui.build_bench("sim", "sim")           # active tab 0 (Full Bench)
    for i, m in enumerate(bench.modes):                   # emulate a healthy started state
        m.rx_engine.suspended = (i != 0)
        m.tx_engine.suspended = (i != 0)
    bench.hub._arb._owner["rx"] = bench.modes[0].rx_engine
    bench.hub._arb._owner["tx"] = bench.modes[0].tx_engine
    bench._watchdog_tick()
    assert "LIFECYCLE WARNING" not in bench._wd_label.text() and "active:" in bench._wd_label.text()
    bench.hub._arb._owner["rx"] = bench.modes[2].rx_engine   # non-active Point Op owns rx (torn)
    bench._watchdog_tick()                                # streak 1 -> transient, not yet flagged
    assert "LIFECYCLE WARNING" not in bench._wd_label.text()
    bench._watchdog_tick()                                # streak 2 -> sustained -> flagged
    assert "LIFECYCLE WARNING" in bench._wd_label.text() and "rx" in bench._wd_label.text()
    bench.hub._arb._owner["rx"] = bench.modes[0].rx_engine   # recover
    bench._watchdog_tick()
    assert "LIFECYCLE WARNING" not in bench._wd_label.text()


def test_install_exit_cleanup_runs_once_and_hooks_signals(monkeypatch):
    """On Ctrl-C / kill the GUI must release its lease (else the next launch is blocked till the
    TTL). install_exit_cleanup registers SIGINT/SIGTERM handlers and runs the cleanup exactly once."""
    import signal
    import qt_common
    registered = {}
    monkeypatch.setattr(signal, "signal", lambda s, h: registered.__setitem__(s, h))
    calls = []
    run = qt_common.install_exit_cleanup(lambda: calls.append(1))
    run(); run()                                         # idempotent: cleanup fires once
    assert calls == [1]
    assert signal.SIGINT in registered and signal.SIGTERM in registered


def test_arbiter_thread_safe_under_concurrent_acquire_release():
    """The Arbiter is hit by engine threads (acquire) and the main thread (release); the lock keeps
    the owner map from tearing under concurrency. End state is internally consistent."""
    import threading
    from instrument_hub import Arbiter

    class _Eng:
        def __init__(self, name):
            self.name = name
            self.suspended = False

        def suspend(self):
            self.suspended = True

        def resume(self):
            self.suspended = False

    arb = Arbiter()
    engs = [_Eng(f"e{i}") for i in range(6)]
    errors = []

    def worker(e):
        try:
            for _ in range(400):
                arb.acquire("rx", e)
                arb.release("rx", e)
        except Exception as ex:                          # noqa: BLE001
            errors.append(ex)

    ts = [threading.Thread(target=worker, args=(e,)) for e in engs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    o = arb.owner("rx")
    assert o is None or o in engs                        # never a torn/garbage owner


def test_close_event_runs_ordered_shutdown(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6 import QtGui
    bench = bench_gui.build_bench("sim", "sim")
    calls = []
    monkeypatch.setattr(bench.sa, "stop", lambda: calls.append("sa_stop"))
    monkeypatch.setattr(bench.sg, "stop", lambda: calls.append("sg_stop"))
    monkeypatch.setattr(bench.hub, "shutdown", lambda: calls.append("hub_shutdown"))
    bench.window.closeEvent(QtGui.QCloseEvent())     # simulate the window-close (X) path
    assert calls == ["sa_stop", "sg_stop", "hub_shutdown"]   # RF off (sg_stop) before leases released
