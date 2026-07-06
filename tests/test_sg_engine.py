"""SourceEngine tests with a FAKE source + fake hub: apply CW, RF on/off, step-sweep, rf-off
safety on stop, absent-on-error. No Qt, no threads (step_once).

Run:  uv run python -m pytest rf-se/se299/tests/test_sg_engine.py -q
"""
from __future__ import annotations

import os
import queue
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sg_gui


class _FakeSource:
    def __init__(self, fail=False):
        self.fail = fail
        self.freq = None
        self.power = None
        self.rf = False
        self.calls = []

    def set_freq(self, hz):
        if self.fail:
            raise IOError("bridge dropped")
        self.freq = hz; self.calls.append(("freq", hz))

    def set_power(self, dbm): self.power = dbm; self.calls.append(("power", dbm))

    def rf_on(self): self.rf = True; self.calls.append(("rf_on",))

    def rf_off(self): self.rf = False; self.calls.append(("rf_off",))

    def await_settled(self, settle_s=0.05, use_opc=True): pass

    def settled_ok(self): return True


class _FakeHub:
    def __init__(self, source, ok=True):
        self._s = source; self.ok = ok; self.released = 0

    @property
    def source(self): return self._s

    def acquire(self, instrument, engine): return (self.ok, None if self.ok else "session 3")

    def release(self, instrument, engine): self.released += 1


def _engine(fail=False, ok=True):
    s = _FakeSource(fail=fail)
    q = queue.Queue()
    return sg_gui.SourceEngine(_FakeHub(s, ok=ok), q), s, q


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_apply_cw_sets_freq_power_and_rf_on():
    eng, s, q = _engine()
    eng.enqueue(("apply", 2.45e9, -10.0, True))
    eng.step_once()
    assert s.freq == 2.45e9 and s.power == -10.0 and s.rf is True
    assert any(e[0] == "settled" for e in _drain(q))


def test_apply_rf_off_turns_off():
    eng, s, q = _engine()
    eng.enqueue(("apply", 2.45e9, -10.0, True)); eng.step_once()
    eng.enqueue(("apply", 2.45e9, -10.0, False)); eng.step_once()
    assert s.rf is False


def test_step_sweep_walks_points():
    eng, s, q = _engine()
    eng.enqueue(("apply", 1e9, -10.0, True)); eng.step_once()
    eng.enqueue(("step_sweep", [1e9, 2e9, 3e9], 0.0))
    for _ in range(3):
        eng.step_once()
    swept = [e[1] for e in _drain(q) if e[0] == "swept"]
    assert swept == [1e9, 2e9, 3e9]


def test_rf_off_safe_on_stop():
    eng, s, q = _engine()
    eng.enqueue(("apply", 2.45e9, -10.0, True)); eng.step_once()
    eng.rf_off_safe()
    assert s.rf is False


def test_error_publishes_absent():
    eng, s, q = _engine(fail=True)
    eng.enqueue(("apply", 2.45e9, -10.0, True)); eng.step_once()
    assert any(e[0] == "absent" for e in _drain(q))


def test_acquire_denied_publishes_absent_and_refuses():
    eng, s, q = _engine(ok=False)                # hub.acquire returns (False, "session 3")
    eng.enqueue(("apply", 2.45e9, -10.0, True))
    eng.step_once()
    evts = _drain(q)
    assert any(e[0] == "absent" and "tx" in e[1] for e in evts)   # tx denial surfaced
    assert s.rf is False and s.freq is None                       # source NOT driven while denied


def test_run_finally_forces_rf_off():
    import threading
    eng, s, q = _engine()
    eng.enqueue(("apply", 2.45e9, -10.0, True)); eng.step_once()   # RF on, tx held
    assert s.rf is True
    stop = threading.Event(); stop.set()          # already stopped -> loop body skipped
    eng.run(stop)                                 # finally must force RF off
    assert s.rf is False
