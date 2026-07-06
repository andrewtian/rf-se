"""Shared PySide6 + PyQtGraph scaffolding for the se299 operator GUIs (se_gui, walkaround, live).

This module imports Qt at top, so the GUI modules import it LAZILY (inside the view __init__/run),
never at their module top -- that keeps their pure models (SEFigureModel / NearFieldModel /
LiveSpectrumModel) importable with no GUI dependency (migration contract A), so the model unit tests
run in the shared CAD venv with no Qt installed.

Provides:
  ensure_app()        -- the process-singleton QApplication (offscreen in tests via QT_QPA_PLATFORM).
  run_live(win, tick) -- show the window + drive `tick` from a main-thread QTimer + exec the app loop
                         (the QTimer is the Qt analog of matplotlib's FuncAnimation; only tick touches
                         widgets, so the worker-thread -> queue -> main-thread-drain contract is kept).
  OptionalDoubleSpin  -- a QDoubleSpinBox with an 'auto' (=None) state at its minimum sentinel, for the
                         sweep-band / tone-power fields that mean "band default" when left on auto.
  ControlPanel        -- a reusable operator control column (buttons / combo / spinners / help), which
                         replaces the copy-pasted matplotlib add_axes([x,y,w,h]) geometry in each GUI.
  new_plot()          -- a white-background pyqtgraph PlotWidget wired with axis labels + grid.
"""
from __future__ import annotations

import sys

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

# scientific look: white background, black axes, antialiased curves (matches the old matplotlib feel).
pg.setConfigOptions(antialias=True, background="w", foreground="k")


def ensure_app():
    """Return the singleton QApplication, creating it if needed. Idempotent and safe to call before
    constructing any widget. Tests set QT_QPA_PLATFORM=offscreen in the environment before import so
    this builds a headless app with no display."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv if len(sys.argv) > 0 else ["se299"])
    return app


def install_exit_cleanup(cleanup):
    """Run `cleanup` (release bridge leases + RF off) on SIGINT/SIGTERM and at process exit, so a
    Ctrl-C or kill of the GUI FREES its lease instead of stranding it for the full lease TTL -- a
    stranded lease blocks the NEXT launch's feed (the operator closes/kills the window and relaunches
    within the TTL, and the analyzer is still 'locked by session N'). The window-close path already
    runs cleanup via closeEvent; this covers the terminal-kill paths that never fire it. SIGKILL
    cannot be caught -- there the lease TTL is the only backstop. Runs cleanup at most once.

    Safe off the main thread / where signals are unsupported: signal.signal simply no-ops there."""
    import atexit
    import os
    import signal

    ran = {"done": False}

    def _run():
        if ran["done"]:
            return
        ran["done"] = True
        try:
            cleanup()
        except Exception:                                # noqa: BLE001 -- exit path, best effort
            pass

    atexit.register(_run)

    def _on_signal(signum, _frame):
        _run()                                           # release leases first
        os._exit(128 + signum)                           # then exit promptly (avoid Qt reentrancy)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):                # not the main thread / unsupported platform
                pass
    return _run                                          # returned for testability (idempotent runner)


def run_live(window, tick, interval_ms=250):
    """Interactive live loop: show `window`, drive `tick` (a zero-arg callable = drain queue + repaint)
    from a main-thread QTimer, and run the Qt event loop. Blocks until the window closes. Lives ONLY
    at the operator entry point -- tests never call this; they call the view's _tick()/render() directly
    (no event loop needed, since setData + direct-connected slots are synchronous)."""
    app = ensure_app()
    window.show()
    timer = QtCore.QTimer(window)
    timer.setInterval(int(interval_ms))
    timer.timeout.connect(tick)
    timer.start()
    app.exec()
    return timer


def new_plot(title="", xlabel="", ylabel=""):
    """A white-background PlotWidget with grid + labels, ready for realtime setData()."""
    pw = pg.PlotWidget(title=title or None)
    pw.showGrid(x=True, y=True, alpha=0.3)
    if xlabel:
        pw.setLabel("bottom", xlabel)
    if ylabel:
        pw.setLabel("left", ylabel)
    return pw


class OptionalDoubleSpin(QtWidgets.QDoubleSpinBox):
    """A QDoubleSpinBox with an 'auto' (None) state. Its minimum is a sentinel one step below the real
    low bound; at the sentinel the box shows 'auto' (setSpecialValueText) and optional_value() is None.
    Used for the sweep-band lo/hi and tone-power fields whose blank/auto means 'use the band default'."""

    def __init__(self, lo, hi, value=None, step=1.0, decimals=3, suffix="", parent=None):
        super().__init__(parent)
        self._sentinel = float(lo) - float(step)
        self.setDecimals(int(decimals))
        self.setRange(self._sentinel, float(hi))
        self.setSingleStep(float(step))
        self.setSpecialValueText("auto")                 # shown when value == minimum() == sentinel
        if suffix:
            self.setSuffix(" " + suffix)
        self.set_optional(value)

    def optional_value(self):
        v = self.value()
        return None if v <= self._sentinel + 1e-9 else v

    def set_optional(self, v):
        self.setValue(self._sentinel if v is None else float(v))


class FreqStepSpinBox(QtWidgets.QDoubleSpinBox):
    """A center-frequency spin box (value in GHz) whose arrow-key / wheel / button step is
    FREQUENCY-APPROPRIATE: one press moves a sensible fraction of the current frequency via the
    freqstep ladder (step scales with the decade) instead of a fixed step, so a single Up/Down is
    ~1-10% everywhere from 10 MHz to 40 GHz. Hold SHIFT for a fine (10x smaller) step. Because it
    overrides stepBy, this works uniformly for the Up/Down arrow keys, the mouse wheel, and the
    spin buttons; PageUp/Page-Down pass a larger `steps` and take that many ladder steps."""

    def stepBy(self, steps: int) -> None:
        import freqstep
        fine = bool(QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.ShiftModifier)
        lo_hz, hi_hz = self.minimum() * 1e9, self.maximum() * 1e9
        f_hz = self.value() * 1e9
        up = steps > 0
        for _ in range(abs(int(steps)) or 1):
            f_hz = freqstep.step_freq(f_hz, up=up, fine=fine, lo_hz=lo_hz, hi_hz=hi_hz)
        self.setValue(f_hz / 1e9)


class ControlPanel(QtWidgets.QWidget):
    """A reusable operator control column: a bold header then stacked rows. Every add_* returns the
    created widget(s) so the GUI wires signals to its own handlers. This is the single home for the
    operator-control layout that each GUI used to hand-place with matplotlib add_axes rectangles."""

    def __init__(self, title="OPERATOR", parent=None):
        super().__init__(parent)
        self._box = QtWidgets.QVBoxLayout(self)
        self._box.setContentsMargins(8, 8, 8, 8)
        self._box.setSpacing(6)
        hdr = QtWidgets.QLabel(title)
        f = hdr.font()
        f.setBold(True)
        hdr.setFont(f)
        self._box.addWidget(hdr)

    def add_buttons(self, *specs):
        """specs: (text, slot), ... laid out in a row. Returns the list of QPushButton."""
        row = QtWidgets.QHBoxLayout()
        btns = []
        for text, slot in specs:
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
            btns.append(b)
        self._box.addLayout(row)
        return btns

    def add_label(self, text):
        lbl = QtWidgets.QLabel(text)
        self._box.addWidget(lbl)
        return lbl

    def add_combo(self, label, options, on_change=None, index=0):
        if label:
            self.add_label(label)
        c = QtWidgets.QComboBox()
        c.addItems([str(o) for o in options])
        c.setCurrentIndex(int(index))
        if on_change is not None:
            c.currentTextChanged.connect(on_change)
        self._box.addWidget(c)
        return c

    def add_spin(self, label, lo, hi, value, step=1.0, decimals=1, suffix="", on_change=None):
        if label:
            self.add_label(label)
        sb = QtWidgets.QDoubleSpinBox()
        sb.setDecimals(int(decimals))
        sb.setRange(float(lo), float(hi))
        sb.setSingleStep(float(step))
        if suffix:
            sb.setSuffix(" " + suffix)
        sb.setValue(float(value))
        if on_change is not None:
            sb.valueChanged.connect(on_change)
        self._box.addWidget(sb)
        return sb

    def add_freq_spin(self, label, lo, hi, value, decimals=6, suffix="GHz", on_change=None):
        """A center-frequency spinner (GHz) with FREQUENCY-APPROPRIATE arrow-key stepping (see
        FreqStepSpinBox): Up/Down move a sensible fraction of the current frequency, Shift = fine."""
        if label:
            self.add_label(label + "  (arrows step; shift=fine)")
        sb = FreqStepSpinBox()
        sb.setDecimals(int(decimals))
        sb.setRange(float(lo), float(hi))
        sb.setSingleStep(0.001)                          # nominal; stepBy() overrides the real step
        if suffix:
            sb.setSuffix(" " + suffix)
        sb.setValue(float(value))
        if on_change is not None:
            sb.valueChanged.connect(on_change)
        self._box.addWidget(sb)
        return sb

    def add_checkbox(self, label, checked=False, on_change=None):
        c = QtWidgets.QCheckBox(label)
        c.setChecked(bool(checked))
        if on_change is not None:
            c.stateChanged.connect(lambda *_: on_change())
        self._box.addWidget(c)
        return c

    def add_optional_spin(self, label, lo, hi, value=None, step=1.0, decimals=3, suffix="",
                          on_change=None):
        if label:
            self.add_label(label)
        sb = OptionalDoubleSpin(lo, hi, value, step, decimals, suffix)
        if on_change is not None:
            sb.valueChanged.connect(lambda *_: on_change())
        self._box.addWidget(sb)
        return sb

    def add_help(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#333333;")
        f = lbl.font()
        f.setPointSize(max(7, f.pointSize() - 2))
        lbl.setFont(f)
        self._box.addWidget(lbl)
        return lbl

    def add_stretch(self):
        self._box.addStretch(1)
