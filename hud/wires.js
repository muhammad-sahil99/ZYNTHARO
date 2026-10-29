/* ZYNTHARO live wiring — canvas 2D overlay.
 *
 * The wires are not decoration: they are the fleet's real topology and traffic.
 *   • ring radius encodes rank (Sovereign inside, Steward next, Foremen, then specialists
 *     and operatives on the outside)
 *   • node colour is the role colour the backend assigned, its ring shows status
 *     (green running, amber blocked, red failed, grey idle)
 *   • a wire brightens and carries a travelling pulse only while a message actually moves
 *     along it — agent.message, action.invoked/result, quota failures, transplants
 *   • clicking a node focuses that agent in the fleet panel
 */

(function () {
  "use strict";

  const RANK_RADIUS = { 0: 0, 1: 158, 2: 232, 3: 300, 4: 332, 5: 366, 6: 386, 7: 404 };
  const STATUS_COLOR = { running: "#4ade80", blocked: "#ffd166", failed: "#ff5f7a", stopped: "#5b708f", idle: "#5b708f" };

  class WireGraph {
    constructor(canvas, options) {
      options = options || {};
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.agents = [];
      this.nodes = new Map();
      this.pulses = [];
      this.heat = new Map();      // nodeId -> 0..1 glow
      this.wireHeat = new Map();  // "from>to" -> 0..1
      this.coreEnergy = 0;
      this.showLabels = options.labels !== false;
      this.onSelect = options.onSelect || function () {};
      this._bindEvents();
      this.resize();
    }

    _bindEvents() {
      this.canvas.style.pointerEvents = "auto";
      this.canvas.addEventListener("click", (event) => {
        const rect = this.canvas.getBoundingClientRect();
        const hit = this.hitTest(event.clientX - rect.left, event.clientY - rect.top);
        if (hit) this.onSelect(hit.agent_id);
      });
      this.canvas.addEventListener("mousemove", (event) => {
        const rect = this.canvas.getBoundingClientRect();
        const hit = this.hitTest(event.clientX - rect.left, event.clientY - rect.top);
        this.canvas.style.cursor = hit ? "pointer" : "default";
      });
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
    }

    setAgents(agents) {
      this.agents = agents || [];
      const byRank = new Map();
      this.agents.forEach((agent) => {
        const rank = agent.rank === undefined ? 5 : agent.rank;
        if (!byRank.has(rank)) byRank.set(rank, []);
        byRank.get(rank).push(agent);
      });
      this.nodes.clear();
      byRank.forEach((list, rank) => {
        const radius = RANK_RADIUS[rank] === undefined ? 404 : RANK_RADIUS[rank];
        list.forEach((agent, index) => {
          const spread = rank === 0 ? 1 : list.length;
          const angle = (-Math.PI / 2) + (index + 0.5) * ((Math.PI * 2) / Math.max(1, spread)) + rank * 0.4;
          this.nodes.set(agent.agent_id, { agent: agent, angle: angle, radius: radius, rank: rank });
        });
      });
    }

    pulse(fromId, toId, kind) {
      const from = this.nodes.get(fromId);
      const to = toId && this.nodes.get(toId);
      this.pulses.push({
        from: fromId, to: toId || null, kind: kind || "message",
        born: performance.now(), life: 1100,
        color: (to && to.agent.color) || (from && from.agent.color) || "#5ad1ff",
        trail: [],
      });
      if (fromId) this.heat.set(fromId, 1);
      if (toId) this.heat.set(toId, 1);
      const key = fromId + ">" + (toId || "core");
      this.wireHeat.set(key, 1);
      if (this.pulses.length > 80) this.pulses.shift();
    }

    setEnergy(value) { this.coreEnergy = value; }

    centre() {
      return { x: this.width / 2, y: this.height * 0.52 };
    }

    nodePosition(entry, time) {
      const centre = this.centre();
      if (entry.radius === 0) return { x: centre.x, y: centre.y };
      // slow orbital precession, so the graph breathes without ever looking like a spinner
      const angle = entry.angle + Math.sin(time / 9000 + entry.rank) * 0.06;
      return { x: centre.x + Math.cos(angle) * entry.radius, y: centre.y + Math.sin(angle) * entry.radius * 0.86 };
    }

    hitTest(x, y) {
      const time = performance.now();
      for (const entry of this.nodes.values()) {
        const position = this.nodePosition(entry, time);
        if (Math.hypot(position.x - x, position.y - y) < 22) return entry.agent;
      }
      return null;
    }

    frame(now) {
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.width, this.height);
      const centre = this.centre();

      if (this.coreEnergy > 0.01) {
        const glow = ctx.createRadialGradient(centre.x, centre.y, 20, centre.x, centre.y, 260);
        glow.addColorStop(0, "rgba(124, 246, 255, " + (0.16 * this.coreEnergy).toFixed(3) + ")");
        glow.addColorStop(1, "rgba(124, 246, 255, 0)");
        ctx.fillStyle = glow;
        ctx.fillRect(centre.x - 280, centre.y - 280, 560, 560);
      }

      /* ---- routed buses. One concentric channel per rank, each with a plate on the X axis.
         This is what turns a scatter of curves into a wiring harness: the traces are routed
         down channels, and a rank's channel lights warm when it is a command tier. */
      const rankRadii = new Map();
      this.nodes.forEach((entry) => {
        if (!rankRadii.has(entry.rank)) rankRadii.set(entry.rank, entry.radius);
      });
      Array.from(rankRadii.keys()).sort((a, b) => a - b).forEach((rank) => {
        const radius = rankRadii.get(rank);
        if (!radius) return;
        const warm = rank <= 1;
        ctx.save();
        ctx.translate(centre.x, centre.y);
        ctx.setLineDash([3, 9]);
        ctx.beginPath();
        ctx.arc(0, 0, radius, 0, Math.PI * 2);
        ctx.strokeStyle = warm ? "rgba(255,138,60,0.18)" : "rgba(90,209,255,0.12)";
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(0, 0, radius, -0.085, 0.085);
        ctx.strokeStyle = warm ? "rgba(255,138,60,0.7)" : "rgba(90,209,255,0.44)";
        ctx.lineWidth = 1.4;
        ctx.stroke();
        ctx.font = "8px ui-monospace, monospace";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillStyle = warm ? "rgba(255,138,60,0.72)" : "rgba(90,209,255,0.5)";
        ctx.fillText("BUS." + String(rank).padStart(2, "0"), radius + 5, 0);
        ctx.restore();
      });

      // wires
      for (const entry of this.nodes.values()) {
        const position = this.nodePosition(entry, now);
        const key = entry.agent.agent_id + ">core";
        const reverse = "core>" + entry.agent.agent_id;
        const heat = Math.max(this.wireHeat.get(key) || 0, this.wireHeat.get(reverse) || 0);
        const parent = entry.agent.parent_id && this.nodes.get(entry.agent.parent_id);
        const origin = parent ? this.nodePosition(parent, now) : centre;
        ctx.beginPath();
        ctx.moveTo(origin.x, origin.y);
        const midX = (origin.x + position.x) / 2 + Math.sin(now / 2600 + entry.rank) * 14;
        const midY = (origin.y + position.y) / 2 + Math.cos(now / 3100 + entry.rank) * 14;
        ctx.quadraticCurveTo(midX, midY, position.x, position.y);
        // Three passes turn a flat line into a light beam: a wide faint halo, a mid body, and a
        // thin bright core. Cost is three strokes per wire, which is nothing next to the draw.
        // command-tier links run warm: the orange traces in the reference plate are the main
        // agent's own bus, and keeping the rest in role colour preserves who owns what.
        const wireColor = entry.rank <= 1 ? "#ff8a3c" : (entry.agent.color || "#5ad1ff");
        ctx.lineCap = "round";
        ctx.lineWidth = 5.5 + heat * 6;
        ctx.strokeStyle = heat > 0.02 ? this._rgba(wireColor, 0.05 + heat * 0.10) : "rgba(120, 190, 255, 0.028)";
        ctx.stroke();
        ctx.lineWidth = 2.1 + heat * 3.4;
        ctx.strokeStyle = heat > 0.02 ? this._rgba(wireColor, 0.12 + heat * 0.30) : "rgba(120, 190, 255, 0.07)";
        ctx.stroke();
        ctx.lineWidth = 0.7 + heat * 1.6;
        ctx.strokeStyle = heat > 0.02
          ? this._rgba(wireColor, 0.30 + heat * 0.65)
          : "rgba(120, 190, 255, 0.14)";
        ctx.stroke();
        if (this.wireHeat.get(key) !== undefined) {
          this.wireHeat.set(key, Math.max(0, (this.wireHeat.get(key) || 0) - 0.02));
        }
        if (this.wireHeat.get(reverse) !== undefined) {
          this.wireHeat.set(reverse, Math.max(0, (this.wireHeat.get(reverse) || 0) - 0.02));
        }
        void key; void heat;
      }

      // pulses travelling outward from the core along each wire
      this.pulses = this.pulses.filter((pulse) => now - pulse.born < pulse.life);
      for (const pulse of this.pulses) {
        const target = this.nodes.get(pulse.from) || this.nodes.get(pulse.to);
        if (!target) continue;
        const position = this.nodePosition(target, now);
        const progress = (now - pulse.born) / pulse.life;
        const x = centre.x + (position.x - centre.x) * progress;
        const y = centre.y + (position.y - centre.y) * progress;
        const alpha = 1 - progress;
        // trail: the packet remembers where it has been, so traffic reads as movement
        pulse.trail.push({ x: x, y: y });
        if (pulse.trail.length > 14) pulse.trail.shift();
        pulse.trail.forEach((point, index) => {
          const fade = (index / pulse.trail.length) * alpha * 0.5;
          if (fade <= 0.01) return;
          ctx.beginPath();
          ctx.arc(point.x, point.y, 1.2 + 2.6 * (index / pulse.trail.length), 0, Math.PI * 2);
          ctx.fillStyle = this._rgba(pulse.color, fade);
          ctx.fill();
        });
        ctx.beginPath();
        ctx.arc(x, y, 3.4 + 3 * alpha, 0, Math.PI * 2);
        ctx.fillStyle = this._rgba(pulse.color, alpha);
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, 9 + 6 * alpha, 0, Math.PI * 2);
        ctx.strokeStyle = this._rgba(pulse.color, alpha * 0.35);
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      // nodes
      for (const entry of this.nodes.values()) {
        const position = this.nodePosition(entry, now);
        const heat = this.heat.get(entry.agent.agent_id) || 0;
        const status = entry.agent.status || "idle";
        const color = entry.agent.color || "#5ad1ff";
        const radius = entry.rank <= 1 ? 15 : 11;

        if (heat > 0.02) {
          ctx.beginPath();
          ctx.arc(position.x, position.y, radius + 10 * heat, 0, Math.PI * 2);
          ctx.fillStyle = this._rgba(color, 0.18 * heat);
          ctx.fill();
        }
        // outer halo, then a rotating arc for a busy agent: state is visible at a glance
        ctx.beginPath();
        ctx.arc(position.x, position.y, radius + 5, 0, Math.PI * 2);
        ctx.fillStyle = this._rgba(color, 0.07);
        ctx.fill();
        ctx.beginPath();
        ctx.arc(position.x, position.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(8, 18, 36, 0.92)";
        ctx.fill();
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = STATUS_COLOR[status] || color;
        ctx.stroke();
        if (status === "running" || status === "working" || status === "deferred") {
          const sweep = (now / 700) % (Math.PI * 2);
          ctx.beginPath();
          ctx.arc(position.x, position.y, radius + 4.5, sweep, sweep + 1.9);
          ctx.lineWidth = 2;
          ctx.strokeStyle = this._rgba(color, 0.75);
          ctx.stroke();
        }
        if (entry.rank <= 1) {
          ctx.beginPath();
          ctx.arc(position.x, position.y, radius + 8, 0, Math.PI * 2);
          ctx.lineWidth = 0.8;
          ctx.strokeStyle = this._rgba(color, 0.22);
          ctx.stroke();
        }

        ctx.fillStyle = color;
        ctx.font = (radius + 2) + "px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(entry.agent.insignia || "●", position.x, position.y + 0.5);

        if (this.showLabels) {
          // a name plate rather than floating text: brackets, a status rule and the model id,
          // so each node reads as a labelled unit on a panel face
          const name = String(entry.agent.name || "").toUpperCase().slice(0, 18);
          const model = (entry.agent.model || "unassigned").split(":").pop().slice(0, 20);
          const width = Math.max(ctx.measureText(name).width, model.length * 5.4) + 12;
          const top = position.y + radius + 6;
          ctx.font = "9px ui-monospace, monospace";
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.strokeStyle = this._rgba(color, 0.35);
          ctx.lineWidth = 1;
          ctx.strokeRect(position.x - width / 2, top, width, 22);
          ctx.fillStyle = "rgba(4, 10, 22, 0.78)";
          ctx.fillRect(position.x - width / 2, top, width, 22);
          ctx.fillStyle = this._rgba(color, 0.95);
          ctx.fillText(name, position.x, top + 3);
          ctx.fillStyle = "rgba(143, 166, 196, 0.72)";
          ctx.font = "8px ui-monospace, monospace";
          ctx.fillText(model, position.x, top + 13);
          // status rule under the plate: green running, amber blocked, red failed
          ctx.fillStyle = (STATUS_COLOR[status] || color);
          ctx.fillRect(position.x - width / 2, top + 21, width * (status === "running" ? 1 : 0.45), 1.4);
        }
        if (this.heat.get(entry.agent.agent_id) !== undefined) {
          this.heat.set(entry.agent.agent_id, Math.max(0, heat - 0.02));
        }
      }
    }

    _rgba(hex, alpha) {
      const value = hex.replace("#", "");
      const full = value.length === 3 ? value.split("").map((c) => c + c).join("") : value;
      const int = parseInt(full, 16);
      const r = (int >> 16) & 255, g = (int >> 8) & 255, b = int & 255;
      return "rgba(" + r + "," + g + "," + b + "," + alpha.toFixed(3) + ")";
    }
  }

  window.WireGraph = WireGraph;
})();
