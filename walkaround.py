"""Near-field-probe WALKAROUND GUI: the operator physically walks the enclosure holding a
near-field probe (on the 8565EC input) while the 68367C transmits a fixed CW tone; the GUI shows
the probe's received level LIVE, HOLDS the peak (so a hot spot found while moving is captured),
colours the readout by how hot the spot is, and lets the operator MARK suspect positions. Both
units are networked (net:HOST:PORT:PAD; the golden two-VM). This is leak LOCALIZATION (find WHERE
the enclosure leaks after a campaign shows a low-SE frequency) -- distinct from the SE(f) campaign
in se_gui.py; the data schemas and verbs are kept separate to avoid confusion.

Repo model/view split (hardware-free testable):
  NearFieldModel : pure data. Accumulates the live level series, the running MAX-HOLD (peak +
                   frame), a quiet baseline, and the operator's marks. Classifies the current
                   level's rise above the baseline into quiet/cool/warm/hot. NO matplotlib.
  NearFieldGUI   : PySide6 + pyqtgraph view + operator controls (Start/Stop/Mark/Reset-peak/Clear).
                   Qt is imported LAZILY (headless paths set QT_QPA_PLATFORM=offscreen). The
                   walkaround runs in a background thread (coordinator: source on once, analyzer
                   reads in a loop) feeding a thread-safe queue drained on the main thread. RF is
                   turned off in loop.nearfield_walkaround's finally, so Stop (or any error) never
                   leaves the source radiating.
"""
from __future__ import annotations

import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg_mod

# heat class -> colour (rise of the current level above the quiet baseline, in dB)
HEAT_COLOR = {"idle": "#999999", "quiet": "#2ca02c", "cool": "#9acd32",
              "warm": "#ff7f0e", "hot": "#d62728"}


# ============================================================== model (no matplotlib)

class NearFieldModel:
    """Pure accumulator for a live near-field walkaround at one frequency."""

    HISTORY_CAP = 600

    def __init__(self, freq_hz):
        self.freq_hz = freq_hz
        self.current_level_dbm = None
        self.max_hold_dbm = None            # running peak level (the worst leak found)
        self.max_hold_frame = None
        self.baseline_dbm = None            # running quiet floor (min) -- the no-leak reference
        self.frame_count = 0
        self.levels = []                    # [(frame, level_dbm), ...] capped
        self.marks = []                     # [{frame, level_dbm, label}]
        self.phase = "idle"                 # idle|walking|done|error
        self.error = None

    # -- feed (main thread, from the GUI queue drain) ---------------------------
    def set_phase(self, phase):
        self.phase = phase

    def set_freq(self, freq_hz):
        self.freq_hz = freq_hz

    def add_frame(self, i, level_dbm):
        self.current_level_dbm = level_dbm
        self.frame_count += 1
        self.levels.append((i, level_dbm))
        if len(self.levels) > self.HISTORY_CAP:
            self.levels = self.levels[-self.HISTORY_CAP:]
        if self.max_hold_dbm is None or level_dbm > self.max_hold_dbm:
            self.max_hold_dbm, self.max_hold_frame = level_dbm, i
        if self.baseline_dbm is None or level_dbm < self.baseline_dbm:
            self.baseline_dbm = level_dbm

    def add_mark(self, label=""):
        """Log the CURRENT reading as a suspect hot spot; returns the mark (or None if no read)."""
        if self.current_level_dbm is None:
            return None
        m = {"frame": self.frame_count, "level_dbm": self.current_level_dbm,
             "label": label or f"spot{len(self.marks) + 1}"}
        self.marks.append(m)
        return m

    def reset_peak(self):
        """Restart the max-hold at the current level (search the next section fresh)."""
        self.max_hold_dbm = self.current_level_dbm
        self.max_hold_frame = self.frame_count

    def clear_marks(self):
        self.marks = []

    def reset(self):
        self.__init__(self.freq_hz)

    def set_error(self, exc):
        self.error = str(exc)
        self.phase = "error"

    # -- pure display helpers (the view + tests consume these) ------------------
    def rise_db(self):
        """dB the current level sits above the quiet baseline (None until the first read)."""
        if self.current_level_dbm is None or self.baseline_dbm is None:
            return None
        return self.current_level_dbm - self.baseline_dbm

    def heat(self):
        """Classify the current rise above baseline: quiet < 4 <= cool < 10 <= warm < 20 <= hot."""
        r = self.rise_db()
        if r is None:
            return "idle"
        if r >= 20:
            return "hot"
        if r >= 10:
            return "warm"
        if r >= 4:
            return "cool"
        return "quiet"

    def history_curve(self):
        return ([f for f, _ in self.levels], [lvl for _, lvl in self.levels])

    def headline_text(self):
        if self.phase == "error":
            return f"ERROR: {self.error}"
        if self.current_level_dbm is None:
            return f"{self.freq_hz / 1e9:.3f} GHz  --  press Start, then walk the probe"
        r = self.rise_db()
        mh = f"   peak {self.max_hold_dbm:.1f} dBm" if self.max_hold_dbm is not None else ""
        return (f"{self.current_level_dbm:6.1f} dBm  [{self.heat().upper()}]  "
                f"+{r:.0f} dB over floor{mh}   @ {self.freq_hz / 1e9:.3f} GHz")

    def marks_text(self):
        if not self.marks:
            return "marked hot spots:\n  (none -- press Mark)"
        rows = "\n".join(f"  {m['label']:<8} {m['level_dbm']:6.1f} dBm  (frame {m['frame']})"
                         for m in self.marks)
        return "marked hot spots:\n" + rows

    def marks_csv(self):
        out = ["label,level_dbm,frame,freq_hz"]
        for m in self.marks:
            out.append(f"{m['label']},{m['level_dbm']:.2f},{m['frame']},{self.freq_hz:.0f}")
        return "\n".join(out) + "\n"


# ============================================================== gui (PySide6 + pyqtgraph)

class NearFieldGUI:
    """PySide6 + pyqtgraph view + operator controls over a NearFieldModel. The walkaround runs in a
    BACKGROUND thread; frames flow through a thread-safe queue drained by a main-thread QTimer. The
    class holds a QMainWindow (self.window); Qt is imported LAZILY so the pure model stays Qt-free."""

    def __init__(self, model, walk_factory,
                 title="se299 near-field walkaround (leak localization)"):
        self.model = model
        self.walk_factory = walk_factory     # (gain, rbw) -> (coordinator, bench)
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._gain = 33
        self._rbw = 1000.0
        self._power_dbm = None               # tone power (dBm); None = band default
        self._timer = None

        import pyqtgraph as pg                # lazy: importing walkaround needs no Qt
        from PySide6 import QtWidgets, QtGui
        import qt_common
        self._pg, self._QtWidgets, self._QtGui = pg, QtWidgets, QtGui
        qt_common.ensure_app()

        self.window = QtWidgets.QMainWindow()
        self.window.setWindowTitle(title)
        central = QtWidgets.QWidget()
        self.window.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        self.controls = self._build_controls(qt_common)
        root.addWidget(self.controls, 0)

        right = QtWidgets.QVBoxLayout()
        root.addLayout(right, 1)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        # big colour-coded live readout (the heat meter)
        self.headline = QtWidgets.QLabel("")
        big = QtGui.QFont(mono)
        big.setPointSize(max(16, big.pointSize() + 6))
        big.setBold(True)
        self.headline.setFont(big)
        right.addWidget(self.headline)

        self.plot = qt_common.new_plot(
            "probe level vs time (walk the probe over seams / gaskets / penetrations)",
            "frame", "received level (dBm)")
        right.addWidget(self.plot, 1)
        self._line = self.plot.plot([], [], pen=pg.mkPen("#1f77b4", width=1.5))
        self._maxline = pg.InfiniteLine(pos=-200, angle=0, movable=False,
                                        pen=pg.mkPen("#d62728", style=QtGui.Qt.DashLine, width=1.0),
                                        label="max-hold", labelOpts={"color": "#d62728"})
        self.plot.addItem(self._maxline)
        self._markpts = pg.ScatterPlotItem(size=14, symbol="t", brush=pg.mkBrush("#7b3294"))
        self.plot.addItem(self._markpts)

        self.status_txt = QtWidgets.QLabel("")
        self.status_txt.setFont(mono)
        self.status_txt.setStyleSheet("color:#333333;")
        right.addWidget(self.status_txt)
        # marks table (its own column on the right)
        self.marks_txt = QtWidgets.QLabel("")
        self.marks_txt.setFont(mono)
        self.marks_txt.setAlignment(QtGui.Qt.AlignTop)
        self.marks_txt.setMinimumWidth(200)
        root.addWidget(self.marks_txt, 0)

    # -- operator controls (reusable ControlPanel; no hand-placed geometry) ------
    def _build_controls(self, qt_common):
        import drivers
        max_pow = drivers.Anritsu68369.HARD_MAX_OUTPUT_DBM   # F4: cap the spinner at the source clamp
        cp = qt_common.ControlPanel("OPERATOR")
        rb = cp.add_buttons(("Start", self._on_start), ("Stop", self._on_stop))
        self.btn_start, self.btn_stop = rb[0], rb[1]
        rb2 = cp.add_buttons(("Mark spot", self._on_mark), ("Reset peak", self._on_reset_peak))
        self.btn_mark, self.btn_rpk = rb2[0], rb2[1]
        self.btn_clr = cp.add_buttons(("Clear marks", self._on_clear))[0]
        self.spin_freq = cp.add_spin("leak freq (GHz)", 0.01, 60.0, self.model.freq_hz / 1e9,
                                     step=0.1, decimals=3, suffix="GHz", on_change=self._on_freq)
        self.spin_pow = cp.add_optional_spin("tone power (dBm) -- auto = band default", -60.0, max_pow,
                                             None, step=1.0, decimals=1, suffix="dBm",
                                             on_change=self._on_power)
        self.combo_gain = cp.add_combo("top-band gain (dBi)", ("33", "25"), self._on_gain, index=0)
        self.spin_rbw = cp.add_spin("analyzer RBW (Hz)", 1.0, 1e7, 1000.0, step=100.0, decimals=0,
                                    suffix="Hz", on_change=self._on_rbw)
        cp.add_help("green=quiet, amber=warm, red=HOT (leak). Mark logs the current reading; "
                    "Reset peak restarts the max-hold for the next section.")
        cp.add_stretch()
        return cp

    def _on_gain(self, label):
        self._gain = int(label)

    def _on_rbw(self, val):
        self._rbw = float(val)

    def _on_freq(self, val):
        self.model.set_freq(float(val) * 1e9)

    def _on_power(self, *_):
        """Set the transmitted tone power (dBm). 'auto' -> the band default (None)."""
        self._power_dbm = self.spin_pow.optional_value()

    def _on_start(self, _e=None):
        if self._thread is not None and self._thread.is_alive():
            return
        self.model.reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_walk, daemon=True)
        self._thread.start()

    def _on_stop(self, _e=None):
        self._stop.set()

    def _on_mark(self, _e=None):
        self.model.add_mark()

    def _on_reset_peak(self, _e=None):
        self.model.reset_peak()

    def _on_clear(self, _e=None):
        self.model.clear_marks()

    # -- background walkaround -> queue ------------------------------------------
    def _run_walk(self):
        q, stop = self._q, self._stop
        try:
            coord, bench = self.walk_factory(self._gain, self._rbw)
            if not coord.ensure_ready():
                raise RuntimeError("instruments not READY (no fake) -- point at live units or sim")
            q.put(("phase", "walking"))
            coord.walkaround(self.model.freq_hz,
                             on_frame=lambda i, lvl: q.put(("frame", i, lvl)),
                             should_stop=lambda: stop.is_set(), bench=bench,
                             power_dbm=self._power_dbm)
            q.put(("phase", "done"))
        except Exception as e:                              # noqa: BLE001 -- surfaced to the GUI
            q.put(("error", e))

    # -- main-thread drain + redraw ----------------------------------------------
    def _drain(self):
        try:
            while True:
                evt = self._q.get_nowait()
                if evt[0] == "phase":
                    self.model.set_phase(evt[1])
                elif evt[0] == "frame":
                    if self.model.phase != "walking":
                        self.model.set_phase("walking")
                    self.model.add_frame(evt[1], evt[2])
                elif evt[0] == "error":
                    self.model.set_error(evt[1])
        except queue.Empty:
            pass

    def render(self):
        """Paint the trace + max-hold line + marks into pyqtgraph and set the heat readout/label
        colour. Returns (line, maxline, markpts) so tests can assert on plotted data + line pos."""
        m = self.model
        frames, levels = m.history_curve()
        if frames:
            self._line.setData(frames, levels)
            self.plot.setXRange(max(0, frames[-1] - m.HISTORY_CAP), max(10, frames[-1] + 1))
            lo, hi = min(levels), max(levels)
            pad = max(3.0, (hi - lo) * 0.15)
            self.plot.setYRange(lo - pad, hi + pad)
        if m.max_hold_dbm is not None:
            self._maxline.setPos(m.max_hold_dbm)
        if m.marks:
            self._markpts.setData([mk["frame"] for mk in m.marks],
                                  [mk["level_dbm"] for mk in m.marks])
        else:
            self._markpts.setData([], [])
        self.headline.setText(m.headline_text())
        self.headline.setStyleSheet(f"color:{HEAT_COLOR.get(m.heat(), '#000000')};")
        self.marks_txt.setText(m.marks_text())
        floor = m.baseline_dbm if m.baseline_dbm is None else round(m.baseline_dbm, 1)
        self.status_txt.setText(f"phase={m.phase}  frames={m.frame_count}  floor={floor} dBm")
        return (self._line, self._maxline, self._markpts)

    def _tick(self):
        self._drain()
        return self.render()

    def run(self, interval_ms=200, frames=None):
        """Interactive live GUI: a QTimer drains the frame queue + repaints, then the Qt event loop
        runs. Blocks until the window closes. `frames` is accepted for compatibility and ignored."""
        import qt_common
        self._timer = qt_common.run_live(self.window, self._tick, interval_ms)
        return self._timer


# ============================================================== shared construction

def build_cfg(gain_dbi=33, rbw_hz=1000.0, use_opc=False):
    """Campaign config for the walkaround (top-band gain + RBW), matching se_gui.build_cfg."""
    bands = list(cfg_mod.DEFAULT_BANDS)
    if int(gain_dbi) == 25:
        bands[-1] = cfg_mod.WR28_STANDARD_25DBI
    analyzer = cfg_mod.AnalyzerSettings(rbw_hz=float(rbw_hz), vbw_hz=float(rbw_hz))
    source = cfg_mod.SourceSettings(use_opc=bool(use_opc))
    return cfg_mod.Campaign(bands=tuple(bands), analyzer=analyzer, source=source, label="walkaround")


def build_walkaround(analyzer_addr="sim", source_addr="sim", freq_hz=5.0e9,
                     gain_dbi=33, rbw_hz=1000.0, client_id=None):
    """Build (NearFieldModel, NearFieldGUI) wired to run a live walkaround over the given
    addresses ('sim'/'sim' -> simulator; else net:/VISA via from_addresses). Both units networked
    is the golden two-VM case: net:HOST:PORT:PAD for each. `client_id` (if given) is announced to
    each bridge so this walkaround client shows in the device session registry."""
    import control_plane

    def factory(gain, rbw):
        cfg = build_cfg(gain, rbw)
        if analyzer_addr == "sim" and source_addr == "sim":
            cp = control_plane.simulated(cfg)
        else:
            cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                              client_id=client_id)
        return cp.make_coordinator(), getattr(cp, "bench", None)

    model = NearFieldModel(freq_hz)
    gui = NearFieldGUI(model, factory)
    return model, gui
