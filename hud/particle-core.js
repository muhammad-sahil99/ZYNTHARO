/* ORBYNTH particle core — hand-written WebGL, no dependencies.
 *
 * Thousands of individually simulated points in a slow axial drift, morphing between
 * topologies as the system changes state:
 *   sphere  — idle, listening
 *   torus   — thinking / planning
 *   helix   — executing a multi-step task
 *   grid    — inspecting data, extracting, editing files
 *   wave    — speaking (radial energy ripple)
 *
 * Because every point carries its own seed, the drift is never a rigid rotation: particles
 * lead and lag each other, which is what makes the core read as alive instead of animated.
 */

(function () {
  "use strict";

  const VERT = `
    attribute vec3 aPosA;
    attribute vec3 aPosB;
    attribute float aSeed;
    attribute float aSize;
    uniform float uT;
    uniform float uTime;
    uniform float uEnergy;   // speaking ripple
    uniform float uShear;    // tool-use shear
    uniform float uCollapse; // error collapse
    uniform float uPixelRatio;
    uniform vec3  uColorA;
    uniform vec3  uColorB;
    varying float vSeed;
    varying float vEnergy;
    varying float vDepth;

    void main() {
      float t = uT;
      vec3 p = mix(aPosA, aPosB, t);

      // axial drift: each particle rotates at its own rate (earth-like spin, per particle)
      float angle = uTime * (0.045 + aSeed * 0.05) + aSeed * 6.2831853;
      float sa = sin(angle + uShear * 1.2);
      float ca = cos(angle + uShear * 1.2);
      p = vec3(p.x * ca - p.z * sa, p.y, p.x * sa + p.z * ca);

      // radial ripple while speaking
      float r = length(p);
      p *= 1.0 + uEnergy * 0.26 * sin(r * 9.0 - uTime * 6.5 + aSeed * 3.0);

      // tool-use shear: directional stretch, as if something is being pulled through the core
      p.x += uShear * 0.22 * sin(aSeed * 12.0 + uTime * 2.0);
      p.y -= uShear * 0.18 * (aSeed - 0.5);

      // collapse on error, then re-seed outward
      float k = 1.0 - uCollapse * (0.55 + 0.45 * aSeed);
      p *= k;

      // breathing
      p *= 1.0 + 0.02 * sin(uTime * 0.9 + aSeed * 4.0);

      // cheap perspective: nearer points are bigger and pushed outwards
      float persp = 1.0 / (1.65 - p.z * 0.55);
      gl_Position = vec4(p.xy * persp * 1.18, 0.0, 1.0);
      gl_PointSize = aSize * (0.55 + 0.95 * persp) * uPixelRatio * 2.4;

      vSeed = aSeed;
      vEnergy = uEnergy;
      vDepth = p.z;
    }
  `;

  const FRAG = `
    precision mediump float;
    varying float vSeed;
    varying float vEnergy;
    varying float vDepth;
    uniform vec3 uColorA;
    uniform vec3 uColorB;
    void main() {
      vec2 d = gl_PointCoord - vec2(0.5);
      float r2 = dot(d, d);
      if (r2 > 0.25) discard;
      float alpha = pow(1.0 - r2 * 4.0, 1.7);
      vec3 col = mix(uColorA, uColorB, vSeed);
      col += vEnergy * 0.45 * vec3(0.55, 0.85, 1.0);
      col += max(0.0, vDepth) * 0.10 * vec3(0.4, 0.7, 1.0);
      gl_FragColor = vec4(col, alpha * 0.9);
    }
  `;

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error("shader compile failed: " + gl.getShaderInfoLog(shader));
    }
    return shader;
  }

  class ParticleCore {
    constructor(canvas, options) {
      options = options || {};
      this.canvas = canvas;
      this.count = Math.max(600, Math.min(options.count || 6000, 60000));
      this.shape = "sphere";
      this.energy = 0;
      this.shear = 0;
      this.collapse = 0;
      this.t = 1;
      this.morphing = false;
      this.colorA = [0.35, 0.82, 1.0];
      this.colorB = [0.61, 0.48, 1.0];
      this.state = { energy: 0, shear: 0, collapse: 0 };
      this.gl = null;
      this.ok = false;
      try {
        this._init();
        this.ok = true;
      } catch (error) {
        this.error = error;
        console.warn("[orbynth] particle core unavailable:", error.message);
      }
    }

    _init() {
      const gl = this.canvas.getContext("webgl", {
        alpha: true, antialias: false, depth: false, premultipliedAlpha: false, powerPreference: "low-power"
      }) || this.canvas.getContext("experimental-webgl");
      if (!gl) throw new Error("WebGL is not available in this browser");
      this.gl = gl;

      const program = gl.createProgram();
      gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERT));
      gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error("program link failed: " + gl.getProgramInfoLog(program));
      }
      gl.useProgram(program);

      this.program = program;
      this.uniforms = {};
      ["uT", "uTime", "uEnergy", "uShear", "uCollapse", "uPixelRatio", "uColorA", "uColorB"].forEach((name) => {
        this.uniforms[name] = gl.getUniformLocation(program, name);
      });
      this.attribs = {
        aPosA: gl.getAttribLocation(program, "aPosA"),
        aPosB: gl.getAttribLocation(program, "aPosB"),
        aSeed: gl.getAttribLocation(program, "aSeed"),
        aSize: gl.getAttribLocation(program, "aSize"),
      };

      this.buffers = {};
      ["aPosA", "aPosB"].forEach((key) => {
        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(gl.ARRAY_BUFFER, this._shape("sphere"), gl.DYNAMIC_DRAW);
        this.buffers[key] = buffer;
      });

      const seeds = new Float32Array(this.count);
      const sizes = new Float32Array(this.count);
      for (let i = 0; i < this.count; i++) {
        seeds[i] = Math.random();
        sizes[i] = 0.7 + Math.random() * 1.5;
      }
      this.seeds = seeds;
      this.buffers.aSeed = this._staticBuffer(seeds);
      this.buffers.aSize = this._staticBuffer(sizes);
      this.shapeA = "sphere";
      this.shapeB = "sphere";
      this.positionsA = this._shape("sphere");
      this.positionsB = this.positionsA.slice(0);

      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE); // additive: the core glows where particles overlap
      gl.disable(gl.DEPTH_TEST);
      gl.clearColor(0, 0, 0, 0);

      this.resize();
    }

    _staticBuffer(data) {
      const gl = this.gl;
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      return buffer;
    }

    _shape(name) {
      const n = this.count;
      const out = new Float32Array(n * 3);
      const golden = Math.PI * (3 - Math.sqrt(5));
      for (let i = 0; i < n; i++) {
        let x = 0, y = 0, z = 0;
        if (name === "sphere") {
          const t = i / Math.max(1, n);
          y = 1 - t * 2;
          const radius = Math.sqrt(Math.max(0, 1 - y * y));
          const theta = golden * i;
          x = Math.cos(theta) * radius;
          z = Math.sin(theta) * radius;
          const shell = 0.86 + Math.random() * 0.14;
          x *= shell; y *= shell; z *= shell;
        } else if (name === "torus") {
          const u = (i / n) * Math.PI * 2 * 7;
          const v = (i / n) * Math.PI * 2;
          const R = 0.72, r = 0.26;
          x = (R + r * Math.cos(v)) * Math.cos(u);
          y = r * Math.sin(v) * 1.1;
          z = (R + r * Math.cos(v)) * Math.sin(u);
        } else if (name === "helix") {
          const t = i / n;
          const angle = t * Math.PI * 2 * 6;
          const radius = 0.28 + t * 0.5;
          x = Math.cos(angle) * radius;
          y = (t - 0.5) * 1.8;
          z = Math.sin(angle) * radius;
        } else if (name === "grid") {
          const side = Math.ceil(Math.sqrt(n));
          const gx = (i % side) / side - 0.5;
          const gy = Math.floor(i / side) / side - 0.5;
          x = gx * 1.7;
          y = gy * 1.7;
          z = Math.sin(gx * 9) * Math.cos(gy * 9) * 0.09;
        } else if (name === "wave") {
          const t = i / n;
          const angle = t * Math.PI * 2;
          const radius = 0.35 + 0.55 * Math.abs(Math.sin(t * Math.PI * 3));
          x = Math.cos(angle) * radius;
          z = Math.sin(angle) * radius;
          y = Math.sin(angle * 6) * 0.35;
        }
        out[i * 3] = x;
        out[i * 3 + 1] = y;
        out[i * 3 + 2] = z;
      }
      return out;
    }

    /* ---- public API ---- */
    setShape(name, instant) {
      if (!this.ok || name === this.shapeB) return;
      if (!["sphere", "torus", "helix", "grid", "wave"].includes(name)) return;
      // Snapshot the current interpolated cloud as the new starting point, so morphs chain
      // smoothly instead of snapping back to the previous shape.
      const current = new Float32Array(this.count * 3);
      const a = this.positionsA, b = this.positionsB, t = this.morphing ? this.t : 1;
      for (let i = 0; i < current.length; i++) current[i] = a[i] + (b[i] - a[i]) * t;
      this.positionsA = current;
      this.positionsB = this._shape(name);
      this.shapeA = this.shapeB;
      this.shapeB = name;
      this.shape = name;
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.aPosA);
      gl.bufferData(gl.ARRAY_BUFFER, this.positionsA, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.aPosB);
      gl.bufferData(gl.ARRAY_BUFFER, this.positionsB, gl.DYNAMIC_DRAW);
      this.t = instant ? 1 : 0;
      this.morphing = !instant;
      this.morphStart = performance.now();
      this.morphMs = 1200;
    }

    setEnergy(value) { this.state.energy = Math.max(0, Math.min(1, value)); }
    setShear(value) { this.state.shear = Math.max(0, Math.min(1, value)); }
    pulseShear(amount) { this.setShear(Math.min(1, this.state.shear + (amount || 0.5))); }
    pulseEnergy(amount) { this.setEnergy(Math.min(1, this.state.energy + (amount || 0.5))); }
    collapse() { this.state.collapse = 1; }
    reseed() { this.state.collapse = 0; this.setShape("sphere"); }
    setColors(a, b) { if (a) this.colorA = a; if (b) this.colorB = b; }

    resize() {
      if (!this.ok) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = this.canvas.clientWidth || window.innerWidth;
      const height = this.canvas.clientHeight || window.innerHeight;
      this.canvas.width = Math.floor(width * ratio);
      this.canvas.height = Math.floor(height * ratio);
      this.pixelRatio = ratio;
      this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    }

    frame(now) {
      if (!this.ok) return;
      const gl = this.gl;
      const time = now / 1000;

      if (this.morphing) {
        const progress = Math.min(1, (now - this.morphStart) / this.morphMs);
        this.t = progress < 0.5 ? 2 * progress * progress : 1 - Math.pow(-2 * progress + 2, 2) / 2;
        if (progress >= 1) { this.morphing = false; this.t = 1; }
      }
      // energy and shear decay back to rest
      this.state.energy += (0 - this.state.energy) * 0.045;
      this.state.shear += (0 - this.state.shear) * 0.05;
      this.state.collapse += (0 - this.state.collapse) * 0.06;

      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(this.program);

      const bind = (name, size) => {
        const location = this.attribs[name];
        if (location < 0) return;
        gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers[name]);
        gl.enableVertexAttribArray(location);
        gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
      };
      bind("aPosA", 3);
      bind("aPosB", 3);
      bind("aSeed", 1);
      bind("aSize", 1);

      gl.uniform1f(this.uniforms.uT, this.t);
      gl.uniform1f(this.uniforms.uTime, time);
      gl.uniform1f(this.uniforms.uEnergy, this.state.energy);
      gl.uniform1f(this.uniforms.uShear, this.state.shear);
      gl.uniform1f(this.uniforms.uCollapse, this.state.collapse);
      gl.uniform1f(this.uniforms.uPixelRatio, this.pixelRatio || 1);
      gl.uniform3fv(this.uniforms.uColorA, this.colorA);
      gl.uniform3fv(this.uniforms.uColorB, this.colorB);

      gl.drawArrays(gl.POINTS, 0, this.count);
    }

    dispose() {
      if (!this.ok) return;
      const gl = this.gl;
      Object.values(this.buffers).forEach((buffer) => gl.deleteBuffer(buffer));
      gl.deleteProgram(this.program);
    }
  }

  window.ParticleCore = ParticleCore;
})();
