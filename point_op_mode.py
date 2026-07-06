"""Point-Operation mode for the se299 bench -- one of the pluggable bench MODES.

Purpose: operate the pair at ONE frequency point and read the SE figure in LARGE type beside the
live PSD graph. The TX emits a CW tone at the point; the RX shows a narrow-span PSD centred on it
with a peak marker. Press "Set reference" with NO barrier (antennas facing / cable through) to
capture the baseline received level; the big readout then shows SE = reference - current (dB), the
substitution SE at that point, live as you move the shield or re-tune.

Arrow-key scheme (task): the whole mode widget takes the keys so the operator drives the pair from
the keyboard while watching the trace:
    Up / Down    = TX tone LEVEL   (+/- 1 dB; Shift = 0.1 dB fine)
    Left / Right = shift RX + TX FREQUENCY together (freqstep ladder; Shift = fine)
Both units move in lockstep on Left/Right so the tone stays centred; only the TX level changes on
Up/Down. RF defaults OFF (safety).

Modular contract (BenchMode): exposes `.widget` / `.name` / `.start(ms)` / `.stop()` / `.suspend()`
/ `.resume()`; bench_gui hosts it in the tab strip and suspends the inactive mode (RF off + leases
released) so only the active mode drives the shared 68367C + 8565EC.

Pure/Qt split: PointOpModel carries NO Qt (unit-testable); the panel imports Qt lazily.
"""
from __future__ import annotations

import os
import queue as _queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import freqstep
import measurements
import presets

MODE_NAME = "Point Operation (SE + PSD)"
DEFAULT_SPAN_HZ = 5e6              # narrow PSD window centred on the point
FLOOR_MIN_HZ, FLOOR_MAX_HZ = 10e6, 40e9    # 68367C rated band = the tunable range
POWER_MIN_DBM, POWER_MAX_DBM = -60.0, 17.0
PRESELECTOR_MIN_HZ = 2.9e9        # above this the 8565EC needs preselector peaking
POINT_SETTLE_S = 0.6              # source settle after a point retune before the tone is trustworthy
                                  # (0.05 s reads a suppressed tone; >=0.4 s reads it true -- +0.2 margin)
INPUT_DEBOUNCE_MS = 120          # coalesce a flurry of control edits (typing/scrolling a spin) into one
                                  # apply after the operator pauses -- on top of the engine conflation
TONE_MARGIN_DB = 8.0             # a valid tone must sit this far above the sweep's noise floor
FREQ_TOL_MIN_HZ = 60e3          # on-frequency tolerance floor (covers the +1.3 ppm timebase offset,
                                  # +53 kHz at 40 GHz); widened to span/40 on a wider span


def _set_line_label(line, text):
    """Set an InfiniteLine's label text SYNCHRONOUSLY. pyqtgraph's InfLineLabel refreshes via a
    queued sigPositionChanged (which does not fire during an offscreen grab, and can lag live) AND
    skips the refresh entirely while the line is hidden -- so bake the value into `format` and force
    a refresh. Caller must make the line visible BEFORE this (valueChanged no-ops when hidden)."""
    lbl = getattr(line, "label", None)
    if lbl is None:
        return
    try:
        lbl.format = text                          # no {value} placeholder -> stays as-is on refresh
    except Exception:
        pass
    vc = getattr(lbl, "valueChanged", None)         # the pyqtgraph refresh hook (setText from format)
    if callable(vc):
        try:
            vc(); return
        except Exception:
            pass
    st = getattr(lbl, "setText", None)
    if callable(st):
        try:
            st(text)
        except Exception:
            pass


class PointOpModel:
    """Pure state for point operation: the single operating frequency (shared RX+TX), TX power, RX
    span, RF, the captured no-barrier REFERENCE level, and the live CURRENT received peak. SE is the
    substitution figure reference - current (dB), defined only once a reference is captured AND a
    current tone is being read."""

    def __init__(self):
        self.freq_hz = 2.45e9
        self.power_dbm = -10.0
        self.span_hz = DEFAULT_SPAN_HZ
        self.rf_on = False                 # SAFETY: RF off by default
        self.reference_dbm = None          # captured baseline (no barrier); None until "Set reference"
        self.current_dbm = None            # live received peak
        self.peak_freq_hz = None           # frequency of that peak (to check it is the commanded tone)
        self.floor_dbm = None              # noise-floor estimate this sweep (to check the tone is real)
        self.tx_state = {}                 # last 68367C live state (OF1/OSB/level) for the debug pane
        self.rx_state = {}                 # last 8565EC live state (CF/SP/RBW/DET/errors)
        self.settling = False              # True from a retune until the TX settles: the RX sweeps at the
        #                                    new CF ~0.6 s before the tone arrives, so suppress the alarming
        #                                    OFF-FREQ/NO-TONE flash and show a neutral SETTLING instead

    def span_lo_hz(self) -> float:
        return max(FLOOR_MIN_HZ, self.freq_hz - self.span_hz / 2.0)

    def span_hi_hz(self) -> float:
        return min(FLOOR_MAX_HZ, self.freq_hz + self.span_hz / 2.0)

    def set_current(self, level_dbm):
        self.current_dbm = None if level_dbm is None else float(level_dbm)

    def set_settling(self, settling):
        """Mark the pair mid-retune (a tone-emitting retune was commanded, TX not yet settled). While set,
        reading_status() reports SETTLING instead of a transient OFF-FREQ/NO-TONE from the not-yet-arrived
        tone. Cleared when the TX 'settled' event lands (see PointOpPanel._drain)."""
        self.settling = bool(settling)

    def set_reading(self, peak_dbm, peak_freq_hz, floor_dbm):
        """Richer read than set_current: the peak level PLUS its frequency and the sweep's noise
        floor, so reading_status() can tell a valid settled tone from a spur/floor/off-frequency read."""
        self.current_dbm = None if peak_dbm is None else float(peak_dbm)
        self.peak_freq_hz = None if peak_freq_hz is None else float(peak_freq_hz)
        self.floor_dbm = None if floor_dbm is None else float(floor_dbm)

    def reading_status(self):
        """Classify the current received peak so the operator never trusts a bad number:
        ('TONE OK', True)   -- a real tone, above the floor AND at the commanded frequency
        ('NO TONE', False)  -- nothing above the noise floor (source off / path broken / not settled)
        ('OFF-FREQ ...',F)  -- a peak exists but not at the commanded point (spur, or mid-retune slew)
        ('NO SWEEP', False) -- no trace yet."""
        if self.settling:
            # mid-retune: the RX is sweeping at the new CF but the TX tone has not settled yet. Do NOT
            # cry OFF-FREQ/NO-TONE for the transient -- say SETTLING (not-ok so a reference capture still
            # refuses to baseline off an unsettled tone). Clears on the TX 'settled' event.
            return ("SETTLING", False)
        if self.current_dbm is None:
            return ("NO SWEEP", False)
        floor = self.floor_dbm if self.floor_dbm is not None else (self.current_dbm - 99.0)
        if self.current_dbm <= floor + TONE_MARGIN_DB:
            return ("NO TONE", False)
        if self.peak_freq_hz is not None:
            tol = max(FREQ_TOL_MIN_HZ, self.span_hz / 40.0)
            off = self.peak_freq_hz - self.freq_hz
            if abs(off) > tol:
                return (f"OFF-FREQ ({off / 1e3:+.0f} kHz)", False)
        return ("TONE OK", True)

    def set_reference(self):
        """Capture the current received peak as the no-barrier baseline (returns it, or None)."""
        self.reference_dbm = self.current_dbm
        return self.reference_dbm

    def clear_reference(self):
        self.reference_dbm = None

    def se_db(self):
        """Substitution SE at the point = reference - current (dB), or None if either is missing."""
        if self.reference_dbm is None or self.current_dbm is None:
            return None
        return self.reference_dbm - self.current_dbm

    def shift_freq(self, up: bool, fine: bool = False):
        """Move the operating point one arrow step (freqstep ladder), snapped + clamped to the band.
        Both RX and TX follow this single frequency, so the tone stays centred."""
        self.freq_hz = freqstep.step_freq(self.freq_hz, up=up, fine=fine,
                                          lo_hz=FLOOR_MIN_HZ, hi_hz=FLOOR_MAX_HZ)
        return self.freq_hz

    def bump_power(self, up: bool, fine: bool = False):
        """Change the TX tone level by +/- 1 dB (fine = 0.1 dB), clamped to the source range."""
        step = 0.1 if fine else 1.0
        p = self.power_dbm + (step if up else -step)
        self.power_dbm = round(min(max(p, POWER_MIN_DBM), POWER_MAX_DBM), 3)
        return self.power_dbm

    def big_text(self) -> str:
        """The LARGE headline: the SE figure once a reference is set, else the live received level."""
        se = self.se_db()
        if se is not None:
            return f"SE {se:+.1f} dB"
        if self.current_dbm is not None:
            return f"RX {self.current_dbm:+.1f} dBm"
        return "RX --  (press Run)"

    def readout_text(self) -> str:
        ref = f"{self.reference_dbm:+.1f}" if self.reference_dbm is not None else "--"
        cur = f"{self.current_dbm:+.1f}" if self.current_dbm is not None else "--"
        state = "ON" if self.rf_on else "off"
        return (f"{self.freq_hz / 1e9:.6f} GHz   TX {self.power_dbm:+.1f} dBm  RF {state}   "
                f"ref {ref} dBm   RX {cur} dBm   span {self.span_hz / 1e6:.2f} MHz")

    # -- per-unit live-state debug pane (fed from the engines' read_state events) -----------------
    def set_tx_state(self, st):
        self.tx_state = dict(st or {})

    def set_rx_state(self, st):
        self.rx_state = dict(st or {})

    def tx_state_text(self) -> str:
        """One line of 68367C source truth: OF1 frequency readback, OL1 level, OSB leveled/locked."""
        s = self.tx_state
        if not s:
            return "TX 68367C:  (waiting)"
        of1, osb, lvl = s.get("of1_mhz"), s.get("osb"), s.get("level_dbm")
        of1s = f"{of1 / 1e3:.6f} GHz" if of1 is not None else "--"      # OF1 readback is in MHz
        lvls = f"{lvl:+.1f} dBm" if lvl is not None else "--"
        if osb is None:
            osbs, flag = "--", ""
        else:
            lev = "UNLEV" if (osb & 0x04) else "leveled"
            lock = "UNLOCK" if (osb & 0x08) else "locked"
            osbs, flag = f"0x{osb:02X}", f" [{lev}/{lock}]"
        return f"TX 68367C:  OF1 {of1s}   OL1 {lvls}   OSB {osbs}{flag}"

    def rx_state_text(self) -> str:
        """One line of 8565EC analyzer truth: applied CF/SP/RBW/DET + the error queue (a wedge shows
        its reference-unlock codes here; benign parser codes are already dropped upstream)."""
        s = self.rx_state
        if not s:
            return "RX 8565EC:  (waiting)"
        c, sp, rb, det, errs = (s.get("center_hz"), s.get("span_hz"), s.get("rbw_hz"),
                                s.get("detector"), s.get("errors"))
        cs = f"{c / 1e9:.6f} GHz" if c is not None else "--"
        sps = f"{sp / 1e6:.3f} MHz" if sp is not None else "--"
        rbs = ("auto" if rb == 0 else f"{rb:.0f} Hz") if rb is not None else "--"
        errs_s = "none" if not errs else ",".join(str(e) for e in errs)
        wedge = "  <-- WEDGE" if errs else ""
        # model-vs-device reconciliation result (from read_state on the throttled tick): a non-empty
        # drift list means the device's ACTUAL state disagrees with what the model commanded.
        drift = s.get("drift")
        drift_s = ("  <-- DRIFT: " + "; ".join(drift)) if drift else ""
        return (f"RX 8565EC:  CF {cs}   SP {sps}   RB {rbs}   DET {det or '--'}   "
                f"ERR {errs_s}{wedge}{drift_s}")


class PointOpPanel:
    """The Point-Operation bench mode: a large SE/level readout beside a narrow-span PSD, driven by
    the coordinated arrow-key scheme. Reuses the tested SpectrumEngine (RX) + SourceEngine (TX) over
    the shared InstrumentHub, exactly like range_mode."""

    name = MODE_NAME

    def __init__(self, hub):
        import pyqtgraph as pg
        from PySide6 import QtWidgets, QtGui
        import qt_common
        import sa_gui
        import sg_gui
        self._pg, self._QtWidgets, self._QtGui = pg, QtWidgets, QtGui
        qt_common.ensure_app()

        self.hub = hub
        self.model = PointOpModel()
        self._rxq, self._txq = _queue.Queue(), _queue.Queue()
        self.rx_engine = sa_gui.SpectrumEngine(hub, self._rxq)
        self.tx_engine = sg_gui.SourceEngine(hub, self._txq)
        self.rx_model = sa_gui.SpectrumModel()
        self._rx_settings = sa_gui.SpectrumSettings()
        self._stop = threading.Event()
        self._threads, self._timer = [], None
        self._STATE_EVERY = 5              # poll per-unit debug state every 5th tick (~1 Hz at 200 ms)
        self._state_counter = 0
        self._settling_await_sweep = False          # TX settled but the fresh RX sweep at the new CF has
        #                                             not landed yet -> hold SETTLING until it does (a slow
        #                                             ASCII feed can lag the settle by >1 s -> stale-trace flash)
        self._rx_absent = self._tx_absent = None   # link-unavailable reason (single-consumer contention
        #                                            or a wedge) -- surfaced so no-feed is never silent
        self._ref_msg = ""                          # last reference/save outcome, shown by the chip
        self._meas_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "measurements")

        self.widget = QtWidgets.QWidget()
        self.widget.setFocusPolicy(QtGui.Qt.StrongFocus)      # so the arrow keys reach this mode
        root = QtWidgets.QHBoxLayout(self.widget)
        self.controls = self._build_controls(qt_common)
        root.addWidget(self.controls, 0)
        right = QtWidgets.QVBoxLayout()
        root.addLayout(right, 1)

        # LARGE SE / level headline (task: "SE figure in large type alongside the PSD graph")
        self.big = QtWidgets.QLabel("")
        big_font = QtGui.QFont()
        big_font.setPointSize(34)
        big_font.setBold(True)
        self.big.setFont(big_font)
        right.addWidget(self.big)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.readout = QtWidgets.QLabel("")
        self.readout.setFont(mono)
        right.addWidget(self.readout)
        # reading-validity chip: is the big number a real settled tone, or a spur/floor/off-freq read?
        self.status = QtWidgets.QLabel("")
        self.status.setFont(mono)
        right.addWidget(self.status)
        # per-unit LIVE-STATE debug pane (TX + RX truth, polled at ~1 Hz off the engine threads)
        self.debug = QtWidgets.QLabel("")
        self.debug.setFont(mono)
        self.debug.setTextFormat(QtGui.Qt.PlainText)
        self.debug.setStyleSheet("color:#334155; background:#f1f5f9; padding:4px; border:1px solid #cbd5e1;")
        right.addWidget(self.debug)
        self.plot = qt_common.new_plot("received PSD (point)", "frequency (GHz)", "level (dBm)")
        right.addWidget(self.plot, 1)
        self._live = self.plot.plot([], [], pen=pg.mkPen("#1f77b4", width=1.2))
        self._marker = pg.ScatterPlotItem(size=11, brush=pg.mkBrush("#7b3294"))
        self.plot.addItem(self._marker)
        # commanded TX output power drawn as a labeled horizontal reference line: the gap DOWN to the
        # received peak is the path loss (cable + shield) at a glance.
        self._tx_line = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False,
            pen=pg.mkPen("#2ca02c", style=QtGui.Qt.DashLine, width=1.5),
            label="TX {value:+.1f} dBm", labelOpts={"position": 0.06, "color": "#2ca02c"})
        self.plot.addItem(self._tx_line)
        self._peak_label = pg.TextItem(color="#7b3294", anchor=(0.5, 1.3))   # peak value at the marker
        self.plot.addItem(self._peak_label)
        # captured REFERENCE level as a labeled line; SE is drawn as the vertical GAP down to the peak,
        # so the substitution measurement is visible on the graph, not just a number.
        self._ref_line = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False,
            pen=pg.mkPen("#e08e0b", style=QtGui.Qt.DashLine, width=1.3),
            label="ref {value:+.1f} dBm", labelOpts={"position": 0.12, "color": "#e08e0b"})
        self._ref_line.setVisible(False)
        self.plot.addItem(self._ref_line)
        self._se_gap = self.plot.plot([], [], pen=pg.mkPen("#e08e0b", width=1.4, style=QtGui.Qt.DotLine))
        self._se_label = pg.TextItem(color="#e08e0b", anchor=(0.0, 0.5))
        self.plot.addItem(self._se_label)
        self._pin_xaxis()                                   # pin x to the window from the first frame

        self._install_shortcuts(qt_common)

        # debounce continuous control edits: a single-shot timer coalesces a burst of spin/checkbox
        # changes into ONE apply after the operator pauses (arrows stay immediate -- see _shift_freq).
        from PySide6 import QtCore
        self._apply_timer = QtCore.QTimer(self.widget)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(INPUT_DEBOUNCE_MS)
        self._apply_timer.timeout.connect(self._apply)

    # -- controls + coordinated arrow keys -------------------------------------------
    def _build_controls(self, qt_common):
        cp = qt_common.ControlPanel("POINT OP")
        self.spin_freq = cp.add_freq_spin("frequency (GHz)", 0.01, 40.0, self.model.freq_hz / 1e9,
                                          decimals=6, suffix="GHz", on_change=self._on_change)
        self.spin_span = cp.add_spin("span (MHz)", 0.1, 1000.0, self.model.span_hz / 1e6,
                                     step=1.0, decimals=2, suffix="MHz", on_change=self._on_change)
        self.spin_power = cp.add_spin("TX power (dBm)", POWER_MIN_DBM, POWER_MAX_DBM,
                                      self.model.power_dbm, step=1.0, decimals=1, suffix="dBm",
                                      on_change=self._on_change)
        self.chk_rf = cp.add_checkbox("RF ON (tone)", False, self._on_change)
        # JUMP-TO-FREQUENCY presets: each button retunes BOTH units to a common frequency (source CW +
        # analyzer CF + preselector above 2.9 GHz). Two rows -- instrument landmarks + EMI/ISM checkpoints
        # -- data-driven from presets.py so the set tracks the campaign, not a hardcoded GUI list.
        cp.add_label("jump to (both units):")
        for row in (presets.landmark_presets(), presets.ism_presets()):
            btns = cp.add_buttons(*((p.label, (lambda f=p.freq_hz: self._jump_to(f))) for p in row))
            for b, p in zip(btns, row):
                b.setToolTip(f"{p.freq_hz / 1e9:.4f} GHz -- {p.note}")
        # +f / -f RELATIVE nudge of BOTH units by a fixed decade step (10 MHz / 100 MHz / 1 GHz). Complements
        # the frequency-proportional arrow ladder with predictable fixed steps; clamped to the joint range.
        cp.add_label("step (both units):")
        step_btns = cp.add_buttons(*((presets.step_label(d), (lambda dd=d: self._nudge(dd)))
                                     for d in presets.step_deltas()))
        for b, d in zip(step_btns, presets.step_deltas()):
            b.setToolTip(f"shift RX+TX by {'+' if d >= 0 else '-'}{abs(d) / 1e6:.0f} MHz")
        rb = cp.add_buttons(("Set reference (no barrier)", self._set_reference),
                            ("Clear reference", self._clear_reference))
        self.btn_ref, self.btn_clr = rb[0], rb[1]
        self.btn_save = cp.add_buttons(("Save measurement", self._save_measurement))[0]
        cp.add_help("Arrows drive the pair: Up/Down = TX level (Shift=fine); Left/Right = shift RX+TX "
                    "frequency together (Shift=fine). Presets jump both units to a common frequency. Set "
                    "reference with NO barrier -> big readout is SE = ref - RX. Save writes the trace + "
                    "context to measurements/.")
        cp.add_stretch()
        return cp

    def _install_shortcuts(self, qt_common):
        from PySide6 import QtGui           # PySide6/Qt6: QShortcut lives in QtGui, not QtWidgets
        w = self.widget
        ctx = QtGui.Qt.WidgetWithChildrenShortcut
        self._shortcuts = []
        for keyseq, fn in (("Up", lambda: self._bump_power(True, False)),
                           ("Down", lambda: self._bump_power(False, False)),
                           ("Shift+Up", lambda: self._bump_power(True, True)),
                           ("Shift+Down", lambda: self._bump_power(False, True)),
                           ("Right", lambda: self._shift_freq(True, False)),
                           ("Left", lambda: self._shift_freq(False, False)),
                           ("Shift+Right", lambda: self._shift_freq(True, True)),
                           ("Shift+Left", lambda: self._shift_freq(False, True))):
            sc = QtGui.QShortcut(QtGui.QKeySequence(keyseq), w)
            sc.setContext(ctx)
            sc.setAutoRepeat(False)        # one retune per keypress: holding a key must not flood the
            #                                source with retunes it settles far slower than the repeat
            sc.activated.connect(fn)
            self._shortcuts.append(sc)

    def _arm_settling_for_retune(self):
        """A frequency retune just started (button/arrow), BEFORE the debounced _apply. If a tone is on,
        hold SETTLING now so the pre-apply window never flashes OFF-FREQ off the stale (old-CF) trace.
        A new retune supersedes any pending post-settle clear."""
        if self.model.rf_on:
            self.model.set_settling(True)
            self._settling_await_sweep = False

    def _bump_power(self, up, fine=False):
        # Update the MODEL + the on-screen control IMMEDIATELY (responsive tuning), but DEBOUNCE the
        # instrument apply: a fast burst of Up/Down taps must not fire a retune per tap, which ratchets
        # the 68367C step attenuator and re-CLRWs (blanks) the 8565EC on every keystroke. One apply
        # fires after the operator pauses (INPUT_DEBOUNCE_MS), retuning to the FINAL level only.
        self.model.bump_power(up, fine)
        self.spin_power.blockSignals(True)                    # display now; don't double-fire _on_change
        self.spin_power.setValue(self.model.power_dbm)
        self.spin_power.blockSignals(False)
        self._apply_timer.start()                             # coalesce the burst -> one debounced apply

    def _shift_freq(self, up, fine=False):
        # Same contract as _bump_power: model + display update per tap, instrument retune is debounced.
        # Both RX + TX follow this one frequency; coalescing a burst avoids a train of source band-switch
        # relay clicks ("ratchet") and analyzer blanks, and lets the tone settle once at the final point.
        self.model.shift_freq(up, fine)
        self._arm_settling_for_retune()
        self.spin_freq.blockSignals(True)
        self.spin_freq.setValue(self.model.freq_hz / 1e9)
        self.spin_freq.blockSignals(False)
        self._apply_timer.start()                             # coalesce the burst -> one debounced apply

    def _jump_to(self, freq_hz):
        """Preset click: JOINT retune of BOTH units to an ABSOLUTE frequency (source CW + analyzer CF +
        preselector above 2.9 GHz -- all via _apply). Same contract as _shift_freq: update model + display
        now, debounce the instrument apply so rapid preset clicks coalesce to ONE retune to the final
        point (never ratchet the source / re-CLRW the analyzer per click)."""
        f = max(FLOOR_MIN_HZ, min(FLOOR_MAX_HZ, float(freq_hz)))
        self.model.freq_hz = f
        self._arm_settling_for_retune()
        self.spin_freq.blockSignals(True)
        self.spin_freq.setValue(f / 1e9)
        self.spin_freq.blockSignals(False)
        self._apply_timer.start()

    def _nudge(self, delta_hz):
        """+f / -f control: shift BOTH units by a FIXED delta (10 MHz / 100 MHz / 1 GHz), clamped to the
        joint range. Same debounced contract as the presets/arrows -- update model + display now, coalesce
        the instrument retune."""
        f = max(FLOOR_MIN_HZ, min(FLOOR_MAX_HZ, self.model.freq_hz + float(delta_hz)))
        self.model.freq_hz = f
        self._arm_settling_for_retune()
        self.spin_freq.blockSignals(True)
        self.spin_freq.setValue(f / 1e9)
        self.spin_freq.blockSignals(False)
        self._apply_timer.start()

    def _save_measurement(self, *_):
        """Snapshot the on-screen received trace + full context to measurements/<ts>-<freq>.json (schema
        se299-measurement/1). READ-ONLY capture -- never touches the instruments; safe any time."""
        import datetime
        fg, live, _mh = self.rx_model.curve()                 # GHz x, dBm y (exactly what is on screen)
        if not live:
            self._ref_msg = "nothing to save -- no trace yet"
            return
        stxt, ok = self.model.reading_status()
        ctx = {
            "mode": "point_op",
            "center_hz": self.model.freq_hz,
            "span_hz": self.model.span_hz,
            "tx_power_dbm": self.model.power_dbm,
            "rf_on": self.model.rf_on,
            "reading_status": stxt,
            "reading_ok": bool(ok),
            "reference_dbm": self.model.reference_dbm,
            "se_db": self.model.se_db(),
        }
        now = datetime.datetime.now()
        rec = measurements.build_measurement(
            [f * 1e9 for f in fg], live, context=ctx,
            label=f"point {self.model.freq_hz / 1e9:.4f} GHz",
            timestamp=now.isoformat(timespec="seconds"))
        os.makedirs(self._meas_dir, exist_ok=True)
        path = os.path.join(self._meas_dir, measurements.default_filename(self.model.freq_hz, now.strftime("%Y%m%d-%H%M%S")))
        measurements.save_measurement(path, rec)
        self._ref_msg = f"saved {os.path.basename(path)} ({len(live)} pts)"

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
    def _pin_xaxis(self):
        # Pin the PSD x-axis to the commanded sweep window [center - span/2, center + span/2].
        # Without this, pyqtgraph auto-ranges x to include the empty trace + the TextItems that sit at
        # their default (0,0): once the real sweep arrives at ~2.45 GHz the view stretches 0 -> 2.45 GHz
        # and the actual window is squeezed off the right edge ("the axis scrolls right until it
        # disappears on start"). An explicit setXRange also turns OFF x auto-range so stray items at
        # x=0 can never pull it again; Y stays auto so the level scales to the trace.
        c = self.model.freq_hz / 1e9                        # GHz (model, set before the plot exists)
        half = (self.model.span_hz / 1e9) / 2.0             # GHz half-span
        if half > 0:
            self.plot.setXRange(c - half, c + half, padding=0.0)

    def _apply(self, *_):
        m = self.model
        m.freq_hz = self.spin_freq.value() * 1e9
        m.span_hz = self.spin_span.value() * 1e6
        m.power_dbm = self.spin_power.value()
        m.rf_on = self.chk_rf.isChecked()
        # mid-retune SETTLING gate: only when a tone is actually being emitted (RF on) is there a tone to
        # settle -- with RF off the honest status is NO TONE, not SETTLING. Cleared by the TX 'settled' event.
        m.set_settling(bool(m.rf_on))
        self._settling_await_sweep = False                  # this apply restarts the settle cycle
        self._pin_xaxis()                                   # keep the view on the sweep window
        # RX: narrow span centred on the point, peak detector, LIVE (not max-hold -- a point read)
        s = self._rx_settings
        s.center_hz, s.span_hz = m.freq_hz, m.span_hz
        s.max_hold, s.continuous, s.detector = False, True, "peak"
        self.rx_model.settings = s
        self.rx_model.reset_traces()
        self.rx_engine.enqueue(("apply_settings", s))
        if m.freq_hz >= PRESELECTOR_MIN_HZ:
            self.rx_engine.enqueue(("preselector_peak", None))   # peak the YIG on the tone >2.9 GHz
        # TX: CW tone at the point (or RF off). A longer settle so the ALC output level is fully
        # ramped before the analyzer reads the tone (see POINT_SETTLE_S; the 0.05 s engine default
        # reads a suppressed tone after a retune).
        self.tx_engine.enqueue(("apply", m.freq_hz, m.power_dbm, m.rf_on, POINT_SETTLE_S))

    def _on_change(self, *_):
        # debounce: restart the single-shot timer so a burst of edits applies once, after the pause
        self._apply_timer.start()

    def _set_reference(self, *_):
        # gate capture on a valid reading: never let the operator baseline SE off a suppressed tone,
        # a spur, or a mid-retune off-frequency read (the exact failure modes we characterized live).
        stxt, ok = self.model.reading_status()
        if not ok:
            self._ref_msg = f"reference NOT captured -- {stxt}; wait for TONE OK"
            return
        ref = self.model.set_reference()
        self._ref_msg = f"reference captured @ {ref:+.1f} dBm" if ref is not None else "reference cleared"

    def _clear_reference(self, *_):
        self.model.clear_reference()
        self._ref_msg = "reference cleared"

    def _drain(self):
        try:
            while True:
                evt = self._rxq.get_nowait()
                if evt[0] == "trace":
                    self.rx_model.set_trace(evt[1], evt[2])
                    self._rx_absent = None                       # a fresh sweep -> the analyzer feeds
                    if self._settling_await_sweep:               # the fresh post-settle sweep landed ->
                        self.model.set_settling(False)           # real status resumes (kills the stale flash)
                        self._settling_await_sweep = False
                elif evt[0] == "rx_state":
                    self.model.set_rx_state(evt[1])
                elif evt[0] == "absent":
                    self._rx_absent = evt[1] if len(evt) > 1 else "unavailable"
                    self.model.set_rx_state({"errors": None, "absent": self._rx_absent})
        except _queue.Empty:
            pass
        try:
            while True:
                evt = self._txq.get_nowait()
                if evt[0] == "tx_state":
                    self.model.set_tx_state(evt[1]); self._tx_absent = None
                elif evt[0] == "settled":
                    self._tx_absent = None                       # the source answered -> it is present
                    # TX settled, but do NOT clear SETTLING yet: the RX may still be showing the pre-retune
                    # trace (peak at the old CF) until its next sweep lands. Arm the clear on that sweep.
                    if self.model.settling:
                        self._settling_await_sweep = True
                elif evt[0] == "absent":
                    self._tx_absent = evt[1] if len(evt) > 1 else "unavailable"
        except _queue.Empty:
            pass

    def render(self):
        fg, live, _mh = self.rx_model.curve()
        self._live.setData(fg, live)
        self._tx_line.setValue(self.model.power_dbm)      # labeled line at the commanded TX power
        _set_line_label(self._tx_line, f"TX {self.model.power_dbm:+.1f} dBm")
        ref = self.model.reference_dbm
        if ref is not None:                               # reference line (SE baseline) when captured
            self._ref_line.setVisible(True)               # visible BEFORE labelling (valueChanged skips hidden)
            self._ref_line.setValue(ref)
            _set_line_label(self._ref_line, f"ref {ref:+.1f} dBm")
        else:
            self._ref_line.setVisible(False)
        if live:
            k = max(range(len(live)), key=lambda i: live[i])
            # noise floor from the trace EDGES, not the median: Point Op centres a NARROW span on the
            # tone, so the tone fills the middle of the span and a median-of-the-span floor tracks the
            # tone itself (floor ~= peak -> a real tone misreads as "NO TONE"). The outer eighths are
            # the skirts away from the centred tone -> a true floor estimate. (Live-proven bug.)
            n = len(live)
            edge = max(1, n // 8)
            edge_pts = live[:edge] + live[-edge:]
            floor = sorted(edge_pts)[len(edge_pts) // 2]
            self.model.set_reading(live[k], fg[k] * 1e9, floor)   # fg is GHz -> Hz for the freq check
            self._marker.setData([{"pos": (fg[k], live[k])}])
            self._peak_label.setText(f"peak {live[k]:+.1f} dBm")
            self._peak_label.setPos(fg[k], live[k])
            se = self.model.se_db()
            if ref is not None and se is not None:        # draw SE as the gap: reference -> peak
                self._se_gap.setData([fg[k], fg[k]], [ref, live[k]])
                self._se_label.setText(f"SE {se:.0f} dB")
                self._se_label.setPos(fg[k], (ref + live[k]) / 2.0)
            else:
                self._se_gap.setData([], []); self._se_label.setText("")
        else:
            self.model.set_reading(None, None, None)
            self._marker.setData([])
            self._peak_label.setText("")
            self._se_gap.setData([], []); self._se_label.setText("")
        # SURFACE an unavailable analyzer/source instead of a silent blank PSD: the single-consumer
        # lease means another session (a campaign, a tool, a second GUI) -- or a wedged/offline unit --
        # yields NO feed. Say so, with the reason, so "no PSD" is never a mystery to the operator.
        if self._rx_absent:
            self.big.setText("RX ANALYZER UNAVAILABLE")
            self.readout.setText(f"no PSD feed -- {self._rx_absent}  "
                                 f"(another session holds the 8565EC, or it is wedged/offline)")
            self._set_status("no analyzer feed", ok=False)
        else:
            self.big.setText(self.model.big_text())
            self.readout.setText(self.model.readout_text())
            stxt, ok = self.model.reading_status()
            parts = [f"reading: {stxt}"]
            if self.model.reference_dbm is None:                  # two-step substitution workflow cue
                if ok:
                    parts.append("shield OUT -> capture reference")
            else:
                parts.append("shield IN -> SE = ref - peak")
            if self._ref_msg:
                parts.append(self._ref_msg)
            self._set_status("   |   ".join(parts), ok)
        tx_line = (f"TX 68367C:  UNAVAILABLE -- {self._tx_absent}" if self._tx_absent
                   else self.model.tx_state_text())
        self.debug.setText(f"{tx_line}\n{self.model.rx_state_text()}")
        return (self._live, self._marker)

    def _set_status(self, text, ok):
        self.status.setText(text)
        self.status.setStyleSheet("color:#15803d; font-weight:bold;" if ok else
                                  "color:#b91c1c; font-weight:bold;")

    def _tick(self):
        self._drain()
        # poll each unit's live state at ~1 Hz (throttled -- adds ~4 bus reads/sec, not per-sweep),
        # so the debug pane stays current without loading the bus or slowing the trace.
        self._state_counter += 1
        if self._state_counter % self._STATE_EVERY == 0:
            self.rx_engine.enqueue(("read_state", None))
            self.tx_engine.enqueue(("read_state", None))
        return self.render()


def build_point_op_mode(hub):
    return PointOpPanel(hub)
