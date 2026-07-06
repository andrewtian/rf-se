"""Canonical per-device ABSOLUTE-state records + model-vs-device reconciliation for the bench.

Pure data + comparison logic -- no Qt, no hardware, no bus I/O. The drivers' `read_state()` methods
return these records (the device's ACTUAL state, queried from the instrument); the reconciliation
functions compare an ACTUAL snapshot against the model's DESIRED intent and report drift. An amplitude
drift (reference level or dB/division) is the one that must invalidate the binary trace calibration, so
the feed can never publish a numerically wrong amplitude.

See BENCH_STATE_CONSISTENCY_MODEL.md (this is the code form of section 3).
"""
from __future__ import annotations

from dataclasses import dataclass


# ---- absolute-state records (the device's ACTUAL state, device-queried) ---------------------

@dataclass(frozen=True)
class AnalyzerState:
    """8565EC ACTUAL state read back over the bus (CF?/SP?/RB?/VB?/RL?/AT?/DET?/LG?/AUNITS?/ST?)."""
    center_hz: float
    span_hz: float
    rbw_hz: float
    vbw_hz: float
    ref_level_dbm: float
    atten_db: float
    detector: str
    scale_db_div: float          # dB per division; 0.0 => linear amplitude mode
    aunits: str
    sweep_time_s: float


@dataclass(frozen=True)
class SourceState:
    """68367C ACTUAL state read back over the bus (OF1 register / OL1 register / OSB status byte)."""
    freq_hz: float
    level_dbm: float
    leveled: bool                # OSB bit2 (RF-unleveled) clear
    locked: bool                 # OSB bit3 (lock-error) clear
    syntax_ok: bool              # OSB bit5 (syntax-error) clear -- a rejected command clears this


@dataclass(frozen=True)
class Drift:
    """One field where ACTUAL disagrees with DESIRED."""
    field: str
    desired: object
    actual: object
    detail: str = ""

    def __str__(self) -> str:
        base = f"{self.field}: want {self.desired}, got {self.actual}"
        return f"{base} ({self.detail})" if self.detail else base


# ---- tolerances (see the schema table in the model doc) -------------------------------------

RL_TOL_DB = 0.05
LEVEL_TOL_DB = 0.1
SCALE_TOL = 0.01
_AMPLITUDE_FIELDS = ("ref_level_dbm", "scale_db_div")


def freq_tol_hz(f_hz: float) -> float:
    """Frequency comparison tolerance: 1 ppm, floored at 1 Hz."""
    return max(1.0, 1e-6 * abs(float(f_hz)))


# Detector reconciliation must be form-agnostic: the model may hold a FRIENDLY label ("peak", the GUI
# combo value) while the device reports the 8560 MNEMONIC ("POS"). Comparing them literally raised a
# spurious permanent "detector drift" in the live Point-Op pane. Canonicalize both sides. Mirrors
# drivers.normalize_detector (the authority for applying a detector); kept here as a pure map so
# device_state imports nothing (drivers imports device_state -- importing back would cycle).
_DETECTOR_CANON = {
    "PEAK": "POS", "POSITIVE": "POS", "POS": "POS",
    "SAMPLE": "SMP", "SMP": "SMP",
    "NEG-PEAK": "NEG", "NEGATIVE": "NEG", "NEG": "NEG",
    "NORMAL": "NRM", "NRM": "NRM",
}


def _norm_detector(d) -> str:
    key = str(d).strip().upper()
    return _DETECTOR_CANON.get(key, key)          # unknown -> compare literally (still catches a real change)


def reconcile_analyzer(actual: AnalyzerState, *, center_hz=None, span_hz=None, ref_level_dbm=None,
                       detector=None, rbw_hz=None, scale_db_div=None) -> list:
    """Compare an AnalyzerState (ACTUAL) against DESIRED intent; return a list of Drift ([] = consistent).

    Only the DESIRED fields you pass are checked. `rbw_hz=None` means AUTO -- the resolved RBW is never
    flagged (the instrument picks it). Amplitude drifts (ref_level_dbm/scale_db_div) are what
    `analyzer_amplitude_drift` keys on to invalidate the binary cal cache."""
    drifts = []
    if center_hz is not None and abs(actual.center_hz - center_hz) > freq_tol_hz(center_hz):
        drifts.append(Drift("center_hz", center_hz, actual.center_hz))
    if span_hz is not None and abs(actual.span_hz - span_hz) > freq_tol_hz(span_hz):
        drifts.append(Drift("span_hz", span_hz, actual.span_hz))
    if ref_level_dbm is not None and abs(actual.ref_level_dbm - ref_level_dbm) > RL_TOL_DB:
        drifts.append(Drift("ref_level_dbm", ref_level_dbm, actual.ref_level_dbm))
    if scale_db_div is not None and abs(actual.scale_db_div - scale_db_div) > SCALE_TOL:
        drifts.append(Drift("scale_db_div", scale_db_div, actual.scale_db_div))
    if detector is not None and _norm_detector(actual.detector) != _norm_detector(detector):
        drifts.append(Drift("detector", detector, actual.detector))
    if rbw_hz is not None and rbw_hz > 0 and abs(actual.rbw_hz - rbw_hz) > max(1.0, 0.05 * rbw_hz):
        drifts.append(Drift("rbw_hz", rbw_hz, actual.rbw_hz))
    return drifts


def reconcile_source(actual: SourceState, *, freq_hz=None, level_dbm=None) -> list:
    """Compare a SourceState (ACTUAL) against DESIRED intent; return a list of Drift ([] = consistent).
    A cleared syntax bit (the device rejected a command) is always reported as drift."""
    drifts = []
    if freq_hz is not None and abs(actual.freq_hz - freq_hz) > max(1e3, freq_tol_hz(freq_hz)):
        drifts.append(Drift("freq_hz", freq_hz, actual.freq_hz))
    if level_dbm is not None and abs(actual.level_dbm - level_dbm) > LEVEL_TOL_DB:
        drifts.append(Drift("level_dbm", level_dbm, actual.level_dbm))
    if not actual.syntax_ok:
        drifts.append(Drift("syntax_ok", True, False, "source rejected a command (OSB syntax bit)"))
    return drifts


def analyzer_amplitude_drift(drifts) -> bool:
    """True if any drift is on an amplitude field (ref_level_dbm / scale_db_div). When True the binary
    MU->dBm calibration is stale and MUST be cleared so the feed recalibrates instead of publishing a
    wrong amplitude."""
    return any(d.field in _AMPLITUDE_FIELDS for d in drifts)
