"""se299 telemetry pub/sub -- the live control plane's data distribution.

The SE-coordinator instance owns the bus; observer instances (a dashboard, a logger) must NOT
issue their own bus ops against a leased instrument (that would perturb the measurement).
Instead the coordinator PUBLISHES what it measures -- the live SE figure per wall point, the
roster, the final summary -- and observers SUBSCRIBE and render. That keeps the R8 live SE
figure flowing to every instance while exactly one controller drives the hardware.

Wire: one JSON object per line over TCP, {"topic": str, "data": <json>}\\n. Pure stdlib.
Best-effort fan-out: a subscriber that errors is dropped, never blocking the measurement.
"""
from __future__ import annotations

import json
import socket
import threading
import time


class TelemetryHub:
    """Publisher: accepts subscriber connections and fans a published message out to all of
    them. Runs its accept loop in a daemon thread; publish() is safe from the measurement
    thread. Bind port 0 for an ephemeral port (tests)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, int(port)))
        self._srv.listen(8)
        self._subs = []                                # list of (sock, writable file)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @property
    def port(self) -> int:
        return self._srv.getsockname()[1]

    @property
    def host(self) -> str:
        """The interface the accept socket is actually bound to (e.g. '127.0.0.1' loopback-only,
        or '0.0.0.0' when reachable from other hosts). What the CLI should report."""
        return self._srv.getsockname()[0]

    def _accept_loop(self):
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._subs.append((conn, conn.makefile("wb")))

    def start(self):
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def wait_subscribers(self, n: int, timeout_s: float = 2.0) -> bool:
        """Block until at least `n` subscribers are connected (or timeout). Lets a test
        publish only once the observer is guaranteed to be listening."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.subscriber_count() >= n:
                return True
            time.sleep(0.01)
        return self.subscriber_count() >= n

    def publish(self, topic: str, data) -> None:
        """Send one message to every live subscriber; prune any that error (best-effort)."""
        line = json.dumps({"topic": topic, "data": data}).encode("ascii") + b"\n"
        with self._lock:
            live = []
            for sock, f in self._subs:
                try:
                    f.write(line)
                    f.flush()
                    live.append((sock, f))
                except OSError:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._subs = live

    def stop(self):
        self._stop.set()
        with self._lock:
            for sock, f in self._subs:
                for c in (f, sock):
                    try:
                        c.close()
                    except OSError:
                        pass
            self._subs = []
        try:
            self._srv.close()
        except OSError:
            pass


class TelemetrySubscriber:
    """Subscriber: connect to a hub and read published messages. Either poll recv_one(), or
    start(on_message) for a background reader thread."""

    def __init__(self, host: str, port: int, timeout_s: float = 5.0):
        self._sock = socket.create_connection((host, int(port)), timeout=timeout_s)
        self._f = self._sock.makefile("rb")
        self._stop = threading.Event()
        self._thread = None

    def recv_one(self, timeout_s: float = 1.0):
        """Read the next message -> {"topic","data"}, or None on timeout / closed stream."""
        self._sock.settimeout(timeout_s)
        try:
            line = self._f.readline()
        except (socket.timeout, OSError):
            return None
        if not line:
            return None
        try:
            msg = json.loads(line.decode("ascii", "replace"))
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None

    def start(self, on_message):
        """Background thread: call on_message(topic, data) for each message until closed.

        Reads RAW bytes with a persistent line buffer instead of makefile.readline(): a buffered
        readline under a 0.5s socket timeout DESYNCS when messages are spaced more than the timeout
        apart (every live wall point is seconds apart over the network), silently dropping every
        message after the first idle tick -- so an observer would see the roster and then go blind.
        recv() + a buffer split on b"\\n" keeps the partial line across idle ticks and never loses a
        message: a socket-timeout is just an empty window (keep waiting), real bytes accumulate."""
        def loop():
            self._sock.settimeout(0.5)
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    continue                       # idle window -> keep the buffer, keep waiting
                except OSError:
                    break
                if not chunk:
                    break                          # peer closed
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("ascii", "replace"))
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and "topic" in msg:
                        on_message(msg["topic"], msg.get("data"))
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        for c in (self._f, self._sock):
            try:
                c.close()
            except OSError:
                pass
