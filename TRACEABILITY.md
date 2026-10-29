# TRACEABILITY — spec section → code → test

Every row answers one question: *if this breaks, what fails?* A row whose "test / gate" column is
empty is a gap, and gaps belong in [LIMITATIONS.md](LIMITATIONS.md) with a reason — not in this
table pretending to be covered.

Legend: **T** = unit/integration test, **G** = release-gate operation (`python tools/release_gate.py`),
**M** = manual acceptance in `tools/acceptance.json` (needs hardware/accounts/licences).

| spec | what it covers | code | test / gate |
|---|---|---|---|
| 1 | Product shape, single-user desktop operative, process architecture | `zyntharo/cli.py` (`build_stack`), `zyntharo/ops/supervisor.py`, `zyntharo/ops/spawn.py` | T `tests/test_ops.py`, G `supervisor_cleanup`, M `process_architecture` |
| 2 | Resource budgets, low-priority behaviour, light footprint | `zyntharo/ops/governor.py`, `zyntharo/ops/sensors.py` (`overhead`) | T `tests/test_ops.py`, G `governor`, `hardware_sensors` |
| 3 | Contracts: action, receipt, task, evidence; generated bindings | `zyntharo/contracts.py`, `tools/contracts.py`, `zyntharo/ops/receipt.py` | T `tests/test_contracts.py`, G `contracts` |
| 4 | Tool surface and capability gate, verification tiers, journal, matrix | `zyntharo/actions/registry.py`, `zyntharo/ops/risk.py`, `zyntharo/ops/verification.py`, `zyntharo/ops/journal.py`, `zyntharo/ops/capability.py` | T `tests/test_actions.py`, `test_journal.py`, `test_capability.py`, G `risk_tiers`, `journal`, `capability_matrix` |
| 5 | Memory: episodes, facts, tasks, FTS recall, per-agent isolation | `zyntharo/memory.py` | T `tests/test_core.py`, `test_agents_plugins.py` |
| 6 | Event bus and HUD topic coverage | `zyntharo/events.py`, `hud/app.js`, `tools/hud_coverage.py` | T `tests/test_hud.py`, G `hud_coverage` |
| 7 | Model runtime, providers, failover, quotas, Steward | `zyntharo/llm/providers.py`, `zyntharo/llm/runtime.py`, `zyntharo/llm/quotas.py`, `zyntharo/agents/orchestrator.py` (`steward_watch`) | T `tests/test_llm.py`, G `governor` |
| 8 | Fleet: ranks, spawning, teams, per-agent memory, reports | `zyntharo/agents/*.py` | T `tests/test_agents_plugins.py` |
| 9 | Desktop, files, shell, terminal, Office (A and B) | `zyntharo/actions/{filesystem,shell,terminal,office,office_files,input_control,windows,vision}.py`, `zyntharo/ops/{terminal,docengine}.py` | T `test_actions.py`, `test_terminal.py`, `test_office_files.py`, G `terminal_engine`, `office_tier_b`, M `office_tier_a`, `terminal_interactive` |
| 10 | Browser control | `zyntharo/actions/browser.py` | T `tests/test_actions.py`, M `browser_acceptance` |
| 11 | WhatsApp: chats, media, calls, sidecar | `zyntharo/actions/whatsapp.py`, `zyntharo/voice/callbridge.py` | T `tests/test_actions.py` (offline double), M `whatsapp_*` |
| 12 | Plugins: authoring, install, sandbox, rollback | `zyntharo/plugins/{forge,loader}.py` | T `tests/test_agents_plugins.py`, G `plugin_lifecycle` |
| 13 | Voice: wake, STT, TTS, barge-in, languages | `zyntharo/voice/*.py` | T `tests/test_core.py`, `test_ops.py` (language), M `voice_quality`, `voice_duplex_call` |
| 14.1 | Memory vault and per-item permissions | `zyntharo/memory.py` | T `test_core.py` |
| 14.2 | Local search index: FTS5, incremental, verified results | `zyntharo/ops/index.py`, `zyntharo/actions/localsearch.py` | T `tests/test_index.py`, G `local_index`, M `search_benchmark` |
| 15 | MCP and local API/SDKs | — | **not built** → LIMITATIONS |
| 16 | Hardware monitor: collection, plausibility, rules, protection, alerts | `zyntharo/ops/sensors.py`, `zyntharo/actions/hardware.py` | T `tests/test_sensors.py`, G `hardware_sensors`, M `sensor_rig`, `sensor_accuracy` |
| 17 | HUD: particle core, wiring, panels, scenes, effects | `hud/*.js`, `hud/index.html`, `hud/styles.css`, `zyntharo/hud/service.py` | T `tests/test_hud.py`, `node tools/hud_visual_check.js`, G `hud_coverage` |
| 18 | Mobile handover and remote control | — | **not built** → LIMITATIONS |
| sellable workflows | Invoice intake files and dedupes by content hash into a verified xlsx ledger; the reply desk stores drafts and refuses to send without owner approval or through the wrong platform; the recurring report writes docx+xlsx and reads the totals back | `zyntharo/workflows/*`, `zyntharo/actions/channels.py`, `zyntharo/cli.py` | T `tests/test_workflows.py` |
| reply desk on mail | A connected mailbox is read through the connector's declared `search`/`read` operations, mapped onto the channel contract with the subject carried, answered with a threaded `Re:` only after approval, deduped by folder-namespaced uid, and unreadable messages reported instead of dropped | `zyntharo/workflows/mailbox.py`, `zyntharo/workflows/reply_desk.py`, `zyntharo/actions/channels.py` | T `tests/test_mailbox_desk.py` |
| replies at the owner's screen | The panel lists drafts with the inbound message beside the proposal; editing never sends, an empty edit is refused, sending happens once through the draft's own source, and one press drafts at most the declared limit | `hud/index.html`, `hud/app.js`, `zyntharo/hud/service.py` (`reply.*`), `zyntharo/workflows/reply_desk.py` (`edit`) | T `tests/test_replies_hud.py` |
| connected accounts | Every outside account is one of four honest states; the credential is the customer's own (no shipped secret); a capability split keeps `connector.read` agents from reaching send endpoints; the vault uses DPAPI on Windows and reports its protection truthfully elsewhere | `zyntharo/connectors/*`, `zyntharo/actions/connectors.py` | T `tests/test_connectors.py` |
| connected accounts: interface | The Accounts panel is driven by the registry, and its form is generated from each connector's declared `Field` list — a connector cannot be shown a form that does not match the calls it makes; connecting, verifying, disconnecting, running one operation and starting a consent flow are all bridge commands | `hud/index.html`, `hud/app.js`, `zyntharo/hud/service.py` | T `tests/test_connectors_hud.py` |
| interactive sign-in | Google PKCE loopback and Microsoft device code run as bounded background sessions with declared scope profiles; the session payload carries instructions and an outcome, never a token | `zyntharo/connectors/signin.py` | T `tests/test_connectors_hud.py` (`SignInSessionTests`) |
| channel honesty | Telegram uses the official Bot API with real HTTP and keeps the platform's own error text; WhatsApp is the bundled sidecar; every other platform is declared with its real business gate and refuses with that reason | `zyntharo/channels/*` | T `tests/test_workflows.py` (`RegistryHonestyTests`, `TelegramChannelTests`) |
| 19 | Packaging, installer, updates, docs | `tools/build_windows_bundle.py`, `tools/build_windows_installer.py`, `installer/ZYNTHARO.iss`, `zyntharo/resources.py`, `pyproject.toml`, `zyntharo/ops/catalog.py` | T `tests/test_packaging.py`, G bundle/installer-input checks; signing and clean-VM acceptance pending |
| 20 | Cloud sync of selected items | — | **not built** → LIMITATIONS |
| 21 | Security: secrets, ownership, no bypass | `zyntharo/ops/owner.py`, `zyntharo/ops/risk.py`, `zyntharo/config.py` (`Secrets`) | T `tests/test_ops.py`, G `owner_gate`, `risk_tiers` |
| 22.1 | Supervised processes, cleanup, reconciliation | `zyntharo/ops/supervisor.py`, `zyntharo/ops/spawn.py` | G `supervisor_cleanup`, T `test_ops.py` |
| 22.2 | Unattended runs: preflight, deferral, honest refusal | `zyntharo/ops/preflight.py` | G `preflight`, T `test_ops.py` |
| 22.3 | One master release gate with milestones and waivers | `tools/release_gate.py`, `tools/acceptance.json`, `tools/release_waivers.json` | G — the gate itself |
| 22.4 | Outcome checks against what was asked | `zyntharo/ops/verification.py` | T `test_agents_plugins.py`, G `risk_tiers` |
| 22.5 | Build order: first working slice, then waves | `tools/release_gate.py` (M0), this document | G `m0_slice` |
| 22.6 | Who pays: BYOK, gateway, local-only + abuse control | `zyntharo/ops/billing.py` | T `test_ops.py`, G `billing_modes` |
| 22.7 | Owner identity, recovery, shared PCs | `zyntharo/ops/owner.py` | G `owner_gate`, T `test_ops.py`, M `owner_recovery` |
| 22.8 | Speed versus verification, batched harmless actions | `zyntharo/ops/verification.py` | T `test_ops.py`, G `risk_tiers` |
| 22.9 | Test lab, flaky policy, dedicated accounts | `tools/acceptance.json`, this file | manual — see OWNER_REQUIREMENTS |
| 22.10 | Safe updates, retired models, signed catalogue | `zyntharo/ops/catalog.py`, `zyntharo/ops/update.py` | T `test_ops.py`, G `catalog_rollback` |
| 22.11 | Other languages, RTL, PC responsiveness, low priority | `zyntharo/ops/languages.py`, `zyntharo/ops/governor.py` | T `test_ops.py`, G `language_rtl`, `governor` |
| resource admission | Parallel work is capped before it starts and the cap shrinks with load; teams run in bounded batches; the managed browser is released on shutdown and on request; an idle or hidden HUD does not render at full rate | `zyntharo/ops/governor.py` (`worker_limit`), `zyntharo/agents/orchestrator.py` (`_worker_batch_size`), `zyntharo/actions/browser.py` (`release`), `hud/app.js` (power policy), `zyntharo/cli.py` (`Stack.shutdown`) | T `test_ops.py`, `test_agents_plugins.py`, `test_actions.py`, `test_hud.py` |

| fleet comms | Agents ask each other for help, answer, hand over files; every message stored in a thread, open requests stay visible | `zyntharo/agents/comms.py`, `zyntharo/actions/fleet.py` | T `test_comms.py` |
| reserve models | A model is added with its rpm/rpd/rpw and tpm/tpd/tpw envelope, optionally as a reserve; activation is the manager's authority | `zyntharo/llm/reserve.py`, `zyntharo/actions/fleet.py` | T `test_reserve.py` |
| control manager | The Steward holds `model.manage` and `agent.messaging`; reserves are quota-tracked from the first call and assigned to nobody until activated | `zyntharo/agents/roles.py`, `zyntharo/config.py` | T `test_reserve.py` |

| presence: speakers | How many people are talking, which segments belong to whom, male/female/unknown per voice, voice enrolment and identification | `zyntharo/voice/speakers.py`, `zyntharo/actions/presence.py` | T `test_speakers.py`, `test_presence.py` (actions) |
| presence: camera | Consent-gated capture, face detection and enrolment, refusal on OS denial, no frame retention | `zyntharo/vision/camera.py`, `zyntharo/actions/presence.py` | T `test_camera.py`, `test_presence.py` |
| presence: etiquette | Speak / shorten / screen-only / wait / silent, with the reason; private content is never read aloud in front of a stranger | `zyntharo/ops/presence.py`, `zyntharo/actions/presence.py` | T `test_presence.py` |
| presence: identity is not authority | A recognised voice or face never grants a capability; the owner gate still decides | `zyntharo/ops/owner.py`, `zyntharo/ops/risk.py` | T `test_ops.py`, `test_presence.py` |
| mistake memory | A repeated failure carries its known fix; agents read the patterns before choosing a tool; nothing is auto-executed from a lesson | `zyntharo/ops/lessons.py`, `zyntharo/actions/registry.py`, `zyntharo/agents/base.py` | T `test_lessons.py`, `test_agents_plugins.py` |
| core arithmetic | Quota cooldowns and spend survive a restart; warnings fire once per window-minute; headroom prunes expired windows; budgets enforce a wall clock started at 0.0; kind-filtered memory search binds the right parameters on both retrieval paths | `zyntharo/llm/quotas.py`, `zyntharo/llm/runtime.py`, `zyntharo/memory.py` | T `test_core_audit.py`, `test_llm.py`, `test_core.py` |
| role reachability | Every declared capability is granted to some delegable role, or listed in `OWNER_ONLY_CAPABILITIES` with a reason; identity enrolment is never granted to an agent | `zyntharo/agents/roles.py`, `zyntharo/config.py` | T `test_roles_audit.py` |
| logic audit | Camera threshold vs model boundary, recognition-capability vs recognition-hit, single unverified voice, OS denial recorded, interrupted scheduled run | `zyntharo/vision/camera.py`, `zyntharo/ops/presence.py`, `zyntharo/ops/schedule.py` | T `test_logic_audit.py` |
| scheduled work | One-shot and recurring queues, a grace window that skips stale runs, GUI work deferred on a locked session, bounded ticks, restart persistence | `zyntharo/ops/schedule.py`, `zyntharo/actions/schedule.py` | T `test_schedule.py` |

## How to keep this honest

1. A change that adds a capability adds a row here and a check to `tools/acceptance.json`.
2. A capability that only works with hardware, a licence or an account gets a **manual** entry with
   `requires`, so the gate reports it as *blocked*, never as *passed*.
3. `python tools/reality_gate.py` fails the build if a shipped file contains a stub, a test double
   or a marker instead of real behaviour.
