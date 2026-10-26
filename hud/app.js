/* ORBYNTH HUD controller.
 *
 * Everything on screen is driven by the bridge protocol. If a live core is reachable the HUD
 * shows truth; if not, it says "DEMO DATA" and replays mock.js. It never blurs the two.
 *
 * Controls: ☰ menu (chat input, panels, wiring, speaking, microphone), hold-to-talk, clicking
 * an agent node or card, confirm cards for destructive capabilities, and the scene viewer for
 * visual explanations.
 */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    live: false,
    demo: false,
    snapshot: null,
    agents: [],
    quotas: [],
    plugins: [],
    drivers: [],
    transcript: [],
    actionLog: [],
    scene: null,
    selectedAgent: null,
    speakReplies: true,
    micEnabled: false,
    voiceMuted: false,
    seenMessages: new Set(),
  };

  const core = new ParticleCore($("core"), { count: 6000 });
  const wires = new WireGraph($("wires"), { onSelect: selectAgent });

  /* ------------------------------------------------------------------ core state mapping */
  function coreState(label, sub) {
    $("core-state").textContent = label;
    if (sub !== undefined) $("core-sub").textContent = sub;
  }

  const TOPIC_EFFECTS = {
    "agent.spawned": (e) => { core.pulseShear(0.35); core.setShape("torus"); coreState("fleet growing", (e.payload.name || "agent") + " joined"); },
    "agent.terminated": (e) => { core.pulseShear(0.2); coreState("fleet shrinking", (e.payload.agent || "agent") + " left"); },
    "agent.message": (e) => { core.pulseShear(0.18); },
    "agent.report": (e) => { core.pulseEnergy(0.4); coreState(e.payload.status === "done" ? "task complete" : "task " + (e.payload.status || "partial"), e.payload.summary || ""); },
    "agent.error": () => { core.collapse(); coreState("agent error", "recovering"); },
    "task.created": () => core.setShape("helix"),
    "task.assigned": (e) => { coreState("delegating", (e.payload.agent || "") + " ← " + (e.payload.task || "")); },
    "action.invoked": (e) => { core.pulseShear(0.5); core.setShape(shapeForAction(e.payload.action)); coreState("acting", e.payload.action || ""); },
    "action.result": (e) => { core.pulseEnergy(e.payload.ok ? 0.25 : 0.5); },
    "action.denied": (e) => { core.pulseShear(0.7); coreState("capability denied", (e.payload.action || "") + " needs " + (e.payload.capability || "")); },
    "action.confirm": (e) => { coreState("confirmation required", e.payload.action || ""); },
    "quota.warning": () => coreState("quota pressure", "Steward watching"),
    "quota.exhausted": () => { core.pulseShear(0.6); coreState("quota exhausted", "failover in progress"); },
    "model.fallback": (e) => { core.pulseShear(0.4); coreState("model failover", (e.payload.intended || "") + " → " + (e.payload.actual || "")); },
    "provider.error": () => coreState("provider error", "retrying on another model"),
    "memory.write": () => core.pulseEnergy(0.12),
    "memory.recall": () => core.pulseShear(0.12),
    "plugin.authored": () => core.setShape("grid"),
    "plugin.installed": (e) => { core.pulseEnergy(0.55); coreState("capability extended", (e.payload.plugin || "") + " installed"); },
    "plugin.failed": () => { core.collapse(); coreState("plugin rejected", "rolled back"); },
    "plugin.uninstalled": () => coreState("plugin removed", ""),
    "voice.wake": () => { core.pulseEnergy(0.6); coreState("listening", "wake word detected"); },
    "voice.listening": () => { core.setShape("sphere"); coreState("listening", "speak now"); },
    "voice.transcript": (e) => addTranscript(e.payload.role || "user", e.payload.text || ""),
    "voice.speaking": (e) => { core.pulseEnergy(1); core.setShape("wave"); coreState("speaking", (e.payload.text || "").slice(0, 80)); },
    "hud.notice": (e) => notice(e.payload.text || "", e.payload.severity || "info", e.payload.speak !== false, e.payload.agent),
    "system.log": () => {},
  };

  function shapeForAction(name) {
    if (!name) return "sphere";
    if (name.startsWith("fs_") || name.startsWith("office_")) return "grid";
    if (name.startsWith("browser_")) return "torus";
    if (name.startsWith("agent_") || name.startsWith("model_")) return "helix";
    return "wave";
  }

  /* ------------------------------------------------------------------ bridge / demo link */
  let socket = null;
  let mockStop = null;
  let reconnectTimer = null;

  function connect() {
    const forcedDemo = new URLSearchParams(location.search).get("mock") === "1";
    if (forcedDemo) return startDemo("mock=1");
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = protocol + "//" + location.host + "/ws";
    setLink("connecting…", "");
    try {
      socket = new WebSocket(url);
    } catch (error) {
      return startDemo("socket unavailable");
    }
    socket.addEventListener("open", () => {
      state.live = true;
      state.demo = false;
      setLink("live", "ok");
      $("m-mock").checked = false;
      if (mockStop) { mockStop(); mockStop = null; }
    });
    socket.addEventListener("message", (event) => {
      try {
        handle(JSON.parse(event.data));
      } catch (error) {
        console.warn("[orbynth] bad frame", error);
      }
    });
    socket.addEventListener("close", () => {
      state.live = false;
      setLink("offline", "bad");
      if (!state.demo) reconnectTimer = window.setTimeout(connect, 4000);
    });
    socket.addEventListener("error", () => {
      if (socket.readyState > 1) socket.close();
    });
  }

  function startDemo(why) {
    state.demo = true;
    state.live = false;
    setLink("demo data", "warn");
    $("m-mock").checked = true;
    $("menu-note").textContent = "bridge: none (" + why + ") — showing DEMO DATA";
    applySnapshot(window.OrbythMock.snapshot());
    if (mockStop) mockStop();
    mockStop = window.OrbythMock.start((message) => handle(message));
  }

  function handle(message) {
    switch (message.type) {
      case "hello":
        applySnapshot(message.snapshot || {});
        break;
      case "state":
        applySnapshot(message.state || {});
        break;
      case "event": {
        const topic = message.topic || (message.event && message.event.topic) || "";
        const payload = message.event || { payload: {} };
        const effect = TOPIC_EFFECTS[topic];
        if (effect) effect(payload);
        if (socket && topic === "action.confirm") { /* card rendered by effect chain below */ }
        if (topic === "action.confirm") renderConfirmation(payload);
        if (topic === "action.invoked" || topic === "action.result" || topic === "action.denied") pushActionLog(payload);
        if (topic.startsWith("agent.")) pulseAgent(payload);
        break;
      }
      case "notice":
        notice(message.text || "", message.severity || "info", message.speak !== false);
        break;
      case "transcript":
        addTranscript(message.role || "user", message.text || "");
        break;
      case "scene":
        openScene(message.kind || "diagram", message.title || "Scene", message.payload || {});
        break;
      case "ack":
        if (message.command === "confirm") removeConfirmation(message.result && message.result.action);
        if (message.command === "state.request") refreshState();
        break;
      case "error":
        notice(String(message.error || "bridge error"), "error", false);
        break;
      default:
        break;
    }
  }

  function send(type, extra) {
    const payload = Object.assign({ type: type }, extra || {});
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  function setLink(text, level) {
    $("link-state").textContent = text;
    const vital = $("link-state").closest(".vital");
    vital.classList.toggle("bad", level === "bad");
    vital.classList.toggle("warn", level === "warn");
  }

  /* ------------------------------------------------------------------ rendering */
  function applySnapshot(snapshot) {
    if (!snapshot || !Object.keys(snapshot).length) return;
    state.snapshot = snapshot;
    if (snapshot.demo) state.demo = true;
    if (snapshot.tagline) $("tagline").textContent = snapshot.tagline;
    if (snapshot.agents) state.agents = snapshot.agents;
    if (snapshot.quotas) state.quotas = snapshot.quotas;
    if (snapshot.plugins) state.plugins = snapshot.plugins;
    if (snapshot.drivers) state.drivers = snapshot.drivers;
    if (snapshot.transcript && state.transcript.length === 0) {
      state.transcript = snapshot.transcript.slice(-40);
    }
    renderVitals(snapshot);
    renderAgents();
    renderQuotas();
    renderPlugins();
    renderDrivers();
    renderTranscript();
    wires.setAgents(state.agents);
    (snapshot.pending_confirmations || []).forEach(renderConfirmation);
  }

  function renderVitals(snapshot) {
    const models = snapshot.models || [];
    const sovereign = state.agents.find((a) => a.rank === 0);
    $("model-state").textContent = (sovereign && sovereign.model) || (models[0] && models[0].qualified) || "—";
    $("agent-count").textContent = String(state.agents.length);
    $("action-count").textContent = String((snapshot.actions && snapshot.actions.count) || 0);
    const worst = state.quotas.some((q) => q.status === "exhausted" || q.status === "cooling")
      ? "critical"
      : state.quotas.some((q) => q.status === "warning") ? "pressure" : "ok";
    $("quota-state").textContent = worst;
    const quotaVital = $("quota-state").closest(".vital");
    quotaVital.classList.toggle("warn", worst === "pressure");
    quotaVital.classList.toggle("bad", worst === "critical");
    const memory = snapshot.memory || {};
    $("memory-state").textContent = [memory.episodes || 0, memory.facts || 0, memory.tasks || 0].join(" / ");
    if (snapshot.demo && $("link-state").textContent !== "demo data") setLink("demo data", "warn");
  }

  function renderAgents() {
    const container = $("agents");
    container.innerHTML = "";
    const sorted = state.agents.slice().sort((a, b) => (a.rank - b.rank) || String(a.name).localeCompare(String(b.name)));
    sorted.forEach((agent) => {
      const node = document.createElement("div");
      node.className = "agent status-" + (agent.status || "idle") + (state.selectedAgent === agent.agent_id ? " active" : "");
      node.innerHTML =
        '<div class="row1">' +
          '<span class="insignia" style="color:' + (agent.color || "#5ad1ff") + '">' + (agent.insignia || "●") + "</span>" +
          '<span class="name">' + escapeHtml(agent.name || "") + "</span>" +
          '<span class="rank">rank-' + (agent.rank === undefined ? "?" : agent.rank) + "</span>" +
        "</div>" +
        '<div class="row2">' + escapeHtml(agent.model || "unassigned") + " · " + escapeHtml(agent.status || "idle") +
          " · mem " + ((agent.memory && agent.memory.episodes) || 0) + " · caps " + ((agent.capabilities || []).length) +
        "</div>" +
        (agent.task ? '<div class="task">▸ ' + escapeHtml(agent.task) + "</div>" : "");
      node.addEventListener("click", () => selectAgent(agent.agent_id));
      container.appendChild(node);
    });
    $("team-count").textContent = ((state.snapshot && state.snapshot.teams && state.snapshot.teams.length) || 0) + " teams";
  }

  function renderQuotas() {
    const table = $("quota-table");
    table.innerHTML = "";
    state.quotas.forEach((quota) => {
      const row = document.createElement("div");
      row.className = "trow " + (quota.status || "");
      const ratio = (pair) => (pair && pair.limit ? Math.min(1, pair.used / pair.limit) : 0);
      row.innerHTML =
        '<span class="m" title="' + escapeHtml(quota.model) + '">' + escapeHtml(quota.model) + "</span>" +
        '<span class="bar"><i style="width:' + (ratio(quota.rpm) * 100).toFixed(0) + '%"></i></span>' +
        '<span class="bar"><i style="width:' + (ratio(quota.tpm) * 100).toFixed(0) + '%"></i></span>' +
        '<span>' + (quota.rpm ? quota.rpm.used + "/" + quota.rpm.limit : "—") + "</span>" +
        '<span class="status ' + (quota.status || "") + '">' +
          (quota.status === "cooling" ? "cool " + Math.round(quota.cooldown_s || 0) + "s" : (quota.status || "ok")) +
        "</span>";
      row.title =
        (quota.model || "") + "\n" +
        "rpm " + fmtPair(quota.rpm) + "  rpd " + fmtPair(quota.rpd) + "\n" +
        "tpm " + fmtPair(quota.tpm) + "  tpd " + fmtPair(quota.tpd) + "\n" +
        "errors: " + (quota.errors || 0) + (quota.last_error ? " — " + quota.last_error : "");
      table.appendChild(row);
    });
  }

  function fmtPair(pair) {
    if (!pair) return "—";
    return pair.used + "/" + (pair.limit === null || pair.limit === undefined ? "∞" : pair.limit);
  }

  function renderPlugins() {
    const container = $("plugins");
    container.innerHTML = "";
    if (!state.plugins.length) {
      container.innerHTML = '<p class="hint">No plugins yet. When the fleet repeats a workflow, the Artificer writes and installs one.</p>';
    }
    state.plugins.forEach((plugin) => {
      const node = document.createElement("div");
      node.className = "plugin";
      node.innerHTML =
        '<div class="p-name">' + escapeHtml(plugin.name || "") + " <span class='chip'>v" + escapeHtml(plugin.version || "?") + "</span></div>" +
        '<div class="p-meta">' + escapeHtml(plugin.description || "") + "</div>" +
        '<div class="p-meta">actions: ' + escapeHtml((plugin.actions || []).join(", ")) + "</div>" +
        '<div class="p-meta">caps: ' + escapeHtml((plugin.capabilities || []).join(", ")) + " · " + escapeHtml(plugin.status || "") + "</div>";
      container.appendChild(node);
    });
  }

  function renderDrivers() {
    const table = $("drivers");
    table.innerHTML = "";
    state.drivers.forEach((driver) => {
      const row = document.createElement("div");
      row.className = "trow" + (driver.available ? "" : " warning");
      row.innerHTML =
        '<span class="m">' + escapeHtml(driver.driver || "") + "</span>" +
        '<span style="grid-column: span 3; color: var(--ink-dim)">' + escapeHtml(driver.module || "") + "</span>" +
        '<span class="status ' + (driver.available ? "" : "warning") + '">' + (driver.available ? "ok" : "missing") + "</span>";
      table.appendChild(row);
    });
  }

  function renderTranscript() {
    const container = $("transcript");
    container.innerHTML = "";
    state.transcript.slice(-40).forEach((entry) => {
      container.appendChild(messageNode(entry.role, entry.text, entry.meta));
    });
    container.scrollTop = container.scrollHeight;
  }

  function messageNode(role, text, meta) {
    const node = document.createElement("div");
    node.className = "msg " + (role === "assistant" ? "assistant" : role === "user" ? "user" : "system");
    node.innerHTML =
      '<div class="who">' + escapeHtml(role) + "</div>" +
      '<div class="text">' + escapeHtml(text || "") + "</div>" +
      (meta ? '<div class="meta">' + escapeHtml(meta) + "</div>" : "");
    return node;
  }

  function addTranscript(role, text, meta) {
    if (!text) return;
    state.transcript.push({ role: role, text: text, meta: meta });
    const container = $("transcript");
    container.appendChild(messageNode(role, text, meta));
    container.scrollTop = container.scrollHeight;
    if (role === "assistant" && state.speakReplies && !state.voiceMuted) speak(text);
  }

  function pushActionLog(event) {
    const payload = event.payload || {};
    state.actionLog.unshift({
      ts: event.ts || Date.now() / 1000,
      action: payload.action || "",
      outcome: payload.ok === undefined ? (event.topic === "action.denied" ? "denied" : "…") : (payload.ok ? "ok" : "error"),
      capability: payload.capability || "",
      duration: payload.duration,
    });
    state.actionLog = state.actionLog.slice(0, 120);
    const container = $("action-log");
    container.innerHTML = "";
    state.actionLog.forEach((row) => {
      const line = document.createElement("div");
      line.className = "line " + row.outcome;
      const stamp = new Date(row.ts * 1000).toLocaleTimeString();
      line.textContent = stamp + "  " + row.action + "  →  " + row.outcome +
        (row.capability ? "  [" + row.capability + "]" : "") +
        (row.duration ? "  " + row.duration + "s" : "");
      container.appendChild(line);
    });
  }

  function pulseAgent(payload) {
    const from = payload.source;
    const to = payload.payload && payload.payload.to;
    wires.pulse(from, to, payload.topic);
    if (payload.topic === "agent.report" || payload.topic === "agent.spawned") refreshState();
  }

  function notice(text, severity, speakIt, agent) {
    const container = $("notices");
    const node = document.createElement("div");
    node.className = "notice " + (severity || "info");
    node.innerHTML = (agent ? '<div class="agent">' + escapeHtml(agent) + "</div>" : "") + escapeHtml(text);
    container.appendChild(node);
    window.setTimeout(() => node.remove(), 9000);
    if (speakIt && state.speakReplies && !state.voiceMuted) speak(text);
  }

  /* ------------------------------------------------------------------ confirmations */
  function renderConfirmation(event) {
    const payload = event.payload || {};
    const action = payload.action || "";
    if (!action || document.querySelector('[data-confirm="' + CSS.escape(action) + '"]')) return;
    const card = document.createElement("div");
    card.className = "confirm";
    card.dataset.confirm = action;
    card.innerHTML =
      '<div class="title">CONFIRMATION REQUIRED · ' + escapeHtml(payload.capability || "") + "</div>" +
      '<div class="detail">' + escapeHtml(payload.detail || action) + "</div>" +
      '<div class="row">' +
        '<button class="pill" data-yes="1">approve once</button>' +
        '<button class="pill" data-no="1">deny</button>' +
      "</div>";
    card.querySelector('[data-yes="1"]').addEventListener("click", () => {
      send("confirm", { action: action, approved: true });
      card.remove();
    });
    card.querySelector('[data-no="1"]').addEventListener("click", () => {
      send("confirm", { action: action, approved: false });
      card.remove();
    });
    $("confirmations").appendChild(card);
    if (state.speakReplies && !state.voiceMuted) speak("Confirmation needed for " + action);
  }

  function removeConfirmation(action) {
    if (!action) return;
    const node = document.querySelector('[data-confirm="' + CSS.escape(action) + '"]');
    if (node) node.remove();
  }

  /* ------------------------------------------------------------------ scene viewer */
  function openScene(kind, title, payload) {
    state.scene = { kind: kind, title: title, payload: payload };
    $("scene-title").textContent = title;
    const body = $("scene-body");
    body.innerHTML = "";
    if (kind === "chart") {
      const canvas = document.createElement("canvas");
      canvas.width = 460; canvas.height = 280;
      body.appendChild(canvas);
      const series = payload.series || payload.data || [];
      const ctx = canvas.getContext("2d");
      const max = Math.max.apply(null, series.map((s) => s.value || 0).concat([1]));
      series.forEach((item, index) => {
        const barWidth = (460 - 60) / Math.max(1, series.length);
        const height = ((item.value || 0) / max) * 200;
        ctx.fillStyle = index % 2 ? "#9d7bff" : "#5ad1ff";
        ctx.fillRect(40 + index * barWidth, 240 - height, barWidth * 0.62, height);
        ctx.fillStyle = "rgba(232,244,255,0.85)";
        ctx.font = "11px monospace";
        ctx.fillText(String(item.label || ""), 40 + index * barWidth, 258);
        ctx.fillText(String(item.value || 0), 40 + index * barWidth, 238 - height);
      });
    } else if (kind === "table") {
      const rows = payload.rows || [];
      const table = document.createElement("table");
      const header = document.createElement("tr");
      (payload.columns || []).forEach((column) => {
        const th = document.createElement("th");
        th.textContent = column;
        header.appendChild(th);
      });
      table.appendChild(header);
      rows.forEach((row) => {
        const tr = document.createElement("tr");
        row.forEach((cell) => {
          const td = document.createElement("td");
          td.textContent = String(cell);
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      body.appendChild(table);
    } else if (kind === "image") {
      const img = document.createElement("img");
      img.src = payload.src || payload.path || "";
      body.appendChild(img);
    } else {
      const wrap = document.createElement("div");
      (payload.nodes || payload.steps || ["step one", "step two", "step three"]).forEach((node) => {
        const chip = document.createElement("span");
        chip.className = "diagram-node";
        chip.textContent = typeof node === "string" ? node : (node.label || node.id || "node");
        wrap.appendChild(chip);
      });
      if (payload.text) {
        const paragraph = document.createElement("p");
        paragraph.textContent = payload.text;
        wrap.appendChild(paragraph);
      }
      body.appendChild(wrap);
    }
    $("scene").classList.remove("hidden");
  }

  /* ------------------------------------------------------------------ voice */
  function speak(text) {
    if (!("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(String(text).slice(0, 400));
      utterance.rate = 1.03;
      utterance.pitch = 1.0;
      utterance.volume = 0.95;
      window.speechSynthesis.speak(utterance);
    } catch (error) {
      console.warn("[orbynth] speech failed", error);
    }
  }

  let recognition = null;
  function startMic() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      notice("This browser has no speech recognition. The core's own wake word and STT take over when installed.", "warning", false);
      return false;
    }
    recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = "en-US";
    recognition.onresult = (event) => {
      const result = event.results[event.results.length - 1];
      const text = result[0].transcript.trim();
      if (!text) return;
      addTranscript("user", text);
      if (!send("chat", { text: text })) notice("Not connected to a core — message not delivered.", "warning", false);
    };
    recognition.onerror = (event) => {
      if (event.error !== "no-speech") notice("microphone: " + event.error, "warning", false);
    };
    try {
      recognition.start();
      state.micEnabled = true;
      $("m-mic").checked = true;
      notice("Browser microphone active — speak and the transcript is sent to the core.", "info", false);
      return true;
    } catch (error) {
      notice("could not start the microphone: " + error.message, "warning", false);
      return false;
    }
  }

  function stopMic() {
    if (recognition) { try { recognition.stop(); } catch (e) { /* ignore */ } }
    state.micEnabled = false;
    $("m-mic").checked = false;
  }

  function setVoiceMuted(muted) {
    state.voiceMuted = muted;
    const button = $("voice-toggle");
    button.classList.toggle("muted", muted);
    $("voice-label").textContent = muted ? "VOICE MUTED" : "VOICE LIVE";
    send("voice.mute", { muted: muted });
  }

  /* ------------------------------------------------------------------ controls */
  function selectAgent(agentId) {
    state.selectedAgent = agentId;
    renderAgents();
    const agent = state.agents.find((a) => a.agent_id === agentId);
    if (!agent) return;
    const memory = agent.memory || {};
    openScene("diagram", agent.name, {
      steps: [
        "role: " + (agent.role || "") + " · rank " + (agent.rank === undefined ? "?" : agent.rank),
        "model: " + (agent.model || "unassigned"),
        "status: " + (agent.status || "idle"),
        "capabilities: " + (agent.capabilities || []).length,
        "memory: " + (memory.episodes || 0) + " episodes / " + (memory.facts || 0) + " facts",
        "task: " + (agent.task || "—"),
      ],
      text: "Memory is bound to the agent identity, not the model — assigning another model keeps everything.",
    });
  }

  function bindMenu() {
    $("menu-btn").addEventListener("click", () => $("menu").classList.toggle("hidden"));
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".menu-wrap")) $("menu").classList.add("hidden");
    });
    $("m-chat").addEventListener("change", (event) => {
      $("chatbar").classList.toggle("hidden", !event.target.checked);
      if (event.target.checked) $("chat-input").focus();
    });
    $("m-chat-panel").addEventListener("change", (event) => {
      document.querySelector('[data-panel="transcript"]').style.display = event.target.checked ? "block" : "none";
    });
    $("m-agents").addEventListener("change", (event) => {
      $("agents-panel").classList.toggle("hidden", !event.target.checked);
    });
    $("m-panels").addEventListener("change", (event) => {
      $("side-panels").classList.toggle("hidden", !event.target.checked);
    });
    $("m-wires").addEventListener("change", (event) => {
      $("wires").style.display = event.target.checked ? "block" : "none";
      wires.showLabels = event.target.checked;
    });
    $("m-webgl").addEventListener("change", (event) => {
      $("core").style.display = event.target.checked ? "block" : "none";
    });
    $("m-speak").addEventListener("change", (event) => { state.speakReplies = event.target.checked; });
    $("m-mic").addEventListener("change", (event) => {
      if (event.target.checked) { if (!startMic()) event.target.checked = false; } else { stopMic(); }
    });
    $("m-mock").addEventListener("change", (event) => {
      if (event.target.checked) { startDemo("menu toggle"); }
      else { if (mockStop) { mockStop(); mockStop = null; } state.demo = false; connect(); }
    });
    document.querySelectorAll("[data-shape]").forEach((button) => {
      button.addEventListener("click", () => {
        core.setShape(button.dataset.shape);
        coreState(button.dataset.shape, "manual topology");
      });
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((other) => other.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
        tab.classList.add("active");
        const panel = document.querySelector('[data-panel="' + tab.dataset.tab + '"]');
        if (panel) panel.classList.add("active");
      });
    });
  }

  function bindChat() {
    const submit = () => {
      const value = $("chat-input").value.trim();
      if (!value) return;
      $("chat-input").value = "";
      addTranscript("user", value);
      if (!send("chat", { text: value })) {
        notice("No core connected — running in demo mode. Start `orbynth hud` to send real instructions.", "warning", false);
      }
    };
    $("chat-send").addEventListener("click", submit);
    $("chat-input").addEventListener("keydown", (event) => { if (event.key === "Enter") submit(); });
    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        $("m-chat").checked = true;
        $("chatbar").classList.remove("hidden");
        $("chat-input").focus();
      }
      if (event.key === "Escape") $("scene").classList.add("hidden");
    });
  }

  function bindButtons() {
    $("voice-toggle").addEventListener("click", () => setVoiceMuted(!state.voiceMuted));
    const ptt = $("ptt");
    const down = () => { send("voice.ptt", { down: true }); ptt.classList.add("live"); core.setShape("sphere"); coreState("holding to talk", "release to send"); };
    const up = () => { send("voice.ptt", { down: false }); ptt.classList.remove("live"); };
    ptt.addEventListener("mousedown", down);
    ptt.addEventListener("mouseup", up);
    ptt.addEventListener("mouseleave", up);
    $("spawn-agent").addEventListener("click", () => {
      const ok = send("agent.spawn", { role: "operative", capabilities: ["filesystem.read", "filesystem.write"] });
      if (!ok) notice("No core connected — cannot spawn an agent in demo mode.", "warning", false);
    });
    $("refresh-state").addEventListener("click", refreshState);
    $("forge-btn").addEventListener("click", () => {
      const need = $("forge-need").value.trim();
      if (!need) return;
      const ok = send("plugin.forge", { need: need });
      if (!ok) notice("No core connected — the Forge needs a live Artificer.", "warning", false);
      else notice("Artificer is designing a plugin…", "info", false);
      $("forge-need").value = "";
    });
    $("memory-btn").addEventListener("click", () => {
      const query = $("memory-query").value.trim();
      if (!query) return;
      if (!send("memory.search", { query: query, k: 12 })) {
        notice("No core connected — memory search needs a live core.", "warning", false);
      }
      send("audit", { limit: 40 });
    });
    $("scene-close").addEventListener("click", () => $("scene").classList.add("hidden"));
    $("scene-pop").addEventListener("click", () => $("scene").classList.toggle("pop"));
    makeDraggable($("scene"), $("scene").querySelector(".scene-head"));
  }

  function refreshState() {
    if (!send("state.request")) {
      if (state.demo) applySnapshot(window.OrbythMock.snapshot());
    }
  }

  function makeDraggable(element, handle) {
    let offsetX = 0, offsetY = 0, dragging = false;
    handle.addEventListener("mousedown", (event) => {
      dragging = true;
      const rect = element.getBoundingClientRect();
      offsetX = event.clientX - rect.left;
      offsetY = event.clientY - rect.top;
      event.preventDefault();
    });
    document.addEventListener("mousemove", (event) => {
      if (!dragging) return;
      element.style.left = (event.clientX - offsetX) + "px";
      element.style.top = (event.clientY - offsetY) + "px";
      element.style.right = "auto";
      element.style.bottom = "auto";
    });
    document.addEventListener("mouseup", () => { dragging = false; });
  }

  /* ------------------------------------------------------------------ animation */
  let last = 0;
  function loop(now) {
    const delta = now - last;
    last = now;
    if (state.demo) {
      // keep the demo alive: the shell's energy drifts so the core never looks frozen
      core.setEnergy(0.12 + 0.1 * Math.sin(now / 2200));
    }
    core.frame(now);
    wires.frame(now);
    void delta;
    window.requestAnimationFrame(loop);
  }

  function escapeHtml(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ------------------------------------------------------------------ boot */
  let booted = false;
  function boot() {
    if (booted) return;
    booted = true;
    bindMenu();
    bindChat();
    bindButtons();
    coreState("initialising", "link established");
    if (!core.ok) {
      $("core-sub").textContent = "WebGL unavailable — running in reduced mode";
      document.querySelector('.core-label #core-state').textContent = "core offline";
    }
    window.addEventListener("resize", () => { core.resize(); wires.resize(); });
    refreshState();
    window.setTimeout(refreshState, 1500);
    connect();
    window.requestAnimationFrame(loop);
    window.setTimeout(() => core.setShape("torus"), 2600);
    window.setTimeout(() => { core.setShape("sphere"); coreState("idle", "awaiting instruction"); }, 5200);
  }

  document.addEventListener("DOMContentLoaded", boot);
  if (document.readyState !== "loading") boot();
})();
