"""Frequency presets (presets.py): the curated jump-to-frequency set for JOINT operation of the pair.
Pure data -- no Qt, no hardware.

Run:  uv run python -m pytest rf-se/se299/tests/test_presets.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import presets


def test_default_presets_within_joint_range_and_sorted():
    ps = presets.default_presets()
    assert ps == sorted(ps, key=lambda p: p.freq_hz)                       # low -> high
    assert all(presets.JOINT_MIN_HZ <= p.freq_hz <= presets.JOINT_MAX_HZ for p in ps)
    assert len(ps) >= 8


def test_landmarks_include_the_key_operational_boundaries():
    fs = {p.freq_hz for p in presets.landmark_presets()}
    # source floor, preselector crossover, joint ceiling -- the boundaries an operator jumps to
    assert 10e6 in fs and 2.9e9 in fs and 40e9 in fs


def test_ism_checkpoints_present():
    fs = {p.freq_hz for p in presets.ism_presets()}
    assert 2.45e9 in fs and 5.8e9 in fs                                    # WiFi/ISM where isolation matters


def test_campaign_band_edges_data_driven_from_config():
    edges = presets.campaign_band_edges()
    assert edges == sorted(edges, key=lambda p: p.freq_hz)
    assert all(presets.JOINT_MIN_HZ <= p.freq_hz <= presets.JOINT_MAX_HZ for p in edges)
    assert any(abs(p.freq_hz - 10e6) < 1 for p in edges)                  # DC low band starts at 10 MHz


def test_labels_are_compact():
    assert presets._label(2.45e9) == "2.45G"
    assert presets._label(300e6) == "300M"
    assert presets._label(40e9) == "40G"


def test_no_preset_outside_the_joint_range():
    # default_presets() raises if any curated point is outside where BOTH units work -- guard the invariant
    for p in presets.default_presets():
        assert presets.JOINT_MIN_HZ <= p.freq_hz <= presets.JOINT_MAX_HZ


def test_step_sizes_are_the_three_hardware_decades():
    # 10 MHz source floor -> smallest fixed step; decades up to the named 1 GHz
    assert presets.STEP_SIZES_HZ == (10e6, 100e6, 1e9)


def test_step_deltas_are_symmetric_low_to_high():
    d = presets.step_deltas()
    assert d == [-1e9, -100e6, -10e6, 10e6, 100e6, 1e9]                    # -big..-small +small..+big
    assert [x for x in d if x > 0] == [-x for x in d if x < 0][::-1]       # symmetric +/-


def test_step_labels_are_signed_and_compact():
    assert presets.step_label(1e9) == "+1G"
    assert presets.step_label(-100e6) == "-100M"
    assert presets.step_label(10e6) == "+10M"
