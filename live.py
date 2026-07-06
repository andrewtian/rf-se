"""Live (moving) 8565EC spectrum display for the near-field-probe sweeper.

Two cleanly separated units so the whole thing is hardware-free testable:

  LiveSpectrumModel : pure data. Wraps a probe_sweep.ProbeSweeper and pumps one
                      SweepFrame per step(), tracking the latest frame and the hot-bin
                      frequency history. NO matplotlib -- fully unit-testable, and the
                      thing the "is this actually live?" proof asserts against.

  LiveSpectrumGUI   : the PySide6 + pyqtgraph view over a LiveSpectrumModel. Qt is imported
                      LAZILY (in __init__ / run) so importing this module needs no Qt -- select
                      QT_QPA_PLATFORM=offscreen BEFORE construction in headless/test paths.

The data path is the production one: LiveSpectrumModel <- ProbeSweeper <- AnalyzerLink
<- NetworkTransport <- gpib bridge <- FakeBackend(signal="moving") (or a real 8565EC
over the network GPIB bridge). build_live() shares the exact construction the CLI and
the tests use, via cli.build_analyzer_link, so 'sim' / 'net:HOST:PORT:GPIBADDR' /
'auto' / a VISA string all work identically here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe_sweep


# ============================================================== model (no matplotlib)

class LiveSpectrumModel:
    """Pure-data live model over a ProbeSweeper: one SweepFrame per step()."""

    HISTORY_CAP = 200

    def __init__(self, sweeper):
        self.sweeper = sweeper
        self._i = 0
        self.latest = None            # the most recent SweepFrame (or None until first frame)
        self.hot_history = []         # hot-bin frequency (Hz) per produced frame, capped
        self.frame_count = 0          # number of frames actually produced

    def step(self):
        """Pull one sweep. Returns the SweepFrame, or None if the link was not
        available this round (absent / just dropped -- the next step reconnects)."""
        frame = self.sweeper.sweep_once(self._i)
        self._i += 1
        if frame is not None:
            self.latest = frame
            self.frame_count += 1
            self.hot_history.append(frame.hot_freq_hz)
            if len(self.hot_history) > self.HISTORY_CAP:
                self.hot_history = self.hot_history[-self.HISTORY_CAP:]
        return frame


# ============================================================== gui (PySide6 + pyqtgraph)

class LiveSpectrumGUI:
    """PySide6 + pyqtgraph view over a LiveSpectrumModel: a curve for the spectrum, a marker for the
    hot bin, and a readout label. Unlike se_gui/walkaround this view has no operator controls and no
    worker thread -- update() itself pulls one sweep per timer tick (unchanged data flow); only the
    library changes. The class holds a QMainWindow (self.window); Qt is imported lazily (model stays
    Qt-free -- select QT_QPA_PLATFORM=offscreen before import in headless/test paths).

    SUPERSEDED: the `cli.py live` verb now opens `sa_gui.SpectrumAnalyzerPanel` (via `cmd_sa`); this
    class is kept importable but is no longer the operator entry point."""

    def __init__(self, model, title="8565EC live spectrum (near-field probe)"):
        self.model = model
        import pyqtgraph as pg                    # lazy: importing live.py needs no Qt
        from PySide6 import QtWidgets, QtGui
        import qt_common
        self._pg, self._QtWidgets = pg, QtWidgets
        qt_common.ensure_app()

        self.window = QtWidgets.QMainWindow()
        self.window.setWindowTitle(title)
        central = QtWidgets.QWidget()
        self.window.setCentralWidget(central)
        col = QtWidgets.QVBoxLayout(central)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.text = QtWidgets.QLabel("")
        self.text.setFont(mono)
        col.addWidget(self.text)
        self.plot = qt_common.new_plot(title, "frequency (GHz)", "level (dBm)")
        col.addWidget(self.plot, 1)
        span = self._span_ghz()
        if span is not None:
            self.plot.setXRange(span[0], span[1])
        self.plot.setYRange(-100.0, 0.0)
        self.line = self.plot.plot([], [], pen=pg.mkPen("#1f77b4", width=1.0))
        self.hot_marker = pg.ScatterPlotItem(size=10, brush=pg.mkBrush("#d62728"))
        self.plot.addItem(self.hot_marker)
        self._timer = None

    def _span_ghz(self):
        span = getattr(self.model.sweeper, "span", None)
        if span and len(span) == 2:
            return (span[0] / 1e9, span[1] / 1e9)
        return None

    def update(self, _=None):
        """Advance the model one frame and push it into the plot. Returns the artists. A None frame
        (link absent this round) leaves the display unchanged."""
        frame = self.model.step()
        if frame is None:
            return (self.line, self.hot_marker, self.text)
        freqs_ghz = [f / 1e9 for f in frame.freqs]
        self.line.setData(freqs_ghz, frame.levels)
        self.hot_marker.setData([frame.hot_freq_hz / 1e9], [frame.hot_level_dbm])
        state = getattr(frame.status, "state", "?")
        self.text.setText(
            f"f={frame.hot_freq_hz / 1e9:.4f} GHz  lvl={frame.hot_level_dbm:.1f} dBm  "
            f"frame {self.model.frame_count}  status {state}")
        # rescale: x to the span, y to the data (with a small pad)
        if freqs_ghz:
            self.plot.setXRange(min(freqs_ghz), max(freqs_ghz))
        lo, hi = min(frame.levels), max(frame.levels)
        pad = max(1.0, (hi - lo) * 0.1)
        self.plot.setYRange(lo - pad, hi + pad)
        return (self.line, self.hot_marker, self.text)

    def run(self, interval_ms=200, frames=None):
        """Interactive live view: a QTimer calls update() every interval_ms, then the Qt event loop
        runs. Blocks until the window closes. `frames` is accepted for compatibility and ignored."""
        import qt_common
        self._timer = qt_common.run_live(self.window, self.update, interval_ms)
        return self._timer


# ============================================================== shared construction

def build_live(analyzer_addr, span_ghz=(1.0, 6.0), n_points=601, retries=3):
    """Build a LiveSpectrumModel through the production connection path.

    Uses cli.build_analyzer_link to resolve analyzer_addr ('sim' / 'auto' /
    'net:HOST:PORT:GPIBADDR' / a VISA string) into an auto-reconnecting AnalyzerLink,
    wraps it in a ProbeSweeper, and returns (LiveSpectrumModel, simulated_flag) so the
    GUI and the tests share ONE construction path."""
    import cli                                     # lazy: avoid a live<->cli import cycle
    span = (float(span_ghz[0]) * 1e9, float(span_ghz[1]) * 1e9)
    link, simulated = cli.build_analyzer_link(analyzer_addr, span, retries=retries)
    sweeper = probe_sweep.ProbeSweeper(link, span, n_points=n_points)
    return LiveSpectrumModel(sweeper), simulated
