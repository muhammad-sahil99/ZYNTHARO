# ORBYNTH

**A desktop-resident AI operative.** ORBYNTH is a voice-first, multi-agent automation
platform that operates a real computer the way a human does — vision, mouse, keyboard,
browser, file system, Office applications — and extends itself by writing and installing
its own plugins.

- CLI command: `orbynth`
- HUD (the interface): a WebGL particle core with dynamic wiring, hosted by the local
  bridge and rendered in the OS webview.
- Verified-clean alternate brand name: `SYNOVARIS`.

> **Status: Phase 1 — the spine.** This repository is the working skeleton plus a
> complete, runnable architectural contract. Core subsystems (memory, quota management,
> multi-agent orchestration, plugin lifecycle, event bus, HUD bridge) are implemented and
> covered by tests that run headless. OS-level action drivers (input, vision, Office,
> browser, WhatsApp) are real, fully specified modules with their full API surface and
> capability gates implemented, but they require a real desktop (Windows/macOS/Linux with
> a display) and their native dependencies installed to execute. Nobody can ship a
> finished JARVIS-class system in one sitting; this is the foundation everything else
> bolts onto. See `ARCHITECTURE.md` for the honest subsystem-by-subsystem status table.

---

## Install (developer machine)

```bash
git clone <this repo> && cd orbynth
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .                                  # core only, stdlib-first
pip install -e ".[desktop]"                       # + input, vision, browser, Windows/Office
pip install -e ".[voice]"                         # + STT/TTS/wake word
pip install -e ".[all]"
```

The **core install has zero third-party dependencies**. That is deliberate: on a target
machine with no coding environment, the packaged installer ships the bundled runtime and
nothing else. Optional extras are exactly that — optional.

## Run

```bash
orbynth doctor            # environment + capability report
orbynth hud               # start the HUD bridge and webview (the interface)
orbynth hud --no-webview  # bridge only; open printed URL in any browser
orbynth run "open my browser, go to my invoices, download the newest PDF"
orbynth run "file the invoices" --expect ~/Documents/invoices/filed.xlsx   # a real "done" check
orbynth agents            # list configured agent roster + models + quotas
orbynth plugins list      # installed plugins, permissions, health
orbynth voice             # voice session (wake word + streaming)
```

## Operate it

First run asks who the owner is:

```bash
orbynth owner setup       # choose a PIN; prints the one-time recovery code
orbynth owner status      # who owns this profile, is the session valid
```

Then the operational commands, one per hole that revision 1 of the design left open:

```bash
orbynth billing           # who pays: BYOK (default) / gateway / local-only, and what blocks it
orbynth catalog check     # are the configured model IDs still served by their providers?
orbynth catalog apply --file model-catalog.json --public-key pinned.pem   # only between actions
orbynth catalog rollback  # one command back to the previous configuration
orbynth supervise clean   # reconcile jobs a previous session left running
orbynth gate              # the master release gate: every acceptance check, blocking or not
tools/release_gate.py --list   # what the gate covers, by milestone
python3 tools/hud_coverage.py  # every emitted topic has a place in the interface (blocking at M4)
```

`orbynth run` writes a contract before the work starts and checks the finished state against
it, rather than trusting the agent's own summary. Without `--expect` the criteria are the
files the task actually produced (checked as they are written), so a task that did nothing
is reported `not_met` or `uncertain`, never `done`.

The gate is the single answer to "is this releasable?": M0 through GA, every check marked
blocking or non-blocking, checks whose prerequisites are missing reported as
`blocked_prerequisite` rather than passed, and blocking checks waivable only with a named,
dated sign-off in [tools/release_waivers.json](tools/release_waivers.json).

## Interface

The HUD is the product's face and it is not a chat box with a gradient:

- **Particle core** — one instrument in a real perspective camera, built from four bodies:
  a banded globe (shell, inner dust volume, evenly spread sparks, a rim lattice) where every
  dot carries its own axial rate, so the body shears like a fluid planet rather than spinning
  as a rigid shell; five equatorial ringlets plus wispy tendrils that rotate differentially,
  inner bands racing the outer ones; four thin arcs in crossing planes; and two faint polar
  jets. A few seeds run warm orange against the cyan, which is what stops it reading as a
  single-hue wireframe. It morphs between topologies (sphere, torus, helix, grid, waveform) as
  the system changes state: speaking drives a radial energy ripple, tool use drives directional
  shear, errors collapse the core and re-seed it, and reports and installs send a shockwave
  ring through the whole assembly. Far-side particles dim with depth, so the globe stays
  readable as a solid object instead of a silhouette.
- **Instrument cage** — rotating tick graduations with degree plates every 15°, dashed datum
  rings, six segmented arcs (three running warm), calibration rails with hash marks and end
  nodes, target brackets that tighten while the system is working, and a telemetry column
  carrying real values only. The cage is measured from the viewport using the same projection
  constants as the core, so it stays locked to the globe at any window size.
- **System grid backdrop** (`hud/grid.js`) — the mesh the console is mounted in: converging
  structural rules above and below the horizon, edge rulers that really do measure the frame
  they are drawn in, datum plates reporting the actual viewport, pixel ratio, frame rate and
  particle budget, corner reticles, and six labelled columns of scrolling hexadecimal buses
  down the margins of the core field.
- **Instrument widgets** (`hud/widgets.js`) — seventeen canvas instruments set into the
  housing: a sweeping radar whose blips are the real roster, a hexagon lattice whose lit cells
  are real ranks and teams, sparklines and bar clusters built by sampling real counters, a cell
  matrix of memory blocks, per-channel ladder meters and ruled scales with needles. Every one
  of them is fed the same counters the panels render.
- **Signal strips and dials** — four honest planes: speech energy, tool traffic, memory writes
  and helper-process lanes. Each is raised only by an event that actually happened, never by a
  loop, and the strip labels name the plane that is live. Gauge dials and ladder meters report
  fleet load, quota pressure, memory density and per-channel capability shares as real ratios.
- **Situations** (`hud/effects.js`) — the plate changes machine with the work. Twenty-two
  situations (idle, offline, wake, listening, speaking, thinking, delegating, acting, browsing,
  document work, memory, self-extension, verified, blocked, confirmation required, quota
  pressure, model failover, fault, outcome not met, reconciling, models updated, yielding the
  machine) and ten transient effects that stack and decay (flare, shock, ember, glitch, settle,
  sweep, stamp, dock, tear, breathe). Each situation blends the colour palette, globe rotation
  and sustained energy, ripple amplitude, backdrop intensity / scroll speed / alarm tint /
  interference, cage brightness, warmth, alert hazard ring and arc speed, and instrument jitter
  and sweep speed — changing over 700 ms rather than snapping. Every topic the core emits is
  routed to a situation, and the mapping lives beside the registry, so nothing on screen changes
  unless the system really is in that state.
- **Dynamic wiring** — live links between the core and every active agent node, colour
  coded by rank, thickness by activity, and animated only while a message path is hot.
- **Agent graph** — main, manager, heads and workers drawn as a real tree with rank
  insignia, current task, permission scope, memory size and quota state.
- **Panels** (dockable, pinnable): transcript, task tree, memory inspector, quota monitor,
  plugin manager, provider settings, action log with screenshots.
- **3D explanation window** — the core does not have to send a picture to explain something
  spatial. It sends a mesh (nodes and edges) or asks for a parametric solid (sphere, torus,
  saddle, wave, cylinder, helix) and the window renders it with an orbit camera: drag to
  rotate, wheel to zoom, axis triad, ground grid. Closing the window stops the renderer at
  once rather than leaving a hidden loop drawing.
- **Voice by default.** The microphone is live on launch. A chat input exists and is
  toggled from the menu for when the user prefers typing.

## The product: three workflows, real channels

What is sold is not the tool count, it is three recurring jobs, each with its own verification
(see [PRODUCT.md](PRODUCT.md)):

```bash
orbynth workflows list
orbynth workflows run --name invoice_intake \
  --params '{"source":"~/Invoices/in","dest":"~/Invoices/filed","ledger":"~/Invoices/ledger.xlsx"}'
orbynth workflows run --name daily_report \
  --params '{"ledger":"~/Invoices/ledger.xlsx","out_dir":"~/Reports"}'
orbynth channels            # which platform can actually be used here, and why the others cannot
orbynth connectors          # every outside account, its state, and its operations
orbynth connectors verify --connector slack
```

- **Invoice intake** files every new invoice by year and quarter and records it once in an Excel
  ledger. Re-running it is free: identical content is skipped by hash.
- **Reply desk** collects customer messages and prepares replies; **nothing is sent until the
  owner approves it** (`orbynth` action `reply_approve`, or the app). Drafts live in SQLite, so a
  crash loses no work and no message is answered twice.
- **Recurring report** writes a Word summary and an Excel breakdown from the ledger and reads both
  back to prove the totals.

Channels are honest by construction: Telegram uses the official Bot API, WhatsApp uses the bundled
sidecar, and Instagram, Messenger, X, LinkedIn, TikTok and Snapchat are declared unavailable with
the exact business gate that would change that — they refuse with the reason instead of showing a
button that never works.

## Automation: what a task actually does

The action layer is verified, not assumed. Three behaviours matter more than the action count:

- **The browser is searched deeply.** Every finder walks open shadow roots and every same-origin
  iframe document, and reports how many cross-origin frames it could not reach — so a selector
  that matches nothing is explainable instead of silent. Clicks are followed by a page
  fingerprint comparison (url, title, node count, text hash) and retried with the next target
  strategy before failure is reported; `browser_act` additionally waits for the element or text
  you said should appear. Downloads come from the browser's own download events and are only
  reported complete when they are (`browser_download_wait`), and a blocking
  `alert`/`confirm`/`beforeunload` can be answered or policed (`browser_dialog`) instead of
  freezing the page. Navigation waits for the document to genuinely finish loading, so the next
  action never runs against the previous page.
- **Typing cannot land in the wrong window or silently vanish.** The focus guard captures the
  target window before the first keystroke, re-checks it between chunks, aborts mid-flight when
  focus drifts, and reads the control back, so a dropped keystroke is a failure rather than a
  success.
- **WhatsApp runs over a bundled sidecar.** `sidecars/whatsapp/sidecar.js` (Node, dependencies in
  its own `package.json`) holds the WhatsApp Web session and speaks newline-delimited JSON-RPC
  over pipes; the Python side never drives the web app through pixels. Tests check the sidecar's
  RPC surface against the client, so a method the client calls but the sidecar does not implement
  fails the suite instead of timing out on a user's machine. Chats can be listed, searched, read,
  archived, pinned, muted, reacted to, replied to, forwarded and exported; media is
  confirm-gated; and outbound messages to chats outside the allow-list are refused.
- **Windows and applications are driven with proof.** Closing a window verifies it actually
  disappeared, and when it does not, names the prompt that is holding it (usually "save
  changes?") instead of returning `closed`. Minimise/maximise/restore ask the OS for the real
  state and fail if it disagrees. A click activates the window, verifies the foreground, then
  re-resolves the element immediately before clicking and reports whether its state changed.
  Launching only accepts a window that is genuinely *new*, so a document with the same title
  that was already open can never be mistaken for the one just started.
- **Coordinates share the OS's coordinate space.** The process declares per-monitor DPI
  awareness and reports the scale: without it a 150%-scaled laptop receives a virtualised
  viewport while the accessibility tree reports physical pixels, so every click computed from
  control bounds lands hundreds of pixels off — the classic "it clicked the wrong thing".
- **Office saves are proven, not assumed.** Word and Excel gained a plain `save` (typing into a
  document only reached disk if you guessed to use `save_as` with a path), and a save fails when
  the file is missing, empty, or unchanged. Word refuses to close a document with unsaved
  changes instead of discarding it, and modal prompts (file in use, replace, keep changes) are
  suppressed so a COM call cannot hang behind a dialog nobody will click.

### Voice on a call

The phrase you ask ORBYNTH to say goes through the best voice tier the machine can run, and the
live tier is always reported (`whatsapp_audio`):

| tier | engines | how it sounds |
|---|---|---|
| `realtime` | OpenAI realtime | full-duplex, generated for the conversation — the human-sounding tier |
| `neural_stream` | ElevenLabs, Azure | frames arrive while the sentence is still being synthesised |
| `neural_batch` | edge-tts | a good neural voice, but audibly recited |
| `offline` | piper, then espeak | local; espeak is named robotic in the output rather than passed off |

Speech is written into the call in 20 ms frames, so it starts immediately and stops mid-sentence
when the caller talks over it (barge-in is checked between frames, not before playback). What the
call hears depends on routing, which the bridge reports instead of assuming: a loopback device
(VB-Audio Cable, BlackHole, a PipeWire null sink) is used as the virtual microphone for clean call
audio; without one it falls back to speaking aloud in the room; with neither it raises the install
step rather than pretending. The session's STT is wired into the same bridge, so `whatsapp_listen`
returns the caller's words.

## Repository layout

```
orbynth/            Python core package (stdlib-first)
  config.py         settings, providers, quota specs, agent roster
  events.py         typed async event bus; every HUD wire and log line comes from here
  memory.py         SQLite per-agent memory (episodic + semantic + shared board)
  llm/              provider registry, quota manager (minute/day/week windows), fallback
                    runtime, reserve pool (models added with limits, called up by the Steward)
  agents/           identity/ranks, agent loop, orchestrator (teams, delegation, reports),
                    comms (agent-to-agent requests, answers, file handoff, stored threads)
  actions/          capability-gated action drivers (input, vision, windows, office,
                    filesystem, browser, whatsapp, shell)
  plugins/          manifest, loader, sandboxed host, self-extension Forge
  ops/              supervisor, preflight, verification, owner, billing, catalog, governor,
                    languages (the operational contract), sensors.py (deterministic hardware
                    monitor), index.py (local FTS5 search), and risk.py, the single list of
                    what counts as irreversible
  voice/            wake word, streaming STT, TTS, barge-in session
  hud/              websocket bridge + static server + webview launcher
hud/                the front-end (HTML/CSS/JS, hand-written WebGL, no CDN)
  grid.js           system grid backdrop: mesh, rulers, datum plates, data columns (canvas 2D)
  particle-core.js  the globe, ring system, orbit arcs and jets (WebGL, two draw calls)
  chrome.js         instrument cage, waveform strips and gauge dials (canvas 2D)
  widgets.js        seventeen canvas instruments fed by real counters (canvas 2D)
  wires.js          routed rank buses, node plates and live message traffic
sidecars/           the Node sidecars the drivers speak to over JSON-RPC (WhatsApp)
tools/              release gate + acceptance manifest + waivers + hud_visual_check.js,
                    which executes the renderers headlessly against a stubbed GL context
tests/              headless test suite for the core and the operational layer
```

## Documentation

| file | what it answers |
|---|---|
| [BUILD_STATUS.md](BUILD_STATUS.md) | what actually runs today, with the command that proves it |
| [LIMITATIONS.md](LIMITATIONS.md) | what is not built, what a partial feature cannot do, and why |
| [TRACEABILITY.md](TRACEABILITY.md) | spec section → code → test or gate check |
| [OWNER_REQUIREMENTS.md](OWNER_REQUIREMENTS.md) | the decisions, accounts, licences and hardware only you can supply |
| [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md) | every tool, and whether it is proven on this machine |
| [ARCHITECTURE.md](ARCHITECTURE.md) | how the parts fit together |
| [AGENTS.md](AGENTS.md) | the rules for anyone (or anything) changing this code |

## Security posture

Every action declares a capability (`input.type`, `filesystem.write`, `browser.eval`, …).
Every agent gets an explicit capability set and a resource budget. Nothing executes
outside its grant, every call is audited with before/after evidence, destructive
capabilities are confirm-gated by default, and the audit log is append-only.
