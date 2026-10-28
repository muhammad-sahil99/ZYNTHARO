# BUILD STATUS — where ORBYNTH actually is

Last verified on this machine with the commands at the bottom. This file exists so nobody has
to guess what is real: every claim is either measured here, covered by a named test or gate
check, or listed as *not built* in [LIMITATIONS.md](LIMITATIONS.md).

Read the three markers literally:

| marker | meaning |
|---|---|
| **done** | implemented, exercised by a test or gate op, and honest about its edges |
| **partial** | a real working subset ships; the missing part is named in LIMITATIONS.md |
| **unbuilt** | designed in the spec, not in this repository |

## What runs today

| area | state | evidence |
|---|---|---|
| Action layer, capability gate, confirmation, audit | done | `tests/test_actions.py`, `tests/test_core.py`, gate `m0_slice` |
| Tool contracts (Python/TypeScript/C# bindings, receipts) | done | `tests/test_contracts.py`, gate `contracts` |
| Capability Matrix (real evidence, no invented green) | done | `tests/test_capability.py`, gate `capability_matrix` |
| Retry/receipt/journal: exactly-once, reconcile, undo | done | `tests/test_journal.py`, gate `journal` |
| Supervisor: owned child processes, orphans reconciled, cleanup | done | `tests/test_ops.py`, gate `supervisor_cleanup` |
| Owner gate: PIN for irreversible work, per-OS-user profiles | done | gate `owner_gate` |
| Preflight (locked screen, power, GUI needs, deferral) | done | gate `preflight` |
| Responsiveness governor (priority, pauses, admission cap) | done | gate `governor`, `tests/test_ops.py` (`worker_limit`) |
| Resource admission: bounded team batches, browser release on shutdown, idle HUD pacing | done | `tests/test_agents_plugins.py` (`bounded_batches`), `tests/test_actions.py` (`browser_release`), `tests/test_hud.py` (`AnimationPowerTests`) |
| Language/OCR/RTL handling | partial | gate `language_rtl` |
| Billing modes (BYOK default, gateway, local-only) | partial | gate `billing_modes` |
| Model catalogue (signed, rollback, retired IDs) | partial | gate `catalog_rollback` |
| Outcome verification (criteria written first, verifier agent) | done | gate `risk_tiers`, `tests/test_agents_plugins.py` |
| Risk-scaled verification effort (L0–L3, batching, latency) | done | `orbynth/ops/verification.py`, `tests/test_ops.py` |
| Terminal engine (owned PTY, markers, echo verification) | done | `tests/test_terminal.py`, gate `terminal_engine` |
| Office Tier B (docx/xlsx/CSV without Office installed) | done | `tests/test_office_files.py`, gate `office_tier_b` |
| Office Tier A (installed Word/Excel/Access/Outlook) | partial, OS-bound | requires Windows + Office; see acceptance `office_tier_a` |
| Browser control (tabs, icons, forms, extraction, downloads) | partial | `orbynth/actions/browser.py`; needs Playwright/extension |
| Desktop control (mouse/keyboard/UIA, focus verification) | partial | `orbynth/actions/input_control.py`, `windows.py` |
| Vision (capture, OCR, grounding) | partial | `orbynth/actions/vision.py`; OCR needs an engine |
| WhatsApp (chats, media, calls) | partial | `orbynth/actions/whatsapp.py`; needs the sidecar |
| Plugins: authoring, install, rollback, sandbox | done (authoring tested offline) | `tests/test_agents_plugins.py`, gate `plugin_lifecycle` |
| Multi-agent fleet, roles, memory, reports, teams | done | `tests/test_agents_plugins.py` |
| Quotas, failover, Steward | done | `tests/test_llm.py` |
| HUD: particle core, wiring, panels, effects, scenes | done | `tests/test_hud.py`, `tools/hud_visual_check.js` |
| HUD topic coverage (no silent event) | done | gate `hud_coverage` |
| Hardware monitor S1 (real readings, filter, rules, protection) | partial | `tests/test_sensors.py`, gate `hardware_sensors` |
| Hardware monitor S2 (privileged fan/rail/MSR helper) | unbuilt | needs a signed helper; see LIMITATIONS |
| Local search index (FTS5, incremental, verified hits) | partial | `tests/test_index.py`, gate `local_index` |
| Fleet comms: memos, help requests, answers, file handoff, stored threads | done | `tests/test_comms.py`, `orbynth/agents/comms.py` |
| Model bench: limits intake (rpm/rpd/rpw, tpm/tpd/tpw), reserve activate/retire | done | `tests/test_reserve.py`, `orbynth/llm/reserve.py` |
| Weekly quota windows enforced end to end | done | `tests/test_reserve.py`, `orbynth/llm/quotas.py` |
| Speaker recognition: diarization, male/female/unknown estimate, voice enrolment | done (analysis is stdlib) | `tests/test_speakers.py`, `orbynth/voice/speakers.py` |
| Camera presence: consent gate, face detection, face enrolment | partial, hardware-bound | `tests/test_camera.py`, `orbynth/vision/camera.py`; needs a camera + `opencv-python`, identity needs dlib |
| Etiquette: speak, shorten, screen-only, wait or stay silent, with a reason | done | `tests/test_presence.py`, `orbynth/ops/presence.py` |
| Mistake memory: failures distilled to lessons, known fix attached to the next error | done | `tests/test_lessons.py`, `orbynth/ops/lessons.py` |
| Scheduled work: one-shot and recurring, grace window, gated GUI runs, bounded tick | done | `tests/test_schedule.py`, `orbynth/ops/schedule.py` |
| Semantic search (embeddings, `sqlite-vec`) | unbuilt | lexical only; see LIMITATIONS |
| MCP client/server, local REST/WS API, SDKs | unbuilt | spec 15 |
| Windows application payload builder (embedded Python/HUD/sidecar/browser manifest) | partial | `tools/build_windows_bundle.py`, `tests/test_packaging.py`; requires Windows release inputs |
| Signed Windows installer definition and hash-validated input gate | partial | `installer/ORBYNTH.iss`, `tools/build_windows_installer.py`, `tests/test_packaging.py`; needs a Windows signing environment and clean-VM acceptance |

## Verification commands

```bash
python -m unittest discover -s tests -q      # the unit/integration suite
python tools/reality_gate.py                 # no fakes, no markers, no test doubles in the package
python tools/capability_matrix.py            # regenerate the matrix
python tools/hud_coverage.py                 # regenerate topic coverage
node tools/hud_visual_check.js               # the HUD's visual layer, without a browser
python tools/release_gate.py --milestone M0  # the master release gate
```

The release gate is the single definition of "done" (spec 22.3). A blocking check cannot be
waived by an agent: waivers live in `tools/release_waivers.json` with the owner's written
sign-off recorded in them.

## What the numbers on this machine mean

The Capability Matrix on this host reports **0 green / 155 provisional / 24 unavailable** tool
rows (179 declared tools). That is not a regression and it is not modesty: *green* requires a recorded successful run
with evidence from this machine, and the automated suite runs against an offline `echo` provider,
so its runs do not count as proof of a desktop capability. A tool becomes green the first time
it really runs here and its run is recorded in `artifacts/capability_evidence.json`.