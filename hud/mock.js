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
  ];

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
        window.setTimeout(tick, 1400 + Math.random() * 1800);
      };
      window.setTimeout(tick, 900);
      return () => { stopped = true; };
    },
  };
})();
