"""Cross-instance synchronization for the SE substitution campaign (task 31).

In the golden deployment the TX source and the RX analyzer live on TWO different networked
instances (two qemus, two bridges). The coordinator is nonetheless a SINGLE process issuing
sequential BLOCKING bus transactions, so ordering is guaranteed regardless of which instance a
call lands on -- network latency delays a transaction but cannot reorder it. These tests pin the
required per-frequency handshake:

  set source power+freq  ->  rf_on  ->  source.await_settled (settle + *OPC?)  ->  analyzer read

i.e. the analyzer NEVER reads before the source has retuned AND settled at that frequency.

Hardware-free: a recording source + recording analyzer share one global event log so the
interleaving the coordinator produces is asserted directly.

Run:  uv run python -m pytest rf-se/se299/tests/test_sync.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import loop


class _Recorder:
    def __init__(self):
        self.events = []           # ordered (actor, action, arg) across BOTH instruments

    def log(self, actor, action, arg=None):
        self.events.append((actor, action, arg))


class RecSource:
    """A source instance (TX) that records every call; await_settled logs a SETTLE event."""
    def __init__(self, rec): self.rec = rec; self.freq = None
    def set_power(self, p): self.rec.log("source", "set_power", p)
    def set_freq(self, f): self.freq = f; self.rec.log("source", "set_freq", f)
    def rf_on(self): self.rec.log("source", "rf_on")
    def rf_off(self): self.rec.log("source", "rf_off")
    def await_settled(self, settle_s=0.05, use_opc=True):
        self.rec.log("source", "await_settled", (settle_s, use_opc))


class RecAnalyzer:
    """An analyzer instance (RX) that records configure + every measure_peak/measure_floor (the
    reads). measure_floor (the source-off SAMPLE-detector floor read, C2/P1-4) is logged as its
    own action so the read-order assertions below (which filter on "measure_peak") continue to
    track only the RF-ON tone read, unaffected by the floor read that always happens RF-OFF."""
    def __init__(self, rec, level=-40.0): self.rec = rec; self.level = level
    def configure(self, *a): self.rec.log("analyzer", "configure", a)
    def measure_peak(self, f_hz, settle_s):
        self.rec.log("analyzer", "measure_peak", f_hz)
        return (f_hz, self.level)
    def measure_floor(self, f_hz, settle_s):
        self.rec.log("analyzer", "measure_floor", f_hz)
        return (f_hz, self.level)


def _one_band_cfg():
    # a single 2-point band keeps the event log short + readable
    band = config.BandPlan("t", 1e9, 2e9, 2, 14.0, 12.0, -150.0, target_se_db=100.0)
    return config.Campaign(bands=(band,), label="sync-test")


def _indices(events, actor, action):
    return [i for i, (ac, an, _) in enumerate(events) if ac == actor and an == action]


def test_source_settled_before_every_analyzer_read_reference():
    rec = _Recorder()
    loop.acquire_reference(_one_band_cfg(), RecSource(rec), RecAnalyzer(rec))
    ev = rec.events
    # every analyzer read that follows an rf_on (the reference read, RF ON) must have a
    # source.await_settled between that rf_on and the read -- never a read mid-transition.
    for i, (actor, action, _) in enumerate(ev):
        if actor == "analyzer" and action == "measure_peak":
            # find the most recent source RF state before this read
            prior = ev[:i]
            last_rf = next((a for a in reversed(prior)
                            if a[0] == "source" and a[1] in ("rf_on", "rf_off")), None)
            if last_rf and last_rf[1] == "rf_on":
                # a settled must sit between the rf_on and this read
                rf_on_idx = max(j for j, e in enumerate(prior) if e[0:2] == ("source", "rf_on"))
                settled = [j for j, e in enumerate(prior)
                           if e[0:2] == ("source", "await_settled") and j > rf_on_idx]
                assert settled, f"analyzer read at {i} with RF ON but no source settle first"


def test_source_freq_set_before_analyzer_read_each_point():
    # per point the source frequency is set BEFORE the analyzer measures at that frequency
    rec = _Recorder()
    loop.acquire_reference(_one_band_cfg(), RecSource(rec), RecAnalyzer(rec))
    ev = rec.events
    set_freqs = _indices(ev, "source", "set_freq")
    reads = _indices(ev, "analyzer", "measure_peak")
    assert set_freqs and reads
    # the first set_freq precedes the first read; ordering is monotone (set, read, set, read...)
    assert set_freqs[0] < reads[0]


def test_await_settled_uses_configured_settle_and_opc():
    rec = _Recorder()
    cfg = config.Campaign(bands=(config.BandPlan("t", 1e9, 1e9, 1, 14.0, 12.0, -150.0),),
                          source=config.SourceSettings(settle_s=0.123, use_opc=False))
    loop.acquire_reference(cfg, RecSource(rec), RecAnalyzer(rec))
    settled = [arg for actor, action, arg in rec.events
               if actor == "source" and action == "await_settled"]
    assert settled and settled[0] == (0.123, False)     # coordinator passes cfg.source through


def test_wall_pass_also_settles_before_read():
    rec = _Recorder()
    cfg = _one_band_cfg()
    ref = loop.acquire_reference(cfg, RecSource(_Recorder()), RecAnalyzer(_Recorder()))
    rec2 = _Recorder()
    loop.measure_wall(cfg, RecSource(rec2), RecAnalyzer(rec2), ref)
    ev = rec2.events
    # the wall read (RF ON) is preceded by a settle after rf_on
    for i, (actor, action, _) in enumerate(ev):
        if actor == "analyzer" and action == "measure_peak":
            rf_on_idx = max((j for j, e in enumerate(ev[:i]) if e[0:2] == ("source", "rf_on")),
                            default=None)
            if rf_on_idx is not None:
                assert any(e[0:2] == ("source", "await_settled")
                           for e in ev[rf_on_idx:i]), "wall read without a preceding settle"
