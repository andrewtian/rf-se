"""Hardware-free tests that the drivers send the CORRECT instrument command strings, per the
persisted manuals (reference/operator-manuals/anritsu-68000-series-operation.md +
agilent-8560e-users-guide.md) and the DEVICE_OPERATION_AUDIT.md. Uses a fake transport that
records writes and scripts query replies -- no hardware.

Run:  uv run python -m pytest rf-se/se299/tests/test_device_operation.py -q
"""
from __future__ import annotations

import math
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drivers


class FakeT:
    def __init__(self, replies=None):
        self.writes, self.queries = [], []
        self.timeout_ms = None
        self._replies = dict(replies or {})

    def write(self, cmd):
        self.writes.append(cmd)

    def _raw(self, cmd):
        if cmd in self._replies:
            v = self._replies[cmd]
        elif cmd.startswith("TRA?") or cmd.startswith("TRB?"):
            # DEFAULT trace = a LIVE (changing) 601-point sweep, so Agilent856xEC._sweep_is_live() is
            # True by default (a healthy analyzer). Successive calls differ (fresh-sweep noise), so the
            # fresh-sweep guard passes. A test wanting a FROZEN/wedged analyzer scripts a FIXED "TRA?".
            self._tra_n = getattr(self, "_tra_n", 0) + 1
            v = ",".join(f"{-100.0 + ((i + self._tra_n) % 5) * 0.2:.1f}" for i in range(601))
        else:
            v = next((val for k, val in self._replies.items() if cmd.startswith(k)), "0")
        return v.encode("latin-1", "replace") if isinstance(v, str) else bytes(v)

    def query_raw(self, cmd):
        # FAITHFUL to NetworkTransport.query_raw: the RAW response bytes, no decode/strip.
        self.queries.append(cmd)
        return self._raw(cmd)

    def query(self, cmd):
        # FAITHFUL to NetworkTransport.query: bytes -> .decode('ascii','replace') -> .strip(). This
        # REPRODUCES the binary-status corruption (0x0C form-feed -> '' etc.), so a code path that
        # wrongly reads a binary byte via query() is caught by the tests instead of silently passing.
        self.queries.append(cmd)
        return self._raw(cmd).decode("ascii", "replace").strip()

    def set_timeout(self, ms):
        self.timeout_ms = ms

    def reconnect(self):
        self.reconnected = getattr(self, "reconnected", 0) + 1

    def close(self):
        pass


# A 601-point TRA? trace with a strong tone (one -10 dBm spike over a -100 dBm floor). peak_preselector's
# real-tone guard reads the trace and only peaks the YIG when peak-over-floor clears its margin, so a test
# exercising the PP sequence must present a tone (the FakeT default flat-noise trace = "no tone" -> skip).
_TONE_TRACE = ",".join(["-100.0"] * 300 + ["-10.0"] + ["-100.0"] * 300)


# ------------------------------------------------------------- source (Anritsu 68000-series)

def test_source_prepare_sends_known_good_clearing_string():
    t = FakeT()
    drivers.Anritsu68369(t).prepare()
    # rules out all 6 "leveled but no RF" software modes (anritsu-68000-series-operation.md)
    assert t.writes == ["RST", "IL1", "AT0", "ATT00", "TR0", "LO0", "LOG"]


def test_source_set_freq_uses_CF1_not_CW1():
    t = FakeT()
    drivers.Anritsu68369(t).set_freq(5e9)
    assert t.writes == ["CF1 5.000000000 GH"]           # CF1 = CW mode + value; NOT CW1
    assert not any("CW1" in w for w in t.writes)


def test_source_level_and_rf_mnemonics():
    t = FakeT()
    sg = drivers.Anritsu68369(t)
    sg.set_power(12.5); sg.rf_on(); sg.rf_off()
    assert t.writes == ["L1 12.50 DM", "RF1", "RF0"]


def test_source_settled_ok_reads_osb_leveled_locked():
    # OSB bit2 = RF Unleveled, bit3 = Lock Error; settled_ok True iff both clear
    assert drivers.Anritsu68369(FakeT({"OSB": chr(0x00)})).settled_ok() is True
    assert drivers.Anritsu68369(FakeT({"OSB": chr(0x04)})).settled_ok() is False   # unleveled
    assert drivers.Anritsu68369(FakeT({"OSB": chr(0x08)})).settled_ok() is False   # lock error


def test_source_native_readbacks():
    sg = drivers.Anritsu68369(FakeT({"OF1": "5000.0", "OL1": "12.00"}))
    assert sg.output_freq_mhz() == 5000.0
    assert sg.output_level_dbm() == 12.0


def test_source_idn_queries_star_idn():
    t = FakeT({"*IDN?": "ANRITSU,68367C,070326,2.35"})
    assert "68367C" in drivers.Anritsu68369(t).idn()
    assert t.queries == ["*IDN?"]                        # native-mode unit answers *IDN? only


def test_source_idn_prefers_star_idn_when_answered():
    # ISSUE 4: *IDN? is the richer "683"-matchable string; when the unit answers it, use it verbatim.
    t = FakeT({"*IDN?": "ANRITSU,68367C,070326,2.35"})
    assert drivers.Anritsu68369(t).idn() == "ANRITSU,68367C,070326,2.35"


class _PoisonIDN(FakeT):
    """a source whose firmware does NOT answer *IDN? -- the query raises (times out / poisons)."""
    def query(self, cmd):
        if cmd == "*IDN?":
            raise TimeoutError("cannot read from timed out object")
        return super().query(cmd)


def test_source_idn_falls_back_to_native_OI_without_poisoning():
    # ISSUE 4: idn() is a LIVENESS probe; on firmware lacking *IDN? it must NOT die -- it reconnects
    # (clears the poisoned socket) and returns the native OI identity instead.
    t = _PoisonIDN({"OI": "6867 0.0140.00-120.0 3.02.35070326C3"})
    idn = drivers.Anritsu68369(t).idn()
    assert idn == "6867 0.0140.00-120.0 3.02.35070326C3"
    assert getattr(t, "reconnected", 0) >= 1               # cleared the poisoned socket before OI


class _ErrSeq(FakeT):
    """ERR? returns a scripted sequence (models the read-clears-but-chronic-codes-re-enter queue)."""
    def __init__(self, seq):
        super().__init__(); self._seq = list(seq); self._i = 0

    def query(self, cmd):
        if cmd == "ERR?":
            v = self._seq[min(self._i, len(self._seq) - 1)]; self._i += 1; return v
        return super().query(cmd)


def test_analyzer_error_baseline_delta_flags_only_new_codes():
    # ISSUE 6 (audit F12): a chronically-sick 8565EC re-enters its LO/IF codes on every read, so the
    # loop A-V7 self-check perpetually FAILED. snapshot_error_baseline() captures the chronic set;
    # query_new_errors() then reports ONLY codes the measurement newly introduced.
    # seq: [clear, baseline-read (chronic), post-sweep-read (chronic + a NEW 901)]
    a = drivers.Agilent856xEC(_ErrSeq(["313,499", "313,499", "313,499,901"]))
    assert a.snapshot_error_baseline() == [313, 499]       # chronic baseline
    assert a.query_new_errors() == [901]                   # only the NEW code, chronic excluded


def test_analyzer_query_new_errors_without_baseline_is_all_codes():
    # with no snapshot taken, every code counts as new (== query_errors), so nothing is silently hidden
    a = drivers.Agilent856xEC(_ErrSeq(["361,313"]))
    assert a.query_new_errors() == [361, 313]


def test_source_status_byte_parses_binary_osb():
    # OSB returns a raw binary byte (NOT ascii); status_byte must read it raw, not via the text path
    assert drivers.Anritsu68369(FakeT({"OSB": chr(0x00)})).status_byte() == 0x00
    assert drivers.Anritsu68369(FakeT({"OSB": chr(0x0C)})).status_byte() == 0x0C  # unlev+lock


def test_source_status_byte_survives_ascii_strip_corruption():
    # REGRESSION (audit HIGH): OSB=0x0C (RF-unleveled 0x04 + lock-error 0x08 -- the WORST case) is
    # ascii form-feed. The text query() path decode/strips it to EMPTY -> status read as 0 ->
    # settled_ok() FALSELY reports leveled+locked, silently defeating the interlock exactly when both
    # faults are present. status_byte() must use query_raw so the real fault byte reaches the guard.
    for val in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20):        # all ascii-whitespace status values
        t = FakeT({"OSB": chr(val)})
        assert t.query("OSB") == ""                         # the text path DOES corrupt these to ''
        assert t.query_raw("OSB") == bytes([val])           # the raw path preserves the byte
        assert drivers.Anritsu68369(t).status_byte() == val  # driver reads the real byte via raw
    # and the worst case (both interlock faults set) is correctly reported NOT settled
    assert drivers.Anritsu68369(FakeT({"OSB": chr(0x0C)})).settled_ok() is False


def test_source_await_settled_default_uses_osb_handshake_not_opc():
    # C1: the default settle handshake is the native OSB read (leveled+locked), NEVER *OPC?
    # (the bench 68367C never answers *OPC? -- a timeout would poison the socket).
    t = FakeT({"OSB": chr(0x00)})                        # leveled+locked -> settles first poll
    drivers.Anritsu68369(t).await_settled(settle_s=0.0)  # use_opc default False
    assert "OSB" in t.queries                            # native completion handshake ran
    assert "*OPC?" not in t.queries                      # and *OPC? was NOT issued


def test_source_await_settled_opc_optin_falls_back_to_reconnect():
    # use_opc=True ADDITIONALLY issues *OPC?; a unit that does not answer it (raises) must
    # reconnect (not poison the socket), and the native OSB handshake still runs first.
    class _RaiseOnOpc(FakeT):
        def __init__(self):
            super().__init__({"OSB": chr(0x00)})
            self.reconnects = 0

        def query(self, cmd):
            if cmd == "*OPC?":
                self.queries.append(cmd)
                raise IOError("gpib bridge error: read timed out")
            return super().query(cmd)

        def reconnect(self):
            self.reconnects += 1

    t = _RaiseOnOpc()
    drivers.Anritsu68369(t).await_settled(settle_s=0.0, use_opc=True)
    assert "OSB" in t.queries and "*OPC?" in t.queries   # both the native handshake AND *OPC?
    assert t.reconnects == 1                             # *OPC? timeout -> clean reconnect, no poison


def test_source_settings_use_opc_defaults_false():
    import config
    assert config.SourceSettings().use_opc is False      # C1: native OSB handshake is the default


# ------------------------------------------------------------- analyzer (8565EC / 8560 E-series)

def test_analyzer_prepare_presets_and_flushes():
    t = FakeT()
    drivers.Agilent856xEC(t).prepare()
    # CONTS (continuous), NOT SNGLS: a single-sweep TS reads a STALE trace over the networked GPIB
    # bridge (live-proven); CONTS free-run + the flush sweep gives a fresh trace.
    assert t.writes == ["IP", "CONTS", "TS"]             # IP preset + continuous + flush stale sweep
    assert "DONE?" in t.queries                          # sweep-complete sync on the flush


def test_preselector_peak_noop_below_2p9ghz():
    t = FakeT()
    dac = drivers.Agilent856xEC(t).peak_preselector(1e9)
    assert dac is None and t.writes == []                # band 0: no preselector, no commands


def test_preselector_peak_sequence_above_2p9ghz():
    t = FakeT({"PSDAC?": "131", "TRA?": _TONE_TRACE})   # a real tone is present -> PP proceeds
    ana = drivers.Agilent856xEC(t); ana._ARM_DWELL_S = 0
    dac = ana.peak_preselector(6e9)
    assert dac == 131
    # PP requires a nonzero high-band span + RBW>100Hz, mark the tone to center, then PP
    assert any(w.startswith("SP ") and w != "SP 0HZ" for w in t.writes)
    assert "MKPK HI" in t.writes and "MKCF" in t.writes and "PP" in t.writes
    assert t.writes.index("MKCF") < t.writes.index("PP")  # mark-to-center BEFORE peaking


def test_preselector_peak_skips_when_no_tone_present():
    # REAL-TONE GUARD: with only noise (no tone), peak_preselector must NOT run PP. Peaking the YIG onto
    # a noise bin mis-tunes it to REJECT that frequency and BLANKS the display until the next retune
    # (LIVE bug: peaking before the source settled blanked the read). It leaves CF + the preselector
    # UNTOUCHED and returns None, so the caller reads the (absent/low) tone -- never a self-inflicted blank.
    t = FakeT({"PSDAC?": "131"})                             # FakeT default trace = flat noise, no tone
    ana = drivers.Agilent856xEC(t); ana._ARM_DWELL_S = 0
    dac = ana.peak_preselector(6e9)
    assert dac is None
    assert "PP" not in t.writes and "MKCF" not in t.writes   # YIG + center left untouched


def test_set_preselector_dac_reapplies_and_sweeps():
    t = FakeT()
    drivers.Agilent856xEC(t).set_preselector_dac(131)
    assert "PSDAC 131" in t.writes and "TS" in t.writes   # hardware applies at end of sweep -> TS


# ------------------------------------------------------------- C2: two-pass metrology fixes

def test_measure_peak_no_longer_queries_marker_freq():
    # MINOR-6: MKF? is a wasted read -- every caller discards the first element (_, amp).
    # measure_peak now returns the COMMANDED frequency directly, with no MKF? round-trip.
    t = FakeT({"MKA?": "-42.5"})
    ana = drivers.Agilent856xEC(t); ana._ARM_DWELL_S = 0   # deterministic FakeT -> no real completion sleep
    f_hz, amp = ana.measure_peak(6e9, 0.0)
    assert f_hz == 6e9 and amp == -42.5
    assert "MKF?" not in t.queries


# --- Task 1: fresh-sweep-verified read. measure_peak must PROVE the trace re-swept (a live sweep)
# --- before trusting the marker; a FROZEN trace (the reference/LO wedge) must RAISE, never return a
# --- stale value. The stabilize loop alone cannot tell "settled" from "frozen" (both yield agreeing
# --- reads) -- this is what the user saw as "the graph not moving on the screen while freq changes".

def test_measure_peak_returns_marker_when_sweep_is_live():
    # a LIVE analyzer (FakeT's default TRA? changes each sweep) -> _sweep_is_live() True -> normal read.
    t = FakeT({"MKA?": "-42.5"})
    ana = drivers.Agilent856xEC(t)
    ana._SWEEP_LIVE_DWELL_S = 0.0                      # FakeT is deterministic (no bridge race) -> no dwell
    ana._ARM_DWELL_S = 0                               # ...and no completion-dwell sleep in the read path
    assert ana._sweep_is_live() is True
    f_hz, amp = ana.measure_peak(2e9, 0.0)
    assert f_hz == 2e9 and amp == -42.5


def test_sweep_is_live_detects_frozen_vs_live_trace():
    # the liveness signal: a FROZEN analyzer returns a byte-IDENTICAL trace every sweep (-> False); a
    # LIVE one dithers (FakeT's default TRA? changes each call -> True). This is the wedge signal the
    # health gate keys off, on the noisy floor.
    frozen = ",".join(["-100.0"] * 601)
    tf = FakeT({"TRA?": frozen})
    af = drivers.Agilent856xEC(tf); af._SWEEP_LIVE_DWELL_S = 0.0    # deterministic fixture -> no dwell
    assert af._sweep_is_live() is False

    tl = FakeT()                                        # default TRA? = a changing (live) trace
    al = drivers.Agilent856xEC(tl); al._SWEEP_LIVE_DWELL_S = 0.0
    assert al._sweep_is_live() is True


def test_require_live_sweep_raises_on_frozen_floor_only():
    # _require_live_sweep is the FLOOR-ONLY hard check (run RF off, before keying a tone). A frozen
    # floor -> AnalyzerNotSweeping; a live floor -> no raise. It is NOT called from measure_peak, because
    # a strong stable tone reads as a near-constant trace that would false-positive.
    frozen = ",".join(["-100.0"] * 601)
    tf = FakeT({"TRA?": frozen})
    af = drivers.Agilent856xEC(tf); af._SWEEP_LIVE_DWELL_S = 0.0
    with pytest.raises(drivers.AnalyzerNotSweeping):
        af._require_live_sweep(2e9)

    # measure_peak itself does NOT raise on a frozen fixture (guard removed) -- it returns the marker;
    # wedge detection is the floor health gate's job, not the per-read's.
    tp = FakeT({"TRA?": frozen, "MKA?": "-42.5"})
    ap = drivers.Agilent856xEC(tp); ap._SWEEP_LIVE_DWELL_S = 0.0; ap._ARM_DWELL_S = 0
    f_hz, amp = ap.measure_peak(2e9, 0.0)
    assert f_hz == 2e9 and amp == -42.5


def test_measure_peak_dwells_for_sweep_completion(monkeypatch):
    # THE 2026-07-06 FIX: measure_peak must DWELL after each TS so the sweep COMPLETES over the qemu GPIB
    # bridge before the marker read. Without it a configure()-CLRW-cleared trace reads the BLANK bottom
    # graticule and the stabilize loop "settles" on that constant blank -- the LIVE root cause of the
    # false floor / false NO-COUPLING (a no-dwell read reported 0 dB coupling where a dwelled read
    # recovered +21..24 dB on the bench 8565EC). Assert the completion dwell (>= _ARM_DWELL_S), sized off
    # the auto-coupled sweep time ST?, is issued after a continuous-sweep TS.
    slept = []
    monkeypatch.setattr(drivers.time, "sleep", lambda s: slept.append(s))
    t = FakeT({"MKA?": "-42.5", "ST?": "0.05"})           # 50 ms sweep -> dwell = max(0.12, 0.075) = 0.12
    ana = drivers.Agilent856xEC(t)
    f_hz, amp = ana.measure_peak(2e9, 0.0)
    assert f_hz == 2e9 and amp == -42.5
    assert any(q == "ST?" for q in t.queries)             # queried the sweep time to size the dwell
    assert "CONTS" in t.writes                            # bridge-reliable continuous sweep
    assert slept and max(slept) >= ana._ARM_DWELL_S       # dwelled >= the completion floor


def test_sweep_completion_dwell_tracks_sweep_time():
    # dwell >= the auto-coupled sweep time (1.5x margin), floored at _ARM_DWELL_S; unreadable ST? -> floor.
    ana_slow = drivers.Agilent856xEC(FakeT({"ST?": "0.40"}))    # 400 ms sweep -> 0.40 * 1.5 = 0.60 s
    assert abs(ana_slow._sweep_completion_dwell_s() - 0.60) < 1e-6
    ana_fast = drivers.Agilent856xEC(FakeT({"ST?": "0.01"}))    # 10 ms -> 0.015 < floor -> _ARM_DWELL_S
    assert ana_fast._sweep_completion_dwell_s() == ana_fast._ARM_DWELL_S
    ana_bad = drivers.Agilent856xEC(FakeT({"ST?": "garbage"}))  # unreadable ST? -> floor, never crash
    assert ana_bad._sweep_completion_dwell_s() == ana_bad._ARM_DWELL_S
    ana_stuck = drivers.Agilent856xEC(FakeT({"ST?": "2000"}))   # LIVE pathology: a stuck 2000 s sweep time
    assert ana_stuck._sweep_completion_dwell_s() == 5.0         # -> CAPPED at 5 s, never a 3000 s sleep


# --- Task 3: single-consumer enforcement. lease_exclusive() must acquire an exclusive device lease,
# --- and on a conflicting lease (IOError) raise SingleConsumerConflict carrying the bridge lease
# --- table, so a standalone tool REFUSES to add a second concurrent consumer instead of crashing.

class _LeaseFakeT:
    def __init__(self, conflict: bool, report: str = "session 7 pad 18 lease device u=other"):
        self._conflict, self._report = conflict, report
        self.leased_ttl = None

    def lease(self, scope="device", ttl_s=30.0):
        if self._conflict:
            raise IOError("gpib bridge error: pad 18 leased by another controller")
        self.leased_ttl = ttl_s
        return "= OK lease device"

    def lease_report(self):
        return self._report


def test_lease_exclusive_acquires_when_free():
    t = _LeaseFakeT(conflict=False)
    grant = drivers.lease_exclusive(t, "8565EC analyzer (RX)", ttl_s=400)
    assert "OK" in grant and t.leased_ttl == 400


def test_lease_exclusive_refuses_on_conflict_with_lease_table():
    t = _LeaseFakeT(conflict=True)
    with pytest.raises(drivers.SingleConsumerConflict) as ei:
        drivers.lease_exclusive(t, "8565EC analyzer (RX)", ttl_s=400)
    assert ei.value.label == "8565EC analyzer (RX)"
    assert "u=other" in ei.value.report                    # the live lease table is carried for the operator
    assert "only one consumer" in str(ei.value)


def test_sim_analyzer_sweep_is_always_live_guard_is_noop():
    # the sim has no frozen-sweep failure mode: _sweep_is_live() is True and measure_peak still reads.
    bench = drivers.SimBench(separation_m=0.6)
    an = drivers.SimSpectrumAnalyzer(bench)
    assert an._sweep_is_live() is True
    f_hz, amp = an.measure_peak(1e9, 0.0)
    assert f_hz == 1e9 and isinstance(amp, float) and amp == amp   # finite dBm, guard did not raise


def test_configure_stores_the_configured_detector():
    t = FakeT()
    a = drivers.Agilent856xEC(t)
    assert a._detector == "POS"                           # default before any configure()
    a.configure(1e3, 3e3, -10.0, "NRM")
    assert a._detector == "NRM"


# --- P0-1 / P0-2: the RX-dead-on-arrival driver-contract bugs (RBW-auto sends RB 0HZ; detector
# --- combo sends invalid mnemonics). Both are SILENTLY accepted-or-rejected by the 8565EC, so the
# --- byte sequence is the contract.

def test_configure_rbw_vbw_auto_emit_AUTO_not_zero_hz():
    # the GUI's "RBW auto" sends 0.0; a literal "RB 0HZ" is invalid (RX dead on arrival) -> "RB AUTO"
    t = FakeT()
    drivers.Agilent856xEC(t).configure(0.0, 0.0, 0.0, "POS")
    assert "RB AUTO" in t.writes and "VB AUTO" in t.writes
    assert "RB 0HZ" not in t.writes and "VB 0HZ" not in t.writes   # never a zero-Hz BANDWIDTH
    assert "SP 0HZ" in t.writes                            # (zero SPAN is correct + unchanged)
    # a manual value still goes through verbatim
    t2 = FakeT()
    drivers.Agilent856xEC(t2).configure(1000.0, 3000.0, 0.0, "POS")
    assert "RB 1000HZ" in t2.writes and "VB 3000HZ" in t2.writes


def test_configure_normalizes_detector_labels_to_mnemonics():
    for label, mnem in [("peak", "POS"), ("sample", "SMP"), ("neg-peak", "NEG"),
                        ("normal", "NRM"), ("POS", "POS"), ("SMP", "SMP")]:
        t = FakeT()
        a = drivers.Agilent856xEC(t)
        a.configure(1e3, 1e3, 0.0, label)
        assert f"DET {mnem}" in t.writes                   # human label OR mnemonic -> valid mnemonic
        assert a._detector == mnem                         # stored as the mnemonic (measure_floor restore)
    assert drivers.normalize_detector("rms") == "POS"      # unknown -> safe positive-peak default


def test_measure_tracked_peak_real_searches_and_preselector_peaks_high_band():
    # ISSUE 3: measure_tracked_peak SEARCHES for the tone (band 0: narrow span; >2.9 GHz: preselector
    # peak) instead of a bare zero-span-at-exact-CF read. Below 2.9 GHz it sets a nonzero search span;
    # above 2.9 GHz it invokes the preselector peak (PP). Verify the command path per band.
    t = FakeT({"MKA?": "-12.0", "MKF?": "2000000000"})
    ana = drivers.Agilent856xEC(t); ana._ARM_DWELL_S = 0
    f, amp = ana.measure_tracked_peak(2.0e9)     # band 0
    assert amp == -12.0 and abs(f - 2.0e9) < 1
    assert any(w.startswith("SP ") and w != "SP 0HZ" for w in t.writes)   # a NONZERO search span
    assert "PP" not in t.writes                                        # no preselector below 2.9 GHz
    t2 = FakeT({"MKA?": "-8.0", "MKF?": "10000000000", "PSDAC?": "127", "TRA?": _TONE_TRACE})
    drivers.Agilent856xEC(t2).measure_tracked_peak(10.0e9)            # high band, tone present
    assert "PP" in t2.writes                                          # preselector peaked above 2.9 GHz


def test_sim_measure_tracked_peak_delegates_to_zero_span():
    # sim has no source<->analyzer offset and no preselector -> tracked read == the zero-span read
    bench = drivers.SimBench(separation_m=0.6)
    an = drivers.SimSpectrumAnalyzer(bench)
    f, amp = an.measure_tracked_peak(2e9)
    assert f == 2e9 and amp == amp                                    # finite, at the commanded freq


def test_set_detector_normalizes_human_labels():
    # AUDIT F1: set_detector() must normalize like configure() -- a raw "DET sample" is SILENTLY
    # IGNORED by the 8565EC (stale detector -> wrong number). A human label -> the valid mnemonic.
    for label, mnem in [("sample", "SMP"), ("peak", "POS"), ("neg-peak", "NEG"), ("normal", "NRM"),
                        ("garbage", "POS")]:               # unknown -> POS (safe positive-peak default)
        t = FakeT()
        drivers.Agilent856xEC(t).set_detector(label)
        assert t.writes == [f"DET {mnem}"]                 # normalized, not the raw label; never a raw DET


def test_sim_set_detector_normalizes_human_labels():
    # G.3: SimSpectrumAnalyzer.set_detector must normalize a human label to the mnemonic (parity with
    # Agilent856xEC.set_detector + sim.configure), else a raw 'peak' != 'POS' silently loses the
    # _subst_base positive-peak floor bump keyed on detector == "POS".
    an = drivers.SimSpectrumAnalyzer(drivers.SimBench(separation_m=0.6))
    for label, mnem in [("peak", "POS"), ("sample", "SMP"), ("neg-peak", "NEG"), ("garbage", "POS")]:
        an.set_detector(label)
        assert an.detector == mnem                         # normalized, not the raw label


def test_sim_set_detector_pos_bump_parity_in_subst_base():
    # the +2.5 dB positive-peak floor bump (_subst_base swept path) must follow a HUMAN 'peak' label
    # now that set_detector normalizes -> 'POS'; a raw 'peak' would have silently lost it.
    bench = drivers.SimBench(separation_m=0.6)
    bench.src_rf_on = False                                # floor branch of _subst_base
    an = drivers.SimSpectrumAnalyzer(bench)
    an.configure(1e3, 3e3, -10.0, "sample")               # valid RBW + SMP detector
    smp = an._subst_base(2e9, bench)
    an.set_detector("peak")                                # human label -> POS via the G.3 fix
    pos = an._subst_base(2e9, bench)
    assert an.detector == "POS"
    assert pos == pytest.approx(smp + 2.5)                # POS floor sits +2.5 dB above SMP


def test_configure_pins_amplitude_units_to_dbm():
    # AUDIT F2: configure() must assert AUNITS DBM so MKA?/TRA? reads are unambiguously dBm even if a
    # prior session left AUNITS at V/W (a persistent state) -- otherwise a silent wrong number.
    t = FakeT()
    drivers.Agilent856xEC(t).configure(1e3, 3e3, -10.0, "POS")
    assert "AUNITS DBM" in t.writes


def test_configure_asserts_clear_write_defeating_stale_max_hold():
    # AUDIT 2026-07-04: MXMH TRA (max-hold) is a PERSISTENT state the range/paint mode leaves ON;
    # a later measurement would inherit a HELD trace that mimics a hardware wedge (0/601 change,
    # RF-toggle 0 dB). configure() must assert CLRW TRA so every SE read starts from a live trace.
    t = FakeT()
    drivers.Agilent856xEC(t).configure(1e3, 3e3, -10.0, "POS")
    assert "CLRW TRA" in t.writes


def test_sim_configure_auto_rbw_does_not_crash_and_reads():
    # the operability-expert smoke: the DEFAULT panel state (rbw auto -> 0.0) must NOT crash the sim
    # (log10(0)) and mis-render as "analyzer ABSENT" -- it must produce a real read.
    bench = drivers.SimBench(separation_m=0.6)
    an = drivers.SimSpectrumAnalyzer(bench)
    an.configure(0.0, 0.0, -10.0, "peak")                 # auto RBW + human detector label
    assert bench.rbw_hz > 0                                # kept a valid RBW, not 0
    assert an.detector == "POS"                            # human label normalized
    _, amp = an.measure_peak(2e9, 0.0)                     # no math-domain crash
    assert amp == amp                                      # finite (not NaN)


def test_measure_floor_uses_sample_detector_and_restores_configured():
    # P1-4: the source-off floor MUST be read with DET SMP (POS inflates it ~+2.5 dB), then the
    # CONFIGURED detector (not hardcoded POS) must be restored for the tone read that follows.
    t = FakeT({"MKA?": "-95.0"})
    a = drivers.Agilent856xEC(t); a._ARM_DWELL_S = 0
    a.configure(1e3, 3e3, -10.0, "NRM")                   # a non-default configured detector
    t.writes.clear()                                      # isolate measure_floor's own writes
    f_hz, floor = a.measure_floor(6e9, 0.0)
    assert f_hz == 6e9 and floor == -95.0
    assert t.writes[0] == "DET SMP"                       # SAMPLE detector BEFORE the read
    assert t.writes[-1] == "DET NRM"                      # restores the CONFIGURED detector after
    assert "MKF?" not in t.queries                        # delegates to the MKF?-free measure_peak


def test_sim_measure_floor_matches_measure_peak_no_pos_bias_in_stepped_path():
    # VERIFIED: SimBench.amplitude_dbm() (what measure_peak reads) has no POS/SMP-dependent bias
    # -- the +2.5 dB positive-peak bias is modeled only in _subst_base (the swept-trace path),
    # not in the stepped zero-span path the SE loop uses. So on the sim, measure_floor is
    # numerically IDENTICAL to measure_peak at the same frequency (same seeded noise draw).
    bench = drivers.SimBench(separation_m=0.6)
    an = drivers.SimSpectrumAnalyzer(bench)
    _, pk = an.measure_peak(2e9, 0.0)
    _, fl = an.measure_floor(2e9, 0.0)
    assert fl == pk
    assert an.detector == "POS"                           # detector restored after the toggle


def test_measure_average_uses_sample_detector_and_linear_mean():
    # a masker-robust read: SAMPLE detector + linear-power average of the trace
    trace = ",".join(["-100"] * 600 + ["-40"])           # 600 floor pts + 1 tone-ish pt
    t = FakeT({"TRA?": trace})
    ana = drivers.Agilent856xEC(t); ana._ARM_DWELL_S = 0
    _, avg = ana.measure_average(2e9, 0.1, sweeps=8)
    assert "DET SMP" in t.writes and "VAVG 8" in t.writes
    lin = (600 * 10 ** (-100 / 10) + 10 ** (-40 / 10)) / 601
    assert abs(avg - 10 * math.log10(lin)) < 1e-6


def test_analyzer_set_continuous_toggles_conts_sngls():
    t = FakeT(); an = drivers.Agilent856xEC(t)
    an.set_continuous(True); an.set_continuous(False)
    assert t.writes == ["CONTS", "SNGLS"]                # continuous vs single-sweep control


def test_analyzer_query_errors_filters_zero():
    # ERR? returns the native error queue; 0 = no error is filtered out, real codes kept
    assert drivers.Agilent856xEC(FakeT({"ERR?": "0"})).query_errors() == []
    assert drivers.Agilent856xEC(FakeT({"ERR?": "142,0,151"})).query_errors() == [142, 151]


def test_analyzer_query_status_parses_stb():
    assert drivers.Agilent856xEC(FakeT({"STB?": "2"})).query_status() == 2


def test_analyzer_idn_queries_id():
    t = FakeT({"ID?": "HP8565E,001,006,007,008"})
    assert "8565E" in drivers.Agilent856xEC(t).idn()
    assert t.queries == ["ID?"]                          # 8560-series identity query (not *IDN?)


def test_validate_devices_runs_the_sequence_on_sim():
    # the executable adherence check runs the automatable per-device validation steps end-to-end
    import config
    import control_plane
    import loop
    band = config.BandPlan("t", 1e9, 6e9, 3, 14.0, 12.0, -150.0)
    cfg = config.Campaign(bands=(band,), label="validate-test")
    cp = control_plane.simulated(cfg)
    coord = cp.make_coordinator()
    assert coord.ensure_ready()                             # open the rx+tx links (as the CLI does)
    res = loop.validate_devices(cfg, coord.source, coord.analyzer, bench=cp.bench, probe_hz=5e9)
    ids = {c["id"] for c in res["checks"]}
    assert {"S-V1", "S-V3", "S-V5", "A-V1", "A-V4", "A-V5", "A-V7"} <= ids   # sequence present
    assert res["n_fail"] == 0                                # sim adheres on every automatable step
    # the source freq/level readback checks actually pass on the sim (interface + readback wired)
    sv3 = next(c for c in res["checks"] if c["id"] == "S-V3")
    assert sv3["status"] == "PASS"
    av5 = next(c for c in res["checks"] if c["id"] == "A-V5")
    assert av5["status"] == "NA"                             # sim has no preselector -> N/A


def test_8560_error_classification_by_range():
    import loop
    # 100-199 parser (benign) ; 200-799 hardware ; 900-999 measurement (8560 UG 08560-90146 Ch.9)
    assert loop.classify_8560_error(111) == "parser"        # # ARGMTS
    assert loop.classify_8560_error(112) == "parser"        # ??CMD??
    assert loop.classify_8560_error(311) == "hardware"      # LO/RF failure -> service
    assert loop.classify_8560_error(901) == "measurement"   # TGFrqLmt -> bad setup
    # only benign parser codes -> WARN; any hardware/measurement code -> FAIL
    assert loop._error_queue_status([]) == "PASS"
    assert loop._error_queue_status([111, 112]) == "WARN"
    assert loop._error_queue_status([111, 311]) == "FAIL"   # a hardware code fails even amid parser
    assert loop._error_queue_status([902]) == "FAIL"        # bad measurement setup fails
    assert loop._error_queue_status("oops") == "FAIL"       # not a list -> FAIL


def test_sim_defaults_cover_new_analyzer_methods():
    # the Sim analyzer inherits the base no-op/default preselector + average behavior
    bench = drivers.SimBench(separation_m=2.0)
    an = drivers.SimSpectrumAnalyzer(bench)
    assert an.peak_preselector(10e9) is None             # sim has no preselector
    an.set_preselector_dac(100)                          # no-op, must not raise
    _, pk = an.measure_peak(2e9, 0.0)
    _, av = an.measure_average(2e9, 0.0)                 # base default -> measure_peak
    assert av == pk


# ------------------------------------------------------------- SAFETY GUARDS
# The TX is directly cabled to the 8565EC. These assert the driver code can never command the
# source above its rated leveled power / outside its rated band, and can never drop the analyzer
# input attenuation below a protective floor -- protecting BOTH the source and a directly-wired
# analyzer front end on every code path that drives real RF.

import warnings as _warnings


def test_source_power_clamped_to_safety_cap():
    # lowering max_output_dbm (as a bare-loopback operator would, to protect the 8565EC input)
    # must HARD-CAP the commanded L1 level -- a request above the cap is clamped, not obeyed.
    t = FakeT()
    sg = drivers.Anritsu68369(t)
    sg.max_output_dbm = 0.0                               # loopback-protective cap
    with _warnings.catch_warnings(record=True) as w:
        _warnings.simplefilter("always")
        sg.set_power(12.0)                                # ask for +12 dBm
    assert t.writes == ["L1 0.00 DM"]                    # clamped to the 0 dBm cap, not +12
    assert any("safety cap" in str(x.message) for x in w)  # and it warned


def test_source_power_within_cap_is_unchanged():
    # the DEFAULT cap (17 dBm) must not perturb an in-range level (band power 12 / test value 12.5)
    t = FakeT()
    drivers.Anritsu68369(t).set_power(12.5)
    assert t.writes == ["L1 12.50 DM"]                   # unchanged: 12.5 < default cap 17.0


def test_source_freq_clamped_into_rated_band():
    # an out-of-band CW command can unlevel/fault the synth; set_freq clamps into the rated band
    t = FakeT()
    with _warnings.catch_warnings(record=True) as w:
        _warnings.simplefilter("always")
        drivers.Anritsu68369(t).set_freq(60e9)           # above the 40 GHz ceiling
    assert t.writes == ["CF1 40.000000000 GH"]           # clamped to the rated max, not 60 GHz
    assert any("outside rated" in str(x.message) for x in w)


def test_analyzer_configure_enforces_atten_floor_when_set():
    # RAISING min_atten_db (loopback protection) makes configure() emit the floor on BOTH read
    # paths; the DEFAULT (0.0) emits NO extra AT write, preserving full shielded-read sensitivity.
    t = FakeT()
    a = drivers.Agilent856xEC(t)
    a.min_atten_db = 20.0
    a.configure(1e3, 3e3, -10.0, "POS")
    assert "AT 20DB" in t.writes                          # 20 dB floor enforced at configure time

    t2 = FakeT()
    drivers.Agilent856xEC(t2).configure(1e3, 3e3, -10.0, "POS")   # default floor 0.0
    assert not any(w.startswith("AT ") for w in t2.writes)        # no atten write -> full sensitivity


def test_analyzer_set_attenuation_never_below_floor():
    # an explicit low-atten request is floored up to min_atten_db (never below the protection floor)
    t = FakeT()
    a = drivers.Agilent856xEC(t)
    a.min_atten_db = 20.0
    a.set_attenuation(db=10)                              # ask for 10 dB
    assert t.writes == ["AT 20DB"]                        # floored to 20, not 10
    t.writes.clear()
    a.set_attenuation(db=30)                              # a request ABOVE the floor is honored
    assert t.writes == ["AT 30DB"]


def test_sim_source_power_capped_like_real():
    bench = drivers.SimBench(separation_m=0.6)
    sg = drivers.SimSignalGenerator(bench)
    sg.max_output_dbm = 0.0
    sg.set_power(12.0)
    assert bench.src_power_dbm == 0.0                    # sim clamps identically to the real driver


def test_sim_analyzer_atten_floored_like_real():
    bench = drivers.SimBench(separation_m=0.6)
    an = drivers.SimSpectrumAnalyzer(bench)
    an.min_atten_db = 20.0
    an.set_attenuation(db=10)
    assert an.atten_db == 20.0                            # floored to the protective minimum


# ---- ensemble-audit hardening: the invariant "TX cannot transmit more than the 8565EC can handle
# ---- directly" must hold BY CONSTRUCTION, not just when an operator remembers to configure it.

def test_source_cap_cannot_be_raised_above_hard_ceiling():
    # F2: max_output_dbm is settable DOWN (loopback protection) but can NEVER be raised above the
    # hard ceiling -- so no caller can defeat the clamp by writing a huge cap. The 17 dBm ceiling is
    # itself 13 dB below the 8565EC +30 dBm / 1 W input max, so the TX can never exceed it directly.
    t = FakeT()
    sg = drivers.Anritsu68369(t)
    with _warnings.catch_warnings(record=True) as w:
        _warnings.simplefilter("always")
        sg.max_output_dbm = 30.0                          # try to raise the cap to the RX damage level
    assert sg.max_output_dbm == drivers.Anritsu68369.HARD_MAX_OUTPUT_DBM   # clamped to 17.0
    assert any("hard ceiling" in str(x.message) for x in w)
    sg.set_power(30.0)                                    # and a +30 dBm command still clamps to 17
    assert t.writes == ["L1 17.00 DM"]


def test_analyzer_auto_attenuation_honors_protection_floor():
    # F3: with a floor armed, AT AUTO (which can couple to 0 dB at low RL) is OVERRIDDEN with a fixed
    # attenuation at the floor -- the front end can never drop below protection via the AUTO path.
    t = FakeT()
    a = drivers.Agilent856xEC(t)
    a.min_atten_db = 20.0
    with _warnings.catch_warnings(record=True) as w:
        _warnings.simplefilter("always")
        a.set_attenuation(auto=True)
    assert t.writes == ["AT 20DB"]                        # pinned to the floor, NOT "AT AUTO"
    assert any("AT AUTO overridden" in str(x.message) for x in w)

    t2 = FakeT()
    drivers.Agilent856xEC(t2).set_attenuation(auto=True)  # no floor armed -> genuine AUTO
    assert t2.writes == ["AT AUTO"]


def test_sim_auto_attenuation_honors_floor():
    bench = drivers.SimBench(separation_m=0.6)
    an = drivers.SimSpectrumAnalyzer(bench)
    an.min_atten_db = 20.0
    an.set_attenuation(auto=True)
    assert an.atten_db == 20.0                            # AUTO cannot drop below the floor on the sim


def test_arm_direct_chain_arms_a_provably_safe_envelope():
    # arm_direct_chain caps the source, floors the analyzer atten, applies it immediately, and PROVES
    # the connector + first-mixer levels sit under the 8565EC damage limits before any tone flows.
    ts, ta = FakeT(), FakeT()
    src = drivers.Anritsu68369(ts)
    an = drivers.Agilent856xEC(ta)
    env = drivers.arm_direct_chain(src, an, source_cap_dbm=0.0, rx_min_atten_db=20.0)
    assert src.max_output_dbm == 0.0                      # source capped at 0 dBm
    assert an.min_atten_db == 20.0                        # analyzer floored at 20 dB
    assert ta.writes == ["AT 20DB"]                       # protection applied to the instrument NOW
    # provable envelope: connector 0 dBm << +30 dBm max; mixer -20 dBm << +20 dBm ceiling
    assert env["connector_dbm"] <= drivers.Agilent856xEC.RX_ABS_MAX_INPUT_DBM
    assert env["mixer_dbm"] <= drivers.Agilent856xEC.RX_MIXER_MAX_DBM
    assert env["rx_min_atten_db"] >= drivers.Agilent856xEC.RX_RATING_MIN_ATTEN_DB
    # a subsequent over-power command is clamped to the armed cap
    ts.writes.clear()
    src.set_power(15.0)
    assert ts.writes == ["L1 0.00 DM"]


def test_arm_direct_chain_clamps_absurd_request_but_stays_safe():
    # even an absurd requested source cap is clamped to the hard ceiling, and the armed envelope is
    # STILL proven safe (connector 17 dBm < +30 dBm; mixer 17-10 = +7 dBm < +20 dBm ceiling).
    src = drivers.Anritsu68369(FakeT())
    an = drivers.Agilent856xEC(FakeT())
    env = drivers.arm_direct_chain(src, an, source_cap_dbm=100.0, rx_min_atten_db=5.0)
    assert env["source_cap_dbm"] == drivers.Anritsu68369.HARD_MAX_OUTPUT_DBM   # 17.0
    assert env["rx_min_atten_db"] == drivers.Agilent856xEC.RX_RATING_MIN_ATTEN_DB  # floored up to 10
    assert env["connector_dbm"] <= drivers.Agilent856xEC.RX_ABS_MAX_INPUT_DBM
    assert env["mixer_dbm"] <= drivers.Agilent856xEC.RX_MIXER_MAX_DBM


def test_sim_direct_chain_power_at_mixer_stays_under_damage_limit():
    # F5: exercise the DIRECT-CABLE topology on the sim (which otherwise only models a radiated link)
    # and assert the power reaching the first mixer stays under the 8565EC ceiling even when the
    # operator commands far more than the cap. connector = capped source power (lossless cable);
    # mixer = connector - armed attenuation.
    bench = drivers.SimBench(separation_m=0.6)
    sg = drivers.SimSignalGenerator(bench)
    sa = drivers.SimSpectrumAnalyzer(bench)
    drivers.arm_direct_chain(sg, sa, source_cap_dbm=0.0, rx_min_atten_db=20.0)
    sg.set_power(50.0)                                    # absurd over-command
    sg.rf_on()
    connector_dbm = bench.src_power_dbm                   # direct lossless cable: all power reaches RX
    mixer_dbm = connector_dbm - sa.atten_db
    assert connector_dbm <= drivers.Agilent856xEC.RX_ABS_MAX_INPUT_DBM   # <= +30 dBm (1 W)
    assert mixer_dbm <= drivers.Agilent856xEC.RX_MIXER_MAX_DBM           # <= +20 dBm mixer ceiling


def test_arm_and_wait_dwells_for_each_sweep_to_complete(monkeypatch):
    """arm_and_wait must DWELL >= the sweep time after EACH of its two TS, because over the networked
    GPIB bridge TS;DONE? does not block for the new sweep. Without the dwell, read_trace() gets a
    partial trace -- and a CLRW-cleared trace (every configure()/set_max_hold apply) never refills, so
    the read rails blank at the bottom graticule (the live Point Op 'NO TONE' bug). Assert it queries
    the sweep time, takes two continuous sweeps, and sleeps for >= that time after each."""
    slept = []
    monkeypatch.setattr(drivers.time, "sleep", lambda s: slept.append(s))
    t = FakeT({"ST?": "0.2"})                              # 200 ms sweep
    a = drivers.Agilent856xEC(t)
    a._ARM_DWELL_S = 0.0                                  # let the sweep time drive the dwell
    a.arm_and_wait(timeout_s=5.0)                         # fresh=True (default): flush + fresh
    assert t.writes.count("CONTS") == 1                   # continuous (front panel stays live), not SNGLS
    assert t.writes.count("TS") == 2                      # flush stale + fresh
    assert t.queries.count("DONE?") == 2
    assert "ST?" in t.queries                             # queried the sweep time for the dwell
    assert len(slept) == 2                                # a real dwell after EACH sweep
    assert all(s >= 0.2 for s in slept)                   # >= the 200 ms sweep time (0.2 * 1.5 = 0.3)


def test_arm_and_wait_takes_one_sweep_when_parked(monkeypatch):
    """PARKED (fresh=False, steady-state feed with no settings change): the analyzer is already
    free-running in CONTS so ONE completed sweep is current -- skip the stale-flush sweep. This
    ~halves the per-read time (LIVE-measured 0.6 -> 0.8-0.9 reads/s). fresh=True still takes two."""
    slept = []
    monkeypatch.setattr(drivers.time, "sleep", lambda s: slept.append(s))
    t = FakeT({"ST?": "0.05"})
    a = drivers.Agilent856xEC(t)
    a.arm_and_wait(timeout_s=5.0, fresh=False)
    assert t.writes.count("TS") == 1 and len(slept) == 1  # ONE sweep + one dwell when parked
    assert t.writes.count("CONTS") == 1
    assert all(s >= 0.05 for s in slept)                  # dwell still >= the sweep time (floor 0.12 here)


# ------------------------------------------------------------- binary trace fast-read (TDF B)

def _rich_dbm():
    """A realistic zero-span sweep: a rippling ~-92 dBm floor with a gaussian tone rising to ~-14 dBm at
    center. MANY distinct levels (floor ripple + tone skirt) so a WRONG binary parse (byte swap) fails
    the linear fit -- a clean 2-level trace would fit a line either way and hide the bug."""
    out = []
    for i in range(601):
        floor = -92.0 + 3.0 * math.sin(i * 0.31)
        tone = 78.0 * math.exp(-((i - 300) / 12.0) ** 2)
        out.append(round(floor + tone, 1))
    return out


class _FormatFakeT(FakeT):
    """Models the 8560 trace formats on ONE sweep, live-confirmed on the 8565EC: query('TRA?') returns
    ASCII dBm (the TDF P path), query_raw('TRA?') returns 601 big-endian uint16 measurement units (the
    TDF B path). MU is the inverse of the measured map dBm = RL - (600-MU)/6, i.e. MU = (dBm-RL)*6 + 600.
    byteswap=True emits little-endian bytes to simulate a WRONG parse (the driver reads big-endian, so the
    fit is loose -> the calibration is rejected -> ASCII fallback)."""

    def __init__(self, dbm, rl=0.0, byteswap=False, fa=2.4475e9, fb=2.4525e9,
                 binary_raises=False, ascii_raises=False, truncate_binary=None):
        super().__init__({"FA?": f"{fa}", "FB?": f"{fb}"})
        self._dbm = list(dbm)
        self._mu = [max(0, min(0xFFFF, int(round((d - rl) * 6.0 + 600.0)))) for d in self._dbm]
        self._byteswap = byteswap
        self._binary_raises = binary_raises     # simulate a bridge hiccup on the binary transfer
        self._ascii_raises = ascii_raises       # simulate a genuine ASCII read failure (dead bus)
        self._truncate_binary = truncate_binary  # emit only N points (a desynced/partial binary read)

    def query(self, cmd):
        if cmd.startswith("TRA?") or cmd.startswith("TRB?"):
            self.queries.append(cmd)
            if self._ascii_raises:
                raise IOError("gpib bridge closed the connection")
            return ",".join(f"{d:.1f}" for d in self._dbm)
        return super().query(cmd)

    def query_raw(self, cmd):
        if cmd.startswith("TRA?") or cmd.startswith("TRB?"):
            self.queries.append(cmd)
            if self._binary_raises:
                raise IOError("gpib bridge error on binary read")
            mu = self._mu if self._truncate_binary is None else self._mu[:self._truncate_binary]
            order = "<" if self._byteswap else ">"
            return struct.pack(f"{order}{len(mu)}H", *mu)
        return super().query_raw(cmd)


def test_fit_linear_recovers_mu_to_dbm_and_rejects_flat():
    a, b, rms = drivers._fit_linear([0, 60, 600], [-100.0, -90.0, 0.0])   # dBm = MU/6 - 100 (RL=0)
    assert abs(a - 1.0 / 6.0) < 1e-9 and abs(b + 100.0) < 1e-9 and rms < 1e-9
    assert drivers._fit_linear([5, 5, 5], [1, 2, 3]) is None              # no x spread -> unfittable
    assert drivers._fit_linear([], []) is None


def test_binary_trace_calibrate_then_parked_read_matches_ascii():
    """calibrate=True freezes the sweep, reads it as ASCII (dBm) AND binary (MU), fits dBm=a*MU+b and
    caches it; a later parked read then uses the ~3x-smaller binary transfer and reconstructs the SAME
    dBm. Regression for the TDF B fast-read (live-derived map dBm = RL - (600-MU)/6)."""
    dbm = _rich_dbm()
    t = _FormatFakeT(dbm, rl=0.0)
    an = drivers.Agilent856xEC(t)
    freqs, levels = an.read_trace("A", calibrate=True)
    assert levels == dbm                                  # ASCII returned on the calibrate tick
    assert an._bin_cal is not None                        # tight fit -> calibration accepted
    a, b = an._bin_cal
    assert abs(a - 1.0 / 6.0) < 1e-3 and abs(b + 100.0) < 0.5   # recovered dBm = MU/6 - 100
    assert "SNGLS" in t.writes and t.writes.count("CONTS") >= 1  # froze then restored continuous
    freqs2, levels2 = an.read_trace("A")                  # parked: uses the cached binary path
    assert any(w == "TDF B" for w in t.writes)            # binary transfer was exercised
    assert max(abs(levels2[i] - dbm[i]) for i in range(601)) < 0.2   # binary dBm == ASCII dBm


def test_binary_trace_falls_back_to_ascii_on_bad_parse():
    """A wrong binary byte order makes the fit loose, so the calibration is REJECTED and every read stays
    ASCII (still-correct amplitudes). Binary can never ship a wrong number."""
    dbm = _rich_dbm()
    t = _FormatFakeT(dbm, rl=0.0, byteswap=True)          # little-endian bytes; driver reads big-endian
    an = drivers.Agilent856xEC(t)
    freqs, levels = an.read_trace("A", calibrate=True)
    assert levels == dbm and an._bin_cal is None          # loose fit -> no cache
    freqs2, levels2 = an.read_trace("A")                  # parked read stays ASCII
    assert levels2 == dbm                                  # correct amplitudes, no binary


def test_configure_clears_binary_calibration():
    t = _FormatFakeT(_rich_dbm())
    an = drivers.Agilent856xEC(t)
    an.read_trace("A", calibrate=True)
    assert an._bin_cal is not None
    an.configure(0.0, 0.0, 0.0, "POS")                    # RL/scale may change -> drop the cached map
    assert an._bin_cal is None


def test_calibrate_binary_failure_still_returns_ascii_trace():
    """STATE CONSISTENCY (the 'PSD disappears' regression): if the OPTIONAL binary calibration machinery
    hiccups, read_trace(calibrate=True) must still deliver the real ASCII trace -- NOT a silent ([], [])
    that blanks the PSD. Calibration is skipped (cache empty), the trace is intact."""
    dbm = _rich_dbm()
    t = _FormatFakeT(dbm, binary_raises=True)              # every binary transfer throws
    an = drivers.Agilent856xEC(t)
    freqs, levels = an.read_trace("A", calibrate=True)
    assert levels == dbm                                   # ASCII deliverable intact, not blanked
    assert an._bin_cal is None                             # calibration cleanly skipped


def test_calibrate_ascii_failure_propagates_not_blank():
    """A GENUINE read failure (dead bus) during a calibrate tick must PROPAGATE so the engine surfaces
    'absent' -- not be swallowed into an empty trace. Distinguishes a real fault from a blank."""
    t = _FormatFakeT(_rich_dbm(), ascii_raises=True)
    an = drivers.Agilent856xEC(t)
    with pytest.raises(Exception):
        an.read_trace("A", calibrate=True)


def test_parked_binary_truncated_falls_back_to_ascii():
    """A truncated/desynced binary transfer (fewer than 601 points) is rejected and the read falls back
    to ASCII, so a partial binary trace can never render as a truncated PSD."""
    dbm = _rich_dbm()
    t = _FormatFakeT(dbm, truncate_binary=600)             # binary returns 600 pts, not 601
    an = drivers.Agilent856xEC(t)
    an._bin_cal = (1.0 / 6.0, -100.0)                      # pretend already calibrated
    freqs, levels = an.read_trace("A")                     # parked read
    assert levels == dbm                                   # ASCII fallback delivered the full trace
    assert an._bin_cal is None                             # bad binary read cleared the cache
