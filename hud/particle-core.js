/* ZYNTHARO particle core — hand-written WebGL, no dependencies.
 *
 * One instrument, four bodies, all drawn in a real perspective camera (no fake depth):
 *
 *   globe   — a banded shell of thousands of dots around a dim inner dust volume, with a
 *             handful of bright sparks. Each dot carries its own axial rate, so the body
 *             shears like a fluid planet instead of spinning as one rigid shell.
 *   rings   — five ringlets plus wispy tendrils in the equatorial plane, differentially
 *             rotating: inner bands race, outer bands trail. A few seeds run warm (orange),
 *             which is what stops the instrument reading as a single-hue wireframe.
 *   orbits  — four arcs in crossing planes, thin and slightly broken, drawn warm.
 *   jets    — two faint cones along the polar axis.
 *
 * Every particle is simulated in the vertex shader from uniforms, so the CPU cost per frame is
 * two draw calls and a handful of floats: this has to run on a laptop without a fan spinning
 * up. Topologies still morph (sphere / torus / helix / grid / wave) — the bodies morph, the
 * ring system stays where physics would put it.
 *
 * Lightweight by design: four cores or a reduced-motion preference halves the particle budget
 * and drops the glow pass.
 */

(function () {
  "use strict";

  /* Camera and shape budget. Focal/height are tuned so the globe plus its outer ringlets fill
   * roughly two-thirds of the viewport height at any window size — the composition in the
   * design is a function of the frame, not of the pixel count. */
  const CAM_Z = 2.68;
  const FOCAL = 1.34;
  const TILT = 0.40;
  const RINGLET_RADII = [1.14, 1.26, 1.38, 1.52, 1.64];
  const RINGLET_WIDTH = [0.010, 0.018, 0.012, 0.030, 0.052];
  const ORBIT_ARCS = 4;

  const ROTATIONS = `
    vec3 spin_y(vec3 p, float a) { float s = sin(a), c = cos(a); return vec3(p.x * c - p.z * s, p.y, p.x * s + p.z * c); }
    vec3 tilt_x(vec3 p, float a) { float s = sin(a), c = cos(a); return vec3(p.x, p.y * c - p.z * s, p.y * s + p.z * c); }
  `;

  /* ------------------------------------------------------------------ globe (morphing body) */

  const BODY_VERT = ROTATIONS + `
    attribute vec3 aPosA;
    attribute vec3 aPosB;
    attribute float aSeed;
    attribute float aSize;
    attribute float aBand;      // 0 shell, 1 inner dust, 2 spark, 3 rim lattice
    uniform float uT, uTime, uEnergy, uShear, uCollapse;
    uniform float uPixelRatio, uPixelScale, uPulse, uPulseR, uBloom, uSpin, uTilt, uFocal, uCamZ;
    uniform float uAurora, uGlitch;   // sustained-state bands, and interference tearing
    uniform vec2 uRes;
    varying float vSeed, vEnergy, vBand, vFar, vAlpha;

    float band_alpha(float band) {
      if (band < 0.5) return 0.60;   // shell: the visible surface
      if (band < 1.5) return 0.30;   // inner dust: volume, not surface
      if (band < 2.5) return 1.00;   // spark
      return 0.82;                   // rim lattice
    }

    void main() {
      vec3 p = mix(aPosA, aPosB, uT);

      // per-dot axial rate: the globe shears instead of turning as a rigid body
      float rate = 0.055 + aSeed * 0.05;
      p = spin_y(p, uSpin + uTime * rate + uShear * 1.15);

      float r = length(p);

      // speaking: a radial wave travelling outward through the cloud
      p *= 1.0 + uEnergy * 0.22 * sin(r * 7.5 - uTime * 6.0 + aSeed * 3.0);

      // tool use: the body pulls along one axis, as if work is being dragged through it
      p.x += uShear * 0.20 * sin(aSeed * 12.0 + uTime * 2.0);
      p.y -= uShear * 0.16 * (aSeed - 0.5);

      // error: collapse inward, then re-seed outward
      p *= 1.0 - uCollapse * (0.50 + 0.40 * aSeed);

      // three incommensurate fields, so the cloud never repeats perceptibly
      p += 0.020 * vec3(
        sin(uTime * 0.71 + aSeed * 31.0),
        sin(uTime * 0.63 + aSeed * 57.0),
        sin(uTime * 0.83 + aSeed * 13.0)
      );

      // shockwave: a travelling ring, used for reports, installs and confirmations
      float shock = exp(-pow((r - uPulseR) * 5.0, 2.0)) * uPulse;
      p *= 1.0 + shock * 0.40;

      // breathing
      p *= 1.0 + 0.015 * sin(uTime * 0.9 + aSeed * 4.0);

      // interference: a torn transmission. Only a couple of percent of dots shift, so it reads as
      // a fault in the signal rather than as a change of shape.
      p.x += uGlitch * 0.10 * step(0.982, fract(aSeed * 91.7 + uTime * 0.7));

      vec3 v = tilt_x(p, uTilt);
      float depth = uCamZ - v.z;
      float f = uFocal / max(0.35, depth);
      float aspect = uRes.x / max(1.0, uRes.y);
      gl_Position = vec4(v.x * f / aspect, v.y * f, 0.0, 1.0);

      float size = aSize * (0.70 + 0.55 * step(1.5, aBand));
      gl_PointSize = clamp(size * uPixelScale * f * (1.0 + uBloom * 2.4 + shock * 1.6) * uPixelRatio, 1.0, 46.0);

      vSeed = aSeed;
      vEnergy = uEnergy;
      vBand = aBand;
      // depth fog: the far hemisphere dims but never disappears, so the globe stays readable
      // as a solid object rather than a silhouette
      vFar = clamp((depth - (uCamZ - 1.05)) / 2.1, 0.0, 1.0);
      // aurora: travelling latitudinal bands. This is the sustained-state effect — pressure burns
      // the globe from the inside, an alert pulses it, calm barely shows it at all.
      float aurora = 0.5 + 0.5 * sin(p.y * 5.2 - uTime * 1.5 + aSeed * 2.2);
      vAlpha = band_alpha(aBand) * (1.0 - uCollapse * 0.35) * (1.0 - uAurora * (1.0 - aurora) * 0.60);
    }
  `;

  const BODY_FRAG = `
    precision mediump float;
    varying float vSeed, vEnergy, vBand, vFar, vAlpha;
    uniform vec3 uColorA, uColorB, uWarm;
    uniform float uBloom;

    void main() {
      vec2 d = gl_PointCoord - vec2(0.5);
      float r2 = dot(d, d);
      if (r2 > 0.25) discard;
      float core = pow(max(0.0, 1.0 - r2 * 4.0), 1.55);

      vec3 col = vBand < 0.5 ? uColorA : (vBand < 1.5 ? uColorB : vec3(1.0, 1.0, 1.0));
      // a small fraction runs warm: the blue/orange duality, dense near the rim
      float warm = smoothstep(0.94, 1.0, vSeed) * (1.0 - step(1.5, vBand));
      col = mix(col, uWarm, warm * 0.85);

      col *= mix(1.0, 0.40, vFar);
      col += vEnergy * 0.30 * vec3(0.45, 0.82, 1.0);

      // the glow pass is wide and faint: two additive draws read as bloom without a
      // post-processing pipeline, which is what keeps this cheap on integrated graphics
      gl_FragColor = vec4(col, core * vAlpha * mix(1.0, 0.22, uBloom));
    }
  `;

  /* ------------------------------------------- rings, orbit arcs and jets (static geometry) */

  const RING_VERT = ROTATIONS + `
    attribute vec3 aPos;
    attribute float aSeed;
    attribute float aSize;
    attribute float aKind;      // 0 ringlet, 1 tendril, 2 orbit arc, 3 polar jet
    uniform float uTime, uSpin, uTilt, uEnergy, uCollapse;
    uniform float uPixelRatio, uPixelScale, uPulse, uPulseR, uBloom, uFocal, uCamZ;
    uniform float uAurora, uGlitch;
    uniform vec2 uRes;
    varying float vSeed, vEnergy, vBand, vFar, vAlpha;

    float kind_alpha(float kind) {
      if (kind < 0.5) return 0.90;   // ringlet
      if (kind < 1.5) return 0.44;   // tendril
      if (kind < 2.5) return 0.72;   // orbit arc
      return 0.26;                   // polar jet
    }

    void main() {
      vec3 p = aPos;
      float radius = max(0.25, length(p.xz));

      // differential rotation: inner ringlets race, outer ones trail. This shear is the whole
      // difference between a ring system and a painted ellipse.
      float rate = 0.34 / radius + aSeed * 0.012;
      float drag = 1.0 - 0.35 * step(1.5, aKind);
      p = spin_y(p, uSpin * 1.35 + uTime * rate * drag);

      p *= 1.0 + uEnergy * 0.05 * sin(radius * 9.0 - uTime * 5.0 + aSeed * 6.0);
      p *= 1.0 - uCollapse * 0.35;
      p *= 1.0 + exp(-pow((radius - uPulseR) * 5.5, 2.0)) * uPulse * 0.5;

      vec3 v = tilt_x(p, uTilt);
      float depth = uCamZ - v.z;
      float f = uFocal / max(0.35, depth);
      float aspect = uRes.x / max(1.0, uRes.y);
      gl_Position = vec4(v.x * f / aspect, v.y * f, 0.0, 1.0);

      gl_PointSize = clamp(aSize * uPixelScale * f * (1.0 + uBloom * 2.4) * uPixelRatio, 1.0, 46.0);

      vSeed = aSeed;
      vEnergy = uEnergy;
      vBand = aKind;
      vFar = clamp((depth - (uCamZ - 1.05)) / 2.1, 0.0, 1.0);
      // the ring system carries the same sustained bands as the globe, plus the fault dimming
      float aurora = 0.5 + 0.5 * sin(radius * 4.6 - uTime * 1.5 + aSeed * 3.0);
      vAlpha = kind_alpha(aKind) * (1.0 - uAurora * (1.0 - aurora) * 0.50) * (1.0 - uGlitch * 0.25);
    }
  `;

  const RING_FRAG = `
    precision mediump float;
    varying float vSeed, vEnergy, vBand, vFar, vAlpha;
    uniform vec3 uColorA, uColorB, uWarm;
    uniform float uBloom;

    void main() {
      vec2 d = gl_PointCoord - vec2(0.5);
      float r2 = dot(d, d);
      if (r2 > 0.25) discard;
      float core = pow(max(0.0, 1.0 - r2 * 4.0), 1.5);

      vec3 col = uColorA;
      col = mix(col, vec3(0.96, 0.99, 1.0), step(vBand, 0.5));            // ringlets: near white
      col = mix(col, uColorB, step(0.5, vBand) * step(vBand, 1.5));        // tendrils: cool violet
      col = mix(col, uWarm, step(1.5, vBand) * step(vBand, 2.5));          // orbit arcs: warm
      col = mix(col, vec3(0.55, 0.85, 1.0), step(2.5, vBand));             // jets: pale cyan

      // warm sparks scattered through the ring, the way the reference plate shows them
      col = mix(col, uWarm, smoothstep(0.955, 1.0, vSeed) * 0.85);

      col *= mix(1.0, 0.45, vFar);
      col += vEnergy * 0.25 * vec3(0.45, 0.82, 1.0);

      gl_FragColor = vec4(col, core * vAlpha * mix(1.0, 0.24, uBloom));
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

  function link(gl, vertexSource, fragmentSource) {
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, vertexSource));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, fragmentSource));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error("program link failed: " + gl.getProgramInfoLog(program));
    }
    return program;
  }

  class ParticleCore {
    constructor(canvas, options) {
      options = options || {};
      this.canvas = canvas;
      this.lowPower =
        (typeof navigator !== "undefined" && (navigator.hardwareConcurrency || 8) <= 4) ||
        (typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

      this.bodyCount = Math.max(600, Math.min(options.count || 6000, 60000));
      if (this.lowPower) this.bodyCount = Math.min(this.bodyCount, 2600);
      // the instrument is globe plus ring system; the ring budget rides along with the globe
      this.satelliteCount = Math.round(this.bodyCount * 0.52);
      if (this.lowPower) this.satelliteCount = Math.round(this.satelliteCount * 0.5);

      this.shape = "sphere";
      this.t = 1;
      this.morphing = false;
      this.pulse = 0;
      this.pulseRadius = 0.15;
      this.spin = 0;
      this.spinRate = 1;      // set by the effects director per situation
      this.tilt = TILT;
      this.tiltBias = 0;
      this.lastNow = 0;
      this.aurora = 0;        // sustained banding (pressure, embers, alerts)
      this.glitch = 0;        // interference tearing (faults, failovers)
      this.state = { energy: 0, shear: 0, collapse: 0 };
      this.colorA = [0.62, 0.90, 1.0];   // icy shell
      this.colorB = [0.30, 0.48, 1.0];   // inner dust volume
      this.warm = [1.0, 0.52, 0.16];     // the orange counter-element
      this.gl = null;
      this.ok = false;
      try {
        this._init();
        this.ok = true;
      } catch (error) {
        this.error = error;
        console.warn("[zyntharo] particle core unavailable:", error.message);
      }
    }

    /* ------------------------------------------------------------------ setup */
    _init() {
      const gl = this.canvas.getContext("webgl", {
        alpha: true, antialias: false, depth: false, premultipliedAlpha: false, powerPreference: "low-power",
      }) || this.canvas.getContext("experimental-webgl");
      if (!gl) throw new Error("WebGL is not available in this browser");
      this.gl = gl;

      this.body = this._program(BODY_VERT, BODY_FRAG, {
        attributes: {
          aPosA: 3, aPosB: 3, aSeed: 1, aSize: 1, aBand: 1,
        },
        uniforms: [
          "uT", "uTime", "uEnergy", "uShear", "uCollapse", "uPixelRatio", "uPixelScale",
          "uPulse", "uPulseR", "uBloom", "uSpin", "uTilt", "uFocal", "uCamZ", "uRes",
          "uAurora", "uGlitch",
          "uColorA", "uColorB", "uWarm",
        ],
      });
      this.ring = this._program(RING_VERT, RING_FRAG, {
        attributes: { aPos: 3, aSeed: 1, aSize: 1, aKind: 1 },
        uniforms: [
          "uTime", "uSpin", "uTilt", "uEnergy", "uCollapse", "uPixelRatio", "uPixelScale",
          "uPulse", "uPulseR", "uBloom", "uFocal", "uCamZ", "uRes",
          "uAurora", "uGlitch", "uColorA", "uColorB", "uWarm",
        ],
      });

      // globe: shell + dust + sparks + rim lattice, all in one interleaved set of buffers
      this.globe = this._globeBuffers();
      this.satellites = this._satelliteBuffers();
      // morph targets are full copies of the globe positions; the ring never morphs
      this.positionsA = this.globe.spawn.slice(0);
      this.positionsB = this.globe.spawn.slice(0);
      this.globe.aPosA = this._floatBuffer(this.positionsA);
      this.globe.aPosB = this._floatBuffer(this.positionsB);

      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE); // additive: the core glows where particles overlap
      gl.disable(gl.DEPTH_TEST);
      gl.clearColor(0, 0, 0, 0);

      this.resize();
    }

    _program(vertexSource, fragmentSource, layout) {
      const gl = this.gl;
      const program = link(gl, vertexSource, fragmentSource);
      gl.useProgram(program);
      const uniforms = {};
      layout.uniforms.forEach((name) => { uniforms[name] = gl.getUniformLocation(program, name); });
      const attributes = {};
      Object.keys(layout.attributes).forEach((name) => {
        attributes[name] = gl.getAttribLocation(program, name);
      });
      return { program: program, uniforms: uniforms, attributes: attributes, sizes: layout.attributes, buffers: {} };
    }

    _floatBuffer(data) {
      const gl = this.gl;
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      return buffer;
    }

    _dynamicBuffer(data) {
      const gl = this.gl;
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
      return buffer;
    }

    /* A banded globe: a dense shell, a dim interior volume, bright sparks and a thin lattice
     * at the very rim. The banding is what makes dense regions read as structure rather than
     * as noise — a uniform sphere of dots looks like fog. */
    _globeBuffers() {
      const n = this.bodyCount;
      const spawn = this._shape("sphere");
      const seeds = new Float32Array(n);
      const sizes = new Float32Array(n);
      const bands = new Float32Array(n);
      const radii = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const x = spawn[i * 3], y = spawn[i * 3 + 1], z = spawn[i * 3 + 2];
        radii[i] = Math.sqrt(x * x + y * y + z * z);
        seeds[i] = Math.random();
        bands[i] = radii[i] >= 0.93 ? 0 : 1;         // shell or inner dust
        // sparks are spread evenly (one in seventeen) rather than scattered at random: evenly
        // spread they glint across the whole body, drawn at random they clump and read as noise
        if (i % 12 === 0) bands[i] = 2;
      }
      // The rim lattice is the brightest band, so it must never come out empty: the outermost
      // few percent of points are assigned to it deterministically.
      const order = Array.from({ length: n }, (unused, index) => index).sort((a, b) => radii[b] - radii[a]);
      const rim = Math.max(8, Math.round(n * 0.04));
      for (let k = 0; k < rim; k++) bands[order[k]] = 3;
      for (let i = 0; i < n; i++) {
        sizes[i] = bands[i] === 2 ? 1.9 + Math.random() * 0.9 : 0.62 + Math.random() * 0.95;
      }
      return {
        spawn: spawn,
        count: n,
        aSeed: this._floatBuffer(seeds),
        aSize: this._floatBuffer(sizes),
        aBand: this._floatBuffer(bands),
      };
    }

    /* Rings, orbit arcs and jets. Built once, in ring-local space: the shader rotates the whole
     * assembly onto the globe's equatorial plane, so the ring can never drift out of alignment
     * with the body it belongs to. */
    _satelliteBuffers() {
      const n = this.satelliteCount;
      const positions = new Float32Array(n * 3);
      const seeds = new Float32Array(n);
      const sizes = new Float32Array(n);
      const kinds = new Float32Array(n);
      const ringlets = Math.round(n * 0.44);
      const tendrils = Math.round(n * 0.20);
      const orbits = Math.round(n * 0.26);
      // jets fill the remainder
      const random = () => Math.random() * 2 - 1;

      for (let i = 0; i < n; i++) {
        let x = 0, y = 0, z = 0, kind = 0, size = 1;
        if (i < ringlets) {
          const band = Math.min(RINGLET_RADII.length - 1, Math.floor(Math.random() * RINGLET_RADII.length));
          const radius = RINGLET_RADII[band] + random() * RINGLET_WIDTH[band];
          const angle = Math.random() * Math.PI * 2;
          kind = 0;
          size = 0.7 + Math.random() * 0.8;
          x = Math.cos(angle) * radius;
          z = Math.sin(angle) * radius;
          y = random() * 0.004;
        } else if (i < ringlets + tendrils) {
          // wisps that cross the bands instead of sitting in them: this is what gives the ring
          // its streaked texture rather than five clean circles
          const t = Math.random();
          const radius = 1.10 + t * 0.62 + random() * 0.02;
          const angle = Math.random() * Math.PI * 2 + t * 2.2;
          kind = 1;
          size = 0.55 + Math.random() * 0.7;
          x = Math.cos(angle) * radius;
          z = Math.sin(angle) * radius;
          y = random() * 0.02;
        } else if (i < ringlets + tendrils + orbits) {
          // four arcs in deliberately different planes, each spanning part of its circle, so the
          // assembly reads as orbiting traffic rather than as decoration
          const arc = Math.floor(Math.random() * ORBIT_ARCS);
          const radius = 1.24 + arc * 0.14 + random() * 0.006;
          const span = 2.6 + arc * 0.5;
          const offset = arc * 1.9;
          const angle = offset + (Math.random() - 0.5) * span;
          const plane = 0.25 + arc * 0.34;
          kind = 2;
          size = 0.6 + Math.random() * 0.9;
          const px = Math.cos(angle) * radius;
          const pz = Math.sin(angle) * radius;
          // bake the plane tilt into the point itself: sharing one uniform would flatten every
          // arc into the same plane
          x = px;
          y = pz * Math.sin(plane);
          z = pz * Math.cos(plane) * Math.cos(plane * 0.7);
        } else {
          // polar jets: two sparse cones along the axis
          const side = Math.random() < 0.5 ? -1 : 1;
          const t = Math.pow(Math.random(), 0.7);
          const radius = 0.06 + t * 0.34;
          const angle = Math.random() * Math.PI * 2;
          kind = 3;
          size = 0.5 + Math.random() * 0.7;
          x = Math.cos(angle) * radius;
          z = Math.sin(angle) * radius;
          y = side * (1.02 + t * 0.85);
        }
        positions[i * 3] = x;
        positions[i * 3 + 1] = y;
        positions[i * 3 + 2] = z;
        seeds[i] = Math.random();
        sizes[i] = size;
        kinds[i] = kind;
      }
      return {
        count: n,
        aPos: this._floatBuffer(positions),
        aSeed: this._floatBuffer(seeds),
        aSize: this._floatBuffer(sizes),
        aKind: this._floatBuffer(kinds),
      };
    }

    /* ------------------------------------------------------------------ geometry */
    _shape(name) {
      const n = this.bodyCount;
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
          // latitudinal banding plus a shell/volume split: even coverage with visible structure.
          // The shell runs out to 1.0 so the rim lattice has a surface to sit on.
          const belt = 0.975 + 0.02 * Math.sin(y * 11.0);
          const shell = Math.random() < 0.62
            ? belt * (0.955 + 0.05 * Math.random())
            : Math.pow(Math.random(), 0.45) * 0.93;
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

    /* ------------------------------------------------------------------ public API */
    setShape(name, instant) {
      if (!this.ok || name === this.shape) return;
      if (["sphere", "torus", "helix", "grid", "wave"].indexOf(name) < 0) return;
      const gl = this.gl;
      const count = this.bodyCount;
      // snapshot the current interpolated cloud, so morphs chain smoothly instead of snapping
      const current = new Float32Array(count * 3);
      const a = this.positionsA, b = this.positionsB, t = this.morphing ? this.t : 1;
      for (let i = 0; i < current.length; i++) current[i] = a[i] + (b[i] - a[i]) * t;
      this.positionsA = current;
      this.positionsB = this._shape(name);
      this.shape = name;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.globe.aPosA);
      gl.bufferData(gl.ARRAY_BUFFER, this.positionsA, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.globe.aPosB);
      gl.bufferData(gl.ARRAY_BUFFER, this.positionsB, gl.DYNAMIC_DRAW);
      this.t = instant ? 1 : 0;
      this.morphing = !instant;
      this.morphStart = performance.now();
      this.morphMs = 1200;
    }

    setEnergy(value) { this.state.energy = Math.max(0, Math.min(1, value)); }
    setShear(value) { this.state.shear = Math.max(0, Math.min(1, value)); }
    pulseShear(amount) { this.setShear(Math.min(1, this.state.shear + (amount || 0.5))); }
    /** Energy burst plus a travelling shockwave — used for reports, installs and wakes. */
    pulseEnergy(amount) {
      this.setEnergy(Math.min(1, this.state.energy + (amount || 0.5)));
      this.pulseRing(Math.min(1, (amount || 0.5) + 0.35));
    }
    pulseRing(amount) {
      this.pulse = Math.max(this.pulse, Math.min(1, amount || 0.6));
      this.pulseRadius = 0.15;
    }
    collapse() { this.state.collapse = 1; }
    reseed() { this.state.collapse = 0; this.setShape("sphere"); }
    setColors(a, b, warm) {
      if (a) this.colorA = a;
      if (b) this.colorB = b;
      if (warm) this.warm = warm;
    }

    /** Sustained banding, fed every frame by the effects director. */
    setAurora(value) { this.aurora = Math.max(0, Math.min(1.5, value)); }
    /** Interference tearing, for faults and failovers. */
    setGlitch(value) { this.glitch = Math.max(0, Math.min(1.5, value)); }
    /** Globe rotation rate: how fast the situation runs. */
    setSpinRate(value) { this.spinRate = Math.max(0, Math.min(4, value)); }
    /** A tilt offset, so different situations hold the globe at different attitudes. */
    setTiltBias(value) { this.tiltBias = Math.max(-0.5, Math.min(0.5, value)); }

    resize() {
      if (!this.ok) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = this.canvas.clientWidth || window.innerWidth;
      const height = this.canvas.clientHeight || window.innerHeight;
      this.canvas.width = Math.floor(width * ratio);
      this.canvas.height = Math.floor(height * ratio);
      this.pixelRatio = ratio;
      this.resolution = [width, height];
      // size in device pixels is a fraction of the frame, so the composition survives resizing
      this.pixelScale = Math.max(2.0, Math.min(height, width) * 0.0055);
      this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    }

    _bind(target) {
      const gl = this.gl;
      Object.keys(target.attributes).forEach((name) => {
        const location = target.attributes[name];
        if (location < 0 || !target[name]) return;
        gl.bindBuffer(gl.ARRAY_BUFFER, target[name]);
        gl.enableVertexAttribArray(location);
        gl.vertexAttribPointer(location, target.sizes[name], gl.FLOAT, false, 0, 0);
      });
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
      // energy, shear and collapse all decay back to rest
      this.state.energy += (0 - this.state.energy) * 0.045;
      this.state.shear += (0 - this.state.shear) * 0.05;
      this.state.collapse += (0 - this.state.collapse) * 0.06;
      this.pulse += (0 - this.pulse) * 0.035;
      this.pulseRadius += 0.045 * (1.0 + this.pulse);
      // spin accumulates from real elapsed time so the director can drive its rate; a phase would
      // jump whenever the rate changed.
      const delta = this.lastNow ? Math.min(0.1, (now - this.lastNow) / 1000) : 0.016;
      this.lastNow = now;
      this.spin += delta * 0.05 * this.spinRate;
      this.aurora += (0 - this.aurora) * 0.02;
      this.glitch += (0 - this.glitch) * 0.06;
      // a slow tilt breathing keeps the plate from ever looking like a fixed photograph
      this.tilt = TILT + 0.03 * Math.sin(time * 0.17) + this.tiltBias;

      gl.clear(gl.COLOR_BUFFER_BIT);
      const pass = (bloom) => {
        this._drawBody(time, bloom);
        this._drawSatellites(time, bloom);
      };
      if (this.lowPower) pass(0);
      else { pass(0); pass(1); }   // crisp pass + wide glow pass = bloom without a framebuffer
    }

    _commonUniforms(target, time, bloom) {
      const gl = this.gl;
      const u = target.uniforms;
      const set = (name, fn) => { if (u[name]) fn(u[name]); };
      set("uTime", (l) => gl.uniform1f(l, time));
      set("uEnergy", (l) => gl.uniform1f(l, this.state.energy));
      set("uCollapse", (l) => gl.uniform1f(l, this.state.collapse));
      set("uPixelRatio", (l) => gl.uniform1f(l, this.pixelRatio || 1));
      set("uPixelScale", (l) => gl.uniform1f(l, this.pixelScale || 3));
      set("uPulse", (l) => gl.uniform1f(l, this.pulse));
      set("uPulseR", (l) => gl.uniform1f(l, this.pulseRadius));
      set("uBloom", (l) => gl.uniform1f(l, bloom));
      set("uSpin", (l) => gl.uniform1f(l, this.spin));
      set("uTilt", (l) => gl.uniform1f(l, this.tilt));
      set("uFocal", (l) => gl.uniform1f(l, FOCAL));
      set("uCamZ", (l) => gl.uniform1f(l, CAM_Z));
      set("uRes", (l) => gl.uniform2f(l, (this.resolution || [1, 1])[0], (this.resolution || [1, 1])[1]));
      set("uAurora", (l) => gl.uniform1f(l, this.aurora));
      set("uGlitch", (l) => gl.uniform1f(l, this.glitch));
      set("uColorA", (l) => gl.uniform3fv(l, this.colorA));
      set("uColorB", (l) => gl.uniform3fv(l, this.colorB));
      set("uWarm", (l) => gl.uniform3fv(l, this.warm));
    }

    _drawBody(time, bloom) {
      const gl = this.gl;
      const target = this.body;
      gl.useProgram(target.program);
      const buffers = target.buffers;
      buffers.aPosA = this.globe.aPosA;
      buffers.aPosB = this.globe.aPosB;
      buffers.aSeed = this.globe.aSeed;
      buffers.aSize = this.globe.aSize;
      buffers.aBand = this.globe.aBand;
      this._bind(Object.assign({}, target, buffers));
      this._commonUniforms(target, time, bloom);
      if (target.uniforms.uT) gl.uniform1f(target.uniforms.uT, this.t);
      if (target.uniforms.uShear) gl.uniform1f(target.uniforms.uShear, this.state.shear);
      gl.drawArrays(gl.POINTS, 0, this.globe.count);
    }

    _drawSatellites(time, bloom) {
      const gl = this.gl;
      const target = this.ring;
      gl.useProgram(target.program);
      const buffers = {
        aPos: this.satellites.aPos, aSeed: this.satellites.aSeed,
        aSize: this.satellites.aSize, aKind: this.satellites.aKind,
      };
      this._bind(Object.assign({}, target, buffers));
      this._commonUniforms(target, time, bloom);
      gl.drawArrays(gl.POINTS, 0, this.satellites.count);
    }

    dispose() {
      if (!this.ok) return;
      const gl = this.gl;
      [this.body, this.ring].forEach((target) => {
        Object.values(target.buffers).forEach((buffer) => { if (buffer) gl.deleteBuffer(buffer); });
        gl.deleteProgram(target.program);
      });
      [this.globe, this.satellites].forEach((set) => {
        Object.keys(set).forEach((key) => {
          if (set[key] && typeof set[key] === "object" && set[key].constructor === WebGLBuffer) gl.deleteBuffer(set[key]);
        });
      });
    }
  }

  window.ParticleCore = ParticleCore;
})();
