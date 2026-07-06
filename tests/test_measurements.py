"""Saved-measurement persistence (measurements.py): build / save / load / CSV. Pure logic -- no Qt, no
hardware.

Run:  uv run python -m pytest rf-se/se299/tests/test_measurements.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import measurements


def test_build_and_roundtrip(tmp_path):
    freqs = [2.4e9, 2.45e9, 2.5e9]
    levels = [-90.0, -14.0, -88.0]
    rec = measurements.build_measurement(freqs, levels, context={"center_hz": 2.45e9},
                                         label="t", timestamp="2026-07-05T00:00:00")
    assert rec["schema"] == measurements.MEAS_SCHEMA and rec["n_points"] == 3
    p = str(tmp_path / "m.json")
    measurements.save_measurement(p, rec)
    f2, l2, r2 = measurements.load_measurement(p)
    assert f2 == freqs and l2 == levels
    assert r2["context"]["center_hz"] == 2.45e9 and r2["label"] == "t"


def test_build_rejects_length_mismatch():
    with pytest.raises(ValueError):
        measurements.build_measurement([1, 2, 3], [1, 2])


def test_save_validates_schema(tmp_path):
    with pytest.raises(ValueError):
        measurements.save_measurement(str(tmp_path / "x.json"), {"schema": "nope", "freqs_hz": [], "levels_dbm": []})


def test_load_rejects_wrong_schema(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema": "other/9", "freqs_hz": [], "levels_dbm": []}))
    with pytest.raises(ValueError):
        measurements.load_measurement(str(p))


def test_save_is_atomic_no_tmp_left(tmp_path):
    p = str(tmp_path / "m.json")
    rec = measurements.build_measurement([1e9, 2e9], [-10.0, -20.0])
    measurements.save_measurement(p, rec)
    assert os.path.exists(p) and not os.path.exists(p + ".tmp")           # atomic replace, no leftover tmp


def test_export_csv(tmp_path):
    p = tmp_path / "t.csv"
    measurements.export_csv(str(p), [1e9, 2e9], [-10.0, -20.0])
    lines = p.read_text().strip().splitlines()
    assert lines[0] == "freq_hz,level_dbm"
    assert lines[1].startswith("1000000000") and lines[1].endswith("-10.000")


def test_default_filename():
    assert measurements.default_filename(2.45e9, "20260705-120000") == "20260705-120000-2.45GHz.json"
    assert measurements.default_filename(300e6, "20260705-120000") == "20260705-120000-300MHz.json"
