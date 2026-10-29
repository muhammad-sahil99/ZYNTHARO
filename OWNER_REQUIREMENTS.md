# OWNER REQUIREMENTS — what only you can provide or decide

This is the short list of things an agent cannot do for you. Until each one is done, the
corresponding part of the product either does not run or runs in a reduced mode — and says so in
the HUD instead of pretending.

## Decisions

1. **Launch mode** (spec 22.6). Default is **bring-your-own-keys**: each user supplies their own
   provider keys. The hosted gateway (you pay, users connect) additionally needs a legal review
   confirming every provider's terms allow resale; the gate blocks that mode until the
   acknowledgement is recorded in configuration.
2. **Whether the AI may power the machine down** (spec 16.5). Off by default. Turning it on lets a
   CRITICAL hardware alert hibernate or suspend the machine.
3. **Which personal sources may be indexed** (spec 14.2): email, WhatsApp chats, browser history,
   clipboard. Each is opt-in, separately, and adding one needs your PIN.
4. **Waivers**. If you choose to ship with a blocking acceptance check failing, the waiver must be
   written and signed in `tools/release_waivers.json`. Agents never waive a blocking check.
5. **Owner identity recovery** (spec 22.7): issue and store the recovery code. Voice and PIN both
   need a fallback, because voices can be cloned and PINs can be forgotten.

## Accounts, licences and hardware

| need | used for | without it |
|---|---|---|
| Provider API keys (Gemini/OpenAI/Anthropic/…) | real model calls | the app runs against the offline `echo` provider only |
| A Telegram bot token (`ORBYNTH_TELEGRAM_TOKEN`) | the reply desk over Telegram | the channel reports `needs_setup` with that instruction; nothing is sent |
| One live account per connector you intend to sell (a Gmail/Workspace mailbox with an app password, a Microsoft 365 tenant app registration, a Slack workspace, a GitHub org, one CRM) | proving each connector against the real service | connectors stay `configured`/`needs_setup`; their request shapes are tested, but no live acceptance has been run |
| A Google OAuth client (Desktop app type) in a test project | the Google connector's consent, refresh and quota behaviour | the Google connector stays `needs_setup` with the exact Cloud Console steps |
| A WhatsApp account to pair, plus a second phone to test with | reply desk over WhatsApp, media and call acceptance | the channel stays unpaired and refuses instead of queueing messages |
| Meta business account + reviewed app (only if Instagram/Messenger is ever sold) | DM access | those platforms stay declared unavailable, with the gate shown to the customer |
| Microsoft 365 test tenant + Office licences | Office Tier A acceptance, Outlook/Word/Excel/Excel-macro tests | Tier A checks are marked *blocked*, not passed; Tier B (file-based) still works |
| Google test tenant | Gmail-related acceptance | those checks stay blocked |
| Test WhatsApp numbers (two, plus a spare) | pairing, chats, media, call tests | WhatsApp checks stay blocked; the code refuses rather than faking |
| Code-signing certificate | signing the packaged binaries and the S2 helper | unsigned builds warn on launch; the S2 helper cannot be installed |
| Hardware matrix: one Intel and one AMD desktop, one laptop, NVIDIA and AMD GPUs | sensor accuracy acceptance (within 2 °C / 1% / 5% of a reference tool) | sensor rules ship tested against synthetic streams only (spec 16.7 requires that for rule tests, but accuracy claims need the rig) |
| A dedicated always-on machine or VM | unattended GUI work at night | such tasks are deferred with a reason |
| Sensor rig + privileged helper consent | fans, rails, CPU registers | those sensors show as unavailable with the reason |

## Environment and setup

* **For source/developer runs only:** Python 3.11+ and the optional extras you actually need:
  `pip install -e ".[desktop,voice,vision,hud]"` (add `windows` on Windows for UIA/Office COM).
  End users must not perform this setup: the Windows payload builder is now in
  `tools/build_windows_bundle.py`, but the signed installer and clean-machine acceptance are not
  built yet — see [BUILD_STATUS.md](BUILD_STATUS.md).
* **For source/developer runs only:** Playwright Chromium for browser control: `playwright install chromium`.
* **OCR engine** (`rapidocr-onnxruntime`) for reading text off images; **`pypdf`** for PDF text.
* **A virtual audio device** plus the WhatsApp sidecar for real-time voice calls.
* **Provider quotas**: fill in each model's rpm/rpd/tpm so the Steward can manage failover
  instead of discovering a limit by hitting it.
* **First run**: `orbynth owner setup` (PIN, voice samples, recovery code), then
  `orbynth doctor` to see which drivers exist on this machine.

## What you should never have to do

* Paste an API key into a chat, a file that is committed, or the source tree: keys belong in the
  app's settings/secrets, and the UI writes them there.
* Read a log to find out whether a claim is true: the Capability Matrix, the HUD panels and the
  release gate exist so the answer is on screen and in a file.