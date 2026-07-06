"""Live SE figure GUI: watch the concurrent dual-instrument substitution campaign paint a
running SE(f) curve in dB, with operator controls to conduct the test.

Two cleanly separated units (the repo's model/view split, hardware-free testable):

  SEFigureModel : pure data. Accumulates the reference pass (per-point EA8 capability) and the
                  wall pass (per-point SE = reference_received - wall_received, in dB) into an
                  ordered SE(f) curve, tracks the running worst-case, phase, progress, and the
                  final summary. NO matplotlib -- fully unit-testable. This is the thing the
                  "is the live SE figure correct?" proof asserts against.

  SELiveGUI     : the PySide6 + pyqtgraph view + OPERATOR CONTROLS over an SEFigureModel. Renders
                  the SE(f) scatter coloured by per-point verdict (PASS/FAIL/INCONCLUSIVE), hollow
                  markers for floor-limited lower bounds, the acceptance target line, the running
                  worst-case, and progress. Native Qt controls (Run/Stop buttons, gain combo, RBW /
                  sweep-band / tone spinners) let the operator conduct the run. Qt is imported LAZILY
                  so importing this module needs no Qt (headless/test paths set QT_QPA_PLATFORM=
                  offscreen before construction, guarded by importorskip on the se299-gui group).

Data path is the production one: the GUI runs coordinator.run_campaign over a real ControlPlane
(sim OR net:HOST:PORT:PAD bridges OR the qemu --vm/golden bring-up), receiving the SAME per-point
rows the coordinator computes -- SE(f) = reference(f) - wall(f), TX held constant so it cancels
(IEEE-299 substitution). NO fake in the runtime path; sim is a test double only.
"""
from __future__ import annotations

import os
import sys
import threading
import queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg_mod


class CampaignAborted(Exception):
    """Raised from a per-point callback when the operator hits Stop, to unwind the campaign
    cleanly (run_campaign's finally releases control)."""


# verdict -> colour (colour-blind-safe-ish): PASS green, FAIL red, INCONCLUSIVE amber
VERDICT_COLOR = {"PASS": "#2ca02c", "FAIL": "#d62728", "INCONCLUSIVE": "#ff7f0e"}


# ============================================================== model (no matplotlib)

class SEFigureModel:
    """Pure accumulator for a live substitution campaign. Fed on the MAIN thread (from the GUI
    queue) so it needs no lock of its own."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.total_points = len(cfg.frequencies())        # points per pass
        self.phase = "idle"                               # idle|reference|wall|done|error|aborted
        self.reference_points = []                        # {f_hz, band, capability_db, ea8_ok}
        self.wall_points = []                             # {f_hz, band, se_db, se_reported_db,
                                                          #  floor_limited, verdict, target_db}
        self.worst = None                                 # {se_db, lower_bound, band, f_hz, points}
        self.summary = None
        self.error = None

    # -- feed (called from the GUI timer as it drains the campaign queue) --------
    def set_phase(self, phase):
        self.phase = phase

    def add_reference_point(self, i, row):
        self.reference_points.append({
            "f_hz": row["f_hz"], "band": row["band"],
            "capability_db": row["capability_db"], "ea8_ok": row["ea8_ok"],
            "ref_dbm": row.get("ref_dbm"),                  # 8565EC received level (reference feed)
            "src_power_dbm": row.get("src_power_dbm")})     # KNOWN TX power at the source output

    def add_wall_point(self, figure, row):
        self.wall_points.append({
            "f_hz": row["f_hz"], "band": row["band"],
            "se_db": row.get("se_db"), "se_reported_db": row["se_reported_db"],
            "floor_limited": bool(row["floor_limited"]), "verdict": row["verdict"],
            "target_db": row.get("target_db"),
            "wall_dbm": row.get("wall_dbm")})               # 8565EC received level (wall feed)
        self.worst = dict(figure)                          # {se_db, lower_bound, band, f_hz, points,...}

    def set_summary(self, summary):
        self.summary = summary
        self.phase = "done"

    def set_error(self, exc):
        self.error = str(exc)
        self.phase = "aborted" if isinstance(exc, CampaignAborted) else "error"

    def reset(self):
        self.__init__(self.cfg)

    # -- display helpers (pure; the view consumes these) -------------------------
    def curve(self):
        """The wall-pass SE(f) curve sorted by frequency:
        (freqs_ghz, se_reported_db, verdicts, floor_limited)."""
        pts = sorted(self.wall_points, key=lambda r: r["f_hz"])
        return ([p["f_hz"] / 1e9 for p in pts],
                [p["se_reported_db"] for p in pts],
                [p["verdict"] for p in pts],
                [p["floor_limited"] for p in pts])

    def reference_curve(self):
        """The reference-pass capability(f) curve sorted by frequency: (freqs_ghz, capability_db,
        ea8_ok)."""
        pts = sorted(self.reference_points, key=lambda r: r["f_hz"])
        return ([p["f_hz"] / 1e9 for p in pts],
                [p["capability_db"] for p in pts],
                [p["ea8_ok"] for p in pts])

    def received_curve(self):
        """The raw 8565EC RECEIVED-LEVEL feed (dBm) per swept point, one series per pass:
        {"reference": (freqs_ghz, ref_dbm), "wall": (freqs_ghz, wall_dbm)}. This is the analyzer's
        OWN reading that FEEDS SE = ref - wall, surfaced so the operator sees the actual received
        power, not only the derived SE curve. Points missing a level (older rows) are dropped."""
        rp = sorted((p for p in self.reference_points if p.get("ref_dbm") is not None),
                    key=lambda r: r["f_hz"])
        wp = sorted((p for p in self.wall_points if p.get("wall_dbm") is not None),
                    key=lambda r: r["f_hz"])
        return {"reference": ([p["f_hz"] / 1e9 for p in rp], [p["ref_dbm"] for p in rp]),
                "wall": ([p["f_hz"] / 1e9 for p in wp], [p["wall_dbm"] for p in wp])}

    def _tx_values(self):
        """The commanded TX (source-output) powers dBm: the reference rows' src_power_dbm (actual,
        override included) once the run has started, else the cfg bands (shown before Run)."""
        vals = [p["src_power_dbm"] for p in self.reference_points if p.get("src_power_dbm") is not None]
        return vals or [b.source_power_dbm for b in self.cfg.bands]

    def tx_power_dbm(self):
        """The KNOWN TX power (dBm) as a scalar if uniform across bands, else None (varies by band)."""
        uniq = sorted({round(v, 3) for v in self._tx_values()})
        return uniq[0] if len(uniq) == 1 else None

    def tx_power_text(self):
        vals = self._tx_values()
        lo, hi = min(vals), max(vals)
        return f"TX {lo:+.1f} dBm" if lo == hi else f"TX {lo:+.1f}..{hi:+.1f} dBm"

    def peak_received(self):
        """The HIGHEST received power (dBm) found across both passes: {dbm, f_hz, pass} or None.
        The strongest tone the 8565EC saw during the run ('pass' = 'reference' or 'wall')."""
        best = None
        for pass_name, pts, key in (("reference", self.reference_points, "ref_dbm"),
                                    ("wall", self.wall_points, "wall_dbm")):
            for p in pts:
                v = p.get(key)
                if v is not None and (best is None or v > best["dbm"]):
                    best = {"dbm": v, "f_hz": p["f_hz"], "pass": pass_name}
        return best

    def peak_text(self):
        pk = self.peak_received()
        if pk is None:
            return "peak RX: (waiting)"
        return f"peak RX {pk['dbm']:+.1f} dBm @ {pk['f_hz'] / 1e9:.2f} GHz ({pk['pass']})"

    def target_db(self):
        """The acceptance target (dB). Uses the max band target as the headline line."""
        return max((b.target_se_db for b in self.cfg.bands), default=100.0)

    def band_spans_ghz(self):
        """[(name, lo_ghz, hi_ghz), ...] for x-axis band shading."""
        return [(b.name, b.f_lo_hz / 1e9, b.f_hi_hz / 1e9) for b in self.cfg.bands]

    def progress(self):
        """(phase, done, total_for_phase). Reference and wall each have total_points."""
        if self.phase in ("reference", "idle"):
            return (self.phase, len(self.reference_points), self.total_points)
        return (self.phase, len(self.wall_points), self.total_points)

    def worst_text(self):
        """One-line running worst-case SE readout (what the headline shows)."""
        if self.error and self.phase == "error":
            return f"ERROR: {self.error}"
        if self.phase == "aborted":
            return "STOPPED by operator"
        if self.worst is None or self.worst.get("se_db") is None:
            if self.phase == "reference":
                return f"acquiring reference (EA8): {len(self.reference_points)}/{self.total_points}"
            return "SE: (waiting -- press Run)"
        rel = ">=" if self.worst.get("lower_bound") else "="
        tail = "  CAMPAIGN PASS" if (self.summary or {}).get("campaign_pass") else ""
        if self.phase == "done" and not (self.summary or {}).get("campaign_pass"):
            tail = "  CAMPAIGN FAIL"
        return (f"worst SE {rel} {self.worst['se_db']:.1f} dB @ "
                f"{(self.worst['f_hz'] or 0) / 1e9:.2f} GHz "
                f"({self.worst['points']}/{self.total_points} pts){tail}")


# ============================================================== gui (PySide6 + pyqtgraph)

class SELiveGUI:
    """PySide6 + pyqtgraph view + operator controls over an SEFigureModel. The campaign runs in a
    BACKGROUND thread; it pushes events into a thread-safe queue that a main-thread QTimer drains
    (only _tick touches widgets, so the worker never crosses into Qt). The class holds a QMainWindow
    (self.window); Qt is imported LAZILY so importing this module needs no Qt (the model stays pure)."""

    def __init__(self, model, campaign_factory, title="se299 live SE figure (IEEE-299 substitution)"):
        self.model = model
        self.campaign_factory = campaign_factory          # (gain,rbw,span_lo,span_hi,power) -> (coord, bench)
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._shield_ok = threading.Event()               # operator "shield inserted, continue" gate
        self._thread = None
        self._gain = 33
        self._rbw = 1000.0
        self._span_lo = None            # operator sweep band (GHz); None = default 1-40 GHz plan
        self._span_hi = None
        self._power = None              # tone (source) power override (dBm); None = band default
        self._timer = None

        import pyqtgraph as pg                             # lazy: importing se_gui needs no Qt
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
        self.headline = QtWidgets.QLabel("")
        self.headline.setFont(mono)
        right.addWidget(self.headline)
        self.plot = qt_common.new_plot(title, "frequency (GHz)", "shielding effectiveness SE (dB)")
        right.addWidget(self.plot, 2)
        # 8565EC RECEIVED-LEVEL feed: TX power + peak-found readout, then the raw analyzer readings
        # (dBm) that FEED SE = ref - wall -- so the operator sees the actual received power, the TX
        # reference, and the strongest tone, not only the derived SE curve.
        self.feed_txt = QtWidgets.QLabel("")
        self.feed_txt.setFont(mono)
        self.feed_txt.setStyleSheet("color:#1f4e79;")
        right.addWidget(self.feed_txt)
        self.feed_plot = qt_common.new_plot("8565EC received level", "frequency (GHz)",
                                            "received power (dBm)")
        right.addWidget(self.feed_plot, 1)
        self.progress_txt = QtWidgets.QLabel("")
        self.progress_txt.setFont(mono)
        self.progress_txt.setStyleSheet("color:#333333;")
        right.addWidget(self.progress_txt)

        self._draw_static()
        # live artist: one scatter, restyled per point each render
        self._pts = pg.ScatterPlotItem(size=11)
        self.plot.addItem(self._pts)
        # feed artists: reference-pass curve (blue), wall-pass curve (red), a dashed TX-power line,
        # and a highlighted marker at the highest received power found.
        self._ref_feed = self.feed_plot.plot([], [], pen=pg.mkPen("#1f77b4", width=2))
        self._wall_feed = self.feed_plot.plot([], [], pen=pg.mkPen("#d62728", width=2))
        self._peak_pt = pg.ScatterPlotItem(size=13, pen=pg.mkPen("#000000", width=1.5),
                                           brush=pg.mkBrush("#ffd400"))
        self.feed_plot.addItem(self._peak_pt)
        self._tx_line = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False,
            pen=pg.mkPen("#2ca02c", style=self._QtGui.Qt.DashLine, width=1.5),
            label="TX", labelOpts={"position": 0.05, "color": "#2ca02c"})
        self.feed_plot.addItem(self._tx_line)

    # -- static scene ------------------------------------------------------------
    def _draw_static(self):
        pg, m = self._pg, self.model
        spans = m.band_spans_ghz()
        lo = min((s[1] for s in spans), default=1.0)
        hi = max((s[2] for s in spans), default=40.0)
        self.plot.setXRange(lo, hi)
        self.plot.setYRange(0.0, max(120.0, m.target_db() + 20.0))
        self.feed_plot.setXRange(lo, hi)                   # received-level panel shares the freq axis
        # band shading: a fixed translucent region per band
        shades = ["#f2f2ff", "#eaffea", "#fff2ea"]
        for k, (_name, blo, bhi) in enumerate(spans):
            reg = pg.LinearRegionItem(values=(blo, bhi), movable=False,
                                      brush=pg.mkBrush(shades[k % len(shades)]))
            reg.setZValue(-10)
            self.plot.addItem(reg)
        # acceptance target line
        t = m.target_db()
        self._target_line = pg.InfiniteLine(pos=t, angle=0, movable=False,
                                            pen=pg.mkPen("#555555", style=self._QtGui.Qt.DashLine, width=1.5),
                                            label=f"target {t:.0f} dB",
                                            labelOpts={"position": 0.95, "color": "#555555"})
        self.plot.addItem(self._target_line)

    # -- operator controls (reusable ControlPanel; no hand-placed geometry) ------
    def _build_controls(self, qt_common):
        cp = qt_common.ControlPanel("OPERATOR")
        rb = cp.add_buttons(("Run", self._on_run), ("Stop", self._on_stop))
        self.btn_run, self.btn_stop = rb[0], rb[1]
        # P0-5: between the reference and wall passes the campaign PAUSES for the operator to insert
        # the shield; this button releases the (blocked) worker. Disabled until the prompt fires.
        self.btn_shield = cp.add_buttons(("Shield inserted -> continue wall pass",
                                          self._on_shield_continue))[0]
        self.btn_shield.setEnabled(False)
        self.combo_gain = cp.add_combo("top-band gain (dBi)", ("33", "25"), self._on_gain, index=0)
        self.spin_rbw = cp.add_spin("analyzer RBW (Hz)", 1.0, 1e7, 1000.0, step=100.0, decimals=0,
                                    suffix="Hz", on_change=self._on_rbw)
        cp.add_label("sweep band GHz (lo / hi) -- auto = default 1-40")
        self.spin_slo = cp.add_optional_spin("", 0.0, 60.0, None, step=0.5, decimals=3, suffix="GHz",
                                             on_change=self._on_span)
        self.spin_shi = cp.add_optional_spin("", 0.0, 60.0, None, step=0.5, decimals=3, suffix="GHz",
                                             on_change=self._on_span)
        self.spin_pow = cp.add_optional_spin("tone power (dBm) -- auto = band default", -60.0, 30.0,
                                             None, step=1.0, decimals=1, suffix="dBm",
                                             on_change=self._on_power)
        cp.add_help("SE(f) = ref(f) - wall(f); TX held const (cancels). "
                    "green PASS, amber INCONC, red FAIL; open = floor-limited (>=).")
        cp.add_stretch()
        return cp

    def _on_gain(self, label):
        self._gain = int(label)

    def _on_rbw(self, val):
        self._rbw = float(val)

    def _on_span(self, *_):
        """A valid lo<hi pair sets the operator sweep band; either on 'auto' clears back to the
        default 1-40 GHz plan."""
        lo, hi = self.spin_slo.optional_value(), self.spin_shi.optional_value()
        if lo is not None and hi is not None and hi > lo:
            self._span_lo, self._span_hi = lo, hi
        else:
            self._span_lo = self._span_hi = None

    def _on_power(self, *_):
        self._power = self.spin_pow.optional_value()

    # -- operator seeding (from the CLI --span-lo/--span-hi/--power flags) --------
    def seed_span(self, lo_ghz, hi_ghz):
        self.spin_slo.set_optional(lo_ghz)
        self.spin_shi.set_optional(hi_ghz)
        self._on_span()

    def seed_power(self, power_dbm):
        self.spin_pow.set_optional(power_dbm)
        self._on_power()

    def _on_run(self, _event=None):
        if self._thread is not None and self._thread.is_alive():
            return                                          # already running
        self.model.reset()
        self._stop.clear()
        self._shield_ok.clear()                             # re-arm the shield gate for this run
        self.btn_shield.setEnabled(False)
        self._thread = threading.Thread(target=self._run_campaign, daemon=True)
        self._thread.start()

    def _on_stop(self, _event=None):
        self._stop.set()
        self._shield_ok.set()                               # unblock a worker parked at the shield prompt

    def _on_shield_continue(self, _event=None):
        # operator confirmed the shield is in place -> release the blocked worker into the wall pass
        self.btn_shield.setEnabled(False)
        self._shield_ok.set()

    def _shield_prompt(self):
        """on_shield_prompt hook -- runs on the WORKER thread BETWEEN the reference and wall passes.
        Tell the GUI to prompt, then BLOCK the worker (NOT the Qt main thread) until the operator
        clicks 'Shield inserted' or Stop. Stop during the pause unwinds via CampaignAborted so
        run_campaign's finally releases control + RF off."""
        self._q.put(("shield_prompt", None))
        while not self._shield_ok.wait(0.1):                # park the worker; main-thread timer keeps draining
            if self._stop.is_set():
                raise CampaignAborted()

    # -- background campaign -> queue --------------------------------------------
    def _run_campaign(self):
        q, stop = self._q, self._stop
        try:
            coord, bench = self.campaign_factory(self._gain, self._rbw,
                                                 self._span_lo, self._span_hi, self._power)

            def on_ref(i, row):
                if stop.is_set():
                    raise CampaignAborted()
                q.put(("ref", i, row))

            def on_wall(fig, row):
                q.put(("wall", fig, row))
                if stop.is_set():
                    raise CampaignAborted()

            # drive the coordinator directly so we stream BOTH reference and wall points.
            if not coord.ensure_ready():
                raise RuntimeError("instruments not READY (no fake) -- point at live units or sim")
            q.put(("phase", "reference"))
            # pre-gate on the RF path: refuse to run a full campaign on a dead path (which would
            # report SE ~= 0 as infinite shielding). PathNotLive surfaces via the except below.
            result = coord.run_campaign(bench=bench, on_se_update=on_wall,
                                        on_reference_point=on_ref,
                                        on_shield_prompt=self._shield_prompt,
                                        pre_check_path=True)
            q.put(("summary", result["summary"]))
        except Exception as e:                              # noqa: BLE001 -- surfaced to the GUI
            q.put(("error", e))

    # -- main-thread drain + redraw ----------------------------------------------
    def _drain(self):
        drained = False
        try:
            while True:
                evt = self._q.get_nowait()
                drained = True
                kind = evt[0]
                if kind == "phase":
                    self.model.set_phase(evt[1])
                elif kind == "shield_prompt":
                    self.model.set_phase("insert-shield")   # headline shows the pause
                    self.btn_shield.setEnabled(True)        # main-thread: enable the release button
                elif kind == "ref":
                    self.model.add_reference_point(evt[1], evt[2])
                elif kind == "wall":
                    if self.model.phase != "wall":
                        self.model.set_phase("wall")
                    self.model.add_wall_point(evt[1], evt[2])
                elif kind == "summary":
                    self.model.set_summary(evt[1])
                elif kind == "error":
                    self.model.set_error(evt[1])
        except queue.Empty:
            pass
        return drained

    def render(self):
        """Push the current model state into the pyqtgraph scatter + labels. Safe to call any time
        (the QTimer AND the headless tests call it). Returns the scatter item so tests can assert on
        its data (spot positions + brushes)."""
        pg, m = self._pg, self.model
        freqs, se, verdicts, floor = m.curve()
        if freqs:
            # auto-expand the y-lower so floor-flat / below-zero SE (e.g. a live run with no RF path,
            # ref==floor) stays VISIBLE rather than falling off the bottom of the axis.
            lo = min(0.0, min(se) - 10.0)
            hi = max(120.0, m.target_db() + 20.0)
            spots = []
            for x, y, v, fl in zip(freqs, se, verdicts, floor):
                color = VERDICT_COLOR.get(v, "#1f77b4")
                # floor-limited (lower bound) points drawn hollow: white face, coloured edge
                brush = pg.mkBrush("w") if fl else pg.mkBrush(color)
                spots.append({"pos": (x, y), "brush": brush, "pen": pg.mkPen(color, width=1.5)})
            self._pts.setData(spots)
            self.plot.setYRange(lo, hi)
        else:
            self._pts.setData([])
        # 8565EC received-level feed: reference + wall curves, the TX-power line, the peak marker.
        rc = m.received_curve()
        rf, rd = rc["reference"]
        wf, wd = rc["wall"]
        self._ref_feed.setData(rf, rd)
        self._wall_feed.setData(wf, wd)
        tx = m.tx_power_dbm()
        if tx is not None:
            self._tx_line.setValue(tx)
            self._tx_line.show()
        else:
            self._tx_line.hide()                           # power varies by band -> no single line
        pk = m.peak_received()
        self._peak_pt.setData([{"pos": (pk["f_hz"] / 1e9, pk["dbm"])}] if pk else [])
        ys = list(rd) + list(wd) + ([tx] if tx is not None else [])
        if ys:
            self.feed_plot.setYRange(min(ys) - 5.0, max(ys) + 5.0)
        self.feed_txt.setText(f"{m.tx_power_text()}   |   {m.peak_text()}")
        self.headline.setText(m.worst_text())
        ph, done, total = m.progress()
        self.progress_txt.setText(f"phase={ph}  {done}/{total} pts")
        return self._pts

    def _tick(self):
        self._drain()
        return self.render()

    def run(self, interval_ms=250, frames=None):
        """Interactive live GUI: a QTimer drains the campaign queue + repaints, then the Qt event
        loop runs. Blocks until the window closes. `frames` is accepted for call-site compatibility
        and ignored (the QTimer runs until close)."""
        import qt_common
        self._timer = qt_common.run_live(self.window, self._tick, interval_ms)
        return self._timer


# ============================================================== shared construction

def build_cfg(gain_dbi=33, rbw_hz=1000.0, use_opc=False,
              span_lo_ghz=None, span_hi_ghz=None, points=None, power_dbm=None):
    """A Campaign with the operator's controls applied. gain_dbi + rbw_hz set the top-band horn
    gain + analyzer RBW. When span_lo_ghz/span_hi_ghz are given, the DEFAULT 1-40 GHz plan is
    REPLACED by a single operator-defined sweep band (points = number of frequencies) -- this is
    how the operator sets the SWEEP FREQUENCY from the GUI. power_dbm overrides the transmitted
    TONE power on every band (None = the band default). use_opc defaults to False: the bench 68367C
    does not answer *OPC?, so a completion query just times out; the blocking bus order + settle
    dwell + the analyzer's own TS/DONE? still guarantee TX-settled-before-RX."""
    import dataclasses
    if span_lo_ghz is not None and span_hi_ghz is not None:
        gain = 33.0 if int(gain_dbi) == 33 else cfg_mod.WR28_STANDARD_25DBI.antenna_gain_dbi
        pwr = 12.0 if power_dbm is None else float(power_dbm)
        bands = [cfg_mod.BandPlan("operator sweep", float(span_lo_ghz) * 1e9,
                                  float(span_hi_ghz) * 1e9, int(points or 20), gain, pwr,
                                  -150.0, target_se_db=100.0)]
    else:
        bands = list(cfg_mod.DEFAULT_BANDS)
        if int(gain_dbi) == 25:
            bands[-1] = cfg_mod.WR28_STANDARD_25DBI
        if power_dbm is not None:
            bands = [dataclasses.replace(b, source_power_dbm=float(power_dbm)) for b in bands]
    analyzer = cfg_mod.AnalyzerSettings(rbw_hz=float(rbw_hz), vbw_hz=float(rbw_hz))
    source = cfg_mod.SourceSettings(use_opc=bool(use_opc))
    return cfg_mod.Campaign(bands=tuple(bands), analyzer=analyzer, source=source, label="se-gui")


def build_se_gui(analyzer_addr="sim", source_addr="sim", gain_dbi=33, rbw_hz=1000.0,
                 telemetry_port=0, client_id=None):
    """Build (SEFigureModel, SELiveGUI) wired to run a real campaign over the given addresses.
    'sim'/'sim' -> the simulator control plane; otherwise net:/VISA addresses via from_addresses.
    Returns the model + GUI; call gui.run() for the interactive window. `client_id` (if given) is
    announced to each bridge so this se-gui client shows in the device session registry."""
    import control_plane

    def factory(gain, rbw, span_lo=None, span_hi=None, power=None):
        cfg = build_cfg(gain, rbw, span_lo_ghz=span_lo, span_hi_ghz=span_hi, power_dbm=power)
        if analyzer_addr == "sim" and source_addr == "sim":
            cp = control_plane.simulated(cfg)
        else:
            cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                              client_id=client_id)
        return cp.make_coordinator(), getattr(cp, "bench", None)

    model = SEFigureModel(build_cfg(gain_dbi, rbw_hz))
    gui = SELiveGUI(model, factory)
    return model, gui
