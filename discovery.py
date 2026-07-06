"""se299 zero-dependency UDP discovery -- role instances find bridges on the LAN with no
hand-configured address.

A Beacon runs on each bridge host and answers a broadcast PROBE with a BeaconInfo describing
where its TCP bridge listens and which instruments sit behind it. discover() broadcasts one
probe and collects the replies for a short window. Pure stdlib (socket + json), so there is
NO zeroconf/mDNS dependency -- which, besides the extra package, would bind a fixed well-known
port and collide across parallel pytest-xdist workers. The discovery port is injectable and
the tests run on loopback with ephemeral ports.

Wire payloads:
  probe  (client -> beacons):  b"se299-discover?"
  reply  (beacon -> client):   JSON of BeaconInfo, service-tagged so foreign UDP is ignored
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import asdict, dataclass


DISCOVERY_PORT = 52999                       # default se299 discovery UDP port
PROBE = b"se299-discover?"
SERVICE = "se299-gpib-bridge"


@dataclass
class BeaconInfo:
    host: str                                # where the bridge TCP server is reachable
    port: int                                # the bridge TCP port
    board: int = 0
    instruments: tuple = ()                  # ({"pad":int,"model":str,"kind":"rx"|"tx"}, ...)
    token_required: bool = False
    version: str = "1"
    service: str = SERVICE


def encode_beacon(info: BeaconInfo) -> bytes:
    return json.dumps(asdict(info)).encode("ascii")


def decode_beacon(data: bytes):
    """Parse a beacon reply -> BeaconInfo, or None if it is not a se299 beacon (foreign UDP,
    malformed JSON, or the wrong service tag)."""
    try:
        d = json.loads(data.decode("ascii", "replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("service") != SERVICE:
        return None
    return BeaconInfo(host=d.get("host", ""), port=int(d.get("port", 0)),
                      board=int(d.get("board", 0)),
                      instruments=tuple(d.get("instruments") or ()),
                      token_required=bool(d.get("token_required", False)),
                      version=str(d.get("version", "1")), service=SERVICE)


class Beacon:
    """UDP responder: replies to a se299 PROBE with this bridge's BeaconInfo. Bind host ''
    (all interfaces) to catch LAN broadcasts; bind '127.0.0.1' + port 0 for a test."""

    def __init__(self, info: BeaconInfo, host: str = "", port: int = DISCOVERY_PORT):
        self.info = info
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, int(port)))
        self._stop = threading.Event()
        self._thread = None

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    def serve_one(self, timeout_s: float = 1.0) -> bool:
        """Answer a single probe. Returns True if one was answered, False on timeout."""
        self._sock.settimeout(timeout_s)
        try:
            data, addr = self._sock.recvfrom(2048)
        except (socket.timeout, OSError):
            return False
        if data.strip() == PROBE:
            self._sock.sendto(encode_beacon(self.info), addr)
            return True
        return False

    def serve_forever(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() == PROBE:
                try:
                    self._sock.sendto(encode_beacon(self.info), addr)
                except OSError:
                    pass

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def discover(port: int = DISCOVERY_PORT, timeout_s: float = 1.0,
             broadcast_host: str = "255.255.255.255") -> list:
    """Broadcast one probe and collect BeaconInfo replies for timeout_s, de-duplicated by
    (host, port). broadcast_host='127.0.0.1' targets a local beacon (loopback unicast)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    found = {}
    try:
        sock.sendto(PROBE, (broadcast_host, int(port)))
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _addr = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            info = decode_beacon(data)
            if info is not None:
                found[(info.host, info.port)] = info
    finally:
        sock.close()
    return list(found.values())
