"""Save / load a captured bench measurement: the on-screen trace plus the full instrument context, as
JSON (schema se299-measurement/1). Mirrors the calibration persistence pattern (loop.CAL_SCHEMA) so a
saved measurement is reproducible, comparable (load-to-overlay), and shareable. Pure logic -- no Qt, no
hardware -- so it is fully unit-testable and reusable by both the CLI and the GUI.

Timestamps are passed IN by the caller (kept out of here) so this module stays deterministic + testable.
"""
from __future__ import annotations

import json
import os

MEAS_SCHEMA = "se299-measurement/1"


def build_measurement(freqs_hz, levels_dbm, *, context=None, label="", timestamp=None) -> dict:
    """Assemble a measurement record from a captured trace + a context dict (CF/span/RBW/ref/detector,
    source f/power/OSB, band_trust, unit addresses, calibration in force, ...). The trace lengths must
    match. `timestamp` is a caller-supplied ISO string (or None)."""
    freqs = [float(f) for f in freqs_hz]
    levels = [float(x) for x in levels_dbm]
    if len(freqs) != len(levels):
        raise ValueError(f"trace length mismatch: {len(freqs)} freqs vs {len(levels)} levels")
    return {
        "schema": MEAS_SCHEMA,
        "label": str(label or ""),
        "timestamp": timestamp,
        "n_points": len(levels),
        "freqs_hz": freqs,
        "levels_dbm": levels,
        "context": dict(context or {}),
    }


def _validate(rec) -> None:
    if rec.get("schema") != MEAS_SCHEMA:
        raise ValueError(f"not a {MEAS_SCHEMA} record (schema={rec.get('schema')!r})")
    if len(rec.get("freqs_hz", [])) != len(rec.get("levels_dbm", [])):
        raise ValueError("trace length mismatch (freqs_hz vs levels_dbm)")


def save_measurement(path, record) -> str:
    """Atomically write a validated measurement record to JSON. Returns the path."""
    _validate(record)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh, indent=2)
    os.replace(tmp, path)                              # atomic: never leave a half-written file
    return path


def load_measurement(path):
    """Read + validate a measurement JSON. Returns (freqs_hz, levels_dbm, record) so a caller can overlay
    the trace directly."""
    with open(path) as fh:
        rec = json.load(fh)
    _validate(rec)
    return (rec["freqs_hz"], rec["levels_dbm"], rec)


def export_csv(path, freqs_hz, levels_dbm) -> str:
    """Write the trace as two-column CSV (freq_hz,level_dbm) for external plotting. Returns the path."""
    if len(freqs_hz) != len(levels_dbm):
        raise ValueError("trace length mismatch")
    with open(path, "w") as fh:
        fh.write("freq_hz,level_dbm\n")
        for f, x in zip(freqs_hz, levels_dbm):
            fh.write(f"{float(f):.1f},{float(x):.3f}\n")
    return path


def default_filename(freq_hz, timestamp_compact) -> str:
    """A stable auto-name for a saved measurement: <YYYYMMDD-HHMMSS>-<freq>.json (caller supplies the
    already-formatted compact timestamp so this stays deterministic)."""
    return f"{timestamp_compact}-{presets_label(freq_hz)}.json"


def presets_label(freq_hz) -> str:
    """Compact frequency tag for a filename (MHz/GHz, trailing zeros trimmed) -- matches presets._label."""
    f = float(freq_hz)
    if f >= 1e9:
        return f"{f / 1e9:.3f}".rstrip("0").rstrip(".") + "GHz"
    return f"{f / 1e6:.3f}".rstrip("0").rstrip(".") + "MHz"
