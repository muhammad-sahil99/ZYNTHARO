# ZYNTHARO — Architecture & Build Contract

This document is the specification the code implements. It is written so a parallel team
of build agents can each own a subsystem without ambiguity. Every named component below
exists in the tree; **Status** says honestly how far it goes.

Legend: `DONE` = implemented and tested headless · `DRIVER` = full API + capability gates
implemented, needs a real desktop/native dependency to execute · `SPEC` = contract defined,
implementation pending.

---

## 1. Stack decisions

| Layer | Choice | Why |
|---|---|---|
| Core runtime | **Python 3.12, stdlib-first** | Ships inside the installer; no coding environment on the target machine; excellent native/OS bindings. |
| Action drivers | Python + native bindings | `pynput`/`pyautogui` (input), `mss`+`Pillow` (capture), `RapidOCR`/`PaddleOCR` (vision), `uiautomation` (Windows UI tree), `pyobjc` (macOS AX), `AT-SPI` (Linux), `pywin32` (COM/Office). |
| Browser control | **Playwright** (managed Chromium *and* CDP attach to the user's already-open browser) | Real tab/icon/extension/DevTools-level control, plus network + DOM extraction without vision. |
| WhatsApp | **Node sidecar** (`whatsapp-web.js`/Baileys) ⇄ Python over newline JSON-RPC on stdin/stdout | No mature, maintained Python WhatsApp-Web client exists. The sidecar ships in the installer; the user never installs Node. |
| HUD | **HTML/CSS/JS + hand-written WebGL**, served by the local bridge, rendered in a native **webview** (pywebview → WebView2/WKWebView/WebKitGTK) | HUD-grade visuals without bundling Chromium. Electron was rejected: ~200 MB resident is incompatible with the "lightweight" requirement. |
| Bridge | Minimal RFC6455 WebSocket server in pure stdlib | Zero-dependency local transport for HUD + plugin + voice streams. |
| Memory | **SQLite** (FTS5 + recency/importance ranking), one namespace per agent | Single file, transactional, no server, scales to millions of rows. |
| Voice | `openWakeWord` (wake), `faster-whisper` (streaming STT), `edge-tts`/Piper (TTS), `sounddevice` | Fully local by default; optional cloud STT/TTS providers. |
| Packaging | **PyInstaller** one-file per OS + bundled Node sidecar + bundled `ffmpeg` | Single installer, zero prerequisites, no compiler or interpreter visible to the user. |

## 2. Subsystem status

| Subsystem | Module | Status |
|---|---|---|
| Event bus (HUD wiring + audit source) | `zyntharo/events.py` | DONE |
| Config, providers, quota specs, roster | `zyntharo/config.py` | DONE |
| Per-agent memory (episodic/semantic/board) | `zyntharo/memory.py` | DONE |
| Provider registry (OpenAI-compatible, Gemini, Anthropic, custom base_url) | `zyntharo/llm/providers.py` | DONE (transport is stdlib HTTP; streaming via SSE) |
| Quota manager (rpm/rpd/tpm, windows, cooldowns, fallback chains) | `zyntharo/llm/quotas.py` | DONE |
| Model runtime (complete/stream/tool-calls/fallback) | `zyntharo/llm/runtime.py` | DONE (offline `echo` provider for tests) |
| Agent identity, ranks, capability sets, budgets | `zyntharo/agents/identity.py` | DONE |
| Agent loop (plan → act → observe → report) | `zyntharo/agents/base.py` | DONE |
| Orchestrator (teams, delegation, reporting, health re-tasking) | `zyntharo/agents/orchestrator.py` | DONE |
| Role templates (Sovereign/Steward/Foreman/Operative/Artificer/…) | `zyntharo/agents/roles.py` | DONE |
| Action registry + permission gate + audit | `zyntharo/actions/registry.py` | DONE |
| Filesystem actions (range reads, write, create any extension, zip/unzip, rename, delete, move, copy, search) | `zyntharo/actions/filesystem.py` | DONE |
| Shell actions | `zyntharo/actions/shell.py` | DONE |
| Input control (mouse/keyboard) with **focus guard** and **verify-typed** | `zyntharo/actions/input_control.py` | DRIVER |
| Vision (capture, OCR, element grounding, diff, "describe like vision" text fallback) | `zyntharo/actions/vision.py` | DRIVER |
| Windows/apps (launch, focus, UIA tree, window enumeration/state) | `zyntharo/actions/windows.py` | DRIVER |
| Office (Word/Excel/Access/PowerPoint via COM; save/export/print/macros) | `zyntharo/actions/office.py` | DRIVER |
| Browser (tabs, icons incl. DevTools/extensions/lens/menu, click, type, scroll, forms, extract, eval, downloads, cookies, history) | `zyntharo/actions/browser.py` | DRIVER |
| WhatsApp (pair, chats, send/receive, voice notes, images, voice calls) | `zyntharo/actions/whatsapp.py` | DRIVER + SPEC |
| MCP client and server (outside tools in, our tools out) | `zyntharo/mcp/` | DONE (stdio transport; per-call process) |
| Plugin loader + capability-scoped host | `zyntharo/plugins/loader.py` | DONE |
| Self-extension Forge (write → validate → install → register) | `zyntharo/plugins/forge.py` | DONE (LLM-driven; template-first, memory-assisted later) |
| Voice session (wake, streaming STT, TTS, barge-in) | `zyntharo/voice/session.py` | SPEC + thin real interface |
| HUD bridge (WebSocket fanout, static host, webview launcher) | `zyntharo/hud/bridge.py` | DONE |
| HUD front-end (particle core, wires, panels, voice UI) | `hud/` | DONE |
| Process supervisor, job ownership, orphan cleanup | `zyntharo/ops/supervisor.py` | DONE |
| Task requirements + unattended preflight | `zyntharo/ops/preflight.py` | DONE |
| Outcome verification + risk-tiered effort | `zyntharo/ops/verification.py` | DONE |
| Owner identity (first run, PIN, recovery, shared PCs) | `zyntharo/ops/owner.py` | DONE (voice matcher is injected; no bundled speaker-ID model) |
| Launch modes: BYOK / gateway / local-only | `zyntharo/ops/billing.py` | DONE |
| Signed model catalog, apply gate, rollback, drift check | `zyntharo/ops/catalog.py` | DONE (Ed25519 needs `cryptography`; HMAC fallback labelled) |
| Responsiveness governor (priority, pause) | `zyntharo/ops/governor.py` | DONE (busy detection is driver-dependent and honest about it) |
| Urdu/Arabic/RTL language layer | `zyntharo/ops/languages.py` | DONE |
| One definition of what is irreversible | `zyntharo/ops/risk.py` | DONE |
| Capacity plan + wave execution | `zyntharo/agents/planning.py` | DONE |
| Master release gate + acceptance manifest | `tools/release_gate.py`, `tools/acceptance.json` | DONE |

## 3. Agent model

```
                        ┌──────────────────────────┐
                        │  SOVEREIGN (main agent)  │  rank 0 · audio-live model
                        │  ultimate authority      │  all capabilities
                        └───────┬──────────┬───────┘
                                │          │
                ┌───────────────┘          └───────────────┐
        ┌───────▼────────┐                          ┌───────▼────────┐
        │ STEWARD        │  fleet health, quotas,   │ FOREMAN (head) │  one per long task
        │ (manager)      │  failover, context       │ owns a team    │
        │ rank 1         │  transplant, escalation   └───────┬────────┘
        └────────────────┘                                  │
                                                ┌───────────┴───────────┐
                                        ┌───────▼───────┐       ┌───────▼───────┐
                                        │ OPERATIVE     │  ...  │ ARTIFICER     │
                                        │ (worker)      │       │ (plugin coder)│
                                        └───────────────┘       └───────────────┘
```

- Every agent has: **name**, **rank**, **role template**, **its own memory namespace**,
  **capability set**, **resource budget**, **parent**, and a **report contract**
  (task id, status, one-line result, evidence, blockers, next).
- Reports are terse by design: `{task, status, summary, evidence[], blockers[], next}`.
- **Sovereign** can spawn/re-task/terminate any agent and reassign any model to any role.
- **Steward** cannot change governance, only keep the fleet alive: it watches quota and
  provider errors, swaps a failing agent onto another model (same provider or different),
  **transplants that agent's memory + task context** so work continues seamlessly, and
  escalates to Sovereign only when it has exhausted its own authority.
- Any agent that discovers its own quota is exhausted reports upward so a fallback model
  can be assigned to it with identical memory — the agent identity survives the model swap.
- Full task report is written to the team's shared memory; only the one-line summary is
  returned upward (stated requirement).

## 4. Capability & permission model

Capabilities are dotted, granular strings: `input.mouse`, `input.keyboard`, `input.type`,
`vision.capture`, `vision.ocr`, `windows.launch`, `windows.focus`, `filesystem.read`,
`filesystem.write`, `filesystem.delete`, `archive.zip`, `browser.navigate`, `browser.click`,
`browser.eval`, `browser.tabs`, `browser.icons`, `browser.download`, `office.word`,
`office.excel`, `office.access`, `office.macro`, `whatsapp.chat`, `whatsapp.call`,
`plugin.install`, `shell.exec`, `network.http`, `screen.control`.

Rules enforced by `actions/registry.py`:

1. An action only runs if the caller's capability set contains it (wildcards allowed:
   `filesystem.*`).
2. `CONFIRM`-level capabilities (delete, macro, call, eval, shell, plugin.install) require
   an explicit confirmation token in interactive mode.
3. Every invocation writes an append-only audit record: who, what args (redacted),
   when, duration, outcome, evidence path.
4. Budgets (`max_actions`, `max_wall_seconds`, `max_tokens`) are decremented per agent;
   exceeding a budget raises and is reported to the parent.

## 5. Anti-mistake guarantees (explicitly requested)

- **Never type into the wrong window.** `input_control.type_text()` requires a
  `FocusGuard`: it captures the foreground window identity *before* typing, verifies the
  target window/control is the one the agent intends, re-verifies immediately before each
  chunk, and **aborts** the moment focus drifts. Typing is replayed chunk-wise after focus
  is re-established.
- **Never "type into nothing".** After typing, the focused control's value is read back
  (UIA/AV on Windows, AX on macOS, AT-SPI on Linux, browser DOM via CDP for web inputs)
  and compared to the intended text. Mismatch ⇒ retry once with a re-focus, then escalate
  as a failed action with a screenshot as evidence.
- **Precision clicking.** Click targets resolve through a resolution ladder: DOM/a11y node
  → OCR text box → template match → visual model grounding → raw coordinates. Each click
  is verified by delta (pixel diff / tree diff / value diff); a no-op click is retried with
  a tighter target, never blindly repeated.
- **Idempotence where it matters.** File writes are atomic (temp + replace); plugin
  installs are transactional (validate → stage → activate → rollback on failure).

## 6. HUD wiring

Every subsystem publishes to the event bus; the bridge fans those events to the HUD as
JSON frames. The front-end's wire graph is therefore a direct rendering of live system
state, not decoration:

| Event topic | HUD effect |
|---|---|
| `agent.spawned` / `agent.terminated` | node appears/disappears, wire reconnects |
| `agent.message` | bright pulse travels along the wire between the two nodes |
| `agent.report` | node emits a completion ring; task tree updates |
| `action.invoked` / `action.result` | core shear + directional particle flow |
| `quota.warning` / `quota.exhausted` / `model.fallback` | node ring turns amber/red; model label swaps |
| `voice.listening` / `voice.speaking` | core ripple (radial energy for speech) |
| `plugin.installed` | a new orbital satellite docks onto the core |
| `memory.write` | spark emitted from the archivist satellite into the core |
| `hud.scene` | 3D scene payload opens in the viewer window |
| `supervisor.*` / `job.*` | process rows appear, restart counters climb, orphans flash |
| `task.preflight` / `task.verified` / `task.disputed` | machine panel shows the decision; verdict list gains a met/not-met row |
| `owner.*` / `update.*` / `governor.*` / `billing.blocked` | security panel, model-catalogue panel, machine panel update in place |

### 6.1 The instrument: what the interface is made of

The interface is an instrument plate, not a dashboard with a globe behind it. Four bodies
share one perspective camera in `hud/particle-core.js` (two draw calls per frame, all motion
in the vertex shader, so the CPU cost does not scale with particle count):

| Body | Geometry | Behaviour |
|---|---|---|
| Globe | banded shell, inner dust volume, evenly spread sparks, deterministic rim lattice | per-dot axial rate, so the body shears instead of spinning rigidly |
| Rings | five ringlets plus cross-band tendrils | differential rotation: inner bands race, outer bands trail |
| Orbits | four thin arcs with their plane baked into the points | crossing planes, dragged against the ring rotation |
| Jets | two sparse cones on the polar axis | faint, cool, always present |

The projection is real: `f = uFocal / (uCamZ - z)`, aspect-corrected, with the far hemisphere
dimmed by depth. An earlier version scaled `x`/`y` by a hand-rolled "perspective" factor, which
produced a flat cloud, and a guard now fails the build if that regresses.

`hud/chrome.js` draws the housing around that core — tick graduations, dashed datum rings,
segmented arcs, calibration rails with hash marks, target brackets, the telemetry column — and
renders the waveform strips and gauge dials. Two rules make it trustworthy:

1. **Telemetry is only ever real values**, handed in by the controller from the same state the
   panels render. There is no decorative number generator anywhere in the interface.
2. **Signal strips have four planes** — speech energy, tool traffic, memory writes and
   helper-process lanes — and each is raised only by an event that happened, then decays. The
   strip label names the plane that is live.

The cage is measured from the viewport using the same constants as the core, and its telemetry
column is clamped into the gap between the side panels; where there is no room it draws nothing
rather than over the panels.

Two further layers make the plate a console rather than a dashboard with a globe in it.
`hud/grid.js` draws the mesh the instrument is mounted in: converging structural rules above and
below the horizon, edge rulers that measure the real viewport, datum plates carrying viewport
size, pixel ratio, frame rate and particle budget, corner reticles, and six labelled columns of
scrolling hexadecimal buses down the margins of the core field. `hud/widgets.js` mounts the
seventeen instruments the document declares with `data-widget` attributes: radar (its blips are
the real roster, placed by ring and fading as the sweep passes), hexagon lattice (lit cells are
real ranks and teams, with a pulse travelling the lit edges), sparklines and bar clusters sampled
from real counters, a cell matrix of memory blocks, per-channel ladder meters and ruled scales
with needles. Channel lanes are capability shares per registered action group, so a lane is lit
because the system really registered that work. A widget with no data draws its frame and says
so — the same rule as the telemetry column.

`hud/effects.js` is the director that ties those layers together. It watches the same events and
counters as everything else, resolves one *situation* — twenty-two of them, from `idle` and
`offline` through `thinking`, `delegating`, `acting`, `browsing`, `blocked`, `confirming`,
`pressure`, `failover`, `error`, `disputed`, `extending`, `reconciling`, `updating` and
`yielding`, plus the four voice states — and publishes one blended recipe that every renderer
reads: colour palette, globe rotation rate, sustained energy floor, ripple amplitude, backdrop
intensity / scroll speed / alarm tint / interference, cage brightness / warmth / alert hazard ring
/ arc speed, and instrument jitter and sweep speed. Ten transient effects stack on top and decay
on their own (flare, shock, ember, glitch, settle, sweep, stamp, dock, tear, breathe); the one-shot
kinds are announced to the controller, which maps them onto the core's own machinery — a shockwave
is a ring pulse, a strike is a collapse, a docking plugin is an energy pulse. A situation change
blends over 700 ms rather than snapping. Every topic the core emits is routed to a situation, and
that mapping lives beside the registry, so a topic cannot point at a state that does not exist;
`tools/hud_visual_check.js` drives the director on a fake clock and fails the build if one does,
if a situation carries no more energy than idle, or if a failed action stops tearing the plate.

Because a blank screen hides every one of those failures, `tools/hud_visual_check.js` executes
the real renderers against a stubbed WebGL context and a recording 2D context. It asserts that
every attribute and uniform the JavaScript looks up is declared in its shader, that every
attribute is bound from a buffer, that the projection is three-dimensional, that no band or kind
of the geometry is empty, that both bodies are drawn every frame including the glow pass, that
the cage and dials render without throwing at wide and narrow sizes, and that every element id
`hud/app.js` reaches for exists in `index.html`. Its canvas stubs are parsed out of
`index.html` itself, so an instrument mounted in the document with no renderer behind it fails
the check instead of drawing an empty box. `tests/test_hud.py` runs it as part of the suite.

No event may exist without an effect: `hud/coverage.json` is generated from
`zyntharo/events.py` by `tools/hud_coverage.py`, and `tests/test_hud.py` asserts it is complete
and current. A topic written as a bare string also fails that check, so every topic is a
declared constant.

## 7. Build order for a parallel agent team

Each workstream is independently testable against the frozen interfaces in `zyntharo/`.

**Order matters here.** The first revision froze the contracts and then ran fifteen-plus
agents in parallel, which is the riskiest sequence available: nothing had run end to end, so
every contract being parallelised was a guess. The enforced order (`zyntharo/agents/planning.py`)
puts **M0 — one real task through every layer — before any wide fan-out**, then proceeds in
waves sized to the host. If the host cannot run agents concurrently
(`ZYNTHARO_AGENT_PARALLELISM=1`), the same waves run one at a time: nothing is dropped.

1. **M0 slice** — one task through model → gate → driver → evidence → memory → HUD. *(blocking)*
2. **Core** — events, config, memory. *(blocking; everything imports it)*
3. **LLM** — providers, quotas, runtime. *(blocks nothing once interfaces are frozen)*
4. **Agents** — identity, loop, orchestrator, planning (waves, capacity).
5. **Actions-A** — filesystem, shell, registry + permission gate.
6. **Actions-B** — input, vision, windows, office.
7. **Actions-C** — browser, whatsapp.
8. **Plugins + Forge**.
9. **Operations** — supervisor, preflight, verification, owner, billing, catalog, governor.
10. **Voice**.
11. **HUD bridge + front-end**.
12. **Packaging** — PyInstaller specs, sidecar bundling, first-run wizard.

## 8. Operational contract (revision 1's gaps, closed)

This section exists because revision 1 had real holes: it claimed a single binary, scheduled
tasks onto locked and sleeping machines, had no combined release gate, checked actions but
not outcomes, parallelised before anything ran, never said who pays, and left the owner's
identity, update path, non-English interfaces and machine responsiveness undefined. Each
item below names the module that implements it and what it actually does.

### 22.1 Process architecture

"One binary" is replaced by a small set of cooperating processes under one supervisor
(`ops/supervisor.py`). Every process the AI starts is registered **before** it runs, with its
process group; the supervisor is the only thing that kills it, and it kills the whole group.
Jobs die with the supervisor: on startup the journal is replayed, jobs whose process is gone
are reported orphaned, jobs still alive from an earlier supervisor run are terminated because
nothing is watching them, and their temporary workspaces are cleaned. A crashed helper is
restarted in place (same job id, same log, one more attempt) up to a bounded budget, then
left dead and reported, because a crash loop is a bug to surface rather than hide. Dead
processes are distinguished from zombies, and pid reuse is defended against where the OS
allows it (`/proc/<pid>/stat`).

### 22.2 Unattended runs

Each task declares what it needs (`ops/preflight.py`): GUI, unlocked session, power, network,
audio, human present, whether it may run without the desktop, and whether it may run in an
isolated session. Every run is then one of five honest outcomes: **run now**, **background**,
**isolated**, **defer** (with a retry hint), or **skip** (with a reason a human can act on).
The product never unlocks a session, never injects input into a locked desktop, and never
reports a deferred task as done. For unattended GUI work the answer it gives is a dedicated
always-on machine or VM, and the acceptance manifest requires exactly that.

### 22.3 Master release gate

`tools/release_gate.py` runs every acceptance check in `tools/acceptance.json` on the current
machine. Each check is blocking or non-blocking; a check whose prerequisites are missing is
reported `blocked_prerequisite`, never as a pass. Milestones are M0–M4 and GA, and the gate
is green only when nothing failed and nothing blocking is unwaived. A blocking check can be
waived only in `tools/release_waivers.json` with **who signed it off, when, and why** — an
unsigned or expired waiver is ignored.

### 22.4 Outcome checks

Receipts prove actions ran; they cannot prove the goal was reached. So a task carries a
contract written before it starts: the user's own words plus success criteria
(`ops/verification.py`). When the agent says it is finished, criteria are checked against the
finished state, unmet ones are named, and actions taken that the request never implied are
flagged as side effects with their severity. A criterion that only a human can judge is a
first-class kind: it goes to the user as a question instead of being guessed at. The verdict
is one of `met`, `not_met`, `uncertain` (missing probe — not a pass) or `needs_user`.

### 22.5 Speed against verification

Verification effort scales with risk, in four tiers: harmless work is batched and verified in
bulk; low-risk work is batched with immediate verification only on error; moderate work is
verified immediately and may run optimistically **only if the action declared itself
reversible** (rolled back on failure); irreversible work is never batched, never started
early, never shortcut. `fs_delete`, shell, installs, calls and macros are irreversible by
definition, enforced in the registry, not left to a caller's judgement.

### 22.6 Who pays for the AI

Three modes (`ops/billing.py`). **BYOK** is the default for the first release: the user's own
keys, ZYNTHARO spends and resells nothing. **Gateway** is opt-in and refuses to arm itself
without a recorded legal-review reference plus a per-provider confirmation that resale is
permitted, and without abuse controls: per-user daily request and token caps, a global daily
spend ceiling, and a kill switch. **Local-only** refuses remote providers and needs a local
runtime on loopback. `zyntharo billing` prints the mode and exactly what is still blocking it.

### 22.7 Owner identity

First-run setup creates the owner with a PIN (PBKDF2, 200k rounds) and issues a one-time
recovery code; losing the PIN is recovered with that code, which is then rotated
(`ops/owner.py`). Risky actions need the PIN even inside an unlocked voice session, because a
voice can be recorded or cloned: voice may start low-risk work and never authorises an
irreversible one. Five failed attempts lock the gate for a cooling-off period. Profiles are
per OS user, so two accounts on a shared PC have two owners, two memories and two permission
sets, and a profile owned by a different OS user is refused rather than shared.

### 22.8 Test lab

The manifest states what cannot be verified on a development machine and what the owner must
supply: test WhatsApp numbers, Microsoft 365 and Google test tenants, Office licences, a
code-signing certificate, audio hardware, a dedicated always-on machine and a second clean
machine. Those checks are blocking; until the prerequisite exists they are reported as
`blocked_prerequisite`, which is honest and visible rather than absent. Flakiness policy: the
suite is repeated as a non-blocking check, and the concurrency tests (supervisor restarts,
parallel subagents on one SQLite store) are deterministic by construction rather than retried.
A test may not read the host it happens to run on: every probe is travel-injectable, and a
test that needs a machine state passes it in. This is not tidiness — the responsiveness policy
used to read the real load average inside `evaluate()`, so a blocking gate passed or failed
according to what else the CI box was doing at the time.

### 22.9 Change over time

Model knowledge lives in a signed catalog (`ops/catalog.py`) that adds, updates, deprecates
and retires models without a new release. Verification is Ed25519 when `cryptography` is
installed and the method used is reported; the symmetric HMAC fallback is labelled for what
it is; an unsigned catalog is refused outside development. A catalog is applied **only
between actions** — swapping a model under a running agent loses its context — snapshots the
previous configuration first, and rolls back in one command. Retired model IDs are removed
rather than flagged, because a retired ID is a call that will fail. A nightly drift check
asks each provider which models it still serves, using the correct endpoint per wire protocol,
and reports missing IDs and unreachable providers without pretending to know.

### 22.10 Languages and responsiveness

Urdu, Arabic, Persian, Hebrew and the other right-to-left scripts are first class
(`ops/languages.py`): direction detection that ignores a stray character in a Latin label,
Urdu distinguished from Arabic by script-specific letters, text normalisation (presentation
forms, tatweel, diacritics, Arabic-Indic digits) applied on **both** sides of every comparison
so a correct Arabic interaction does not "fail" verification, per-language OCR packs, TTS
voices and STT hints. On responsiveness (`ops/governor.py`): ZYNTHARO runs below interactive
priority (`nice` / `BELOW_NORMAL_PRIORITY_CLASS`), and games, video calls and presentations
pause background and maintenance work and defer desktop work — while work the user asked for
right now is never paused, because it is their machine and their request.

### 22.12 Logic corrections (review pass)

A review of the seams — where two subsystems meet — found the following, all now fixed in
code and pinned by a test. They are listed because each one was a *logical* fault, not a
missing feature: the code ran, and the answer it produced was wrong.

| Fault | What was wrong | Now |
|---|---|---|
| Supervisor adoption | `reconcile()` cleared `jobs` *after* adopting, so an adopted job was forgotten and the report always claimed nothing was adopted | adopted jobs survive and are watched (`test_adopted_jobs_survive_reconciliation`) |
| Orphan workspaces | The cleanup guard only covered jobs it had just terminated, skipping the process-is-gone case the docstring promised | scratch directories from interrupted runs are cleaned (`test_interrupted_workspaces_are_cleaned_at_startup`) |
| Recovery code | Rotation lived in the CLI command, so any other caller (HUD, scheduled task, library) left the used code valid forever | `verify_recovery` consumes and rotates it itself (`test_using_the_recovery_code_rotates_it_without_asking_the_caller`) |
| Audio probe | Windows audio was detected with the screen-width metric, so a machine with no sound hardware looked ready for voice work | real playback + capture device query; unknowable is reported as no (`test_windows_audio_probe_is_not_a_display_probe`) |
| Persian detection | `پ چ ژ گ ک ی` are shared by Urdu and Persian, and Urdu was tested first, so Persian could never win | the sets are split: Urdu needs an Urdu-only letter, Persian needs Persian evidence and no Arabic-only letter (`test_persian_is_not_swallowed_by_urdu`) |
| "Done" with nothing to check | An empty contract fell through to `met`, certifying a task with no criteria | empty criteria → `uncertain`, and the summary says no criteria were written (`test_a_contract_with_nothing_to_check_is_never_met`) |
| "0 side effects" | A contract with no declared capabilities reported zero side effects, which reads like a clean bill of health | reported as *not judged*, with the reason (`test_side_effects_line_is_honest_when_no_capabilities_were_declared`) |
| Primary entry point | `zyntharo run` handed the objective to the Sovereign with no contract at all, bypassing outcome verification entirely | a contract is written from the request; `--expect PATH` states promised outputs (`test_the_live_task_path_checks_the_outcome_against_a_written_contract`) |
| Streamed usage | Streamed answers (the voice path) charged no quota, budget or billing, so the Steward saw headroom that was already spent | vendor usage is threaded out of every adapter and charged; an empty stream raises instead of returning silence (`test_streamed_answers_are_charged_to_quota`) |
| Helper ownership | `Launched.terminate()` used a module-level global, so a second supervisor meant stopping the wrong process book | the launched process records its own supervisor (`test_a_launched_helper_remembers_the_supervisor_that_owns_it`) |
| Invisible subsystems | `hud/app.js` claimed a generated coverage file and a test that did not exist; topics were also emitted as bare strings | `tools/hud_coverage.py`, `hud/coverage.json`, every topic a declared constant, asserted in `tests/test_hud.py` and blocking at M4 |
| HUD state drift | `agent.status`, `task.done` and `model.call` had no effect, so the roster could show a stale status | each has a specific effect in the interface |
| Three definitions of "irreversible" | `ops/owner.py`, `ops/verification.py` and the action registry each carried their own list, and they disagreed: `model.assign` and `memory.admin` demanded a PIN but were tiered as routine work | one canonical list in `ops/risk.py`, imported by all three (`test_one_definition_of_irreversible_governs_every_consumer`) |
| Self-cancelling approval gate | `Agent.act` pre-approved every capability in `safety.confirm_capabilities` for rank ≤ 1 — the same list that *requires* confirmation — so the Sovereign and Steward never emitted an `action.confirm` card for a single destructive action | pre-approval comes only from an explicit approval (`registry.approve`); the panel now applies to every rank (`test_a_sovereign_still_asks_before_a_destructive_action`) |
| Flaky responsiveness policy | `evaluate()` read the host's load average directly, so injecting a detector did not make the policy deterministic: on a busy machine background work paused itself and the release gate failed for reasons unrelated to the change | the load reading is injectable (`load_provider`) and tests pin it to zero (`test_a_loaded_machine_pauses_background_work`) |
| Relative priority | `apply_priority` used `os.nice()`, which is cumulative: two calls reached nice 20 and "below normal" was silently "as low as possible", with no way back | absolute `os.setpriority` / `SetPriorityClass`, idempotent and reversible (`test_priority_is_absolute_not_cumulative`) |
| Dead preflight branches | `MachineState` declared `idle_seconds`, `network` and `busy_reason` and the probe filled none of them, so `user_active` was always true, `user_present` work never deferred, and "machine busy" was reachable only from a test | idle, network and route probes; the governor supplies the busy reason; a headless process counts as nobody present (`test_preflight_defers_desktop_work_when_the_governor_says_busy`) |
| Isolation that could never happen | `run_isolated` required `isolation_available`, which nothing ever set | `ops.isolated_session_available` in config, passed to the preflight, and the decision says which path it took (`test_isolation_is_only_offered_when_it_is_configured`) |
| Journal with secrets | The supervisor journal persisted each helper's full environment, which can hold a provider API key, into a plain-text file | only variable *names* are recorded (`test_supervisor_journal_never_records_helper_environment_values`) |
| Windows busy detection | The Windows branch enumerated every pid and discarded the list without reading a single name, so known games and calls could never match on the main target platform | image names are read with `QueryFullProcessImageNameW` (unverified here: no Windows machine in this container) |
| Lingering verdict status | The verifier set `report.status = "disputed"`, a value absent from the documented vocabulary and from the interface's status styling | `disputed` is part of the vocabulary and gets its own rail colour and pulse (`test_the_interface_applies_the_direction_and_styles_a_disputed_report`) |
| Right-to-left chrome | The language layer knew the direction and the interface never asked, so Urdu and Arabic users got left-anchored chrome | the snapshot carries `language_dir`, the HUD sets `dir`, and side-anchored styles are logical (`border-inline-start`) (`test_the_interface_is_told_which_way_the_language_reads`) |
| A console that was not an instrument | The core projected with a hand-rolled factor (`vec4(p.xy * persp, 0, 1)`) and had no depth at all: a flat cloud behind an ordinary dashboard, with no ring system, no cage, no signal strips and no dials | real perspective camera with depth-shaded far hemisphere, a ring/tendril/orbit/jet assembly, the instrument cage, waveform strips and gauges — all of it executed headlessly in CI (`node tools/hud_visual_check.js`, run by `tests/test_hud.py`) |
| Silently dead geometry | Sparks were assigned at random (5%) and the rim lattice required a radius the generator could not produce, so a whole band of the globe was dead code that never drew a single point | sparks are spread deterministically (one in seventeen) and the rim lattice is assigned to the outermost points by rank, so no band can come out empty — asserted in the visual harness (`globe band N is empty` fails the suite) |
| A handler for an event that cannot happen | The new signal-routing map listed `browser.action`, which is not a declared topic, so the interface promised an effect for an event the core can never emit | the coverage guard caught it before the suite ran; only declared topics are routed, and `hud/coverage.json` is unchanged |
| A hidden window that kept drawing | A scene renderer started from the viewer's own loop would have kept rendering after the window closed | the renderer is driven by the main animation loop through `state.sceneFrame` and cleared in `closeScene()`, so closing the window stops the work immediately (`test_a_3d_explanation_window_stops_rendering_when_it_closes`) |

### What only the owner can provide or decide

These are not code problems and the gate reports them as outstanding blocking items:

1. **Launch mode** — BYOK (default), hosted gateway (needs the legal review and caps), or
   local-only. `zyntharo billing --mode gateway` lists what is missing.
2. **Test accounts and licences** — WhatsApp test number and device, Microsoft 365 and Google
   test tenants, Office licences, code-signing certificate(s), a second clean machine, a
   dedicated always-on machine or VM, audio hardware for voice and call tests.
3. **Legal review** — provider terms, privacy review, and specifically whether each provider
   permits resale if the hosted gateway is ever launched.
4. **Waivers** — if a blocking check is to ship unverified, `tools/release_waivers.json` needs
   the owner's name, the date and the reason.

## 9. Non-goals in Phase 1

- No kernel-level driver, no anti-cheat/DRM circumvention, no credential harvesting.
- WhatsApp automation runs against the user's own paired account, opt-in, with per-chat
  policy; it never messages third parties without that chat being explicitly allowed.
- No cloud dependency: local-first, BYO API keys.
