# ORBYNTH — Architecture & Build Contract

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
| Event bus (HUD wiring + audit source) | `orbynth/events.py` | DONE |
| Config, providers, quota specs, roster | `orbynth/config.py` | DONE |
| Per-agent memory (episodic/semantic/board) | `orbynth/memory.py` | DONE |
| Provider registry (OpenAI-compatible, Gemini, Anthropic, custom base_url) | `orbynth/llm/providers.py` | DONE (transport is stdlib HTTP; streaming via SSE) |
| Quota manager (rpm/rpd/tpm, windows, cooldowns, fallback chains) | `orbynth/llm/quotas.py` | DONE |
| Model runtime (complete/stream/tool-calls/fallback) | `orbynth/llm/runtime.py` | DONE (offline `echo` provider for tests) |
| Agent identity, ranks, capability sets, budgets | `orbynth/agents/identity.py` | DONE |
| Agent loop (plan → act → observe → report) | `orbynth/agents/base.py` | DONE |
| Orchestrator (teams, delegation, reporting, health re-tasking) | `orbynth/agents/orchestrator.py` | DONE |
| Role templates (Sovereign/Steward/Foreman/Operative/Artificer/…) | `orbynth/agents/roles.py` | DONE |
| Action registry + permission gate + audit | `orbynth/actions/registry.py` | DONE |
| Filesystem actions (range reads, write, create any extension, zip/unzip, rename, delete, move, copy, search) | `orbynth/actions/filesystem.py` | DONE |
| Shell actions | `orbynth/actions/shell.py` | DONE |
| Input control (mouse/keyboard) with **focus guard** and **verify-typed** | `orbynth/actions/input_control.py` | DRIVER |
| Vision (capture, OCR, element grounding, diff, "describe like vision" text fallback) | `orbynth/actions/vision.py` | DRIVER |
| Windows/apps (launch, focus, UIA tree, window enumeration/state) | `orbynth/actions/windows.py` | DRIVER |
| Office (Word/Excel/Access/PowerPoint via COM; save/export/print/macros) | `orbynth/actions/office.py` | DRIVER |
| Browser (tabs, icons incl. DevTools/extensions/lens/menu, click, type, scroll, forms, extract, eval, downloads, cookies, history) | `orbynth/actions/browser.py` | DRIVER |
| WhatsApp (pair, chats, send/receive, voice notes, images, voice calls) | `orbynth/actions/whatsapp.py` | DRIVER + SPEC |
| Plugin loader + capability-scoped host | `orbynth/plugins/loader.py` | DONE |
| Self-extension Forge (write → validate → install → register) | `orbynth/plugins/forge.py` | DONE (LLM-driven; template-first, memory-assisted later) |
| Voice session (wake, streaming STT, TTS, barge-in) | `orbynth/voice/session.py` | SPEC + thin real interface |
| HUD bridge (WebSocket fanout, static host, webview launcher) | `orbynth/hud/bridge.py` | DONE |
| HUD front-end (particle core, wires, panels, voice UI) | `hud/` | DONE |
| Process supervisor, job ownership, orphan cleanup | `orbynth/ops/supervisor.py` | DONE |
| Task requirements + unattended preflight | `orbynth/ops/preflight.py` | DONE |
| Outcome verification + risk-tiered effort | `orbynth/ops/verification.py` | DONE |
| Owner identity (first run, PIN, recovery, shared PCs) | `orbynth/ops/owner.py` | DONE (voice matcher is injected; no bundled speaker-ID model) |
| Launch modes: BYOK / gateway / local-only | `orbynth/ops/billing.py` | DONE |
| Signed model catalog, apply gate, rollback, drift check | `orbynth/ops/catalog.py` | DONE (Ed25519 needs `cryptography`; HMAC fallback labelled) |
| Responsiveness governor (priority, pause) | `orbynth/ops/governor.py` | DONE (busy detection is driver-dependent and honest about it) |
| Urdu/Arabic/RTL language layer | `orbynth/ops/languages.py` | DONE |
| Capacity plan + wave execution | `orbynth/agents/planning.py` | DONE |
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

## 7. Build order for a parallel agent team

Each workstream is independently testable against the frozen interfaces in `orbynth/`.

**Order matters here.** The first revision froze the contracts and then ran fifteen-plus
agents in parallel, which is the riskiest sequence available: nothing had run end to end, so
every contract being parallelised was a guess. The enforced order (`orbynth/agents/planning.py`)
puts **M0 — one real task through every layer — before any wide fan-out**, then proceeds in
waves sized to the host. If the host cannot run agents concurrently
(`ORBYNTH_AGENT_PARALLELISM=1`), the same waves run one at a time: nothing is dropped.

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
keys, ORBYNTH spends and resells nothing. **Gateway** is opt-in and refuses to arm itself
without a recorded legal-review reference plus a per-provider confirmation that resale is
permitted, and without abuse controls: per-user daily request and token caps, a global daily
spend ceiling, and a kill switch. **Local-only** refuses remote providers and needs a local
runtime on loopback. `orbynth billing` prints the mode and exactly what is still blocking it.

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
voices and STT hints. On responsiveness (`ops/governor.py`): ORBYNTH runs below interactive
priority (`nice` / `BELOW_NORMAL_PRIORITY_CLASS`), and games, video calls and presentations
pause background and maintenance work and defer desktop work — while work the user asked for
right now is never paused, because it is their machine and their request.

### What only the owner can provide or decide

These are not code problems and the gate reports them as outstanding blocking items:

1. **Launch mode** — BYOK (default), hosted gateway (needs the legal review and caps), or
   local-only. `orbynth billing --mode gateway` lists what is missing.
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
