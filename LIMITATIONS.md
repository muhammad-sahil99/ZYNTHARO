# LIMITATIONS — what this build does not do

Nothing in this file is a plan in disguise. Each entry says what is missing, what the user sees
when they hit it, and what would close it. A limitation discovered later that is not written here
is a bug in this file.

## Not built at all

| spec | missing | what you see today |
|---|---|---|
| 16.2 S2 | The privileged sensor helper (Super I/O fans, rails, some CPU registers) | the Hardware panel lists those sensors as **unavailable** with the reason, next to a note that the S2 helper is not installed |
| 14.2 | Hybrid semantic search (local embeddings in `sqlite-vec`) | the Index panel says **not in this build (no embeddings)**; search is lexical FTS5 + BM25, so a query using different words from the document misses |
| 14.2 | OS file-change notifications | freshness comes from a scan; the panel says **from scans only** |
| 15.1 | MCP client and server | no MCP tools are exposed or consumed |
| 15.2 | Local REST/WebSocket API, SDKs, remote control from a phone | only the HUD's own local bridge exists |
| 19 | Signed end-user installer for a machine with no coding environment | `tools/build_windows_bundle.py` and `tools/build_windows_installer.py` reject incomplete/tampered payloads, and `installer/ORBYNTH.iss` defines the Windows installer; release signing and clean-VM acceptance are still required |
| 18 | Mobile handover, phone-as-remote, end-to-end encrypted remote control | not present |
| 20 | Cloud sync of a per-item subset of memory and permissions | not present |

## Built partially, with the gap named

* **Office Tier A** (installed Word/Excel/Access/Outlook) needs Windows and an Office licence.
  Everything that can be done by reading and writing files (`.docx`, `.xlsx`, CSV) works with no
  Office installed; PowerPoint authoring is *refused* rather than faked, and says reading is the
  supported direction.
* **Browser control** covers tabs, icons, navigation, forms, extraction, downloads and scrolling.
  It needs Playwright/Chromium installed (`playwright install chromium`); without it, every
  browser tool fails with that instruction instead of pretending to click. Chrome-extension-only
  surfaces (Lens, extension popups) are driven through the icons that exist in the page, and a
  surface that is not there returns "not found" rather than a fabricated success.
* **Desktop control** injects input and verifies focus before typing, so text cannot land in the
  wrong window. On Windows it uses UIA where available; on Linux it is X11-only in practice —
  Wayland input injection is refused with a reason.
* **Vision** captures and locates on screens that can be captured. OCR needs `rapidocr-onnxruntime`
  (or another recogniser); without one, OCR tools report the missing engine rather than returning
  plausible text.
* **Voice** is designed for a real human voice with barge-in and a low-latency path, but the
  quality you get depends on the STT/TTS you configure: the offline default is intelligible, not
  indistinguishable from a person. Real-time duplex calling (WhatsApp voice calls) additionally
  requires the sidecar and the virtual audio device; without them, call tools refuse.
* **Speaker recognition** separates voices and names whoever is enrolled, but the "male/female"
  answer is a *pitch estimate* and says `unknown` inside the overlap band (roughly 150–180 Hz);
  a child, a whisper, a shout or singing will move it without the person changing. It is shown as
  a guess with its basis, never stored as a fact and never consulted by the permission gate.
* **Two people talking at once on one microphone** are *flagged* as overlapping and left
  unattributed. Separating them needs a neural source-separation model, which this build does not
  ship; a confident attribution there would be a fabrication.
* **Camera presence** needs the owner's stored consent **and** the OS camera permission. Either one
  missing means no capture and an explicit refusal — there is no setting, flag or parameter that
  bypasses the operating system, by design. Detection needs `opencv-python`; recognising *who* a
  face belongs to needs `face_recognition` (dlib), and without it enrolment refuses instead of
  storing a box as if it were an identity. Frames are never written to disk or sent to a model.
* **Hardware monitor** detects what the OS exposes and reports the rest as unavailable. Its
  protective actions reduce the AI's own load and notify; hibernation/shutdown only happens if the
  owner turned that on explicitly.
* **Language handling** covers detection, RTL layout and OCR language packs. It does not translate
  the interface; textual *and* non-textual (icon-only, RTL-mirrored) application control is
  verified per application, not universally.
* **Billing** ships the BYOK and local-only paths in full. The hosted gateway code exists but must
  not be enabled until a lawyer confirms each provider's terms allow resale; the gate blocks that
  mode until the acknowledgement is recorded.
* **Model catalogue** applies a signed catalogue with rollback. The nightly drift check is a
  command, not a resident service.

## Resource use: what is controlled and what is not

* **What is enforced.** Background work pauses during games, calls, presentations and high
  load; the number of workers admitted at once is capped before they start and shrinks under
  pressure (default ceiling: two, one under load); team work runs in bounded batches rather than
  queueing every worker at once; the managed automation browser is released on shutdown and on
  request (`browser_release`); an idle HUD renders at 30 fps and a hidden one renders nothing.
* **What is not promised.** A hard CPU-percentage or RAM-percentage budget is *not* enforced.
  Neither Windows nor POSIX gives a portable way for this process to cap a child Chromium, a
  native OCR library or a local model at a chosen percentage, and claiming one would be false.
  The controls that are real are process priority (below interactive), an admission cap, pause
  and deferral, and releasing what we own. Measured CPU/RAM per feature on a target machine is
  an acceptance item, not a shipped claim.
* **Active work costs what it costs.** OCR, speech recognition, browser automation and local
  models are expensive while they run. The system defers and paces them; it does not make them
  free, and a low-end machine will feel them.

## Behaviours that are deliberately limited

* **The system never bypasses a locked screen.** Work needing the desktop while the screen is
  locked is deferred with a reason, or run in an isolated session if one is configured.
* **A voice match never authorises irreversible work.** Voices can be cloned; the PIN or a
  passkey is required for delete, send, call, install, macro and shell. The same holds for a
  recognised face or a known voice: identity changes tone and privacy, never permissions.
* **Scheduled runs are at-least-once across a crash, and never silently replayed.** A run that
  was in flight when the process stopped is marked *interrupted* when the schedule is reloaded.
  One-shot entries are disabled rather than repeated (a half-finished destructive action must not
  run twice), and recurring entries wait for their next real window. Actions that must not repeat
  rely on the journal's idempotency key, not on the scheduler.
* **The scheduler is interval-based, not a cron clone.** It supports one-shot and fixed-interval
  entries (minimum 60 s) with a grace window; calendar expressions and dependency chains between
  scheduled tasks are not implemented. A run missed past its grace window is skipped with a
  reason, and GUI work is deferred while the session is locked — a backlog is never replayed in
  a burst.
* **A learned lesson is advice, not an action.** The known fix is attached to the next error of
  the same kind and shown to agents in their prompt; nothing is retried or changed automatically
  because a lesson exists.
* **The camera is never used covertly.** If the user (or the OS) has turned the camera off, the
  assistant cannot see and says so; it cannot ask for, earn or bypass that permission at runtime.
* **The AI cannot loosen a protective sensor threshold.** It may tighten one; loosening is the
  owner's action, and the refusal says so.
* **Unattended GUI work on this machine** is limited by the same reality as any desktop agent:
  a task needing a visible, unlocked desktop is deferred when the machine is asleep, locked or
  running a video call.
* **`provisional` in the Capability Matrix is not a claim of working.** A tool is only *green*
  after a recorded successful run on this machine; nothing is upgraded by declaration.