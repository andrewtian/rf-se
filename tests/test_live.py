"""Hardware-free PROOF that the live 8565EC GUI displays the analyzer's LIVE trace.

Everything here runs the PRODUCTION path with NO instrument and NO pyvisa:

    LiveSpectrumModel <- ProbeSweeper <- AnalyzerLink <- NetworkTransport
                      <- gpib bridge (real TCP) <- FakeBackend(signal="moving")

The bridge is spun in a daemon thread with signal="moving" (a real live signal whose
peak marches across the span each sweep), addressed as net:127.0.0.1:{port}:18 -- the
exact M1 network-GPIB-bridge path. These are the model + live-signal + auto-reconnect
proofs; they need no Qt at all. (The GUI-render surface is now sa_gui.SpectrumAnalyzerPanel,
covered by test_sa_panel.py -- see that file for the headless PySide6 view tests.)

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_live.py -q
"""
from __future__ import annotations

import os
import socket
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # harmless if unused; kept for import safety

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import live
from gpib_bridge import ni_gpib_server


def _start_moving_server(token=None):
    """A moving-signal fake bridge on an ephemeral port; returns the port. Daemon
    thread serves sequential connections until the test process exits."""
    srv = ni_gpib_server.listen("127.0.0.1", 0)
    port = srv.getsockname()[1]
    threading.Thread(target=ni_gpib_server.serve, args=(srv,),
                     kwargs={"fake": True, "signal": "moving", "token": token},
                     daemon=True).start()
    return port


def _net_addr(port):
    return f"net:127.0.0.1:{port}:18"


# ----------------------------------------------------------------- model over the bridge

def test_live_model_produces_frames_over_bridge():
    port = _start_moving_server()
    model, simulated = live.build_live(_net_addr(port), span_ghz=(1.0, 6.0))
    assert simulated is False                            # a net: bridge is real, not sim
    for _ in range(8):
        frame = model.step()
        assert frame is not None
        assert len(frame.freqs) == 601 and len(frame.levels) == 601
        assert frame.status.valid is True
    assert model.frame_count == 8
    assert model.latest is not None


# ----------------------------------------------------------------- THE live proof

def test_signal_is_actually_live():
    """Core proof: the displayed trace is a LIVE signal, not a static one -- the
    level arrays change AND the peak actually moves across the span AND it stands
    above the noise floor."""
    port = _start_moving_server()
    model, _ = live.build_live(_net_addr(port))
    frames = [model.step() for _ in range(12)]
    assert all(fr is not None for fr in frames)
    levels = [tuple(fr.levels) for fr in frames]
    hots = [fr.hot_freq_hz for fr in frames]
    # 1) the trace CHANGES sweep to sweep (not a frozen picture)
    assert len(set(levels)) > 1
    # 2) the PEAK MOVES: the hot bin takes >= 3 distinct frequencies
    assert len({round(h, 1) for h in hots}) >= 3
    # 3) the peak stands clearly above the ~ -90 dBm floor
    assert all(fr.hot_level_dbm > -60 for fr in frames)


# ----------------------------------------------------------------- auto-reconnect

def test_live_autoreconnects_after_drop():
    """A dropped link recovers and frames resume (the M1 path over the bridge)."""
    port = _start_moving_server()
    model, _ = live.build_live(_net_addr(port))
    first = model.step()                                 # initial connect + one frame
    assert first is not None
    link = model.sweeper.link
    # forcibly break the live transport socket (a mid-run GPIB link drop)
    link.analyzer.t._sock.shutdown(socket.SHUT_RDWR)
    recovered = None
    for _ in range(6):                                   # the drop costs a sweep, then reconnect
        fr = model.step()
        if fr is not None and fr.status.valid:
            recovered = fr
            break
    assert recovered is not None                         # frames resumed after the drop
    assert link.reconnects >= 1                           # the link actually reconnected
