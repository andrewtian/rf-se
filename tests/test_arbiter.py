"""Pure unit tests for the Arbiter: rx/tx internal ownership + one-level suspend/resume.
No Qt, no threads, no I/O -- the concurrency-critical handoff logic is deterministic here.

Run:  uv run python -m pytest rf-se/se299/tests/test_arbiter.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from instrument_hub import Arbiter


class _Eng:
    def __init__(self, name):
        self.name = name
        self.suspended = 0
        self.resumed = 0

    def suspend(self):
        self.suspended += 1

    def resume(self):
        self.resumed += 1


def test_acquire_free_instrument_sets_owner():
    a, sa = Arbiter(), _Eng("sa")
    a.acquire("rx", sa)
    assert a.owner("rx") is sa and sa.suspended == 0


def test_acquire_owned_suspends_prior_and_release_restores():
    a, sa, se = Arbiter(), _Eng("sa"), _Eng("se")
    a.acquire("rx", sa)
    a.acquire("rx", se)                       # SE preempts SA
    assert a.owner("rx") is se and sa.suspended == 1 and sa.resumed == 0
    a.release("rx", se)                        # SE done -> SA restored + resumed
    assert a.owner("rx") is sa and sa.resumed == 1


def test_reacquire_by_same_engine_is_noop():
    a, sa = Arbiter(), _Eng("sa")
    a.acquire("rx", sa)
    a.acquire("rx", sa)
    assert a.owner("rx") is sa and sa.suspended == 0


def test_release_by_non_owner_is_ignored():
    a, sa, se = Arbiter(), _Eng("sa"), _Eng("se")
    a.acquire("rx", sa)
    a.release("rx", se)                        # se does not own rx
    assert a.owner("rx") is sa


def test_rx_and_tx_are_independent():
    a, sa, sg = Arbiter(), _Eng("sa"), _Eng("sg")
    a.acquire("rx", sa)
    a.acquire("tx", sg)
    assert a.owner("rx") is sa and a.owner("tx") is sg and sa.suspended == 0
