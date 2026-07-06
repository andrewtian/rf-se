# GPIB Command-Correctness Audit — Anritsu 68369A / 68367C Signal Source

Scope: `rf-se/se299/drivers.py` class `Anritsu68369(SignalGenerator)` (lines 533-706) and
base `SignalGenerator` (355-404). Cross-checked the bridge dead-man safe-state in
`rf-se/se299/gpib_bridge/ni_gpib_server.py` (573-643).

Authoritative reference: `reference/operator-manuals/anritsu-68000-series-operation.md`
(Anritsu 682XXB/683XXB Operation Manual P/N 10370-10284 Rev H; native GPIB dictionary
cross-checked to MG369xB GPIB PM P/N 10370-10366 Rev D — identical 68000-series Native
language). The bench unit is a 68367C fw 2.35, live-verified for OF1/OL1/OSB readback per
the driver docstring. Hardware is currently ABSENT from the bus — this audit is static
code + manual only.

Bottom line: the native command STRINGS the driver sends (CF1/L1/RF1/RF0/OSB/OF1/OL1 and
the LST list-sweep set) are correct for this instrument's Native language. The safe-state
RF-off literal is correct. The real defects are (1) a binary status byte (OSB) mangled by
the transport's ascii-decode+strip, which silently defeats the leveled+locked interlock,
and (2) identity via `*IDN?` on a native-only unit. No off-by-10^n frequency/units error
found; the frequency and level units are correct.

---

## 1. Per-method command table

| Method (drivers.py:line) | Exact string sent | Manual-correct form (cite) | Verdict |
|---|---|---|---|
| `idn` (578) | `query("*IDN?")` | Native identity is `OI` (model/serial/limits/fw); `*IDN?` is 488.2 and only *usually* answered by legacy fw (OM 10370-10284 sec 3-13; distillation lines 104, 134) | WORKS ON BENCH, NOT GUARANTEED — see F2 |
| `prepare` (593) | `RST` `IL1` `AT0` `ATT00` `TR0` `LO0` `LOG` | Reset-to-leveled string `IL1 AT0 ATT00 TR0 LO0 LOG` (manual body lines 74-78, 122); `CSB` recommended before `RST` | CORRECT (CSB omitted — F4) |
| `set_freq` (609) | `write(f"CF1 {f_hz/1e9:.9f} GH")` | `CF1 <freq> GH` = set CW mode at reg F1 + value; GH/MH/KH/HZ terminators (spec line 20; body 68-72) | CORRECT — units are GHz value + `GH` suffix, no 10^n error |
| `set_power` (621) | `write(f"L1 {p_dbm:.2f} DM")` | `L1 <value> DM` = CW level reg 1 in dBm (`DM`=dBm log) (spec line 22-24) | CORRECT |
| `rf_on` (624) | `write("RF1")` | `RF1` = output ON, power-on default (spec line 31-33) | CORRECT |
| `rf_off` (627) | `write("RF0")` | `RF0` = output OFF, red LED (spec line 31-33; body 72) | CORRECT (native-mode caveat — F0) |
| `set_list_sweep` (679) | `LST` / `ELN1` / `ELI0` / `LF <g> GH,...` / `LDT <ms> MS` / `LEA` / `LIB0` / `LIE<n-1>` / `MNT` | LST list mode; ELN0-3 select list; ELI load index; LF load freqs; LDT dwell MS|SEC; LEA learn; LIB/LIE start/stop; MNT manual trigger (spec lines 49-51) | MNEMONICS CORRECT; ordering/first-point semantics — F3 |
| `arm_sweep` (699) | `write("RSS")` | `RSS` resets the sweep to the start index (spec line 50-51) | CORRECT string; arm-vs-first-UP semantics — F3 |
| `trigger_point` (702) | `write("UP")` | `UP` = index +1, native replacement for unsupported `*TRG` (spec line 50) | CORRECT |
| `status_byte`/`settled_ok` (642/644) | `query("OSB")` then `ord(raw[0])` | `OSB` = one-byte primary status; bit2 RF-Unleveled, bit3 Lock-Error (spec 40-42) | STRING CORRECT, READBACK CORRUPTED — F1 |
| `output_freq_mhz` (632) | `query("OF1")` | `OFn` = reg n freq in MHz (body 102-104) | CORRECT |
| `output_level_dbm` (636) | `query("OL1")` | `OLn` = reg n level dBm, log mode (spec 22-24) | CORRECT |
| `await_settled` opt-in (670) | `query("*OPC?")` only if `use_opc=True` (default False) | `*OPC?` is SCPI/Opt-19, NOT answered by native fw; poisons socket (spec 46-47; body 128-138) | CORRECT — default-off + reconnect-on-timeout fallback |
| bridge safe-state (ni_gpib_server.py:580,638) | `backend.write(b"RF0")` to pad 5 on lease-drop | `RF0` = output OFF (spec 31-33) | CORRECT literal for THIS instrument |

---

## 2. Ranked findings

### F0 — SAFETY / SAFE-STATE (headline): `rf_off` and bridge dead-man de-key send `RF0` — CORRECT, with one native-mode residual. Severity: PASS + MEDIUM residual.

- `rf_off` (drivers.py:627) writes `RF0`; the manual defines `RF0` = RF output OFF (red LED)
  (reference spec "RF output on/off", MG369xB GPIB PM p.3-112; distillation lines 31-33, 72).
  This is the correct, unambiguous kill for THIS instrument's Native language.
- The bridge's per-pad dead-man safe-state default `_DEFAULT_SAFE_STATE = {5: b"RF0"}`
  (ni_gpib_server.py:580) and its send path `backend.write(cmd)` (638) push the SAME literal
  `RF0` to source pad 5 on lease-drop / crash / idle-reap (`_send_safe_state`, 613-642). The
  literal matches `rf_off` exactly. VERIFIED CORRECT.
- RESIDUAL RISK (MEDIUM): `RF0` de-keys ONLY while the unit is in Native (Product-Specific)
  language mode. If the source is ever in SCPI mode (Option 19), `RF0` is a syntax error,
  silently discarded (OSB bit5), and the output STAYS KEYED — a silent no-op of the dead-man
  command. The bench 68367C is confirmed native-mode, so this is latent, not active. There is
  also no readback confirmation of the de-key (best-effort `backend.write`, teardown must not
  raise) — acceptable for a dead-man on a crashed client, but it means a wrong mode fails
  silently. NEEDS-LIVE-VERIFICATION: on the real unit, confirm `RF0` drops the output with an
  external power meter, and confirm the unit boots/stays in Native mode (not SCPI). If SCPI mode
  is ever possible, the safe-state map should also carry the SCPI form (`OUTP:STAT OFF`) or the
  bridge should force native mode before de-key.
- Terminator: the safe-state relies on linux-gpib asserting EOI (no explicit CR/LF appended);
  the 68367C terminator is selectable and "must match the controller" (distillation line 138).
  Bench-verified implicitly (OF1/OL1 worked), so EOI is accepted. NEEDS-LIVE if the unit's
  terminator config changes.

### F1 — HIGH: OSB binary status byte is corrupted by the transport's `decode('ascii','replace').strip()`, silently defeating the leveled+locked interlock. drivers.py:642 (`status_byte`) + NetworkTransport.query drivers.py:211 (and VisaTransport.query drivers.py:71).

- `status_byte()` does `raw = self.t.query("OSB"); return ord(raw[0])`. The manual is explicit
  that OSB returns ONE RAW BINARY BYTE (distillation lines 40-42, 104-108), and the driver
  comment even says "raw binary, not ASCII -- do not float() it" (drivers.py:640). But BOTH
  transports run every query through `.decode('ascii','replace').strip()`
  (NetworkTransport.query line 211; VisaTransport.query line 71), which mangles a binary byte.
- Empirically reproduced (byte -> after decode/strip -> `status_byte` -> `settled_ok`):
  - `0x0C` (RF-Unleveled bit2 AND Lock-Error bit3 — the worst-case double fault) -> `''` -> `0` -> **settled_ok=True (FALSE OK)**
  - `0x0A` (bit1 end-of-sweep + bit3 lock) -> `''` -> `0` -> **True**
  - `0x0D` (bit0+bit2+bit3) -> `''` -> `0` -> **True**
  - `0x20` (bit5 syntax error, exactly ASCII space) -> `''` -> `0` -> **True**
  - `0x09` (bit0+bit3) -> `''` -> `0` -> **True**
  - `0x80` (bit7 ext-status-2 summary) -> `U+FFFD` -> `65533` -> settled_ok=False (mangled value)
  - `0x04`, `0x08`, `0x00`, `0x01`, `0x40` survive correctly.
  Any status byte whose value is an ASCII whitespace code (0x09,0x0A,0x0B,0x0C,0x0D,0x20) is
  STRIPPED TO EMPTY and read as 0 = "clean/leveled/locked"; any value >= 0x80 is replaced with
  U+FFFD (65533). The single most dangerous case, RF-Unleveled + Lock-Error together = `0x0C`,
  maps to a form-feed and is silently read as a clean, settled source.
- Impact: `settled_ok()` is the native completion/health gate. It feeds `await_settled`
  (drivers.py:665 — low harm there, the loop dwells anyway) BUT is also surfaced directly to the
  operator as a leveled+locked verdict in `loop.py:621` (checkpath "settled") and
  `sg_gui.py:130`. A live bring-up would be told "source leveled+locked OK" while the source is
  actually unleveled AND lock-errored, yielding wrong SE numbers. This is a correctness/interlock
  defect, not an RF-keying hazard (it does not affect rf_off). Distinct from the already-fixed
  bridge END/ibcnt short-read bug — this corruption is at the driver/transport ascii boundary.
- Suggested fix: give the transport a binary/raw query path (no decode, no strip) for OSB, e.g.
  `query_raw(cmd) -> bytes` and have `status_byte` use it: `raw = self.t.query_raw("OSB");
  return raw[0] if raw else 0`. At minimum, stop `.strip()`-ing and stop lossy `ascii` decode for
  this one read. NEEDS-LIVE-VERIFICATION: on the real 68367C, read OSB while forcing an unleveled
  state (e.g. AT1+ATT11 pad or open ALC) and confirm `status_byte` returns 0x04/0x0C rather than 0.

### F2 — MEDIUM: `idn()` uses `*IDN?`; native `OI` is the reliable identity, and `*IDN?` is not guaranteed on a native-only 683xx. drivers.py:578.

- `idn()` returns `self.t.query("*IDN?")`. The manual: `*IDN?` "often still responds because it
  is the one 488.2 identifier most legacy firmwares implement" — but 488.2 queries that are NOT
  implemented "block until timeout and ... desynchronize the transaction" (distillation lines
  128-138). The native, always-present identity is `OI` (model / serial / limits / firmware;
  distillation line 104).
- The class docstring claims "68367C answers *IDN?" but the cited live verification is for
  OF1/OL1/OSB readback (drivers.py:536-537), NOT `*IDN?` specifically. `idn()` is used as a
  liveness probe that "raises on a silent/wedged device" (control_plane.py:121) and in the
  connection fault path (connection.py:310) — the exact places where an unanswered `*IDN?`
  would hang to timeout and (per the manual) poison the socket, the same failure mode the code
  carefully avoids for `*OPC?`.
- Suggested fix: prefer native `OI` for identity (guaranteed to answer, no poison risk), or gate
  `*IDN?` behind the same reconnect-on-timeout fallback used for `*OPC?` in `await_settled`.
  NEEDS-LIVE-VERIFICATION: confirm the bench 68367C actually answers `*IDN?` (query it directly);
  if it does not, `idn()` must switch to `OI`.

### F3 — LOW: `set_list_sweep` / `arm_sweep` learn-vs-bounds ordering and RSS arm-vs-first-UP semantics. drivers.py:679-703.

- All list-sweep mnemonics are correct per the manual (LST/ELN/ELI/LF/LDT/LEA/LIB/LIE/MNT/UP/RSS;
  distillation lines 49-51). Two behavioral (not mnemonic) concerns need live confirmation:
  1. `LEA` (learn/precompute the list) is issued BEFORE `LIB0`/`LIE<n-1>` (start/stop bounds).
     If the instrument learns against the currently-set bounds, learning before the bounds are
     set could precompute the wrong span. Safer order: LF -> LDT -> LIB/LIE -> LEA -> MNT.
  2. `arm_sweep`=`RSS` resets to the start index; `trigger_point`=`UP` advances +1. If, after
     RSS, the output already sits AT index 0, a caller that triggers-then-reads would step over
     index 0 (first `UP` -> index 1) and never measure the first point (off-by-one), while a
     caller that reads-then-triggers is correct. The base-class contract says "arm at its first
     point," implying read-then-trigger, but this depends on the loop's usage of the optional
     hardware sweep path (not clearly exercised in the software per-point path).
- Suggested fix: reorder LEA after LIB/LIE; document the read-then-trigger contract at the call
  site. NEEDS-LIVE-VERIFICATION: load a 3-point list, RSS, read OF1 (should be point 0), UP,
  read OF1 (point 1) to confirm the first point is not skipped and the learn span is correct.

### F4 — LOW: `prepare()` omits `CSB` (clear GPIB status) and `LDT` can dip below the 1 ms floor. drivers.py:593, 692.

- The manual's recommended reset sequence is `CSB` (clear GPIB status) then `RST` then the
  leveling/attenuator string (distillation body lines 74-78). `prepare()` starts at `RST` and
  omits `CSB`, so latched status bits from a prior session are not explicitly cleared before the
  campaign. Low impact (reading OSB also clears latched bits; RST resets most state), but adding
  `CSB` first matches the documented procedure.
- `set_list_sweep` computes `LDT {dwell_s*1e3:.3f} MS`; the LDT range floor is 1 ms
  (distillation line 50). A caller passing dwell_s < 0.001 would emit a sub-1-ms value the
  instrument may clamp or reject. Guard `dwell_s` to >= 1 ms when nonzero.

### F5 — LOW / informational: frequency resolution and terminator match.

- `set_freq` formats GHz with `%.9f` = 1 Hz resolution. Standard 683xx resolution is met; a unit
  with Option 11 (0.1 Hz) would be truncated to 1 Hz. Non-issue for an SE sweep. No units error:
  the driver passes the GHz VALUE with the `GH` suffix, matching the manual (NOT Hz-with-HZ, NOT
  a bare number) — the highest-risk off-by-10^n class is clean.
- All native writes rely on linux-gpib EOI termination; the 68367C terminator (CR or CR/LF) is
  selectable and must match the controller (distillation line 138). Bench-verified implicitly.
  NEEDS-LIVE if the unit's GPIB terminator setting is ever changed.

---

## 3. Reference-corpus note (repo policy)

Every command cited above traces to the in-repo distillation
`reference/operator-manuals/anritsu-68000-series-operation.md`, which itself cites the Anritsu
682XXB/683XXB OM (10370-10284) and MG369xB GPIB PM (10370-10366). No external Anritsu manual had
to be fetched for this audit — the in-repo distillation covers CF1/L1/RF0/RF1/OSB/OF/OL and the
full LST list-sweep dictionary. If the F0 SCPI-mode safe-state or the F2 `OI`-vs-`*IDN?` fixes are
adopted, the exact 68367C C-series identity/SCPI behavior is NOT in the corpus (the distillation
uses the 68369B sibling as the documented proxy); acquiring the 68367C-specific programming manual
and distilling it per `reference/CLAUDE.md` would be required before citing 68367C-specific
identity/SCPI behavior in project docs.
