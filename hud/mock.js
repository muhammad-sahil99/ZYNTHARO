/* ORBYNTH demo data.
 *
 * The interface is driven entirely by bridge frames. When no ORBYNTH core is running — for
 * example in a hosted preview, where no desktop exists to automate — this module replays a
 * realistic snapshot and a rolling event stream so the HUD can be inspected and judged on its
 * own. It is labelled "DEMO DATA" in the interface and is never silently substituted for a
 * live core. */

(function () {
  "use strict";

  const now = () => Date.now() / 1000;

  function agents() {
    return [
      { agent_id: "sov-1", name: "Sovereign", role: "sovereign", title: "Sovereign — main agent", rank: 0, rank_name: "sovereign",
        insignia: "✦", color: "#7cf6ff", model: "gemini:gemini-2.5-flash-live", status: "running", task: "invoice triage",
        team_id: "team-9f21", parent_id: null, capabilities: ["*"], memory: { episodes: 412, facts: 37, messages: 1180, tasks: 26, audit: 2410 } },
      { agent_id: "ste-1", name: "Steward", role: "steward", title: "Steward — fleet manager", rank: 1, rank_name: "steward",
        insignia: "◈", color: "#ffd166", model: "gemini:gemini-2.5-flash", status: "running", task: "quota watch",
        team_id: "fleet", parent_id: "sov-1", capabilities: ["process.list", "model.assign", "agent.control", "agent.spawn", "memory.admin", "network.http"], memory: { episodes: 88, facts: 12, messages: 210, tasks: 4, audit: 61 } },
      { agent_id: "for-1", name: "Foreman-9f21", role: "foreman", title: "Foreman — task head", rank: 2, rank_name: "foreman",
        insignia: "▲", color: "#9d7bff", model: "gemini:gemini-2.5-pro", status: "running", task: "fetch 41 invoices, rename, file",
        team_id: "team-9f21", parent_id: "sov-1", capabilities: ["agent.spawn", "agent.control", "filesystem.*", "browser.*", "vision.*", "input.*"], memory: { episodes: 63, facts: 9, messages: 188, tasks: 9, audit: 402 } },
      { agent_id: "car-2", name: "Cartographer", role: "cartographer", title: "Cartographer — browser specialist", rank: 3, rank_name: "specialist",
        insignia: "◎", color: "#22d3ee", model: "gemini:gemini-2.5-flash", status: "running", task: "download invoice PDFs from portal",
        team_id: "team-9f21", parent_id: "for-1", capabilities: ["browser.*", "vision.capture", "input.mouse", "input.type"], memory: { episodes: 141, facts: 22, messages: 320, tasks: 12, audit: 880 } },
      { agent_id: "scr-3", name: "Scrivener", role: "scrivener", title: "Scrivener — office specialist", rank: 3, rank_name: "specialist",
        insignia: "▤", color: "#eab308", model: "gemini:gemini-2.5-flash", status: "blocked", task: "waiting on Excel COM lock",
        team_id: "team-9f21", parent_id: "for-1", capabilities: ["office.excel", "office.word", "filesystem.write"], memory: { episodes: 74, facts: 15, messages: 160, tasks: 6, audit: 233 } },
      { agent_id: "art-4", name: "Artificer", role: "artificer", title: "Artificer — plugin engineer", rank: 3, rank_name: "specialist",
        insignia: "⚙", color: "#f97316", model: "gemini:gemini-2.5-pro", status: "running", task: "forging portal-invoice-fetch",
        team_id: null, parent_id: "sov-1", capabilities: ["filesystem.*", "plugin.author", "plugin.install", "shell.exec"], memory: { episodes: 52, facts: 18, messages: 121, tasks: 7, audit: 190 } },
      { agent_id: "ope-7", name: "Operative-3", role: "operative", title: "Operative — action worker", rank: 5, rank_name: "operative",
        insignia: "●", color: "#4ade80", model: "echo:offline-echo", status: "idle", task: "",
        team_id: "team-9f21", parent_id: "for-1", capabilities: ["input.*", "vision.*", "filesystem.read", "filesystem.write", "browser.click", "browser.type"], memory: { episodes: 19, facts: 3, messages: 44, tasks: 2, audit: 120 } },
      { agent_id: "pro-5", name: "Probe-1", role: "probe", title: "Probe — disposable explorer", rank: 7, rank_name: "probe",
        insignia: "○", color: "#94a3b8", model: "echo:offline-echo", status: "stopped", task: "",
        team_id: "team-9f21", parent_id: "for-1", capabilities: ["filesystem.read", "vision.capture", "browser.extract"], memory: { episodes: 6, facts: 0, messages: 11, tasks: 1, audit: 22 } },
    ];
  }

  function snapshot() {
    return {
      product: "ORBYNTH",
      alternate_brand: "SYNOVARIS",
      tagline: "desktop operative · vision · input · browser · files · office · self-extending",
      version: "0.1.0",
      demo: true,
      server_time: now(),
      config: {
        home: "~/Library/Application Support/ORBYNTH",
        theme: "orbynth-core",
        particle_budget: 6000,
        wires: true,
        voice: { enabled: true, wake_words: ["orbynth", "orb"], stt: "faster-whisper", tts: "edge-tts", tts_voice: "en-US-AriaNeural", barge_in: true },
        safety: { confirm_capabilities: ["filesystem.delete", "office.macro", "browser.eval", "shell.exec", "plugin.install", "whatsapp.call"], max_parallel_agents: 8, allow_network: true },
      },
      models: [
        { provider: "gemini", kind: "gemini", model: "gemini-2.5-flash", qualified: "gemini:gemini-2.5-flash", ready: true, rpm: 10, rpd: 250, tpm: 250000, tags: ["fast", "general"], audio_live: false },
        { provider: "gemini", kind: "gemini", model: "gemini-2.5-pro", qualified: "gemini:gemini-2.5-pro", ready: true, rpm: 5, rpd: 100, tpm: 250000, tags: ["reasoning"], audio_live: false },
        { provider: "groq", kind: "openai_compatible", model: "llama-3.3-70b", qualified: "groq:llama-3.3-70b", ready: true, rpm: 30, rpd: 14400, tpm: 6000, tags: ["fast", "cheap"], audio_live: false },
        { provider: "local", kind: "openai_compatible", model: "qwen2.5-coder-32b", qualified: "local:qwen2.5-coder-32b", ready: true, rpm: 120, rpd: null, tpm: 100000, tags: ["coding", "reasoning"], audio_live: false },
        { provider: "echo", kind: "echo", model: "offline-echo", qualified: "echo:offline-echo", ready: true, rpm: 1000, rpd: 100000, tpm: 500000, tags: ["offline"], audio_live: false },
      ],
      quotas: [
        { model: "gemini:gemini-2.5-flash", status: "warning", cooldown_s: 0, rpm: { used: 8, limit: 10 }, rpd: { used: 141, limit: 250 }, tpm: { used: 154000, limit: 250000 }, tpd: { used: 620000, limit: null }, errors: 0, last_error: "", total_requests: 141, total_tokens: 620000 },
        { model: "gemini:gemini-2.5-pro", status: "cooling", cooldown_s: 42, rpm: { used: 5, limit: 5 }, rpd: { used: 63, limit: 100 }, tpm: { used: 210000, limit: 250000 }, tpd: { used: 410000, limit: null }, errors: 2, last_error: "rate_limit: 429 quota exceeded", total_requests: 63, total_tokens: 410000 },
        { model: "groq:llama-3.3-70b", status: "ok", cooldown_s: 0, rpm: { used: 3, limit: 30 }, rpd: { used: 210, limit: 14400 }, tpm: { used: 8200, limit: 6000 }, tpd: { used: 40000, limit: null }, errors: 0, last_error: "", total_requests: 210, total_tokens: 40000 },
        { model: "local:qwen2.5-coder-32b", status: "ok", cooldown_s: 0, rpm: { used: 11, limit: 120 }, rpd: { used: 340, limit: null }, tpm: { used: 22000, limit: 100000 }, tpd: { used: 180000, limit: null }, errors: 0, last_error: "", total_requests: 340, total_tokens: 180000 },
      ],
      agents: agents(),
      teams: [{ team_id: "team-9f21", objective: "Collect every outstanding invoice from the supplier portal, rename to ISO dates, file by month", foreman: "for-1", members: ["car-2", "scr-3", "ope-7", "pro-5"], age_s: 384 }],
      tasks: [
        { task_id: "task-1", team_id: "team-9f21", owner: "car-2", title: "download 41 invoices", status: "running", detail: {}, report: {} },
        { task_id: "task-2", team_id: "team-9f21", owner: "scr-3", title: "reconcile amounts in Excel", status: "blocked", detail: {}, report: {} },
        { task_id: "task-3", team_id: "team-9f21", owner: "ope-7", title: "extract totals, write summary", status: "done", detail: {}, report: { summary: "summarised 41 invoices" } },
      ],
      plugins: [
        { name: "portal-invoice-fetch", version: "0.3.1", status: "loaded", capabilities: ["browser.click", "browser.extract", "filesystem.write"], actions: ["fetch_portal_invoices"], description: "Logs into the supplier portal and pulls every invoice PDF directly — no vision, no clicking.", task: "stop re-clicking the same download flow" },
        { name: "excel-reconcile", version: "0.2.0", status: "loaded", capabilities: ["office.excel", "filesystem.read"], actions: ["reconcile_sheet"], description: "Compares two Excel ranges and reports mismatches.", task: "manual amount checking" },
      ],
      actions: { count: 96, groups: { filesystem: ["fs_read", "fs_write", "fs_edit", "fs_zip", "fs_unzip", "fs_delete", "fs_list", "fs_search"], browser: ["browser_navigate", "browser_click", "browser_type", "browser_model", "browser_chrome_click"], input: ["input_click", "input_type", "input_hotkey"], office: ["office_word", "office_excel", "office_access"], orchestration: ["agent_task", "agent_team", "model_assign", "memory_recall"], whatsapp: ["whatsapp_pair", "whatsapp_send", "whatsapp_call"], vision: ["vision_capture", "vision_ocr", "vision_ground"], windows: ["windows_launch", "windows_focus"], shell: ["proc_run"] } },
      drivers: [
        { driver: "input", module: "pyautogui", available: true }, { driver: "vision", module: "mss", available: true },
        { driver: "vision", module: "rapidocr_onnxruntime", available: true }, { driver: "browser", module: "playwright", available: true },
        { driver: "office", module: "win32com.client", available: true }, { driver: "windows", module: "uiautomation", available: true },
        { driver: "voice", module: "faster_whisper", available: true }, { driver: "hud", module: "pywebview", available: true },
        { driver: "platform", module: "Windows 11", available: true },
      ],
      memory: { agents: 8, episodes: 855, facts: 116, messages: 2234, tasks: 67, audit: 3908 },
      pending_confirmations: [],
      jobs: [
        { job_id: "j1", name: "whatsapp-sidecar", kind: "sidecar", pid: 4271, status: "running", restarts: 0, age_s: 1220 },
        { job_id: "j2", name: "automation-browser-9222", kind: "browser", pid: 4302, status: "running", restarts: 1, age_s: 640 },
        { job_id: "j3", name: "ocr-worker", kind: "helper", pid: 4310, status: "restart #2", restarts: 2, age_s: 90 },
      ],
      verdicts: [
        { status: "needs_user", task: "task-3", summary: "2/2 criteria met (unmet: none); 0 unintended side effect(s); claim was: summarised 41 invoices", question: "Please confirm the parts only you can judge: does the summary read correctly?" },
        { status: "met", task: "task-2", summary: "3/3 criteria met; 0 unintended side effects" },
        { status: "not_met", task: "task-1", summary: "6/7 criteria met (unmet: invoices/2026-03); 0 unintended side effects", question: "" },
      ],
      preflight: [{ action: "defer", reasons: ["machine busy: video_call"], advice: "desktop work waits while the user is in a call", at: now() - 120 }],
      security: { configured: true, owner: "Owner", machine_kind: "personal", os_user: "owner-pc", foreign_profile: false, voice_prints: 2, recovery_available: true, session_valid: true, session_remaining_s: 1400, locked_out: false, irreversible_needs: "pin" },
      updates: { mode: "byok", catalog_path: "/home/orbynth/catalog.json", catalog_version: "2026.10", applied: "2026.10 (ed25519)", drift: "2 configured id(s) no longer served", armable: true, billing_issues: [], requests_today: 212, spend_today_usd: 0 },
      machine: { session_locked: false, display_attached: true, on_ac: true, battery_pct: 88, network: true, audio_device: true, idle_seconds: 4, busy_reason: "video_call: teams", priority: "below_normal", paused: { background: true, maintenance: true }, languages: "en, ur (ocr: eng+urd)", language_dir: "ltr" },
      hardware: {
        attached: true, worst: "clear", ts: now(), active_alerts: [], suspect: [],
        groups: {
          cpu: [{ key: "cpu.temp", label: "CPU package", value: 54.2, unit: "C", source: "sysfs/hwmon", available: true, suspect: "" },
                { key: "cpu.load", label: "CPU load", value: 0.23, unit: "ratio", source: "procfs/stat", available: true, suspect: "" }],
          memory: [{ key: "mem.used", label: "Memory used", value: 0.41, unit: "ratio", source: "procfs/meminfo", available: true, suspect: "" }],
          battery: [{ key: "battery.charge", label: "Battery", value: 88, unit: "%", source: "sysfs/power_supply", available: true, suspect: "" }],
          cooling: [{ key: "fan.cpu", label: "CPU fan", value: null, unit: "rpm", source: "", available: false, suspect: "", reason: "Super I/O fans need the S2 helper, which is not installed" }]
        },
        overhead: { samples: 240, window_s: 300.0, collector_cpu_ms_total: 420.0, collector_cpu_share: 0.0014, last_sample_ms: 1.5, target_share: 0.01, warming_up: false, within_target: true },
        report: { found_count: 4, unavailable: [{ key: "fan.cpu", reason: "Super I/O fans need the S2 helper, which is not installed" }] }
      },
      capabilities: {
        attached: true,
        summary: { tools: 149, green: 12, provisional: 123, unavailable: 14, ready_here: 40 },
        rows: [
          { tool: "fs_write", capability: "filesystem.write", status: "green", runs: 412 },
          { tool: "browser_click", capability: "browser.click", status: "provisional", runs: 0 },
          { tool: "office_word", capability: "office.word", status: "unavailable", runs: 0 }
        ]
      },
      search: {
        attached: true, documents: 18422, unextractable: 96, note: "search is lexical (FTS5, BM25): no embeddings, no OCR or PDF text without an installed extractor",
        sources: [{ name: "documents", kind: "files", root: "C:/Users/Owner/Documents", enabled: 1, opt_in: 0 }],
        capabilities: { lexical: true, semantic: false, ocr: true, pdf: true, file_watch: false }
      },
      replies: {
        attached: true,
        pending: [
          { id: 41, channel: "mail", chat: "dana@example.com", subject: "Invoice 2026-03", status: "pending_approval", created_at: now() - 620, sender: "Dana Reed <dana@example.com>", inbound_text: "Hello, invoice 2026-03 is still unpaid. Could you confirm whether it reached you?", reply: "Hi Dana — it did arrive, and it is scheduled for payment on Friday. I will send you the confirmation once it clears." },
          { id: 42, channel: "telegram", chat: "8812334", subject: "", status: "pending_approval", created_at: now() - 300, sender: "acme_ops", inbound_text: "Can you send the March statement as a PDF today?", reply: "Yes — sending the March statement as a PDF this afternoon." },
        ],
        history: [
          { id: 38, channel: "mail", chat: "sam@example.org", subject: "Change of address", status: "sent", decided_at: now() - 7200, error: "" },
          { id: 37, channel: "mail", chat: "dana@example.com", subject: "Invoice 2026-02", status: "rejected", decided_at: now() - 9000, error: "discarded from the interface" },
        ],
        counts: { pending: 2, sent: 1, rejected: 1, failed: 0 },
        sources: [
          { name: "mail", state: "ready", detail: "Gmail / Google Workspace as owner@example.com", auth: "app_password", account: "owner@example.com", verified_at: now() - 86400, operations: 7 },
          { name: "telegram", state: "needs_setup", detail: "Create a bot with @BotFather, then set the token.", auth: "api_token", account: "", verified_at: 0, operations: 0 },
        ],
      },
      connectors: {
        attached: true,
        total: 4, ready: 2, needs_setup: 1,
        rows: [
          { name: "mail", title: "Mailbox (IMAP/SMTP)", category: "mail", state: "ready", detail: "Gmail / Google Workspace as owner@example.com", auth: "app_password", account: "owner@example.com", verified_at: now() - 86400, operations: 7, needs: "The mailbox address and an app password from the provider's security settings.", where: "Your mail provider's security page (see the provider note in the app).", docs: "https://support.google.com/accounts/answer/185833" },
          { name: "slack", title: "Slack", category: "chat", state: "ready", detail: "Slack; credential verified against the service", auth: "api_token", account: "acme", verified_at: now() - 3600, operations: 7, needs: "A Slack app in your workspace with a bot token (xoxb-...) and the scopes you want to grant.", where: "api.slack.com/apps → create app → OAuth & Permissions → install → copy the Bot User OAuth Token.", docs: "https://api.slack.com/authentication/token-types" },
          { name: "google", title: "Google (Gmail, Drive, Calendar, Sheets, Tasks, Contacts)", category: "mail", state: "needs_setup", detail: "Google is not connected. You need an OAuth client id (Desktop app type) from Google Cloud, then consent once.", auth: "oauth2", account: "", verified_at: 0, operations: 14, needs: "A Google OAuth client id (Desktop app type), created once in Google Cloud, plus consent for the scopes you want.", where: "console.cloud.google.com → APIs & Services → Credentials → Create OAuth client ID → Desktop app.", docs: "https://developers.google.com/identity/protocols/oauth2/native-app" },
          { name: "github", title: "GitHub", category: "code", state: "configured", detail: "GitHub credential stored; run verify to prove it works", auth: "api_token", account: "", verified_at: 0, operations: 7, needs: "A personal access token with the scopes you want to grant.", where: "github.com/settings/tokens → Generate new token (classic) → copy it.", docs: "https://docs.github.com/en/rest" },
        ],
        unavailable: {
          instagram: "Meta requires an Instagram Business account, a reviewed Meta app and the instagram_manage_messages permission. Until that review is granted, no desktop product can read a customer's DMs.",
          linkedin: "LinkedIn does not license direct-message automation to third-party software at all.",
        },
        mail_providers: [
          { key: "gmail", title: "Gmail / Google Workspace", imap: "imap.gmail.com:993", smtp: "smtp.gmail.com:587", note: "Create an App Password at myaccount.google.com → Security.", supported_here: true },
          { key: "outlook", title: "Outlook / Microsoft 365", imap: "", smtp: "", note: "Microsoft disabled password sign-in; use the Microsoft 365 connector.", supported_here: false },
        ],
        vault: { path: "C:/Users/Owner/AppData/Roaming/ORBYNTH/connectors.vault.json", at_rest_protected: true, note: "encrypted with Windows DPAPI, bound to this Windows account" },
        connected: [{ name: "mail", keys: ["address", "password"], account: "owner@example.com" }, { name: "slack", keys: ["token"], account: "acme" }],
        signin_profiles: {
          google: [
            { key: "mail_read", label: "Read mail", scopes: ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"], note: "Reading Gmail is a restricted scope.", restricted: true },
            { key: "drive", label: "Drive files", scopes: ["openid", "email", "https://www.googleapis.com/auth/drive.file"], note: "", restricted: false },
          ],
          microsoft: [
            { key: "desk", label: "Mail, calendar, files, contacts and tasks", scopes: ["offline_access", "User.Read", "Mail.Send"], note: "Mail.Send is included; the owner approves each send.", restricted: false },
          ],
        },
        signin: [],
      },
      transcript: [
        { role: "user", text: "Pull the invoices off the supplier portal and file them by month.", ts: now() - 240 },
        { role: "assistant", text: "Foreman-9f21 is on it: downloading 41 invoices in parallel while Scrivener reconciles amounts.", ts: now() - 232 },
        { role: "user", text: "Also tell me if any are late.", ts: now() - 96 },
        { role: "assistant", text: "Three are past due — I'll flag them in the summary sheet.", ts: now() - 88 },
      ],
    };
  }

  const SCRIPT = [
    (t) => ({ type: "event", topic: "agent.message", event: { topic: "agent.message", source: "car-2", severity: "info", payload: { to: "for-1", preview: "invoices 12–24 downloaded", agent: "Cartographer" }, ts: t } }),
    (t) => ({ type: "event", topic: "action.result", event: { topic: "action.result", source: "car-2", severity: "info", payload: { action: "browser_click", ok: true, duration: 0.31, capability: "browser.click" }, ts: t } }),
    (t) => ({ type: "event", topic: "action.invoked", event: { topic: "action.invoked", source: "scr-3", severity: "info", payload: { action: "office_excel", args: { operation: "set_range" } }, ts: t } }),
    (t) => ({ type: "event", topic: "quota.warning", event: { topic: "quota.warning", source: "quota_manager", severity: "warning", payload: { model: "gemini:gemini-2.5-flash", window: "rpm", used: 8, limit: 10 }, ts: t } }),
    (t) => ({ type: "event", topic: "model.fallback", event: { topic: "model.fallback", source: "steward", severity: "notice", payload: { agent: "car-2", intended: "gemini:gemini-2.5-pro", actual: "groq:llama-3.3-70b", context_preserved: true }, ts: t } }),
    (t) => ({ type: "event", topic: "memory.write", event: { topic: "memory.write", source: "art-4", severity: "info", payload: { kind: "plugin", episode: 855 }, ts: t } }),
    (t) => ({ type: "event", topic: "action.denied", event: { topic: "action.denied", source: "ope-7", severity: "warning", payload: { action: "fs_delete", capability: "filesystem.delete", reason: "missing capability" }, ts: t } }),
    (t) => ({ type: "notice", text: "Scrivener is blocked on an Excel COM lock — retrying in 12s.", severity: "warning", speak: false, ts: t }),
    (t) => ({ type: "transcript", text: "how many invoices are already filed?", role: "user", ts: t }),
    (t) => ({ type: "transcript", text: "Twenty-eight so far; thirteen still downloading.", role: "assistant", ts: t }),
    (t) => ({ type: "event", topic: "agent.report", event: { topic: "agent.report", source: "pro-5", severity: "info", payload: { agent: "Probe-1", status: "done", summary: "portal search confirms 41 invoices outstanding", evidence: ["portal:search"] }, ts: t } }),
    (t) => ({ type: "event", topic: "plugin.installed", event: { topic: "plugin.installed", source: "plugin_host", severity: "info", payload: { plugin: "portal-invoice-fetch", version: "0.3.1", actions: ["fetch_portal_invoices"] }, ts: t } }),
    (t) => ({ type: "event", topic: "job.started", event: { topic: "job.started", source: "supervisor", severity: "info", payload: { name: "ocr-worker", kind: "helper", pid: 4433 }, ts: t } }),
    (t) => ({ type: "event", topic: "task.verified", event: { topic: "task.verified", source: "verifier", severity: "info", payload: { task_id: "task-4", status: "met", summary: "4/4 criteria met; 0 unintended side effects" }, ts: t } }),
    (t) => ({ type: "event", topic: "task.disputed", event: { topic: "task.disputed", source: "verifier", severity: "warning", payload: { task_id: "task-1", status: "not_met", summary: "6/7 criteria met (unmet: invoices/2026-03)" }, ts: t } }),
    (t) => ({ type: "event", topic: "governor.paused", event: { topic: "governor.paused", source: "governor", severity: "notice", payload: { reasons: "busy: video_call" }, ts: t } }),
    (t) => ({ type: "event", topic: "governor.resumed", event: { topic: "governor.resumed", source: "governor", severity: "notice", payload: {}, ts: t } }),
    (t) => ({ type: "event", topic: "update.applied", event: { topic: "update.applied", source: "catalog", severity: "info", payload: { version: "2026.10", method: "ed25519" }, ts: t } }),
    (t) => ({ type: "event", topic: "job.restarted", event: { topic: "job.restarted", source: "supervisor", severity: "notice", payload: { name: "ocr-worker", restarts: 2 }, ts: t } }),
    (t) => ({ type: "event", topic: "voice.language", event: { topic: "voice.language", source: "voice", severity: "notice", payload: { detected: "ur", configured: "en", text: "رپورٹ محفوظ کریں" }, ts: t } }),
    (t) => ({ type: "event", topic: "owner.denied", event: { topic: "owner.denied", source: "owner", severity: "warning", payload: { reason: "bad PIN", locked_out: false }, ts: t } }),
    (t) => ({ type: "event", topic: "voice.speaking", event: { topic: "voice.speaking", source: "voice", severity: "info", payload: { text: "Three invoices are past due — flagging them in the summary sheet." }, ts: t } }),
    (t) => ({ type: "event", topic: "voice.listening", event: { topic: "voice.listening", source: "voice", severity: "info", payload: { wake: "orbynth" }, ts: t } }),
    (t) => ({ type: "event", topic: "agent.message", event: { topic: "agent.message", source: "scr-3", severity: "info", payload: { to: "for-1", preview: "Excel lock released, reconciling", agent: "Scrivener" }, ts: t } }),
    (t) => ({ type: "event", topic: "task.assigned", event: { topic: "task.assigned", source: "for-1", severity: "info", payload: { agent: "Cartographer", task: "verify 13 remaining downloads" }, ts: t } }),
  ];

  /* A 3D explanation the demo can open in the scenario window, so the mesh renderer is visible
   * in a hosted preview where no core exists to send one. */
  const SCENE_3D = {
    type: "scene",
    kind: "mesh",
    title: "Invoice pipeline — 3D flow",
    payload: {
      text: "Each hop is a real action; the warm node is the one the fleet is working on.",
      highlight: ["portal"],
      nodes: [
        { id: "portal", label: "supplier portal", x: -1.1, y: 0.5, z: 0.2 },
        { id: "download", label: "download", x: -0.2, y: 0.9, z: -0.5 },
        { id: "ocr", label: "OCR extract", x: 0.7, y: 0.6, z: 0.3 },
        { id: "excel", label: "Excel reconcile", x: 1.3, y: -0.1, z: -0.4 },
        { id: "summary", label: "summary sheet", x: 0.4, y: -0.7, z: 0.6 },
        { id: "archive", label: "file by month", x: -0.7, y: -0.9, z: -0.2 },
      ],
      edges: [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [1, 3], [5, 0]],
    },
  };

  window.OrbythMock = {
    snapshot: snapshot,

    /* Emit a rolling simulation of core traffic. Returns a stop function. */
    start(emit) {
      let index = 0;
      let stopped = false;
      const tick = () => {
        if (stopped) return;
        const factory = SCRIPT[index % SCRIPT.length];
        index += 1;
        try {
          emit(factory(now()));
        } catch (error) {
          console.warn("[orbynth] mock emit failed", error);
        }
        if (index % 9 === 3) {
          emit({ type: "scene", kind: "chart", title: "Invoice status by month", payload: { series: [{ label: "filed", value: 28 }, { label: "pending", value: 13 }] }, ts: now() });
        }
        if (index % 13 === 7) {
          emit(Object.assign({}, SCENE_3D, { ts: now() }));
        }
        window.setTimeout(tick, 1400 + Math.random() * 1800);
      };
      window.setTimeout(tick, 900);
      return () => { stopped = true; };
    },
  };
})();
