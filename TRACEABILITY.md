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
| 19 | Packaging, installer, updates, docs | `pyproject.toml`, `orbynth/ops/catalog.py`, this file set | T `tests/test_ops.py`, G `catalog_rollback`; installer **not built** |
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

## How to keep this honest

1. A change that adds a capability adds a row here and a check to `tools/acceptance.json`.
2. A capability that only works with hardware, a licence or an account gets a **manual** entry with
   `requires`, so the gate reports it as *blocked*, never as *passed*.
3. `python tools/reality_gate.py` fails the build if a shipped file contains a stub, a test double
   or a marker instead of real behaviour.
