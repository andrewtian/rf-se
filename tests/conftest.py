"""Session Qt lifecycle: force per-test C++ QObject destruction so the FULL board (Qt + non-Qt tests
co-located in one process) does not SIGSEGV inside shiboken at interpreter finalization.

Root cause (rehearsed, read-only): the crash is a nondeterministic shiboken QObject-teardown
segfault at interpreter shutdown, driven by Qt widgets/timers that were never destroyed and survive
to finalization. In the Qt-only or non-Qt-only split each run is small enough to dodge it; the full
712-test board co-locates them and trips it intermittently.

Fix: after every test, if Qt was loaded, drain the deferred-delete queue + close windows so no QObject
survives to interpreter exit; and keep ONE reference to the QApplication so it is never GC-destroyed
at a bad moment (the app is deliberately leaked to true interpreter exit with an empty widget set).

PRIOR-SAFE (this conftest is imported for EVERY test, including the Qt-free board that collects and
runs WITHOUT the se299-gui/PySide6 group -- committed capability 9b7c54bd):
  * NO Qt import at module top -- only os/gc/sys/pytest.
  * The teardown is a PURE NO-OP unless PySide6.QtWidgets is ALREADY in sys.modules (a Qt-free test
    never imports Qt, so the fixture does nothing and cannot break it).
  * No --timeout / pytest-timeout dependency (not installed); rely on pytest's built-in faulthandler.
"""
import gc
import os
import sys

import pytest

# Set the offscreen platform before any test module import can trigger a Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_LEAKED_APP = []          # hold ONE ref to the QApplication so it is not GC-destroyed pre-exit


@pytest.fixture(autouse=True)
def _qt_object_teardown():
    """Drain Qt deferred deletions after each test that touched Qt, so no widget/timer survives to
    interpreter finalization (the shiboken shutdown SIGSEGV). Pure no-op when Qt was never loaded."""
    yield
    qtw = sys.modules.get("PySide6.QtWidgets")
    if qtw is None:                                   # Qt-free test -> nothing loaded -> no-op
        return
    app = qtw.QApplication.instance()
    if app is None:
        return
    if not _LEAKED_APP:                               # leak the app: never GC-destroy it mid-run
        _LEAKED_APP.append(app)
    qtc = sys.modules.get("PySide6.QtCore")
    try:
        qtw.QApplication.closeAllWindows()
        if qtc is not None:
            app.sendPostedEvents(None, qtc.QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()
        app.processEvents()
    except Exception:                                 # teardown is best-effort, never fail a test
        pass


def pytest_sessionfinish(session, exitstatus):
    """Final drain: close any straggler windows + flush DeferredDelete once more so the interpreter
    finalizes with an empty Qt widget set. No-op if Qt was never loaded."""
    qtw = sys.modules.get("PySide6.QtWidgets")
    if qtw is None:
        return
    app = qtw.QApplication.instance()
    if app is None:
        return
    qtc = sys.modules.get("PySide6.QtCore")
    try:
        qtw.QApplication.closeAllWindows()
        if qtc is not None:
            app.sendPostedEvents(None, qtc.QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()
    except Exception:
        pass
