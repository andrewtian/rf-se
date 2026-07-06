"""Hardware-free tests for the 8565EC near-field-probe SWEEPER + automatic
connection lifecycle (discover -> open -> identify -> validate -> auto-reconnect).

Run:
    cd <repo root>
    uv run python -m pytest rf-se/se299/tests/test_probe_sweep.py -q

Everything here runs with NO pyvisa and NO instrument: fake discover/open
functions and the built-in simulator stand in for the bus and the 8565EC.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import connection as conn
import discover as disc
import drivers
import probe_sweep as ps


# ----------------------------------------------------------------- discovery

def test_parse_idn_488_2_quad():
    d = disc.parse_idn("HEWLETT-PACKARD,8565E,3702A00874,A.03.06")
    assert d["model"] == "8565E"
    assert d["serial"] == "3702A00874"


def test_parse_idn_bare_model_from_ID_query():
    # the 8565EC ID? query returns a bare model token, not a 4-field IDN
    d = disc.parse_idn("HP8565E")
    assert "8565" in d["model"]


def test_sim_inventory_contains_a_matching_8565ec():
    devs = disc.sim_inventory()
    sa = [d for d in devs if "8565" in d.model]
    assert sa, "sim inventory must include an 8565-class analyzer"
    assert sa[0].address == "sim"


def test_discover_visa_never_raises_without_pyvisa():
    # absent pyvisa -> empty list + note, never an exception
    out = disc.discover_visa()
    assert isinstance(out, list)


# ----------------------------------------------------------------- validity

def _dev(model="8565E", serial="X1", addr="sim", transport="sim"):
    return disc.DiscoveredDevice(transport=transport, address=addr, model=model,
                                 serial=serial, options=(), firmware="", raw_idn=model)


def test_validate_accepts_8565ec_covering_the_span():
    ok, reason = conn.validate_analyzer(_dev(), conn.DEFAULT_8565EC, (1e9, 6e9))
    assert ok, reason


def test_validate_rejects_wrong_model():
    ok, reason = conn.validate_analyzer(_dev(model="8566B"), conn.DEFAULT_8565EC, (1e9, 6e9))
    assert not ok
    assert "8566B" in reason or "model" in reason.lower()


def test_validate_rejects_span_beyond_instrument_range():
    # 8565EC tops out at 50 GHz; a 60 GHz span is out of range -> invalid
    ok, reason = conn.validate_analyzer(_dev(), conn.DEFAULT_8565EC, (1e9, 60e9))
    assert not ok
    assert "range" in reason.lower() or "ghz" in reason.lower()


# ----------------------------------------------------------------- sim sweep (near-field probe)

def test_sim_sweep_trace_returns_requested_length_and_finite_levels():
    sa = drivers.SimSpectrumAnalyzer(nf_model=drivers.demo_nearfield_spectrum())
    freqs, levels = sa.sweep_trace(1e9, 6e9, 101)
    assert len(freqs) == 101 and len(levels) == 101
    assert all(math.isfinite(v) for v in levels)
    assert freqs[0] == pytest.approx(1e9) and freqs[-1] == pytest.approx(6e9)


def test_sim_sweep_finds_the_nearfield_leak_peak():
    # demo near-field spectrum puts a hot seam leak at 2.45 GHz; over 1-6 GHz it
    # must be the strongest bin (this is what the probe is meant to localize).
    sa = drivers.SimSpectrumAnalyzer(nf_model=drivers.demo_nearfield_spectrum())
    freqs, levels = sa.sweep_trace(1e9, 6e9, 601)
    hot_i = max(range(len(levels)), key=lambda i: levels[i])
    assert freqs[hot_i] == pytest.approx(2.45e9, abs=100e6)


# ----------------------------------------------------------------- lifecycle

def _sim_link(span=(1e9, 6e9), retries=3):
    """An AnalyzerLink wired to the simulator (no bus, no pyvisa)."""
    return conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=span,
        discover_fn=disc.sim_inventory,
        open_fn=lambda dev: drivers.SimSpectrumAnalyzer(
            nf_model=drivers.demo_nearfield_spectrum()),
        retries=retries)


def test_lifecycle_connect_to_ready_reports_detected_and_valid():
    link = _sim_link()
    st = link.connect()
    assert st.state == "READY"
    assert st.detected is True and st.valid is True
    assert "8565" in st.model


def test_lifecycle_absent_when_nothing_on_the_bus():
    link = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9),
        discover_fn=lambda: [], open_fn=lambda dev: None, retries=1)
    st = link.connect()
    assert st.state == "ABSENT"
    assert st.detected is False and st.valid is False
    assert link.ensure() is False


def test_lifecycle_invalid_when_wrong_model_present():
    wrong = disc.DiscoveredDevice("visa", "GPIB0::18::INSTR", "8566B", "S9", (), "", "8566B")
    link = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9),
        discover_fn=lambda: [wrong], open_fn=lambda dev: None, retries=1)
    st = link.connect()
    assert st.state == "INVALID"
    assert st.detected is True and st.valid is False


def test_read_sweep_requires_ready():
    link = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9),
        discover_fn=lambda: [], open_fn=lambda dev: None, retries=1)
    link.connect()  # ABSENT
    with pytest.raises(conn.LinkNotReady):
        link.read_sweep(101)


def test_read_sweep_returns_a_frame_when_ready():
    link = _sim_link()
    link.connect()
    freqs, levels = link.read_sweep(201)
    assert len(freqs) == 201 == len(levels)


# ----------------------------------------------------------------- auto-reconnect


class _Flaky:
    """A sim 8565EC analyzer whose FIRST sweep raises (a dropped link), to force
    the lifecycle to re-discover + re-open transparently."""

    def __init__(self, state):
        self.state = state
        self._nf = drivers.demo_nearfield_spectrum()

    def idn(self):
        return "SIM,8565E-flaky,0,sim"

    def sweep_trace(self, lo, hi, n, settle_s=0.0):
        if self.state["drop_once"]:
            self.state["drop_once"] = False
            raise IOError("simulated GPIB link drop")
        sa = drivers.SimSpectrumAnalyzer(nf_model=self._nf)
        return sa.sweep_trace(lo, hi, n)

    def close(self):
        pass


def test_auto_reconnect_after_a_dropped_link():
    state = {"drop_once": True}
    link = conn.AnalyzerLink(
        expected=conn.DEFAULT_8565EC, span=(1e9, 6e9),
        discover_fn=disc.sim_inventory,
        open_fn=lambda dev: _Flaky(state), retries=3)
    sweeper = ps.ProbeSweeper(link, span=(1e9, 6e9), n_points=101, settle_s=0.0)
    frames = sweeper.run(sweeps=3)
    # the drop costs one sweep; the link auto-reconnects and the rest succeed
    assert len(frames) >= 2
    assert sweeper.reconnects >= 1


# ----------------------------------------------------------------- probe sweeper end to end

def test_probe_sweeper_run_against_sim_localizes_the_leak():
    link = _sim_link()
    sweeper = ps.ProbeSweeper(link, span=(1e9, 6e9), n_points=601, settle_s=0.0)
    frames = sweeper.run(sweeps=3)
    assert len(frames) == 3
    last = frames[-1]
    assert last.hot_freq_hz == pytest.approx(2.45e9, abs=100e6)
    assert last.status.valid is True


def test_render_frame_is_ascii_and_shows_status():
    link = _sim_link()
    sweeper = ps.ProbeSweeper(link, span=(1e9, 6e9), n_points=101, settle_s=0.0)
    frame = sweeper.sweep_once()
    text = ps.render_frame(frame, width=40)
    assert text.isascii()
    assert "8565" in text
    assert "HOT" in text.upper()
