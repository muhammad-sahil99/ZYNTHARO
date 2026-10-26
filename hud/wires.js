/* ORBYNTH live wiring — canvas 2D overlay.
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
        ctx.lineWidth = 0.8 + heat * 2.2;
        ctx.strokeStyle = heat > 0.02
          ? this._rgba(entry.agent.color || "#5ad1ff", 0.18 + heat * 0.65)
          : "rgba(120, 190, 255, 0.13)";
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
        ctx.beginPath();
        ctx.arc(position.x, position.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(8, 18, 36, 0.92)";
        ctx.fill();
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = STATUS_COLOR[status] || color;
        ctx.stroke();

        ctx.fillStyle = color;
        ctx.font = (radius + 2) + "px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(entry.agent.insignia || "●", position.x, position.y + 0.5);

        if (this.showLabels) {
          ctx.fillStyle = "rgba(232, 244, 255, 0.86)";
          ctx.font = "11px ui-monospace, monospace";
          ctx.fillText(entry.agent.name || "", position.x, position.y + radius + 13);
          ctx.fillStyle = "rgba(143, 166, 196, 0.7)";
          ctx.font = "9px ui-monospace, monospace";
          ctx.fillText((entry.agent.model || "").split(":").pop().slice(0, 22), position.x, position.y + radius + 25);
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
