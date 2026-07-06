"""Per-process client identity for the se299 network layer.

Every client process (coordinator, dashboard, se-gui, walkaround, the `devices` query,
...) gets ONE identity, computed once at import, and sends it to each bridge it connects
to via the `X` verb. The device-side session registry (and the `devices` listing built
from it) uses that identity to attribute every bridge session to a client and to GROUP a
client's rx+tx sessions back together.

Format (compact, space-free, pipe-delimited ASCII):

    role|host=<hostname>|pid=<pid>|u=<uuid8>

- It has NO spaces so it survives the space-framed `S` report line, and uses `|` with
  `key=value` fields so a reader can split it deterministically.
- `u=<uuid8>` is a single uuid4().hex[:8] chosen ONCE per process. It is the STABLE
  GROUPING KEY: the same `u=` is embedded in the id sent to rx AND tx, so grouping by
  `u=` reunites the two sessions a single client opens across two bridges.
- `role` names what the client is doing (coordinator / dashboard / se-gui / walkaround /
  checkpath / calibrate / wall / validate / devices / operator). `set_client_role` stamps
  it; it is carried in the id's first field.
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import Optional


def _sanitize(s: str) -> str:
    """Keep an id field space-free and pipe-safe: drop whitespace, pipes, and '='."""
    return "".join(ch for ch in str(s) if ch not in " \t\r\n|=")


_U = uuid.uuid4().hex[:8]                     # one grouping key per process (chosen at import)
_HOST = _sanitize(socket.gethostname()) or "unknown"
_PID = os.getpid()
_role = "operator"                            # default role until a verb stamps its own


def set_client_role(role: Optional[str]) -> str:
    """Stamp this process's client role (coordinator / dashboard / se-gui / ...). Returns
    the resulting full client id. Called once per verb, near its entry point."""
    global _role
    r = _sanitize(role) if role else ""
    _role = r or "operator"
    return client_id()


def client_id(role: Optional[str] = None) -> str:
    """This process's client id: ``role|host=<hostname>|pid=<pid>|u=<uuid8>``. If `role` is
    given, stamp it first (convenience so a call site is one line)."""
    if role is not None:
        set_client_role(role)
    return f"{_role}|host={_HOST}|pid={_PID}|u={_U}"


def parse_client_id(cid: str) -> dict:
    """Split a client id into {role, host, pid, u} (missing fields -> '')."""
    parts = str(cid).split("|")
    out = {"role": parts[0] if parts else "", "host": "", "pid": "", "u": ""}
    for field in parts[1:]:
        if "=" in field:
            k, v = field.split("=", 1)
            if k in out:
                out[k] = v
    return out


def group_key(cid: str) -> str:
    """The cross-bridge grouping key for a client id: its ``u=<uuid8>`` if present, else the
    whole id (so an id without a u= groups only with itself)."""
    for field in str(cid).split("|"):
        if field.startswith("u="):
            return field[2:]
    return str(cid)


def role_of(cid: str) -> str:
    """The role field (first `|`-segment) of a client id, or '' if absent."""
    return str(cid).split("|", 1)[0] if cid else ""
