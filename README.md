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
```

The gate is the single answer to "is this releasable?": M0 through GA, every check marked
blocking or non-blocking, checks whose prerequisites are missing reported as
`blocked_prerequisite` rather than passed, and blocking checks waivable only with a named,
dated sign-off in [tools/release_waivers.json](tools/release_waivers.json).

## Interface

The HUD is the product's face and it is not a chat box with a gradient:

- **Particle core** — a sphere of thousands of individually simulated points in a slow
  axial drift, morphing between topologies (sphere, torus, helix, grid, waveform) as the
  system changes state. Speaking drives a radial energy ripple; tool use drives
  directional shear; errors collapse the core and re-seed it.
- **Dynamic wiring** — live links between the core and every active agent node, colour
  coded by rank, thickness by activity, and animated only while a message path is hot.
- **Agent graph** — main, manager, heads and workers drawn as a real tree with rank
  insignia, current task, permission scope, memory size and quota state.
- **Panels** (dockable, pinnable): transcript, task tree, memory inspector, quota monitor,
  plugin manager, provider settings, action log with screenshots, 3D scene viewer.
- **Voice by default.** The microphone is live on launch. A chat input exists and is
  toggled from the menu for when the user prefers typing.

## Repository layout

```
orbynth/            Python core package (stdlib-first)
  config.py         settings, providers, quota specs, agent roster
  events.py         typed async event bus; every HUD wire and log line comes from here
  memory.py         SQLite per-agent memory (episodic + semantic + shared board)
  llm/              provider registry, quota manager, fallback runtime
  agents/           identity/ranks, agent loop, orchestrator (teams, delegation, reports)
  actions/          capability-gated action drivers (input, vision, windows, office,
                    filesystem, browser, whatsapp, shell)
  plugins/          manifest, loader, sandboxed host, self-extension Forge
  ops/              supervisor, preflight, verification, owner, billing, catalog,
                    governor, languages (the operational contract)
  voice/            wake word, streaming STT, TTS, barge-in session
  hud/              websocket bridge + static server + webview launcher
hud/                the front-end (HTML/CSS/JS, hand-written WebGL, no CDN)
tools/              release gate + acceptance manifest + waivers
tests/              headless test suite for the core and the operational layer
```

## Security posture

Every action declares a capability (`input.type`, `filesystem.write`, `browser.eval`, …).
Every agent gets an explicit capability set and a resource budget. Nothing executes
outside its grant, every call is audited with before/after evidence, destructive
capabilities are confirm-gated by default, and the audit log is append-only.
