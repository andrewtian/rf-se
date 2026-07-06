"""Full-fidelity Spectrum Analyzer panel for the se299 bench (PySide6 + pyqtgraph), plus its pure
model. SpectrumModel carries NO Qt; the panel + engine are added in later tasks. Supersedes the
simple live-spectrum GUI in live.py.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import device_state                 # ABSOLUTE-state reconciliation on the throttled read_state tick

PRESELECTOR_MIN_HZ = 2.9e9          # harmonic-mixing bands above this need preselector peaking


@dataclass
class SpectrumSettings:
    center_hz: float = 2.45e9
    span_hz: float = 100e6
    rbw_hz: float = 1e5
    rbw_auto: bool = True
    vbw_hz: float = 1e5
    vbw_auto: bool = True
    sweep_time_s: float = 0.05
    sweep_auto: bool = True
    ref_dbm: float = 0.0
    scale_db_div: float = 10.0
    atten_db: float = 10.0
    atten_auto: bool = True
    detector: str = "peak"
    continuous: bool = True
    max_hold: bool = False
    video_avg: bool = False
    avg_count: int = 8
    preselector_on: bool = False


class SpectrumModel:
    """Pure accumulator for the SA display: latest live trace, per-bin max-hold, running
    video-average, peak marker, ABSENT state, and the settings the engine applies."""

    def __init__(self):
        self.settings = SpectrumSettings()
        self._freqs = []            # Hz
        self._live = []             # dBm (post video-average if enabled)
        self._maxhold = []          # dBm per-bin max, or [] when disabled
        self._avg_n = 0
        self.marker = None          # (freq_hz, level_dbm) or None
        self.absent = False

    def reset_traces(self):
        self._maxhold = []
        self._avg_n = 0

    def set_trace(self, freqs_hz, levels_dbm):
        self.absent = False
        freqs_hz = list(freqs_hz)
        levels = list(levels_dbm)
        raw = list(levels)
        if self.settings.video_avg and self._live and len(self._live) == len(levels):
            self._avg_n += 1
            a = 1.0 / max(1, min(self._avg_n + 1, self.settings.avg_count))
            levels = [(1 - a) * p + a * n for p, n in zip(self._live, levels)]
        else:
            self._avg_n = 0
        self._freqs, self._live = freqs_hz, levels
        if self.settings.max_hold:
            if len(self._maxhold) != len(raw):
                self._maxhold = list(raw)
            else:
                self._maxhold = [max(m, n) for m, n in zip(self._maxhold, raw)]
        else:
            self._maxhold = []

    def set_marker(self, freq_hz, level_dbm):
        self.marker = (freq_hz, level_dbm)

    def set_absent(self, absent, reason=""):
        self.absent = bool(absent)
        self.reason = str(reason or "")           # the link's actionable reason (FAULT vs ABSENT)

    def curve(self):
        fg = [f / 1e9 for f in self._freqs]
        return (fg, list(self._live), list(self._maxhold))

    def preselector_applicable(self):
        return self.settings.center_hz >= PRESELECTOR_MIN_HZ

    def readout_text(self):
        if self.absent:
            # show the link's actionable reason VERBATIM so a FAULT ("power-cycle the ... adapter")
            # reads distinctly from a plain ABSENT (nothing on the bus).
            r = getattr(self, "reason", "")
            if r and "power-cycle" in r.lower():
                return f"analyzer FAULT -- {r}"
            return f"analyzer ABSENT -- {r}" if r else "analyzer ABSENT -- bridge unreachable"
        s = self.settings
        mk = ""
        if self.marker is not None:
            mk = f"   MK {self.marker[0] / 1e9:.4f} GHz {self.marker[1]:.1f} dBm"
        return (f"CF {s.center_hz / 1e9:.4f} GHz  SP {s.span_hz / 1e6:.1f} MHz  "
                f"RBW {'auto' if s.rbw_auto else f'{s.rbw_hz:.0f}'}  REF {s.ref_dbm:.0f} dBm{mk}")


import queue as _queue


class SpectrumEngine:
    """Background command-queue engine for the SA panel. Loop = drain commands -> apply to the
    analyzer -> (if continuous) one sweep + read -> publish. Owns rx via the hub; suspend()
    pauses the loop + releases ownership; resume() re-acquires + re-applies the current settings.
    step_once() is the test seam (call it directly; run() threads it)."""

    name = "sa"

    _PRESEL_RETRY = 6            # sweeps to keep retrying a high-band preselector peak until the tone
    #                             appears (covers the RX/TX thread race where the peak is requested
    #                             before the source settles); bounded so an absent tone can't churn forever

    def __init__(self, hub, out_queue):
        self.hub = hub
        self.q = out_queue
        self._cmds = _queue.Queue()
        self._settings = SpectrumSettings()
        self.suspended = False
        self._have_rx = False
        self._presel_pending = 0     # remaining high-band preselector-peak retry budget (see step_once)

    def enqueue(self, cmd):
        self._cmds.put(cmd)

    def suspend(self):
        self.suspended = True
        if self._have_rx:
            self.hub.release("rx", self)
            self._have_rx = False

    def resume(self):
        self.suspended = False
        self.enqueue(("apply_settings", self._settings))     # re-apply after handoff/reconnect

    def _acquire(self):
        if not self._have_rx:
            ok, who = self.hub.acquire("rx", self)
            if not ok:
                self.q.put(("absent", f"rx {who}"))
                return False
            self._have_rx = True
        return True

    def _effective_sweep_s(self, s):
        """Best estimate of how long a sweep takes, so the read timeout can exceed it.
        Manual mode: the set sweep time. Auto mode: query the analyzer's sweep_time() if it
        exposes one (exact), else a conservative ceiling comfortably above a realistic sweep."""
        if not s.sweep_auto:
            return max(0.05, float(s.sweep_time_s))
        getter = getattr(self.hub.analyzer, "sweep_time", None)
        if callable(getter):
            try:
                return max(0.05, float(getter()))
            except Exception:
                pass
        return 30.0

    def _apply(self, s):
        self._settings = s
        an = self.hub.analyzer
        # configure() sets amplitude/BW/detector but ENDS with "SP 0HZ" (its zero-span CW-read
        # heritage for the SE point measurement). Assert the real span AFTER it -- otherwise every
        # SA / Range / Point-Op sweep collapses to ZERO SPAN (power vs time at CF), the spectrum is a
        # flat line, and a tone shows as a raised flat line instead of a spectral PEAK. (Live-proven:
        # SP?=0 in the bench; reordering restores SP?=span and the tone appears at its frequency.)
        an.configure(0.0 if s.rbw_auto else s.rbw_hz, 0.0 if s.vbw_auto else s.vbw_hz,
                     s.ref_dbm, s.detector)
        an.set_frequency(center_hz=s.center_hz, span_hz=s.span_hz)
        an.set_sweep_time(seconds=None if s.sweep_auto else s.sweep_time_s, auto=s.sweep_auto)
        an.set_max_hold(s.max_hold)
        # raise the transport read timeout above the sweep time so a slow sweep does not time out
        eff = self._effective_sweep_s(s)
        t = getattr(an, "t", None)
        if t is not None:
            t.set_timeout(int(max(3000, eff * 2000)))

    def _emit_state(self):
        """Publish the 8565EC live state for a debug pane: the APPLIED CF/span/RBW/detector (from the
        current settings, no bus read) plus the analyzer's error queue (a wedge shows its reference-
        unlock codes here). Benign parser codes (100-199, e.g. 111 that a zero-span TS posts on every
        sweep) are dropped so the pane surfaces only meaningful faults. Defensive; only on 'read_state'
        (throttled) so it adds no per-sweep bus load."""
        an = self.hub.analyzer
        s = self._settings
        st = {"center_hz": s.center_hz, "span_hz": s.span_hz,
              "rbw_hz": (0.0 if s.rbw_auto else s.rbw_hz), "detector": s.detector, "errors": None,
              "actual": None, "drift": None}
        fn = getattr(an, "query_errors", None)
        if callable(fn):
            try:
                st["errors"] = [e for e in fn() if not (100 <= int(e) < 200)]
            except Exception:
                pass
        # RECONCILE model vs device: read the ABSOLUTE state and compare to intent. Runs ONLY on this
        # throttled tick (~1 Hz), so it adds no per-sweep bus load. An AMPLITUDE drift (out-of-band RL or
        # dB/div change) invalidates the binary cal so the feed recalibrates instead of publishing a wrong
        # amplitude -- the one path that could ship a numerically wrong trace. Best-effort: a wedge/absent
        # read must not crash the tick (the error queue above already flags a wedge).
        rs = getattr(an, "read_state", None)
        if callable(rs):
            try:
                actual = an.read_state()
                drifts = device_state.reconcile_analyzer(
                    actual, center_hz=s.center_hz, span_hz=s.span_hz, ref_level_dbm=s.ref_dbm,
                    detector=s.detector, rbw_hz=(None if s.rbw_auto else s.rbw_hz))
                st["actual"] = actual
                st["drift"] = [str(d) for d in drifts]
                if device_state.analyzer_amplitude_drift(drifts):
                    an.invalidate_calibration()
            except Exception:
                pass
        self.q.put(("rx_state", st))

    def step_once(self):
        if self.suspended:
            return
        if not self._acquire():
            return
        try:
            # CONFLATE the queue: arrow-key auto-repeat enqueues a fresh apply_settings (+ preselector)
            # per key event, but only the FINAL center matters. Coalesce a drain batch to one retune +
            # one preselector peak so we don't re-tune the (wedge-prone) analyzer N times per burst.
            pending_settings = None
            want_marker = want_preselector = want_state = False
            while not self._cmds.empty():
                kind, payload = self._cmds.get_nowait()
                if kind == "apply_settings":
                    pending_settings = payload                 # last settings win
                elif kind == "marker_peak":
                    want_marker = True
                elif kind == "preselector_peak":
                    want_preselector = True
                elif kind == "read_state":
                    want_state = True
            drained_apply = False
            if pending_settings is not None:
                self._apply(pending_settings)
                drained_apply = True
            # PRESELECTOR PEAK (high band, >2.9 GHz). peak_preselector self-guards: it only tunes the YIG
            # when a real tone is present, else returns None and leaves it untouched (so a not-yet-settled
            # source can't blank the display). Because RX and TX run on separate threads, the peak request
            # can arrive BEFORE the 68367C tone settles -> the first attempt no-ops. So on request we ARM a
            # bounded retry and re-attempt on subsequent sweeps until it catches the tone; once peaked (or
            # the budget is spent) we stop. Either way restore the measurement window (peak_preselector
            # leaves SP=200 MHz + an MKCF-recenter; the DAC it sets is persistent hardware state) so the
            # full-trace read below is the intended span -- else the pinned-x-axis PSD reads BLANK.
            cf = self._settings.center_hz
            did_presel = False
            if want_preselector:
                dac = self.hub.analyzer.peak_preselector(cf)
                self._presel_pending = self._PRESEL_RETRY if (cf > 2.9e9 and dac is None) else 0
                if cf > 2.9e9:
                    self.hub.analyzer.set_frequency(center_hz=cf, span_hz=self._settings.span_hz)
                    # RESTORE the parked RBW: peak_preselector zooms to 300 kHz; without this the feed keeps
                    # sweeping at 300 kHz while the readout says 'auto' (coarse trace + inflated floor).
                    self.hub.analyzer.set_resolution_bandwidth(self._settings.rbw_hz, self._settings.rbw_auto)
                    did_presel = True
            elif self._presel_pending and cf > 2.9e9:
                dac = self.hub.analyzer.peak_preselector(cf)
                self._presel_pending = 0 if dac is not None else self._presel_pending - 1
                self.hub.analyzer.set_frequency(center_hz=cf, span_hz=self._settings.span_hz)
                self.hub.analyzer.set_resolution_bandwidth(self._settings.rbw_hz, self._settings.rbw_auto)
                did_presel = True
            if want_marker:
                f, a = self.hub.analyzer.marker_peak()
                self.q.put(("marker", f, a))
            if want_state:
                self._emit_state()
            if self._settings.continuous or drained_apply:
                an = self.hub.analyzer
                eff = self._effective_sweep_s(self._settings)
                # fresh=True (flush the stale one-behind) ONLY when we changed analyzer state this tick
                # (a retune/CLRW via apply, or a preselector zoom+restore). PARKED steady-state reads pass
                # fresh=False -> one sweep instead of two, ~halving the per-read time for the live feed.
                fresh = drained_apply or did_presel
                an.arm_and_wait(timeout_s=max(5.0, eff * 2.0), fresh=fresh)
                # calibrate=fresh: on a settings-change tick, (re)derive the binary measurement-units ->
                # dBm map from a paired ASCII+binary read; parked ticks then read the smaller binary
                # transfer (the live-feed bottleneck). Falls back to ASCII automatically if uncalibrated.
                freqs, levels = an.read_trace("A", calibrate=fresh)
                # Only publish a NON-EMPTY trace: a momentary empty/partial read (a bridge hiccup, a
                # cleared-but-not-yet-refilled sweep) must NOT erase a good PSD -- keep the last trace and
                # try again next tick. A genuine bus failure RAISES above -> 'absent' below, which is the
                # only thing that should change the display away from live data.
                if levels:
                    self.q.put(("trace", freqs, levels))
        except Exception as e:                            # noqa: BLE001 -- surface, keep the loop
            self.q.put(("absent", str(e)))

    def run(self, stop_event):
        import time
        while not stop_event.is_set():
            self.step_once()
            if self.suspended or not self._settings.continuous:
                time.sleep(0.02)


class SpectrumAnalyzerPanel:
    """PySide6 + pyqtgraph SA panel over a SpectrumModel + SpectrumEngine. A QWidget usable
    standalone (wrapped in a window) or embedded in the bench. Control callbacks mutate
    model.settings and enqueue an apply; a QTimer drains the engine queue + repaints."""

    def __init__(self, hub, title="8565EC spectrum analyzer"):
        import pyqtgraph as pg
        from PySide6 import QtWidgets, QtGui
        import qt_common
        self._pg, self._QtWidgets = pg, QtWidgets
        qt_common.ensure_app()

        self.hub = hub
        self.model = SpectrumModel()
        self._q = _queue.Queue()
        self.engine = SpectrumEngine(hub, self._q)
        self._stop = threading.Event()
        self._thread = None
        self._timer = None

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
        self.plot = qt_common.new_plot(title, "frequency (GHz)", "level (dBm)")
        right.addWidget(self.plot, 1)
        self._live = self.plot.plot([], [], pen=pg.mkPen("#1f77b4", width=1.0))
        self._maxhold = self.plot.plot([], [], pen=pg.mkPen("#d62728", width=1.0, style=QtGui.Qt.DashLine))
        self._marker = pg.ScatterPlotItem(size=10, brush=pg.mkBrush("#7b3294"))
        self.plot.addItem(self._marker)
        self._apply_from_controls()

    def _build_controls(self, qt_common):
        cp = qt_common.ControlPanel("SPECTRUM ANALYZER")
        self.spin_center = cp.add_freq_spin("center (GHz)", 0.01, 50.0, self.model.settings.center_hz / 1e9,
                                            decimals=6, suffix="GHz", on_change=self._on_freq)
        self.spin_span = cp.add_spin("span (MHz)", 0.0, 50000.0, self.model.settings.span_hz / 1e6,
                                     step=10.0, decimals=3, suffix="MHz", on_change=self._on_freq)
        self.spin_rbw = cp.add_spin("RBW (Hz)", 1.0, 1e7, 1e5, step=100.0, decimals=0, suffix="Hz",
                                    on_change=self._on_settings)
        self.chk_rbw_auto = cp.add_checkbox("RBW auto", True, self._on_settings)
        self.spin_ref = cp.add_spin("ref level (dBm)", -120.0, 30.0, 0.0, step=1.0, decimals=1,
                                    suffix="dBm", on_change=self._on_settings)
        # peak/sample/neg-peak/normal -> POS/SMP/NEG/NRM in the driver (normalize_detector).
        # No "rms": the 8565EC has no RMS detector (it would silently no-op).
        self.combo_detector = cp.add_combo("detector", ("peak", "sample", "neg-peak", "normal"),
                                           self._on_settings, index=0)
        self.chk_continuous = cp.add_checkbox("continuous sweep", True, self._on_settings)
        self.chk_maxhold = cp.add_checkbox("max hold", False, self._on_settings)
        self.chk_avg = cp.add_checkbox("video average", False, self._on_settings)
        self.btn_peak = cp.add_buttons(("Peak search", self._on_peak))[0]
        self.btn_presel = cp.add_buttons(("Peak preselector", self._on_presel))[0]
        cp.add_help("Full-fidelity 8565EC control. Preselector peak applies above 2.9 GHz.")
        cp.add_stretch()
        return cp

    def _apply_from_controls(self):
        s = self.model.settings
        s.center_hz = self.spin_center.value() * 1e9
        s.span_hz = self.spin_span.value() * 1e6
        s.rbw_auto = self.chk_rbw_auto.isChecked()
        s.rbw_hz = self.spin_rbw.value()
        s.ref_dbm = self.spin_ref.value()
        s.detector = self.combo_detector.currentText()
        s.continuous = self.chk_continuous.isChecked()
        s.max_hold = self.chk_maxhold.isChecked()
        s.video_avg = self.chk_avg.isChecked()
        self.btn_presel.setEnabled(self.model.preselector_applicable())

    def _on_freq(self, *_):
        self._apply_from_controls()
        self.model.reset_traces()
        self.engine.enqueue(("apply_settings", self.model.settings))

    def _on_settings(self, *_):
        self._apply_from_controls()
        self.engine.enqueue(("apply_settings", self.model.settings))

    def _on_peak(self, *_):
        self.engine.enqueue(("marker_peak", None))

    def _on_presel(self, *_):
        self.engine.enqueue(("preselector_peak", None))

    def _drain(self):
        try:
            while True:
                evt = self._q.get_nowait()
                if evt[0] == "trace":
                    self.model.set_trace(evt[1], evt[2])
                elif evt[0] == "marker":
                    self.model.set_marker(evt[1], evt[2])
                elif evt[0] == "absent":
                    self.model.set_absent(True, evt[1] if len(evt) > 1 else "")
        except _queue.Empty:
            pass

    def render(self):
        fg, live, mh = self.model.curve()
        self._live.setData(fg, live)
        self._maxhold.setData(fg if mh else [], mh)
        if self.model.marker is not None:
            self._marker.setData([self.model.marker[0] / 1e9], [self.model.marker[1]])
        else:
            self._marker.setData([], [])
        self.readout.setText(self.model.readout_text())
        return (self._live, self._maxhold, self._marker)

    def _tick(self):
        self._drain()
        return self.render()

    def start(self, interval_ms=200):
        from PySide6 import QtCore
        self.engine.enqueue(("apply_settings", self.model.settings))
        self._stop.clear()
        self._thread = threading.Thread(target=self.engine.run, args=(self._stop,), daemon=True)
        self._thread.start()
        self._timer = QtCore.QTimer(self.widget)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def stop(self):
        self._stop.set()
        if self._timer is not None:
            self._timer.stop()


def build_sa_panel(hub, title="8565EC spectrum analyzer"):
    return SpectrumAnalyzerPanel(hub, title=title)
