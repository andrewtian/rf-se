"""Link-budget math for the no-cable IEEE-299 substitution SE measurement.

Pure functions: no numpy, no instruments. These reproduce the doc 159
sec 4.2 / 4.2a / 4.2b budget so the simulator and the EA8 capability gate
share one source of truth (and so a unit test can pin the doc's numbers).

Convention: powers in dBm, gains in dBi (per horn), freq in Hz, distance in m.
"""
from __future__ import annotations

import math

C = 299_792_458.0  # speed of light, m/s


def fspl_db(f_hz: float, d_m: float) -> float:
    """Free-space path loss (dB) at frequency f_hz over separation d_m.

    FSPL = 20 log10(4 pi d f / c). At 40 GHz / 0.6 m this is ~60.0 dB
    (the IEEE 299-2006 geometry: 0.3 m standoff each side, doc 159 sec 4.2).
    """
    return 20.0 * math.log10(4.0 * math.pi * d_m * f_hz / C)


def reference_amp_dbm(p_src_dbm: float, g_tx_dbi: float, g_rx_dbi: float,
                      f_hz: float, d_m: float) -> float:
    """Power at the analyzer with the horns face-to-face, NO wall (the 0 dB reference).

    P_ref = P_src + G_tx + G_rx - FSPL. Antenna gain enters at +1 dB per dBi
    per horn (so +2 dB of measurable SE per +1 dBi each end) -- the lever that
    lets 33 dBi horns substitute for the RX LNA (doc 159 sec 4.2a).
    """
    return p_src_dbm + g_tx_dbi + g_rx_dbi - fspl_db(f_hz, d_m)


def noise_floor_dbm(danl_dbm_per_hz: float, rbw_hz: float) -> float:
    """Displayed average noise level integrated over the resolution bandwidth.

    floor = DANL + 10 log10(RBW). Narrowing RBW one decade buys +10 dB of
    measurable SE at no hardware cost -- the free lever (doc 159 sec 4.2b).
    """
    return danl_dbm_per_hz + 10.0 * math.log10(rbw_hz)


def se_capability_db(p_src_dbm: float, g_tx_dbi: float, g_rx_dbi: float,
                     f_hz: float, d_m: float, danl_dbm_per_hz: float,
                     rbw_hz: float, margin_db: float) -> float:
    """Deepest SE the setup can MEASURE at f with the stated margin.

    = reference_amp - noise_floor - margin. This is the EA8 / PC6 gate quantity:
    the campaign can verify a target T at frequency f only if se_capability >= T.

    Sanity (doc 159 sec 4.2b, +3 dBm @ 40 GHz, 0.6 m, DANL -143, RBW 1 kHz, margin 10):
      33 dBi horns -> ~112 dB  (clears 100 dB target, no LNA)
      25 dBi horns -> ~96 dB   (short -> needs the LNA or <=100 Hz RBW)
    """
    ref = reference_amp_dbm(p_src_dbm, g_tx_dbi, g_rx_dbi, f_hz, d_m)
    floor = noise_floor_dbm(danl_dbm_per_hz, rbw_hz)
    return ref - floor - margin_db


def db_power_sum(a_dbm: float, b_dbm: float) -> float:
    """Incoherent (power) sum of two dBm levels -- used to floor-limit a reading."""
    return 10.0 * math.log10(10.0 ** (a_dbm / 10.0) + 10.0 ** (b_dbm / 10.0))
