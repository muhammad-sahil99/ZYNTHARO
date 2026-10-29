/* ZYNTHARO instrument widgets — canvas 2D mini-instruments.

   The console in the reference is dense because it is *busy with instruments*: a sweeping radar,
   a lattice diagram, bar clusters, ruled scales, cell matrices, ladders. These are those
   instruments, and the rule that governs every one of them is the same rule as the rest of the
   console:

     **they are only ever fed real values.**

   ``setState`` receives counters the core actually reported (fleet size, action totals, memory
   counts, quota state, helper processes). History strips are built by sampling those counters
   over time, which is a real measurement of the system, not a decorative oscillator. A widget
   with no data draws its frame and says so, because a console that invents numbers is worse
   than one that shows fewer of them.

   Cost: each widget is a few dozen strokes, no filters, no shadows, DPR capped at 2. */

(function () {
  "use strict";

  const CYAN = [90, 209, 255];
  const SOFT = [155, 234, 255];
  const WARM = [255, 138, 60];
  const VIOLET = [157, 123, 255];
  const GREEN = [86, 240, 155];
  const AMBER = [255, 200, 97];
  const RED = [255, 95, 122];

  function rgba(colour, alpha) {
    const a = Math.max(0, Math.min(1, alpha));
    return "rgba(" + colour[0] + "," + colour[1] + "," + colour[2] + "," + a.toFixed(3) + ")";
  }

  /* ------------------------------------------------------------------ shared state */
  const state = {
    link: "—",
    model: "—",
    agents: { total: 0, running: 0, blocked: 0, failed: 0, idle: 0 },
    teams: 0,
    actions: 0,
    actionGroups: {},
    memory: { episodes: 0, facts: 0, tasks: 0, audits: 0 },
    quota: "ok",
    jobs: { running: 0, total: 0 },
    tokens: 0,
    traffic: 0,
    audio: 0,
    memoryRate: 0,
    jobRate: 0,
    // set by the effects director: how much the needles shake and how fast the instruments sweep
    mood: { jitter: 0.08, speed: 1 },
    /* Channel levels are action-count shares per capability group: a channel is "loaded" when
       the system really registered that work, so the ladders track the registry, not a mood. */
    levels: { vision: 0, input: 0, browser: 0, office: 0, files: 0, load: 0, latency: 0 },
  };

  function setState(partial) {
    if (!partial) return;
    Object.keys(partial).forEach((key) => {
      const value = partial[key];
      if (value && typeof value === "object" && !Array.isArray(value) && state[key] && typeof state[key] === "object") {
        state[key] = Object.assign({}, state[key], value);
      } else {
        state[key] = value;
      }
    });
  }

  function setMood(partial) {
    if (partial) state.mood = Object.assign({}, state.mood, partial);
  }

  /* History strips. ``sample`` is called a few times a second by the controller; each entry is a
     real reading of a counter, so a spike in the strip means the system really spiked. */
  const HISTORY_KEYS = ["traffic", "actions", "memory", "tokens", "jobs"];
  const HISTORY_LEN = 96;
  const history = {};
  HISTORY_KEYS.forEach((key) => { history[key] = new Array(HISTORY_LEN).fill(0); });
  let lastActions = 0;

  function historyValue(key) {
    if (key === "traffic") return state.traffic;
    if (key === "actions") return state.actions;
    if (key === "memory") return state.memory.episodes + state.memory.facts;
    if (key === "tokens") return state.tokens;
    if (key === "jobs") return state.jobs.running;
    return 0;
  }

  function sample() {
    HISTORY_KEYS.forEach((key) => {
      const value = historyValue(key);
      history[key].push(key === "actions" ? Math.max(0, value - lastActions) : value);
      if (history[key].length > HISTORY_LEN) history[key].shift();
    });
    lastActions = state.actions;
    // decay the two sampled rate planes: a strip must fall back when the work stops
    state.memoryRate *= 0.94;
    state.jobRate *= 0.94;
  }

  function series(key) {
    const data = history[key] || [];
    if (!data.length) return [];
    const max = Math.max.apply(null, data.concat([1]));
    return data.map((value) => value / max);
  }

  /* ------------------------------------------------------------------ base widget */
  class Widget {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.source = canvas.dataset ? (canvas.dataset.source || "") : "";
      this.t0 = 0;
      this.resize();
    }

    resize() {
      const ctx = this.ctx;
      if (!ctx) return;
      const ratio = Math.min((typeof window !== "undefined" && window.devicePixelRatio) || 1, 2);
      const width = this.canvas.clientWidth || this.canvas.width || 120;
      const height = this.canvas.clientHeight || this.canvas.height || 40;
      this.canvas.width = Math.max(1, Math.floor(width * ratio));
      this.canvas.height = Math.max(1, Math.floor(height * ratio));
      this.width = width;
      this.height = height;
      this.ratio = ratio;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    /* A hairline frame, drawn by every widget: the instrument sits in a housing. */
    framePlate(ctx, label) {
      ctx.strokeStyle = rgba(CYAN, 0.12);
      ctx.lineWidth = 1;
      ctx.strokeRect(0.5, 0.5, this.width - 1, this.height - 1);
      if (!label) return;
      ctx.font = "7px " + FONT;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(CYAN, 0.42);
      ctx.fillText(label, 3, 2);
    }
  }

  const FONT = "ui-monospace, SFMono-Regular, Menlo, monospace";

  /* ------------------------------------------------------------------ sparkline */
  class Spark extends Widget {
    framePlateDraw() { this.framePlate(this.ctx, String(this.source).toUpperCase().slice(0, 14) + " · 96s"); }

    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      const data = series(this.source);
      ctx.clearRect(0, 0, this.width, this.height);
      const pad = 4;
      const w = this.width - pad * 2;
      const h = this.height - pad * 2 - 8;
      // graticule: four ruled lines, so a curve can be read against a scale
      ctx.strokeStyle = rgba(CYAN, 0.09);
      for (let i = 0; i <= 3; i++) {
        const y = pad + 8 + (h * i) / 3;
        ctx.beginPath();
        ctx.moveTo(pad, y);
        ctx.lineTo(pad + w, y);
        ctx.stroke();
      }
      this.framePlateDraw();
      if (data.length < 2) {
        ctx.font = "8px " + FONT;
        ctx.fillStyle = rgba(CYAN, 0.5);
        ctx.textAlign = "center";
        ctx.fillText("no data", this.width / 2, this.height / 2);
        return;
      }
      const step = w / (data.length - 1);
      ctx.beginPath();
      data.forEach((value, index) => {
        const x = pad + index * step;
        const y = pad + 8 + h - value * h;
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = rgba(SOFT, 0.85);
      ctx.lineWidth = 1.2;
      ctx.stroke();
      // the fill under the curve is what makes a thin strip readable at 46px tall
      ctx.lineTo(pad + w, pad + 8 + h);
      ctx.lineTo(pad, pad + 8 + h);
      ctx.closePath();
      ctx.fillStyle = rgba(CYAN, 0.14);
      ctx.fill();
      const last = data[data.length - 1];
      const lastX = pad + w;
      const lastY = pad + 8 + h - last * h;
      ctx.beginPath();
      ctx.arc(lastX, lastY, 2.2, 0, Math.PI * 2);
      ctx.fillStyle = last > 0.72 ? rgba(WARM, 0.95) : rgba(SOFT, 0.95);
      ctx.fill();
    }
  }

  /* ------------------------------------------------------------------ bar cluster */
  class Bars extends Widget {
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      this.framePlate(ctx, String(this.source).toUpperCase().slice(0, 16));
      const data = series(this.source).slice(-14);
      if (!data.length) return;
      const pad = 3;
      const top = 10;
      const w = this.width - pad * 2;
      const h = this.height - top - pad;
      const step = w / data.length;
      data.forEach((value, index) => {
        const barWidth = Math.max(1.5, step * 0.56);
        const barHeight = Math.max(0.6, value * h);
        const x = pad + index * step;
        ctx.fillStyle = value > 0.8 ? rgba(WARM, 0.9) : rgba(CYAN, 0.30 + value * 0.55);
        ctx.fillRect(x, this.height - pad - barHeight, barWidth, barHeight);
        if ((index & 1) === 0) {
          ctx.fillStyle = rgba(CYAN, 0.20);
          ctx.fillRect(x, this.height - pad - 1, barWidth, 1);
        }
      });
      const peak = Math.max.apply(null, data);
      ctx.font = "8px " + FONT;
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(SOFT, 0.8);
      ctx.fillText((peak * 100).toFixed(0) + "%", this.width - 4, 2);
    }
  }

  /* ------------------------------------------------------------------ radar sweep */
  class Radar extends Widget {
    constructor(canvas) {
      super(canvas);
      this.blips = [];
    }
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      const cx = this.width / 2;
      const cy = this.height / 2;
      const radius = Math.min(this.width, this.height) * 0.42;
      const seconds = now / 1000;

      // rings and spokes
      ctx.strokeStyle = rgba(CYAN, 0.16);
      for (let ring = 1; ring <= 3; ring++) {
        ctx.beginPath();
        ctx.arc(cx, cy, (radius * ring) / 3, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.strokeStyle = rgba(CYAN, 0.12);
      ctx.beginPath();
      ctx.moveTo(cx - radius, cy); ctx.lineTo(cx + radius, cy);
      ctx.moveTo(cx, cy - radius); ctx.lineTo(cx, cy + radius);
      ctx.stroke();

      // the sweep: a rotating sector, the one moving part
      const angle = seconds * 0.9 * ((state.mood && state.mood.speed) || 1);
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(angle);
      const gradient = ctx.createLinearGradient ? null : null;
      void gradient;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, radius, -0.34, 0);
      ctx.closePath();
      ctx.fillStyle = rgba(CYAN, 0.16);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(radius, 0);
      ctx.strokeStyle = rgba(SOFT, 0.7);
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.restore();

      /* Blips are agents, placed by rank ring and identity angle, and each one fades as the
         sweep passes it. With no agents the radar is honestly empty. */
      const total = state.agents.total || 0;
      for (let i = 0; i < total; i++) {
        const ring = 0.32 + ((i % 4) / 4) * 0.68;
        const blipAngle = (i / Math.max(1, total)) * Math.PI * 2 + Math.PI / 3;
        const phase = ((angle - blipAngle) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
        const glow = Math.max(0, 1 - phase / 1.7);
        if (glow <= 0.02) continue;
        const x = cx + Math.cos(blipAngle) * radius * ring;
        const y = cy + Math.sin(blipAngle) * radius * ring;
        const running = i < state.agents.running;
        const colour = running ? GREEN : (i < state.agents.running + state.agents.blocked ? AMBER : CYAN);
        ctx.beginPath();
        ctx.arc(x, y, 1.6 + glow * 1.8, 0, Math.PI * 2);
        ctx.fillStyle = rgba(colour, 0.25 + glow * 0.7);
        ctx.fill();
      }

      ctx.font = "7px " + FONT;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(CYAN, 0.45);
      ctx.fillText("FLEET", 3, 2);
      ctx.textAlign = "right";
      ctx.fillStyle = rgba(SOFT, 0.75);
      ctx.fillText(String(total) + " SIG", this.width - 3, 2);
    }
  }

  /* ------------------------------------------------------------------ hexagon lattice */
  class Hex extends Widget {
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      const seconds = now / 1000;
      const cx = this.width / 2;
      const cy = this.height / 2 + 4;
      const radius = Math.min(this.width, this.height) * 0.30;

      /* Seven-cell lattice: centre is the Sovereign, ring one the command tier, ring two the
         operatives. Lit cells are real rosters; unlit cells are empty seats, drawn dim. */
      const cells = [
        { u: 0, v: 0, count: 1 },
        { u: 0, v: -2, count: state.teams || 0 },
        { u: 2, v: -1, count: state.agents.running || 0 },
        { u: 2, v: 1, count: state.agents.blocked || 0 },
        { u: 0, v: 2, count: state.agents.failed || 0 },
        { u: -2, v: 1, count: state.agents.idle || 0 },
        { u: -2, v: -1, count: Math.max(0, (state.agents.total || 0) - (state.agents.running || 0)) },
      ];
      const hexPoints = (hx, hy, r) => {
        const points = [];
        for (let i = 0; i < 6; i++) {
          const angle = (Math.PI / 3) * i - Math.PI / 6;
          points.push([hx + Math.cos(angle) * r, hy + Math.sin(angle) * r]);
        }
        return points;
      };
      const positions = cells.map((cell) => {
        const hx = cx + (cell.u + cell.v / 2) * (radius * 1.02);
        const hy = cy + cell.v * (radius * 0.60);
        return { cell: cell, x: hx, y: hy };
      });
      const lit = (index) => (positions[index].cell.count || 0) > 0;

      // lattice edges first, under the cells
      ctx.strokeStyle = rgba(CYAN, 0.16);
      ctx.lineWidth = 1;
      positions.forEach((position, index) => {
        if (index === 0) return;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(position.x, position.y);
        ctx.stroke();
      });
      // a travelling pulse along the lit edges: this is the "activity" the plate shows
      ctx.strokeStyle = rgba(WARM, 0.5 + 0.3 * Math.sin(seconds * 3));
      ctx.lineWidth = 1.2;
      positions.forEach((position, index) => {
        if (index === 0 || !lit(index)) return;
        const t = ((seconds * 0.7 + index * 0.19) % 1);
        ctx.beginPath();
        ctx.moveTo(cx + (position.x - cx) * t, cy + (position.y - cy) * t);
        ctx.lineTo(cx + (position.x - cx) * Math.min(1, t + 0.16), cy + (position.y - cy) * Math.min(1, t + 0.16));
        ctx.stroke();
      });

      positions.forEach((position, index) => {
        const points = hexPoints(position.x, position.y, radius * (index === 0 ? 0.52 : 0.40));
        ctx.beginPath();
        points.forEach((point, k) => { if (k === 0) ctx.moveTo(point[0], point[1]); else ctx.lineTo(point[0], point[1]); });
        ctx.closePath();
        ctx.strokeStyle = lit(index) ? rgba(SOFT, 0.7) : rgba(CYAN, 0.22);
        ctx.lineWidth = index === 0 ? 1.4 : 1;
        ctx.stroke();
        if (lit(index)) {
          ctx.fillStyle = rgba(CYAN, index === 0 ? 0.20 : 0.10);
          ctx.fill();
        }
        ctx.font = "8px " + FONT;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = lit(index) ? rgba(SOFT, 0.9) : rgba(CYAN, 0.35);
        ctx.fillText(String(position.cell.count || 0), position.x, position.y);
      });
      ctx.font = "7px " + FONT;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(CYAN, 0.45);
      ctx.fillText("TOPOLOGY", 3, 2);
    }
  }

  /* ------------------------------------------------------------------ cell matrix */
  class Cells extends Widget {
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      this.framePlate(ctx, "MEMORY BLOCKS");
      const columns = 34;
      const rows = 3;
      const top = 12;
      const cellWidth = (this.width - 6) / columns;
      const cellHeight = (this.height - top - 4) / rows;
      const episodes = state.memory.episodes || 0;
      const facts = state.memory.facts || 0;
      const audits = state.memory.audits || 0;
      const filled = Math.min(columns * rows, episodes);
      const factRatio = episodes ? facts / episodes : 0;
      for (let index = 0; index < columns * rows; index++) {
        const x = 3 + (index % columns) * cellWidth;
        const y = top + Math.floor(index / columns) * cellHeight;
        const on = index < filled;
        if (!on) {
          ctx.fillStyle = rgba(CYAN, 0.07);
          ctx.fillRect(x, y, Math.max(1, cellWidth - 1.2), Math.max(1, cellHeight - 1.2));
          continue;
        }
        const warm = factRatio > 0.2 && index % 5 === 0;
        const audit = audits > 400 && index % 11 === 0;
        ctx.fillStyle = audit ? rgba(VIOLET, 0.75) : (warm ? rgba(WARM, 0.7) : rgba(CYAN, 0.45));
        ctx.fillRect(x, y, Math.max(1, cellWidth - 1.2), Math.max(1, cellHeight - 1.2));
      }
      const sweep = Math.floor((now / 90) % columns);
      ctx.fillStyle = rgba(SOFT, 0.22);
      ctx.fillRect(3 + sweep * cellWidth, top, Math.max(1, cellWidth - 1.2), cellHeight * rows);
    }
  }

  /* ------------------------------------------------------------------ ladder meter */
  class Ladder extends Widget {
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      const level = Math.max(0, Math.min(1, state.levels[this.source] || 0));
      const segments = 12;
      const top = 3;
      const height = this.height - 6;
      const segHeight = height / segments;
      for (let i = 0; i < segments; i++) {
        const value = (i + 1) / segments;
        const on = value <= level + 0.0001;
        const y = this.height - 3 - (i + 1) * segHeight;
        ctx.fillStyle = on
          ? rgba(value > 0.85 ? WARM : (value > 0.6 ? AMBER : CYAN), 0.85)
          : rgba(CYAN, 0.09);
        ctx.fillRect(2, y + 0.8, this.width - 4, Math.max(1, segHeight - 1.6));
      }
      ctx.font = "7px " + FONT;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(SOFT, 0.75);
      ctx.fillText(Math.round(level * 100) + "%", this.width / 2, 0);
    }
  }

  /* ------------------------------------------------------------------ ruled scale */
  class Ruler extends Widget {
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      this.framePlate(ctx, null);
      const level = Math.max(0, Math.min(1, state.levels[this.source] || 0));
      const y = this.height - 12;
      ctx.strokeStyle = rgba(CYAN, 0.32);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(4, y);
      ctx.lineTo(this.width - 4, y);
      ctx.stroke();
      const ticks = 30;
      for (let i = 0; i <= ticks; i++) {
        const x = 4 + ((this.width - 8) * i) / ticks;
        const major = i % 5 === 0;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x, y + (major ? 6 : 3));
        ctx.strokeStyle = rgba(CYAN, major ? 0.4 : 0.18);
        ctx.stroke();
      }
      // jitter is real tension: pressure and fault states make the needles tremble
      const shake = ((state.mood && state.mood.jitter) || 0) * 0.04 * Math.sin(now / 47);
      const needleX = 4 + (this.width - 8) * Math.max(0, Math.min(1, level + shake));
      ctx.beginPath();
      ctx.moveTo(needleX, y - 9);
      ctx.lineTo(needleX, y + 8);
      ctx.strokeStyle = level > 0.85 ? rgba(WARM, 0.95) : rgba(SOFT, 0.9);
      ctx.lineWidth = 1.3;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(needleX, y - 10, 2, 0, Math.PI * 2);
      ctx.fillStyle = rgba(SOFT, 0.9);
      ctx.fill();
      ctx.font = "8px " + FONT;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(CYAN, 0.45);
      ctx.fillText(String(this.source).toUpperCase().slice(0, 12), 4, 2);
      ctx.textAlign = "right";
      ctx.fillStyle = rgba(SOFT, 0.85);
      ctx.fillText((level * 100).toFixed(0) + "%", this.width - 4, 2);
    }
  }

  /* ------------------------------------------------------------------ channel matrix */
  class Matrix extends Widget {
    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.width, this.height);
      /* One column per channel the console really has, filled to that channel's measured share
         of the registered action groups. This is the same set of numbers the ladders show, in a
         denser form, which is why the two can never disagree. */
      const lanes = Object.keys(state.levels);
      const rows = 4;
      const cellWidth = (this.width - 4) / lanes.length;
      const cellHeight = (this.height - 9) / rows;
      const seconds = now / 1000;
      lanes.forEach((lane, index) => {
        const level = Math.max(0, Math.min(1, state.levels[lane] || 0));
        const lit = Math.round(level * rows);
        for (let row = 0; row < rows; row++) {
          const on = (rows - row) <= lit;
          // a travelling glint, so the plate reads as live without inventing a reading
          const glint = 0.35 + 0.25 * Math.sin(seconds * 2.4 * ((state.mood && state.mood.speed) || 1) + index + row);
          ctx.fillStyle = on
            ? rgba(level > 0.8 ? WARM : (level > 0.5 ? AMBER : CYAN), 0.42 + 0.36 * glint)
            : rgba(CYAN, 0.06);
          ctx.fillRect(2 + index * cellWidth, row * cellHeight, Math.max(1, cellWidth - 1.4), Math.max(1, cellHeight - 1.4));
        }
        ctx.font = "6px " + FONT;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = rgba(SOFT, 0.55);
        ctx.fillText(lane.slice(0, 2).toUpperCase(), 2 + index * cellWidth + cellWidth / 2 - 0.7, rows * cellHeight + 1);
      });
    }
  }

  const KINDS = { spark: Spark, bars: Bars, radar: Radar, hex: Hex, cells: Cells, ladder: Ladder, ruler: Ruler, matrix: Matrix };

  /* ------------------------------------------------------------------ assembly */
  const instrument = { widgets: [] };

  function attach() {
    instrument.widgets = [];
    if (typeof document.querySelectorAll !== "function") return instrument;
    document.querySelectorAll("canvas[data-widget]").forEach((canvas) => {
      const Kind = KINDS[canvas.dataset.widget];
      if (!Kind) return;
      try {
        instrument.widgets.push(new Kind(canvas));
      } catch (error) {
        if (typeof console !== "undefined" && console.warn) console.warn("[zyntharo] widget failed: " + canvas.dataset.widget, error);
      }
    });
    return instrument;
  }

  function resize() { instrument.widgets.forEach((widget) => widget.resize()); }
  function frame(now) { instrument.widgets.forEach((widget) => widget.frame(now)); }

  /** Raise the memory plane because a fact was really written or recalled. */
  function pulseMemory(amount) { state.memoryRate = Math.min(1, state.memoryRate + (amount === undefined ? 0.35 : amount)); }
  /** Raise the process plane because a helper really started or exited. */
  function pulseJobs(amount) { state.jobRate = Math.min(1, state.jobRate + (amount === undefined ? 0.3 : amount)); }

  window.OrbythWidgets = {
    Spark, Bars, Radar, Hex, Cells, Ladder, Ruler, Matrix,
    state, history, series, setState, setMood, sample, attach, resize, frame, pulseMemory, pulseJobs,
  };
})();
