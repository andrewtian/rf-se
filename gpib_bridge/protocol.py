"""Wire protocol for the se299 network GPIB bridge (shared by client + server).

One request or reply per line, newline-terminated:  ``<TOKEN> <base64-payload>\\n``
The base64 payload keeps arbitrary instrument bytes (commas, spaces, CR/LF inside a
601-point trace) from colliding with the line/space framing. An empty payload emits
just ``<TOKEN>\\n``.

Requests (client -> server):
  H <token>  authenticate (constant-time compared); required before any bus op when the
             server is started with a token. Omitted when the bus is open (loopback dev).
  A <addr>   bind the GPIB primary address (payload = ascii int)
  W <bytes>  write to the instrument (no response expected)  [arbitrated by the lease]
  Q <bytes>  query: write, then read the response            [arbitrated by the lease]
  T <ms>     set the read timeout (payload = ascii int milliseconds)
  L <s> <t>  acquire an exclusive lease on scope s ('BUS' or a pad) for TTL t seconds
  K <t>      keepalive: renew the caller's lease to TTL t seconds
  U          release the caller's lease
  R          report the live lease table (who holds what)
  X <id>     announce this session's client identity (role|host=..|pid=..|u=..) so the
             device-side session registry can attribute + group it. Best-effort: an old
             bridge replies '! unknown verb' and the client swallows it (X is then a no-op).
  S          report the live SESSION table (every connected client, joined with the lease
             table): one line per session -- controllers AND observers. Observer-readable.
  Z <sub>    health/heal endpoint (payload = ascii subcommand). 'Z ping' -> a cheap bounded
             liveness serial-poll (reply '= OK <statusbyte>' or a structured '!'); 'Z recover'
             -> escalating self-heal (ibclr -> ibonl off/on -> fresh handle -> probe) then a
             classified verdict '= <OK|DEVICE_SILENT|BUS_WEDGED|ADAPTER_WEDGED> <detail>'.
  C          close the session

New verbs (X/S/Z) reuse the same framing (any token letter is legal), so they are additive:
a client that never sends X/Z, or a bridge that predates S, is unaffected.

Replies (server -> client):
  +          ok, no data
  = <bytes>  ok, with data (a query response)
  ! <msg>    error (payload = ascii message)

Request and reply share one framing, so a single encode/decode pair serves both.
"""
from __future__ import annotations

import base64


def encode(token: str, payload: bytes = b"") -> bytes:
    """Frame one message: ``<token> <b64(payload)>\\n`` (or ``<token>\\n`` if empty)."""
    if payload:
        return f"{token} {base64.b64encode(payload).decode('ascii')}\n".encode("ascii")
    return f"{token}\n".encode("ascii")


def decode(line) -> tuple:
    """Parse one framed line (bytes or str) -> (token, payload_bytes). A blank line
    yields ("", b""). Tolerates a missing/empty payload."""
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("ascii")
    line = line.rstrip("\r\n")
    if not line:
        return ("", b"")
    parts = line.split(" ", 1)
    token = parts[0]
    payload = base64.b64decode(parts[1]) if len(parts) > 1 and parts[1] else b""
    return (token, payload)


# request and reply use identical framing
encode_request = encode
decode_request = decode
encode_reply = encode
decode_reply = decode
