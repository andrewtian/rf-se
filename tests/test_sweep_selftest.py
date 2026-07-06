"""Hardware-free tests for tools/sweep_selftest.py: the pure GO/NO-GO classification and the blank-trace
retry. The live low->high pass is exercised on the bench; these lock the decision logic that turns a
trace + source state into PASS / NO-TONE / NO-SRC / BLANK, and prove the read retries a cleared sweep."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import sweep_selftest as ss


def _floor_trace(n=601, floor=-90.0):
    # a realistic noisy floor (carries variation -> NOT degenerate), no tone
    return [floor + (i % 3) * 0.4 for i in range(n)]


def _tone_trace(n=601, floor=-90.0, peak=-20.0):
    t = _floor_trace(n, floor)
    t[n // 2] = peak
    return t


def _blank_trace(n=601, rail=-110.0):
    return [rail] * n                                   # cleared / unswept: every point identical


def test_classify_pass_when_tone_clears_margin_and_source_ok():
    freqs = list(range(601))
    verdict, over = ss._classify(True, freqs, _tone_trace())
    assert verdict == "PASS" and over >= ss.TONE_MARGIN_DB


def test_classify_no_tone_when_source_ok_but_no_peak():
    verdict, _ = ss._classify(True, list(range(601)), _floor_trace())
    assert verdict == "NO-TONE"                         # source fine, but nothing rises over the floor


def test_classify_no_src_when_tone_present_but_source_not_ok():
    verdict, _ = ss._classify(False, list(range(601)), _tone_trace())
    assert verdict == "NO-SRC"                          # a tone is there, but the source was not leveled/on-freq


def test_classify_blank_beats_everything_on_a_cleared_trace():
    # a cleared/unswept trace (all points identical) is a READ bug -> BLANK regardless of source state
    for src_ok in (True, False):
        verdict, _ = ss._classify(src_ok, list(range(601)), _blank_trace())
        assert verdict == "BLANK"
    assert ss._classify(True, [], [])[0] == "BLANK"     # empty trace also blank


class _FakeAna:
    """Minimal analyzer exercising _gui_read: scripts a sequence of traces returned by successive
    read_trace() calls so a blank-then-live sequence proves the retry, and records preselector peaks."""
    def __init__(self, traces):
        self._traces = list(traces)
        self.presel_calls = 0
        self.freq_applies = []

    def configure(self, *a): pass
    def set_attenuation(self, **k): pass
    def arm_and_wait(self, timeout_s=6.0, fresh=True): pass

    def set_frequency(self, *, center_hz=None, span_hz=None):
        self.freq_applies.append((center_hz, span_hz))

    def peak_preselector(self, f_hz):
        self.presel_calls += 1
        return 130

    def read_trace(self, trace="A", calibrate=False):
        t = self._traces.pop(0) if len(self._traces) > 1 else self._traces[0]
        return ([0.0] * len(t), t)


def test_gui_read_retries_past_a_blank_first_sweep():
    # first sweep blank (cleared), second sweep a real tone -> _gui_read must return the real trace
    an = _FakeAna([_blank_trace(), _tone_trace()])
    _, levels = ss._gui_read(an, 1e9, 5e6, 0.0)
    assert (max(levels) - min(levels)) > ss._DEGENERATE_SPAN_DB   # got the live trace, not the blank


def test_gui_read_peaks_preselector_and_restores_window_high_band():
    an = _FakeAna([_tone_trace()])
    ss._gui_read(an, 10e9, 5e6, 0.0)
    assert an.presel_calls == 1                          # peaked above 2.9 GHz
    # the LAST frequency apply restores the measurement window (CF + span) after the preselector zoom
    assert an.freq_applies[-1] == (10e9, 5e6)


def test_gui_read_skips_preselector_low_band():
    an = _FakeAna([_tone_trace()])
    ss._gui_read(an, 1e9, 5e6, 0.0)
    assert an.presel_calls == 0                          # no preselector below 2.9 GHz
