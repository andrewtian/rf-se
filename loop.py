"""SE substitution control loop: EA8 reference pass -> through-wall pass -> SE.

Implements the doc 159 sec 4a control loop + the PC6/EA8 gate + PC8 logging.
Hardware-agnostic: the same code runs against real instruments or the simulator
(drivers.open_instruments). SE is the substitution ratio SE(f) = reference(f) -
wall(f); a reading at the noise floor yields only a LOWER BOUND on SE.

Verdict per frequency:
  PASS         measured SE >= target (or floor-limited AND capability >= target)
  FAIL         measured SE < target and not floor-limited
  INCONCLUSIVE floor-limited and capability < target -> need more dynamic range
               (drop RBW, add the RX LNA, or use higher-gain horns; doc 159 4.2b)
"""
from __future__ import annotations

import csv
import dataclasses
import json
import math
import os
import subprocess


class AcquisitionRejected(RuntimeError):
    """A sweep/acquisition failed a measurement-integrity guard (UNCAL, wrong units,
    short trace, a span too coarse to resolve the expected leak notch, or a
    reference/wall axis mismatch) and MUST NOT be trusted for an SE result."""


def _set_source(source, f_hz, p_dbm):
    source.set_power(p_dbm)
    source.set_freq(f_hz)


# The band-0 / preselected crossover. Below it there is no YIG preselector AND no harmonic mixing, so
# a measured tone's frequency is as accurate as the source. Above it the 8565EC's marginal precision
# reference is HARMONIC-MULTIPLIED (N x LO), so the measured tone frequency is off by N x the reference
# error -- untrustworthy until the reference is serviced. See audit/2026-07-03-gpib-low-level-audit.md.
_PRESELECTOR_MIN_HZ = 2.9e9


def _band_trust(f_hz: float) -> str:
    """'trusted' at or below the 2.9 GHz preselected crossover (no preselector, no harmonic-multiplied
    reference error -> the SE number is metrology-grade today); 'provisional' above it (the marginal
    reference makes the high-band FREQUENCY untrustworthy, so a high-band SE must never be reported as
    trusted until the reference is serviced). Task 4 -- surfaced per row so a consumer never treats a
    provisional high-band SE as a certified number."""
    return "trusted" if f_hz <= _PRESELECTOR_MIN_HZ else "provisional"


def _effective_rbw_ladder(analyzer_cfg):
    """The RBW rungs a reference pass will actually try, in order: rbw_hz first, then any
    entries of rbw_ladder_hz strictly NARROWER than it. A caller that overrides rbw_hz alone
    (without touching rbw_ladder_hz) gets back a single-rung list -- no auto-narrowing, so a
    campaign pinning a fixed RBW is unaffected by the C3 default ladder."""
    base = analyzer_cfg.rbw_hz
    return [base] + [r for r in analyzer_cfg.rbw_ladder_hz if r < base]


def _configure_analyzer(analyzer, cfg, rbw_hz):
    """Apply the acceptance-path analyzer settings for ONE point at rbw_hz and return the
    settings FINGERPRINT that the reference and wall passes MUST share.

    This is the single home of the identical-settings invariant (methodology point 2): SE =
    reference - wall is a ratio, so ANY analyzer setting that differs between the two passes is a
    fixed dB offset masquerading as shielding. Both acquire_reference and measure_wall configure a
    point through here, and the returned fingerprint is recorded per row (row["settings"]) and
    gated for ref/wall parity in summarize() -- so a divergence in RBW, VBW, reference level,
    detector OR attenuation fails campaign_pass instead of silently corrupting SE.

    Two corrections vs a bare analyzer.configure() call:
      VBW <= RBW (methodology point 1): the adaptive ladder narrows RBW to buy dynamic range, but
        a video bandwidth left WIDER than the resolution bandwidth stops the video filter from
        smoothing the noise floor, throwing that gain away on real hardware. VBW is clamped to the
        current RBW here so every rung honors VBW <= RBW. (VBW=0/AUTO is passed through unchanged.)
      attenuation APPLIED (methodology points 2 + 7): cfg.analyzer.attenuation_db is otherwise a
        dead knob in the stepped-CW acceptance path (analyzer.configure does not take it), so an
        operator who raises attenuation for the strong baseline per point 7 would have it silently
        ignored. It is written here (identically in both passes) via set_attenuation, which floors
        it at any armed input-protection minimum. Guarded so a minimal duck-typed analyzer without
        set_attenuation (some unit-test fakes) still runs."""
    a = cfg.analyzer
    vbw = min(a.vbw_hz, rbw_hz) if (a.vbw_hz and a.vbw_hz > 0) else a.vbw_hz
    analyzer.configure(rbw_hz, vbw, a.ref_level_dbm, a.detector)
    if hasattr(analyzer, "set_attenuation"):
        analyzer.set_attenuation(db=a.attenuation_db)
    return {"rbw_hz": rbw_hz, "vbw_hz": vbw, "ref_level_dbm": a.ref_level_dbm,
            "detector": a.detector, "attenuation_db": a.attenuation_db}


def acquire_reference(cfg, source, analyzer, bench=None, on_point=None):
    """EA8 empty-frame pass (horns face-to-face, NO wall).

    Per f: measure the noise floor (RF off) and the 0 dB reference (RF on) at the FIRST rung of
    the RBW ladder (cfg.analyzer.rbw_hz); the measured capability = reference - floor - margin.

    ADAPTIVE RBW LADDER (C3, doc 159 sec 4.2b "RBW is free gain" -- narrowing RBW one decade buys
    +10 dB of dynamic range at no hardware cost): if this point is EA8-limited (capability <
    band.target_se_db) and the ladder (cfg.analyzer.rbw_ladder_hz) has a narrower rung left,
    RE-MEASURE this SAME point's floor + reference at that rung, recompute capability, and repeat
    down the ladder until capability clears the target or the ladder is exhausted. The FINAL
    rbw_hz tried is recorded in the row ("rbw_hz") -- measure_wall reads this exact point back at
    that SAME rbw_hz (symmetric per index: SE = ref - wall is a ratio, so an asymmetric RBW
    between the two passes is a fixed dB offset masquerading as SE). PC6 gate: ea8_ok iff the
    FINAL capability >= target (the setup can SEE down to the target with margin).

    on_point(i, row), if given, is called after each point completes -- the hook the live
    coordinator uses to stream EA8/reference progress during concurrent operation.
    """
    # bring both units to a known-good state ONCE at campaign start: the source to a leveled CW
    # output (RST/IL1/AT0), the analyzer to a clean preset (IP + stale-sweep flush). Without this
    # the analyzer's marker reads are non-physical and non-repeatable from a dirty prior state.
    # Guarded so a minimal duck-typed instrument (no prepare) still runs.
    if hasattr(source, "prepare"):
        source.prepare()
    if hasattr(analyzer, "prepare"):
        analyzer.prepare()
    ladder = _effective_rbw_ladder(cfg.analyzer)
    _configure_analyzer(analyzer, cfg, ladder[0])
    if bench is not None:
        bench.wall_present = False
    rows = {}
    # keyed by integer index, NOT f_hz: adjacent bands share an endpoint
    # (e.g. 18 GHz is band-1 top and band-2 bottom) and an f_hz key would collide.
    for i, (f_hz, band) in enumerate(cfg.frequencies()):
        _set_source(source, f_hz, band.source_power_dbm)
        if bench is not None:                              # sim: use THIS band's horn/DANL
            bench.gain = band.antenna_gain_dbi
            bench.danl = band.danl_dbm_per_hz
        rbw = ladder[0]
        # reset to the ladder's first (widest) rung for EVERY point: a previous point may have
        # narrowed the analyzer while chasing its own EA8 gate; each new point starts fresh.
        settings = _configure_analyzer(analyzer, cfg, rbw)
        source.rf_off()
        # source-off floor with the SAMPLE detector (not POS): POS inflates the floor ~+2.5 dB,
        # which does NOT cancel because the floor gates capability/floor_limited, not the
        # ref-wall tone difference.
        _, floor = analyzer.measure_floor(f_hz, cfg.analyzer.settle_s)
        psdac = None
        # RF-ON window in try/finally (CRITICAL SAFETY): a LinkDropped/timeout from any read below
        # must NOT leave the source radiating -- rf_off always runs. Same pattern as
        # nearfield_walkaround.
        source.rf_on()
        try:
            # cross-instance sync: let the source SETTLE at f (and confirm completion) BEFORE the
            # analyzer reads -- the coordinator's sequential blocking calls order the two networked
            # instances; await_settled adds the synthesizer settle the ordering alone does not.
            source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
            if f_hz > 2.9e9 and hasattr(analyzer, "peak_preselector"):
                # peak the YIG preselector ON THE LIVE TONE (the through-shield tone in the wall
                # pass is too weak to re-peak reliably), then re-assert the zero-span acceptance
                # config -- peak_preselector zooms to SP 50MHZ / RB 1kHz to find the peak, so the
                # CW dwell config must be re-applied before the tone read below. Peaked ONCE here, at
                # this (widest) rung -- PP needs RBW > 100 Hz and is hardware DAC state independent
                # of RBW, so it is never re-peaked while narrowing below; the recorded DAC is reused
                # verbatim through the rest of this point (and the wall pass, per C2).
                psdac = analyzer.peak_preselector(f_hz)
                settings = _configure_analyzer(analyzer, cfg, rbw)
            _, ref = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        finally:
            source.rf_off()
        cap = ref - floor - cfg.margin_db
        # ADAPTIVE RBW LADDER (C3): EA8-limited at this rung -> narrow and re-measure (own SMP
        # floor + ref), stopping as soon as capability clears the target or the ladder runs out.
        for next_rbw in ladder[1:]:
            if cap >= band.target_se_db:
                break                                  # already clears the target -- stop here
            rbw = next_rbw
            settings = _configure_analyzer(analyzer, cfg, rbw)
            source.rf_off()
            _, floor = analyzer.measure_floor(f_hz, cfg.analyzer.settle_s)
            source.rf_on()
            try:                                       # RF-ON window in try/finally (see above)
                source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
                _, ref = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
            finally:
                source.rf_off()
            cap = ref - floor - cfg.margin_db
        rows[i] = {
            "band": band.name, "f_hz": f_hz,
            "src_power_dbm": band.source_power_dbm,  # KNOWN TX: calibrated source-output power
            "floor_dbm": floor, "ref_dbm": ref,
            "coupling_db": ref - band.source_power_dbm,  # RX-measured thru-path gain (ref - set TX)
            "capability_db": cap, "target_db": band.target_se_db,
            "ea8_ok": cap >= band.target_se_db,
            "rbw_hz": rbw,        # FINAL rbw the ladder ended on; measure_wall reads back at this
            "settings": settings,  # identical-settings fingerprint (point 2); gated in summarize
            "preselector_dac": psdac,        # reused verbatim in the wall pass (None <=2.9GHz/sim)
            "floor_detector": "SMP",
            "acq_mode": "stepped-cw-zerospan", "purpose": "acceptance",
            "source_tracked": True,          # the source is set to f at every point
            "band_trust": _band_trust(f_hz),  # Task 4: 'trusted' <=2.9GHz / 'provisional' above
        }
        if on_point is not None:
            on_point(i, rows[i])
    return rows


def measure_wall(cfg, source, analyzer, reference, bench=None, on_point=None):
    """Through-wall pass: per f, measure wall(f); SE = reference - wall.

    C3 SYMMETRIC RBW: reads each point at the SAME rbw_hz the reference pass ended on for that
    index (re-configuring the analyzer per point, not once for the whole pass) -- SE is a ratio,
    so an asymmetric RBW between the two passes at any index is a fixed dB offset masquerading
    as SE. Falls back to the campaign default cfg.analyzer.rbw_hz for a reference row that
    predates this key (e.g. a hand-built unit-test fixture never ran the adaptive ladder).

    Takes its OWN source-off floor per point (never reuses only the reference pass's stale
    floor); floor_limited is tested against max(ref_floor, wall_floor), the conservative
    (worse-case) of the two. Floor-limited readings (wall within margin of that floor) are
    reported as a LOWER BOUND (SE >= capability, taken from the reference row). Asserts the
    reference exists for each f.

    on_point(i, row), if given, is called after each point -- the row carries se_db /
    se_reported_db / floor_limited / verdict, so a live consumer knows the running SE
    figure WHILE the two units operate concurrently (R8).
    """
    if bench is not None:
        bench.wall_present = True
    rows = {}
    for i, (f_hz, band) in enumerate(cfg.frequencies()):
        ref_row = reference[i]
        if abs(ref_row["f_hz"] - f_hz) > 1.0:            # SE is a ratio: axes MUST match
            raise AcquisitionRejected(
                f"wall freq {f_hz:.0f} != reference freq {ref_row['f_hz']:.0f} at index {i}")
        # C3: symmetric per-index RBW -- read this point back at whatever RBW the reference
        # pass ended on for THIS index (its adaptive ladder may have narrowed it independently
        # of every other index).
        rbw = ref_row.get("rbw_hz", cfg.analyzer.rbw_hz)
        settings = _configure_analyzer(analyzer, cfg, rbw)
        _set_source(source, f_hz, band.source_power_dbm)
        if bench is not None:                              # sim: use THIS band's horn/DANL
            bench.gain = band.antenna_gain_dbi
            bench.danl = band.danl_dbm_per_hz
        source.rf_off()
        # the wall pass takes its OWN source-off floor per point (never trusts the stale
        # reference-pass floor alone): a time-varying ambient/noise floor is not guaranteed
        # to match what the reference pass saw. SAMPLE detector, same reason as the ref pass.
        _, wall_floor = analyzer.measure_floor(f_hz, cfg.analyzer.settle_s)
        psdac = ref_row.get("preselector_dac")
        # RF-ON window in try/finally (CRITICAL SAFETY): rf_off always runs even if a read below
        # raises LinkDropped/timeout, so the source is never left radiating on a mid-point drop.
        source.rf_on()
        try:
            source.await_settled(cfg.source.settle_s, cfg.source.use_opc)   # settle before RX reads
            # reuse the EXACT preselector DAC peaked in the reference pass -- the through-shield
            # tone here is too weak to re-peak reliably. No-op below 2.9 GHz / when the ref pass
            # never peaked (sim, or a duck-typed analyzer without set_preselector_dac).
            if psdac is not None and hasattr(analyzer, "set_preselector_dac"):
                analyzer.set_preselector_dac(psdac)
            else:
                psdac = None
            _, wall = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        finally:
            source.rf_off()
        cap = ref_row["capability_db"]
        # conservative floor for the floor_limited test: the WORSE (higher) of the two passes'
        # floors, since either pass's floor could gate whether the tone is actually resolved.
        floor = max(ref_row["floor_dbm"], wall_floor)
        # REGIME (methodology point 5): SE here is the dBm SUBTRACTION ref - wall, which IS
        # 10*log10(P_ref / P_dut) -- the POWER-RATIO SE form, correct for the direct-coax /
        # plane-wave (0.3-40 GHz) regime this rig measures, and equally correct for any matched
        # 50-ohm antenna read (power into a matched load: 10*log10(power) == 20*log10(field)). The
        # 20*log10 FIELD-RATIO form applies ONLY to a direct field-magnitude measurement (a loop or
        # biconical reading H-/E-field magnitude, not power into 50 ohm, below 300 MHz) -- which
        # this coax-substitution phase does not do. If such an antenna-field regime is ever added,
        # its SE must NOT reuse this dBm subtraction as a field ratio without the factor-of-two.
        se = ref_row["ref_dbm"] - wall
        floor_limited = wall <= floor + cfg.margin_db
        if floor_limited:
            verdict = "PASS" if cap >= band.target_se_db else "INCONCLUSIVE"
            se_report = cap                       # lower bound: SE >= capability
        else:
            verdict = "PASS" if se >= band.target_se_db else "FAIL"
            se_report = se
        rows[i] = {
            "band": band.name, "f_hz": f_hz, "wall_dbm": wall,
            "wall_floor_dbm": wall_floor, "floor_detector": "SMP",
            "preselector_dac": psdac,
            "rbw_hz": rbw,           # C3: matches ref_row["rbw_hz"] by construction (symmetric)
            "settings": settings,    # identical-settings fingerprint (point 2); == ref by construction
            "se_db": se, "se_reported_db": se_report,
            "floor_limited": floor_limited, "target_db": band.target_se_db,
            "verdict": verdict,
            "acq_mode": "stepped-cw-zerospan", "purpose": "acceptance",
            "source_tracked": True,          # source retuned to f before each wall read
            "band_trust": _band_trust(f_hz),  # Task 4: 'trusted' <=2.9GHz / 'provisional' above
        }
        if on_point is not None:
            on_point(i, rows[i])
    return rows


def reference_drift(cfg, source, analyzer, reference, bench=None, tol_db=3.0, on_point=None):
    """Baseline-drift re-check (methodology point 4): RE-MEASURE the 0 dB reference (no wall) and
    compare it, per frequency, to the ORIGINAL reference pass. A substitution SE is only valid if
    the baseline held STILL between the reference and wall passes -- source level, path, and
    analyzer gain must not have drifted. If the re-measured reference has moved more than +/-tol_db
    (IEEE-299-style 3 dB) at any point, the SE numbers are suspect and the run should be repeated.

    Re-measures at the SAME final rbw_hz each point ended on (via _configure_analyzer, so the
    identical-settings fingerprint is reused) and reuses the recorded preselector DAC -- a drift is
    then a real amplitude change, not an RBW/preselector artifact. Does NOT re-run the adaptive
    ladder: this checks STABILITY, not capability. Returns {schema, rows, n, tol_db,
    max_abs_drift_db, drift_ok, verdict}; verdict STABLE iff every point held within tol_db, else
    DRIFTED. on_point(i, row) streams progress. RF is turned OFF in a finally per point (a hung
    read never leaves the source radiating), mirroring acquire_reference."""
    if bench is not None:
        bench.wall_present = False
    rows = []
    max_abs = 0.0
    for i, (f_hz, band) in enumerate(cfg.frequencies()):
        ref_row = reference[i]
        if abs(ref_row["f_hz"] - f_hz) > 1.0:            # SE is a ratio: axes MUST match
            raise AcquisitionRejected(
                f"drift re-check freq {f_hz:.0f} != reference freq {ref_row['f_hz']:.0f} at index {i}")
        rbw = ref_row.get("rbw_hz", cfg.analyzer.rbw_hz)
        _configure_analyzer(analyzer, cfg, rbw)          # SAME settings the point was captured at
        _set_source(source, f_hz, band.source_power_dbm)
        if bench is not None:                              # sim: use THIS band's horn/DANL
            bench.gain = band.antenna_gain_dbi
            bench.danl = band.danl_dbm_per_hz
        source.rf_off()
        _, floor = analyzer.measure_floor(f_hz, cfg.analyzer.settle_s)
        psdac = ref_row.get("preselector_dac")
        source.rf_on()
        try:
            source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
            if psdac is not None and hasattr(analyzer, "set_preselector_dac"):
                analyzer.set_preselector_dac(psdac)      # same preselector state as the ref pass
            _, recheck = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        finally:
            source.rf_off()
        drift = recheck - ref_row["ref_dbm"]
        within = abs(drift) <= tol_db
        max_abs = max(max_abs, abs(drift))
        row = {"band": band.name, "f_hz": f_hz,
               "ref_dbm": ref_row["ref_dbm"], "recheck_ref_dbm": recheck,
               "recheck_floor_dbm": floor, "drift_db": drift, "within_tol": within,
               "tol_db": tol_db, "band_trust": _band_trust(f_hz)}
        rows.append(row)
        if on_point is not None:
            on_point(i, row)
    drift_ok = bool(rows) and all(r["within_tol"] for r in rows)
    return {"schema": "se299-reference-drift/1", "rows": rows, "n": len(rows),
            "tol_db": tol_db, "max_abs_drift_db": round(max_abs, 3),
            "drift_ok": drift_ok, "verdict": "STABLE" if drift_ok else "DRIFTED"}


def localize(cfg, source, analyzer, freq_hz, positions, source_power_dbm=None,
             move_probe=None, bench=None):
    """Fixed-frequency seam/leak localization: level-vs-probe-position, acquired
    DIGITALLY over the bus (NOT screen-read -- the display refresh rate is irrelevant).

    `positions`     probe positions (m, or opaque labels) to sample.
    `move_probe`    optional callable(pos) that places the probe -- a robotic-stage
                    command or an operator prompt; None in sim (the bench tracks position).
    Returns (rows, peak): rows = [{position, level_dbm}], peak = the hottest position.
    The source transmits a fixed CW tone (TX horn outside); the inside near-field probe
    -> analyzer reads the leaked level digitally at each position. A leak shows as a
    level peak (low local SE) -- that position is where a seam/gasket needs attention.
    """
    band = cfg.band_for(freq_hz)
    p = source_power_dbm if source_power_dbm is not None else band.source_power_dbm
    analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                       cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
    if bench is not None:
        bench.localize_mode = True
        bench.gain, bench.danl = band.antenna_gain_dbi, band.danl_dbm_per_hz
    source.set_power(p)
    source.set_freq(freq_hz)
    rows = []
    # RF-ON window in try/finally (CRITICAL SAFETY): rf_off (and the sim localize-mode reset) always
    # run even if a probe read raises mid-scan, so the source never keeps radiating on a drop.
    source.rf_on()
    try:
        for pos in positions:
            if move_probe is not None:
                move_probe(pos)
            if bench is not None:
                bench.probe_position = pos
            _, lvl = analyzer.measure_peak(freq_hz, cfg.analyzer.settle_s)
            rows.append({"position": pos, "level_dbm": lvl})
    finally:
        source.rf_off()
        if bench is not None:
            bench.localize_mode = False
    peak = max(rows, key=lambda r: r["level_dbm"])
    return rows, peak


def nearfield_walkaround(cfg, source, analyzer, freq_hz, on_frame, should_stop,
                         bench=None, use_average=False, sim_span_m=2.4, power_dbm=None):
    """Live NEAR-FIELD-PROBE WALKAROUND: the operator physically moves a near-field probe (on the
    8565EC input) over the enclosure surface while the 68367C transmits a fixed CW tone at freq_hz.
    The analyzer reads the probe level in a tight loop; each read is a frame -> on_frame(i, dbm).
    A LOCAL LEVEL PEAK as the probe passes a seam/gasket is a leak (low local SE).

    Metrology (agilent-8560e-users-guide.md): zero-span at freq_hz, positive-peak, the campaign RBW/
    RL; the preselector is PEAKED once when freq_hz > 2.9 GHz (else a real leak reads low). Set
    use_average=True to read through a pulsed interferer (sample detector + averaging).

    on_frame(i, level_dbm) is called per read; should_stop() ends the loop. RF is turned OFF in a
    finally (a hung read never leaves the source radiating). bench!=None (sim) advances a simulated
    probe position each frame so the level traces the demo leak profile -- a realistic 'walking past
    the leak' rehearsal; on real hardware bench is None and the operator's hand moves the probe."""
    band = cfg.band_for(freq_hz)
    if hasattr(source, "prepare"):
        source.prepare()
    if hasattr(analyzer, "prepare"):
        analyzer.prepare()
    analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                       cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
    if bench is not None:
        bench.localize_mode = True
        bench.gain, bench.danl = band.antenna_gain_dbi, band.danl_dbm_per_hz
        if getattr(bench, "leak_profile", None) is None:
            import drivers as _d
            bench.leak_profile = _d.demo_seam_leak()
    source.set_power(band.source_power_dbm if power_dbm is None else float(power_dbm))
    source.set_freq(freq_hz)
    source.rf_on()
    try:
        source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
        if freq_hz > 2.9e9:
            analyzer.peak_preselector(freq_hz)          # correct amplitude above the 2.9 GHz crossover
        i = 0
        while not should_stop():
            if bench is not None:                        # sim: sweep the probe past the leak
                bench.probe_position = round((i % 50) * sim_span_m / 49.0, 4)
            if use_average:
                _, lvl = analyzer.measure_average(freq_hz, cfg.analyzer.settle_s, sweeps=1)
            else:
                _, lvl = analyzer.measure_peak(freq_hz, cfg.analyzer.settle_s)
            on_frame(i, lvl)
            i += 1
    finally:
        source.rf_off()
        if bench is not None:
            bench.localize_mode = False
    return i


def stepped_cw_sweep(cfg, source, analyzer, freqs_hz, bench=None, wall=True):
    """Acceptance-GRADE synthetic sweep with the SOURCE TRACKING the sweep in software:
    the controller sets the source CW to each f and reads a zero-span narrow-RBW
    positive-peak dwell there, so a tone is present at every measured point
    (source_tracked=True). Its high dynamic range (narrow RBW) is exactly what a swept-span
    screen LACKS, so it catches deep leaks -- but a single pass with no reference is still
    SCREENING; the SE acceptance verdict comes from capture (reference + wall)."""
    analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                       cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
    if bench is not None:
        bench.wall_present = wall
    freqs = list(freqs_hz)
    levels = []
    # the source stays ON across the whole (fast) sweep; wrap the loop in try/finally (CRITICAL
    # SAFETY) so rf_off always runs even if a measure_peak raises LinkDropped/timeout mid-sweep,
    # never leaving the source radiating. Same pattern as nearfield_walkaround.
    try:
        for f in freqs:
            band = cfg.band_for(f)
            if bench is not None:
                bench.gain, bench.danl = band.antenna_gain_dbi, band.danl_dbm_per_hz
            _set_source(source, f, band.source_power_dbm)
            source.rf_on()
            # methodology-correct per-point sync (compass sec 4, controller-paced loop): after the
            # source is set + blasting, let it SETTLE (synth lock + analyzer tune) BEFORE the zero-span
            # read, and peak the YIG preselector above 2.9 GHz so a high-band tone is not under-read,
            # restoring the zero-span acceptance config afterward. Same discipline as acquire_reference.
            source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
            if f > _PRESELECTOR_MIN_HZ and hasattr(analyzer, "measure_tracked_peak"):
                # HIGH BAND (Task 4): find the tone's LEVEL with measure_tracked_peak -- it peaks the
                # YIG preselector AND re-centers on the tone. A plain measure_peak-at-exact-CF MISSES
                # the harmonic-multiplied reference offset (its CF write also clobbers the preselector
                # peak's MKCF re-centering). Then restore the zero-span acceptance config for the next
                # (possibly low-band) point. The LEVEL is trustworthy; the tone FREQUENCY is provisional.
                _, lvl = analyzer.measure_tracked_peak(f, settle_s=cfg.analyzer.settle_s)
                analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                                   cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
            else:
                _, lvl = analyzer.measure_peak(f, cfg.analyzer.settle_s)
            levels.append(lvl)
    finally:
        source.rf_off()
    hot_i = max(range(len(levels)), key=lambda k: levels[k]) if levels else 0
    return {"acq_mode": "stepped-cw-zerospan", "purpose": "screening",
            "freqs_hz": freqs, "levels_dbm": levels,
            "band_trust_by_point": [_band_trust(f) for f in freqs],   # Task 4: per-point trust
            "trusted_band_max_hz": _PRESELECTOR_MIN_HZ,
            "hot_freq_hz": freqs[hot_i] if freqs else None,
            "hot_level_dbm": levels[hot_i] if levels else None,
            "source_tracked": True, "tracking": "software-lockstep"}


def tracked_sweep(cfg, source, analyzer, freqs_hz, bench=None, hardware=False, wall=True):
    """A swept SE acquisition with the SOURCE TRACKING the analyzer sweep -- a tone is
    present at every measured frequency. This is the REQUIREMENT for a valid swept SE: a
    swept trace taken against a parked or absent source is only a screen (FM8), never an
    SE measurement. Two mechanisms, both returning source_tracked=True:

      software (default): the controller sets the source AND the analyzer to each f (one
        GPIB round-trip per point) -- i.e. stepped_cw_sweep. Robust, no extra wiring.
      hardware=True: the source runs a preloaded LIST sweep advanced by one external
        trigger per point (set_list_sweep / arm_sweep / trigger_point), so the tone tracks
        the analyzer bin with no per-point source GPIB -- faster, but needs the trigger
        wire (8560 trig-out -> 683xx trig-in) and the 683xx list-sweep commands (VERIFY).
    """
    freqs = list(freqs_hz)
    if not hardware:
        return stepped_cw_sweep(cfg, source, analyzer, freqs, bench=bench, wall=wall)
    analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                       cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
    if bench is not None:
        bench.wall_present = wall
    mid_band = cfg.band_for(freqs[len(freqs) // 2]) if freqs else cfg.bands[0]
    source.set_power(mid_band.source_power_dbm)
    source.set_list_sweep(freqs)
    source.rf_on()
    source.arm_sweep()
    levels = []
    for i, f in enumerate(freqs):
        band = cfg.band_for(f)
        if bench is not None:
            bench.gain, bench.danl = band.antenna_gain_dbi, band.danl_dbm_per_hz
        if i:
            source.trigger_point()                # advance the source list to this f
        _, lvl = analyzer.measure_peak(f, cfg.analyzer.settle_s)
        levels.append(lvl)
    source.rf_off()
    hot_i = max(range(len(levels)), key=lambda k: levels[k]) if levels else 0
    return {"acq_mode": "stepped-cw-zerospan", "purpose": "screening",
            "freqs_hz": freqs, "levels_dbm": levels,
            "hot_freq_hz": freqs[hot_i] if freqs else None,
            "hot_level_dbm": levels[hot_i] if levels else None,
            "source_tracked": True, "tracking": "hardware-list-sweep"}


def require_source_tracked(frame):
    """Gate: a sweep frame may feed an SE result ONLY if its source tracked the analyzer
    sweep. A frame with source_tracked False (e.g. swept_screen) is screening-only and
    raises AcquisitionRejected -- promote it to a verdict only after a tracked re-measure."""
    if not frame.get("source_tracked", False):
        raise AcquisitionRejected(
            "source did not track the sweep: a swept acquisition without a per-point tone "
            "is a SCREEN (lower bound), not an SE measurement -- use tracked_sweep / capture")
    return frame


def swept_screen(analyzer, sweep, expect_points=601, expected_notch_hz=None):
    """Fast swept-span SCREEN over one analyzer sweep (no per-point source step). Returns
    a screening frame -- NEVER an SE verdict: its span-coupled RBW floor is too high to
    see deep leaks (blind), so it yields only a lower bound and a spur/hot-bin map. Applies
    the integrity guards that make a swept read trustworthy AS A SCREEN, raising
    AcquisitionRejected (never a silent wrong number) on UNCAL, non-DBM units, a short
    trace, or a point spacing too coarse to resolve the expected leak notch."""
    analyzer.set_amplitude_units(sweep.aunits)
    analyzer.set_attenuation(db=sweep.attenuation_db)
    analyzer.set_detector("POS")                          # hunt leak peaks: never under-read
    analyzer.set_video_average(sweep.video_avg or None)
    analyzer.set_max_hold(sweep.max_hold)
    if sweep.sweep_time_s:
        analyzer.set_sweep_time(seconds=sweep.sweep_time_s)
    else:
        analyzer.set_sweep_time(auto=True)
    analyzer.set_frequency(start_hz=sweep.span_lo_hz, stop_hz=sweep.span_hi_hz)
    analyzer.arm_and_wait(timeout_s=max(10.0, sweep.sweep_time_s + 1.0))
    if analyzer.measurement_uncalibrated():
        raise AcquisitionRejected("MEAS UNCAL: sweep time too short for the span/RBW")
    if sweep.aunits != "DBM":
        raise AcquisitionRejected(f"amplitude units {sweep.aunits!r} != DBM")
    freqs, levels = analyzer.read_trace("A")
    if expect_points and len(levels) != expect_points:
        raise AcquisitionRejected(f"trace length {len(levels)} != expected {expect_points}")
    if expected_notch_hz and len(freqs) > 1:
        spacing = (freqs[-1] - freqs[0]) / (len(freqs) - 1)
        if spacing > expected_notch_hz / 3.0:
            raise AcquisitionRejected(
                f"point spacing {spacing:.3g} Hz too coarse to resolve a "
                f"{expected_notch_hz:.3g} Hz notch (need >= 3 points across it)")
    hot_i = max(range(len(levels)), key=lambda k: levels[k])
    return {"acq_mode": "swept-span", "purpose": "screening", "source_tracked": False,
            "freqs_hz": freqs, "levels_dbm": levels,
            "hot_freq_hz": freqs[hot_i], "hot_level_dbm": levels[hot_i],
            "note": "SCREEN -- lower-bound only, source NOT tracking (no per-point tone), "
                    "blind to deep leaks; re-measure hits with a tracked stepped-CW dwell"}


def composite_q(volume_m3, transfer_ratio, f_hz):
    """Reverberation composite quality factor of an overmoded cavity:
    Q = 16 pi^2 V <Pr/Pt> / lambda^3 (Holloway 2008 Eq. 34; IEC 61000-4-21). Inputs:
    interior volume (m^3), the frequency-averaged LINEAR received/transmitted power
    ratio, and frequency (Hz). Valid only above the chamber's lowest usable frequency."""
    lam = 299_792_458.0 / f_hz
    return 16.0 * math.pi ** 2 * volume_m3 * transfer_ratio / (lam ** 3)


def cavity_q(analyzer, cav, bench=None):
    """Cavity Q-factor characterization (both units inside; NOT IEEE-299). Sweeps the
    span, then returns the LOW-BAND resolved-resonance linewidth Q = f0 / BW_n_db (from
    the pulled trace, no MKBW mnemonic). Set bench.resonance (sim) or drive the real
    cavity to exercise it. Composite/high-band Q uses composite_q() with an averaged
    transfer ratio."""
    analyzer.set_detector("SMP")                          # power, not peak
    analyzer.set_video_average(cav.video_avg or None)
    analyzer.set_frequency(start_hz=cav.span_lo_hz, stop_hz=cav.span_hi_hz)
    analyzer.arm_and_wait(timeout_s=10.0)
    f0, peak = analyzer.marker_peak()
    bw = analyzer.marker_bandwidth(n_db=cav.n_db_down, from_trace=True)
    q = (f0 / bw) if bw > 0 else float("inf")
    return {"acq_mode": "cavity-q", "purpose": "characterization",
            "f0_hz": f0, "bw_hz": bw, "q": q, "peak_dbm": peak,
            "n_db_down": cav.n_db_down}


def run_demo(cfg, source, analyzer, bench):
    """In-process reference + wall demo (sim only). Returns (reference, wall)."""
    reference = acquire_reference(cfg, source, analyzer, bench)
    wall = measure_wall(cfg, source, analyzer, reference, bench)
    return reference, wall


def summarize(reference, wall):
    """Roll up the campaign: EA8 failures, verdict counts, worst capability point.

    campaign_pass is AFFIRMATIVE: it is True only if there are points, EA8 passed
    everywhere, EVERY row is a stepped-CW acceptance datum, EVERY verdict is PASS, and
    (C3) the reference and wall passes used the SAME rbw_hz at every index. A screening
    row (swept-span / probe) can never make the campaign pass -- it is structurally
    excluded, not merely "not a FAIL"; neither can an asymmetric per-index RBW."""
    ea8_fail = [r for r in reference.values() if not r["ea8_ok"]]
    verdicts = {}
    for r in wall.values():
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    worst = min(reference.values(), key=lambda r: r["capability_db"])
    wall_rows = list(wall.values())
    all_acceptance = all(
        r.get("acq_mode", "stepped-cw-zerospan") == "stepped-cw-zerospan"
        and r.get("purpose", "acceptance") == "acceptance"
        and r.get("source_tracked", True) for r in wall_rows)
    # C3 acceptance-integrity gate: PER-INDEX rbw_hz match (not a single global scalar) -- SE is
    # a ratio, so an asymmetric RBW between the two passes at any one index is a fixed dB offset
    # masquerading as SE. Rows that predate this key (both sides missing it) compare as matching.
    rbw_symmetric = all(
        reference.get(k, {}).get("rbw_hz") == r.get("rbw_hz") for k, r in wall.items())
    # IDENTICAL-SETTINGS gate (methodology point 2 -- the single most important SE invariant):
    # the FULL per-index analyzer fingerprint (rbw, vbw, ref level, detector, attenuation) must
    # match between the reference and wall passes, not just RBW. Any divergence is a fixed dB
    # offset that masquerades as shielding, so it fails the campaign. Rows predating the "settings"
    # key (both sides missing it, e.g. a hand-built fixture) compare as matching -- back-compat.
    settings_symmetric = all(
        reference.get(k, {}).get("settings") == r.get("settings") for k, r in wall.items())
    all_pass = len(wall_rows) > 0 and all(r["verdict"] == "PASS" for r in wall_rows)
    return {
        "n_points": len(wall),
        "ea8_fail_count": len(ea8_fail),
        "verdicts": verdicts,
        "worst_capability_db": round(worst["capability_db"], 2),
        "worst_capability_f_hz": worst["f_hz"],
        "rbw_symmetric": rbw_symmetric,
        "settings_symmetric": settings_symmetric,
        "campaign_pass": (len(ea8_fail) == 0 and all_acceptance and all_pass
                          and rbw_symmetric and settings_symmetric),
    }


# ----------------------------------------------------------------- device validation (executable)

def classify_8560_error(code) -> str:
    """Classify an 8565EC/8560 ERR? code by range (8560 E-series UG 08560-90146 Ch.9 "Error Code
    Listing"): 100-199 = programming/parser errors (BENIGN, user-recoverable -- e.g. 111 #ARGMTS,
    112 ??CMD??); 200-799 = hardware failures (instrument needs SERVICE); 800-899 = option module;
    900-999 = user-generated measurement errors (bad setup -- e.g. 901 TGFrqLmt, 902 BAD NORM)."""
    try:
        c = abs(int(code))
    except (TypeError, ValueError):
        return "other"
    if 100 <= c <= 199:
        return "parser"
    if 200 <= c <= 799:
        return "hardware"
    if 800 <= c <= 899:
        return "option"
    if 900 <= c <= 999:
        return "measurement"
    return "other"


def _error_queue_status(errs) -> str:
    """PASS if empty; FAIL if any hardware/measurement/option/unknown code; WARN if only benign
    100-series parser codes (e.g. the per-sweep 111 the bench 8565E posts on every zero-span TS
    while the data stays valid)."""
    if not isinstance(errs, list):
        return "FAIL"
    if not errs:
        return "PASS"
    classes = {classify_8560_error(e) for e in errs}
    if classes <= {"parser"}:
        return "WARN"
    return "FAIL"


def validate_devices(cfg, source, analyzer, bench=None, probe_hz: float = 5.0e9):
    """Executable verification of ADHERENCE to correct operation -- runs the automatable subset of
    the per-device validation sequence (EQUIPMENT_VALIDATION.md), each step traced to the manual.
    Returns {checks:[{id,name,cite,status,detail}], n_pass, n_fail, n_na, all_pass}. status is
    'PASS' / 'FAIL' / 'NA' (not applicable, e.g. preselector below 2.9 GHz or on the simulator).
    Physical steps (the 8565E 300 MHz CAL-OUT amplitude check, RF coupling) are NOT automatable and
    are excluded -- they remain operator steps in the sequence."""
    checks = []

    def rec(cid, name, cite, status, detail=""):
        checks.append({"id": cid, "name": name, "cite": cite, "status": status, "detail": str(detail)[:60]})

    def ok(cond):
        return "PASS" if cond else "FAIL"

    # -- source (Anritsu 68000-series; cite anritsu-68000-series-operation.md) ------------------
    try:
        idn = source.idn()
    except Exception as e:                                # noqa: BLE001
        idn = f"<err:{e}>"
    up = idn.upper()
    rec("S-V1", "source identity (*IDN?)", "anritsu OM 10370-10284",
        ok("68" in up or "ANRITSU" in up or "SIM" in up), idn.strip())
    try:
        source.prepare(); sp = True
    except Exception as e:                                # noqa: BLE001
        sp = False; idn = str(e)
    rec("S-V2", "clean state RST/IL1/AT0/ATT00/TR0/LO0/LOG", "anritsu S2", ok(sp))
    try:
        source.set_freq(probe_hz)
        of = source.output_freq_mhz()
        fok = abs(of - probe_hz / 1e6) <= max(1.0, probe_hz / 1e6 * 1e-6)
    except Exception as e:                                # noqa: BLE001
        of, fok = None, False
    rec("S-V3", "CW output CF1 + OF1 readback", "anritsu S3",
        ok(fok), f"OF1={of} MHz (set {probe_hz/1e6:.0f})")
    try:
        source.set_power(0.0)
        ol = source.output_level_dbm()
        lok = abs(ol) <= 2.0
    except Exception as e:                                # noqa: BLE001
        ol, lok = None, False
    rec("S-V4", "level L1 + OL1 readback", "anritsu S4", ok(lok), f"OL1={ol} dBm (set 0)")
    try:
        source.rf_on()
        settled = source.settled_ok()
    except Exception as e:                                # noqa: BLE001
        settled = False
    rec("S-V5", "leveled+locked (OSB bits clear)", "anritsu S5/S6", ok(settled))
    try:
        source.rf_off(); rok = True
    except Exception:                                    # noqa: BLE001
        rok = False
    rec("S-V7", "RF output on/off (RF1/RF0)", "anritsu S7", ok(rok))

    # -- analyzer (8565EC; cite agilent-8560e-users-guide.md) ----------------------------------
    try:
        aidn = analyzer.idn()
    except Exception as e:                                # noqa: BLE001
        aidn = f"<err:{e}>"
    rec("A-V1", "analyzer identity (ID?)", "agilent A1",
        ok("856" in aidn or "SIM" in aidn.upper()), aidn.strip())
    try:
        analyzer.prepare(); ap = True
    except Exception:                                    # noqa: BLE001
        ap = False
    rec("A-V2", "clean state IP + flush", "agilent A2", ok(ap))
    try:
        analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                           cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
        _, amp = analyzer.measure_peak(probe_hz, cfg.analyzer.settle_s)
        rok2 = amp == amp and amp is not None            # finite number (not NaN/None)
    except Exception as e:                                # noqa: BLE001
        amp, rok2 = None, False
    rec("A-V4", "zero-span read (CF/TS/DONE?/MKPK/MKA?)", "agilent A4",
        ok(rok2), f"{amp:.1f} dBm" if isinstance(amp, float) else str(amp))
    try:
        source.set_freq(probe_hz); source.rf_on()
        dac = analyzer.peak_preselector(probe_hz)
        source.rf_off()
        if probe_hz <= 2.9e9 or dac is None:
            pstatus = "NA"                                # no preselector below 2.9 GHz / sim
        else:
            pstatus = ok(isinstance(dac, int) and 0 <= dac <= 255)
    except Exception as e:                                # noqa: BLE001
        dac, pstatus = None, "FAIL"
    rec("A-V5", "preselector peak >2.9 GHz (PP/PSDAC)", "agilent A5", pstatus, f"PSDAC={dac}")
    # A-V7 isolates the MEASUREMENT's own error codes: clear the queue, take one representative
    # sweep, then read ERR?. An empty queue is PASS. Residual codes are reported as WARN, not a
    # hard adherence FAIL -- command ACCEPTANCE is already proven by the readback checks (S-V3/V4,
    # A-V4/V5), and some 8560 codes (e.g. 111 seen every zero-span TS on the bench 8565E while the
    # data stays valid) are benign per-sweep conditions, not command rejections. The codes are
    # surfaced so the operator can act on genuine faults.
    try:
        analyzer.snapshot_error_baseline()               # capture the CHRONIC codes (re-enter unaided)
        analyzer.measure_peak(probe_hz, cfg.analyzer.settle_s)   # one representative acquisition
        errs = analyzer.query_new_errors()               # only codes NEW since the baseline (F12)
        estatus = _error_queue_status(errs)              # range-classified (parser=WARN, hw=FAIL)
    except Exception as e:                                # noqa: BLE001
        errs, estatus = None, "FAIL"
    # NEW codes only: a sick unit's chronic LO/IF baseline no longer forces a perpetual FAIL here --
    # this check now flags a fault the MEASUREMENT introduced. (Chronic instrument health is reported
    # separately.) codes=[] on a healthy OR a chronically-sick-but-stable analyzer.
    rec("A-V7", "NEW error codes from a sweep (ERR? delta)", "agilent A7", estatus, f"new_codes={errs}")

    n_pass = sum(1 for c in checks if c["status"] == "PASS")
    n_fail = sum(1 for c in checks if c["status"] == "FAIL")
    n_na = sum(1 for c in checks if c["status"] == "NA")
    n_warn = sum(1 for c in checks if c["status"] == "WARN")
    return {"checks": checks, "n_pass": n_pass, "n_fail": n_fail, "n_na": n_na, "n_warn": n_warn,
            "all_pass": n_fail == 0}


# ----------------------------------------------------------------- recorder (PC8)

def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def write_run(out_dir, cfg, reference, wall, summary, timestamp="", note=""):
    """PC8: write the full state (config + per-f reference/wall rows + summary)
    as JSON, plus a flat CSV, into out_dir. Reproducible + auditable."""
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "git_commit": _git_commit(),
        "timestamp": timestamp,
        "note": note,
        "config": dataclasses.asdict(cfg),
        "settings_key": list(cfg.settings_key()[:5]),  # scalar part (bands are in config)
        "summary": summary,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    with open(os.path.join(out_dir, "reference.json"), "w", encoding="utf-8") as fh:
        json.dump([reference[k] for k in sorted(reference)], fh, indent=2)
    with open(os.path.join(out_dir, "wall.json"), "w", encoding="utf-8") as fh:
        json.dump([wall[k] for k in sorted(wall)], fh, indent=2)
    with open(os.path.join(out_dir, "se_results.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        # dr_db = reference - noise floor = the dynamic range the DR gate checks (>= target + margin,
        # methodology point 3); wall_floor_dbm = the wall-pass noise floor logged per point (point 3
        # "log the noise floor per point" -- the reference floor_dbm alone dropped it from the CSV).
        w.writerow(["f_hz", "band", "rbw_hz", "ref_dbm", "floor_dbm", "wall_floor_dbm", "dr_db",
                    "capability_db", "wall_dbm", "se_db", "se_reported_db", "floor_limited",
                    "target_db", "ea8_ok", "verdict"])
        for k in sorted(reference):
            r, m = reference[k], wall[k]
            dr_db = r["ref_dbm"] - r["floor_dbm"]
            w.writerow([f"{k:.0f}", r["band"], r.get("rbw_hz", ""), f"{r['ref_dbm']:.2f}",
                        f"{r['floor_dbm']:.2f}", f"{m.get('wall_floor_dbm', float('nan')):.2f}",
                        f"{dr_db:.2f}", f"{r['capability_db']:.2f}",
                        f"{m['wall_dbm']:.2f}", f"{m['se_db']:.2f}",
                        f"{m['se_reported_db']:.2f}", m["floor_limited"],
                        f"{r['target_db']:.0f}", r["ea8_ok"], m["verdict"]])
    return out_dir


# ----------------------------------------------------------------- RF path self-test (preflight)

def check_path(cfg, source, analyzer, freqs_hz, bench=None, guard_db=6.0):
    """Go/no-go RF-path self-test BEFORE trusting any SE number: transmit a CW tone at each of
    freqs_hz and check the RX actually sees it rise above its own noise floor. This is the step
    that catches a dead/open RF path (loose source-out cable, disconnected antenna feed) instead
    of silently reporting SE = 0.

    Per f: measure RX with TX off (floor) and TX on (tone); delta = on - off. `couples` iff
    delta >= guard_db. Also reports the TX-off ambient level (a live RX picks up ambient RF; a
    dead RX input sits at thermal noise) to help localize which side is broken.

    Returns {rows:[{f_hz, tx_off_dbm, tx_on_dbm, delta_db, couples}], n_couple, n, verdict,
    max_ambient_dbm}. verdict: PATH-LIVE (our tone couples somewhere) / NO-COUPLING (it never
    does -- TX not reaching RX)."""
    if hasattr(source, "prepare"):
        source.prepare()
    if hasattr(analyzer, "prepare"):
        analyzer.prepare()
    analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                       cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
    if bench is not None:
        bench.wall_present = False
    rows = []
    for f_hz in freqs_hz:
        _set_source(source, f_hz, cfg.bands[0].source_power_dbm)
        # off / on / off: the tone must be REVERSIBLE -- rise above the floor when the source is
        # on AND fall back when off. A single off->on delta is fooled by an ambient signal that
        # merely drifts in during the read (observed live: a ~-53 dBm ambient that appears mid-
        # measurement masquerades as coupling). Bracketing the ON read with two OFF reads and
        # requiring ON to exceed BOTH rejects that.
        source.rf_off()
        _, off1 = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        source.rf_on()
        source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
        _, on = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        source.rf_off()
        source.await_settled(cfg.source.settle_s, cfg.source.use_opc)
        _, off2 = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        floor = max(off1, off2)                    # worst-case ambient/floor bracketing the tone
        delta = on - floor
        rows.append({"f_hz": f_hz, "tx_off_dbm": floor, "tx_off1_dbm": off1,
                     "tx_off2_dbm": off2, "tx_on_dbm": on,
                     "delta_db": delta, "couples": delta >= guard_db})
    n_couple = sum(r["couples"] for r in rows)
    return {
        "rows": rows, "n": len(rows), "n_couple": n_couple,
        "guard_db": guard_db,
        "max_ambient_dbm": max((r["tx_off_dbm"] for r in rows), default=None),
        "verdict": "PATH-LIVE" if n_couple > 0 else "NO-COUPLING",
    }


def chain_sweep(cfg, source, analyzer, bench=None, guard_db=6.0, settle_s=None, on_point=None):
    """Validated emitted-vs-received ramp UP the preset bands -- the "chain" continuity check.

    Walks EVERY preset-band frequency low -> high (cfg.frequencies(): DC_TO_40GHZ_BANDS or
    DEFAULT_BANDS). Per point, in the standards-ordered sequence:
      1. command the TX to THIS band's setpoint (freq + the band's leveled power),
      2. read the RX source-OFF floor,
      3. turn the TX on and CONFIRM it is transmitting AT the setpoint BEFORE the RX integrates
         -- leveled+locked via the native OSB handshake ONCE per band, then a fixed settle dwell
         per point within the band (the 683xx retunes/settles in < 40 ms per datasheet, so paying
         the ~85 ms OSB round-trip every point is wasted -- the source-bus speed lever),
      4. read the RX tone, TX off,
      5. validate that what was EMITTED was RECEIVED: delta = tone - floor >= guard_db.
    Rolls the result up PER BAND + overall, so you can watch the chain carry the tone from low to
    high and see exactly where (if anywhere) it drops.

    This is a chain-CONTINUITY gate (emitted-vs-received), NOT an SE number -- it precedes the SE
    acceptance campaign (acquire_reference/measure_wall) and shares its per-point ordering rigor.

    settle_s defaults to cfg.source.settle_s; assumes a MATCHED TX/RX antenna pair (same gain both
    ends) as the substitution method requires. Returns {rows, bands, n, n_couple, guard_db,
    chain_live, verdict}. verdict: CHAIN-LIVE (every point carried) / PARTIAL / NO-COUPLING.
    """
    settle_s = cfg.source.settle_s if settle_s is None else settle_s
    if hasattr(source, "prepare"):
        source.prepare()
    if hasattr(analyzer, "prepare"):
        analyzer.prepare()
    analyzer.configure(cfg.analyzer.rbw_hz, cfg.analyzer.vbw_hz,
                       cfg.analyzer.ref_level_dbm, cfg.analyzer.detector)
    if bench is not None:
        bench.wall_present = False
    rows = []
    cur_band = None
    for i, (f_hz, band) in enumerate(cfg.frequencies()):
        if bench is not None:                                  # sim: use THIS band's horn/DANL
            bench.gain = band.antenna_gain_dbi
            bench.danl = band.danl_dbm_per_hz
        new_band = band.name != cur_band
        cur_band = band.name
        _set_source(source, f_hz, band.source_power_dbm)       # emitted setpoint (power + CW freq)
        source.rf_off()
        _, floor = analyzer.measure_floor(f_hz, cfg.analyzer.settle_s)
        source.rf_on()
        # ORDERING INVARIANT: the TX must be confirmed transmitting at the setpoint BEFORE the RX
        # integrates. OSB leveled+locked handshake at each BAND start; fixed dwell within the band.
        if new_band:
            source.await_settled(settle_s, cfg.source.use_opc)
        else:
            source.settle(settle_s)
        _, tone = analyzer.measure_peak(f_hz, cfg.analyzer.settle_s)
        source.rf_off()
        delta = tone - floor
        row = {
            "band": band.name, "f_hz": f_hz,
            "src_power_dbm": band.source_power_dbm,            # KNOWN emitted (calibrated) power
            "floor_dbm": floor, "tone_dbm": tone, "delta_db": delta,
            "coupling_db": tone - band.source_power_dbm,       # RX-received relative to emitted
            "couples": delta >= guard_db,
            "settle_confirmed": new_band,                      # True where OSB was re-confirmed
        }
        rows.append(row)
        if on_point is not None:
            on_point(i, row)
    # per-band roll-up (in first-seen order)
    bands = {}
    for r in rows:
        b = bands.get(r["band"])
        if b is None:
            b = {"band": r["band"], "n": 0, "n_couple": 0,
                 "f_lo_hz": r["f_hz"], "f_hi_hz": r["f_hz"]}
            bands[r["band"]] = b
        b["n"] += 1
        b["n_couple"] += int(r["couples"])
        b["f_lo_hz"] = min(b["f_lo_hz"], r["f_hz"])
        b["f_hi_hz"] = max(b["f_hi_hz"], r["f_hz"])
    n_couple = sum(r["couples"] for r in rows)
    all_carry = bool(rows) and n_couple == len(rows)
    return {
        "rows": rows, "bands": list(bands.values()),
        "n": len(rows), "n_couple": n_couple, "guard_db": guard_db,
        "chain_live": all_carry,
        "verdict": "CHAIN-LIVE" if all_carry else ("PARTIAL" if n_couple > 0 else "NO-COUPLING"),
    }


# ----------------------------------------------------------------- calibration (reference pass)

CAL_SCHEMA = "se299-calibration/1"


def calibration_summary(reference, floor_guard_db=3.0):
    """Roll up a reference pass into a CALIBRATION quality report.

    A calibration is the reference pass captured in a given geometry (here: both antennas inside
    the enclosure). It records, per f, the KNOWN TX power (calibrated source-output level), the
    RX-measured reference level, the noise floor, the through-path coupling (ref - TX), and the
    EA8 dynamic-range capability (ref - floor - margin).

    usable[i] is True only when that point's reference sits above the noise floor by
    floor_guard_db -- i.e. the RX actually SEES the tone, so the reference is a real datum rather
    than a noise reading. `status`:
      USABLE            every point is above the floor (a trustworthy reference)
      PARTIAL           some points above, some at the floor
      FLOOR-LIMITED     NO point rises above the floor -- the reference is at the noise floor
                        everywhere, so it can only bound SE (SE >= capability), never certify it.
                        This is the expected result for a weak/unreliable in-enclosure link.
    TX cancels in SE = reference - wall, so a floor-limited calibration still yields valid SE
    LOWER BOUNDS; it just cannot confirm a number deeper than its capability."""
    rows = [reference[k] for k in sorted(reference)]
    marks = [(r["ref_dbm"] - r["floor_dbm"]) >= floor_guard_db for r in rows]
    n_up = sum(marks)
    if n_up == len(rows) and rows:
        status = "USABLE"
    elif n_up == 0:
        status = "FLOOR-LIMITED"
    else:
        status = "PARTIAL"
    couplings = [r["coupling_db"] for r in rows if (r["ref_dbm"] - r["floor_dbm"]) >= floor_guard_db]
    worst_cap = min(rows, key=lambda r: r["capability_db"]) if rows else None
    return {
        "schema": CAL_SCHEMA,
        "n_points": len(rows),
        "n_above_floor": n_up,
        "status": status,
        "floor_guard_db": floor_guard_db,
        "src_power_dbm": rows[0]["src_power_dbm"] if rows else None,
        "median_coupling_db": (round(sorted(couplings)[len(couplings) // 2], 2)
                               if couplings else None),
        "worst_capability_db": round(worst_cap["capability_db"], 2) if worst_cap else None,
        "worst_capability_f_hz": worst_cap["f_hz"] if worst_cap else None,
        "ea8_ok_all": all(r["ea8_ok"] for r in rows) if rows else False,
    }


def write_calibration(path, cfg, reference, summary, timestamp="", note=""):
    """Persist a calibration (reference pass + quality summary) as one JSON file, loadable later
    as the reference for a wall/measurement pass. TX cancels in SE, but the known TX power is
    recorded for audit + link-budget cross-checks."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    doc = {
        "schema": CAL_SCHEMA,
        "git_commit": _git_commit(),
        "timestamp": timestamp,
        "note": note,
        "settings_key": list(cfg.settings_key()[:5]),
        "summary": summary,
        "reference": [reference[k] for k in sorted(reference)],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return path


def load_calibration(path):
    """Load a calibration JSON back into the integer-indexed `reference` dict that measure_wall
    consumes. Raises if the schema tag is missing/wrong (never silently trust a foreign file)."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schema") != CAL_SCHEMA:
        raise AcquisitionRejected(
            f"{path}: not an se299 calibration (schema={doc.get('schema')!r})")
    return {i: row for i, row in enumerate(doc["reference"])}
