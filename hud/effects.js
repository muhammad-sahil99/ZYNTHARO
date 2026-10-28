/* ORBYNTH effects director — the layer that makes the plate behave like different machines.

   One console, many attitudes. The director watches the same events and counters everything else
   watches, decides which *situation* the system is in, and publishes one blended recipe that every
   visual layer reads: the particle core, the cage, the backdrop mesh and the instruments.

   What it changes, per situation: colour palette (eight of them), globe rotation rate, sustained
   energy floor, breathing amplitude, the backdrop's intensity / scroll speed / alarm tint /
   interference, the cage's brightness / warmth / alert hazard ring / arc spin, instrument jitter
   and sweep speed, and the topology the core morphs into.

   On top of the sustained situation, ten *transient* effects stack and decay on their own:
   flare, shock, ember, glitch, settle, sweep, stamp, dock, tear, breathe. One-shot kinds are
   announced through ``onPulse`` so the controller can drive the core (shockwave, collapse) while
   the rest are published as scalar channels in ``view()``.

   The whole thing is driven by real system state — every topic below is a topic the core emits
   and every situation is a state it can really be in. There is no random mood generator: if
   nothing is happening, the plate sits in ``idle``.

   Cost: a handful of floats per frame and one lerp per channel. */

(function () {
  "use strict";

  /* ------------------------------------------------------------------ palettes */
  const PALETTES = {
    calm: { core: [0.62, 0.90, 1.00], dust: [0.30, 0.48, 1.00], warm: [1.00, 0.52, 0.16], cage: [90, 209, 255], grid: [90, 209, 255] },
    listen: { core: [0.55, 1.00, 1.00], dust: [0.25, 0.65, 1.00], warm: [0.45, 0.88, 1.00], cage: [124, 246, 255], grid: [90, 209, 255] },
    speak: { core: [0.84, 1.00, 1.00], dust: [0.36, 0.72, 1.00], warm: [1.00, 0.78, 0.38], cage: [155, 234, 255], grid: [124, 246, 255] },
    plan: { core: [0.62, 0.74, 1.00], dust: [0.44, 0.36, 1.00], warm: [0.74, 0.46, 1.00], cage: [157, 123, 255], grid: [129, 110, 240] },
    act: { core: [0.74, 0.94, 1.00], dust: [0.34, 0.55, 1.00], warm: [1.00, 0.55, 0.12], cage: [255, 138, 60], grid: [120, 190, 255] },
    warn: { core: [1.00, 0.82, 0.44], dust: [0.86, 0.55, 0.16], warm: [1.00, 0.45, 0.10], cage: [255, 200, 97], grid: [255, 172, 72] },
    fail: { core: [1.00, 0.46, 0.56], dust: [0.76, 0.20, 0.34], warm: [1.00, 0.30, 0.20], cage: [255, 95, 122], grid: [255, 92, 112] },
    good: { core: [0.56, 1.00, 0.82], dust: [0.20, 0.76, 0.56], warm: [0.92, 0.90, 0.42], cage: [86, 240, 155], grid: [86, 240, 155] },
  };

  /* ------------------------------------------------------------------ situations
     priority  higher wins while its hold lasts
     ttl       ms the situation holds before the derived base state takes over again (0 = derive)
     morph     topology the core moves to when the situation begins
     floor     sustained energy floor: a floor, not a fake value — it is the visual weight of work
               the system really reports as in progress
     ripple    radial ripple amplitude (speech, listening, broadcasts)
     grid      backdrop mesh: intensity, scroll speed, alarm tint, interference
     chrome    cage: brightness, warm bias, alert hazard ring, arc spin
     widgets   instrument jitter and sweep speed */
  const SITUATIONS = {
    idle: { label: "IDLE", sub: "awaiting instruction", palette: "calm", priority: 0, ttl: 0, morph: null,
      recipe: { spin: 1.0, floor: 0.05, ripple: 0.10, breath: 1.0, grid: { intensity: 0.60, speed: 1.0, alarm: 0, glitch: 0 }, chrome: { bright: 1.0, warm: 0, alert: 0, spin: 1.0 }, widgets: { jitter: 0.05, speed: 1.0 } } },
    offline: { label: "NO CORE LINK", sub: "demo data replay", palette: "warn", priority: 1, ttl: 0, morph: null,
      recipe: { spin: 0.55, floor: 0.04, ripple: 0.20, breath: 1.6, grid: { intensity: 0.45, speed: 0.5, alarm: 0.25, glitch: 0 }, chrome: { bright: 0.82, warm: 0.35, alert: 0.30, spin: 0.7 }, widgets: { jitter: 0.18, speed: 0.6 } } },
    wake: { label: "WAKE WORD", sub: "listening for you", palette: "speak", priority: 7, ttl: 2200, morph: "sphere",
      recipe: { spin: 1.6, floor: 0.30, ripple: 0.90, breath: 1.8, grid: { intensity: 0.85, speed: 1.6, alarm: 0, glitch: 0 }, chrome: { bright: 1.25, warm: 0.15, alert: 0, spin: 1.5 }, widgets: { jitter: 0.10, speed: 1.8 } } },
    listening: { label: "LISTENING", sub: "speak now", palette: "listen", priority: 3, ttl: 1500, morph: "sphere",
      recipe: { spin: 1.15, floor: 0.22, ripple: 0.75, breath: 1.4, grid: { intensity: 0.72, speed: 1.2, alarm: 0, glitch: 0 }, chrome: { bright: 1.12, warm: 0.05, alert: 0, spin: 1.2 }, widgets: { jitter: 0.08, speed: 1.4 } } },
    speaking: { label: "SPEAKING", sub: "", palette: "speak", priority: 4, ttl: 900, morph: "wave",
      recipe: { spin: 1.2, floor: 0.55, ripple: 1.0, breath: 1.6, grid: { intensity: 0.90, speed: 1.4, alarm: 0, glitch: 0 }, chrome: { bright: 1.30, warm: 0.25, alert: 0, spin: 1.3 }, widgets: { jitter: 0.14, speed: 1.6 } } },
    thinking: { label: "THINKING", sub: "reasoning", palette: "plan", priority: 2, ttl: 2500, morph: "helix",
      recipe: { spin: 2.1, floor: 0.28, ripple: 0.30, breath: 1.2, grid: { intensity: 0.78, speed: 2.2, alarm: 0, glitch: 0 }, chrome: { bright: 1.05, warm: 0.10, alert: 0, spin: 2.6 }, widgets: { jitter: 0.10, speed: 2.0 } } },
    delegating: { label: "DELEGATING", sub: "fleet at work", palette: "plan", priority: 2, ttl: 2200, morph: "torus",
      recipe: { spin: 1.7, floor: 0.30, ripple: 0.35, breath: 1.3, grid: { intensity: 0.74, speed: 1.8, alarm: 0, glitch: 0 }, chrome: { bright: 1.06, warm: 0.20, alert: 0, spin: 2.0 }, widgets: { jitter: 0.16, speed: 1.8 } } },
    acting: { label: "ACTING", sub: "tool in hand", palette: "act", priority: 3, ttl: 1400, morph: "grid",
      recipe: { spin: 1.35, floor: 0.42, ripple: 0.35, breath: 1.1, grid: { intensity: 0.86, speed: 1.7, alarm: 0, glitch: 0.04 }, chrome: { bright: 1.15, warm: 0.55, alert: 0.10, spin: 1.6 }, widgets: { jitter: 0.22, speed: 1.5 } } },
    browsing: { label: "BROWSING", sub: "inside a page", palette: "act", priority: 3, ttl: 1600, morph: "torus",
      recipe: { spin: 1.5, floor: 0.34, ripple: 0.30, breath: 1.2, grid: { intensity: 0.80, speed: 1.5, alarm: 0, glitch: 0.06 }, chrome: { bright: 1.10, warm: 0.40, alert: 0, spin: 1.8 }, widgets: { jitter: 0.16, speed: 1.4 } } },
    officing: { label: "DOCUMENT WORK", sub: "office applications", palette: "act", priority: 3, ttl: 1600, morph: "grid",
      recipe: { spin: 1.10, floor: 0.32, ripple: 0.22, breath: 1.1, grid: { intensity: 0.76, speed: 1.3, alarm: 0, glitch: 0 }, chrome: { bright: 1.08, warm: 0.60, alert: 0, spin: 1.3 }, widgets: { jitter: 0.12, speed: 1.2 } } },
    remembering: { label: "MEMORY", sub: "writing to long-term store", palette: "listen", priority: 2, ttl: 1200, morph: null,
      recipe: { spin: 0.9, floor: 0.20, ripple: 0.45, breath: 1.0, grid: { intensity: 0.70, speed: 0.9, alarm: 0, glitch: 0 }, chrome: { bright: 1.04, warm: 0, alert: 0, spin: 0.9 }, widgets: { jitter: 0.06, speed: 0.9 } } },
    extending: { label: "SELF-EXTENSION", sub: "writing its own capability", palette: "good", priority: 4, ttl: 3000, morph: "torus",
      recipe: { spin: 2.3, floor: 0.60, ripple: 0.65, breath: 1.4, grid: { intensity: 0.88, speed: 2.0, alarm: 0, glitch: 0 }, chrome: { bright: 1.22, warm: 0.45, alert: 0, spin: 2.4 }, widgets: { jitter: 0.24, speed: 1.9 } } },
    verified: { label: "OUTCOME VERIFIED", sub: "checked against the request", palette: "good", priority: 5, ttl: 2600, morph: "sphere",
      recipe: { spin: 1.0, floor: 0.34, ripple: 0.55, breath: 1.5, grid: { intensity: 0.82, speed: 1.1, alarm: 0, glitch: 0 }, chrome: { bright: 1.20, warm: 0.20, alert: 0, spin: 1.1 }, widgets: { jitter: 0.08, speed: 1.1 } } },
    blocked: { label: "BLOCKED", sub: "needs a decision", palette: "warn", priority: 6, ttl: 3200, morph: null,
      recipe: { spin: 0.5, floor: 0.24, ripple: 0.30, breath: 1.8, grid: { intensity: 0.66, speed: 0.6, alarm: 0.55, glitch: 0.05 }, chrome: { bright: 1.08, warm: 0.75, alert: 0.65, spin: 0.6 }, widgets: { jitter: 0.30, speed: 0.7 } } },
    confirming: { label: "CONFIRMATION REQUIRED", sub: "your approval gates this", palette: "warn", priority: 7, ttl: 6000, morph: null,
      recipe: { spin: 0.65, floor: 0.30, ripple: 0.40, breath: 1.7, grid: { intensity: 0.70, speed: 0.7, alarm: 0.70, glitch: 0 }, chrome: { bright: 1.15, warm: 0.90, alert: 0.85, spin: 0.8 }, widgets: { jitter: 0.26, speed: 0.8 } } },
    pressure: { label: "QUOTA PRESSURE", sub: "envelopes nearly spent", palette: "warn", priority: 6, ttl: 2600, morph: null,
      recipe: { spin: 0.85, floor: 0.30, ripple: 0.35, breath: 2.0, grid: { intensity: 0.72, speed: 0.9, alarm: 0.45, glitch: 0 }, chrome: { bright: 1.10, warm: 0.85, alert: 0.45, spin: 0.9 }, widgets: { jitter: 0.34, speed: 0.9 } } },
    failover: { label: "MODEL FAILOVER", sub: "same memory, another model", palette: "plan", priority: 6, ttl: 2600, morph: "helix",
      recipe: { spin: 2.6, floor: 0.42, ripple: 0.60, breath: 1.5, grid: { intensity: 0.86, speed: 2.6, alarm: 0.20, glitch: 0.12 }, chrome: { bright: 1.18, warm: 0.35, alert: 0.30, spin: 3.0 }, widgets: { jitter: 0.36, speed: 2.4 } } },
    error: { label: "FAULT", sub: "recovering", palette: "fail", priority: 8, ttl: 2800, morph: null,
      recipe: { spin: 0.4, floor: 0.22, ripple: 0.20, breath: 2.4, grid: { intensity: 0.60, speed: 0.5, alarm: 0.85, glitch: 0.60 }, chrome: { bright: 0.95, warm: 0.60, alert: 1.0, spin: 0.5 }, widgets: { jitter: 0.55, speed: 0.6 } } },
    disputed: { label: "OUTCOME NOT MET", sub: "the check failed", palette: "fail", priority: 8, ttl: 3600, morph: null,
      recipe: { spin: 0.6, floor: 0.26, ripple: 0.25, breath: 2.1, grid: { intensity: 0.64, speed: 0.6, alarm: 0.75, glitch: 0.35 }, chrome: { bright: 1.0, warm: 0.55, alert: 0.90, spin: 0.7 }, widgets: { jitter: 0.45, speed: 0.8 } } },
    reconciling: { label: "RECONCILING", sub: "cleaning up a previous run", palette: "warn", priority: 5, ttl: 3000, morph: "grid",
      recipe: { spin: 1.4, floor: 0.26, ripple: 0.20, breath: 1.2, grid: { intensity: 0.75, speed: 1.4, alarm: 0.30, glitch: 0.08 }, chrome: { bright: 1.05, warm: 0.50, alert: 0.25, spin: 1.6 }, widgets: { jitter: 0.18, speed: 1.3 } } },
    updating: { label: "MODELS UPDATED", sub: "applied between actions", palette: "speak", priority: 5, ttl: 3000, morph: "torus",
      recipe: { spin: 1.9, floor: 0.48, ripple: 0.70, breath: 1.4, grid: { intensity: 0.84, speed: 1.9, alarm: 0, glitch: 0 }, chrome: { bright: 1.20, warm: 0.30, alert: 0, spin: 2.0 }, widgets: { jitter: 0.20, speed: 1.7 } } },
    yielding: { label: "YIELDING THE MACHINE", sub: "you are using it", palette: "calm", priority: 5, ttl: 3000, morph: null,
      recipe: { spin: 0.45, floor: 0.08, ripple: 0.12, breath: 0.8, grid: { intensity: 0.34, speed: 0.35, alarm: 0, glitch: 0 }, chrome: { bright: 0.72, warm: 0, alert: 0, spin: 0.5 }, widgets: { jitter: 0.03, speed: 0.5 } } },
  };

  /* ------------------------------------------------------------------ transient effects
     Each is a decaying scalar channel. One-shot kinds also reach ``onPulse`` so the controller can
     drive the core's own machinery (a shockwave is a ring pulse, a fault is a collapse). */
  const EFFECTS = {
    flare: { life: 900, oneShot: true },
    shock: { life: 1300, oneShot: true },
    settle: { life: 1100, oneShot: true },
    ember: { life: 2400, oneShot: false },
    glitch: { life: 650, oneShot: false },
    tear: { life: 800, oneShot: true },
    sweep: { life: 2000, oneShot: false },
    stamp: { life: 1200, oneShot: false },
    dock: { life: 1600, oneShot: true },
    breathe: { life: 3000, oneShot: false },
  };

  /* Topic → situation. Every key is a topic the core really emits (see orbynth/events.py and
     hud/app.js), so the attitude on screen always corresponds to something that happened. */
  const TOPIC_SITUATIONS = {
    "voice.wake": "wake",
    "voice.listening": "listening",
    "voice.speaking": "speaking",
    "voice.transcript": "listening",
    "voice.language": "listening",
    "task.created": "thinking",
    "task.assigned": "delegating",
    "task.preflight": "thinking",
    "agent.spawned": "delegating",
    "agent.message": "delegating",
    "agent.report": "verified",
    "agent.error": "error",
    "agent.terminated": "idle",
    "action.invoked": "acting",
    "action.confirm": "confirming",
    "action.denied": "blocked",
    "model.call": "thinking",
    "model.fallback": "failover",
    "provider.error": "failover",
    "quota.warning": "pressure",
    "quota.exhausted": "pressure",
    "memory.write": "remembering",
    "memory.recall": "remembering",
    "plugin.authored": "extending",
    "plugin.installed": "extending",
    "plugin.failed": "error",
    "plugin.uninstalled": "idle",
    "task.verified": "verified",
    "task.disputed": "disputed",
    "supervisor.started": "idle",
    "supervisor.reconciled": "reconciling",
    "journal.reconciled": "reconciling",
    "journal.undone": "verified",
    "supervisor.stopped": "idle",
    "job.orphaned": "error",
    "job.started": "acting",
    "job.exited": "acting",
    "governor.paused": "yielding",
    "governor.resumed": "idle",
    "update.staged": "updating",
    "update.applied": "updating",
    "update.blocked": "blocked",
    "billing.blocked": "blocked",
    "owner.denied": "error",
    "owner.setup": "idle",
    "owner.verified": "idle",
  };

  /* Topic → transient effects. A value may be a list, or a function of the event when the effect
     depends on the outcome (a successful action settles, a failed one tears). */
  const TOPIC_PULSES = {
    "action.invoked": [["shock", 0.45], ["glitch", 0.12]],
    "action.result": (event) => (((event && event.payload && event.payload.ok === false)
      ? [["tear", 0.6], ["glitch", 0.35]]
      : [["settle", 0.30]])),
    "action.denied": [["tear", 0.55]],
    "action.confirm": [["stamp", 0.9]],
    "task.verified": [["settle", 0.85], ["stamp", 0.55]],
    "task.disputed": [["tear", 0.75], ["glitch", 0.5], ["ember", 0.5]],
    "plugin.installed": [["dock", 1.0], ["flare", 0.6]],
    "plugin.failed": [["tear", 0.7]],
    "voice.wake": [["flare", 1.0]],
    "voice.speaking": [["breathe", 0.5]],
    "quota.warning": [["ember", 0.45]],
    "quota.exhausted": [["ember", 0.95]],
    "provider.error": [["glitch", 0.5]],
    "supervisor.reconciled": [["sweep", 0.8]],
    "journal.reconciled": [["sweep", 0.7]],
    "journal.undone": [["stamp", 0.6]],
    "job.orphaned": [["tear", 0.4]],
    "update.applied": [["stamp", 0.85], ["flare", 0.5]],
    "governor.paused": [["breathe", 0.6]],
    "memory.write": [["breathe", 0.35]],
    "agent.spawned": [["dock", 0.5]],
    "agent.error": [["tear", 0.8], ["glitch", 0.6]],
  };

  /* ------------------------------------------------------------------ helpers */
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }

  function lerp(a, b, t) { return a + (b - a) * t; }

  function lerpList(a, b, t) {
    const length = Math.max(a.length, b.length);
    const out = [];
    for (let i = 0; i < length; i++) out.push(lerp(a[i] || 0, b[i] || 0, t));
    return out;
  }

  function ease(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }

  const NEUTRAL = {
    spin: 1, floor: 0.06, ripple: 0.15, breath: 1.2, morph: null,
    grid: { intensity: 0.6, speed: 1, alarm: 0, glitch: 0 },
    chrome: { bright: 1, warm: 0, alert: 0, spin: 1 },
    widgets: { jitter: 0.08, speed: 1 },
  };

  function recipeOf(name) {
    const entry = SITUATIONS[name] || SITUATIONS.idle;
    const recipe = entry.recipe || {};
    return {
      spin: recipe.spin === undefined ? NEUTRAL.spin : recipe.spin,
      floor: recipe.floor === undefined ? NEUTRAL.floor : recipe.floor,
      ripple: recipe.ripple === undefined ? NEUTRAL.ripple : recipe.ripple,
      breath: recipe.breath === undefined ? NEUTRAL.breath : recipe.breath,
      morph: entry.morph || null,
      grid: Object.assign({}, NEUTRAL.grid, recipe.grid),
      chrome: Object.assign({}, NEUTRAL.chrome, recipe.chrome),
      widgets: Object.assign({}, NEUTRAL.widgets, recipe.widgets),
    };
  }

  function blendRecipe(a, b, t) {
    return {
      spin: lerp(a.spin, b.spin, t),
      floor: lerp(a.floor, b.floor, t),
      ripple: lerp(a.ripple, b.ripple, t),
      breath: lerp(a.breath, b.breath, t),
      morph: t >= 0.5 ? b.morph : a.morph,
      grid: {
        intensity: lerp(a.grid.intensity, b.grid.intensity, t),
        speed: lerp(a.grid.speed, b.grid.speed, t),
        alarm: lerp(a.grid.alarm, b.grid.alarm, t),
        glitch: lerp(a.grid.glitch, b.grid.glitch, t),
      },
      chrome: {
        bright: lerp(a.chrome.bright, b.chrome.bright, t),
        warm: lerp(a.chrome.warm, b.chrome.warm, t),
        alert: lerp(a.chrome.alert, b.chrome.alert, t),
        spin: lerp(a.chrome.spin, b.chrome.spin, t),
      },
      widgets: {
        jitter: lerp(a.widgets.jitter, b.widgets.jitter, t),
        speed: lerp(a.widgets.speed, b.widgets.speed, t),
      },
    };
  }

  class Director {
    constructor() {
      this.base = "idle";          // derived from system state by the controller
      this.current = "idle";       // the situation actually on screen
      this.heldName = null;        // event-driven situation and how long it holds
      this.heldUntil = 0;
      this.fromName = "idle";      // palette/recipe transition
      this.toName = "idle";
      this.blendMs = 700;
      this.blendStart = 0;
      this.energy = 0;
      this.transients = new Map();
      this.onPulse = null;
      this.viewCache = null;
      this.viewStamp = -1;
      this.changedAt = 0;
    }

    /** The situation derived from system state — what the plate falls back to. */
    setBase(name) {
      if (SITUATIONS[name]) this.base = name;
    }

    /** An event put the system into a situation. Higher priority wins while a hold lasts. */
    setSituation(name, holdMs) {
      if (!SITUATIONS[name]) return false;
      const entry = SITUATIONS[name];
      const hold = holdMs === undefined ? entry.ttl : holdMs;
      if (hold <= 0) { this.setBase(name); return true; }
      const now = performance.now();
      const active = this.heldName && now < this.heldUntil ? SITUATIONS[this.heldName] : null;
      if (active && active.priority > entry.priority) return false;
      this.heldName = name;
      this.heldUntil = now + hold;
      return true;
    }

    /** Fire a transient effect. One-shot kinds also notify the controller. */
    pulse(kind, amount) {
      const spec = EFFECTS[kind];
      if (!spec) return false;
      const strength = clamp(amount === undefined ? 0.6 : amount, 0, 1.5);
      const now = performance.now();
      const existing = this.transients.get(kind);
      this.transients.set(kind, {
        born: now,
        life: spec.life,
        amount: existing ? Math.max(existing.amount, strength) : strength,
      });
      if (spec.oneShot && typeof this.onPulse === "function") this.onPulse(kind, strength);
      return true;
    }

    /** Route one core event: set the situation and fire whatever it implies. */
    route(topic, event) {
      let handled = false;
      const situation = TOPIC_SITUATIONS[topic];
      if (situation) handled = this.setSituation(situation) || handled;
      const pulseSpec = TOPIC_PULSES[topic];
      if (pulseSpec) {
        const list = typeof pulseSpec === "function" ? pulseSpec(event || {}) : pulseSpec;
        list.forEach(([kind, amount]) => { handled = this.pulse(kind, amount) || handled; });
      }
      return handled;
    }

    frame(now) {
      const held = this.heldName && now < this.heldUntil ? this.heldName : null;
      if (!held) this.heldName = null;
      const next = held || this.base;
      if (next !== this.toName) {
        this.fromName = this.current;
        this.toName = next;
        this.blendStart = now;
        this.changedAt = now;
      }
      this.current = next;

      // decay the transient channels; a channel at zero is dropped, so the map stays tiny
      this.transients.forEach((entry, kind) => {
        if (now - entry.born > entry.life) this.transients.delete(kind);
      });

      const progress = this.blendMs <= 0 ? 1 : clamp((now - this.blendStart) / this.blendMs, 0, 1);
      const eased = ease(progress);
      const recipe = blendRecipe(recipeOf(this.fromName), recipeOf(this.toName), eased);
      const palette = this._palette(eased);

      // the sustained floor plus whatever the transients are adding right now
      let energy = recipe.floor;
      const mods = {};
      this.transients.forEach((entry, kind) => {
        const age = (now - entry.born) / entry.life;
        // one-shot effects decay fast at first (a strike), sustained ones fade linearly
        const fall = EFFECTS[kind].oneShot ? Math.pow(1 - age, 1.6) : 1 - age;
        mods[kind] = entry.amount * fall;
      });
      energy += (mods.flare || 0) * 0.6 + (mods.shock || 0) * 0.35 + (mods.dock || 0) * 0.4 + (mods.breathe || 0) * 0.12;
      recipe.floor = clamp(energy, 0, 1);

      // channels that only exist while a transient is alive are read by the visual layers
      const grid = Object.assign({}, recipe.grid, {
        glitch: clamp(recipe.grid.glitch + (mods.glitch || 0) * 0.7, 0, 1),
      });
      const chrome = Object.assign({}, recipe.chrome, {
        bright: clamp(recipe.chrome.bright + (mods.flare || 0) * 0.35 + (mods.stamp || 0) * 0.2, 0.4, 2),
        alert: clamp(recipe.chrome.alert + (mods.tear || 0) * 0.8, 0, 1.2),
        warm: clamp(recipe.chrome.warm + (mods.ember || 0) * 0.7, 0, 1.4),
      });
      const widgets = Object.assign({}, recipe.widgets, {
        jitter: clamp(recipe.widgets.jitter + (mods.tear || 0) * 0.4 + (mods.glitch || 0) * 0.3, 0, 1.5),
      });

      this.viewCache = {
        situation: next,
        label: (SITUATIONS[next] || SITUATIONS.idle).label,
        sub: (SITUATIONS[next] || SITUATIONS.idle).sub,
        priority: (SITUATIONS[next] || SITUATIONS.idle).priority,
        palette: palette,
        recipe: recipe,
        grid: grid,
        chrome: chrome,
        widgets: widgets,
        energy: recipe.floor,
        ripple: clamp(recipe.ripple + (mods.flare || 0) * 0.3 + (mods.breathe || 0) * 0.2, 0, 1.4),
        spin: recipe.spin * (1 + (mods.shock || 0) * 0.5),
        breath: recipe.breath,
        tear: mods.tear || 0,
        ember: clamp((mods.ember || 0) + recipe.grid.alarm * 0.25, 0, 1.2),
        sweep: mods.sweep || 0,
        stamp: mods.stamp || 0,
        dock: mods.dock || 0,
        glitch: grid.glitch,
        morph: recipe.morph,
        changed: now - this.changedAt < 40,
      };
      this.viewStamp = now;
      return this.viewCache;
    }

    view() { return this.viewCache; }

    _palette(t) {
      const from = PALETTES[(SITUATIONS[this.fromName] || SITUATIONS.idle).palette] || PALETTES.calm;
      const to = PALETTES[(SITUATIONS[this.toName] || SITUATIONS.idle).palette] || PALETTES.calm;
      if (t >= 1) return to;
      return {
        core: lerpList(from.core, to.core, t),
        dust: lerpList(from.dust, to.dust, t),
        warm: lerpList(from.warm, to.warm, t),
        cage: lerpList(from.cage, to.cage, t),
        grid: lerpList(from.grid, to.grid, t),
      };
    }
  }

  window.OrbythEffects = {
    Director, SITUATIONS, EFFECTS, PALETTES, TOPIC_SITUATIONS, TOPIC_PULSES, recipeOf,
  };
})();
