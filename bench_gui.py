"""The se299 bench window: a MODULAR, tabbed host for bench MODES over one shared InstrumentHub.

Modes (pluggable -- each exposes .name / .widget / .start(ms) / .stop() / .suspend() / .resume()):
  - Full Bench      : the full-fidelity Spectrum Analyzer (dominant) + Signal Generator strip.
  - Range (paint)   : step a wide-bandwidth painted tone across the range with the arrow keys; the
                      RX shows ONLY that range (range_mode.RangeModePanel).

Only the ACTIVE tab owns the 68367C + 8565EC; switching tabs suspends the leaving mode (RF off +
release leases) and resumes the entering one, so the two modes never contend for the bus. Add a new
mode by appending it to `self.modes`. Qt is imported lazily so importing this module needs no Qt.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FullBenchMode:
    """The original bench view as a mode: SA (dominant) + SG (strip) sharing the hub."""

    name = "Full Bench (SA + SG)"

    def __init__(self, hub):
        from PySide6 import QtWidgets
        import sa_gui
        import sg_gui
        self.hub = hub
        self.sa = sa_gui.build_sa_panel(hub)
        self.sg = sg_gui.build_sg_panel(hub)
        self.widget = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.widget)
        row.addWidget(self.sa.widget, 1)     # dominant
        row.addWidget(self.sg.widget, 0)     # right strip

    def start(self, interval_ms=200):
        self.sa.start(interval_ms)
        self.sg.start(interval_ms)

    def stop(self):
        self.sa.stop()          # stop SA sweeping
        self.sg.stop()          # stop SG + rf_off (safety)

    def suspend(self):
        self.sg.engine.rf_off_safe()
        self.sa.engine.suspend()
        self.sg.engine.suspend()

    def resume(self):
        self.sa.engine.resume()
        self.sg.engine.resume()

    # uniform engine handles so the bench can check ownership across all modes the same way
    @property
    def rx_engine(self):
        return self.sa.engine

    @property
    def tx_engine(self):
        return self.sg.engine


class BenchWindow:
    """Tabbed host for the bench modes over one InstrumentHub, with ordered shutdown (stop every
    mode -- which forces RF off -- then release the hub's leases) and active-tab-only instrument
    ownership."""

    def __init__(self, hub, title="se299 bench -- modular (Full Bench | Range | Point Op)"):
        from PySide6 import QtWidgets
        import qt_common
        import range_mode
        import point_op_mode
        qt_common.ensure_app()

        self.hub = hub
        self.full = FullBenchMode(hub)
        self.range = range_mode.build_range_mode(hub)
        self.point = point_op_mode.build_point_op_mode(hub)
        self.modes = [self.full, self.range, self.point]
        self.sa, self.sg = self.full.sa, self.full.sg     # back-compat handles

        self.window = QtWidgets.QMainWindow()
        self.window.setWindowTitle(title)
        self.tabs = QtWidgets.QTabWidget()
        for m in self.modes:
            self.tabs.addTab(m.widget, m.name)
        self.window.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab)

        # Optional conversational assistant (Venice-backed) as a DOCK -- deliberately NOT a mode/tab, so it
        # never participates in the single-consumer instrument lifecycle. It reads a CACHED state hint (no
        # bus ops) + the source/docs via read-only tools, and is advisory only. Never block the bench if it
        # fails to construct (missing PySide6 feature, etc.).
        self.agent = None
        try:
            from PySide6 import QtCore
            import agent_panel
            self.agent = agent_panel.AgentPanel(hub, state_fn=self._bench_state_hint)
            dock = QtWidgets.QDockWidget("Assistant", self.window)
            dock.setWidget(self.agent.widget)
            self.window.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
            self._agent_dock = dock
        except Exception:                              # noqa: BLE001 -- assistant is optional
            self.agent = None

        # lifecycle watchdog: a persistent status strip that reports each unit's state and flags a
        # SUSTAINED invariant violation, so a torn ownership state can never present as a silent blank
        # graph again (it was the flaky no-feed bug).
        self._wd_label = QtWidgets.QLabel("")
        self.window.statusBar().addPermanentWidget(self._wd_label, 1)
        self._viol_streak = 0
        self._wd_warned = False
        self._wd_timer = None

        orig_close = self.window.closeEvent

        def _close(ev):
            self.shutdown()
            orig_close(ev)

        self.window.closeEvent = _close

    def _on_tab(self, idx):
        # only the active tab drives the shared instruments; the rest release + go RF-off
        for i, m in enumerate(self.modes):
            (m.resume if i == idx else m.suspend)()

    def _bench_state_hint(self):
        """A CACHED, read-only live-state snapshot for the assistant -- reads only the active mode's model
        attributes, NEVER the instruments (a bus query would collide with the mode's single consumer)."""
        try:
            active = self.modes[self.tabs.currentIndex()]
            bits = [f"active mode: {active.name}"]
            m = getattr(active, "model", None)
            for attr in ("freq_hz", "span_hz", "power_dbm", "rf_on", "reference_dbm", "current_dbm"):
                if m is not None and hasattr(m, attr):
                    bits.append(f"{attr}={getattr(m, attr)}")
            return "; ".join(bits)
        except Exception as e:                         # noqa: BLE001 -- best-effort hint
            return f"bench state unavailable: {e}"

    def invariant_violations(self, active=None):
        """Lifecycle invariants for the two shared instruments: the ACTIVE tab is the SOLE driver of
        rx and tx, and every non-active mode is suspended. Returns human-readable violation strings
        ([] = healthy), so a test or a watchdog can assert the bench never drifts into the torn multi-
        owner state that caused the flaky no-feed. Ownership set with no lease yet (a handoff in
        flight) is NOT a violation -- that transient is expected; only a WRONG owner is flagged."""
        active = self.tabs.currentIndex() if active is None else active
        viol = []
        for inst in ("rx", "tx"):
            owner = self.hub.owner(inst)
            if owner is None:
                continue                                     # free / handoff in flight: not a fault
            active_eng = getattr(self.modes[active], f"{inst}_engine", None)
            if owner is not active_eng:
                who = next((m.name for m in self.modes
                            if getattr(m, f"{inst}_engine", None) is owner), "external")
                viol.append(f"{inst}: driven by {who}, not the active tab {self.modes[active].name}")
        for i, m in enumerate(self.modes):
            if i == active:
                continue
            for inst in ("rx", "tx"):
                e = getattr(m, f"{inst}_engine", None)
                if e is not None and not e.suspended:
                    viol.append(f"non-active {m.name}: {inst}_engine not suspended")
        return viol

    def state_report(self):
        """Compact per-unit lifecycle snapshot (owner tab, lease, link health) + any invariant
        violations -- for a debug pane or a health log."""
        return {"units": self.hub.state_snapshot(),
                "active_tab": self.modes[self.tabs.currentIndex()].name,
                "violations": self.invariant_violations()}

    def _watchdog_tick(self):
        """Poll the lifecycle state on the main thread. A single-tick violation is an expected tab-
        handoff transient (owner not yet moved to the new engine); only a SUSTAINED violation (2+
        consecutive ticks) is surfaced as a warning + logged with the full per-unit state, so the
        operator sees exactly what each unit is doing instead of a blank graph."""
        try:
            rep = self.state_report()
        except Exception:                                # never let the watchdog throw into the loop
            return
        v = rep["violations"]
        self._viol_streak = self._viol_streak + 1 if v else 0
        if self._viol_streak >= 2:
            self._set_watchdog("LIFECYCLE WARNING: " + "; ".join(v), True)
            if not self._wd_warned:                      # log once at the onset of a sustained fault
                self._wd_warned = True
                print(f"[bench watchdog] sustained lifecycle violation: {v} | units={rep['units']}")
        else:
            self._wd_warned = False
            u = rep["units"]
            def _u(k):
                s = u[k]
                return f"{k} {s['link_state'] or '?'} {'leased' if s['lease_held'] else 'free'}"
            self._set_watchdog(f"active: {rep['active_tab']}   |   {_u('rx')}   |   {_u('tx')}", False)

    def _set_watchdog(self, text, warn):
        self._wd_label.setText(text)
        self._wd_label.setStyleSheet("color: #b00020; font-weight: bold;" if warn else "color: #556;")

    def start(self, interval_ms=200):
        # LIFECYCLE INVARIANT: exactly ONE mode (the active tab) drives the two shared instruments.
        # Pre-suspend every mode BEFORE starting its engine threads -- otherwise all three modes spawn
        # threads unsuspended and RACE for the rx/tx leases, a non-active mode wins the analyzer, and
        # the active tab (e.g. Point Op) then cannot acquire it -> flaky no-feed. Parking every engine
        # first means only the active mode, resumed below, ever acquires.
        active = self.tabs.currentIndex()
        for m in self.modes:
            m.suspend()                              # engine.suspended=True before any thread runs
            m.start(interval_ms)                     # threads spawn already parked (no acquire race)
        self._on_tab(active)                         # resume ONLY the active tab -> sole owner
        from PySide6 import QtCore                    # lifecycle watchdog on the main thread
        self._wd_timer = QtCore.QTimer(self.window)
        self._wd_timer.setInterval(750)              # 2 ticks (~1.5 s) tolerance for a handoff transient
        self._wd_timer.timeout.connect(self._watchdog_tick)
        self._wd_timer.start()

    def shutdown(self):
        if self._wd_timer is not None:
            try:
                self._wd_timer.stop()
            except Exception:
                pass
        for m in self.modes:
            try:
                m.stop()                                 # RF off + stop threads (per mode)
            except Exception:
                pass                                     # never let one mode's teardown skip the rest
        self.hub.shutdown()      # ALWAYS release leases (else the next launch is blocked till the TTL)

    def run(self, interval_ms=200):
        import qt_common
        self.start(interval_ms)
        return qt_common.run_live(self.window, lambda: None, interval_ms)


def build_bench(analyzer_addr="sim", source_addr="sim", client_id=None):
    """Build a BenchWindow over the given instrument addresses (sim / net:HOST:PORT:PAD / VISA)."""
    import control_plane
    import config as cfg_mod
    from instrument_hub import InstrumentHub

    cfg = cfg_mod.Campaign(instruments=cfg_mod.Instruments(source_addr=source_addr,
                                                           analyzer_addr=analyzer_addr),
                           bands=tuple(cfg_mod.DEFAULT_BANDS), label="bench")
    if analyzer_addr == "sim" and source_addr == "sim":
        cp = control_plane.simulated(cfg)
    else:
        cp = control_plane.from_addresses(cfg, rx_addr=analyzer_addr, tx_addr=source_addr,
                                          client_id=client_id)
    coord = cp.make_coordinator()
    return BenchWindow(InstrumentHub(coord))
