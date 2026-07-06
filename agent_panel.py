"""AgentPanel: the in-GUI conversational bench assistant (Venice-backed).

A chat transcript + input box. The Venice call runs on a BACKGROUND thread and posts the reply back via a
queue drained by a QTimer, so the UI never blocks during a request (the same pattern the instrument
engines use). ADVISORY ONLY: the agent's tools are read-only and it cannot drive the instruments -- it
suggests, the operator acts.

Qt is imported lazily; the brain (agent_core.AgentSession) is Qt-free and injectable, so the panel is
testable offscreen with a fake client/session. The API key is operator-supplied (VENICE_API_KEY env or a
masked field) and never persisted by this code. A `state_fn` (optional) lets the host expose CACHED bench
state to the agent WITHOUT the agent querying the instruments (which would collide with the active mode's
single consumer).
"""
from __future__ import annotations

import os
import queue as _queue
import threading

import agent_core
import venice_client

AGENT_MODEL_ENV = "VENICE_MODEL"
DEFAULT_AGENT_MODEL = "qwen-3-7-max"   # live-verified tool-calling on Venice; override via VENICE_MODEL


class AgentPanel:
    """`state_fn` (optional, zero-arg -> str) supplies a cached live-state snapshot; when given, the
    agent's get_bench_state tool is exposed. `client`/`session` are injectable for tests."""

    def __init__(self, hub=None, *, state_fn=None, client=None, session=None, model=None):
        from PySide6 import QtWidgets, QtGui, QtCore
        import qt_common
        qt_common.ensure_app()
        self._QtWidgets = QtWidgets
        self.hub = hub
        self.state_fn = state_fn
        self.model = model or os.environ.get(AGENT_MODEL_ENV, DEFAULT_AGENT_MODEL)
        self._client = client
        self.session = session
        self._q = _queue.Queue()
        self._busy = False
        self._worker = None                            # last send worker (tests join on it)

        self.widget = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(self.widget)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.transcript = QtWidgets.QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(mono)
        root.addWidget(self.transcript, 1)
        # masked key field, shown only if VENICE_API_KEY is absent (operator pastes it; kept in memory)
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText("VENICE_API_KEY (or set the env var)")
        if os.environ.get("VENICE_API_KEY"):
            self.key_edit.hide()
        root.addWidget(self.key_edit)
        row = QtWidgets.QHBoxLayout()
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText("Ask the bench assistant (it can read the source + docs)...")
        self.input.returnPressed.connect(self._send)
        self.btn_send = QtWidgets.QPushButton("Send")
        self.btn_send.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(self.btn_send)
        root.addLayout(row)
        self.status = QtWidgets.QLabel(self._IDLE)
        root.addWidget(self.status)
        self.transcript.setPlainText(
            "Bench assistant (advisory). Ask about the bench, a reading, or the code -- it reads the actual "
            "source + docs. It suggests; you drive the instruments.\n")

        self._timer = QtCore.QTimer(self.widget)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    _IDLE = "advisory -- read-only tools; the agent suggests, you act"

    def _api_key(self):
        return (self.key_edit.text() or os.environ.get("VENICE_API_KEY", "")).strip()

    def _ensure_session(self):
        """Build the AgentSession lazily once a key is present (or use an injected one)."""
        if self.session is not None:
            return self.session
        key = self._api_key()
        if not key:
            return None
        if self._client is None:
            self._client = venice_client.VeniceClient(api_key=key, model=self.model)
        self.session = agent_core.AgentSession(self._client, model=self.model,
                                               bench_state_provider=self.state_fn)
        return self.session

    def _append(self, who, text):
        self.transcript.appendPlainText(f"{who}: {text}")

    def _send(self):
        if self._busy:
            return
        text = self.input.text().strip()
        if not text:
            return
        sess = self._ensure_session()
        if sess is None:
            self._append("system", "no Venice API key -- paste it in the field above (or set VENICE_API_KEY)")
            return
        self._append("you", text)
        self.input.clear()
        self._busy = True
        self.status.setText("thinking...")

        def work():
            try:
                self._q.put(("assistant", sess.ask(text)))
            except Exception as e:                     # noqa: BLE001 -- surface transport/HTTP failures
                self._q.put(("error", str(e)))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _drain(self):
        try:
            while True:
                who, text = self._q.get_nowait()
                self._append("assistant" if who == "assistant" else "system",
                             text if who == "assistant" else f"error: {text}")
                self._busy = False
                self.status.setText(self._IDLE)
        except _queue.Empty:
            pass
