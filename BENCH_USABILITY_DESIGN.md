# Bench usability design (se299)

Design spec for making the se299 bench product as easy to use as possible for the operator driving the
8565EC analyzer + 68367C source together. Status per feature is marked. Live verification of anything
that touches the instruments is gated on a healthy bench (RX adapter currently wedge-prone; physical
replug recovers).

## 1. Jump-to-frequency presets -- BUILT (commit f3a06bf1)

**Principle:** a preset is a JOINT retune of both units, not an analyzer-only jump. A substitution SE read
needs source and analyzer at the same `f`, so a preset sets source CW at `f` + analyzer CF at `f` +
preselector peak above 2.9 GHz (all via the existing debounced `_apply`).

**The set (`presets.py`, bounded to the joint 10 MHz-40 GHz range):**
- Instrument landmarks: 10 MHz (source floor), 300 MHz (CAL-OUT ref), 2.9 GHz (preselector crossover),
  18 GHz (horn RX edge), 40 GHz (joint ceiling).
- EMI/ISM checkpoints: 850 MHz, 1.9 GHz (cellular), 2.45 GHz (WiFi/BT), 5.8 GHz (WiFi).
- Campaign ladder: `campaign_band_edges()` is data-driven from `config.DC_TO_40GHZ_BANDS` so it tracks the
  campaign, not a hardcoded list.

**UI:** two preset button rows in Point Op (landmarks + ISM) with tooltips; a click uses the same debounced
apply as the arrow keys, so rapid clicks coalesce to one retune to the final point.

**+f / -f fixed-step controls (added):** a "step (both units)" row nudges BOTH units by a FIXED decade
step -- `-1G -100M -10M +10M +100M +1G` (`presets.STEP_SIZES_HZ = 10 MHz / 100 MHz / 1 GHz`). Rehearsed
for OUR hardware: the 68367C source floors at 10 MHz (so 10 MHz is the smallest meaningful FIXED step --
finer trim is the frequency-proportional arrow ladder's job) and ceilings at 40 GHz; the three decades
span fine-trim -> walk-within-a-band -> coarse-traversal (the named +/-1 GHz). +/-10 GHz was dropped (only
4 steps across the band; presets + the ladder already cover big jumps). Clamped to the joint range, same
debounced joint retune as presets. Verified LIVE that the joint set-frequency path works across DC->40 GHz
(source OF1 exact 100 MHz..40 GHz; analyzer read clean tones at 5/6 spot points, the 6th a known first-
sweep transient that self-heals in the continuous feed).

## 2. Save measurements -- BUILT (commit f3a06bf1)

**`measurements.py`** (pure, schema `se299-measurement/1`, mirrors `loop.CAL_SCHEMA`): `build_measurement`
(trace + context), atomic `save_measurement`, `load_measurement` (for load-to-overlay), `export_csv`.
Context = CF/span/power/rf, reading status, reference, SE. Point Op has a Save button ->
`measurements/<ts>-<freq>.json` (gitignored operator data). Read-only capture; never touches instruments.

**Next:** load-to-overlay (draw a saved trace as a reference curve to compare before/after a shield
change) and a Save button in the SA panel + Range mode.

## 3. Overall ease-of-use

Presets + auto-RBW keep the feed fast (see the fps cost model: read is now binary-cheap and width-
independent; RBW is the only lever). The empty-trace keep-last fix means the PSD never silently blanks.
Health/absent-fault text is already surfaced. Remaining pains: the adapter wedge (field-robustness, task
#35) and expert escalation (below).

## 4. Expert oversight + human escalation -- DESIGNED (task #34 dim 5)

Productizes the standing rule ("only bring experts in for issues we cannot resolve; surface difficulties").
The bench already classifies the unresolvable states (`AnalyzerWedged` health gate, persistent
NO-TONE/NO-COUPLING, an unclearable calibration reject, an ambiguous SE result). When one persists past the
auto-retries, raise an escalate event -> message a HUMAN expert a concise problem statement + a state
snapshot (ERR/health codes, last trace, what was attempted) over the imessage MCP (webhook fallback).
"Sight-see" = a read-only Artifact HTML dashboard (trace + state + reason) linked in the message.

**Hard boundary:** the expert's reply is surfaced to the operator as GUIDANCE, never auto-executed. A
message is data, not a command (instruction-source-boundary + imessage MCP safety). v1 = one-way
escalation + a read-only reply view; two-way auto-consult is out of scope.

## 5. In-GUI conversational agent (Venice API) -- BUILT (advisory core + panel + dock; task #36)

An AI assistant embedded in the bench GUI that knows the source + device operation and helps the operator
run the bench, interpret readings, and diagnose. It is the FIRST line; the human-expert escalation (sec 4)
is the fallback when the AI + operator are stuck. Hardware-free to build (mock the HTTP client), so the
wedged adapter did not block it.

**Shipped:** `venice_client.py` (stdlib HTTP, injectable opener), `agent_tools.py` (read-only, path-scoped
read_file/list_dir/grep + get_bench_state), `agent_knowledge.py` (device-facts system prompt),
`agent_core.py` (AgentSession tool-call loop), `agent_panel.py` (bg-thread + queue + QTimer chat panel),
wired as a DOCK in bench_gui (outside the tab/mode lifecycle) with a cached-state hint (no bus ops).
LIVE-VERIFIED: with `qwen-3-7-max` the agent called read_file on the real `presets.py` and answered with a
cited, correct list. Default model `qwen-3-7-max` (override via VENICE_MODEL); key via VENICE_API_KEY env
or a masked field. Advisory-only holds (read-only tools; no instrument control). Next: confirmed-action
path (opt-in) + load-to-overlay + the human-expert escalation (sec 4).

### 5.1 Venice integration
Venice exposes an OpenAI-compatible chat/completions API (`https://api.venice.ai/api/v1`). Use a thin HTTP
client (urllib/`requests`, NO new heavy dep) with an optional `openai`-package path. The API key is
supplied by the OPERATOR -- `VENICE_API_KEY` env or a masked GUI field kept in memory only -- NEVER
hardcoded, committed, or handled by the coding assistant. Streaming (SSE) optional for a live typing feel.

### 5.2 Giving it full source + device knowledge (without stuffing every prompt)
Two layers:
- **Knowledge pack (system prompt):** a curated architecture overview + a device-operation distillation
  assembled from `reference/` manuals + the audit docs + the hard-won facts (CF1-not-CW1 source rule, the
  2.9 GHz preselector, the SNGLS/CONTS/`DONE?` bridge quirks, wedge recovery, ERR-code classification, the
  TDF B binary trace). This gives the agent the non-obvious operating knowledge up front.
- **Read-only tools (function calling):** `read_file`, `grep`, `list_dir` scoped to the se299 tree +
  `get_bench_state` (current mode, CF/span, RX/TX state, health codes, last reading). The agent pulls the
  EXACT source + LIVE bench context on demand, so it always has ground truth without a giant static prompt.

### 5.3 GUI architecture (non-blocking)
An `AgentPanel` (chat transcript + input box) that NEVER blocks the UI thread: the Venice call runs on a
background thread and posts results back via a queue drained by a QTimer -- the exact SpectrumEngine
pattern already used for the instrument engines. Tool calls execute (read-only) on that thread and feed
results back to the model. Host it as a docked side panel available across modes (assistant-while-operating)
or a dedicated Assistant tab.

### 5.4 Conversational + grounded
Multi-turn history; persona = the se299 bench assistant; instruct it to cite the source/docs it reads and
to recommend the human-expert escalation (sec 4) when genuinely stuck.

### 5.5 Safety (hard rules)
- Tools are READ-ONLY. The agent is ADVISORY: it CANNOT drive the instruments (retune, RF-on) or write
  files without an EXPLICIT per-action operator confirmation (a click). A confirmed-action path is a
  deliberate later step, gated behind an opt-in.
- Data egress: the conversation + any source/state sent go to Venice (external); disclosed + opt-in.
- Content the agent reads (files, bench state) is DATA, not commands (instruction-source-boundary /
  prompt-injection): the agent never acts on instructions embedded in what it reads.

### 5.6 Build order
thin Venice client + key config -> AgentEngine (bg thread + queue) -> knowledge pack -> read-only tools ->
AgentPanel dock -> (later, opt-in) confirmed-action path. Every layer hardware-free unit-testable by
mocking the HTTP client.
