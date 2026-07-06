"""Hardware-free tests for the analyzer health gate (Task 2): the Coordinator HALTS a measurement
when the 8565EC is in the reference/LO wedge (halted acquisition + reference-unlock codes) instead of
letting it emit stale numbers off a frozen trace. Complements Task 1's per-read fresh-sweep guard --
Task 1 raises on the frozen READ; this gate raises BEFORE a measurement even starts, and re-checks
periodically through a long wall pass.

A wedge is injected on the sim analyzer by overriding query_errors (reference-unlock codes) and/or
_sweep_is_live (frozen trace) -- the only two inputs analyzer_health() reads. The sim's own
measure_peak does not call either, so injection does not perturb the simulated measurement.

Run:  QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest \
        rf-se/se299/tests/test_analyzer_health_gate.py -q -n0
"""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfg_mod
import control_plane
import coordinator
import drivers


def _coord(target_se_db=55.0):
    """A ready sim coordinator over the DEFAULT bands with a constant known SE model, so a full
    run_campaign flows real wall points. Returns (coord, cp)."""
    bands = tuple(dataclasses.replace(b, target_se_db=target_se_db) for b in cfg_mod.DEFAULT_BANDS)
    cfg = cfg_mod.Campaign(bands=bands)
    cp = control_plane.simulated(cfg)
    cp.bench.se_model = lambda f_hz: 70.0            # constant, comfortably above the 55 dB target
    coord = cp.make_coordinator()
    coord.ensure_ready()
    return coord, cp


def _wedge(coord, ref_codes=(335,), sweeping=False):
    """Inject the reference/LO wedge onto the coordinator's analyzer."""
    ana = coord.analyzer
    ana.query_errors = lambda: list(ref_codes)
    ana._sweep_is_live = lambda: sweeping


# ------------------------------------------------------------------- healthy passes through

def test_analyzer_health_healthy_on_untouched_sim():
    coord, _ = _coord()
    h = coord.analyzer_health()
    assert h == {"healthy": True, "ref_codes": [], "sweeping": True}


def test_reference_unlock_codes_are_filtered_to_the_canonical_set():
    # a NON-reference hardware code (e.g. 750) must NOT trip the gate; only 333/335/337/499 do.
    coord, _ = _coord()
    coord.analyzer.query_errors = lambda: [750]              # some other hw code, not a ref unlock
    h = coord.analyzer_health()
    assert h["healthy"] is True and h["ref_codes"] == []
    coord.analyzer.query_errors = lambda: [750, 337]         # now a real reference unlock is present
    h = coord.analyzer_health()
    assert h["healthy"] is False and h["ref_codes"] == [337]


# ------------------------------------------------------------------- gate raises before measuring

def test_check_path_raises_when_reference_unlocked():
    coord, cp = _coord()
    _wedge(coord, ref_codes=(333, 337), sweeping=True)       # codes present even though sweep 'moves'
    with pytest.raises(coordinator.AnalyzerWedged) as ei:
        coord.check_path([1e9], bench=cp.bench)
    assert sorted(ei.value.ref_codes) == [333, 337]


def test_acquire_reference_raises_when_sweep_is_frozen():
    coord, cp = _coord()
    _wedge(coord, ref_codes=(), sweeping=False)              # no codes yet, but the trace is frozen
    with pytest.raises(coordinator.AnalyzerWedged) as ei:
        coord.acquire_reference(bench=cp.bench)
    assert ei.value.sweeping is False and ei.value.ref_codes == []


def test_take_control_raises_before_leasing_when_wedged():
    coord, _ = _coord()
    _wedge(coord)
    with pytest.raises(coordinator.AnalyzerWedged):
        coord.take_control()
    # the wedge was caught BEFORE any lease was taken -> neither lease is held
    assert not coord._rx_lease.held() and not coord._tx_lease.held()
    # clearing the wedge lets control be taken normally (proves the gate, not a broken coordinator)
    coord.analyzer.query_errors = lambda: []
    coord.analyzer._sweep_is_live = lambda: True
    coord.take_control()
    coord.release_control()


def test_run_campaign_blocks_at_the_start_when_already_wedged():
    coord, cp = _coord()
    _wedge(coord)
    streamed = []
    with pytest.raises(coordinator.AnalyzerWedged):
        coord.run_campaign(bench=cp.bench, on_se_update=lambda fig, row: streamed.append(row))
    assert streamed == []                                    # nothing measured -- halted at take_control


# ------------------------------------------------------------------- periodic mid-run re-check

def test_run_campaign_periodic_check_stops_early_on_midrun_wedge():
    # healthy at the start (reference pass + first wall points flow), then the analyzer wedges
    # mid-wall-pass; health_every=1 catches it on the next point and raises with the partial streamed.
    coord, cp = _coord()
    total = len(cfg_mod.Campaign(bands=coord.cfg.bands).frequencies())
    assert total >= 3                                        # enough points for a meaningful 'early'
    streamed = []

    def on_se(fig, row):
        streamed.append(row)
        if len(streamed) == 2:                              # wedge after the 2nd wall point
            coord.analyzer.query_errors = lambda: [499]
            coord.analyzer._sweep_is_live = lambda: False

    with pytest.raises(coordinator.AnalyzerWedged) as ei:
        coord.run_campaign(bench=cp.bench, on_se_update=on_se, health_every=1)
    assert ei.value.ref_codes == [499]
    assert 0 < len(streamed) < total                        # streamed a PARTIAL, stopped early


def test_run_campaign_health_every_zero_is_off_and_completes():
    # the default (health_every=0) does not add mid-run checks: a healthy campaign completes normally.
    coord, cp = _coord()
    result = coord.run_campaign(bench=cp.bench)
    assert "summary" in result and "se_figure" in result
    assert result["summary"]["campaign_pass"] is True


# ------------------------------------------------------------------- canonical code set

def test_reference_unlock_codes_are_the_canonical_four():
    assert drivers.REFERENCE_UNLOCK_CODES == frozenset({333, 335, 337, 499})
