"""Range mode for the se299 bench -- one of several pluggable bench MODES.

Purpose: step a WIDE-BANDWIDTH tone across the full range with the arrow keys and watch the RX show
ONLY that range. The TX PAINTS the range (the source step-sweeps across [center +/- range/2] on a
continuous loop) while the RX sits in max-hold over the same span, so a wide 'tone' fills the range
window and the operator does not need pinpoint TX/RX alignment to see it. Up/Down on the center
spinner shift the whole range by the freqstep ladder (a sensible fraction of the frequency);
Shift = fine. The only view is the range plot plus the center / range / power controls.

Modular contract (BenchMode): a mode is any object exposing `.widget` (a QWidget), `.start(ms)`,
`.stop()`, `.suspend()`, `.resume()`. bench_gui hosts modes in a tab strip and suspends the inactive
one (releasing its instrument leases) so only the active mode drives the shared 68367C + 8565EC.

Pure/Qt split: RangeModel carries NO Qt (unit-testable); the panel imports Qt lazily.
"""
from __future__ import annotations

import os
import queue as _queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODE_NAME = "Range (paint + step)"
DEFAULT_RANGE_HZ = 500e6            # the painted bandwidth shown (the "range")
DEFAULT_POINTS = 21                 # source steps across the range (paint resolution)
FLOOR_MIN_HZ, FLOOR_MAX_HZ = 10e6, 40e9    # the source's rated band = the steppable range


class RangeModel:
    """Pure state for range mode: center, range (span/painted bandwidth), power, RF, paint points."""

    def __init__(self):
        self.center_hz = 2.45e9
        self.range_hz = DEFAULT_RANGE_HZ
        self.power_dbm = -10.0
        self.rf_on = False             # SAFETY: RF off by default
        self.points = DEFAULT_POINTS
        self.peak_dbm = None

    def span_lo_hz(self) -> float:
        return max(FLOOR_MIN_HZ, self.center_hz - self.range_hz / 2.0)

    def span_hi_hz(self) -> float:
        return min(FLOOR_MAX_HZ, self.center_hz + self.range_hz / 2.0)

    def paint_points(self) -> list:
        """The source frequencies that paint the range (inclusive lo..hi, self.points steps)."""
        lo, hi = self.span_lo_hz(), self.span_hi_hz()
        n = max(2, int(self.points))
        return [lo + (hi - lo) * k / (n - 1) for k in range(n)]

    def set_peak(self, level_dbm):
        self.peak_dbm = None if level_dbm is None else float(level_dbm)

    def readout_text(self) -> str:
        pk = f"   peak {self.peak_dbm:.1f} dBm" if self.peak_dbm is not None else ""
        state = "PAINTING" if self.rf_on else "idle"
        return (f"center {self.center_hz / 1e9:.4f} GHz   range {self.range_hz / 1e6:.1f} MHz "
                f"[{self.span_lo_hz() / 1e9:.4f} - {self.span_hi_hz() / 1e9:.4f} GHz]   "
                f"{self.power_dbm:.1f} dBm   RF {state}{pk}")


class RangeModePanel:
    """The Range bench mode: a lean range-only view (RX spanned plot + center/range/power controls)
    that paints a wide tone across the range and steps it with the arrow keys. Reuses the tested
    SpectrumEngine (RX) + SourceEngine (TX) over the shared InstrumentHub."""

    name = MODE_NAME

    def __init__(self, hub):
        import pyqtgraph as pg
        from PySide6 import QtWidgets, QtGui
        import qt_common
        import sa_gui
        import sg_gui
        self._pg = pg
        qt_common.ensure_app()

        self.hub = hub
        self.model = RangeModel()
        self._rxq, self._txq = _queue.Queue(), _queue.Queue()
        self.rx_engine = sa_gui.SpectrumEngine(hub, self._rxq)
        self.tx_engine = sg_gui.SourceEngine(hub, self._txq)
        self._rx_settings = sa_gui.SpectrumSettings()
        self._stop = threading.Event()
        self._threads, self._timer = [], None

        self.widget = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(self.widget)
        self.controls = self._build_controls(qt_common)
        root.addWidget(self.controls, 0)
        right = QtWidgets.QVBoxLayout()
        root.addLayout(right, 1)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.readout = QtWidgets.QLabel("")
        self.readout.setFont(mono)
        right.addWidget(self.readout)
        self.plot = qt_common.new_plot("range (painted tone)", "frequency (GHz)", "level (dBm)")
        right.addWidget(self.plot, 1)
        self._live = self.plot.plot([], [], pen=pg.mkPen("#1f77b4", width=1.0))
        self._hold = self.plot.plot([], [], pen=pg.mkPen("#d62728", width=1.2))
        self.rx_model = sa_gui.SpectrumModel()

    def _build_controls(self, qt_common):
        cp = qt_common.ControlPanel("RANGE MODE")
        self.spin_center = cp.add_freq_spin("center (GHz)", 0.01, 40.0, self.model.center_hz / 1e9,
                                            decimals=6, suffix="GHz", on_change=self._on_change)
        self.spin_range = cp.add_spin("range (MHz)", 1.0, 40000.0, self.model.range_hz / 1e6,
                                      step=50.0, decimals=1, suffix="MHz", on_change=self._on_change)
        self.spin_power = cp.add_spin("tone power (dBm)", -60.0, 17.0, self.model.power_dbm,
                                      step=1.0, decimals=1, suffix="dBm", on_change=self._on_change)
        self.spin_points = cp.add_spin("paint points", 2.0, 601.0, float(self.model.points),
                                       step=1.0, decimals=0, on_change=self._on_change)
        self.chk_rf = cp.add_checkbox("RF ON (paint)", False, self._on_change)
        cp.add_help("Arrows on 'center' shift the whole range (shift=fine). The TX paints the range; "
                    "the RX shows only that range in max-hold.")
        cp.add_stretch()
        return cp

    # -- modular BenchMode lifecycle -------------------------------------------------
    def start(self, interval_ms=200):
        from PySide6 import QtCore
        self._apply()
        self._stop.clear()
        for eng in (self.rx_engine, self.tx_engine):
            t = threading.Thread(target=eng.run, args=(self._stop,), daemon=True)
            t.start()
            self._threads.append(t)
        self._timer = QtCore.QTimer(self.widget)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def stop(self):
        self._stop.set()
        self.tx_engine.rf_off_safe()               # RF off invariant on leave/close
        if self._timer is not None:
            self._timer.stop()
        self._threads = []

    def suspend(self):
        self.tx_engine.rf_off_safe()
        self.rx_engine.suspend()
        self.tx_engine.suspend()

    def resume(self):
        self.rx_engine.resume()
        self.tx_engine.resume()
        self._apply()

    # -- control -> instruments ------------------------------------------------------
    def _apply(self, *_):
        m = self.model
        m.center_hz = self.spin_center.value() * 1e9
        m.range_hz = self.spin_range.value() * 1e6
        m.power_dbm = self.spin_power.value()
        m.points = int(self.spin_points.value())
        m.rf_on = self.chk_rf.isChecked()
        # RX: center on the range, span = the range, MAX-HOLD so the painted band accumulates
        s = self._rx_settings
        s.center_hz, s.span_hz = m.center_hz, m.range_hz
        s.max_hold, s.continuous, s.detector = True, True, "peak"
        self.rx_model.settings = s
        self.rx_model.reset_traces()
        self.rx_engine.enqueue(("apply_settings", s))
        # TX: paint the range with a continuously-looping step sweep (or RF off)
        if m.rf_on:
            pts = m.paint_points()
            self.tx_engine.enqueue(("apply", pts[0], m.power_dbm, True))     # power + RF on at lo
            self.tx_engine.enqueue(("step_sweep", pts, 0.0, True))            # loop=True -> keep painting
        else:
            self.tx_engine.enqueue(("apply", m.center_hz, m.power_dbm, False))
            self.tx_engine.enqueue(("stop_sweep", None))

    def _on_change(self, *_):
        self._apply()

    def _drain(self):
        try:
            while True:
                evt = self._rxq.get_nowait()
                if evt[0] == "trace":
                    self.rx_model.set_trace(evt[1], evt[2])
        except _queue.Empty:
            pass
        try:
            while True:
                self._txq.get_nowait()          # tx events (settled/swept) not shown here
        except _queue.Empty:
            pass

    def render(self):
        fg, live, mh = self.rx_model.curve()
        self._live.setData(fg, live)
        self._hold.setData(fg if mh else [], mh)
        self.model.set_peak(max(mh) if mh else (max(live) if live else None))
        self.readout.setText(self.model.readout_text())
        return (self._live, self._hold)

    def _tick(self):
        self._drain()
        return self.render()


def build_range_mode(hub):
    return RangeModePanel(hub)
