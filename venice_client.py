"""Thin Venice API client (OpenAI-compatible chat/completions) for the in-GUI bench assistant.

Stdlib only (urllib) -- NO new dependency. The API key is read from the environment (VENICE_API_KEY) or
passed explicitly; it is NEVER hardcoded, logged, or committed. Venice is privacy-focused (does not train
on prompts), but sending source/state to ANY external API is a data egress the operator opts into.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

VENICE_BASE_URL = "https://api.venice.ai/api/v1"
DEFAULT_MODEL = "default"                     # Venice resolves "default" to its current default chat model
API_KEY_ENV = "VENICE_API_KEY"


class VeniceError(RuntimeError):
    """Any transport / HTTP / protocol failure talking to Venice."""


class VeniceClient:
    """Minimal chat client. `chat()` returns the first choice's message dict {role, content, tool_calls?}."""

    def __init__(self, api_key=None, base_url=VENICE_BASE_URL, model=DEFAULT_MODEL, timeout_s=60.0,
                 opener=None):
        self.api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self._opener = opener              # injectable for tests (callable(request, timeout)->bytes)

    def available(self) -> bool:
        return bool(self.api_key)

    def _request(self, path, method="GET", body=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        if self._opener is not None:                    # test seam: bypass the network
            return self._opener(req, self.timeout_s)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise VeniceError(f"HTTP {e.code} on {path}: {detail}")
        except urllib.error.URLError as e:
            raise VeniceError(f"connection error on {path}: {e.reason}")

    def models(self):
        """GET /models -- list model ids (also the cheapest auth check)."""
        if not self.api_key:
            raise VeniceError(f"no Venice API key (set {API_KEY_ENV})")
        payload = json.loads(self._request("/models").decode())
        return [m.get("id") for m in payload.get("data", []) if m.get("id")]

    def chat(self, messages, tools=None, temperature=0.2, model=None):
        """POST /chat/completions. messages = [{role, content}, ...]. Returns the first choice's message
        dict. Raises VeniceError on any failure (never returns partial/ambiguous)."""
        if not self.api_key:
            raise VeniceError(f"no Venice API key (set {API_KEY_ENV})")
        body = {"model": model or self.model, "messages": list(messages),
                "temperature": float(temperature)}
        if tools:
            body["tools"] = tools
        payload = json.loads(self._request("/chat/completions", method="POST", body=body).decode())
        choices = payload.get("choices") or []
        if not choices:
            raise VeniceError(f"no choices in response: {str(payload)[:200]}")
        return choices[0].get("message", {}) or {}
