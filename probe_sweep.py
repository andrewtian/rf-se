"""Near-field-probe SWEEPER driven by the 8565EC (analyzer-as-sweeper, no source).

A near-field probe on the 8565EC input; the analyzer sweeps a span and the whole
level-vs-freq trace is pulled over the bus each sweep (never screen-read). The
ProbeSweeper drives the AnalyzerLink lifecycle automatically -- it ensure()s the
link before every sweep, so a dropped link transparently reconnects and the survey
continues. Each sweep yields a SweepFrame whose hottest bin is the leak the probe
is localizing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from connection import LinkDropped


@dataclass
class SweepFrame:
    freqs: list
    levels: list
    hot_freq_hz: float
    hot_level_dbm: float
    index: int
    status: object        # connection.LinkStatus at the time of the sweep


class ProbeSweeper:
    """Continuously sweep the near-field probe via an AnalyzerLink."""

    def __init__(self, link, span, n_points: int = 601, settle_s: float = 0.0,
                 acquire_fn=None):
        self.link = link
        self.span = tuple(span)
        self.n_points = int(n_points)
        self.settle_s = settle_s
        # acquire_fn(analyzer) -> (freqs, levels). None => swept-span read_sweep
        # (the default near-field survey). Provided => a stepped-CW synthetic sweep
        # flows through the SAME SweepFrame/render/auto-reconnect loop.
        self.acquire_fn = acquire_fn

    @property
    def reconnects(self) -> int:
        return self.link.reconnects

    def sweep_once(self, index: int = 0):
        """One sweep. Returns a SweepFrame, or None if the link is not available
        this round (absent / invalid / just dropped -- the caller can retry)."""
        if not self.link.ensure():
            return None
        try:
            if self.acquire_fn is not None:
                freqs, levels = self.link.read_via(self.acquire_fn)
            else:
                freqs, levels = self.link.read_sweep(self.n_points, self.settle_s)
        except LinkDropped:
            return None                      # dropped mid-sweep; next ensure() reconnects
        if not levels:
            return None
        hot_i = max(range(len(levels)), key=lambda k: levels[k])
        return SweepFrame(freqs, levels, freqs[hot_i], levels[hot_i], index, self.link.status())

    def run(self, sweeps: int, on_frame=None) -> list:
        """Run `sweeps` sweeps; return the SweepFrames produced (a dropped sweep
        yields no frame but auto-reconnects for the next one)."""
        frames = []
        for i in range(sweeps):
            frame = self.sweep_once(i)
            if frame is not None:
                frames.append(frame)
                if on_frame is not None:
                    on_frame(frame)
            if self.settle_s:
                time.sleep(self.settle_s)
        return frames


_RAMP = " .:-=+*#%@"


def render_frame(frame: SweepFrame, width: int = 60) -> str:
    """A compact ASCII view of one sweep: a status header (DETECTED + VALID), a
    level-vs-freq sparkline downsampled to `width` columns, and the hot bin."""
    st = frame.status
    if getattr(st, "valid", False):
        flag = "DETECTED + VALID"
    elif getattr(st, "detected", False):
        flag = "DETECTED (INVALID)"
    else:
        flag = "NOT DETECTED"
    head = (f"8565EC sweeper [{getattr(st, 'model', '') or '?'}] {flag}  "
            f"state={getattr(st, 'state', '?')}")
    lv = frame.levels
    n = len(lv)
    cols = []
    for c in range(width):
        a = c * n // width
        b = max(a + 1, (c + 1) * n // width)
        cols.append(max(lv[a:b]))
    lo, hi = min(cols), max(cols)
    rng = (hi - lo) or 1.0
    bar = "".join(_RAMP[min(len(_RAMP) - 1, int((v - lo) / rng * (len(_RAMP) - 1)))] for v in cols)
    f0, f1 = frame.freqs[0] / 1e9, frame.freqs[-1] / 1e9
    hot = f"HOT {frame.hot_freq_hz / 1e9:.3f} GHz @ {frame.hot_level_dbm:.1f} dBm"
    return f"{head}\n{f0:.2f} [{bar}] {f1:.2f} GHz\n{hot}"
