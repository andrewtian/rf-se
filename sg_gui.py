"""Signal Generator panel for the se299 bench (PySide6), plus its pure model. SourceModel carries
NO Qt. RF defaults OFF (safety). Scope: CW + step-sweep; no modulation.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_SWEEP_POINTS = 100000        # safety cap so a mis-set step cannot build an unbounded list


class SourceModel:
    """Pure model of the 68367C source: CW freq/power/RF + step-sweep params + settle/absent."""

    def __init__(self):
        self.freq_hz = 2.45e9
        self.power_dbm = -20.0
        self.rf_on = False                 # SAFETY: RF off by default
        self.settled = False
        self.absent = False
        self.sweep_start_hz = 1e9
        self.sweep_stop_hz = 6e9
        self.sweep_step_hz = 1e9
        self.sweep_dwell_s = 0.2
        self.sweeping = False

    def set_state(self, freq_hz=None, power_dbm=None, rf_on=None):
        if freq_hz is not None:
            self.freq_hz = float(freq_hz)
        if power_dbm is not None:
            self.power_dbm = float(power_dbm)
        if rf_on is not None:
            self.rf_on = bool(rf_on)
        self.absent = False

    def set_settled(self, settled):
        self.settled = bool(settled)

    def set_absent(self, absent, reason=""):
        self.absent = bool(absent)
        self.reason = str(reason or "")           # link reason (FAULT "power-cycle..." vs ABSENT)

    def sweep_points(self):
        step = self.sweep_step_hz
        if step <= 0:
            return [self.sweep_start_hz]
        pts, f = [], self.sweep_start_hz
        while f <= self.sweep_stop_hz + 1.0 and len(pts) < MAX_SWEEP_POINTS:
            pts.append(f)
            f += step
        return pts

    def readout_text(self):
        if self.absent:
            r = getattr(self, "reason", "")
            if r and "power-cycle" in r.lower():
                return f"source FAULT -- {r}"
            return f"source ABSENT -- {r}" if r else "source ABSENT -- bridge unreachable"
        state = "ON" if self.rf_on else "off"
        settle = "settled" if self.settled else "settling"
        return (f"{self.freq_hz / 1e9:.4f} GHz  {self.power_dbm:.1f} dBm  RF {state}  "
                f"({settle if self.rf_on else 'idle'})")


import queue as _queue


class SourceEngine:
    """Background command-queue engine for the SG panel. Applies CW (freq/power/RF) and walks a
    step-sweep, one point per step_once. Owns tx via the hub. rf_off_safe() forces RF off while
    tx is held (called on stop/shutdown -- the RF safety invariant)."""

    name = "sg"

    def __init__(self, hub, out_queue):
        self.hub = hub
        self.q = out_queue
        self._cmds = _queue.Queue()
        self.suspended = False
        self._have_tx = False
        self._sweep = None          # [points, dwell_s, index] or None
        self._sweep_loop = False    # True = re-arm at the end (continuous paint, range mode)

    def enqueue(self, cmd):
        self._cmds.put(cmd)

    def suspend(self):
        self.suspended = True
        if self._have_tx:
            self.hub.release("tx", self)
            self._have_tx = False

    def resume(self):
        self.suspended = False

    def _acquire(self):
        if not self._have_tx:
            ok, who = self.hub.acquire("tx", self)
            if not ok:
                self.q.put(("absent", f"tx {who}"))
                return False
            self._have_tx = True
        return True

    def rf_off_safe(self):
        try:
            if self._have_tx:
                self.hub.source.rf_off()
        except Exception:
            pass

    def _emit_state(self):
        """Publish the 68367C live state for a debug pane: OF1 frequency readback, OSB status byte,
        and output level. Defensive -- any readback the driver/sim does not support degrades to None
        (never raises into the engine loop). Called only on an enqueued 'read_state' (throttled by the
        caller), so it adds no per-sweep bus load."""
        src = self.hub.source
        st = {"of1_mhz": None, "osb": None, "level_dbm": None}
        for key, meth in (("of1_mhz", "output_freq_mhz"), ("osb", "status_byte"),
                          ("level_dbm", "output_level_dbm")):
            fn = getattr(src, meth, None)
            if callable(fn):
                try:
                    st[key] = fn()
                except Exception:
                    pass
        self.q.put(("tx_state", st))

    def step_once(self):
        if self.suspended:
            return
        if not self._acquire():
            return
        try:
            # CONFLATE the queue: arrow-key auto-repeat can enqueue applies far faster than each
            # 0.4-0.6 s settle drains them, so executing every queued apply would keep retuning the
            # source for seconds after the user stops (and hammer an aging synth). Coalesce a RUN of
            # applies to only the LAST target -- one retune to where the user ended up -- while
            # preserving order against sweep/state commands (flush a pending apply before them).
            pending_apply = None

            def _flush_apply():
                nonlocal pending_apply
                if pending_apply is None:
                    return
                f, p, on, settle_s = pending_apply
                pending_apply = None
                src = self.hub.source
                src.set_freq(f); src.set_power(p)
                (src.rf_on() if on else src.rf_off())
                if on:
                    # A retune-while-hot needs a real analog settle: the OSB handshake reports
                    # leveled+locked almost immediately, but the ALC output level keeps ramping for
                    # a few hundred ms -- read too early and the analyzer sees a suppressed tone
                    # (live-proven: 0.05 s -> -72.8 dBm, >=0.4 s -> the true -3.8 dBm). A caller that
                    # reads the tone right after (point-op) passes a longer settle_s.
                    src.await_settled(settle_s) if settle_s is not None else src.await_settled()
                    self.q.put(("settled", src.settled_ok()))
                self._sweep = None

            while not self._cmds.empty():
                kind, *rest = self._cmds.get_nowait()
                if kind == "apply":
                    settle_s = rest[3] if len(rest) > 3 and rest[3] is not None else None
                    pending_apply = (rest[0], rest[1], rest[2], settle_s)   # last apply wins
                elif kind == "step_sweep":
                    _flush_apply()
                    self._sweep = [rest[0], rest[1], 0]
                    self._sweep_loop = rest[2] if len(rest) > 2 else False   # optional continuous paint
                elif kind == "stop_sweep":
                    _flush_apply()
                    self._sweep = None
                elif kind == "read_state":
                    _flush_apply()
                    self._emit_state()
            _flush_apply()
            if self._sweep is not None:
                points, dwell, idx = self._sweep
                if idx >= len(points):
                    if self._sweep_loop and points:
                        idx = 0                        # wrap: continuously repaint the range
                    else:
                        self._sweep = None
                if self._sweep is not None:
                    self.hub.source.set_freq(points[idx])
                    self.q.put(("swept", points[idx]))
                    self._sweep = [points, dwell, idx + 1]
        except Exception as e:                            # noqa: BLE001
            self.q.put(("absent", str(e)))

    def run(self, stop_event):
        try:
            while not stop_event.is_set():
                self.step_once()
                time.sleep(0.02)
        finally:
            self.rf_off_safe()


import threading


class SignalGeneratorPanel:
    """PySide6 SG panel over a SourceModel + SourceEngine. A QWidget usable standalone or embedded.
    RF defaults off; every control change enqueues an apply; stop() forces RF off."""

    def __init__(self, hub, title="68367C signal generator"):
        from PySide6 import QtWidgets, QtGui
        import qt_common
        import drivers
        self._QtWidgets = QtWidgets
        self._max_power_dbm = drivers.Anritsu68369.HARD_MAX_OUTPUT_DBM   # F4: don't let the spinner
        qt_common.ensure_app()                                          # offer a level the clamp rejects

        self.hub = hub
        self.model = SourceModel()
        self._q = _queue.Queue()
        self.engine = SourceEngine(hub, self._q)
        self._stop = threading.Event()
        self._thread = None
        self._timer = None

        self.widget = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(self.widget)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        cp = qt_common.ControlPanel("SIGNAL GENERATOR")
        self.spin_freq = cp.add_freq_spin("frequency (GHz)", 0.01, 40.0, self.model.freq_hz / 1e9,
                                          decimals=6, suffix="GHz", on_change=self._on_apply)
        self.spin_power = cp.add_spin("power (dBm)", -60.0, self._max_power_dbm, self.model.power_dbm,
                                      step=1.0, decimals=1, suffix="dBm", on_change=self._on_apply)
        self.chk_rf = cp.add_checkbox("RF ON", False, self._on_apply)
        cp.add_label("step sweep (GHz)")
        self.spin_sw_start = cp.add_spin("start", 0.01, 40.0, 1.0, step=0.5, decimals=3, suffix="GHz")
        self.spin_sw_stop = cp.add_spin("stop", 0.01, 40.0, 6.0, step=0.5, decimals=3, suffix="GHz")
        self.spin_sw_step = cp.add_spin("step", 0.001, 40.0, 1.0, step=0.1, decimals=3, suffix="GHz")
        self.btn_sweep = cp.add_buttons(("Run step sweep", self._on_sweep))[0]
        cp.add_help("RF defaults OFF. Stop / close forces RF off.")
        cp.add_stretch()
        col.addWidget(cp)
        self.readout = QtWidgets.QLabel("")
        self.readout.setFont(mono)
        col.addWidget(self.readout)

    def _on_apply(self, *_):
        self.model.set_state(freq_hz=self.spin_freq.value() * 1e9,
                             power_dbm=self.spin_power.value(), rf_on=self.chk_rf.isChecked())
        self.engine.enqueue(("apply", self.model.freq_hz, self.model.power_dbm, self.model.rf_on))

    def _on_sweep(self, *_):
        self.model.sweep_start_hz = self.spin_sw_start.value() * 1e9
        self.model.sweep_stop_hz = self.spin_sw_stop.value() * 1e9
        self.model.sweep_step_hz = self.spin_sw_step.value() * 1e9
        self.engine.enqueue(("step_sweep", self.model.sweep_points(), self.model.sweep_dwell_s))

    def _drain(self):
        try:
            while True:
                evt = self._q.get_nowait()
                if evt[0] == "settled":
                    self.model.set_settled(evt[1])
                elif evt[0] == "swept":
                    self.model.set_state(freq_hz=evt[1])
                elif evt[0] == "absent":
                    self.model.set_absent(True, evt[1] if len(evt) > 1 else "")
        except _queue.Empty:
            pass

    def render(self):
        self.readout.setText(self.model.readout_text())
        return self.readout

    def _tick(self):
        self._drain()
        return self.render()

    def start(self, interval_ms=200):
        from PySide6 import QtCore
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
        self.engine.rf_off_safe()


def build_sg_panel(hub, title="68367C signal generator"):
    return SignalGeneratorPanel(hub, title=title)
