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
    // operational state (contract 22.x): processes, outcome checks, security, updates, machine
    jobs: [],
    verdicts: [],
    preflight: [],
    security: null,
    updates: null,
    machine: null,
    // hardware monitor (16), local index (14.2) and the capability matrix (4.7)
    hardware: null,
    capabilities: null,
    search: null,
    searchHits: [],
    scene: null,
    // 3D explanation window: the renderer is driven by the main animation loop, so closing the
    // window stops it without leaving a second loop running
    sceneFrame: null,
    sceneRot: { x: -0.32, y: 0.6 },
    selectedAgent: null,
    speakReplies: true,
    micEnabled: false,
    voiceMuted: false,
    seenMessages: new Set(),
  };

  const core = new ParticleCore($("core"), { count: 6000 });
  const wires = new WireGraph($("wires"), { onSelect: selectAgent });
  // The instrument cage, waveform strips and gauge dials. Optional: without it the core and the
  // fleet wiring still work, so a browser without canvas 2D never loses the console entirely.
  const chrome = window.OrbythChrome || null;
  // The technical backdrop (mesh, rulers, scrolling buses) and the instrument widgets (radar,
  // lattice, sparklines, cell matrix, ladders, ruled scales). Both optional and both cheap:
  // without them the console still shows the core, the wiring and the panels.
  const grid = window.OrbythGrid ? new window.OrbythGrid.SystemGrid($("grid")) : null;
  const widgets = window.OrbythWidgets || null;
  // The effects director: one blended recipe — palette, spin, energy floor, backdrop mood, cage
  // mood, instrument jitter, target topology — derived from real state and real events.
  const fx = window.OrbythEffects ? new window.OrbythEffects.Director() : null;

  /* ------------------------------------------------------------------ core state mapping */
  function coreState(label, sub) {
    $("core-state").textContent = label;
    if (sub !== undefined) $("core-sub").textContent = sub;
  }

  /* Hardware alerts (spec 16) arrive from the deterministic monitor, not from an agent. They
   * are kept so the hardware panel can show a real timeline instead of the latest line only,
   * and written into the panel's status line when that panel is present. */
  const hardwareAlerts = [];
  function hardwareRow(alert) {
    if (!alert || !alert.alert_id) return;
    hardwareAlerts.push(alert);
    if (hardwareAlerts.length > 40) hardwareAlerts.shift();
    window.ORBYNTH_HARDWARE_ALERTS = hardwareAlerts;
    const node = $("hw-alert");
    if (node) {
      node.textContent = alert.severity + " · " + (alert.sensor || "") + " · " + (alert.message || "");
      node.dataset.severity = alert.severity;
    }
  }

  const TOPIC_EFFECTS = {
    "agent.spawned": (e) => { core.pulseShear(0.35); core.setShape("torus"); coreState("fleet growing", (e.payload.name || "agent") + " joined"); },
    "agent.terminated": (e) => { core.pulseShear(0.2); coreState("fleet shrinking", (e.payload.agent || "agent") + " left"); },
    "agent.message": (e) => { core.pulseShear(0.18); },
    "agent.report": (e) => {
      const status = e.payload.status || "partial";
      // A disputed report is a *failed* outcome check, not a softer kind of done. It gets the
      // shear pulse a failure gets, so it cannot be mistaken for success at a glance.
      if (status === "disputed") core.pulseShear(0.5); else core.pulseEnergy(0.4);
      coreState(status === "done" ? "task complete" : "task " + status, e.payload.summary || "");
    },
    "agent.error": () => { core.collapse(); coreState("agent error", "recovering"); },
    "agent.status": (e) => {
      // A status change used to reach the interface only through a full state push, so an
      // agent could go from running to blocked and the roster kept showing the old word.
      const row = state.agents.find((a) => a.agent_id === e.payload.agent_id || a.name === e.payload.agent);
      if (row) {
        row.status = e.payload.status || row.status;
        if (e.payload.task !== undefined) row.task = e.payload.task;
        renderAgents();
      }
      if (e.payload.status === "running") core.pulseEnergy(0.15);
    },
    "task.created": () => core.setShape("helix"),
    "task.assigned": (e) => { coreState("delegating", (e.payload.agent || "") + " ← " + (e.payload.task || "")); },
    "task.done": (e) => { core.pulseEnergy(0.45); coreState("task complete", e.payload.objective || ""); },
    "action.invoked": (e) => { core.pulseShear(0.5); core.setShape(shapeForAction(e.payload.action)); coreState("acting", e.payload.action || ""); },
    "action.result": (e) => { core.pulseEnergy(e.payload.ok ? 0.25 : 0.5); },
    "action.denied": (e) => { core.pulseShear(0.7); coreState("capability denied", (e.payload.action || "") + " needs " + (e.payload.capability || "")); },
    "action.confirm": (e) => { coreState("confirmation required", e.payload.action || ""); },
    "model.call": (e) => {
      // Every real model call nudges the core: the wires are live traffic, not decoration.
      core.pulseEnergy(0.07);
      if (e.payload.model) $("model-state").textContent = e.payload.provider ? e.payload.provider + ":" + e.payload.model : e.payload.model;
    },
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
    "voice.language": (e) => {
      core.pulseShear(0.15);
      coreState("language " + (e.payload.detected || "?"), "heard « " + (e.payload.text || "").slice(0, 60) + " »");
      renderLanguage(e.payload);
    },
    "hud.notice": (e) => notice(e.payload.text || "", e.payload.severity || "info", e.payload.speak !== false, e.payload.agent),
    // Hardware monitor (spec 16): the deterministic service reports here, not through an
    // agent. A CRITICAL alert gets the collapse, because it means work is being stopped.
    "hardware.alert": (e) => {
      const alert = (e.payload && e.payload.alert) || {};
      const severity = alert.severity || "WATCH";
      if (severity === "CRITICAL" || severity === "DANGER") core.collapse();
      else core.pulseShear(0.45);
      coreState("hardware " + severity.toLowerCase(), (alert.sensor || "") + " " + (alert.message || ""));
      hardwareRow(alert);
    },
    "hardware.cleared": (e) => {
      core.pulseEnergy(0.3);
      coreState("hardware cleared", ((e.payload && e.payload.sensor) || "") + " back within limits");
      renderHardware();
    },
    "hardware.tuned": (e) => {
      // A threshold change is a change in what the machine will warn about, so it is visible
      // rather than silent: the panel is refreshed and the change is named.
      const tuned = (e.payload && e.payload.tuned) || {};
      coreState("threshold " + (tuned.direction || "tuned"), (tuned.rule || "") + " → " + (tuned.value === undefined ? "?" : tuned.value));
      renderHardware();
    },
    "index.scan": (e) => {
      const payload = e.payload || {};
      const changed = (payload.updated || 0) + (payload.removed || 0);
      if (changed) coreState("index scan", changed + " document(s) changed");
      renderIndex();
    },
    "system.log": () => {},

    /* ---- operational layer: jobs, supervisor, preflight, verification, owner, updates ---- */
    "supervisor.started": () => coreState("supervisor online", "processes owned"),
    "supervisor.reconciled": (e) => {
      const terminated = (e.payload.terminated || []).length;
      const orphaned = (e.payload.orphaned || []).length;
      if (terminated || orphaned) {
        core.pulseShear(0.3);
        coreState("reconciled", terminated + " leftover process(es) stopped, " + orphaned + " gone");
      }
    },
    "journal.reconciled": (e) => {
      const landed = e.payload.committed || 0;
      const never = e.payload.abandoned || 0;
      const decisions = e.payload.needs_decision || 0;
      if (landed || never || decisions) {
        core.pulseShear(0.25);
        const summary =
          landed + " action(s) had already landed, " + never + " never ran, " + decisions + " need a decision";
        coreState("journal reconciled", summary);
        if (decisions) notice(summary, "warning", false);
      }
    },
    "journal.undone": (e) => {
      const reversed = e.payload.undone || 0;
      const failed = e.payload.failed || 0;
      const refused = e.payload.refused || 0;
      const summary =
        "undo " + (e.payload.task_id || "last task") + ": " + reversed + " reversed, " + failed +
        " could not be, " + refused + " refused as irreversible";
      core.pulseEnergy(reversed ? 0.4 : 0.6);
      coreState(reversed ? "undone" : "undo incomplete", summary);
      notice(summary, reversed && !failed ? "info" : "warning", false);
    },
    "supervisor.stopped": () => coreState("supervisor stopped", "helpers terminated with the app"),
    "job.started": (e) => { core.pulseEnergy(0.2); pushJob({ name: e.payload.name, pid: e.payload.pid, status: "running", kind: e.payload.kind, at: e.ts }); },
    "job.exited": (e) => { pushJob({ name: e.payload.name, status: e.payload.reason || "exited", exit_code: e.payload.exit_code, at: e.ts }); },
    "job.restarted": (e) => { core.pulseShear(0.25); pushJob({ name: e.payload.name, status: "restart #" + (e.payload.restarts || 1), at: e.ts }); },
    "job.orphaned": (e) => { core.pulseShear(0.4); pushJob({ name: e.payload.name, status: "orphaned", at: e.ts }); },
    "task.preflight": (e) => {
      pushPreflight(e.payload);
      if (e.payload.action && e.payload.action !== "run_now") {
        coreState("preflight: " + e.payload.action, (e.payload.reasons || [])[0] || "");
      }
    },
    "task.verified": (e) => { core.pulseEnergy(0.35); pushVerdict({ status: "met", summary: e.payload.summary, task: e.payload.task_id, at: e.ts }); coreState("outcome verified", e.payload.summary || "met"); },
    "task.disputed": (e) => { core.pulseShear(0.55); pushVerdict({ status: e.payload.status || "disputed", summary: e.payload.summary, task: e.payload.task_id, at: e.ts }); coreState("outcome not met", e.payload.summary || ""); },
    "owner.setup": (e) => { renderSecurity(); coreState("owner set", (e.payload.owner || "") + " · " + (e.payload.machine_kind || "personal")); },
    "owner.verified": () => renderSecurity(),
    "owner.denied": (e) => { core.pulseShear(0.5); notice("owner check denied: " + (e.payload.reason || ""), "warning", false); renderSecurity(); },
    "update.staged": (e) => { renderUpdates({ staged: e.payload.version, method: e.payload.method }); coreState("update staged", (e.payload.version || "") + " via " + (e.payload.method || "?")); },
    "update.applied": (e) => { renderUpdates({ applied: e.payload.version, method: e.payload.method }); core.pulseEnergy(0.3); coreState("models updated", e.payload.version || ""); },
    "update.blocked": (e) => { coreState("update blocked", e.payload.reason || "work in flight"); renderUpdates({ blocked: e.payload.reason }); },
    "governor.paused": (e) => { core.pulseShear(0.2); renderMachine({ paused: true, reason: (e.payload.reasons || "") }); coreState("yielding the machine", e.payload.reasons || "busy"); },
    "governor.resumed": () => { renderMachine({ paused: false }); coreState("machine free", "resuming background work"); },
    "billing.blocked": (e) => { core.pulseShear(0.45); renderUpdates({ billing: e.payload.reason }); coreState("call blocked by billing", (e.payload.mode || "") + ": " + (e.payload.reason || "")); },
  };

  /* Every topic the backend can emit must appear above. `hud/coverage.json` is generated from
   * orbynth/events.py and asserted by tests/test_hud.py, so a new subsystem cannot ship
   * invisible in the interface — which is exactly what happened to the ops layer first time. */
  window.ORBYNTH_TOPICS = Object.keys(TOPIC_EFFECTS);

  /* ------------------------------------------------------------------ chrome signal routing
   * The chrome is fed from the same events as everything else, on two planes: speech energy
   * (the audio strip) and real work (the traffic strip, the fleet gauge, the cage brackets).
   * A strip whose amplitude is invented would be a lie in the one place a console must not lie,
   * so both planes are only ever raised by an event that actually happened.
   */
  const AUDIO_TOPICS = {
    "voice.wake": 0.45, "voice.listening": 0.25, "voice.speaking": 0.5,
    "voice.transcript": 0.2, "voice.language": 0.15,
  };
  const TRAFFIC_TOPICS = {
    "action.invoked": 0.5, "action.result": 0.35, "action.denied": 0.5,
    "agent.message": 0.25, "agent.spawned": 0.3, "agent.status": 0.15,
    "job.started": 0.25, "job.exited": 0.2, "model.call": 0.15,
    "memory.write": 0.15, "memory.recall": 0.12, "plugin.installed": 0.4,
    "quota.warning": 0.4, "quota.exhausted": 0.6, "provider.error": 0.5,
    "billing.blocked": 0.45, "task.assigned": 0.3, "task.done": 0.3,
  };
  // Two more real planes, for the memory stream and the helper-lane strips: a strip is raised by
  // an actual write or an actual process, and decays when nothing happens.
  const MEMORY_TOPICS = { "memory.write": 0.4, "memory.recall": 0.3 };
  const JOB_TOPICS = { "job.started": 0.45, "job.exited": 0.35, "job.restarted": 0.55, "job.orphaned": 0.7 };

  function routeChromePulse(topic, event) {
    // The director sees every event first: it decides the situation the plate should be in and
    // fires the transient effects that event implies.
    if (fx) fx.route(topic, event || {});
    if (chrome) {
      if (AUDIO_TOPICS[topic]) chrome.pulseAudio(AUDIO_TOPICS[topic]);
      if (TRAFFIC_TOPICS[topic]) chrome.pulseTraffic(TRAFFIC_TOPICS[topic]);
      if (MEMORY_TOPICS[topic] && chrome.pulseMemory) chrome.pulseMemory(MEMORY_TOPICS[topic]);
      if (JOB_TOPICS[topic] && chrome.pulseJobs) chrome.pulseJobs(JOB_TOPICS[topic]);
    }
    if (widgets) {
      if (MEMORY_TOPICS[topic]) widgets.pulseMemory(MEMORY_TOPICS[topic]);
      if (JOB_TOPICS[topic]) widgets.pulseJobs(JOB_TOPICS[topic]);
    }
    renderWidgets();
    renderChrome();
    renderSignalLabels();
  }

  /* Hand the cage the true values currently on screen. Nothing here is estimated: every field
   * is either a value the core sent or a value the interface itself just rendered. */
  function renderChrome() {
    if (!chrome) return;
    const running = state.agents.filter((a) => /running|working|deferred/.test(String(a.status || ""))).length;
    const verdicts = state.verdicts || [];
    chrome.setState({
      link: $("link-state").textContent,
      model: $("model-state").textContent,
      agents: state.agents.length,
      agentsWorking: running,
      actions: (state.snapshot && state.snapshot.actions && state.snapshot.actions.count) || 0,
      memory: (state.snapshot && state.snapshot.memory) || {},
      quota: $("quota-state").textContent,
      jobs: (state.jobs || []).filter((job) => /running|restart/i.test(String(job.status || ""))).length,
      verdicts: {
        met: verdicts.filter((v) => /^(met|needs_user)$/.test(String(v.status || ""))).length,
        bad: verdicts.filter((v) => /not_met|disputed|uncertain|failed/.test(String(v.status || ""))).length,
      },
      working: running > 0,
    });
  }

  /* Hand the instrument widgets the same real counters the panels are showing. The channel lanes
   * are capability shares per registered action group, so a lane is lit because the system really
   * registered that work — not because a drawing routine decided to look busy. */
  function renderWidgets() {
    if (!widgets) return;
    const agents = state.agents || [];
    const snapshot = state.snapshot || {};
    const groups = (snapshot.actions && snapshot.actions.groups) || {};
    const largest = Object.keys(groups).reduce((max, name) => Math.max(max, (groups[name] || []).length), 1);
    const share = (names) => {
      let count = 0;
      names.forEach((name) => { count += (groups[name] || []).length; });
      return Math.min(1, count / largest);
    };
    const memory = snapshot.memory || {};
    const tokens = (state.quotas || []).reduce((sum, quota) => sum + (quota.total_tokens || 0), 0);
    const durations = (state.actionLog || []).map((row) => row.duration).filter((value) => typeof value === "number");
    const meanDuration = durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : 0;
    widgets.setState({
      link: $("link-state").textContent,
      model: $("model-state").textContent,
      agents: {
        total: agents.length,
        running: agents.filter((a) => /running|working|deferred/.test(String(a.status || ""))).length,
        blocked: agents.filter((a) => /blocked/.test(String(a.status || ""))).length,
        failed: agents.filter((a) => /failed|disputed/.test(String(a.status || ""))).length,
        idle: agents.filter((a) => /idle|stopped/.test(String(a.status || ""))).length,
      },
      teams: (snapshot.teams || []).length,
      actions: (snapshot.actions && snapshot.actions.count) || 0,
      memory: {
        episodes: memory.episodes || 0,
        facts: memory.facts || 0,
        tasks: memory.tasks || 0,
        audits: memory.audit || memory.audits || 0,
      },
      quota: $("quota-state").textContent,
      jobs: {
        running: (state.jobs || []).filter((job) => /running|restart/i.test(String(job.status || ""))).length,
        total: (state.jobs || []).length,
      },
      tokens: tokens,
      levels: {
        vision: share(["vision"]),
        input: share(["input"]),
        browser: share(["browser"]),
        office: share(["office"]),
        files: share(["filesystem"]),
        load: Math.min(1, agents.length / 12),
        // latency is the mean duration of the actions actually logged, mapped onto a 5 s scale
        latency: Math.min(1, meanDuration / 5),
      },
    });
  }

  /* ------------------------------------------------------------------ effects application
   * One recipe, pushed into every drawing layer. The director owns the numbers; this function owns
   * the translation into the four renderers, and nothing here invents a value.
   */
  let lastSituation = null;

  function applyEffects(now) {
    if (!fx) return;
    const view = fx.frame(now);
    if (view.situation !== lastSituation) {
      lastSituation = view.situation;
      if (view.morph && core.ok) core.setShape(view.morph);
    }
    core.setColors(view.palette.core, view.palette.dust, view.palette.warm);
    core.setEnergy(view.energy);
    core.setSpinRate(view.spin);
    core.setAurora(view.ember);
    core.setGlitch(view.glitch);
    // a yielding machine holds the globe at a lower attitude: visible deference, not a label
    core.setTiltBias(view.situation === "yielding" ? -0.22 : 0);
    if (grid) grid.setMood(view.grid);
    if (chrome && chrome.setMood) chrome.setMood(view.chrome);
    if (widgets && widgets.setMood) widgets.setMood(view.widgets);
  }

  /* The base situation is derived from state the panels are already showing, so the plate always
   * falls back to something true rather than to a guess. */
  function deriveBase() {
    if (!fx) return;
    if (state.demo || !state.live) return fx.setBase("offline");
    if (state.agents.some((a) => /blocked/.test(String(a.status || "")))) return fx.setBase("blocked");
    if ($("quota-state").textContent === "critical") return fx.setBase("pressure");
    if (state.agents.some((a) => /running|working/.test(String(a.status || "")))) return fx.setBase("acting");
    fx.setBase("idle");
  }

  /* One-shot effects the director announces are applied to the core's own machinery: a shockwave
   * is a ring, a strike is a collapse, a docking plugin is an energy pulse. */
  function bindPulses() {
    if (!fx) return;
    fx.onPulse = function (kind, amount) {
      if (!core.ok) return;
      if (kind === "shock") core.pulseRing(0.8 * amount);
      else if (kind === "flare") core.pulseEnergy(0.9 * amount);
      else if (kind === "settle") core.pulseEnergy(0.5 * amount);
      else if (kind === "dock") { core.pulseEnergy(0.6 * amount); core.pulseRing(0.7 * amount); }
      else if (kind === "tear") {
        core.collapse();
        window.setTimeout(() => { if (core.ok) core.reseed(); }, 1400);
      }
    };
  }

  function renderSignalLabels() {
    if (!chrome) return;
    const audio = chrome.state.audio;
    const traffic = chrome.state.traffic;
    const audio2 = $("sig-audio");
    const traffic2 = $("sig-traffic");
    const floor = $("floor-state");
    if (audio2) {
      audio2.textContent = audio > 0.35 ? "voice" : (audio > 0.08 ? "decay" : "idle");
      audio2.classList.toggle("warm", audio > 0.35);
    }
    if (traffic2) traffic2.textContent = traffic > 0.4 ? "busy" : (traffic > 0.08 ? "light" : "idle");
    const memoryLabel = $("sig-memory");
    if (memoryLabel) {
      const memory = chrome.state.memoryRate || 0;
      memoryLabel.textContent = memory > 0.35 ? "writing" : (memory > 0.08 ? "recall" : "idle");
    }
    const jobLabel = $("sig-jobs");
    if (jobLabel) {
      const jobs = chrome.state.jobRate || 0;
      jobLabel.textContent = jobs > 0.35 ? "spawning" : (jobs > 0.08 ? "settling" : "idle");
      jobLabel.classList.toggle("warm", jobs > 0.35);
    }
    if (floor) {
      floor.textContent = state.voiceMuted ? "muted" : (audio > 0.3 ? "active" : "listening");
      floor.classList.toggle("warm", state.voiceMuted === true);
    }
  }

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
  let demoProbe = null;
  let connectAttempts = 0;

  function connect() {
    const forcedDemo = new URLSearchParams(location.search).get("mock") === "1";
    if (forcedDemo) return startDemo("mock=1");
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = protocol + "//" + location.host + "/ws";
    // While demo data is playing, a probe must not flash "connecting…" over the demo plate.
    if (!state.demo) setLink("connecting…", "");
    try {
      socket = new WebSocket(url);
    } catch (error) {
      return startDemo("socket unavailable");
    }
    socket.addEventListener("open", () => {
      state.live = true;
      state.demo = false;
      connectAttempts = 0;
      setLink("live", "ok");
      $("m-mock").checked = false;
      if (mockStop) { mockStop(); mockStop = null; }
      if (demoProbe) { window.clearTimeout(demoProbe); demoProbe = null; }
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
      connectAttempts += 1;
      if (state.demo) {
        // Still replaying demo data: the plate stays honest and the probe comes back later, so a
        // core that starts afterwards takes over without the user touching the menu.
        setLink("demo data", "warn");
        if (demoProbe) window.clearTimeout(demoProbe);
        demoProbe = window.setTimeout(() => {
          demoProbe = null;
          if (!state.live) connect();
        }, 15000);
        return;
      }
      // A page served without a core — a static preview, or the app before `orbynth hud` starts
      // — must not sit on "offline" forever with an empty console. After two failed attempts it
      // falls back to demo data and says so on the link plate.
      if (connectAttempts >= 2) return startDemo("no core reachable at " + location.host);
      setLink("offline", "bad");
      reconnectTimer = window.setTimeout(connect, 4000);
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
    // Keep looking for a real core while the demo plays: opening it must never be a one-way trip.
    if (demoProbe) window.clearTimeout(demoProbe);
    demoProbe = window.setTimeout(() => {
      demoProbe = null;
      if (state.demo && !state.live) connect();
    }, 15000);
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
        routeChromePulse(topic, payload);
        if (socket && topic === "action.confirm") { /* card rendered by effect chain below */ }
        if (topic === "action.confirm") renderConfirmation(payload);
        if (topic === "action.invoked" || topic === "action.result" || topic === "action.denied") pushActionLog(payload);
        if (topic.startsWith("agent.")) pulseAgent(payload);
        if (topic === "agent.report" && payload.payload && payload.payload.verdict) pushVerdict(payload.payload.verdict);
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
        if (message.command === "hw.query") { state.hardware = Object.assign({}, state.hardware, { attached: true, sensors: message.result && message.result.sensors }); renderHardware(); }
        if (message.command === "index.search") {
          state.searchHits = (message.result && message.result.hits) || [];
          renderSearchHits();
          const result = message.result || {};
          coreState("search", (result.count || 0) + " hit(s) for « " + (result.query || "") + " »");
        }
        if (message.command === "index.update") {
          const result = message.result || {};
          notice("index scan: " + (result.updated || 0) + " updated, " + (result.removed || 0) + " removed, " + (result.skipped || 0) + " unchanged", "info", false);
          refreshState();
        }
        if (message.command === "index.add") { notice("index source added", "info", false); refreshState(); }
        if (message.command === "capability.matrix") { state.capabilities = message.result || state.capabilities; renderCapabilities(); }
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
    // Also exposed as a data attribute so CSS can colour it without a class tug-of-war.
    $("link-state").dataset.state = level === "ok" ? "live" : (level || "info");
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
    if (snapshot.jobs) state.jobs = snapshot.jobs;
    if (snapshot.verdicts) state.verdicts = snapshot.verdicts;
    if (snapshot.preflight) state.preflight = snapshot.preflight;
    if (snapshot.hardware) state.hardware = snapshot.hardware;
    if (snapshot.capabilities) state.capabilities = snapshot.capabilities;
    if (snapshot.search) state.search = snapshot.search;
    state.security = snapshot.security || state.security;
    state.updates = snapshot.updates || state.updates;
    state.machine = snapshot.machine || state.machine;
    applyDirection(state.machine);
    renderVitals(snapshot);
    renderAgents();
    renderQuotas();
    renderPlugins();
    renderDrivers();
    renderTranscript();
    renderJobs();
    renderVerdicts();
    renderSecurity();
    renderUpdates();
    renderMachine();
    renderHardware();
    renderIndex();
    renderCapabilities();
    wires.setAgents(state.agents);
    renderWidgets();
    deriveBase();
    renderChrome();
    renderSignalLabels();
    (snapshot.pending_confirmations || []).forEach(renderConfirmation);
  }

  /* ------------------------------------------------- operational panels (contract 22.x) */
  function pushJob(entry) {
    if (!entry || !entry.name) return;
    state.jobs = [{ ...entry }, ...state.jobs.filter((job) => job.name !== entry.name)].slice(0, 40);
    renderJobs();
    renderChrome();
  }

  function pushVerdict(entry) {
    if (!entry) return;
    state.verdicts = [{ ...entry }, ...state.verdicts].slice(0, 60);
    renderVerdicts();
    renderChrome();
  }

  function pushPreflight(entry) {
    state.preflight = [{ ...entry, at: entry.at || Date.now() / 1000 }, ...state.preflight].slice(0, 40);
    renderMachine();
  }

  function renderJobs() {
    const container = $("jobs");
    if (!container) return;
    container.innerHTML = "";
    if (!state.jobs.length) {
      container.innerHTML = '<p class="hint">No helper processes yet. Anything the system starts — the WhatsApp sidecar, an automation browser, a worker — is owned here and dies with the app.</p>';
      return;
    }
    state.jobs.forEach((job) => {
      const row = document.createElement("div");
      const bad = /orphan|gave_up|crash|error/i.test(String(job.status || ""));
      row.className = "trow" + (bad ? " warning" : "");
      row.innerHTML =
        '<span class="m" dir="auto">' + escapeHtml(job.name || "") + "</span>" +
        '<span style="color:var(--ink-dim)">' + escapeHtml(job.kind || "helper") + (job.pid ? " · pid " + job.pid : "") + "</span>" +
        '<span class="status ' + (bad ? "warning" : "") + '">' + escapeHtml(job.status || "running") + "</span>";
      container.appendChild(row);
    });
  }

  function renderVerdicts() {
    const container = $("verdicts");
    if (!container) return;
    container.innerHTML = "";
    if (!state.verdicts.length) {
      container.innerHTML = '<p class="hint">Outcome checks appear here. Receipts prove each action ran; these prove the finished work matches what you asked for — and flag work you never asked for.</p>';
      return;
    }
    state.verdicts.forEach((verdict) => {
      const node = document.createElement("div");
      const status = verdict.status || "uncertain";
      node.className = "plugin" + (status === "not_met" ? " bad" : "");
      node.innerHTML =
        '<div class="p-name" dir="auto">' + escapeHtml(status) + " <span class='chip'>" + escapeHtml(verdict.task || "task") + "</span></div>" +
        '<div class="p-meta" dir="auto">' + escapeHtml(verdict.summary || "") + "</div>" +
        (verdict.question ? '<div class="p-meta" dir="auto">needs you: ' + escapeHtml(verdict.question) + "</div>" : "");
      container.appendChild(node);
    });
  }

  function renderSecurity() {
    const container = $("security");
    if (!container) return;
    const security = state.security || {};
    const rows = [
      ["owner", security.owner || (security.configured ? "configured" : "NOT configured — run `orbynth owner setup`")],
      ["session", security.session_valid ? "open" : "locked (PIN required for irreversible work)"],
      ["machine", security.machine_kind || "personal"],
      ["OS user", security.os_user || "—"],
      ["voice enrolled", String(security.voice_prints || 0) + " sample(s) — voice never authorises irreversible work"],
      ["recovery code", security.recovery_available ? "issued (shown once at setup)" : "none"],
      ["launch mode", (state.updates && state.updates.mode) || "byok"],
    ];
    container.innerHTML = "";
    rows.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "trow" + (String(value).startsWith("NOT") ? " warning" : "");
      row.innerHTML =
        '<span class="m">' + escapeHtml(key) + "</span>" +
        '<span style="grid-column: span 4" dir="auto">' + escapeHtml(value) + "</span>";
      container.appendChild(row);
    });
  }

  function renderUpdates(partial) {
    if (partial) state.updates = { ...(state.updates || {}), ...partial };
    const container = $("updates");
    if (!container) return;
    const updates = state.updates || {};
    const rows = [
      ["launch mode", updates.mode || "byok", "who pays for model calls"],
      ["catalog version", updates.catalog_version || "none loaded", "model knowledge without a release"],
      ["staged", updates.staged ? updates.staged + " (" + (updates.method || "unverified") + ")" : "nothing staged", ""],
      ["last apply", updates.applied || "—", "only ever between actions"],
      ["drift check", updates.drift || "not run yet", "retired model IDs reported, never faked"],
      ["blocked", updates.blocked || updates.billing || "", ""],
    ];
    container.innerHTML = "";
    rows.forEach(([key, value, note]) => {
      const row = document.createElement("div");
      row.className = "trow" + (/blocked|none|not run/i.test(String(value)) && key !== "launch mode" ? " warning" : "");
      row.innerHTML =
        '<span class="m">' + escapeHtml(key) + "</span>" +
        '<span style="grid-column: span 3" dir="auto">' + escapeHtml(String(value)) + "</span>" +
        '<span style="color:var(--ink-dim)">' + escapeHtml(note || "") + "</span>";
      container.appendChild(row);
    });
  }

  /* Right-to-left interfaces are mirrored, not translated (contract 22.11). The core reports
   * the direction of the configured language; the chrome follows it so an Urdu or Arabic user
   * gets a layout that reads correctly instead of a left-anchored one with RTL text in it. */
  function applyDirection(machine) {
    if (!machine || !machine.language_dir) return;
    const dir = String(machine.language_dir).toLowerCase() === "rtl" ? "rtl" : "ltr";
    if (document.documentElement.dir !== dir) document.documentElement.dir = dir;
  }

  function renderMachine(partial) {
    if (partial) state.machine = { ...(state.machine || {}), ...partial };
    applyDirection(state.machine);
    const container = $("machine");
    if (!container) return;
    const machine = state.machine || {};
    const rows = [
      ["priority", machine.priority || "below_normal", "ORBYNTH runs under your interactive work"],
      ["background", machine.paused ? "paused — " + (machine.busy_reason || machine.reason || "machine busy") : "running", ""],
      ["session", machine.session_locked ? "locked" : "unlocked", "input is never injected into a locked desktop"],
      ["power", machine.on_ac === false ? "battery " + (machine.battery_pct || "?") + "%" : "mains", ""],
      ["languages", machine.languages || "en", "OCR packs, speech and text direction"],
    ];
    container.innerHTML = "";
    rows.forEach(([key, value, note]) => {
      const row = document.createElement("div");
      row.className = "trow" + (/paused|locked/i.test(String(value)) ? " warning" : "");
      row.innerHTML =
        '<span class="m">' + escapeHtml(key) + "</span>" +
        '<span style="grid-column: span 3" dir="auto">' + escapeHtml(String(value)) + "</span>" +
        '<span style="color:var(--ink-dim)" dir="auto">' + escapeHtml(note || "") + "</span>";
      container.appendChild(row);
    });
    if (state.preflight.length) {
      const last = state.preflight[0];
      const node = document.createElement("div");
      node.className = "plugin";
      node.innerHTML =
        '<div class="p-name">last preflight: ' + escapeHtml(last.action || "?") + "</div>" +
        '<div class="p-meta" dir="auto">' + escapeHtml((last.reasons || []).join("; ")) + "</div>" +
        (last.advice ? '<div class="p-meta" dir="auto">advice: ' + escapeHtml(last.advice) + "</div>" : "");
      container.appendChild(node);
    }
  }

  /* ---------------------------------------------------- hardware (spec 16) */
  function renderHardware() {
    const table = $("hardware");
    if (!table) return;
    const hardware = state.hardware || {};
    table.innerHTML = "";
    if (!hardware.attached) {
      table.innerHTML = '<p class="hint">' + escapeHtml(hardware.note || "the sensor monitor is not running") + "</p>";
      return;
    }
    const groups = hardware.groups || {};
    Object.keys(groups).sort().forEach((group) => {
      (groups[group] || []).forEach((reading) => {
        const row = document.createElement("div");
        const suspect = reading.suspect ? " warning" : "";
        row.className = "trow" + suspect;
        const value = reading.available === false
          ? '<span class="status warning">unavailable</span>'
          : escapeHtml(String(reading.value)) + " " + escapeHtml(reading.unit || "");
        row.innerHTML =
          '<span class="m" dir="auto">' + escapeHtml(reading.label || reading.key) + "</span>" +
          '<span class="status">' + value + "</span>" +
          '<span style="color:var(--ink-dim)" dir="auto">' +
            escapeHtml(reading.available === false ? (reading.reason || "") : (reading.source || "")) +
          "</span>";
        table.appendChild(row);
      });
    });
    const overhead = hardware.overhead || {};
    const node = $("hw-overhead");
    if (node) {
      node.textContent = overhead.within_target === null || overhead.within_target === undefined
        ? "own cost: measuring (" + (overhead.window_s || 0) + "s)"
        : "own cost " + (overhead.collector_cpu_share === null ? "?" : (overhead.collector_cpu_share * 100).toFixed(2) + "%") +
          " · last sample " + (overhead.last_sample_ms || 0) + "ms";
    }
    const alerts = $("hardware-alerts");
    if (alerts) {
      alerts.innerHTML = "";
      const active = hardware.active_alerts || [];
      if (!active.length) {
        alerts.innerHTML = '<p class="hint">No active alerts. Status: ' + escapeHtml(hardware.worst || "clear") + "</p>";
      }
      active.forEach((alert) => {
        const node2 = document.createElement("div");
        node2.className = "plugin" + (/DANGER|CRITICAL/.test(alert.severity || "") ? " bad" : "");
        node2.innerHTML =
          '<div class="p-name">' + escapeHtml(alert.severity || "") + " · " + escapeHtml(alert.rule || "") + "</div>" +
          '<div class="p-meta" dir="auto">' + escapeHtml(alert.message || "") + "</div>" +
          '<div class="p-meta" dir="auto">causes: ' + escapeHtml((alert.causes || []).join("; ") || "—") + "</div>" +
          '<div class="p-meta" dir="auto">do: ' + escapeHtml((alert.actions || []).join("; ") || "—") + "</div>";
        alerts.appendChild(node2);
      });
    }
    const gaps = $("hardware-gaps");
    if (gaps) {
      const missing = (hardware.report && hardware.report.unavailable) || [];
      const suspect = hardware.suspect || [];
      gaps.innerHTML = "";
      if (suspect.length) {
        gaps.innerHTML += '<p class="hint">Suspect readings (not used for alerts): ' +
          escapeHtml(suspect.map((s) => (s.key || "") + " — " + (s.suspect || "")).join("; ")) + "</p>";
      }
      if (missing.length) {
        gaps.innerHTML += '<p class="hint">' + missing.length + " sensor(s) unavailable on this machine: " +
          escapeHtml(missing.slice(0, 8).map((m) => m.key).join(", ")) + (missing.length > 8 ? " …" : "") + "</p>";
      }
    }
  }

  /* ------------------------------------------------- local index (spec 14.2) */
  function renderIndex() {
    const table = $("index-status");
    if (!table) return;
    const search = state.search || {};
    table.innerHTML = "";
    if (!search.attached) {
      table.innerHTML = '<p class="hint">' + escapeHtml(search.note || "the local index is not attached") + "</p>";
      return;
    }
    const caps = search.capabilities || {};
    const rows = [
      ["documents", String(search.documents || 0)],
      ["unextractable", String(search.unextractable || 0)],
      ["sources", (search.sources || []).map((s) => s.name + " (" + s.kind + ")").join(", ") || "none added yet"],
      ["search", caps.lexical ? "lexical (FTS5)" : "none"],
      ["semantic", caps.semantic ? "available" : "not in this build (no embeddings)"],
      ["ocr / pdf", (caps.ocr ? "ocr" : "no ocr") + " · " + (caps.pdf ? "pdf" : "no pdf extractor")],
      ["freshness", caps.file_watch ? "file watching" : "from scans only"],
    ];
    if (search.note) rows.push(["limits", search.note]);
    rows.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "trow" + (/none|not in|no ocr|no pdf/i.test(value) && key !== "sources" ? " warning" : "");
      row.innerHTML = '<span class="m">' + escapeHtml(key) + "</span>" +
        '<span style="grid-column: span 4" dir="auto">' + escapeHtml(value) + "</span>";
      table.appendChild(row);
    });
    renderSearchHits();
  }

  function renderSearchHits() {
    const container = $("search-results");
    if (!container) return;
    container.innerHTML = "";
    if (!state.searchHits.length) {
      container.innerHTML = '<p class="hint">No search yet.</p>';
      return;
    }
    state.searchHits.forEach((hit) => {
      const node = document.createElement("div");
      node.className = "plugin" + (hit.verified ? "" : " bad");
      node.innerHTML =
        '<div class="p-name" dir="auto">' + escapeHtml(hit.title || hit.path) + "</div>" +
        '<div class="p-meta" dir="auto">' + escapeHtml(hit.path) + " · " + escapeHtml(hit.kind || "") + "</div>" +
        (hit.snippet ? '<div class="p-meta" dir="auto">' + escapeHtml(hit.snippet) + "</div>" : "") +
        '<div class="p-meta" dir="auto">' + (hit.verified ? "verified on disk" : escapeHtml(hit.problem || "unverified")) + "</div>";
      container.appendChild(node);
    });
    renderChrome();
  }

  /* ------------------------------------------- capability matrix (spec 4.7) */
  function renderCapabilities() {
    const container = $("capabilities");
    if (!container) return;
    const matrix = state.capabilities || {};
    container.innerHTML = "";
    if (!matrix.attached) {
      container.innerHTML = '<p class="hint">The capability matrix is not attached.</p>';
      return;
    }
    const summary = matrix.summary || {};
    const chips = $("capability-summary");
    if (chips) {
      chips.innerHTML = ["tools " + (summary.tools || 0), "green " + (summary.green || 0),
        "provisional " + (summary.provisional || 0), "unavailable " + (summary.unavailable || 0),
        "drivable here " + (summary.ready_here || 0)].map((text) => '<span class="chip">' + escapeHtml(text) + "</span>").join("");
    }
    (matrix.rows || []).forEach((row) => {
      const node = document.createElement("div");
      const status = row.status || "provisional";
      node.className = "trow" + (status === "unavailable" ? " warning" : "");
      node.innerHTML =
        '<span class="m" dir="auto">' + escapeHtml(row.tool || "") + "</span>" +
        '<span class="status ' + (status === "unavailable" ? "warning" : "") + '">' + escapeHtml(status) + "</span>" +
        '<span style="color:var(--ink-dim)" dir="auto">' + escapeHtml(row.capability || "") +
          (row.runs ? " · " + row.runs + " run(s)" : " · no runs recorded") + "</span>";
      container.appendChild(node);
    });
  }

  function renderLanguage(payload) {
    renderMachine({ languages: (payload.configured || "en") + (payload.detected && payload.detected !== payload.configured ? " → " + payload.detected : "") });
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
    // dir="auto" lets the browser lay out Urdu/Arabic runs right-to-left while keeping Latin
    // text left-to-right, inside one transcript that mixes both (contract 22.11).
    node.innerHTML =
      '<div class="who">' + escapeHtml(role) + "</div>" +
      '<div class="text" dir="auto">' + escapeHtml(text || "") + "</div>" +
      (meta ? '<div class="meta" dir="auto">' + escapeHtml(meta) + "</div>" : "");
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

  /* ------------------------------------------------------------------ 3D explanation window
   * When the core wants to explain something spatial it does not have to send a picture: it
   * sends a mesh (nodes + edges), a parametric solid or a surface, and this window renders it
   * in 3D with an orbit camera — drag to rotate, wheel to zoom. The renderer is driven by the
   * main animation loop through state.sceneFrame, so closing the window stops the work at once
   * instead of leaving a second loop running behind a hidden panel.
   */
  const SOLIDS = {
    sphere: (u, v) => {
      const phi = u * Math.PI, theta = v * Math.PI * 2;
      return [Math.sin(phi) * Math.cos(theta), Math.cos(phi), Math.sin(phi) * Math.sin(theta)];
    },
    torus: (u, v) => {
      const R = 0.72, r = 0.3, a = u * Math.PI * 2, b = v * Math.PI * 2;
      return [(R + r * Math.cos(b)) * Math.cos(a), r * Math.sin(b), (R + r * Math.cos(b)) * Math.sin(a)];
    },
    saddle: (u, v) => {
      const x = (u - 0.5) * 2.2, z = (v - 0.5) * 2.2;
      return [x, (x * x - z * z) * 0.45, z];
    },
    wave: (u, v) => {
      const x = (u - 0.5) * 2.4, z = (v - 0.5) * 2.4;
      return [x, Math.sin(x * 3) * Math.cos(z * 3) * 0.45, z];
    },
    cylinder: (u, v) => {
      const a = u * Math.PI * 2;
      return [Math.cos(a) * 0.6, (v - 0.5) * 1.8, Math.sin(a) * 0.6];
    },
    helix: (u, v) => {
      const t = u, a = t * Math.PI * 6;
      const r = 0.5 + v * 0.18;
      return [Math.cos(a) * r, (t - 0.5) * 1.9, Math.sin(a) * r];
    },
  };

  function scene3D(canvas, payload, mode) {
    const ctx = canvas.getContext("2d");
    const view = { dragging: false, lastX: 0, lastY: 0, zoom: 1 };
    canvas.style.touchAction = "none";
    canvas.addEventListener("pointerdown", (event) => {
      view.dragging = true; view.lastX = event.clientX; view.lastY = event.clientY;
      if (canvas.setPointerCapture) canvas.setPointerCapture(event.pointerId);
    });
    const release = () => { view.dragging = false; };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("pointermove", (event) => {
      if (!view.dragging) return;
      state.sceneRot.y += (event.clientX - view.lastX) * 0.009;
      state.sceneRot.x = Math.max(-1.45, Math.min(1.45, state.sceneRot.x + (event.clientY - view.lastY) * 0.009));
      view.lastX = event.clientX;
      view.lastY = event.clientY;
    });
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      view.zoom = Math.max(0.5, Math.min(2.6, view.zoom * (event.deltaY > 0 ? 0.92 : 1.08)));
    }, { passive: false });

    const shape = SOLIDS[String(payload.shape || payload.solid || "sphere").toLowerCase()] || SOLIDS.sphere;
    const mesh = payload.nodes ? payload : null;
    const edges = (payload.edges || []).map((edge) => (Array.isArray(edge) ? edge : [edge.from, edge.to]));
    const highlight = new Set(payload.highlight || []);

    // normalise the mesh into the same box the solids occupy, so any scene fills the window
    let extent = 1;
    if (mesh) {
      mesh.nodes.forEach((node) => {
        extent = Math.max(extent, Math.abs(node.x || 0), Math.abs(node.y || 0), Math.abs(node.z || 0));
      });
    }

    return function frame(now) {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = canvas.clientWidth || 420;
      const height = canvas.clientHeight || 300;
      if (canvas.width !== Math.floor(width * ratio) || canvas.height !== Math.floor(height * ratio)) {
        canvas.width = Math.floor(width * ratio);
        canvas.height = Math.floor(height * ratio);
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);

      const seconds = now / 1000;
      const spin = state.sceneRot.y + (view.dragging ? 0 : seconds * 0.12);
      const cosY = Math.cos(spin), sinY = Math.sin(spin);
      const cosX = Math.cos(state.sceneRot.x), sinX = Math.sin(state.sceneRot.x);
      const scale = Math.min(width, height) * 0.34 * view.zoom;

      const project = (point) => {
        const x = point[0] * cosY - point[2] * sinY;
        const z0 = point[0] * sinY + point[2] * cosY;
        const y = point[1] * cosX - z0 * sinX;
        const z = point[1] * sinX + z0 * cosX;
        const focal = 3.4 / Math.max(0.5, 3.4 - z);
        return { x: width / 2 + x * focal * scale, y: height / 2 - y * focal * scale, z: z, f: focal };
      };
      const shade = (depth) => 0.22 + 0.62 * ((depth + 1.4) / 2.8);

      // ground grid: gives the object a scale it can be judged against
      ctx.lineWidth = 0.6;
      for (let i = -2; i <= 2; i++) {
        const t = i * 0.6;
        [[[-1.2, -0.9, t], [1.2, -0.9, t]], [[t, -0.9, -1.2], [t, -0.9, 1.2]]].forEach((segment) => {
          const a = project(segment[0]);
          const b = project(segment[1]);
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = "rgba(90,209,255," + (0.06 + 0.03 * (i === 0 ? 1 : 0)).toFixed(3) + ")";
          ctx.stroke();
        });
      }

      if (mesh) {
        const points = mesh.nodes.map((node) => project([
          (node.x || 0) / extent * 1.2, (node.y || 0) / extent * 1.2, (node.z || 0) / extent * 1.2,
        ]));
        ctx.lineWidth = 1.1;
        edges.forEach(([from, to]) => {
          const a = points[from], b = points[to];
          if (!a || !b) return;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          const depth = (a.z + b.z) / 2;
          ctx.strokeStyle = "rgba(" + (depth > 0 ? "157,123,255" : "90,209,255") + "," + shade(depth).toFixed(3) + ")";
          ctx.stroke();
        });
        points.forEach((point, index) => {
          const node = mesh.nodes[index];
          const id = String(node.id === undefined ? index : node.id);
          const warm = highlight.has(id);
          const radius = warm ? 4.2 : 2.6;
          ctx.beginPath();
          ctx.arc(point.x, point.y, radius * point.f, 0, Math.PI * 2);
          ctx.fillStyle = warm ? "rgba(255,138,60,0.95)" : "rgba(124,246,255," + shade(point.z).toFixed(3) + ")";
          ctx.fill();
          const label = node.label || node.name;
          if (label) {
            ctx.font = "10px ui-monospace, monospace";
            ctx.fillStyle = "rgba(232,244,255,0.72)";
            ctx.fillText(String(label), point.x + 7, point.y - 6);
          }
        });
      } else {
        const steps = 26;
        const colours = { near: "124,246,255", far: "70,110,190" };
        const drawLine = (a, b) => {
          const p1 = project(a);
          const p2 = project(b);
          const depth = (p1.z + p2.z) / 2;
          ctx.beginPath();
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);
          ctx.strokeStyle = "rgba(" + (depth > 0 ? colours.near : colours.far) + "," + (0.12 + shade(depth) * 0.55).toFixed(3) + ")";
          ctx.lineWidth = 0.8;
          ctx.stroke();
        };
        for (let i = 0; i <= steps; i++) {
          for (let k = 0; k < steps; k++) {
            const u = i / steps, v = k / steps, vNext = (k + 1) / steps;
            drawLine(shape(u, v), shape(u, vNext));
            drawLine(shape(u, v), shape((i + 1) / steps, v));
          }
        }
      }

      // axis triad: an explanation window should say which way it is facing
      const origin = project([0, 0, 0]);
      [[[1.35, 0, 0], "X", "90,209,255"], [[0, 1.35, 0], "Y", "74,222,128"], [[0, 0, 1.35], "Z", "255,138,60"]].forEach(([end, label, colour]) => {
        const tip = project(end);
        ctx.beginPath();
        ctx.moveTo(origin.x, origin.y);
        ctx.lineTo(tip.x, tip.y);
        ctx.strokeStyle = "rgba(" + colour + ",0.55)";
        ctx.lineWidth = 1.1;
        ctx.stroke();
        ctx.font = "9px ui-monospace, monospace";
        ctx.fillStyle = "rgba(" + colour + ",0.85)";
        ctx.fillText(label, tip.x + 4, tip.y - 4);
      });
    };
  }

  /* ------------------------------------------------------------------ scene viewer */
  function openScene(kind, title, payload) {
    state.scene = { kind: kind, title: title, payload: payload };
    $("scene-title").textContent = title;
    const body = $("scene-body");
    body.innerHTML = "";
    state.sceneFrame = null;
    // A wireframe in the window is 3D work: it needs pixels, so the window grows to suit it.
    const wants3D = kind === "mesh" || kind === "solid" || kind === "surface";
    $("scene").classList.toggle("three-d", wants3D);
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
    } else if (wants3D) {
      const canvas = document.createElement("canvas");
      canvas.className = "scene3d";
      body.appendChild(canvas);
      const caption = document.createElement("div");
      caption.className = "scene-hint";
      caption.textContent = (payload.text || "") + (payload.text ? "  ·  " : "") + "drag to orbit · wheel to zoom";
      body.appendChild(caption);
      state.sceneFrame = scene3D(canvas, payload, kind === "mesh" ? "mesh" : "solid");
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

  function closeScene() {
    // Stopping the renderer here is the whole point: a hidden window must not keep drawing.
    state.sceneFrame = null;
    $("scene").classList.add("hidden");
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
    // The two stacks are groups, not single panels: the fleet stack carries the diagnostics, the
    // roster and the telemetry matrix, so one switch has to move all of it or the frame looks broken.
    $("m-agents").addEventListener("change", (event) => {
      $("agents-panel").classList.toggle("hidden", !event.target.checked);
      document.querySelectorAll('[data-stack="fleet"]').forEach((panel) => {
        panel.classList.toggle("hidden", !event.target.checked);
      });
    });
    $("m-panels").addEventListener("change", (event) => {
      $("side-panels").classList.toggle("hidden", !event.target.checked);
      document.querySelectorAll('[data-stack="console"]').forEach((panel) => {
        panel.classList.toggle("hidden", !event.target.checked);
      });
    });
    $("m-wires").addEventListener("change", (event) => {
      $("wires").style.display = event.target.checked ? "block" : "none";
      wires.showLabels = event.target.checked;
    });
    $("m-webgl").addEventListener("change", (event) => {
      $("core").style.display = event.target.checked ? "block" : "none";
    });
    $("m-grid").addEventListener("change", (event) => {
      $("grid").style.display = event.target.checked ? "block" : "none";
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
      if (event.key === "Escape") closeScene();
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
    $("search-btn").addEventListener("click", () => {
      const query = $("search-query").value.trim();
      if (!query) return;
      if (!send("index.search", { query: query, limit: 20 })) notice("No core connected — local search needs a live core.", "warning", false);
    });
    $("index-add").addEventListener("click", () => {
      const root = $("index-root").value.trim();
      if (!root) return;
      const name = root.split(/[\\/]/).filter(Boolean).pop() || "docs";
      if (!send("index.add", { name: name, root: root, kind: "files" })) notice("No core connected — the index needs a live core.", "warning", false);
      $("index-root").value = "";
    });
    $("index-update").addEventListener("click", () => {
      if (!send("index.update", { limit: 2000 })) notice("No core connected — the index needs a live core.", "warning", false);
    });
    $("hw-refresh").addEventListener("click", () => {
      if (!send("hw.query", { window: 60 })) notice("No core connected — the sensor monitor needs a live core.", "warning", false);
    });
    $("scene-close").addEventListener("click", closeScene);
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
  let frameCount = 0;
  function loop(now) {
    const delta = now - last;
    last = now;
    // the situation drives every layer before anything is drawn, so the plate is never a frame
    // behind the event it is showing
    if (fx) applyEffects(now);
    core.frame(now);
    if (grid) grid.frame(now);
    if (widgets) widgets.frame(now);
    wires.frame(now);
    if (state.sceneFrame) state.sceneFrame(now, delta);
    if (chrome) {
      chrome.decay();
      chrome.frame(now);
      // label writes touch the DOM, so they run a few times a second, not sixty
      if (frameCount % 12 === 0) renderSignalLabels();
    }
    // the history strips are sampled from real counters a couple of times a second, and the
    // backdrop's datum plates report the frame it is actually running at
    if (widgets && frameCount % 30 === 0) widgets.sample();
    if (grid && frameCount % 12 === 0) {
      grid.setMetrics({
        fps: delta > 0 ? 1000 / delta : 0,
        particles: (core.bodyCount || 0) + (core.satelliteCount || 0),
      });
    }
    frameCount += 1;
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
    if (chrome) chrome.attach();
    if (widgets) widgets.attach();
    bindPulses();
    window.addEventListener("resize", () => {
      core.resize();
      wires.resize();
      if (chrome) chrome.resize();
      if (widgets) widgets.resize();
      if (grid) grid.resize();
    });
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
