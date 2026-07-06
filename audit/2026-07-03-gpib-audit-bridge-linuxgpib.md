# GPIB-over-TCP bridge: linux-gpib I/O correctness audit

Scope: the low-level linux-gpib usage in the se299 network GPIB bridge. Static code trace + linux-gpib
documentation research only. No live hardware was touched.

Files audited:
- `rf-se/se299/gpib_bridge/ni_gpib_server.py` (bridge server, backend, dispatcher)
- `rf-se/se299/gpib_bridge/protocol.py` (wire protocol)
- `rf-se/se299/drivers.py` (client `NetworkTransport`, `Anritsu68369`, `_bus_op` ENOL recovery)
- cross-checked consumers: `connection.py`, `coordinator.py`, `control_lease.py`, `loop.py`

## Authoritative linux-gpib references consulted

- ibtmo timeout code table (TNONE=0 ... T1000s=17): the code's `_TMO_SECONDS` table matches exactly;
  "actual time before timeout may be greater than the period specified, but never less."
  (https://linux-gpib.sourceforge.io/doc_html/reference-function-ibtmo.html)
- ibsta bits: ERR=0x8000, TIMO=0x4000, END=0x2000 ("set if the last io operation ended with the EOI
  line asserted"; also set on EOS char if REOS enabled), CMPL=0x0100, plus RQS=0x800, SPOLL=0x400.
  (https://linux-gpib.sourceforge.io/doc_html/reference-globals-ibsta.html)
- iberr codes: EDVR=0 (system-call failure; errno in ibcnt/ibcntl), ECIC=1, ENOL=2 (no listeners
  addressed for a write), EADR=3, EARG=4, ESAC=5, EABO=6 (read/write aborted -- timeout or device
  clear), ENEB=7. (Note: the linux-gpib doc numbers EDMA=8, EOIP=10, EBUS=14, ESTB=15 -- the
  "EDMA14/EOIP15" pairing in the task brief is NI-488 numbering, not linux-gpib; the code only uses
  EDVR/ENOL/EABO, which are correct.)
  (https://linux-gpib.sourceforge.io/doc_html/reference-globals-iberr.html)
- ibrd read termination: a read ends on EOI (END), EOS char (if configured via ibeos/REOS), device
  clear, interface clear, byte count, or timeout.
  (https://linux-gpib.sourceforge.io/doc_html/reference-function-ibrd.html)
- ibeos EOS flags: REOS=0x400 (terminate reads on EOS char), XEOS=0x800 (assert EOI on EOS during
  writes), BIN=0x1000 (match all 8 bits). Default eos_mode passed by the `Gpib` class is 0 -> no EOS.
  (https://linux-gpib.sourceforge.io/doc_html/reference-function-ibeos.html)
- `Gpib` Python class constructor defaults: `Gpib(name='gpib0', pad=None, sad=0, timeout=13,
  send_eoi=1, eos_mode=0)`; with `pad` given it calls `gpib.dev(board_index, pad, sad, timeout,
  send_eoi, eos_mode)`. `send_eoi=1` => EOI asserted on the last write byte; `eos_mode=0` => no EOS.

---

## 1. Layer-by-layer trace

### 1.1 Addressing  (A verb -> bind -> Gpib(board, pad))
`LinuxGpibBackend.bind` (ni_gpib_server.py:339-344) does `self._dev = self._gpib_cls(self._board,
pad=int(address))` with `_board` an int (default 0 from `--board`). This is the correct linux-gpib
device-descriptor form: `Gpib(0, pad=18)` -> `gpib.dev(0, 18, sad=0, timeout=13, send_eoi=1,
eos_mode=0)`. Board index 0 = `gpib0` = the single NI GPIB-USB-HS. Correct.
- No secondary address (sad): the A verb payload is a single primary pad; `sad` stays 0. The 8565EC
  (pad 18) and Anritsu 683xx (pad 5) are primary-only, so sad is not needed. (L3)
- `bind` deliberately does NOT `ibsic` per bind (comment ni_gpib_server.py:340) -- correct for a
  shared thread-per-connection bus (an IFC would reset the whole bus under another session). Board-level
  IFC/CIC is expected from external `gpib_config` at VM boot. (L4)

### 1.2 Write termination
`write` / `query` call `self._dev.write(data)` (ni_gpib_server.py:374, 381). With the `Gpib` default
`send_eoi=1`, linux-gpib asserts EOI on the last byte, which is the message terminator both instruments
accept. Driver commands are bare native mnemonics with NO appended CR/LF ("RF0", "CF1 2.0 GH", "OSB",
"TRA?"; drivers.py:594-696), so there is exactly one terminator (EOI) -- no double-termination, no
missing terminator. Correct.
- Minor cross-transport asymmetry (informational): the VISA path (`VisaTransport`) lets pyvisa append
  its write terminator, the bridge path appends nothing (EOI only). Both instruments accept EOI, so this
  is transparent; noted only for completeness.

### 1.3 Read termination
`query` calls `self._dev.read(self._read_bytes)` with `read_bytes=65536` and eos_mode=0 (no EOS).
The read therefore terminates ONLY on END (EOI), on the 64 KiB count, or on timeout. This is the
correct universal choice here because BOTH instruments have binary readbacks where an EOS char would
corrupt termination: the Anritsu `OSB` status read is a single raw binary byte (drivers.py:638-642,
"raw binary, not ASCII") that can legally equal 0x0A/0x0D, and 8565EC traces can be pulled in binary.
Setting an LF EOS would truncate those. So relying on EOI is right, not a defect.
- Residual dependency: OF1/OL1/OSB reads (drivers.py:630-651) only terminate if the Anritsu asserts
  EOI. If the source's GPIB terminator were ever set to CR/LF-without-EOI, these reads would run to the
  64 KiB count or time out. This is mitigated two ways: (a) `_read_is_short` turns a no-END read into a
  loud SHORT_READ error rather than silent garbage; (b) MEMORY records these were live-verified via
  native OF1/OL1/OSB readback on the bench 68367C (so the bench unit asserts EOI). See M2.

### 1.4 Status interpretation (ibsta / iberr / ibcnt)
`_status` (ni_gpib_server.py:346-364) fetches each register independently with per-field try/except, so
a build missing `gpib.iberr()` degrades iberr->0 WITHOUT zeroing the real ibsta/ibcnt (this is the
already-fixed bug; NOT re-reported). Consequence that IS still live: on this guest's build `iberr()` is
absent, so iberr is ALWAYS 0 everywhere it is consumed.
- `_read_is_short(ibsta, ibcnt)` (291-298) uses only ibsta END + ibcnt: `short = ibcnt<=0 or not
  (ibsta & END)`. Correct and independent of iberr. Good.
- `query`'s SHORT_READ path (386-389) sets `cls="SHORT_READ"` explicitly, bypassing `_classify_fault`.
  Good -- short reads classify correctly regardless of iberr.
- `_classify_fault(iberr, ...)` (301-314) switches ONLY on iberr. With iberr forced to 0 and
  `_IBERR_EDVR == 0`, every fault falls through to the EDVR branch -> "ADAPTER_WEDGED". DEVICE_SILENT
  (EABO) and BUS_WEDGED (ENOL) are UNREACHABLE on this build. This is the headline defect. See H1.
- Thread/register safety: linux-gpib ibsta/iberr/ibcnt are thread-local; each connection thread owns its
  own `Gpib` handle; all bus ops + the immediately-following `_status` run under the process-global
  `_BUS_LOCK`. So the status read always reflects that thread's own last op. Correct.

### 1.5 Timeout mapping
`_TMO_SECONDS` (39-40) reproduces the linux-gpib ibtmo table exactly (index 0..17 = TNONE..T1000s).
`_timeout_code(ms)` (246-251) picks the smallest code whose seconds >= the request, i.e. it rounds UP,
matching "actual >= requested, never less" -- reads never time out early. The default `bind` sets
T3s (code 12) from 3000 ms. Correct.
- The loop starts at code 1, so TNONE (never-timeout) is unreachable and any ms<=0 maps to T10us
  (~instant). Reasonable as a "reads are always bounded" safety choice, but a caller passing 0 gets
  spurious instant timeouts rather than "disable." See L1.

### 1.6 Device clear / interface clear / online (recover)
`recover` (405-453) escalates: (1) `gpib.clear(dev.id)` = ibclr = Selected Device Clear to the bound
device only; (2) `gpib.online(dev.id, 0)` then `(dev.id, 1)` = ibonl offline/online on the device
descriptor; (3) fresh `Gpib` handle; (4) serial-poll probe. It never calls `ibsic` (IFC), so it does
NOT reset the bus under another session -- correct for the shared bus. Reads registers after each step
into a journalled trail. The terminal classification uses `_classify_fault` and so inherits H1
(always ADAPTER_WEDGED when the probe fails). Also, recover cannot restore board-level CIC if the board
dropped it (no ibsic by design) -- external re-init needed (L4).

### 1.7 Bus lock / atomicity
`_BUS_LOCK` is a module-global mutex (44). `query` holds it across write+read (378-390), so a query's
write-then-read is ATOMIC vs every other session; no other session can inject a bus op mid-query
(any bus op must take the same lock). `write` holds it for the write. The lease `check` (a separate
`_LEASES` lock) is advisory arbitration layered above the hard bus-level atomicity; the check/op
TOCTOU is benign because `_BUS_LOCK` guarantees physical atomicity regardless. Correct. Note `bind` and
`set_timeout` are intentionally outside `_BUS_LOCK` -- neither touches the bus (ibdev allocates a
descriptor; ibtmo is local config), and each session has its own handle, so this is safe.

### 1.8 Safe-state dead-man
`_send_safe_state` (613-642) runs in the single teardown `finally` (786-791), so all three triggers
(clean EOF, idle/half-open timeout, lapsed keepalive still reaped on disconnect) converge on it.
Correct behaviors verified:
- De-key happens BEFORE `_LEASES.release` (790-791), and `_LEASES.check(pad, session_id)` ignores the
  session's own still-held lease, so no successor can grab the pad in the window between check and write
  (our lease still blocks acquire). Ordering is correct and race-free.
- Skip-if-successor (`check` returns not-ok when ANOTHER session leased the pad) is correct.
- Lapsed keepalive still de-keys: teardown reads `controlled` (the local set), not the live lease
  table, so a TTL-expired session still de-keys its pad. Correct.
- Write failure is caught and journalled, never raised past teardown (641-642). But it is NOT retried
  or escalated -- see M4.
- KEY GAP: `controlled` is populated ONLY by a successful `L` lease (728-729). De-key therefore fires
  only for pads the session LEASED, not pads it merely WROTE to. Keying does not require a lease (W is
  allowed whenever no CONFLICTING lease is held; open bus => allowed). So a keyer that bypasses the
  lease leaves the source hot on crash. See M1.

---

## 2. Ranked findings

### H1 (HIGH) -- Fault classifier collapses to ADAPTER_WEDGED on the deployed build; DEVICE_SILENT and BUS_WEDGED are dead verdicts
File: `gpib_bridge/ni_gpib_server.py:301-314` (`_classify_fault`), consumed at `:276` (`GpibFault`),
`:448-452` (`recover`), and client-side `drivers.py:98,236-239` + `connection.py:255-259`.

Defect: `_classify_fault` branches solely on `iberr`. This linux-gpib build lacks `gpib.iberr()`, so
`_status` returns iberr=0 for every fault (documented at ni_gpib_server.py:346-355 and in project
memory). Because `_IBERR_EDVR == 0`, `_classify_fault(0, ...)` always hits the EDVR branch and returns
"ADAPTER_WEDGED". The EABO->DEVICE_SILENT and ENOL->BUS_WEDGED branches are unreachable. Every
non-answering `recover` and every `_fault`-built GpibFault (a failed write/query/serial-poll) is
misclassified as "the NI adapter is gone, power-cycle it."

Why it matters: the verdict drives remediation. `connection.py:259` escalates an ADAPTER_WEDGED verdict
to terminal FAULT IMMEDIATELY (bypassing the `fault_after` consecutive-failure grace). So a merely
powered-off instrument (true EABO/DEVICE_SILENT) or a transiently wedged bus (ENOL/BUS_WEDGED) takes
the link terminal and tells the operator to physically power-cycle the GPIB-USB-HS, when the correct
action is "turn the instrument on / check the cable." This defeats the entire fault-classification
feature on the actual hardware.

Correct linux-gpib behavior: a timeout aborts with ibsta TIMO=0x4000 set (iberr would be EABO); a
write with no listener returns quickly with ERR=0x8000 set and no TIMO (iberr would be ENOL); a
system-call/USB failure sets ERR with the errno in ibcnt (iberr EDVR). The preserved ibsta and ibcnt
still carry enough signal to distinguish these WITHOUT iberr.

Suggested fix: classify from ibsta/ibcnt when iberr is 0/unavailable, e.g.:
```
if iberr == EABO or (iberr == 0 and (ibsta & TIMO)):        return "DEVICE_SILENT"
if iberr == EDVR or (iberr == 0 and ibcnt in _ERRNO_NAMES): return "ADAPTER_WEDGED"
if iberr == ENOL or (iberr == 0 and (ibsta & ERR)):         return "BUS_WEDGED"
return "FAULT"
```
(Order matters: test TIMO before the generic ERR/ENOL bucket, and gate ADAPTER_WEDGED on a real errno
in ibcnt so a plain timeout is not mislabeled a dead adapter.)

NEEDS-LIVE-VERIFICATION: on the guest, confirm (a) `gpib.iberr` is absent while `gpib.ibsta`/`gpib.ibcnt`
are present, and (b) that a timed-out read sets TIMO and a no-listener write sets ERR-without-TIMO, so
the ibsta-based fallback distinguishes them. Suggested (guest-only, hardware present, no second
accessor): with the source powered OFF, `python3 -c "import gpib,Gpib; d=Gpib.Gpib(0,pad=5);
d.timeout(11);
try: print(d.read(16))
except Exception as e: print('exc',e); print('ibsta=0x%04x'%gpib.ibsta(), 'ibcnt=',gpib.ibcnt())"`
and inspect ibsta for TIMO(0x4000). Do NOT run while the live VMs hold the board.

### M1 (MEDIUM, safety-hardening) -- Dead-man de-key is gated on holding a LEASE, not on having keyed the pad
File: `gpib_bridge/ni_gpib_server.py:625-642` (`_send_safe_state`), `:687` + `:728-729` (`controlled`
only filled by `L`), `:758-766` (`W` allowed with no lease when no conflict).

Defect: the bridge de-keys a safe-state pad on teardown only if the session took an `L` lease on it.
But writing (keying) requires no lease: `_LEASES.check` returns ok whenever no CONFLICTING lease is
held, and with no leases in play the bus is fully open. So any client that writes `RF1` to pad 5
without leasing it -- and then crashes/partitions -- leaves the 40 GHz source keyed, because `controlled`
is empty and `_send_safe_state` returns early.

Exposure in this codebase: the intended controller paths DO lease -- `Coordinator.take_control`
(coordinator.py:123-131) and `InstrumentHub` acquire via `ControlLease` -> `transport.lease`
(control_lease.py:66) before running a campaign, so those are ARMED. However, `loop.py`'s standalone
functions (`acquire_reference`, `chain_sweep`, `validate_devices`, `check_path`, `run_demo`, ...) take a
bare `source` and call `source.rf_on()` directly with NO lease (loop.py:98,199,265,356,620,763,826). If
any live entrypoint drives those against a networked source without going through the Coordinator, the
bridge dead-man does not protect it.

Suggested fix: make the dead-man key off "did this session write to a safe-state pad," not "did it lease
it": track a `keyed: set()` updated in the `W` handler when `backend.address in safe_state`, and de-key
`controlled | keyed` (minus successor-held pads) on teardown. Alternatively, refuse a `W` to a
safe-state pad unless the session holds a lease on it (forces the dead-man to be armed whenever the
source can be keyed).

NEEDS-LIVE-VERIFICATION: confirm every live source-keying entrypoint routes through
`Coordinator`/`InstrumentHub` (armed) and none calls `loop.py` source functions on a bare
`NetworkTransport`. Grep target: entrypoints that build a networked source and call the loop functions
directly.

### M2 (MEDIUM) -- Read termination has no EOS fallback; source ASCII readbacks depend on the Anritsu asserting EOI
File: `gpib_bridge/ni_gpib_server.py:378-390` (`query`/`read`), `bind` never calls ibeos; client
`drivers.py:630-651`.

Defect/nuance: eos_mode=0 => reads end only on END (EOI)/count/timeout. This is CORRECT and deliberate
(binary OSB byte + binary traces would be corrupted by an EOS char, so an EOS must NOT be set). The
residual risk is that OF1/OL1/OSB source reads terminate only because the Anritsu asserts EOI; if the
source's terminator were configured CR/LF-without-EOI, those reads would run to 64 KiB or time out and
surface as SHORT_READ/timeout. Not silent corruption (mitigated by `_read_is_short`), and MEMORY notes
live OF1/OL1/OSB verification on the bench 68367C, so the bench unit does assert EOI.

Correct linux-gpib behavior: with no REOS, ibrd ignores CR/LF as terminators and waits for EOI.

Suggested fix: none to the code (do NOT add an EOS -- it would break the raw OSB byte). Instead, encode
the assumption: assert/document that the source's GPIB output terminator must include EOI, and keep the
`_read_is_short` guard (it already converts a missing-EOI read into a loud error).

NEEDS-LIVE-VERIFICATION: confirm the Anritsu GPIB output terminator asserts EOI on the last byte
(front-panel GPIB config / OI readback). Guest-only, and only while the live VMs are idle.

### M3 (MEDIUM) -- Un-arbitrated Z recover / ping operate on the caller's bound pad, so an observer can clear a device another session controls
File: `gpib_bridge/ni_gpib_server.py:400-403` (`ping`), `:405-453` (`recover`), dispatched at
`:767-768` with no lease check (Z is intentionally un-arbitrated).

Defect: `recover` sends SDC (ibclr) and ibonl offline/online to `self._dev` = the CALLER's bound pad.
Z is un-leased by design (the wedged-bus escape hatch), and binding a pad (A) needs no lease. So an
OBSERVER that binds pad 5 and sends `Z recover` will Selected-Device-Clear and re-online the source
while another session actively controls it -- SDC can reset the synth's state out from under the
controller. `Z ping` similarly serial-polls the controlled device (benign state-wise, but clears RQS).

Correct behavior trade-off: recover legitimately must run on a wedged bus even under a lease, so a hard
lease check would defeat it. But it should not silently clear a HEALTHY leased device for a non-holder.

Suggested fix: allow recover unconditionally only when the caller holds the lease on (or no one holds)
the bound pad; when ANOTHER session holds it, either refuse with a clear message or restrict recover to
board-level, non-device-clearing steps (ibonl/probe) and skip the SDC. Log the cross-session recover
prominently.

### M4 (MEDIUM) -- Safe-state write failure is logged but not retried or escalated; a wedged bus at teardown can leave the source keyed
File: `gpib_bridge/ni_gpib_server.py:636-642`.

Defect: if `backend.bind`/`backend.write(RF0)` throws during dead-man teardown (e.g. the bus is wedged),
the code logs "safe-state send FAILED" and moves on. For a 40 GHz emitter dead-man, a single silent
failure means the transmitter can remain keyed with only a journal line as evidence.

Suggested fix: retry the safe-state write a bounded number of times (fresh handle each try), and on final
failure emit a loud/elevated log (and, if a hardware line is available, an out-of-band de-key). At
minimum, distinguish this journal line at a higher severity so monitoring can alert.

### L1 (LOW) -- `_timeout_code` cannot select TNONE; ms<=0 maps to T10us
File: `gpib_bridge/ni_gpib_server.py:246-251`. The `range(1, ...)` start makes never-timeout
unreachable (a sound "reads are bounded" choice) but also maps a 0/negative request to a ~instant
10 us timeout instead of a sane floor. Suggest clamping a non-positive/very-small request to a minimum
sensible code (e.g. T1s) rather than T10us.

### L2 (LOW) -- Client ENOL recovery depends on the GpibError string still containing the ENOL mnemonic
File: `drivers.py:101-106` (`_is_enol`), `:220-239` (`_bus_op`). Because the server's `cls` is broken to
ADAPTER_WEDGED for ENOL (H1), the client's ENOL re-address/recover path triggers ONLY via a substring
match ("no listener"/"enol") on the framed error text -- which is `f"write failed: {GpibError}"`. If the
deployed build's `GpibError.__str__` omits the iberr name, `_is_enol` returns False and ENOL recovery
never fires (the error just propagates). Fixing H1 (server sends a correct BUS_WEDGED cls) would let the
client key off structured classification instead of string-sniffing.
NEEDS-LIVE-VERIFICATION: capture `str(gpib.GpibError)` for a no-listener write on the guest build and
confirm it contains "ENOL"/"no listener".

### L3 (LOW / informational) -- No secondary-address (sad) support
File: `gpib_bridge/ni_gpib_server.py:339-343`; A verb carries only a primary pad. Fine for the pad-5 /
pad-18 primary-only instruments; noted in case a future device needs sad.

### L4 (LOW / informational) -- recover cannot restore board-level CIC
File: `gpib_bridge/ni_gpib_server.py:340-341, 416-435`. By the shared-bus no-IFC policy, recover uses
only device-scoped ibclr/ibonl; if the board loses controller-in-charge it needs external
`gpib_config`/ibsic re-init. Acceptable, documented here for completeness.

### L5 (LOW / informational) -- `_read_is_short` would misflag a legitimately empty EOI-only reply
File: `gpib_bridge/ni_gpib_server.py:291-298`. `ibcnt<=0 -> short`. No current query returns zero bytes
(all driver queries return data; OSB returns 1 byte), so this is not exercised; noted in case a future
query legitimately returns an EOI-terminated empty response.

---

## 3. Positives confirmed (no change needed)
- Timeout table byte-for-byte matches linux-gpib's ibtmo table; rounds UP (never early). (1.5)
- The already-fixed per-field `_status` defaulting correctly preserves ibsta END + ibcnt when iberr()
  is missing; SHORT_READ classification is iberr-independent. (1.4)
- recover uses device-scoped ibclr + ibonl, never bus-wide ibsic -> does not reset the bus under other
  sessions. (1.6)
- END-only (no EOS) is the correct universal terminator given binary readbacks; write EOI via the
  `Gpib` `send_eoi=1` default; no double/missing termination. (1.2, 1.3)
- `_BUS_LOCK` makes a query's write+read atomic vs all sessions; no mid-transaction injection. (1.7)
- Dead-man ordering (de-key before lease release; skip-if-successor; lapsed-keepalive still de-keys) is
  correct for the leased paths. (1.8)
- Board index 0 (int) is the correct `gpib.dev()` device-descriptor usage. (1.1)
