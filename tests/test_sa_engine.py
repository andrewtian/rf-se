"""SpectrumEngine tests with a FAKE analyzer driver + fake hub: command-queue apply, sweep+read
publish, timeout-vs-sweep-time, suspend/resume, absent-on-error. No Qt, no threads (step_once).

Run:  uv run python -m pytest rf-se/se299/tests/test_sa_engine.py -q
"""
from __future__ import annotations

import os
import queue
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sa_gui


class _FakeAnalyzer:
    def __init__(self, fail=False, fail_set_timeout=False):
        self.fail = fail
        self.fail_set_timeout = fail_set_timeout
        self.timeout_ms = 0
        self.applied = []
        self.t = self
        self.center = None
        self.fresh_calls = []

    def set_timeout(self, ms):
        if self.fail_set_timeout:
            raise IOError("set_timeout failed")
        self.timeout_ms = ms

    def sweep_time(self):
        return 8.0

    def set_frequency(self, center_hz=None, span_hz=None):
        self.center, self.applied = center_hz, self.applied + [("freq", center_hz, span_hz)]

    def configure(self, rbw_hz, vbw_hz, ref_dbm, detector):
        self.applied.append(("cfg", rbw_hz, ref_dbm))

    def set_sweep_time(self, seconds=None, auto=False): self.applied.append(("st", seconds, auto))

    def set_max_hold(self, on, trace="A"): self.applied.append(("mh", on))

    def arm_and_wait(self, timeout_s=10.0, fresh=True):
        self.fresh_calls.append(fresh)
        if self.fail:
            raise IOError("bridge dropped")

    def read_trace(self, trace="A", calibrate=False):
        return ([2.4e9, 2.45e9, 2.5e9], [-90.0, -40.0, -88.0])

    def marker_peak(self):
        return (2.45e9, -40.0)

    def peak_preselector(self, f_hz, span_hz=50e6, rbw_hz=1e3):
        self.applied.append(("presel", f_hz))

    def set_resolution_bandwidth(self, rbw_hz=None, auto=False):
        self.applied.append(("rbw", rbw_hz, auto))       # engine restores RBW after a preselector zoom


class _FakeAnalyzerNoSweepTime(_FakeAnalyzer):
    """A driver that does not expose sweep_time() -- forces the conservative ceiling."""
    sweep_time = None


class _FakeHub:
    def __init__(self, analyzer, ok=True):
        self._an = analyzer
        self.ok = ok
        self.acquired = 0
        self.released = 0

    @property
    def analyzer(self): return self._an

    def acquire(self, instrument, engine):
        self.acquired += 1
        return (self.ok, None if self.ok else "session 3 scope 18 ttl 30s")

    def release(self, instrument, engine): self.released += 1


def _engine(fail=False, ok=True, an=None):
    if an is None:
        an = _FakeAnalyzer(fail=fail)
    q = queue.Queue()
    eng = sa_gui.SpectrumEngine(_FakeHub(an, ok=ok), q)
    return eng, an, q


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_apply_settings_then_sweep_publishes_trace():
    eng, an, q = _engine()
    s = sa_gui.SpectrumSettings(center_hz=2.45e9, span_hz=100e6, continuous=True)
    eng.enqueue(("apply_settings", s))
    eng.step_once()
    evts = _drain(q)
    assert ("freq", 2.45e9, 100e6) in an.applied
    assert any(e[0] == "trace" and e[2] == [-90.0, -40.0, -88.0] for e in evts)


def test_engine_reads_fresh_after_a_change_and_parked_when_unchanged():
    """The steady-state live feed must read a SINGLE sweep when parked (fresh=False) to run fast, but a
    FRESH double sweep (fresh=True) on any tick that changed analyzer state (an apply/retune), so a
    CLRW-cleared trace is flushed before the read. Regression for the perf speedup + its safety."""
    eng, an, q = _engine()
    s = sa_gui.SpectrumSettings(center_hz=2.45e9, span_hz=5e6, continuous=True)
    eng.enqueue(("apply_settings", s))
    eng.step_once()                                       # apply tick: state changed -> fresh
    assert an.fresh_calls[-1] is True
    eng.step_once()                                       # parked tick: continuous, no change -> single sweep
    assert an.fresh_calls[-1] is False


def test_empty_trace_is_not_published_keeps_last():
    """STATE CONSISTENCY: a momentary empty/partial read must NOT publish ('trace', [], []) -- that would
    blank a good PSD. The engine skips it (keep the last trace, retry next tick); only a RAISED error ->
    'absent' should change the display away from live data. Regression for 'the PSD disappears'."""
    class _EmptyRead(_FakeAnalyzer):
        def read_trace(self, trace="A", calibrate=False):
            return ([], [])
    eng, an, q = _engine(an=_EmptyRead())
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(center_hz=2.45e9, span_hz=5e6, continuous=True)))
    eng.step_once()
    evts = _drain(q)
    assert not any(e[0] == "trace" for e in evts)   # empty read is NOT published (no blank)
    assert not any(e[0] == "absent" for e in evts)  # and it is NOT a false fault (it's a transient)


def test_timeout_raised_above_sweep_time():
    eng, an, q = _engine()
    s = sa_gui.SpectrumSettings(sweep_time_s=3.0, sweep_auto=False, continuous=True)
    eng.enqueue(("apply_settings", s))
    eng.step_once()
    assert an.timeout_ms >= 3000                       # transport timeout > sweep time


def test_marker_peak_command_publishes_marker():
    eng, an, q = _engine()
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(continuous=False)))
    eng.step_once()
    eng.enqueue(("marker_peak", None))
    eng.step_once()
    assert any(e[0] == "marker" and e[1] == 2.45e9 for e in _drain(q))


def test_error_publishes_absent():
    eng, an, q = _engine(fail=True)
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(continuous=True)))
    eng.step_once()
    assert any(e[0] == "absent" for e in _drain(q))


def test_suspend_stops_sweeping_and_releases():
    eng, an, q = _engine()
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(continuous=True)))
    eng.step_once()                                    # acquires + sweeps
    _drain(q)                                          # discard the pre-suspend trace
    eng.suspend()
    assert eng.hub.released >= 1                        # released ownership on suspend
    eng.step_once()                                    # suspended -> no new sweep
    assert not any(e[0] == "trace" for e in _drain(q))  # no trace published after suspend
    assert eng.suspended is True


def test_auto_sweep_timeout_uses_queried_sweep_time():
    eng, an, q = _engine()                              # default fake analyzer sweep_time() -> 8.0
    s = sa_gui.SpectrumSettings(sweep_auto=True, continuous=True)
    eng.enqueue(("apply_settings", s))
    eng.step_once()
    assert an.timeout_ms >= 16000                       # 8.0s sweep * 2000


def test_auto_sweep_conservative_when_no_getter():
    an = _FakeAnalyzerNoSweepTime()                      # driver with no sweep_time()
    eng, an, q = _engine(an=an)
    s = sa_gui.SpectrumSettings(sweep_auto=True, continuous=True)
    eng.enqueue(("apply_settings", s))
    eng.step_once()
    assert an.timeout_ms >= 60000                       # 30s conservative ceiling * 2000


def test_set_timeout_failure_publishes_absent():
    an = _FakeAnalyzer(fail_set_timeout=True)
    eng, an, q = _engine(an=an)
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(continuous=True)))
    eng.step_once()
    assert any(e[0] == "absent" for e in _drain(q))     # failure surfaced, not swallowed


def test_preselector_peak_command_calls_driver():
    eng, an, q = _engine()
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(continuous=False)))
    eng.step_once()
    eng.enqueue(("preselector_peak", None))
    eng.step_once()
    assert any(c[0] == "presel" for c in an.applied)


def test_preselector_peak_restores_measurement_window_before_trace_read():
    """peak_preselector zooms to a wide span (200 MHz) + MKCF-recenters to TUNE the YIG, leaving the
    sweep at 200 MHz. The engine MUST re-assert the intended CF + span AFTER peaking (the DAC persists)
    so the full-trace read is the measurement window, not 200 MHz -- else the PSD (x-axis pinned to the
    model span) reads BLANK above 2.9 GHz. Regression for the live 'increase freq -> blank graph' bug."""
    eng, an, q = _engine()
    s = sa_gui.SpectrumSettings(center_hz=10e9, span_hz=5e6, continuous=True)
    eng.enqueue(("apply_settings", s))
    eng.enqueue(("preselector_peak", None))
    eng.step_once()
    # the LAST frequency apply must restore the 5 MHz window, and it must come AFTER the preselector peak
    freq_applies = [i for i, c in enumerate(an.applied) if c[0] == "freq"]
    presel_at = next(i for i, c in enumerate(an.applied) if c[0] == "presel")
    assert an.applied[freq_applies[-1]] == ("freq", 10e9, 5e6)      # restored to CF + model span
    assert freq_applies[-1] > presel_at                            # restore happens AFTER the peak


def test_preselector_restores_rbw_not_left_at_300khz():
    """peak_preselector zooms RBW to 300 kHz to tune the YIG; the engine MUST restore the parked RBW
    after, or the feed keeps sweeping at 300 kHz while the readout says 'auto' (coarse + inflated floor).
    Regression for feed-consistency gap C6."""
    eng, an, q = _engine()
    eng.enqueue(("apply_settings", sa_gui.SpectrumSettings(center_hz=10e9, span_hz=5e6, continuous=True)))
    eng.enqueue(("preselector_peak", None))
    eng.step_once()
    rbw_after = [c for i, c in enumerate(an.applied) if c[0] == "rbw"
                 and i > next(j for j, x in enumerate(an.applied) if x[0] == "presel")]
    assert rbw_after and rbw_after[-1][2] is True                   # RBW re-asserted to AUTO after the peak


# ---- model-vs-device reconciliation on the throttled read_state tick (gates C5 + C8) --------

class _StateAnalyzer(_FakeAnalyzer):
    """A fake analyzer that reports a scripted ABSOLUTE state + records cal invalidation, so the engine's
    reconcile-on-read_state path is testable hardware-free."""
    def __init__(self, state):
        super().__init__()
        self._state = state
        self.invalidated = 0

    def query_errors(self):
        return []

    def read_state(self):
        return self._state

    def invalidate_calibration(self):
        self.invalidated += 1


def _astate(**kw):
    import device_state as ds
    base = dict(center_hz=2.45e9, span_hz=5e6, rbw_hz=30e3, vbw_hz=30e3, ref_level_dbm=-10.0,
                atten_db=0.0, detector="POS", scale_db_div=10.0, aunits="DBM", sweep_time_s=0.05)
    base.update(kw)
    return ds.AnalyzerState(**base)


def _run_reconcile(an):
    eng, _, q = _engine(an=an)
    s = sa_gui.SpectrumSettings(center_hz=2.45e9, span_hz=5e6, ref_dbm=-10.0, detector="POS",
                                continuous=False)
    eng.enqueue(("apply_settings", s))
    eng.step_once()                                    # apply sets _settings
    eng.enqueue(("read_state", None))
    eng.step_once()                                    # throttled tick -> _emit_state reconciles
    rx = [e for e in _drain(q) if e[0] == "rx_state"]
    return rx[-1][1] if rx else None


def test_emit_state_reconcile_clean_no_drift_no_invalidate():
    an = _StateAnalyzer(_astate())                     # device == desired
    st = _run_reconcile(an)
    assert st is not None and st["drift"] == [] and an.invalidated == 0


def test_emit_state_amplitude_drift_clears_cal_and_reports():
    an = _StateAnalyzer(_astate(ref_level_dbm=-5.0))   # device RL diverged from desired -10
    st = _run_reconcile(an)
    assert st["drift"] and any("ref_level_dbm" in d for d in st["drift"])
    assert an.invalidated >= 1                          # amplitude drift -> binary cal cache cleared


def test_emit_state_frequency_drift_reported_but_no_cal_clear():
    an = _StateAnalyzer(_astate(center_hz=2.40e9))     # device CF diverged (not an amplitude field)
    st = _run_reconcile(an)
    assert any("center_hz" in d for d in st["drift"]) and an.invalidated == 0
