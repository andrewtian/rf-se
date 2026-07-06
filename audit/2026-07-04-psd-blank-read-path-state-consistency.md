# PSD-disappears audit: read-path state consistency after the TDF B binary trace change

Date: 2026-07-04
Scope: `Agilent856xEC.read_trace` family (drivers.py) + `SpectrumEngine.step_once` (sa_gui.py)
Trigger: user launched the bench GUI; the PSD curve disappears.
Prior change under suspicion: `perf(se299): binary trace read (TDF B)` (commit d6818d18).

## Symptom -> mechanism

The PSD "disappears" = the live curve goes blank (not the "analyzer ABSENT" text).

- The panel renders whatever the model holds: `render()` -> `self._live.setData(fg, live)`.
- `SpectrumModel.set_trace(freqs, levels)` stores `self._freqs, self._live = freqs, levels` verbatim.
- So a published `("trace", [], [])` sets `_live = []` -> `setData([], [])` -> **blank curve**.
- `("absent", ...)` is a DIFFERENT visual (readout text), so a blank curve means an EMPTY TRACE was
  published, not that the link was reported down.

Therefore: some read returned `([], [])` and the engine published it.

## Root cause (state-consistency defect I introduced)

`_read_and_calibrate` (the `calibrate=True` path used on every FRESH tick -- launch, retune, preselector
op) was structured as:

```
freqs, levels = [], []
try:
    ...stability loop: up to 8 binary reads...      # OPTIONAL calibration machinery
    freqs, levels = self._read_trace_ascii(trace)   # the DELIVERABLE, AFTER the binary reads
    ...fit...
except Exception:
    self._bin_cal = None                            # swallow, and freqs/levels stay []
return (freqs, levels)                              # -> ([], []) if any binary read raised first
```

The DELIVERABLE ASCII read sat *behind* the optional binary machinery inside one `try`, and the `except`
returned the empty initial `freqs, levels`. So ANY transient failure in the calibration machinery (a
bridge hiccup on a `query_raw`, a keepalive interleave, a truncated binary transfer) silently returned
an empty trace and blanked the PSD -- a regression the pre-binary one-`query` path never had (it either
returned a trace or RAISED, and a raise surfaces as "absent", not a blank).

Two compounding gaps:

1. DRIVER: calibration failure blanked the deliverable trace. Calibration is an optimization; it must
   never be able to erase the trace.
2. ENGINE: an empty trace *erased* a good PSD. A live feed must be stable across a momentary empty read
   (keep the last trace); only a genuine bus failure (which RAISES -> "absent") should change the display.

## Fixes

**Driver `_read_and_calibrate` (drivers.py):** the ASCII read is the deliverable and is taken OUTSIDE
the binary machinery's `try`. The stability loop (binary reads) is isolated in its own `try/except` that
only clears the local `mu` -- a binary hiccup skips calibration, it does not touch the trace. A genuine
ASCII read failure now PROPAGATES (engine -> "absent"), never a silent `([], [])`.

**Driver `_read_trace_binary` (drivers.py):** validate the binary transfer length against the fixed
`_TRACE_POINTS = 601`. A truncated/desynced read (any other count) raises -> `read_trace` catches it,
clears the cache, and falls back to ASCII -> a partial binary read can never render as a partial PSD.

**Engine `SpectrumEngine.step_once` (sa_gui.py):** publish only a NON-EMPTY trace. A momentary
empty/partial read keeps the last-good PSD and retries next tick; a raised error still surfaces as
"absent". This is the catch-all that keeps ALL three modes (SA panel / Range / Point Op all use this one
engine) stable through transients.

## Degradation guarantee after the fix

- Calibration machinery fails -> ASCII trace still delivered; feed stays on ASCII (slower, VISIBLE).
- Parked binary read fails/truncates -> cache cleared, ASCII fallback; stable.
- Momentary empty read -> last trace kept; updates resume next good tick.
- Genuine dead bus -> read RAISES -> "analyzer ABSENT/FAULT" text (correct), not a silent blank.

The PSD can no longer silently disappear: it either shows live data or an explicit fault reason.

## Tests (hardware-free)

- `test_calibrate_binary_failure_still_returns_ascii_trace` -- binary machinery throws -> ASCII trace
  intact, cache empty.
- `test_calibrate_ascii_failure_propagates_not_blank` -- genuine read failure raises (not `([], [])`).
- `test_parked_binary_truncated_falls_back_to_ascii` -- 600-point binary rejected -> ASCII fallback.
- `test_empty_trace_is_not_published_keeps_last` -- empty read publishes neither "trace" nor "absent".

## Live confirmation (pending)

The GUI held the analyzer lease during this audit (single-consumer), so a live single-consumer repro
was not run. Confirmation = relaunch the GUI; the PSD must stay live (or show an explicit fault), never
silently blank. If binary calibration ever fails to engage live, the feed degrades to ASCII visibly.
