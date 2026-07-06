"""In-GUI agent: Venice client request shaping, read-only tools (path-scoped), and the AgentSession
tool-call loop. All hardware-free and network-free (the Venice client takes an injected opener; the
AgentSession takes a fake client).

Run:  uv run python -m pytest rf-se/se299/tests/test_agent.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_core
import agent_tools
import venice_client


# ---- Venice client (injected opener, no network) --------------------------------------------

def test_venice_client_shapes_request_and_parses():
    captured = {}

    def opener(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode())
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}}]}).encode()

    c = venice_client.VeniceClient(api_key="k", opener=opener)
    msg = c.chat([{"role": "user", "content": "yo"}], model="m")
    assert msg["content"] == "hi"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer k"
    assert captured["body"]["model"] == "m" and captured["body"]["messages"][0]["content"] == "yo"


def test_venice_client_no_key_raises():
    with pytest.raises(venice_client.VeniceError):
        venice_client.VeniceClient(api_key="").chat([{"role": "user", "content": "x"}])


# ---- read-only tools, path-scoped -----------------------------------------------------------

def test_read_file_scoped_and_rejects_escape():
    assert "FreqPreset" in agent_tools.read_file("presets.py")
    with pytest.raises(ValueError):
        agent_tools.read_file("../../../../etc/passwd")
    with pytest.raises(ValueError):
        agent_tools.read_file("/etc/passwd")


def test_grep_finds_known_symbol():
    out = agent_tools.grep("class FreqPreset", "presets.py")
    assert "presets.py" in out and "FreqPreset" in out


def test_grep_and_list_dir_reject_escape():
    # the shared _safe_path guards ALL three read tools, not just read_file -- pin grep + list_dir too
    for bad in ("../../../../etc", "/etc", "../se299-evil"):
        with pytest.raises(ValueError):
            agent_tools.list_dir(bad)
    for bad in ("../../../../etc/passwd", "/etc/passwd"):
        with pytest.raises(ValueError):
            agent_tools.grep("root", bad)


def test_list_dir_lists_modules():
    out = agent_tools.list_dir(".")
    assert "presets.py" in out and "drivers.py" in out


def test_execute_dispatch_and_bench_state():
    assert "bench state unavailable" in agent_tools.execute("get_bench_state", {})
    assert agent_tools.execute("get_bench_state", {}, lambda: "LIVE") == "LIVE"
    assert "unknown tool" in agent_tools.execute("nope", {})


def test_execute_tool_error_is_text_not_raise():
    assert "tool error" in agent_tools.execute("read_file", {"path": "does_not_exist.xyz"})


# ---- AgentSession tool-call loop (fake client) ----------------------------------------------

class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, model=None):
        self.calls.append({"messages": list(messages), "tools": tools, "model": model})
        return self.script.pop(0)


def _tool_call(name, args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_executes_tool_call_then_returns_final():
    c = _FakeClient([_tool_call("read_file", {"path": "presets.py"}),
                     {"role": "assistant", "content": "presets.py defines FreqPreset."}])
    s = agent_core.AgentSession(c)
    out = s.ask("what is in presets.py?")
    assert out == "presets.py defines FreqPreset."
    assert any(m.get("role") == "tool" and "FreqPreset" in m.get("content", "") for m in s.messages)
    assert len(c.calls) == 2                                   # tool hop + final answer


def test_agent_returns_plain_answer_without_tools():
    c = _FakeClient([{"role": "assistant", "content": "hi"}])
    assert agent_core.AgentSession(c).ask("hello") == "hi"


def test_agent_stops_at_tool_hop_limit():
    c = _FakeClient([_tool_call("list_dir", {})] * 20)
    s = agent_core.AgentSession(c, max_tool_hops=3)
    out = s.ask("loop")
    assert "hop limit" in out and len(c.calls) == 3           # bounded, never infinite


def test_get_bench_state_tool_exposed_only_with_provider():
    c = _FakeClient([{"role": "assistant", "content": "ok"}])
    assert not any(t["function"]["name"] == "get_bench_state" for t in agent_core.AgentSession(c)._tools)
    s = agent_core.AgentSession(c, bench_state_provider=lambda: "CF 2.45 GHz")
    assert any(t["function"]["name"] == "get_bench_state" for t in s._tools)


def test_system_prompt_carries_device_facts():
    c = _FakeClient([{"role": "assistant", "content": "ok"}])
    sysmsg = agent_core.AgentSession(c).messages[0]["content"]
    assert "CF1" in sysmsg and "2.9 GHz" in sysmsg and "ADVISORY" in sysmsg   # persona + device facts


# ---- Qt panel (offscreen) -------------------------------------------------------------------

def test_agent_panel_send_appends_reply_via_queue():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import agent_panel

    class _FakeSession:
        def ask(self, text):
            return f"echo: {text}"

    p = agent_panel.AgentPanel(session=_FakeSession())    # injected session -> no key/network needed
    p.input.setText("what is SE?")
    p._send()
    p._worker.join(timeout=5)                             # the bg worker posts to the queue...
    p._drain()                                            # ...the QTimer drain renders it
    txt = p.transcript.toPlainText()
    assert "you: what is SE?" in txt and "echo: what is SE?" in txt


def test_agent_panel_without_key_prompts_for_it(monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("VENICE_API_KEY", raising=False)
    import agent_panel
    p = agent_panel.AgentPanel()                          # no session, no key
    p.input.setText("hello")
    p._send()
    assert "no Venice API key" in p.transcript.toPlainText()   # never silently no-ops


def test_bench_hosts_the_assistant_dock():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import bench_gui
    b = bench_gui.build_bench("sim", "sim")
    assert b.agent is not None                            # assistant dock constructed
    hint = b._bench_state_hint()                          # cached state, no bus ops
    assert "active mode" in hint
