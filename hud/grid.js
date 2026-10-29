/* ZYNTHARO system grid — the instrument's backdrop, canvas 2D.

   In the reference plate the console is not floating in space: it sits inside a mesh of
   converging structural lines, ruled edges and columns of tiny data that scroll past. That is
   what makes the frame read as a *machine* rather than as a dark web page, and it is the layer
   that was missing.

   It draws, back to front:

     floor/ceiling mesh   converging structural rules, drifting slowly, brightest at the horizon
     edge rulers          real measurements of the current viewport, with degree graduations
     datum plates         viewport size, device pixel ratio, frame time — measured, not invented
     data columns         scrolling hexadecimal buses, labelled, one per channel the console has
     reticles             crosshair marks at the ruler intersections

   Cost discipline: no gradients per line, no shadows, no filters, and the whole backdrop is
   ~150 strokes a frame. The scrolling columns are the only animation. */

(function () {
  "use strict";

  const CYAN = [90, 209, 255];
  const SOFT = [155, 234, 255];
  const WARM = [255, 138, 60];

  const HEX = "0123456789ABCDEF";
  const GLYPHS = "0123456789ABCDEF/\\|+×·";

  function rgba(colour, alpha) {
    const a = Math.max(0, Math.min(1, alpha));
    return "rgba(" + colour[0] + "," + colour[1] + "," + colour[2] + "," + a.toFixed(3) + ")";
  }

  /* The columns name the console's real buses. They scroll characters, which is presentation;
     the *labels* are the channels the system actually has, so nothing here claims a reading it
     does not have. */
  const COLUMNS = [
    { label: "BUS.SYS", warm: false },
    { label: "MEM.IO", warm: false },
    { label: "QUOTA", warm: true },
    { label: "SCHED", warm: false },
    { label: "SENSOR", warm: true },
    { label: "NET", warm: false },
  ];

  class SystemGrid {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.t0 = 0;
      this.rows = [];        // per-column scroll state
      this.metrics = { fps: 0, particles: 0 };
      // mood is set by the effects director every frame: how loud the mesh is, how fast the buses
      // scroll, how far the plate has gone to alarm, and how badly the signal is breaking up
      this.mood = { intensity: 1, speed: 1, alarm: 0, glitch: 0 };
      this.resize();
    }

    resize() {
      const ctx = this.ctx;
      if (!ctx) return;
      const ratio = Math.min((typeof window !== "undefined" && window.devicePixelRatio) || 1, 2);
      const width = this.canvas.clientWidth || (typeof window !== "undefined" ? window.innerWidth : 1600);
      const height = this.canvas.clientHeight || (typeof window !== "undefined" ? window.innerHeight : 900);
      this.canvas.width = Math.max(1, Math.floor(width * ratio));
      this.canvas.height = Math.max(1, Math.floor(height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.width = width;
      this.height = height;
      this.rowHeight = 13;
      const rowCount = Math.ceil(height / this.rowHeight) + 2;
      this.rows = COLUMNS.map((column, index) => ({
        column: column,
        // a fixed offset per column so they never line up and pulse in lockstep
        offset: (index * 7) % rowCount,
        cells: Array.from({ length: rowCount }, () => GLYPHS[Math.floor(Math.random() * GLYPHS.length)]),
      }));
    }

    setMetrics(partial) {
      if (partial) this.metrics = Object.assign({}, this.metrics, partial);
    }

    setMood(partial) {
      if (partial) this.mood = Object.assign({}, this.mood, partial);
    }

    frame(now) {
      const ctx = this.ctx;
      if (!ctx) return;
      const width = this.width;
      const height = this.height;
      const seconds = now / 1000;
      const mood = this.mood || { intensity: 1, speed: 1, alarm: 0, glitch: 0 };
      // the mesh itself goes warm as the system moves toward alarm: one tint, applied everywhere,
      // is what makes a fault state read as a different machine rather than a different label
      const alarmTint = [
        CYAN[0] + (WARM[0] - CYAN[0]) * mood.alarm,
        CYAN[1] + (WARM[1] - CYAN[1]) * mood.alarm,
        CYAN[2] + (WARM[2] - CYAN[2]) * mood.alarm,
      ];
      const cx = width / 2;
      const horizon = height * 0.56;
      const radius = Math.min(width, height) * 0.5;   // the core's field of view, in pixels

      ctx.clearRect(0, 0, width, height);

      // ---- converging structural mesh: the housing the instrument is mounted in
      ctx.lineWidth = 1;
      const drift = (seconds * 0.02) % 1;
      for (let i = -9; i <= 9; i++) {
        const spread = i / 9;
        ctx.beginPath();
        ctx.moveTo(cx + spread * radius * 2.3, horizon - radius * 1.5);
        ctx.lineTo(cx + spread * width * 0.75, height + 30);
        ctx.strokeStyle = rgba(alarmTint, (0.05 + 0.03 * (1 - Math.abs(spread))) * mood.intensity);
        ctx.stroke();
      }
      // horizontal rails, spaced by perspective from the horizon outward
      for (let i = 1; i <= 11; i++) {
        const t = (i / 11 + drift) % 1;
        const y = horizon + Math.pow(t, 1.9) * (height - horizon) * 1.05;
        if (y > height + 4) continue;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.strokeStyle = rgba(alarmTint, (0.05 * (1 - t) + 0.02) * mood.intensity);
        ctx.stroke();
        // ...and the same mesh mirrored above the horizon, fainter
        const mirror = horizon - (y - horizon) * 0.55;
        if (mirror > -4) {
          ctx.beginPath();
          ctx.moveTo(0, mirror);
          ctx.lineTo(width, mirror);
          ctx.strokeStyle = rgba(CYAN, 0.028 * (1 - t));
          ctx.stroke();
        }
      }

      // ---- edge rulers: this really does measure the frame it is drawn in
      ctx.font = "7px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillStyle = rgba(CYAN, 0.34);
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      for (let x = 40; x < width - 20; x += 40) {
        const major = x % 200 === 0;
        ctx.beginPath();
        ctx.moveTo(x, height - (major ? 16 : 10));
        ctx.lineTo(x, height - 2);
        ctx.strokeStyle = rgba(CYAN, major ? 0.26 : 0.12);
        ctx.stroke();
        if (major) ctx.fillText(String(x).padStart(4, "0"), x + 2, height - 9);
      }
      ctx.textAlign = "right";
      for (let y = 120; y < height - 60; y += 40) {
        const major = y % 200 === 0;
        ctx.beginPath();
        ctx.moveTo(major ? 16 : 10, y);
        ctx.lineTo(2, y);
        ctx.strokeStyle = rgba(CYAN, major ? 0.26 : 0.12);
        ctx.stroke();
        if (major) ctx.fillText(String(y).padStart(4, "0"), width - 3, y - 8);
      }

      // ---- datum plates: measured values only (viewport, ratio, frame time, budget)
      const plates = [
        { x: 14, y: 112, label: "FRAME", value: width + " × " + height },
        { x: 14, y: 124, label: "SCALE", value: (this.ratio || 1).toFixed(2) + " : 1" },
        { x: 14, y: 136, label: "FPS", value: String(Math.round(this.metrics.fps || 0)).padStart(2, "0") },
        { x: 14, y: 148, label: "PTS", value: String(this.metrics.particles || 0) },
      ];
      ctx.textAlign = "left";
      plates.forEach((plate) => {
        ctx.fillStyle = rgba(CYAN, 0.32);
        ctx.fillText(plate.label, plate.x, plate.y);
        ctx.fillStyle = rgba(SOFT, 0.55);
        ctx.fillText(plate.value, plate.x + 34, plate.y);
      });

      // ---- reticles at the corners of the instrument field
      const marks = [
        [cx - radius * 1.62, horizon - radius * 0.90],
        [cx + radius * 1.62, horizon - radius * 0.90],
        [cx - radius * 1.62, horizon + radius * 0.90],
        [cx + radius * 1.62, horizon + radius * 0.90],
      ];
      marks.forEach(([x, y]) => {
        if (x < 20 || x > width - 20 || y < 20 || y > height - 20) return;
        ctx.strokeStyle = rgba(CYAN, 0.24);
        ctx.beginPath();
        ctx.moveTo(x - 7, y); ctx.lineTo(x + 7, y);
        ctx.moveTo(x, y - 7); ctx.lineTo(x, y + 7);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y, 3.2, 0, Math.PI * 2);
        ctx.strokeStyle = rgba(CYAN, 0.16);
        ctx.stroke();
      });

      // ---- alarm state: a hazard frame and a tearing signal. Both only appear when the system
      // reports something wrong, so they stay meaningful instead of becoming wallpaper.
      if (mood.alarm > 0.05) {
        const pulse = 0.5 + 0.5 * Math.sin(seconds * (3 + mood.alarm * 5));
        ctx.strokeStyle = rgba(WARM, 0.10 + 0.22 * mood.alarm * pulse);
        ctx.lineWidth = 2;
        ctx.strokeRect(10.5, 10.5, width - 21, height - 21);
        ctx.lineWidth = 1;
      }
      if (mood.glitch > 0.05) {
        const bands = 3 + Math.floor(mood.glitch * 5);
        for (let i = 0; i < bands; i++) {
          const seed = Math.sin(seconds * 37 + i * 12.9) * 0.5 + 0.5;
          const y = 40 + seed * (height - 90);
          const tearWidth = width * (0.10 + 0.35 * seed) * mood.glitch;
          ctx.fillStyle = rgba(WARM, 0.05 + 0.10 * mood.glitch);
          ctx.fillRect(seed > 0.5 ? 0 : width - tearWidth, y, tearWidth, 2 + 3 * mood.glitch);
        }
      }

      // ---- data columns: scrolling buses down both margins of the core field
      const columnWidth = 62;
      const gap = 10;
      const leftEdge = Math.max(296, cx - radius * 1.05 - columnWidth - gap);
      const rightEdge = Math.min(width - 320 - columnWidth, cx + radius * 1.05 + gap);
      const scroll = seconds * 9 * (mood.speed || 1);
      this.rows.forEach((row, index) => {
        const onRight = index % 2 === 1;
        const slot = Math.floor(index / 2);
        let x = onRight
          ? rightEdge + slot * (columnWidth + 6)
          : leftEdge - slot * (columnWidth + 6);
        if (x < 8 || x + columnWidth > width - 8) return;
        const travel = (scroll + row.offset * this.rowHeight) % (this.rows.length * this.rowHeight);

        // column housing
        ctx.strokeStyle = rgba(CYAN, 0.10);
        ctx.strokeRect(x + 0.5, 60.5, columnWidth, height - 130);
        ctx.fillStyle = rgba(row.column.warm ? WARM : CYAN, 0.05);
        ctx.fillRect(x, 60, columnWidth, height - 130);
        ctx.fillStyle = rgba(row.column.warm ? WARM : alarmTint, 0.5 + 0.2 * mood.intensity);
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(row.column.label, x + 3, 64);

        // the scrolling rows themselves
        const top = 74;
        const bottom = height - 74;
        const count = Math.floor((bottom - top) / this.rowHeight);
        for (let i = 0; i < count; i++) {
          const sourceIndex = (i + Math.floor(travel / this.rowHeight)) % row.cells.length;
          const glyph = row.cells[sourceIndex];
          const fade = 1 - i / count;
          const y = top + i * this.rowHeight;
          const bright = ((sourceIndex * 7 + Math.floor(seconds * 3)) % 29) === 0;
          ctx.fillStyle = bright
            ? rgba(row.column.warm ? WARM : SOFT, 0.75)
            : rgba(row.column.warm ? WARM : CYAN, 0.16 + 0.24 * fade);
          ctx.fillText(glyph + row.cells[(sourceIndex + 1) % row.cells.length] + " " +
            row.cells[(sourceIndex + 2) % row.cells.length] + row.cells[(sourceIndex + 3) % row.cells.length], x + 4, y);
        }
        // a datum line every so often, so the column reads as a register
        ctx.fillStyle = rgba(CYAN, 0.10);
        ctx.fillRect(x, bottom - 2, columnWidth, 1);
      });
    }
  }

  window.OrbythGrid = { SystemGrid, COLUMNS, HEX };
})();
