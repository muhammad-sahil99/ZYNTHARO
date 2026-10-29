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
| Workflows: invoice intake (file, dedupe, xlsx ledger, read-back) | done (file work is OS-independent) | `tests/test_workflows.py`, gate `tests_core` |
| Workflows: reply desk (durable outbox, mandatory approval, wrong-platform refusal) | done | `tests/test_workflows.py` |
| Workflows: reply desk on a **connected mailbox** (search+read through the mail connector, folder-namespaced dedupe, threaded `Re:` replies, unreadable messages reported) | done as code; needs the customer's mailbox connected to run live | `orbynth/workflows/mailbox.py`, `orbynth/workflows/reply_desk.py`, `tests/test_mailbox_desk.py` |
| Reply desk store upgrade (adds `subject` in place; existing pending drafts kept) | done | `orbynth/workflows/reply_desk.py` (`_migrate`), `tests/test_mailbox_desk.py` |
| Workflows: recurring report (docx + xlsx, totals read back) | done | `tests/test_workflows.py` |
| Channels: Telegram Bot API (send, poll, offset, platform errors) | partial — transport verified against a real local Bot API server; a live bot needs the owner's token | `tests/test_workflows.py` (`TelegramChannelTests`) |
| Channels: WhatsApp sidecar adapter | partial | adapter refuses when unpaired; live pairing acceptance pending |
| Channels: Instagram/Messenger/X/LinkedIn/TikTok/Snapchat | declared unavailable with the exact business gate | `orbynth/channels/registry.py`, `tests/test_workflows.py` |
| Connectors: credential vault (DPAPI on Windows, 0600 elsewhere, values never logged) | done | `orbynth/connectors/store.py`, `tests/test_connectors.py` (`VaultTests`) |
| Connectors: mail over IMAP/SMTP (folders, search, read, attachments, send, mark, move) | done as code; needs the customer's app password to run live | `orbynth/connectors/mail.py`, `tests/test_connectors.py` (`MailConnectorTests`) |
| Connectors: Google (14 ops: Gmail, Drive, Calendar, Sheets, Tasks, Contacts) | partial — PKCE loopback + refresh implemented and now started from the Accounts panel; needs a customer OAuth client to exercise live | `orbynth/connectors/google.py`, `orbynth/connectors/signin.py` |
| Connectors: Microsoft 365 (14 ops: Outlook, Calendar, OneDrive, Excel, Teams) | partial — device-code sign-in wired to the Accounts panel; needs a tenant app registration to exercise live | `orbynth/connectors/microsoft.py`, `orbynth/connectors/signin.py` |
| Connectors: interactive sign-in sessions (scope profiles, pollable state, bounded threads, tokens written to the vault and never returned) | done; the two live flows are exercised with injected flow functions | `orbynth/connectors/signin.py`, `tests/test_connectors_hud.py` (`SignInSessionTests`) |
| Connectors: declared input fields per account (the form is generated from the connector, never hardcoded in the interface) | done | `orbynth/connectors/base.py` (`Field`), `tests/test_connectors_hud.py` (`DeclaredFieldsTests`) |
| HUD: Accounts panel (connect / verify / disconnect / run one operation / sign in, gates quoted) | done | `hud/index.html`, `hud/app.js`, `orbynth/hud/service.py`, `tests/test_connectors_hud.py` (`HudConnectorCommandTests`) |
| Connectors: token-based SaaS (Slack, GitHub, Notion, Airtable, Trello, Asana, Jira, HubSpot, Stripe, Dropbox, Zoom, Salesforce, Linear, Discord) | done as code, 95 declared operations; request shape asserted against an injected transport | `orbynth/connectors/saas.py`, `tests/test_connectors.py` (`SaasConnectorTests`) |
| Connectors: capability split (read / write / send) so a read agent cannot send mail | done | `orbynth/actions/connectors.py`, `tests/test_connectors.py` (`GateTests`) |
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

The Capability Matrix on this host reports **0 green / 155 provisional / 32 unavailable** tool
rows (187 declared tools). That is not a regression and it is not modesty: *green* requires a recorded successful run
with evidence from this machine, and the automated suite runs against an offline `echo` provider,
so its runs do not count as proof of a desktop capability. A tool becomes green the first time
it really runs here and its run is recorded in `artifacts/capability_evidence.json`.