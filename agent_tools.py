"""Read-only tools the in-GUI bench agent can call to ground itself in the ACTUAL source + LIVE bench
state. Every tool is READ-ONLY and PATH-SCOPED to the se299 tree: the agent can inspect, never mutate,
never reach outside the project, never drive an instrument. `get_bench_state` is a callable the GUI
injects so the agent sees what the operator sees.

Design: pure functions + an OpenAI-compatible tool-schema list + a single `execute(name, args, ...)`
dispatcher, so the same tools work from the CLI, tests, and the GUI. No hardware, no new dependency.
"""
from __future__ import annotations

import os
import re

SE299_ROOT = os.path.dirname(os.path.abspath(__file__))
_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "measurements"}
_TEXT_MAX = 20000


def _safe_path(rel: str) -> str:
    """Resolve `rel` under SE299_ROOT and reject any escape (.. or an absolute path outside the tree)."""
    p = os.path.realpath(os.path.join(SE299_ROOT, rel or "."))
    root = os.path.realpath(SE299_ROOT)
    if p != root and not p.startswith(root + os.sep):
        raise ValueError(f"path escapes the se299 tree: {rel!r}")
    return p


def read_file(path: str, max_bytes: int = _TEXT_MAX) -> str:
    """Return up to max_bytes of a text file under the se299 tree (marks truncation)."""
    p = _safe_path(path)
    if not os.path.isfile(p):
        raise ValueError(f"not a file: {path}")
    with open(p, "r", errors="replace") as fh:
        data = fh.read(int(max_bytes) + 1)
    if len(data) > max_bytes:
        return data[:max_bytes] + "\n...[truncated]..."
    return data


def list_dir(path: str = ".") -> str:
    """List entries (dirs marked with a trailing /) of a directory under the se299 tree."""
    p = _safe_path(path)
    if not os.path.isdir(p):
        raise ValueError(f"not a directory: {path}")
    out = []
    for name in sorted(os.listdir(p)):
        out.append(name + ("/" if os.path.isdir(os.path.join(p, name)) else ""))
    return "\n".join(out)


def grep(pattern: str, path: str = ".", max_hits: int = 80) -> str:
    """Search text files under the se299 tree for a regex; return `relpath:lineno: line` hits (bounded)."""
    root = _safe_path(path)
    rx = re.compile(pattern)
    hits, base = [], os.path.realpath(SE299_ROOT)
    targets = [root] if os.path.isfile(root) else None
    if targets is None:
        targets = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if fn.endswith((".py", ".md", ".txt", ".json", ".cfg", ".toml", ".sh")):
                    targets.append(os.path.join(dirpath, fn))
    for fp in targets:
        try:
            with open(fp, "r", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(fp, base)}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= max_hits:
                            return "\n".join(hits) + f"\n...[stopped at {max_hits} hits]..."
        except (OSError, UnicodeError):
            continue
    return "\n".join(hits) if hits else "(no matches)"


# OpenAI-compatible tool schemas (Venice is OpenAI-compatible). get_bench_state is appended by the GUI
# only when a live bench-state provider is available.
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a source or doc file from the se299 project (read-only, path-scoped).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "path relative to the se299 dir, e.g. drivers.py"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files/dirs in a se299 project directory (read-only).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "dir relative to the se299 dir (default '.')"}}}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Regex-search se299 source + docs; returns path:line hits (read-only).",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
]

_BENCH_STATE_SCHEMA = {"type": "function", "function": {
    "name": "get_bench_state",
    "description": "Return the CURRENT live bench state the operator sees: mode, CF/span/RBW, RX/TX "
                   "status, health/ERR codes, last reading. Read-only.",
    "parameters": {"type": "object", "properties": {}}}}


def tool_schemas(bench_state_provider=None):
    """The tool schema list to hand the model. Includes get_bench_state only when the GUI provides a live
    state hook."""
    return list(TOOL_SCHEMAS) + ([_BENCH_STATE_SCHEMA] if bench_state_provider is not None else [])


def execute(name, args, bench_state_provider=None) -> str:
    """Dispatch a tool call. `bench_state_provider` (optional) is a zero-arg callable returning a string.
    Any tool error is returned as text (the model reads it and recovers) rather than raised."""
    args = args or {}
    try:
        if name == "read_file":
            return read_file(args["path"], int(args.get("max_bytes", _TEXT_MAX)))
        if name == "list_dir":
            return list_dir(args.get("path", "."))
        if name == "grep":
            return grep(args["pattern"], args.get("path", "."), int(args.get("max_hits", 80)))
        if name == "get_bench_state":
            if bench_state_provider is None:
                return "bench state unavailable (no live bench attached)"
            return str(bench_state_provider())
        return f"unknown tool: {name}"
    except Exception as e:                              # noqa: BLE001 -- tool errors are data for the model
        return f"tool error in {name}: {e}"
