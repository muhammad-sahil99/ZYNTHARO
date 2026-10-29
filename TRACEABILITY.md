# TRACEABILITY — spec section → code → test

Every row answers one question: *if this breaks, what fails?* A row whose "test / gate" column is
empty is a gap, and gaps belong in [LIMITATIONS.md](LIMITATIONS.md) with a reason — not in this
table pretending to be covered.

Legend: **T** = unit/integration test, **G** = release-gate operation (`python tools/release_gate.py`),
**M** = manual acceptance in `tools/acceptance.json` (needs hardware/accounts/licences).

| spec | what it covers | code | test / gate |
|---|---|---|---|
| 1 | Product shape, single-user desktop operative, process architecture | `orbynth/cli.py` (`build_stack`), `orbynth/ops/supervisor.py`, `orbynth/ops/spawn.py` | T `tests/test_ops.py`, G `supervisor_cleanup`, M `process_architecture` |
| 2 | Resource budgets, low-priority behaviour, light footprint | `orbynth/ops/governor.py`, `orbynth/ops/sensors.py` (`overhead`) | T `tests/test_ops.py`, G `governor`, `hardware_sensors` |
| 3 | Contracts: action, receipt, task, evidence; generated bindings | `orbynth/contracts.py`, `tools/contracts.py`, `orbynth/ops/receipt.py` | T `tests/test_contracts.py`, G `contracts` |
| 4 | Tool surface and capability gate, verification tiers, journal, matrix | `orbynth/actions/registry.py`, `orbynth/ops/risk.py`, `orbynth/ops/verification.py`, `orbynth/ops/journal.py`, `orbynth/ops/capability.py` | T `tests/test_actions.py`, `test_journal.py`, `test_capability.py`, G `risk_tiers`, `journal`, `capability_matrix` |
| 5 | Memory: episodes, facts, tasks, FTS recall, per-agent isolation | `orbynth/memory.py` | T `tests/test_core.py`, `test_agents_plugins.py` |
| 6 | Event bus and HUD topic coverage | `orbynth/events.py`, `hud/app.js`, `tools/hud_coverage.py` | T `tests/test_hud.py`, G `hud_coverage` |
| 7 | Model runtime, providers, failover, quotas, Steward | `orbynth/llm/providers.py`, `orbynth/llm/runtime.py`, `orbynth/llm/quotas.py`, `orbynth/agents/orchestrator.py` (`steward_watch`) | T `tests/test_llm.py`, G `governor` |
| 8 | Fleet: ranks, spawning, teams, per-agent memory, reports | `orbynth/agents/*.py` | T `tests/test_agents_plugins.py` |
| 9 | Desktop, files, shell, terminal, Office (A and B) | `orbynth/actions/{filesystem,shell,terminal,office,office_files,input_control,windows,vision}.py`, `orbynth/ops/{terminal,docengine}.py` | T `test_actions.py`, `test_terminal.py`, `test_office_files.py`, G `terminal_engine`, `office_tier_b`, M `office_tier_a`, `terminal_interactive` |
| 10 | Browser control | `orbynth/actions/browser.py` | T `tests/test_actions.py`, M `browser_acceptance` |
| 11 | WhatsApp: chats, media, calls, sidecar | `orbynth/actions/whatsapp.py`, `orbynth/voice/callbridge.py` | T `tests/test_actions.py` (offline double), M `whatsapp_*` |
| 12 | Plugins: authoring, install, sandbox, rollback | `orbynth/plugins/{forge,loader}.py` | T `tests/test_agents_plugins.py`, G `plugin_lifecycle` |
| 13 | Voice: wake, STT, TTS, barge-in, languages | `orbynth/voice/*.py` | T `tests/test_core.py`, `test_ops.py` (language), M `voice_quality`, `voice_duplex_call` |
| 14.1 | Memory vault and per-item permissions | `orbynth/memory.py` | T `test_core.py` |
| 14.2 | Local search index: FTS5, incremental, verified results | `orbynth/ops/index.py`, `orbynth/actions/localsearch.py` | T `tests/test_index.py`, G `local_index`, M `search_benchmark` |
| 15 | MCP and local API/SDKs | — | **not built** → LIMITATIONS |
| 16 | Hardware monitor: collection, plausibility, rules, protection, alerts | `orbynth/ops/sensors.py`, `orbynth/actions/hardware.py` | T `tests/test_sensors.py`, G `hardware_sensors`, M `sensor_rig`, `sensor_accuracy` |
| 17 | HUD: particle core, wiring, panels, scenes, effects | `hud/*.js`, `hud/index.html`, `hud/styles.css`, `orbynth/hud/service.py` | T `tests/test_hud.py`, `node tools/hud_visual_check.js`, G `hud_coverage` |
| 18 | Mobile handover and remote control | — | **not built** → LIMITATIONS |
| sellable workflows | Invoice intake files and dedupes by content hash into a verified xlsx ledger; the reply desk stores drafts and refuses to send without owner approval or through the wrong platform; the recurring report writes docx+xlsx and reads the totals back | `orbynth/workflows/*`, `orbynth/actions/channels.py`, `orbynth/cli.py` | T `tests/test_workflows.py` |
| reply desk on mail | A connected mailbox is read through the connector's declared `search`/`read` operations, mapped onto the channel contract with the subject carried, answered with a threaded `Re:` only after approval, deduped by folder-namespaced uid, and unreadable messages reported instead of dropped | `orbynth/workflows/mailbox.py`, `orbynth/workflows/reply_desk.py`, `orbynth/actions/channels.py` | T `tests/test_mailbox_desk.py` |
| connected accounts | Every outside account is one of four honest states; the credential is the customer's own (no shipped secret); a capability split keeps `connector.read` agents from reaching send endpoints; the vault uses DPAPI on Windows and reports its protection truthfully elsewhere | `orbynth/connectors/*`, `orbynth/actions/connectors.py` | T `tests/test_connectors.py` |
| connected accounts: interface | The Accounts panel is driven by the registry, and its form is generated from each connector's declared `Field` list — a connector cannot be shown a form that does not match the calls it makes; connecting, verifying, disconnecting, running one operation and starting a consent flow are all bridge commands | `hud/index.html`, `hud/app.js`, `orbynth/hud/service.py` | T `tests/test_connectors_hud.py` |
| interactive sign-in | Google PKCE loopback and Microsoft device code run as bounded background sessions with declared scope profiles; the session payload carries instructions and an outcome, never a token | `orbynth/connectors/signin.py` | T `tests/test_connectors_hud.py` (`SignInSessionTests`) |
| channel honesty | Telegram uses the official Bot API with real HTTP and keeps the platform's own error text; WhatsApp is the bundled sidecar; every other platform is declared with its real business gate and refuses with that reason | `orbynth/channels/*` | T `tests/test_workflows.py` (`RegistryHonestyTests`, `TelegramChannelTests`) |
| 19 | Packaging, installer, updates, docs | `tools/build_windows_bundle.py`, `tools/build_windows_installer.py`, `installer/ORBYNTH.iss`, `orbynth/resources.py`, `pyproject.toml`, `orbynth/ops/catalog.py` | T `tests/test_packaging.py`, G bundle/installer-input checks; signing and clean-VM acceptance pending |
| 20 | Cloud sync of selected items | — | **not built** → LIMITATIONS |
| 21 | Security: secrets, ownership, no bypass | `orbynth/ops/owner.py`, `orbynth/ops/risk.py`, `orbynth/config.py` (`Secrets`) | T `tests/test_ops.py`, G `owner_gate`, `risk_tiers` |
| 22.1 | Supervised processes, cleanup, reconciliation | `orbynth/ops/supervisor.py`, `orbynth/ops/spawn.py` | G `supervisor_cleanup`, T `test_ops.py` |
| 22.2 | Unattended runs: preflight, deferral, honest refusal | `orbynth/ops/preflight.py` | G `preflight`, T `test_ops.py` |
| 22.3 | One master release gate with milestones and waivers | `tools/release_gate.py`, `tools/acceptance.json`, `tools/release_waivers.json` | G — the gate itself |
| 22.4 | Outcome checks against what was asked | `orbynth/ops/verification.py` | T `test_agents_plugins.py`, G `risk_tiers` |
| 22.5 | Build order: first working slice, then waves | `tools/release_gate.py` (M0), this document | G `m0_slice` |
| 22.6 | Who pays: BYOK, gateway, local-only + abuse control | `orbynth/ops/billing.py` | T `test_ops.py`, G `billing_modes` |
| 22.7 | Owner identity, recovery, shared PCs | `orbynth/ops/owner.py` | G `owner_gate`, T `test_ops.py`, M `owner_recovery` |
| 22.8 | Speed versus verification, batched harmless actions | `orbynth/ops/verification.py` | T `test_ops.py`, G `risk_tiers` |
| 22.9 | Test lab, flaky policy, dedicated accounts | `tools/acceptance.json`, this file | manual — see OWNER_REQUIREMENTS |
| 22.10 | Safe updates, retired models, signed catalogue | `orbynth/ops/catalog.py`, `orbynth/ops/update.py` | T `test_ops.py`, G `catalog_rollback` |
| 22.11 | Other languages, RTL, PC responsiveness, low priority | `orbynth/ops/languages.py`, `orbynth/ops/governor.py` | T `test_ops.py`, G `language_rtl`, `governor` |
| resource admission | Parallel work is capped before it starts and the cap shrinks with load; teams run in bounded batches; the managed browser is released on shutdown and on request; an idle or hidden HUD does not render at full rate | `orbynth/ops/governor.py` (`worker_limit`), `orbynth/agents/orchestrator.py` (`_worker_batch_size`), `orbynth/actions/browser.py` (`release`), `hud/app.js` (power policy), `orbynth/cli.py` (`Stack.shutdown`) | T `test_ops.py`, `test_agents_plugins.py`, `test_actions.py`, `test_hud.py` |

| fleet comms | Agents ask each other for help, answer, hand over files; every message stored in a thread, open requests stay visible | `orbynth/agents/comms.py`, `orbynth/actions/fleet.py` | T `test_comms.py` |
| reserve models | A model is added with its rpm/rpd/rpw and tpm/tpd/tpw envelope, optionally as a reserve; activation is the manager's authority | `orbynth/llm/reserve.py`, `orbynth/actions/fleet.py` | T `test_reserve.py` |
| control manager | The Steward holds `model.manage` and `agent.messaging`; reserves are quota-tracked from the first call and assigned to nobody until activated | `orbynth/agents/roles.py`, `orbynth/config.py` | T `test_reserve.py` |

| presence: speakers | How many people are talking, which segments belong to whom, male/female/unknown per voice, voice enrolment and identification | `orbynth/voice/speakers.py`, `orbynth/actions/presence.py` | T `test_speakers.py`, `test_presence.py` (actions) |
| presence: camera | Consent-gated capture, face detection and enrolment, refusal on OS denial, no frame retention | `orbynth/vision/camera.py`, `orbynth/actions/presence.py` | T `test_camera.py`, `test_presence.py` |
| presence: etiquette | Speak / shorten / screen-only / wait / silent, with the reason; private content is never read aloud in front of a stranger | `orbynth/ops/presence.py`, `orbynth/actions/presence.py` | T `test_presence.py` |
| presence: identity is not authority | A recognised voice or face never grants a capability; the owner gate still decides | `orbynth/ops/owner.py`, `orbynth/ops/risk.py` | T `test_ops.py`, `test_presence.py` |
| mistake memory | A repeated failure carries its known fix; agents read the patterns before choosing a tool; nothing is auto-executed from a lesson | `orbynth/ops/lessons.py`, `orbynth/actions/registry.py`, `orbynth/agents/base.py` | T `test_lessons.py`, `test_agents_plugins.py` |
| core arithmetic | Quota cooldowns and spend survive a restart; warnings fire once per window-minute; headroom prunes expired windows; budgets enforce a wall clock started at 0.0; kind-filtered memory search binds the right parameters on both retrieval paths | `orbynth/llm/quotas.py`, `orbynth/llm/runtime.py`, `orbynth/memory.py` | T `test_core_audit.py`, `test_llm.py`, `test_core.py` |
| role reachability | Every declared capability is granted to some delegable role, or listed in `OWNER_ONLY_CAPABILITIES` with a reason; identity enrolment is never granted to an agent | `orbynth/agents/roles.py`, `orbynth/config.py` | T `test_roles_audit.py` |
| logic audit | Camera threshold vs model boundary, recognition-capability vs recognition-hit, single unverified voice, OS denial recorded, interrupted scheduled run | `orbynth/vision/camera.py`, `orbynth/ops/presence.py`, `orbynth/ops/schedule.py` | T `test_logic_audit.py` |
| scheduled work | One-shot and recurring queues, a grace window that skips stale runs, GUI work deferred on a locked session, bounded ticks, restart persistence | `orbynth/ops/schedule.py`, `orbynth/actions/schedule.py` | T `test_schedule.py` |

## How to keep this honest

1. A change that adds a capability adds a row here and a check to `tools/acceptance.json`.
2. A capability that only works with hardware, a licence or an account gets a **manual** entry with
   `requires`, so the gate reports it as *blocked*, never as *passed*.
3. `python tools/reality_gate.py` fails the build if a shipped file contains a stub, a test double
   or a marker instead of real behaviour.
