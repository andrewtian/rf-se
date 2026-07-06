"""Commonly-used frequency presets for the se299 bench: one-click JOINT retune points for the 8565EC
analyzer + 68367C source operated TOGETHER. A preset is a joint retune (source CW at f + analyzer CF at
f + preselector peak above 2.9 GHz), not an analyzer-only jump -- a substitution SE read needs both units
at the same f. Bounded to where BOTH units work (10 MHz source floor -> 40 GHz source ceiling).

The set is curated from the two units' operational landmarks + real-world EMI/ISM checkpoints, and the SE
campaign band plan is exposed data-driven from config so it tracks the campaign, not a hardcoded list.
Pure data -- no Qt, no hardware -- so it is unit-testable and reusable by CLI + GUI.
"""
from __future__ import annotations

from dataclasses import dataclass

import config

# Joint operating range: the 68367C source floors at 10 MHz and ceilings at 40 GHz (the 8565EC reaches
# wider -- 9 kHz to 50 GHz -- but the source gates the pair). Every preset must fall in this window.
JOINT_MIN_HZ = 10e6
JOINT_MAX_HZ = 40e9


@dataclass(frozen=True)
class FreqPreset:
    label: str        # short button text (e.g. "2.45G")
    freq_hz: float
    note: str         # why this frequency matters (button tooltip)


# Fixed relative-nudge steps for the -f / +f controls, chosen for OUR hardware: the 68367C source floors
# at 10 MHz (so 10 MHz is the smallest meaningful FIXED step -- finer trim is what the frequency-
# proportional arrow ladder is for) and ceilings at 40 GHz. The three decades 10 MHz / 100 MHz / 1 GHz
# span the practical nudge range: fine trim -> walk within an SE band -> coarse traversal (the named
# +/-1 GHz). Each is offered as -x and +x. (+/-10 GHz is only 4 steps across the band; the presets +
# proportional ladder already cover big jumps, so the fixed set stays the three decades.)
STEP_SIZES_HZ = (10e6, 100e6, 1e9)


def step_deltas():
    """The signed nudge deltas in button order: -1G, -100M, -10M, +10M, +100M, +1G."""
    return [-s for s in reversed(STEP_SIZES_HZ)] + [s for s in STEP_SIZES_HZ]


def step_label(delta_hz: float) -> str:
    """Signed compact label for a +f/-f button, e.g. '+1G' / '-100M'."""
    return ("+" if delta_hz >= 0 else "-") + _label(abs(delta_hz))


def _label(f_hz: float) -> str:
    """Compact button label: MHz below 1 GHz, GHz above, trailing zeros trimmed."""
    if f_hz >= 1e9:
        return f"{f_hz / 1e9:.3f}".rstrip("0").rstrip(".") + "G"
    return f"{f_hz / 1e6:.3f}".rstrip("0").rstrip(".") + "M"


# Operational landmarks of THIS pair -- the boundaries an operator jumps to constantly.
_LANDMARKS = (
    FreqPreset("10M", 10e6, "68367C source floor -- lowest joint-testable point"),
    FreqPreset("300M", 300e6, "8565EC CAL-OUT amplitude reference (-10 dBm); validation start"),
    FreqPreset("2.9G", 2.9e9, "preselector crossover -- below: direct read; above: the YIG must be peaked"),
    FreqPreset("18G", 18e9, "1-18 GHz horn RX upper edge"),
    FreqPreset("40G", 40e9, "joint ceiling -- 68367C tops out here"),
)

# Real-world EMI / ISM checkpoints -- where the enclosure's DC-40 GHz two-way isolation actually matters.
_ISM = (
    FreqPreset("850M", 850e6, "cellular (LTE low band)"),
    FreqPreset("1.9G", 1.9e9, "cellular / PCS"),
    FreqPreset("2.45G", 2.45e9, "WiFi / Bluetooth / ISM"),
    FreqPreset("5.8G", 5.8e9, "WiFi / ISM"),
)


def _in_range(f_hz: float) -> bool:
    return JOINT_MIN_HZ <= f_hz <= JOINT_MAX_HZ


def landmark_presets() -> list:
    """Instrument-boundary presets, low -> high. One GUI button row."""
    return sorted(_LANDMARKS, key=lambda p: p.freq_hz)


def ism_presets() -> list:
    """Real-world EMI/ISM checkpoint presets, low -> high. One GUI button row."""
    return sorted(_ISM, key=lambda p: p.freq_hz)


def campaign_band_edges(bands=None) -> list:
    """Data-driven presets from the SE campaign band plan (config): the low edge of each band, deduped +
    sorted + clamped to the joint range, so the set tracks the campaign definition rather than a
    hardcoded list. Exposed for a 'campaign ladder' dropdown."""
    bands = config.DC_TO_40GHZ_BANDS if bands is None else bands
    seen, out = set(), []
    for b in bands:
        f = float(b.f_lo_hz)
        key = round(f)
        if _in_range(f) and key not in seen:
            seen.add(key)
            out.append(FreqPreset(_label(f), f, f"SE band start: {b.name}"))
    return sorted(out, key=lambda p: p.freq_hz)


def default_presets() -> list:
    """The curated GUI button set: landmarks + ISM checkpoints, low -> high, all within the joint range.
    Small on purpose (~9 buttons); the full campaign ladder is campaign_band_edges()."""
    ps = sorted(list(_LANDMARKS) + list(_ISM), key=lambda p: p.freq_hz)
    bad = [p for p in ps if not _in_range(p.freq_hz)]
    if bad:                                            # a preset outside where both units work is a bug
        raise ValueError(f"preset(s) outside joint range {JOINT_MIN_HZ}-{JOINT_MAX_HZ} Hz: {bad}")
    return ps
