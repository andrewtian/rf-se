"""Configuration for the IEEE-299 substitution SE automation (Tier-1, real instruments).

This is the experiment definition: instrument addresses, analyzer settings, the
per-band frequency plan + link-budget parameters (antenna gain / source power /
DANL from doc 159 sec 4.2b), the SE target (doc 29 sec 8.1.1 = FLAT >=100 dB
across 1 MHz-40 GHz), the IEEE 299 geometry, and the EA8 margin.

Frozen dataclasses; the resolved config is recorded into every run so the
campaign is reproducible. Swap the 26.5-40 GHz band's antenna_gain_dbi 33 -> 25
to model the standard-horn-plus-LNA case (then the RX LNA becomes required).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Instruments:
    # VISA resource strings. "sim" (default) routes to the built-in simulator:
    # no pyvisa, no hardware -- the pipeline runs end-to-end today. Real examples:
    #   source_addr:   "GPIB0::5::INSTR"   (68369 on the OUTSIDE bus, direct)
    #   analyzer_addr: "TCPIP0::pi-inside.local::gpib0,18::INSTR"  (856x via Pi+fiber)
    source_addr: str = "sim"
    analyzer_addr: str = "sim"
    # PC3 invariant: the analyzer bus MUST reach the inside instrument over FIBER
    # only -- NO copper through the shield (a GPIB/LAN copper run caps the SE).
    # Recorded into the run manifest; not physically enforceable from here.
    analyzer_link: str = "inside Pi USB-GPIB -> fiber media converter -> MW2 -> control Mac"


@dataclass(frozen=True)
class AnalyzerSettings:
    rbw_hz: float = 1000.0        # 1 kHz: fast sweeps; drop to 100/10 Hz to buy +10/+20 dB floor
    vbw_hz: float = 1000.0
    ref_level_dbm: float = 10.0   # set so the no-wall reference does NOT overload the input
    detector: str = "POS"         # positive-peak: never under-read a CW tone
    settle_s: float = 0.2         # dwell after retune before reading the marker
    attenuation_db: float = 0.0   # input attenuation; MUST match ref/wall (part of the ratio)
    aunits: str = "DBM"           # bus-read amplitude units; DBM is asserted before any trust
    # ADAPTIVE RBW LADDER (C3, doc 159 sec 4.2b "RBW is free gain"): acquire_reference measures
    # each point at rbw_hz first; if that point is EA8-limited (capability < band.target_se_db),
    # it re-measures the SAME point at the next entry here, narrowing until capability clears the
    # target or the ladder is exhausted -- recording the FINAL rbw_hz actually used in the
    # reference row. measure_wall then reads that point back at that SAME final rbw_hz (symmetric
    # per index: SE is a ratio, so an asymmetric RBW between the two passes is a fixed dB offset
    # masquerading as shielding). Only rungs STRICTLY NARROWER than rbw_hz are ever tried, so a
    # caller that overrides rbw_hz alone (without touching this field) gets a single-rung ladder --
    # no auto-narrowing, preserving prior fixed-RBW behavior unless the caller opts in.
    rbw_ladder_hz: tuple = (1000.0, 100.0, 10.0)   # default first entry == default rbw_hz


@dataclass(frozen=True)
class SourceSettings:
    """TX-source (68369A) synchronization. In the substitution campaign the coordinator is a
    SINGLE process issuing sequential BLOCKING bus transactions, so ordering (set source -> then
    read analyzer) is guaranteed even when the source and analyzer live on TWO different networked
    instances -- network latency delays a transaction but cannot reorder it. What is NOT free is
    letting the synthesizer SETTLE at the new CW frequency + level before the analyzer reads:
      settle_s  analog settle dwell after a retune / RF-on, before the analyzer measures.
      use_opc   ADDITIONALLY issue IEEE-488.2 *OPC? on top of the native OSB handshake. Default
                False: await_settled always does the native OSB (leveled+locked) completion read,
                which the 683xx answers; *OPC? is NOT answered by the bench 68367C (fw 2.35) and a
                *OPC? timeout poisons the transport socket, so it is opt-in for a genuine 683xxB/C."""
    settle_s: float = 0.05        # 683xx synthesizer settle after a CW retune (seconds)
    use_opc: bool = False         # native OSB handshake is the default; *OPC? opt-in (683xxB/C only)


@dataclass(frozen=True)
class BandPlan:
    """One measurement segment: a matched horn pair + its link-budget params (doc 159 4.2b).

    source_power_dbm / danl_dbm_per_hz are the segment's CONSERVATIVE (worst, i.e.
    top-of-band) values so the EA8 capability estimate does not over-promise.
    """
    name: str
    f_lo_hz: float
    f_hi_hz: float
    n_points: int
    antenna_gain_dbi: float       # per horn (TX == RX, matched identical pair)
    source_power_dbm: float       # 68369 leveled output (opt-2B), worst-in-segment
    danl_dbm_per_hz: float        # 856x EC DANL in this band
    target_se_db: float = 100.0   # doc 29 sec 8.1.1: FLAT >=100 dB, 1 MHz-40 GHz


# Default plan = doc 159 sec 4.2b, with the ELITE 33 dBi WR-28 pair (Multipath
# LHA-WR28-33) on the binding top band -> no RX LNA needed at any frequency.
DEFAULT_BANDS = (
    BandPlan("1-18 GHz broadband DRH",    1e9,    18e9,   9, 14.0, 12.0, -150.0),
    BandPlan("18-26.5 GHz WR-42 SGH",     18e9,   26.5e9, 4, 25.0, 11.0, -148.0),
    BandPlan("26.5-40 GHz WR-28 33 dBi",  26.5e9, 40e9,   7, 33.0,  3.0, -143.0),
)

# Alternative top band for the standard-horn case (the EA8 gate then FAILS at
# 40 GHz at 1 kHz RBW unless the RX LNA is added or RBW dropped to <=100 Hz).
WR28_STANDARD_25DBI = BandPlan("26.5-40 GHz WR-28 25 dBi", 26.5e9, 40e9, 7, 25.0, 3.0, -143.0)

# DC/low segment below the DRH horn band. The Anritsu 68367C source floors at 10 MHz (OI:
# 0.01-40 GHz); the 8565E reaches 9 kHz. A biconical/loop covers this band. Included so a
# DC-to-40-GHz sweep spans the full mission band (CLAUDE.md: two-way isolation DC-40 GHz).
DC_LOW_BAND = BandPlan("DC-1 GHz (10 MHz-1 GHz)", 10e6, 1e9, 6, 3.0, 12.0, -150.0)

# Full DC-to-40-GHz plan = the DC low band prepended to the DEFAULT 1-40 GHz bands. A coordinator
# sweep over this covers 10 MHz -> 40 GHz, the source retuned to follow the analyzer at every point.
DC_TO_40GHZ_BANDS = (DC_LOW_BAND,) + DEFAULT_BANDS


@dataclass(frozen=True)
class Geometry:
    separation_m: float = 0.6     # IEEE 299-2006: 0.3 m standoff each side -> ~0.6 m TX-RX
    standoff_m: float = 0.3
    note: str = "boresight aligned; both polarizations; SAME geometry for reference and wall"


@dataclass(frozen=True)
class Campaign:
    instruments: Instruments = field(default_factory=Instruments)
    analyzer: AnalyzerSettings = field(default_factory=AnalyzerSettings)
    source: SourceSettings = field(default_factory=SourceSettings)
    geometry: Geometry = field(default_factory=Geometry)
    bands: tuple = DEFAULT_BANDS
    margin_db: float = 10.0       # EA8 / PC6 headroom: floor must sit margin_db below target
    label: str = "demo"

    def settings_key(self) -> tuple:
        """Identity tuple asserted to MATCH between the reference and wall passes.

        SE is a ratio: any RBW / ref-level / geometry / band change between the
        two passes injects a fixed dB offset that masquerades as shielding.
        """
        a = self.analyzer
        # NOTE: the scalar prefix is the first 5 elements; cli.cmd_capture stores and
        # compares settings_key()[:5] for the legacy stepped-CW capture. New terms
        # (attenuation, units, the RBW ladder) are appended AFTER the bands tuple so that
        # slice is unchanged; the sweep-SE path compares the FULL key.
        #
        # NOTE (C3): a.rbw_hz here is the campaign's DEFAULT/first-rung RBW, not necessarily
        # what any given point ended up measured at -- acquire_reference's adaptive ladder can
        # narrow a point past this value. This key is only a coarse config-level pre-check (did
        # the two CLI invocations declare the same experiment); it is NOT the authoritative
        # ref/wall RBW match. The authoritative, PER-INDEX invariant (ref_row["rbw_hz"] ==
        # wall_row["rbw_hz"] for every i) is enforced by loop.measure_wall (which reads each
        # point back at its own reference row's final rbw_hz) and gated into
        # loop.summarize(...)["campaign_pass"] via "rbw_symmetric".
        return (a.rbw_hz, a.vbw_hz, a.ref_level_dbm, a.detector,
                self.geometry.separation_m,
                tuple((b.name, b.f_lo_hz, b.f_hi_hz, b.n_points,
                       b.antenna_gain_dbi) for b in self.bands),
                a.attenuation_db, a.aunits, a.rbw_ladder_hz)

    def frequencies(self):
        """Flat [(f_hz, BandPlan), ...] across all bands, log-spaced within each."""
        out = []
        for b in self.bands:
            if b.n_points <= 1:
                out.append((b.f_lo_hz, b))
                continue
            lo, hi = math.log10(b.f_lo_hz), math.log10(b.f_hi_hz)
            for i in range(b.n_points):
                out.append((10 ** (lo + (hi - lo) * i / (b.n_points - 1)), b))
        return out

    def band_for(self, f_hz):
        """The BandPlan whose range contains f_hz (nearest band if on no boundary)."""
        for b in self.bands:
            if b.f_lo_hz <= f_hz <= b.f_hi_hz:
                return b
        return min(self.bands,
                   key=lambda b: min(abs(f_hz - b.f_lo_hz), abs(f_hz - b.f_hi_hz)))

    def validate(self) -> list[str]:
        errs: list[str] = []
        for b in self.bands:
            if not (b.f_lo_hz < b.f_hi_hz):
                errs.append(f"band {b.name}: f_lo must be < f_hi")
            if b.n_points < 1:
                errs.append(f"band {b.name}: n_points must be >= 1")
        if self.margin_db < 0:
            errs.append("margin_db must be >= 0")
        return errs


@dataclass(frozen=True)
class SweepSettings:
    """One sweep-verb acquisition. mode discriminates the ONLY acceptance path
    (stepped-CW zero-span dwell) from the secondary swept-span screen. floor_detector
    is used for the RF-off floor read; the CW tone is always read positive-peak."""
    mode: str = "stepped"             # "stepped" (acceptance) | "swept" (screen, lower bound)
    span_lo_hz: float = 1e9
    span_hi_hz: float = 6e9
    n_points: int = 601               # fixed at 601 on the real 8565EC
    sweep_time_s: float = 0.0         # 0 => ST AUTO (auto-coupled to span/RBW)
    attenuation_db: float = 0.0
    aunits: str = "DBM"
    video_avg: int = 0                # 0 => off (OFF is required for an acceptance peak)
    max_hold: bool = False
    floor_detector: str = "SMP"       # sample for the floor; POS for the CW tone


@dataclass(frozen=True)
class CavitySettings:
    """Cavity Q-factor acquisition (both units inside; NOT IEEE-299). Low band:
    resolved-resonance linewidth Q = f0 / BW_n_db. High band: composite reverb
    Q = 16 pi^2 V <Pr/Pt> / lambda^3 (reference/ holloway-2008 + iec-61000-4-21)."""
    span_lo_hz: float = 9.98e9        # a narrow window AROUND a known resonance
    span_hi_hz: float = 10.02e9       # (sweep wide first to find it, then zoom here)
    n_points: int = 601
    n_db_down: float = 3.0
    video_avg: int = 16               # sample + average for the composite/floor read
    volume_m3: float = 32.0           # enclosure interior volume (composite-Q formula)


def default() -> Campaign:
    return Campaign()
