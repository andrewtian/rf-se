"""The conversational brain for the in-GUI bench agent: an AgentSession that holds the message history,
calls the Venice client, and runs the read-only tool-call loop. No Qt, no hardware -- fully unit-testable
by injecting a fake client. The GUI wraps this in a background thread (see agent_panel).
"""
from __future__ import annotations

import json

import agent_knowledge
import agent_tools


class AgentSession:
    """One conversation. `client` is a VeniceClient (or any object with .chat(messages, tools=...) ->
    message dict). `bench_state_provider` (optional) is a zero-arg callable returning a live-state string;
    when given, the get_bench_state tool is exposed and its output can also seed the system prompt."""

    def __init__(self, client, *, bench_state_provider=None, model=None, max_tool_hops=6,
                 bench_state_hint=""):
        self.client = client
        self.bench_state_provider = bench_state_provider
        self.model = model
        self.max_tool_hops = int(max_tool_hops)
        self._tools = agent_tools.tool_schemas(bench_state_provider)
        self.messages = [{"role": "system",
                          "content": agent_knowledge.system_prompt(bench_state_hint)}]

    def ask(self, user_text: str) -> str:
        """Add the operator's message, run the model with tools (executing any read-only tool calls), and
        return the assistant's final text. Never raises for a tool failure (the tool error is fed back to
        the model); a client/transport failure propagates so the GUI surfaces it."""
        self.messages.append({"role": "user", "content": str(user_text)})
        for _ in range(self.max_tool_hops):
            msg = self.client.chat(self.messages, tools=self._tools, model=self.model)
            self.messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content") or ""
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                result = agent_tools.execute(name, args, self.bench_state_provider)
                self.messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                      "name": name, "content": str(result)[:8000]})
        return "(stopped: reached the tool-call hop limit without a final answer)"

    def reset(self, bench_state_hint: str = ""):
        """Clear the conversation, keeping the configured tools/provider."""
        self.messages = [{"role": "system",
                          "content": agent_knowledge.system_prompt(bench_state_hint)}]
