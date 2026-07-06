# se299 GUI migration: matplotlib -> PySide6 + PyQtGraph

Live tracking file for migrating the se299 operator GUIs off matplotlib onto PySide6 (native Qt
widgets) + PyQtGraph (realtime plots). Updated as each phase completes. Do NOT delete until the
migration is signed off (all phases DONE + full suite green).

## Decision + basis
- Library: PySide6 6.11.1 (LGPL) native widgets + PyQtGraph 0.14.0 (MIT) realtime plots; pytest-qt
  4.5.0 for headless view tests. Chosen over Dear PyGui / web / Textual / matplotlib-restructured
  (see the ranked evaluation; the deciding filter is headless-in-pytest testability + a fast realtime
  plot + native numeric controls + arm64/py3.13 wheels). Verified installed + offscreen-constructible
  on this machine (Python 3.13, macOS arm64): QApplication(offscreen) + PlotWidget.setData +
  QDoubleSpinBox/QPushButton signals all work headless.
- Deps live in an OPTIONAL group `se299-gui` (`uv sync --group se299-gui`) so the shared CAD venv is
  not burdened by ~420 MB of Qt. matplotlib STAYS a core dep (used repo-wide + by non-GUI se299 code).

## The migration contract (must hold at every step)
- A. Pure models UNCHANGED: `SEFigureModel`, `NearFieldModel`, `LiveSpectrumModel` stay plain-Python,
     no GUI dependency, and their existing model unit tests keep passing verbatim.
- B. Keep worker-thread -> thread-safe `queue.Queue` -> main-thread drain. The producer threads
     (`_run_campaign`/`_run_walk`/`_run`) and the `_drain`/`render`/`_tick` contract are preserved;
     only the timer (matplotlib FuncAnimation -> `QTimer`) and the artists (matplotlib -> pyqtgraph)
     change. This keeps the direct-call test path (`gui._drain(); gui.render()`) intact.
- C. Headless-testable: Qt view tests set `QT_QPA_PLATFORM=offscreen` before import (the direct analog
     of today's `matplotlib.use("Agg")`), guarded by `pytest.importorskip("PySide6")` so the default
     shared-env run still passes when the group is not installed. View tests stay at the smoke/wiring
     level (construct, signal fires, model updates, render() does not raise); pixel assertions are out.

## In scope (three operator windows + wiring + tests)
1. `se_gui.py`   `SELiveGUI`        -- live SE(f) scatter + operator controls (`cli.py se-gui`)
2. `walkaround.py` `NearFieldGUI`   -- near-field probe meter/trace + controls (`cli.py walkaround`)
3. `live.py`     `LiveSpectrumGUI`  -- live moving 8565EC spectrum (`cli.py live`)
- Shared: new `qt_common.py` (app bootstrap, reusable operator-control widgets, the queue/drain/QTimer
  LiveView base) -- removes the copy-pasted control-panel geometry the audit flagged.
- `cli.py` wiring (`run()` lifecycle: ensure app + exec; control seeding).
- Tests: `tests/test_se_gui.py`, `tests/test_walkaround.py`, `tests/test_live.py`.

## Out of scope (stay on matplotlib)
- Static/report plots + snapshots elsewhere in `rf-se/` and se299 (`roles.py` has no matplotlib;
  `cli.py` snapshot/other commands; `test_calibration.py`). Only the three interactive windows move.

## Phases (checkbox = DONE + verified)
- [x] P0  Gate: add `se299-gui` group (pyside6, pyqtgraph, pytest-qt); verify offscreen construct.
- [x] P1  `qt_common.py`: ensure_app + ControlPanel + OptionalDoubleSpin + new_plot + run_live.
- [x] P2  Migrate `se_gui.py` view to Qt/pyqtgraph (model + queue + handlers preserved).
- [x] P3  Migrate `walkaround.py` view (fast heat meter + rolling trace + marks).
- [x] P4  Migrate `live.py` `LiveSpectrumGUI` view.
- [x] P5  Rewrite view tests headless offscreen (importorskip); keep model tests verbatim; add REAL
          widget/plot-state assertions (curve data, max-hold line pos, heat-label colour, control vals)
          per the eval's "thin tests give false confidence" risk.
- [x] P6  `cli.py` wiring: `run()` app/exec lifecycle + control seeding for all three.
- [x] P7  Full green: model tests + Qt view tests + non-GUI e2e unaffected; smoke each window offscreen.
- [x] P8  Docs: README GUI section + `pyproject` group note + this file signed off.

## SIGN-OFF: migration COMPLETE (all phases DONE + verified)
- Verification evidence:
  - WITH the `se299-gui` group (offscreen): full hardware-free suite = 266 passed (incl. all Qt view
    tests with real widget/plot-state assertions); the 3 GUI files = 31 passed.
  - WITHOUT the group (PySide6 blocked, simulating a fresh `uv sync` with no group): the 3 GUI files =
    20 passed + 11 skipped (view tests importorskip cleanly; pure models always run). Contract C's
    graceful degradation proven BOTH ways.
  - Live-hardware e2e (test_e2e_live) = 5/5, UNCHANGED -- the GUI migration never touched the
    instrument path.
  - Offscreen smokes of all three windows pass (construct + feed + render, real state asserted).
- Result: `se_gui.py` / `walkaround.py` / `live.py` carry NO matplotlib and NO old widgets; the three
  operator windows are PySide6 native controls + pyqtgraph realtime plots; `qt_common.py` removes the
  copy-pasted `add_axes` control geometry; matplotlib remains a core dep for all non-GUI/report use.
  Pure models (contract A), worker-thread->queue->main-thread drain (contract B), and headless
  pytest testability (contract C) all preserved. Library pick independently re-confirmed by an
  adversarially-verified ranked evaluation (PySide6 + PyQtGraph = recommended tier).

## Status log (newest first)
- P5-P8 done + SIGN-OFF (see above). Tests rewritten with real state assertions; cli seeding moved to
  gui.seed_span/seed_power (old TextBox refs gone); README + pyproject group documented; module
  docstrings de-matplotlib'd.
- P1-P4 done: `qt_common.py` created (ControlPanel kills the copy-pasted add_axes geometry;
  OptionalDoubleSpin gives the auto/None sweep-band + power fields; run_live = QTimer loop). All three
  views ported to pyqtgraph + native Qt controls, models/queue/handlers preserved. Offscreen smokes
  PASS: se_gui (scatter 2 pts + seed), walkaround (trace + max-hold -60 + heat=hot red + mark + freq
  spin), live (101-pt spectrum + hot marker + readout).
- P0 done: `se299-gui` group installed (pyside6 6.11.1 / pyqtgraph 0.14.0 / pytest-qt 4.5.0);
  offscreen construct+update+signal smoke PASS on py3.13 arm64.
