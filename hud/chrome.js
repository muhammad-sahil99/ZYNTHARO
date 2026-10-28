/* ORBYNTH instrument chrome — canvas 2D.
 *
 * The frame around the core, drawn as an instrument rather than as decoration:
 *
 *   cage        a rotating tick ring with degree graduations, datum rings and segmented arcs
 *   rails       calibration rails with hash marks running off each axis of the core
 *   brackets    target brackets that tighten while the system is working
 *   telemetry   a readout column carrying real values handed in by the controller — never
 *               invented numbers, because a console that lies is worse than a console that
 *               shows less
 *   waves       audio and traffic waveform strips (speech energy, action rate)
 *   gauges      circular dials for fleet load, memory and quota pressure
 *
 * Everything is measured from the *current viewport*, using the same projection constants the
 * particle core uses, so the cage stays locked to the globe at any window size. Cost is one
 * canvas of strokes and arcs, redrawn per frame: no filters on large surfaces, no shadows.
 */

(function () {
  "use strict";

  // Must match hud/particle-core.js: the cage is drawn around a known globe geometry, not
  // around a guess.
  const CAM_Z = 2.68;
  const FOCAL = 1.34;
  const CORE_Y = 0.52;      // vertical centre of the core, as a fraction of height
  const GLOBE = FOCAL / CAM_Z;   // globe radius in NDC units

  const CYAN = [90, 209, 255];
  const CYAN_SOFT = [124, 246, 255];
  const WARM = [255, 138, 60];
  const VIOLET = [157, 123, 255];

  function rgba(rgb, alpha) {
    return "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + Math.max(0, Math.min(1, alpha)).toFixed(3) + ")";
  }

  function dash(ctx, pattern) {
    if (typeof ctx.setLineDash === "function") ctx.setLineDash(pattern);
  }

  /* ------------------------------------------------------------------ shared state */
  const state = {
    link: "connecting…",
    model: "—",
    agents: 0,
    agentsWorking: 0,
    actions: 0,
    memory: { episodes: 0, facts: 0, tasks: 0 },
    quota: "ok",
    jobs: 0,
    verdicts: { met: 0, bad: 0 },
    // energy is real: it is raised by speech and by tool use, and decays when nothing happens
    audio: 0,
    traffic: 0,
    memoryRate: 0,
    jobRate: 0,
    // set every frame by the effects director: brightness, warm bias, alert level, arc spin
    mood: { bright: 1, warm: 0, alert: 0, spin: 1 },
    working: false,
    speaking: false,
    listening: false,
  };

  function setState(partial) {
    if (!partial) return;
    Object.keys(partial).forEach((key) => {
      if (key === "memory" && typeof partial.memory === "object") {
        state.memory = Object.assign({}, state.memory, partial.memory);
      } else if (key === "verdicts" && typeof partial.verdicts === "object") {
        state.verdicts = Object.assign({}, state.verdicts, partial.verdicts);
      } else {
        state[key] = partial[key];
      }
    });
  }

  /* ------------------------------------------------------------------ the cage */
  class Instrument {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.t0 = performance.now();
      this.brackets = 0;   // smoothed "working" pulse
      this.energy = 0;
      this.resize();
    }

    resize() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = this.canvas.clientWidth || window.innerWidth;
      const height = this.canvas.clientHeight || window.innerHeight;
      this.canvas.width = Math.floor(width * ratio);
      this.canvas.height = Math.floor(height * ratio);
      this.width = width;
      this.height = height;
      this.ratio = ratio;
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      // globe radius in CSS pixels, from the projection the particle core uses
      this.radius = GLOBE * height * 0.5;
      this.centre = { x: width / 2, y: height * CORE_Y };
    }

    frame(now) {
      const ctx = this.ctx;
      const seconds = (now - this.t0) / 1000;
      const cx = this.centre.x;
      const cy = this.centre.y;
      const R = this.radius;
      // the cage brightens with real activity only: speech energy and tool traffic
      const energy = Math.min(1, state.audio * 0.75 + state.traffic * 0.5);
      const mood = state.mood || { bright: 1, warm: 0, alert: 0, spin: 1 };
      this.brackets += ((state.working ? 1 : 0) - this.brackets) * 0.05;

      ctx.clearRect(0, 0, this.width, this.height);

      // ---- ambient halo, the light the instrument casts on its own housing
      const halo = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 2.1);
      halo.addColorStop(0, rgba(CYAN_SOFT, Math.min(1, (0.10 + energy * 0.06) * mood.bright)));
      halo.addColorStop(0.45, rgba(CYAN, 0.05 + energy * 0.03));
      halo.addColorStop(1, rgba(CYAN, 0));
      ctx.fillStyle = halo;
      ctx.fillRect(cx - R * 2.2, cy - R * 2.2, R * 4.4, R * 4.4);

      // ---- outer datum ring: dashed, counter-rotating, the frame of the instrument
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(-seconds * 0.03);
      dash(ctx, [2, 7]);
      ctx.strokeStyle = rgba(CYAN, 0.22);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(0, 0, R * 1.86, 0, Math.PI * 2);
      ctx.stroke();

      // ---- tick cage: graduations with long marks every fifth and degree plates every 45°
      dash(ctx, []);
      const ticks = 180;
      for (let i = 0; i < ticks; i++) {
        const angle = (i / ticks) * Math.PI * 2;
        const major = i % 5 === 0;
        const inner = R * 1.62;
        const outer = inner + (major ? R * 0.075 : R * 0.032);
        ctx.beginPath();
        ctx.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
        ctx.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
        ctx.strokeStyle = rgba(CYAN, major ? 0.34 : 0.16);
        ctx.lineWidth = major ? 1.2 : 0.8;
        ctx.stroke();
      }
      dash(ctx, [1, 5]);
      ctx.strokeStyle = rgba(CYAN, 0.28);
      ctx.beginPath();
      ctx.arc(0, 0, R * 1.62, 0, Math.PI * 2);
      ctx.stroke();

      // ---- degree plates: numerals on the cage, so it reads as a graduated instrument
      dash(ctx, []);
      ctx.font = "7px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = rgba(CYAN, 0.42);
      for (let degree = 0; degree < 360; degree += 15) {
        const angle = (degree * Math.PI) / 180;
        const plate = R * 1.78;
        ctx.fillText(String(degree).padStart(3, "0"), Math.cos(angle) * plate, Math.sin(angle) * plate);
      }

      // ---- a second datum ring between the cage and the globe: two rings read as a housing
      dash(ctx, [1, 3]);
      ctx.strokeStyle = rgba(CYAN, 0.16);
      ctx.beginPath();
      ctx.arc(0, 0, R * 1.34, 0, Math.PI * 2);
      ctx.stroke();
      dash(ctx, []);

      // ---- segmented arcs, rotating at different rates; two run warm, as in the reference
      dash(ctx, []);
      const arcs = [
        { r: 1.28, from: 0.6, to: 2.2, speed: -0.11, color: WARM, width: 1.4 },
        { r: 1.44, from: 0.15, to: 1.05, speed: 0.09, color: CYAN_SOFT, width: 2.2 },
        { r: 1.52, from: 2.1, to: 3.4, speed: -0.06, color: WARM, width: 1.6 },
        { r: 1.72, from: 3.6, to: 5.0, speed: 0.05, color: CYAN, width: 1.2 },
        { r: 1.88, from: 5.2, to: 6.1, speed: 0.075, color: WARM, width: 1.0 },
        { r: 2.02, from: 1.2, to: 2.4, speed: -0.035, color: VIOLET, width: 1.0 },
      ];
      arcs.forEach((arc, index) => {
        const spin = seconds * arc.speed * mood.spin + index;
        ctx.beginPath();
        ctx.arc(0, 0, R * arc.r, arc.from + spin, arc.to + spin);
        ctx.strokeStyle = rgba(arc.color, Math.min(1, 0.42 + energy * 0.3 + (arc.color === WARM ? mood.warm * 0.35 : 0)));
        ctx.lineWidth = arc.width;
        ctx.stroke();
        // a bead on each arc end: the arc reads as a moving carriage, not a static stroke
        [arc.from, arc.to].forEach((end) => {
          const angle = end + spin;
          ctx.beginPath();
          ctx.arc(Math.cos(angle) * R * arc.r, Math.sin(angle) * R * arc.r, 2.4, 0, Math.PI * 2);
          ctx.fillStyle = rgba(arc.color, 0.8);
          ctx.fill();
        });
      });

      // ---- alert state: a hazard ring that breathes while something needs attention. This is the
      // cage's half of the alarm; the backdrop carries the other half.
      if (mood.alert > 0.03) {
        const beat = 0.5 + 0.5 * Math.sin(seconds * 4.2);
        dash(ctx, [6, 10]);
        ctx.beginPath();
        ctx.arc(0, 0, R * 2.16, 0, Math.PI * 2);
        ctx.strokeStyle = rgba([255, 120, 60], Math.min(1, 0.16 + 0.42 * mood.alert * beat));
        ctx.lineWidth = 1.6;
        ctx.stroke();
        dash(ctx, []);
        // radial spikes, so the ring reads as a hazard marking rather than a decoration
        for (let i = 0; i < 8; i++) {
          const angle = (i / 8) * Math.PI * 2 + seconds * 0.2;
          ctx.beginPath();
          ctx.moveTo(Math.cos(angle) * R * 2.16, Math.sin(angle) * R * 2.16);
          ctx.lineTo(Math.cos(angle) * R * 2.26, Math.sin(angle) * R * 2.26);
          ctx.strokeStyle = rgba([255, 140, 70], 0.2 + 0.4 * mood.alert * beat);
          ctx.stroke();
        }
      }

      // ---- target brackets: tight while the system is working
      const reach = R * (1.06 + 0.05 * (1 - this.brackets));
      const arm = R * 0.14;
      ctx.strokeStyle = rgba(CYAN_SOFT, 0.36 + this.brackets * 0.4);
      ctx.lineWidth = 1.4;
      [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([sx, sy]) => {
        const x = sx * reach * 1.32;
        const y = sy * reach * 1.32;
        ctx.beginPath();
        ctx.moveTo(x, y - sy * arm);
        ctx.lineTo(x, y);
        ctx.lineTo(x - sx * arm, y);
        ctx.stroke();
      });

      // ---- datum ring just outside the globe: the equator of the instrument
      dash(ctx, [1, 4]);
      ctx.strokeStyle = rgba(CYAN, 0.20 + energy * 0.15);
      ctx.beginPath();
      ctx.arc(0, 0, R * 1.16, 0, Math.PI * 2);
      ctx.stroke();
      dash(ctx, []);

      // ---- calibration rails: hairlines off each axis, with hash marks and an end node
      const rails = [
        { dx: 0, dy: -1, length: R * 2.42 },
        { dx: 0, dy: 1, length: R * 1.95 },
        { dx: -1, dy: 0, length: R * 2.1 },
        { dx: 1, dy: 0, length: R * 2.1 },
      ];
      rails.forEach((rail) => {
        ctx.strokeStyle = rgba(CYAN, 0.14);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(rail.dx * R * 1.18, rail.dy * R * 1.18);
        ctx.lineTo(rail.dx * rail.length, rail.dy * rail.length);
        ctx.stroke();
        // hash marks: a graduated rule, so the rail reads as measurement, not as a spoke
        for (let step = 0; step < 14; step++) {
          const t = R * 1.24 + (step / 14) * (rail.length - R * 1.24);
          const size = step % 5 === 0 ? 4.5 : 2.2;
          ctx.beginPath();
          ctx.moveTo(rail.dx * t - rail.dy * size, rail.dy * t - rail.dx * size);
          ctx.lineTo(rail.dx * t + rail.dy * size, rail.dy * t + rail.dx * size);
          ctx.strokeStyle = rgba(CYAN, step % 5 === 0 ? 0.32 : 0.16);
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.arc(rail.dx * rail.length, rail.dy * rail.length, 2.6, 0, Math.PI * 2);
        ctx.fillStyle = rgba(CYAN_SOFT, 0.6);
        ctx.fill();
      });
      ctx.restore();

      // ---- telemetry: real values only, arranged in two columns beside the rails
      const rows = [
        ["LINK", state.link],
        ["MODEL", String(state.model).split(":").slice(-1)[0].slice(0, 22)],
        ["AGENTS", state.agentsWorking + " / " + state.agents],
        ["ACTIONS", String(state.actions)],
        ["QUOTA", state.quota],
        ["JOBS", String(state.jobs)],
        ["MEMORY", state.memory.episodes + " ep · " + state.memory.facts + " facts"],
        ["CHECKS", state.verdicts.met + " met · " + state.verdicts.bad + " failed"],
      ];
      // The readout column lives in the gap between the side panels. Clamping it there — and
      // giving up rather than drawing when the gap is too narrow — is what keeps telemetry from
      // scribbling over the fleet panel on a smaller window.
      const railY = cy - R * 1.75;
      const left = Math.max(316, cx - R * 2.55);
      const right = Math.min(this.width - 400, cx + R * 2.2);
      if (right - left > 120) {
        ctx.font = "9px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.textBaseline = "middle";
        rows.forEach((row, index) => {
          const y = railY + index * 13;
          const onRight = index % 2 === 1;
          const x = onRight ? right : left;
          if (onRight) {
            const valueWidth = ctx.measureText(row[1]).width;
            ctx.textAlign = "right";
            ctx.fillStyle = rgba(CYAN_SOFT, 0.78);
            ctx.fillText(row[1], x, y);
            ctx.fillStyle = rgba(CYAN, 0.42);
            ctx.fillText(row[0], x - valueWidth - 10, y);
          } else {
            ctx.textAlign = "left";
            ctx.fillStyle = rgba(CYAN, 0.42);
            ctx.fillText(row[0], x, y);
            ctx.fillStyle = rgba(CYAN_SOFT, 0.78);
            ctx.fillText(row[1], x + ctx.measureText(row[0] + "   ").width, y);
          }
        });
      }

      // ---- a slow sweep line, so the console never looks frozen
      const sweep = (Math.sin(seconds * 0.35) + 1) / 2;
      const sweepY = cy + R * 1.5 + sweep * 4;
      ctx.beginPath();
      ctx.moveTo(cx - R * 1.3, sweepY);
      ctx.lineTo(cx + R * 1.3, sweepY);
      ctx.strokeStyle = rgba(CYAN_SOFT, 0.18 + 0.1 * Math.sin(seconds * 2.2));
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }

  /* ------------------------------------------------------------------ waveform strips */
  class WaveStrip {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.source = canvas.dataset.wave || "traffic";
      this.bars = 72;
      this.values = new Array(this.bars).fill(0);
      this.resize();
    }

    resize() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = this.canvas.clientWidth || 220;
      const height = this.canvas.clientHeight || 26;
      this.canvas.width = Math.floor(width * ratio);
      this.canvas.height = Math.floor(height * ratio);
      this.width = width;
      this.height = height;
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    frame(now) {
      const ctx = this.ctx;
      const seconds = now / 1000;
      // the level is real: speech energy for the audio plane, action rate for traffic, and the
      // memory / process planes for the strips that show writes and helper lanes
      const plane = {
        audio: state.audio, traffic: state.traffic, memory: state.memoryRate, jobs: state.jobRate,
      }[this.source];
      const level = Math.max(0, Math.min(1, plane === undefined ? 0 : plane));
      const bars = this.bars;
      const width = this.width;
      const height = this.height;
      const mid = height / 2;

      ctx.clearRect(0, 0, width, height);

      // baseline
      ctx.strokeStyle = rgba(CYAN, 0.18);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, mid);
      ctx.lineTo(width, mid);
      ctx.stroke();

      const step = width / bars;
      for (let i = 0; i < bars; i++) {
        // each bar is the level shaped by a travelling wave packet, so the strip reads as a
        // live signal rather than as a volume meter
        const phase = i / bars;
        const motion = 0.5 + 0.5 * Math.sin(seconds * 5.5 - phase * 9.0);
        const envelope = 0.35 + 0.65 * Math.sin(Math.PI * phase);
        const target = level * motion * envelope;
        this.values[i] += (target - this.values[i]) * 0.35;
        const amplitude = this.values[i] * (height * 0.45);
        if (amplitude < 0.4) continue;
        const x = i * step + step * 0.5;
        const warm = this.values[i] > 0.72;
        ctx.beginPath();
        ctx.moveTo(x, mid - amplitude);
        ctx.lineTo(x, mid + amplitude);
        ctx.strokeStyle = warm ? rgba(WARM, 0.85) : rgba(CYAN_SOFT, 0.35 + this.values[i] * 0.55);
        ctx.lineWidth = Math.max(1, step * 0.42);
        ctx.stroke();
      }
    }
  }

  /* ------------------------------------------------------------------ gauge dials */
  class Gauge {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.label = canvas.dataset.label || "";
      this.source = canvas.dataset.gauge || "fleet";
      this.value = 0;
      this.display = 0;
      this.resize();
    }

    resize() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = this.canvas.clientWidth || 78;
      const height = this.canvas.clientHeight || 78;
      this.canvas.width = Math.floor(width * ratio);
      this.canvas.height = Math.floor(height * ratio);
      this.width = width;
      this.height = height;
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    read() {
      if (this.source === "fleet") {
        return state.agents ? state.agentsWorking / state.agents : 0;
      }
      if (this.source === "memory") {
        const total = state.memory.episodes + state.memory.facts + state.memory.tasks;
        if (!total) return 0;
        return Math.min(1, state.memory.facts / Math.max(1, state.memory.episodes));
      }
      if (this.source === "quota") {
        return state.quota === "critical" ? 1 : (state.quota === "pressure" ? 0.72 : 0.28);
      }
      return 0;
    }

    frame(now) {
      const ctx = this.ctx;
      const target = Math.max(0, Math.min(1, this.read()));
      this.display += (target - this.display) * 0.12;
      const seconds = now / 1000;
      const cx = this.width / 2;
      const cy = this.height / 2 + 2;
      const radius = Math.min(this.width, this.height) * 0.38;
      const start = Math.PI * 0.75;
      const sweep = Math.PI * 1.5;
      const value = this.display;

      ctx.clearRect(0, 0, this.width, this.height);

      // graduations
      for (let i = 0; i <= 20; i++) {
        const angle = start + (i / 20) * sweep;
        const major = i % 5 === 0;
        const inner = radius * 0.82;
        const outer = radius * (major ? 1.06 : 0.96);
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(angle) * inner, cy + Math.sin(angle) * inner);
        ctx.lineTo(cx + Math.cos(angle) * outer, cy + Math.sin(angle) * outer);
        ctx.strokeStyle = rgba(CYAN, major ? 0.4 : 0.18);
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      // track
      ctx.beginPath();
      ctx.arc(cx, cy, radius, start, start + sweep);
      ctx.strokeStyle = rgba(CYAN, 0.14);
      ctx.lineWidth = 3;
      ctx.stroke();
      // value arc, warm when loaded
      const colour = value > 0.85 ? WARM : (value > 0.6 ? [255, 209, 102] : CYAN_SOFT);
      ctx.beginPath();
      ctx.arc(cx, cy, radius, start, start + sweep * value);
      ctx.strokeStyle = rgba(colour, 0.85);
      ctx.lineWidth = 3;
      ctx.stroke();
      // needle
      const angle = start + sweep * value;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(angle) * radius * 0.78, cy + Math.sin(angle) * radius * 0.78);
      ctx.strokeStyle = rgba(CYAN_SOFT, 0.9);
      ctx.lineWidth = 1.3;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(cx, cy, 1.8, 0, Math.PI * 2);
      ctx.fillStyle = rgba(CYAN_SOFT, 0.9);
      ctx.fill();

      // readout: the number is the value, the label says what it measures
      ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = rgba(CYAN_SOFT, 0.92);
      ctx.fillText(Math.round(value * 100) + "%", cx, cy + radius * 1.34);
      if (this.label) {
        ctx.font = "8px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.fillStyle = rgba(CYAN, 0.5);
        ctx.fillText(this.label.toUpperCase(), cx, cy - radius * 1.25);
      }
      // a slow orbiting pip: the dial is live even when its value is not moving
      const pip = seconds * 0.8;
      ctx.beginPath();
      ctx.arc(cx + Math.cos(pip) * radius * 1.2, cy + Math.sin(pip) * radius * 1.2, 1.4, 0, Math.PI * 2);
      ctx.fillStyle = rgba(CYAN, 0.5);
      ctx.fill();
    }
  }

  /* ------------------------------------------------------------------ assembly */
  const instrument = { canvas: null, strips: [], gauges: [] };

  function attach() {
    const canvas = document.getElementById("chrome");
    if (canvas) instrument.canvas = new Instrument(canvas);
    instrument.strips = [];
    instrument.gauges = [];
    document.querySelectorAll("canvas[data-wave]").forEach((node) => {
      instrument.strips.push(new WaveStrip(node));
    });
    document.querySelectorAll("canvas[data-gauge]").forEach((node) => {
      instrument.gauges.push(new Gauge(node));
    });
    return instrument;
  }

  function resize() {
    if (instrument.canvas) instrument.canvas.resize();
    instrument.strips.forEach((strip) => strip.resize());
    instrument.gauges.forEach((gauge) => gauge.resize());
  }

  function frame(now) {
    if (instrument.canvas) instrument.canvas.frame(now);
    instrument.strips.forEach((strip) => strip.frame(now));
    instrument.gauges.forEach((gauge) => gauge.frame(now));
  }

  /** Raise the audio plane because the core is actually speaking or listening. */
  function pulseAudio(amount) {
    state.audio = Math.min(1, state.audio + (amount === undefined ? 0.55 : amount));
  }

  /** Raise the traffic plane because real work moved: actions, jobs, messages. */
  function pulseTraffic(amount) {
    state.traffic = Math.min(1, state.traffic + (amount === undefined ? 0.4 : amount));
  }

  /** Raise the memory plane because a fact was really written or recalled. */
  function pulseMemory(amount) {
    state.memoryRate = Math.min(1, state.memoryRate + (amount === undefined ? 0.35 : amount));
  }

  /** Raise the process plane because a helper really started or exited. */
  function pulseJobs(amount) {
    state.jobRate = Math.min(1, state.jobRate + (amount === undefined ? 0.3 : amount));
  }

  /** The effects director's cage channel: brightness, warmth, alert level and arc speed. */
  function setMood(partial) {
    if (partial) state.mood = Object.assign({}, state.mood, partial);
  }

  function decay() {
    state.audio *= 0.965;
    state.traffic *= 0.978;
    state.memoryRate *= 0.972;
    state.jobRate *= 0.974;
  }

  window.OrbythChrome = {
    Instrument, WaveStrip, Gauge,
    state, setState, setMood, attach, resize, frame,
    pulseAudio, pulseTraffic, pulseMemory, pulseJobs, decay,
  };
})();
