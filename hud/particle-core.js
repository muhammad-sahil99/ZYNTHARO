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
  const CAM_Z = 2.02;
  const FOCAL = 1.34;
  const TILT = 0.40;
  /* Every topology the cloud can hold. Kept in one list because the menu, the state machine and
   * the geometry generator must agree: a name in only one of them is a shape that never appears. */
  const SHAPES = [
    "sphere", "saturn", "galaxy", "torus", "helix", "dna", "knot",
    "cube", "cone", "atom", "grid", "wave", "burst",
    /* Two faces, not one: the head field is the same function with a different bone structure,
       and the sex is the only parameter the user should have to choose. */
    "face", "face-m"
  ];

  const RINGLET_RADII = [1.10, 1.16, 1.22, 1.28, 1.34];
  const RINGLET_WIDTH = [0.006, 0.010, 0.007, 0.016, 0.030];
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
    attribute float aPart;      // face region (0 when the cloud is not a face)
    attribute float aShade;     // face lighting, baked from the head field's own gradient
    uniform float uT, uTime, uEnergy, uShear, uCollapse;
    uniform float uFace, uJaw, uSmile, uBrow, uBlink;
    uniform vec2 uGaze;
    uniform float uPixelRatio, uPixelScale, uPulse, uPulseR, uBloom, uSpin, uTilt, uFocal, uCamZ;
    uniform float uAurora, uGlitch;   // sustained-state bands, and interference tearing
    uniform vec2 uRes;
    varying float vSeed, vEnergy, vBand, vFar, vAlpha, vPart, vShade;

    float band_alpha(float band) {
      if (band < 0.5) return 0.46;   // shell: the visible surface
      if (band < 1.5) return 0.22;   // inner dust: volume, not surface
      if (band < 2.5) return 1.00;   // spark
      return 0.62;                   // rim lattice
    }

    void main() {
      vec3 p = mix(aPosA, aPosB, uT);

      /* ------------------------------------------------ face
         The same cloud, holding a head. Everything that makes the other topologies interesting -
         spin, tilt, drift, shear, the shockwave - is turned off here, because a head that
         tumbles is an object and not a presence. What moves instead is the face: jaw, mouth
         corners, brows, lids, and the eyes. All of it is a function of one uniform per muscle,
         so an expression costs nothing and can change on any frame. */
      float face = uFace;
      if (face > 0.001) {
        float lowerLip = step(3.5, aPart) * step(aPart, 4.5) * step(p.y, -0.368);
        float cavity = step(6.5, aPart);
        float chin = smoothstep(-0.40, -0.62, p.y);
        // the jaw drops the lower lip and the chin, and the cavity behind them opens with it
        float open = face * uJaw;
        p.y -= open * (0.078 * lowerLip + 0.050 * cavity + 0.058 * chin);
        p.z -= open * 0.022 * (lowerLip + cavity);

        // a smile is the corners going up, and the cheeks rising with them
        float corner = exp(-pow((abs(p.x) - 0.132) / 0.082, 2.0)) * exp(-pow((p.y + 0.356) / 0.066, 2.0));
        p.y += face * uSmile * 0.052 * corner;
        p.x += face * uSmile * 0.014 * sign(p.x) * corner;
        float cheek = exp(-pow((abs(p.x) - 0.238) / 0.108, 2.0)) * exp(-pow((p.y + 0.190) / 0.104, 2.0));
        p.y += face * uSmile * 0.020 * cheek;

        // brows: the drawn line and the ridge under it move together
        float browPart = step(4.5, aPart) * step(aPart, 5.5);
        float browSkin = exp(-pow((abs(p.x) - 0.180) / 0.120, 2.0)) * exp(-pow((p.y - 0.248) / 0.062, 2.0));
        p.y += face * uBrow * (0.034 * browPart + 0.018 * browSkin);

        // blink: the eye collapses onto the lid line, which is what a closing eye does
        float eyePart = step(0.5, aPart) * step(aPart, 3.5);
        p.y = mix(p.y, 0.100, face * uBlink * eyePart * 0.96);

        // gaze: only the iris and the pupil travel inside the eye
        float iris = step(1.5, aPart) * step(aPart, 3.5);
        p.x += face * iris * uGaze.x * 0.013;
        p.y += face * iris * uGaze.y * 0.011;

        // the head turns a little toward whatever the eyes are on
        p = spin_y(p, face * uGaze.x * 0.15);
        p = tilt_x(p, face * uGaze.y * 0.09);
      }

      // per-dot axial rate: the globe shears instead of turning as a rigid body
      float life = 1.0 - face;
      float rate = 0.055 + aSeed * 0.05;
      p = spin_y(p, (uSpin + uTime * rate + uShear * 1.15) * life);

      float r = length(p);

      // speaking: a radial wave travelling outward through the cloud
      p *= 1.0 + uEnergy * 0.22 * life * sin(r * 7.5 - uTime * 6.0 + aSeed * 3.0);

      // tool use: the body pulls along one axis, as if work is being dragged through it
      p.x += uShear * 0.20 * life * sin(aSeed * 12.0 + uTime * 2.0);
      p.y -= uShear * 0.16 * life * (aSeed - 0.5);

      // error: collapse inward, then re-seed outward
      p *= 1.0 - uCollapse * life * (0.50 + 0.40 * aSeed);

      // three incommensurate fields, so the cloud never repeats perceptibly. A face keeps only a
      // trace of it: a head that shimmers is a rendering artefact, not a presence.
      p += 0.020 * life * vec3(
        sin(uTime * 0.71 + aSeed * 31.0),
        sin(uTime * 0.63 + aSeed * 57.0),
        sin(uTime * 0.83 + aSeed * 13.0)
      );

      // shockwave: a travelling ring, used for reports, installs and confirmations
      float shock = exp(-pow((r - uPulseR) * 5.0, 2.0)) * uPulse;
      p *= 1.0 + shock * 0.40 * life;

      // breathing
      p *= 1.0 + 0.013 * sin(uTime * 0.9 + aSeed * 4.0) * mix(1.0, 0.35, face);

      // interference: a torn transmission. Only a couple of percent of dots shift, so it reads as
      // a fault in the signal rather than as a change of shape.
      p.x += uGlitch * 0.10 * life * step(0.982, fract(aSeed * 91.7 + uTime * 0.7));

      vec3 v = tilt_x(p, uTilt * life);
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

      /* Planetary light. A single light source gives the body a lit limb, a terminator and a dark
         side, and latitude bands give it structure the way a gas giant has it. The equatorial
         plane is exempt: those dots are the ring, and a ring lit from one side looks broken. The
         gate is the plane, not the radius - Saturn's planet sits at r 0.62, so a radius gate left
         the whole body unshaded. */
      float equatorial = 1.0 - smoothstep(0.020, 0.090, abs(p.y));
      vec3 normal = normalize(p + vec3(0.0001, 0.0002, 0.0003));
      float lambert = clamp(dot(normal, normalize(vec3(-0.62, 0.42, 0.66))), 0.0, 1.0);
      float shade = mix(0.30 + 1.05 * pow(lambert, 0.80), 1.0, equatorial);
      float bands = mix(0.88 + 0.16 * sin(p.y * 15.0 + 0.6), 1.0, equatorial);
      vAlpha *= shade * bands;
      // the ring reads as a bright, continuous band rather than as scattered dust
      vAlpha = mix(vAlpha, 0.80, equatorial * smoothstep(0.60, 0.70, r));

      /* A face is lit by its own baked gradient rather than by the planet light: what makes a
         face read is that the brow casts under itself, the nose has two lit sides and one shadow,
         and the sockets sit back. Region alpha keeps the eyes, the lips and the hair separable
         at a glance. */
      float region = aPart > 5.5 ? 0.80 : (aPart > 4.5 ? 0.92 : (aPart > 2.5 ? 1.0 : (aPart > 1.5 ? 0.95 : (aPart > 0.5 ? 0.62 : 1.0))));
      // the hair sits furthest back, so the depth fog was taking most of it: a face whose hair has
      // vanished is an egg with eyes
      vFar *= 1.0 - face * step(5.5, aPart);
      vAlpha = mix(vAlpha, region * (0.26 + 1.00 * aShade), face);
      vPart = aPart;
      vShade = aShade;
    }
  `;

  const BODY_FRAG = `
    precision highp float;
    varying float vSeed, vEnergy, vBand, vFar, vAlpha, vShade;
    uniform vec3 uColorA, uColorB, uWarm;
    uniform float uBloom, uFace;
    varying float vPart;

    void main() {
      vec2 d = gl_PointCoord - vec2(0.5);
      float r2 = dot(d, d);
      if (r2 > 0.25) discard;
      float core = pow(max(0.0, 1.0 - r2 * 4.0), 1.55);

      vec3 col = vBand < 0.5 ? uColorA : (vBand < 1.5 ? uColorB : vec3(1.0, 1.0, 1.0));
      // a small fraction runs warm: the blue/orange duality, dense near the rim
      float warm = smoothstep(0.94, 1.0, vSeed) * (1.0 - step(1.5, vBand));
      col = mix(col, uWarm, warm * 0.85);

      /* The face palette: skin is the interface colour pulled pale, the sclera is white, the iris
         is the accent, the pupil is black, the lips carry the only warm colour, brows and hair are
         the darkest values on the plate. Without the warm mouth and the dark eye, a dot head reads
         as a mask. */
      if (uFace > 0.001) {
        vec3 skin = mix(uColorA, vec3(0.72, 0.84, 0.96), 0.62);
        vec3 part = skin;
        if (vPart > 6.5) part = vec3(0.10, 0.03, 0.05);              // mouth cavity
        else if (vPart > 5.5) part = mix(vec3(0.30, 0.42, 0.58), uColorA, 0.35);  // hair
        else if (vPart > 4.5) part = vec3(0.10, 0.13, 0.20);        // brows and lashes
        else if (vPart > 3.5) part = vec3(0.95, 0.44, 0.54);        // lips
        else if (vPart > 2.5) part = vec3(0.02, 0.03, 0.05);        // pupil
        else if (vPart > 1.5) part = mix(vec3(0.35, 0.85, 1.0), uWarm, 0.55);    // iris
        else if (vPart > 0.5) part = vec3(0.60, 0.70, 0.80);         // sclera
        col = mix(col, part, uFace);
      }

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
    uniform float uFace;
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
      // a ring system would cut through a head, so it dissolves when the face takes the stage
      vAlpha = kind_alpha(aKind) * (1.0 - uAurora * (1.0 - aurora) * 0.50) * (1.0 - uGlitch * 0.25) * (1.0 - uFace * 0.97);
    }
  `;

  const RING_FRAG = `
    precision highp float;
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
      /* Reduced motion is a real preference and halves the budget. A core count is a weak proxy:
         a four-core laptop still drives 36k points without a fan spinning up, and the old <=4 test
         left most Windows machines running the sparse 2.6k budget that never looked like a body. */
      const reducedMotion = typeof window !== "undefined" && window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.lowPower = reducedMotion
        || (typeof navigator !== "undefined" && (navigator.hardwareConcurrency || 8) <= 2);

      this.bodyCount = Math.max(600, Math.min(options.count || 6000, 60000));
      if (this.lowPower) this.bodyCount = Math.min(this.bodyCount, 12000);
      // the instrument is globe plus ring system; the ring budget rides along with the globe
      this.satelliteCount = Math.round(this.bodyCount * 0.08);
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
      // how much of the cloud is currently a face, and the muscle values it is holding
      this.faceMix = 0;
      this.face = { jaw: 0, smile: 0, brow: 0, blink: 0, gazeX: 0, gazeY: 0 };
      this.faceTarget = { jaw: 0, smile: 0, brow: 0, blink: 0, gazeX: 0, gazeY: 0 };
      this._faceCache = null;
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
          aPosA: 3, aPosB: 3, aSeed: 1, aSize: 1, aBand: 1, aPart: 1, aShade: 1,
        },
        uniforms: [
          "uT", "uTime", "uEnergy", "uShear", "uCollapse", "uPixelRatio", "uPixelScale",
          "uPulse", "uPulseR", "uBloom", "uSpin", "uTilt", "uFocal", "uCamZ", "uRes",
          "uAurora", "uGlitch", "uFace", "uJaw", "uSmile", "uBrow", "uBlink", "uGaze",
          "uColorA", "uColorB", "uWarm",
        ],
      });
      this.ring = this._program(RING_VERT, RING_FRAG, {
        attributes: { aPos: 3, aSeed: 1, aSize: 1, aKind: 1 },
        uniforms: [
          "uTime", "uSpin", "uTilt", "uEnergy", "uCollapse", "uPixelRatio", "uPixelScale",
          "uPulse", "uPulseR", "uBloom", "uFocal", "uCamZ", "uRes",
          "uAurora", "uGlitch", "uFace", "uColorA", "uColorB", "uWarm",
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

      // `ok` must be true before the first resize: resize() refuses to run on a core it does not
      // consider usable, so setting the flag afterwards left the canvas at the browser default
      // 300x150 until the user happened to resize the window - and the core was a speck for the
      // whole session.
      this.ok = true;
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

    /* ------------------------------------------------------------------ the head field

       A head as a height field, not as a cloud. Every dot is a sample of a surface function, so
       what comes out is a face rather than a ball of dust: the field carries the anatomy that makes
       a face read at a glance - brow ridge, eye sockets, nose bridge, tip and wings, cheekbones,
       the philtrum, both lips and the hollow under them, the chin, the temples and the jaw line.

       Lighting is baked from the field's own gradient rather than added by hand: wherever the
       surface tilts away from the key light the dots are dark, and the nose therefore ends up with
       two lit sides and a shadow because that is what the geometry says, not because a shadow was
       painted there.

       Sex is bone structure only, and it is the only parameter the user has to choose: a narrower
       jaw, a smaller nose and brow, higher cheekbones and fuller lips, and long hair instead of a
       short cut. */

    _facePoints(sex) {
      const key = sex === "m" ? "m" : "f";
      if (!this._faceCache) this._faceCache = {};
      if (this._faceCache[key]) return this._faceCache[key];

      const n = this.bodyCount;
      const feminine = key === "f";
      const positions = new Float32Array(n * 3);
      const parts = new Float32Array(n);
      const shades = new Float32Array(n);
      const bands = new Float32Array(n);

      const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
      const gauss = (v, s) => Math.exp(-(v * v) / (2 * s * s));
      const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
      const fract = (v) => v - Math.floor(v);
      /* Two decorrelated hashes, not two multiples of the same constant. The first version stepped
         both parameters off one golden-ratio sequence, and the sample set collapsed into diagonal
         ridges that read as corduroy across the whole face. */
      const rnd = (i, salt) => fract(Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453123);

      const HEAD_Y = 0.10;
      const HALF_H = 0.62;      // crown
      const CHIN_H = 0.52;      // chin
      const HALF_W = 0.42;
      const LIGHT = [-0.45, 0.58, 0.68];
      const lightLen = Math.sqrt(LIGHT[0] * LIGHT[0] + LIGHT[1] * LIGHT[1] + LIGHT[2] * LIGHT[2]);
      const LX = LIGHT[0] / lightLen, LY = LIGHT[1] / lightLen, LZ = LIGHT[2] / lightLen;

      /* Half-width of the head at a height: a superellipse, so the crown and the jaw are round
         rather than pointed, with the jaw drawn in below the cheekbones. */
      const width = (y) => {
        const t = y - HEAD_Y;
        if (t >= 0) {
          // crown: a circular segment, which is why a head is round on top and not pointed
          const k = t / HALF_H;
          if (k >= 1) return 0;
          return HALF_W * Math.sqrt(Math.max(0, 1 - k * k * 0.93));
        }
        // jaw: drawn in below the cheekbones to a blunt chin rather than to a point
        const k = Math.min(1, -t / CHIN_H);
        if (k >= 1) return 0;
        const taper = feminine ? 0.48 : 0.38;
        return HALF_W * (feminine ? 1 : 1.05) * (1 - taper * Math.pow(k, 1.9))
          * Math.sqrt(Math.max(0, 1 - Math.pow(k, 3.4) * 0.62));
      };

      // the front of the skull, before any of the face is carved into it
      const skull = (x, y) => {
        const w = Math.max(0.02, width(y));
        const nx = Math.min(0.999, Math.abs(x) / w);
        const vertical = 1 - 0.20 * Math.max(0, (y - 0.16) / 0.56);
        return 0.330 * Math.sqrt(Math.max(0, 1 - nx * nx)) * vertical;
      };

      // the face itself: every feature is a bump or a hollow on that skull
      const features = (x, y) => {
        const ax = Math.abs(x);
        let z = 0;
        z += (feminine ? 0.028 : 0.044) * gauss(y - 0.228, 0.042) * gauss(ax - 0.180, 0.130);   // brow ridge
        z -= (feminine ? 0.050 : 0.044) * gauss(y - 0.095, 0.070) * gauss(ax - 0.170, 0.108);   // eye socket
        z += (feminine ? 0.135 : 0.155) * gauss(ax, 0.042) * smooth(-0.240, -0.10, y) * smooth(0.240, 0.08, y); // bridge
        z += (feminine ? 0.125 : 0.145) * gauss(x, 0.052) * gauss(y + 0.243, 0.045);            // nose tip
        z += 0.026 * gauss(ax - 0.056, 0.025) * gauss(y + 0.258, 0.028);                        // nostril wings
        z += (feminine ? 0.042 : 0.030) * gauss(ax - 0.238, 0.110) * gauss(y + 0.010, 0.090);   // cheekbones
        z -= 0.016 * gauss(y + 0.292, 0.019) * gauss(ax, 0.110);                                // philtrum
        z += 0.028 * gauss(x, 0.120) * gauss(y + 0.344, 0.024);                                 // upper lip
        z += 0.036 * gauss(x, 0.112) * gauss(y + 0.384, 0.028);                                 // lower lip
        z -= 0.018 * gauss(y + 0.418, 0.021) * gauss(x, 0.110);                                 // labiomental crease
        z += (feminine ? 0.030 : 0.042) * gauss(x, 0.082) * gauss(y + 0.472, 0.062);            // chin
        z -= 0.018 * gauss(ax - 0.315, 0.065) * gauss(y - 0.010, 0.130);                        // temple
        return z;
      };

      const depth = (x, y) => skull(x, y) + features(x, y);

      // baked light: the field's own normal decides how bright a dot is
      const shadeAt = (x, y) => {
        const e = 0.011;
        const dx = (depth(clamp(x + e, -HALF_W + 0.02, HALF_W - 0.02), y) - depth(clamp(x - e, -HALF_W + 0.02, HALF_W - 0.02), y)) / (2 * e);
        const dy = (depth(x, clamp(y + e, -0.52, 0.72)) - depth(x, clamp(y - e, -0.52, 0.72))) / (2 * e);
        const len = Math.sqrt(dx * dx + dy * dy + 1);
        const lam = Math.max(0, (-dx * LX - dy * LY + LZ) / len);
        return 0.14 + 1.02 * Math.pow(lam, 0.78);
      };

      const EYE_X = 0.165, EYE_Y = 0.092, LIP_Y = -0.362;
      const partAt = (x, y) => {
        const ax = Math.abs(x);
        const ex = (ax - EYE_X) / (feminine ? 0.102 : 0.096);
        const ey = (y - EYE_Y) / (feminine ? 0.054 : 0.050);
        if (ex * ex + ey * ey < 1) {
          // the top of the eye is the lid line, drawn dark: this is what gives an eye an edge
          if (y > EYE_Y + (feminine ? 0.042 : 0.038)) return 5;
          const d = Math.sqrt((ax - EYE_X) * (ax - EYE_X) + (y - EYE_Y) * (y - EYE_Y));
          if (d < (feminine ? 0.019 : 0.020)) return 3;
          if (d < (feminine ? 0.044 : 0.046)) return 2;
          return 1;
        }
        const nostrilX = (ax - 0.052) / 0.022, nostrilY = (y + 0.278) / 0.014;
        if (nostrilX * nostrilX + nostrilY * nostrilY < 1) return 5;
        const browY = 0.250 + 0.024 * Math.cos(clamp((ax - 0.165) / 0.115, -1, 1) * Math.PI);
        if (ax > 0.055 && ax < 0.295 && Math.abs(y - browY) < (feminine ? 0.014 : 0.018)) return 5;
        const mx = ax / 0.126, my = (y - LIP_Y) / 0.040;
        if (mx * mx + my * my < 1) {
          const cx = ax / 0.098, cy = (y - LIP_Y) / 0.011;
          return (cx * cx + cy * cy < 1) ? 7 : 4;
        }
        return 0;
      };

      const nSurface = Math.round(n * 0.60);
      const nCavity = Math.round(n * 0.02);
      const nHair = Math.round(n * 0.32);
      /* The face is sampled on a jittered grid, not at random: uniformly random dots read as film
         grain, and a face made of grain has no form at all. Rows and columns are balanced by the
         head's own aspect so the cells are square. */
      const cols = Math.max(8, Math.round(Math.sqrt(nSurface * 0.74)));
      const rows = Math.max(8, Math.ceil(nSurface / cols));

      for (let i = 0; i < n; i++) {
        let x = 0, y = 0, z = 0, part = 0, shade = 0.8;

        if (i < nSurface) {
          const row = Math.floor(i / cols);
          const col = i % cols;
          const vv = Math.min(0.999, (row + 0.5 + (rnd(i, 1) - 0.5) * 0.80) / rows);
          const yy = vv * 2 - 1;
          // the exponent is applied to the magnitude: a negative base with a fractional power is NaN,
          // and one NaN here becomes a dot that the geometry check reports as non-finite
          y = HEAD_Y + (yy < 0 ? CHIN_H : HALF_H) * Math.sign(yy) * Math.pow(Math.abs(yy), 1.06);
          const half = width(y);
          const uu = (col + 0.5 + (rnd(i, 2) - 0.5) * 0.80) / cols;
          x = (uu * 2 - 1) * half * 0.99;
          z = depth(x, y) + (rnd(i, 3) - 0.5) * 0.012;
          part = partAt(x, y);
          // the eyeball is a sphere sitting in its socket, not a painted disc on the skin
          if (part >= 1 && part <= 3) z += 0.042;
          shade = shadeAt(x, y);
          if (part >= 1 && part <= 3) shade = 0.82 + 0.28 * shade;
          if (part === 4) shade = 0.66 + 0.34 * shade;
          if (part === 5) shade = 1.0;
          if (part === 7) shade = 0.18;
        } else if (i < nSurface + nCavity) {
          // the cavity behind the lips: when the jaw drops there has to be dark behind them
          const a = rnd(i, 1) * Math.PI * 2;
          const rr = Math.sqrt(rnd(i, 2));
          x = Math.cos(a) * 0.094 * rr;
          y = LIP_Y + Math.sin(a) * 0.017 * rr;
          z = depth(x, clamp(y, -0.44, -0.30)) - 0.055;
          part = 7;
          shade = 0.16;
        } else if (i < nSurface + nCavity + nHair) {
          /* Hair as a halo around the head, not as columns beside it. Hers is a ring the whole way
             round that falls past the jaw; his is an arc over the crown from ear to ear, because a
             folded ring put a bright line of dots straight across his face at eye level. */
          if (feminine) {
            const theta = rnd(i, 1) * Math.PI * 2;
            const spread = 1.03 + 0.24 * Math.pow(rnd(i, 2), 0.7);
            const cx = Math.cos(theta), sy = Math.sin(theta);
            x = cx * HALF_W * spread;
            y = HEAD_Y + sy * HALF_H * spread;
            if (sy < 0.15) {
              const fall = (0.15 - sy) * 0.42 * rnd(i, 3);
              y -= fall;
              x += (cx < 0 ? -1 : 1) * fall * 0.55;
            }
            if (sy > 0.55) y += (sy - 0.55) * 0.22;
            z = -0.20 - 0.20 * Math.abs(cx) + (rnd(i, 3) - 0.5) * 0.12;
            shade = 0.40 + 0.30 * (1 - Math.min(1, Math.abs(z)));
            const reach = Math.sqrt(x * x + y * y);
            if (reach > 1.02) { x *= 1.02 / reach; y *= 1.02 / reach; }
          } else {
            const phi = rnd(i, 1) * 1.92;
            const spread = 1.05 + 0.12 * rnd(i, 2);
            const side = rnd(i, 3) < 0.5 ? -1 : 1;
            const sy = Math.cos(phi);
            x = Math.sin(phi) * HALF_W * spread * side;
            y = HEAD_Y + sy * HALF_H * spread;
            z = 0.03 - 0.22 * Math.abs(Math.sin(phi)) + (rnd(i, 3) - 0.5) * 0.10;
            shade = 0.52 + 0.34 * (1 - Math.min(1, Math.abs(z)));
          }
          part = 6;
        } else {
          // neck: without it the head floats
          const t = rnd(i, 1);
          y = -0.50 - t * 0.38;
          x = (rnd(i, 2) * 2 - 1) * (feminine ? 0.165 : 0.180) * (1 - t * 0.12);
          z = 0.12 - t * 0.05 + (rnd(i, 3) - 0.5) * 0.07;
          part = 0;
          shade = 0.40;
        }

        positions[i * 3] = x;
        positions[i * 3 + 1] = y;
        positions[i * 3 + 2] = z;
        parts[i] = part;
        shades[i] = clamp(shade, 0.05, 1.4);
        // the iris and the pupil are the only dots drawn oversized: the eye is what a face is read by
        bands[i] = (part === 2 || part === 3) ? 2 : 0;
      }

      const face = { positions: positions, parts: parts, shades: shades, bands: bands };
      this._faceCache[key] = face;
      return face;
    }

    /** Put the face's region buffers on the GPU. Built once per sex and reused: switching faces
     *  swaps three buffers instead of rebuilding a hundred thousand floats. */
    _installFace(sex) {
      const gl = this.gl;
      const set = this._facePoints(sex);
      if (!this.globe.facePart) {
        this.globe.facePart = this._floatBuffer(set.parts);
        this.globe.faceShade = this._floatBuffer(set.shades);
        this.globe.faceBand = this._floatBuffer(set.bands);
        return;
      }
      [[this.globe.facePart, set.parts], [this.globe.faceShade, set.shades], [this.globe.faceBand, set.bands]]
        .forEach((pair) => {
          gl.bindBuffer(gl.ARRAY_BUFFER, pair[0]);
          gl.bufferData(gl.ARRAY_BUFFER, pair[1], gl.STATIC_DRAW);
        });
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
        sizes[i] = bands[i] === 2 ? 1.8 + Math.random() * 0.7 : (bands[i] === 3 ? 1.25 + Math.random() * 0.4 : 0.70 + Math.random() * 0.40);
      }
      // every body point needs a part and a shade even when it is not a face, so the attribute is
      // never left unbound: an unbound attribute reads as zeros in some drivers and as the last
      // value in others, and only one of those is a defined colour.
      const parts = new Float32Array(n);
      const shades = new Float32Array(n);
      shades.fill(1);
      return {
        spawn: spawn,
        count: n,
        aSeed: this._floatBuffer(seeds),
        aSize: this._floatBuffer(sizes),
        aBand: this._floatBuffer(bands),
        aPart: this._floatBuffer(parts),
        aShade: this._floatBuffer(shades),
        facePart: null, faceShade: null, faceBand: null,
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
      const ringlets = Math.round(n * 0.80);
      const tendrils = Math.round(n * 0.06);
      const orbits = Math.round(n * 0.10);
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
      if (name === "face" || name === "face-m") {
        // handed back directly: the field is cached per sex and never mutated after it is built
        return this._facePoints(name === "face-m" ? "m" : "f").positions;
      }
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
          // Height and radius are both held just under the shell radius: at the old 1.8 / 0.5
          // the top of the helix reached 1.19 and clipped the frame.
          const angle = t * Math.PI * 2 * 6;
          const radius = 0.28 + t * 0.44;
          x = Math.cos(angle) * radius;
          y = (t - 0.5) * 1.46;
          z = Math.sin(angle) * radius;
        } else if (name === "grid") {
          const side = Math.ceil(Math.sqrt(n));
          const gx = (i % side) / side - 0.5;
          const gy = Math.floor(i / side) / side - 0.5;
          // 1.32, not 1.7: a square grid's corners are diagonal, so 1.7 pushed them to 1.20.
          x = gx * 1.32;
          y = gy * 1.32;
          z = Math.sin(gx * 9) * Math.cos(gy * 9) * 0.09;
        } else if (name === "wave") {
          const t = i / n;
          const angle = t * Math.PI * 2;
          const radius = 0.35 + 0.55 * Math.abs(Math.sin(t * Math.PI * 3));
          x = Math.cos(angle) * radius;
          z = Math.sin(angle) * radius;
          y = Math.sin(angle * 6) * 0.35;
        } else if (name === "saturn") {
          /* A ringed planet, built the way the real one looks: an oblate globe, then three
           * separated bands of ring with clear gaps between them. The gaps matter more than the
           * bands - five evenly spaced circles read as a target, gaps read as a planet. */
          const planetShare = 0.56;
          const planet = i / n < planetShare;
          if (planet) {
            const t = i / n / planetShare;
            const lat = 1 - t * 2;
            const ring = Math.sqrt(Math.max(0, 1 - lat * lat));
            const theta = golden * i;
            const squash = 0.87 + 0.03 * Math.cos(lat * 7.0);
            x = Math.cos(theta) * ring * 0.62;
            z = Math.sin(theta) * ring * 0.62;
            y = lat * 0.62 * squash;
          } else {
            const t = (i - n * planetShare) / (n * (1 - planetShare));
            // band 0: bright inner ring, band 1: wide main ring, band 2: thin outer ring
            const band = t < 0.30 ? 0 : (t < 0.80 ? 1 : 2);
            // Every body has to stay inside the 1.12 shell the geometry check enforces, so the
            // ring is scaled to the frame rather than spilling off the plate.
            const inner = band === 0 ? 0.68 : (band === 1 ? 0.79 : 0.98);
            const width = band === 0 ? 0.07 : (band === 1 ? 0.15 : 0.10);
            const r = inner + Math.pow(Math.random(), 0.85) * width;
            const angle = golden * i * 3.0;
            x = Math.cos(angle) * r;
            z = Math.sin(angle) * r;
            y = (Math.random() - 0.5) * 0.006;
          }
        } else if (name === "galaxy") {
          /* Three logarithmic arms around a bright bulge. Arm membership is assigned by index,
           * not by angle, so the arms stay equally populated as the cloud breathes. */
          const bulge = i / n < 0.22;
          if (bulge) {
            const rr = Math.pow(Math.random(), 0.6) * 0.24;
            const theta = golden * i;
            const phi = Math.acos(1 - 2 * (i / Math.max(1, n * 0.22)));
            x = Math.cos(theta) * Math.sin(phi) * rr;
            y = Math.cos(phi) * rr * 0.8;
            z = Math.sin(theta) * Math.sin(phi) * rr;
          } else {
            const t = (i - n * 0.22) / (n * 0.78);
            const arm = i % 3;
            const rr = 0.16 + Math.pow(t, 0.9) * 0.84;
            const angle = (arm * Math.PI * 2) / 3 + t * 4.6 + (Math.random() - 0.5) * 0.30;
            x = Math.cos(angle) * rr;
            z = Math.sin(angle) * rr;
            y = (Math.random() - 0.5) * 0.10 * Math.exp(-rr * 1.4);
          }
        } else if (name === "cube") {
          /* A wireframe cube: most points ride the twelve edges, the rest sit just inside the six
           * faces, so the body has surfaces without becoming a solid block of dots. */
          const half = 0.58;
          const edges = n * 0.82;
          if (i < edges) {
            const edge = i % 12;
            const t = Math.random() * 2 - 1;
            const a = (edge & 1) ? half : -half;
            const b = (edge & 2) ? half : -half;
            const axis = edge >> 2;
            if (axis === 0) { x = t * half; y = a; z = b; }
            else if (axis === 1) { x = a; y = t * half; z = b; }
            else { x = a; y = b; z = t * half; }
          } else {
            const face = i % 6;
            x = (Math.random() * 2 - 1) * half * 0.94;
            y = (Math.random() * 2 - 1) * half * 0.94;
            z = (Math.random() * 2 - 1) * half * 0.94;
            if (face === 0) x = half * 0.94;
            else if (face === 1) x = -half * 0.94;
            else if (face === 2) y = half * 0.94;
            else if (face === 3) y = -half * 0.94;
            else if (face === 4) z = half * 0.94;
            else z = -half * 0.94;
          }
        } else if (name === "cone") {
          const t = (i / n) * 3.4 % 1;
          const level = i / n;
          const rr = (1 - level) * 0.62;
          const angle = t * Math.PI * 2 * 9 + level * 12;
          x = Math.cos(angle) * rr;
          z = Math.sin(angle) * rr;
          y = 0.78 - level * 1.56;
        } else if (name === "dna") {
          /* Two strands and the rungs between them. Every ninth point is a rung, laid along the
           * straight line joining the strands, which is what makes it read as a molecule. */
          const t = i / n;
          const phase = t * Math.PI * 2 * 3.2;
          // Named separately from the outer ``y``: shadowing it left every point at y = 0, which
          // turned the molecule into a flat disc and only showed up under the geometry check.
          const yy = (t - 0.5) * 1.86;
          y = yy;
          if (i % 9 === 0) {
            const f = Math.random();
            x = Math.cos(phase) * 0.34 * (1 - 2 * f);
            z = Math.sin(phase) * 0.34 * (1 - 2 * f);
          } else {
            const strand = i % 2 === 0 ? 0 : Math.PI;
            x = Math.cos(phase + strand) * 0.34;
            z = Math.sin(phase + strand) * 0.34;
          }
        } else if (name === "knot") {
          const t = (i / n) * Math.PI * 2;
          x = (Math.sin(t) + 2 * Math.sin(2 * t)) * 0.30;
          y = (Math.cos(t) - 2 * Math.cos(2 * t)) * 0.30;
          z = -Math.sin(3 * t) * 0.30;
        } else if (name === "atom") {
          /* Three orthogonal electron shells around a nucleus: the orbital diagram, in dots. */
          if (i / n < 0.30) {
            const rr = Math.pow(Math.random(), 0.5) * 0.19;
            const theta = golden * i;
            const phi = Math.acos(1 - 2 * Math.random());
            x = Math.cos(theta) * Math.sin(phi) * rr;
            y = Math.cos(phi) * rr;
            z = Math.sin(theta) * Math.sin(phi) * rr;
          } else {
            const shell = i % 3;
            const angle = ((i * 7) / n) * Math.PI * 2 * 4 + i * 0.7;
            const rr = 0.90 + (Math.random() - 0.5) * 0.012;
            const a = Math.cos(angle) * rr;
            const b = Math.sin(angle) * rr;
            if (shell === 0) { x = a; y = b; z = 0; }
            else if (shell === 1) { x = a; y = 0; z = b; }
            else { x = 0; y = a; z = b; }
          }
        } else if (name === "burst") {
          /* A nova: rays with most dots gathered near the outer tip, so the silhouette is a
           * star rather than a fuzzy ball. */
          const theta = golden * i;
          const phi = Math.acos(1 - 2 * (i / n));
          const spike = Math.pow(Math.random(), 0.35);
          const rr = 0.18 + spike * 0.88;
          x = Math.cos(theta) * Math.sin(phi) * rr;
          y = Math.cos(phi) * rr;
          z = Math.sin(theta) * Math.sin(phi) * rr;
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
      if (SHAPES.indexOf(name) < 0) return;
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
      if (name === "face" || name === "face-m") this._installFace(name === "face-m" ? "m" : "f");
      this.t = instant ? 1 : 0;
      this.morphing = !instant;
      this.morphStart = performance.now();
      this.morphMs = 1200;
    }

    /** Expression targets. The core eases toward these every frame, so any event source can set
     *  them without smoothing: jaw 0..1, smile and brow -1..1, blink 0..1, gaze -1..1. */
    setExpression(values) {
      if (!values) return;
      const t = this.faceTarget;
      const put = (key, value, lo, hi) => {
        if (value === undefined || value === null || !Number.isFinite(value)) return;
        t[key] = Math.max(lo, Math.min(hi, value));
      };
      put("jaw", values.jaw, 0, 1);
      put("smile", values.smile, -1, 1);
      put("brow", values.brow, -1, 1);
      put("blink", values.blink, 0, 1);
      put("gazeX", values.gazeX, -1, 1);
      put("gazeY", values.gazeY, -1, 1);
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
      this.pixelScale = Math.max(2.2, Math.min(height, width) * 0.0070);
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
      /* Face state eases here, not in the caller: an event can arrive at any rate and still
         produce a face that moves like a face. Blink is the fast one - a lid is closed for about a
         tenth of a second - and the jaw follows the speaking envelope. */
      const faceWanted = (this.shape === "face" || this.shape === "face-m") ? 1 : 0;
      this.faceMix += (faceWanted - this.faceMix) * (faceWanted > this.faceMix ? 0.10 : 0.06);
      const ease = (key, rate) => { this.face[key] += (this.faceTarget[key] - this.face[key]) * rate; };
      ease("jaw", 0.30); ease("smile", 0.12); ease("brow", 0.10);
      ease("gazeX", 0.10); ease("gazeY", 0.10); ease("blink", 0.45);

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
      set("uFace", (l) => gl.uniform1f(l, this.faceMix));
      set("uJaw", (l) => gl.uniform1f(l, this.face.jaw));
      set("uSmile", (l) => gl.uniform1f(l, this.face.smile));
      set("uBrow", (l) => gl.uniform1f(l, this.face.brow));
      set("uBlink", (l) => gl.uniform1f(l, this.face.blink));
      set("uGaze", (l) => gl.uniform2f(l, this.face.gazeX, this.face.gazeY));
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
      const face = this.shape === "face" || this.shape === "face-m";
      buffers.aBand = face && this.globe.faceBand ? this.globe.faceBand : this.globe.aBand;
      buffers.aPart = face && this.globe.facePart ? this.globe.facePart : this.globe.aPart;
      buffers.aShade = face && this.globe.faceShade ? this.globe.faceShade : this.globe.aShade;
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
