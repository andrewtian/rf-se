"""se299 live SE Coordinator -- the role instance that owns BOTH instruments at once.

The SE-coordinator role holds the RX analyzer (an AnalyzerLink) AND the TX source (a
SourceLink), takes exclusive control of each, and drives them in lockstep for the IEEE-299
substitution measurement: SE(f) = reference(f) - wall(f), stepped-CW, source-tracked. It
streams a LIVE SE figure per wall point so the worst-case SE is known WHILE both units
operate concurrently (R8).

Exclusive control is a bus lease when the instrument is networked (NetworkTransport.lease);
it is a no-op for a sim or local-VISA driver, which has no lease surface -- so the same
Coordinator runs hardware-free in tests and arbitrated on the real bench.

This is the modular coordinator of R9: it speaks only the abstract RX/TX link contract
(ensure / .analyzer / .source) plus loop.py's measurement primitives, so any registered
analyzer/source pair -- not just the 8565EC/68369A -- is driven unchanged.
"""
from __future__ import annotations

import contextlib
import time

import control_lease
import drivers
import loop


class CoordinatorNotReady(RuntimeError):
    """A campaign was requested but the RX and/or TX link is not READY."""


class PathNotLive(RuntimeError):
    """The RF path is DEAD: the TX tone never rose above the RX floor at the pre-gate frequencies, so
    a substitution campaign would report SE ~= 0 everywhere (a loose source-out cable / disconnected
    antenna feed reads as 'infinite shielding'). The campaign is BLOCKED before the reference pass
    rather than run to produce meaningless numbers. `result` carries the loop.check_path dict (per-
    frequency tx_off/tx_on/delta + max_ambient) to localize which side is broken."""

    def __init__(self, result):
        self.result = result
        n, tot = result.get("n_couple", 0), result.get("n", 0)
        amb = result.get("max_ambient_dbm")
        amb_s = f"; max TX-off ambient {amb:.1f} dBm" if amb is not None else ""
        super().__init__(
            f"RF path NOT LIVE: the TX tone coupled at {n}/{tot} pre-gate frequencies{amb_s}. A "
            "substitution campaign was blocked (a dead path reports SE ~= 0 as infinite shielding). "
            "Check the source-out cable + antenna feed, then run `checkpath` before the campaign.")


class AnalyzerWedged(RuntimeError):
    """The 8565EC analyzer is WEDGED (reference/LO lock-loss + halted acquisition) -- the marginal
    precision-reference defect. A measurement was BLOCKED rather than allowed to emit numbers off a
    frozen trace (which would be silently stale garbage). `ref_codes` carries the reference-unlock
    codes seen (subset of drivers.REFERENCE_UNLOCK_CODES); `sweeping` is the live-sweep verdict.
    Recovery is a PHYSICAL power-cycle (GPIB cannot clear it); localize with
    tools/diagnose_8565ec.py --external-ref and see audit/2026-07-03-gpib-low-level-audit.md."""

    def __init__(self, ref_codes, sweeping):
        self.ref_codes = list(ref_codes)
        self.sweeping = bool(sweeping)
        detail = (f"reference lock-loss codes {sorted(self.ref_codes)}" if self.ref_codes
                  else "acquisition FROZEN (sweep not re-acquiring)")
        super().__init__(
            f"8565EC analyzer WEDGED: {detail}. A measurement was blocked to avoid emitting stale "
            "numbers off a frozen trace. Power-cycle the analyzer, then confirm with "
            "tools/diagnose_8565ec.py (ERR?=[111], sweep live) before measuring. Localize the fault "
            "with tools/diagnose_8565ec.py --external-ref; see audit/2026-07-03-gpib-low-level-audit.md.")


class ControlConflict(RuntimeError):
    """take_control was refused because a RIVAL client already holds one of the two instruments.

    Raised BEFORE any bus setup op and AFTER rolling back any lease this call took, so a refused
    take_control leaves NOTHING leased (all-or-nothing, the same discipline as
    instrument_hub.acquire_both). The message names WHICH instrument is contended (RX or TX) and
    WHO holds it, so a caller gets a clean, specific error instead of an opaque mid-write abort."""

    def __init__(self, instrument, who):
        self.instrument = instrument
        self.who = who
        super().__init__(f"{instrument} controlled by {who}")


class LiveSEFigure:
    """Running SE figure, updated per wall point during operation (R8).

    The campaign SE is the WORST-CASE (minimum) reported SE across points. A floor-limited
    point contributes a LOWER BOUND (the true SE is at least its reported value), tracked as
    `lower_bound`. Because the worst case is a running minimum, successive snapshots are
    monotonically non-increasing -- a live number that only ever tightens downward.
    """

    def __init__(self):
        self.points = 0
        self.worst_se_db = None
        self.worst_band = ""
        self.worst_f_hz = None
        self.lower_bound = False
        self.any_fail = False

    def update(self, row):
        self.points += 1
        se = row["se_reported_db"]
        if self.worst_se_db is None or se < self.worst_se_db:
            self.worst_se_db = se
            self.worst_band = row["band"]
            self.worst_f_hz = row["f_hz"]
            self.lower_bound = bool(row["floor_limited"])
        if row.get("verdict") == "FAIL":
            self.any_fail = True

    def figure(self):
        """Snapshot of the worst case so far. `lower_bound` True means the limiting point is
        floor-limited, so the real SE is at least `se_db` (i.e. SE >= se_db)."""
        return {"se_db": self.worst_se_db, "lower_bound": self.lower_bound,
                "band": self.worst_band, "f_hz": self.worst_f_hz,
                "points": self.points, "any_fail": self.any_fail}


class Coordinator:
    """Owns an RX AnalyzerLink and a TX SourceLink and runs the substitution campaign over
    the pair, with auto-reconnect readiness, exclusive control, and a live SE figure."""

    def __init__(self, cfg, rx_link, tx_link, lease_ttl_s: float = control_lease.DEFAULT_LEASE_TTL_S,
                 heartbeat_timeout_s=None, vm_spec=None):
        self.cfg = cfg
        self.rx = rx_link
        self.tx = tx_link
        self.lease_ttl_s = lease_ttl_s
        # UNATTENDED SAFETY (opt-in): when heartbeat_timeout_s is set, the measurement loop calls
        # beat() each point; a HUNG loop (beats stop) lets the keepalive lapse -> the dead-man de-keys
        # the source. Default None = disabled (attended use is covered by the operator + the shorter
        # idle_s). Must EXCEED the longest inter-beat gap -- one point measurement, up to ~30 s for a
        # high-band preselector-peak point, plus the pre_check_path probe -- so a healthy slow point
        # never false-stalls; ~90-120 s is a sound floor.
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self._control_depth = 0            # re-entrancy for controlled(): lease on 0->1, release on 1->0
        self._last_beat = None
        hb = (lambda: self._last_beat) if heartbeat_timeout_s else None
        # ONE ControlLease per link is the single home of lease + keepalive + release (Phase 3):
        # take_control acquires both, release_control releases both -- no bespoke keepalive here.
        self._rx_lease = control_lease.ControlLease(self.rx, ttl_s=lease_ttl_s,
                                                    heartbeat=hb, heartbeat_timeout_s=heartbeat_timeout_s)
        self._tx_lease = control_lease.ControlLease(self.tx, ttl_s=lease_ttl_s,
                                                    heartbeat=hb, heartbeat_timeout_s=heartbeat_timeout_s)
        # 44.2: a LOCAL --vm owner wires soft recovery (de-key + QMP virtual-replug) into each link's
        # FAULT threshold. None (a remote net: owner or sim) leaves recover_fn None -> HARD-alert only,
        # since the in-guest bridge cannot reach the host QMP socket.
        self._vm_spec = vm_spec
        if vm_spec is not None:
            self._wire_soft_recovery(vm_spec)

    # -- abstract RX/TX handles (loop.py drives the DRIVER objects) --------------
    @property
    def analyzer(self):
        return self.rx.analyzer

    @property
    def source(self):
        return self.tx.source

    # -- readiness + exclusive control ------------------------------------------
    def ensure_ready(self) -> bool:
        """Both links READY (each auto-reconnects on the way). Returns bool."""
        return bool(self.rx.ensure() and self.tx.ensure())

    def _require_ready(self):
        if not self.ensure_ready():
            raise CoordinatorNotReady(
                f"RX {self.rx.status().state} / TX {self.tx.status().state}: "
                f"both links must be READY to run a campaign")

    # -- analyzer health gate (the 8565EC reference/LO wedge) --------------------
    def analyzer_health(self) -> dict:
        """Read the analyzer's live health WITHOUT starting a measurement: the reference-unlock
        error codes present now (subset of drivers.REFERENCE_UNLOCK_CODES) and whether the trace is
        actively re-acquiring (_sweep_is_live, Task 1). `healthy` is True only when NEITHER a
        reference-unlock code is queued NOR the sweep is frozen. On the sim/base analyzer this is
        always healthy (no wedge failure mode). Best-effort: a bus error reading health does not mask
        as healthy -- it surfaces as not-sweeping."""
        ana = self.analyzer
        try:
            codes = ana.query_errors()
        except Exception:                                     # noqa: BLE001 -- treat unreadable as suspect
            codes = []
        ref = [c for c in codes if c in drivers.REFERENCE_UNLOCK_CODES]
        try:
            sweeping = ana._sweep_is_live()
        except Exception:                                     # noqa: BLE001 -- unreadable trace = not-sweeping
            sweeping = False
        return {"healthy": (not ref) and bool(sweeping), "ref_codes": ref, "sweeping": bool(sweeping)}

    def _require_healthy_analyzer(self):
        """Raise AnalyzerWedged if the analyzer is in the reference/LO wedge, so a measurement HALTS
        instead of emitting stale numbers off a frozen trace. Called alongside _require_ready at the
        head of every measurement primitive."""
        h = self.analyzer_health()
        if not h["healthy"]:
            raise AnalyzerWedged(h["ref_codes"], h["sweeping"])

    def take_control(self):
        """Lease BOTH instruments (RX then TX, canonical order) so no other role instance perturbs
        the bus mid-campaign; each ControlLease also RENEWS its lease at ttl/3 while control is held
        -- a campaign longer than the TTL must not silently lose exclusivity. Networked -> real
        exclusive leases; sim/VISA -> no-op that always succeeds.

        ALL-OR-NOTHING (mirrors instrument_hub.acquire_both): if a rival holds one instrument, RELEASE
        the lease THIS call just took and raise ControlConflict('<RX|TX> controlled by <who>') BEFORE
        any bus setup op -- so a refused acquire never strands a half-held lease and never fails
        opaquely at the first refused source write. Returns None on success; raises on conflict with
        nothing left leased.

        HALTS on the analyzer wedge FIRST (before leasing): if the 8565EC is in the reference/LO
        lock-loss wedge, raise AnalyzerWedged rather than lease + drive a frozen analyzer into
        emitting stale numbers. Checked before any lease so a wedge never strands a held lease."""
        self._require_ready()
        self._require_healthy_analyzer()
        rx_was = self._rx_lease.held()
        rx_ok, rx_who = self._rx_lease.acquire()
        if not rx_ok:
            raise ControlConflict("RX", rx_who)
        tx_ok, tx_who = self._tx_lease.acquire()
        if not tx_ok:
            if not rx_was:                                    # roll back ONLY the RX lease THIS call took
                self._rx_lease.release()
            raise ControlConflict("TX", tx_who)
        self.beat()                                           # fresh liveness before the loop starts

    def beat(self):
        """Signal main-loop liveness for the unattended-safety heartbeat. The measurement loop calls
        this each point while control is held; if beats STOP (a hung loop), the keepalive lapses and
        the dead-man de-keys the source. No-op unless heartbeat_timeout_s was set at construction."""
        if self.heartbeat_timeout_s:
            self._last_beat = time.monotonic()

    def _pause_beat(self):
        """PAUSE the heartbeat during a legitimate operator wait (the shield-insertion prompt), so a
        deliberately-paused campaign is never de-keyed. beat() resumes it. No-op when disabled."""
        self._last_beat = None

    def release_control(self):
        # GUARANTEED RF-OFF backstop FIRST, while we STILL hold the lease: even if a measurement
        # primitive was bypassed or raised before its own try/finally, the source must not keep
        # radiating past control release. rf_off writes over the bus, so it must precede releasing.
        try:
            src = self.source
            if src is not None:
                src.rf_off()
        except Exception:                                     # noqa: BLE001 -- best-effort
            pass
        # each ControlLease stops+joins its keepalive BEFORE sending U, so a finishing renew can
        # never re-grab the lease and steal control back from the next taker.
        self._rx_lease.release()
        self._tx_lease.release()

    @contextlib.contextmanager
    def controlled(self):
        """Hold exclusive control for a block, RE-ENTRANTLY. The OUTERMOST enter leases both
        instruments (take_control); the OUTERMOST exit releases them (release_control, which does the
        guaranteed RF-off backstop FIRST). A nested `with self.controlled():` inside an already-held
        block does NOT re-lease and does NOT early-release -- so a pre-gate that holds control and then
        calls a primitive which also wants control keeps ONE lease, and the RF-off backstop fires
        exactly once, on the outer exit. This is the single home of the take/release discipline that
        walkaround / sweep / run_campaign share."""
        if self._control_depth == 0:
            self.take_control()
        self._control_depth += 1
        try:
            yield
        finally:
            self._control_depth -= 1
            if self._control_depth == 0:
                self.release_control()

    # -- 44.2 supervised soft recovery (local --vm owner) -----------------------
    def _dekey_source_best_effort(self):
        """De-key the source, tolerating a down link -- if the SOURCE's own adapter is the wedged one the
        write may not land, and the bridge dead-man (task #47) is then the real backstop. Mirrors
        release_control's guaranteed RF-off-first."""
        try:
            src = self.source
            if src is not None:
                src.rf_off()
        except Exception:                                 # noqa: BLE001 -- best-effort
            pass

    def _wire_soft_recovery(self, vm_spec, budget: int = 2):
        """Wire each link's recover_fn so a would-be terminal FAULT first attempts a supervised soft
        recovery: de-key the source, then QMP virtual-replug THIS adapter up to `budget` and revalidate
        via the link's side-effect-free probe. RX = the B adapter ('analyzer'); TX = the HS adapter
        ('source'). vm is imported LAZILY so the sim/test path never loads the qemu bridge module. The
        source de-key runs before EITHER adapter's replug -- the source never radiates across a
        re-enumeration."""
        from gpib_bridge import vm as vmmod
        import recovery
        for link, which in ((self.rx, "analyzer"), (self.tx, "source")):
            def _recover(exc, spec=vm_spec, which=which, link=link):
                out = recovery.soft_recover(
                    dekey_fn=self._dekey_source_best_effort,
                    replug_fn=lambda: vmmod.attach_adapter(spec, which),
                    reachable_fn=link.probe_alive,
                    budget=budget)
                return out.recovered
            link._recover_fn = _recover

    # -- measurement primitives (streaming) -------------------------------------
    def check_path(self, freqs_hz, bench=None, guard_db=6.0):
        """RF-path go/no-go self-test over the pair (see loop.check_path)."""
        self._require_ready()
        self._require_healthy_analyzer()
        return loop.check_path(self.cfg, self.source, self.analyzer, freqs_hz,
                               bench=bench, guard_db=guard_db)

    def chain(self, bench=None, guard_db=6.0, settle_s=None, on_point=None):
        """Validated emitted-vs-received ramp UP the preset bands over the pair (see
        loop.chain_sweep) -- the chain-continuity gate that precedes an SE campaign."""
        self._require_ready()
        self._require_healthy_analyzer()
        return loop.chain_sweep(self.cfg, self.source, self.analyzer, bench=bench,
                                guard_db=guard_db, settle_s=settle_s, on_point=on_point)

    def walkaround(self, freq_hz, on_frame, should_stop, bench=None, use_average=False,
                   power_dbm=None):
        """Live near-field-probe walkaround over the pair: source CW on at freq_hz (power_dbm
        overrides the band default), analyzer reads the probe in a loop until should_stop() (see
        loop.nearfield_walkaround). Holds exclusive control for the duration; releases (and RF-off)
        even if a read raises."""
        with self.controlled():
            return loop.nearfield_walkaround(self.cfg, self.source, self.analyzer, freq_hz,
                                             on_frame, should_stop, bench=bench,
                                             use_average=use_average, power_dbm=power_dbm)

    def sweep(self, freqs_hz=None, wall=False, bench=None):
        """INSTANCE-1-DRIVES a source-tracked sweep across the pair: the analyzer is read at each
        frequency while the TX SOURCE (on the other networked instance) is retuned to FOLLOW every
        point through the stack (control-plane -> net transport -> bridge -> GPIB -> source). This
        is the two-instance sweep -- instance 1 (this coordinator, RX side) drives; instance 2 (TX)
        follows. freqs_hz defaults to the FULL cfg plan (e.g. DC_TO_40GHZ_BANDS -> 10 MHz..40 GHz).
        Holds exclusive control for the sweep; the source is left OFF. Returns loop.stepped_cw_sweep's
        result (freqs_hz, levels_dbm, hot bin, source_tracked=True)."""
        with self.controlled():
            freqs = [f for f, _ in self.cfg.frequencies()] if freqs_hz is None else list(freqs_hz)
            return loop.stepped_cw_sweep(self.cfg, self.source, self.analyzer, freqs,
                                         bench=bench, wall=wall)

    def acquire_reference(self, bench=None, on_point=None):
        self._require_ready()
        self._require_healthy_analyzer()
        return loop.acquire_reference(self.cfg, self.source, self.analyzer,
                                      bench=bench, on_point=on_point)

    def measure_wall(self, reference, bench=None, on_point=None):
        self._require_ready()
        self._require_healthy_analyzer()
        return loop.measure_wall(self.cfg, self.source, self.analyzer, reference,
                                 bench=bench, on_point=on_point)

    def recheck_reference(self, reference, bench=None, tol_db=3.0, on_point=None):
        """Baseline-drift re-check over the pair (loop.reference_drift, methodology point 4):
        re-measure the 0 dB reference and flag any point that drifted more than +/-tol_db since
        the reference pass -- the substitution SE is only valid if the baseline held still. Run it
        AFTER the wall pass (or periodically); a DRIFTED verdict means repeat the run. Requires
        READY + a healthy analyzer, like the other measurement primitives."""
        self._require_ready()
        self._require_healthy_analyzer()
        return loop.reference_drift(self.cfg, self.source, self.analyzer, reference,
                                    bench=bench, tol_db=tol_db, on_point=on_point)

    def run_campaign(self, bench=None, on_se_update=None, on_reference_point=None,
                     on_shield_prompt=None, health_every=0, pre_check_path=False,
                     check_path_guard_db=6.0):
        """Full substitution campaign (reference pass, then wall pass), source-tracked, with
        exclusive control held for the duration.

        `on_se_update(figure, row)` fires after each WALL point with the live SE figure (R8);
        `on_reference_point(i, row)` fires per reference point. `on_shield_prompt()`, if given,
        fires ONCE between the reference pass and the wall pass -- the substitution method
        requires a PHYSICAL SHIELD to be inserted there; a caller (GUI/CLI) uses this hook to
        pause and prompt the operator. It runs INSIDE the exclusive-control hold, so a raising
        callback aborts the campaign cleanly with control released (the `finally` below still
        runs). Default None preserves the prior behavior (no shield step). Returns a dict with
        the summary, both passes, and the final SE figure. Control is released even if a pass
        or the callback raises.

        `health_every` (default 0 = off) re-checks analyzer health every N WALL points and raises
        AnalyzerWedged the moment the 8565EC enters the reference/LO wedge mid-run, rather than
        streaming stale points until the next frozen read trips Task 1's guard. The partial result
        is the rows already delivered through `on_se_update` before the abort; control is released
        by the `finally`. (Task 1's per-read fresh-sweep guard is the always-on backstop; this is an
        earlier, code-naming signal for a long unattended wall pass.)
        """
        fig = LiveSEFigure()

        def wall_point(i, row):
            self.beat()                                       # liveness: this wall point completed
            fig.update(row)
            if on_se_update is not None:
                on_se_update(fig.figure(), row)
            if health_every and (i + 1) % health_every == 0:
                h = self.analyzer_health()
                if not h["healthy"]:
                    raise AnalyzerWedged(h["ref_codes"], h["sweeping"])

        with self.controlled():
            if pre_check_path:
                # PRE-GATE: prove the TX tone actually reaches the RX before spending a full campaign.
                # A dead path (loose source-out / disconnected feed) otherwise reads SE ~= 0 as if the
                # shield were perfect. Probe a representative subset (first / middle / last point) so
                # the gate is quick; NO-COUPLING there -> abort with PathNotLive before acquire_reference.
                all_f = [f for f, _ in self.cfg.frequencies()]
                probe = sorted(set([all_f[0], all_f[len(all_f) // 2], all_f[-1]])) if all_f else []
                cp = loop.check_path(self.cfg, self.source, self.analyzer, probe, bench=bench,
                                     guard_db=check_path_guard_db)
                if cp["verdict"] != "PATH-LIVE":
                    raise PathNotLive(cp)
            def ref_point(i, row):
                self.beat()                                   # liveness: this reference point completed
                if on_reference_point is not None:
                    on_reference_point(i, row)
            reference = self.acquire_reference(bench=bench, on_point=ref_point)
            if on_shield_prompt is not None:
                self._pause_beat()                            # operator is inserting the shield: PAUSE
                try:                                          # the heartbeat so the pause never de-keys
                    on_shield_prompt()
                finally:
                    self.beat()                               # resume liveness for the wall pass
            wall = self.measure_wall(reference, bench=bench, on_point=wall_point)
        return {"summary": loop.summarize(reference, wall),
                "reference": reference, "wall": wall, "se_figure": fig.figure()}
