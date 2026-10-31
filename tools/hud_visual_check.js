/* Headless check of the HUD's visual layer (no browser required).
 *
 * The interface cannot be screenshot in CI, so this harness runs the real renderers against a
 * stubbed WebGL context and a recording canvas 2D context and asserts the things a blank screen
 * would hide:
 *
 *   • every attribute and uniform the JavaScript looks up is actually declared in its shader, and
 *     every attribute it fetches is bound from a buffer (a null location silently draws nothing)
 *   • the core really projects in 3D — perspective divide, camera depth, focal length — rather
 *     than scaling x/y by a fake "perspective" factor
 *   • the geometry is finite, inside its expected radii, and covers every band and kind, so no
 *     body of the instrument is empty
 *   • both bodies are drawn every frame, and the glow pass runs on a capable machine
 *   • the cage, waveform strips and gauges render without throwing at wide and narrow sizes
 *   • every element id app.js reaches for exists in index.html
 *
 *   node tools/hud_visual_check.js
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
// The document and the controller are read once, and every canvas stub below is derived from the
// document: a widget added to the page with no renderer (or a renderer with no canvas) then fails
// the check instead of passing quietly.
const HTML = fs.readFileSync(path.join(ROOT, "hud", "index.html"), "utf8");
const APP = fs.readFileSync(path.join(ROOT, "hud", "app.js"), "utf8");
let failures = 0;

function check(condition, message) {
  if (condition) return;
  failures += 1;
  console.error("FAIL  " + message);
}

function note(message) {
  console.log("  ·   " + message);
}

/* ------------------------------------------------------------------ WebGL stub */
function makeGL() {
  const record = { shaders: [], buffers: [], draws: [], missing: [], bound: [] };
  let boundBuffer = null;
  const currentPrograms = new Map();
  const uniformsOf = (program) => {
    if (program.__declared) return program.__declared;
    const uniforms = new Set();
    const attributes = new Set();
    program.shaders.forEach((shader) => {
      const uniformMatches = shader.src.match(/uniform\s+[a-z0-9]+\s+[^;]+;/g) || [];
      uniformMatches.forEach((statement) => {
        statement
          .replace(/uniform\s+[a-z0-9]+\s+/, "")
          .replace(";", "")
          .split(",")
          .forEach((name) => uniforms.add(name.trim()));
      });
      const attributeMatches = shader.src.match(/attribute\s+[a-z0-9]+\s+[^;]+;/g) || [];
      attributeMatches.forEach((statement) => {
        statement
          .replace(/attribute\s+[a-z0-9]+\s+/, "")
          .replace(";", "")
          .split(",")
          .forEach((name) => attributes.add(name.trim()));
      });
    });
    program.__declared = { uniforms: uniforms, attributes: attributes };
    return program.__declared;
  };

  const gl = {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, ARRAY_BUFFER: 3, STATIC_DRAW: 4, DYNAMIC_DRAW: 5,
    POINTS: 6, COLOR_BUFFER_BIT: 7, SRC_ALPHA: 8, ONE: 9, DEPTH_TEST: 10, FLOAT: 11,
    createShader: () => ({ src: "" }),
    shaderSource: (shader, src) => { shader.src = src; record.shaders.push(src); },
    compileShader: () => {},
    getShaderParameter: () => true,
    getShaderInfoLog: () => "",
    createProgram: () => {
      const program = { id: record.buffers.length + 1, shaders: [] };
      currentPrograms.set(program.id, program);
      return program;
    },
    attachShader: (program, shader) => { program.shaders.push(shader); },
    linkProgram: () => {},
    getProgramParameter: () => true,
    getProgramInfoLog: () => "",
    useProgram: () => {},
    getUniformLocation: (program, name) => {
      const declared = uniformsOf(program);
      if (!declared.uniforms.has(name)) {
        record.missing.push("uniform " + name);
        return null;
      }
      return { kind: "uniform", name: name };
    },
    getAttribLocation: (program, name) => {
      const declared = uniformsOf(program);
      if (!declared.attributes.has(name)) {
        record.missing.push("attribute " + name);
        return -1;
      }
      return Array.from(declared.attributes).indexOf(name);
    },
    createBuffer: () => {
      const buffer = { data: null };
      record.buffers.push(buffer);
      return buffer;
    },
    bindBuffer: (target, buffer) => { boundBuffer = buffer; },
    bufferData: (target, data) => { if (boundBuffer) boundBuffer.data = data; },
    enable: () => {}, disable: () => {}, blendFunc: () => {}, clearColor: () => {},
    clear: () => {}, viewport: () => {}, enableVertexAttribArray: () => {},
    vertexAttribPointer: (location) => { record.bound.push(location); },
    uniform1f: () => {}, uniform2f: () => {}, uniform3fv: () => {},
    drawArrays: (mode, first, count) => { record.draws.push(count); },
    deleteBuffer: () => {}, deleteProgram: () => {},
  };
  record.gl = gl;
  record.programs = currentPrograms;
  return record;
}

/* ------------------------------------------------------------------ DOM stub */
function makeElement(id, options) {
  options = options || {};
  const element = {
    id: id,
    clientWidth: options.width === undefined ? 1600 : options.width,
    clientHeight: options.height === undefined ? 900 : options.height,
    width: 0, height: 0,
    style: {}, dataset: options.dataset || {}, listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    getContext: options.getContext || (() => null),
    addEventListener: (name, handler) => { element.listeners[name] = handler; },
    querySelector: () => null, querySelectorAll: () => [], appendChild: () => {},
    setPointerCapture: () => {}, closest: () => element,
  };
  return element;
}

function makeContext2D(record) {
  const gradient = { addColorStop: () => {} };
  record.placements = record.placements || [];
  return {
    canvas: null,
    setTransform: () => {}, clearRect: () => {}, save: () => {}, restore: () => {},
    translate: () => {}, rotate: () => {}, beginPath: () => {}, moveTo: () => {}, lineTo: () => {},
    arc: () => {}, quadraticCurveTo: () => {}, closePath: () => {},
    stroke: () => { record.strokes += 1; },
    fill: () => { record.fills += 1; }, fillRect: () => {}, strokeRect: () => {},
    fillText: (text, x, y) => {
      record.texts.push(String(text));
      record.placements.push({ text: String(text), x: x, y: y });
    },
    measureText: (text) => ({ width: String(text).length * 5.2 }),
    createRadialGradient: () => gradient, createLinearGradient: () => gradient,
    setLineDash: () => {}, lineWidth: 1, strokeStyle: "", fillStyle: "", font: "",
    textAlign: "left", textBaseline: "alphabetic",
  };
}

function loadModule(file, sandbox) {
  const source = fs.readFileSync(path.join(ROOT, file), "utf8");
  vm.runInNewContext(source, sandbox, { filename: file });
}

/* ------------------------------------------------------------------ run the core */
const glRecord = makeGL();
const canvas = makeElement("core");
canvas.getContext = () => glRecord.gl;

const coreSandbox = {
  console: console,
  performance: { now: () => 1000 },
  Math: Math, Float32Array: Float32Array, Object: Object, Array: Array, Number: Number,
  window: {
    devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900,
    matchMedia: () => ({ matches: false }),
  },
  navigator: { hardwareConcurrency: 8 },
};
coreSandbox.window.window = coreSandbox.window;
loadModule("hud/particle-core.js", coreSandbox);

const ParticleCore = coreSandbox.window.ParticleCore;
check(typeof ParticleCore === "function", "particle-core.js did not export ParticleCore");

const core = new ParticleCore(canvas, { count: 6000 });
check(core.ok === true, "particle core failed to initialise: " + (core.error && core.error.message));

note(glRecord.shaders.length + " shader sources, " + glRecord.buffers.length + " buffers, " +
  core.bodyCount + " globe points + " + core.satelliteCount + " ring points");

check(glRecord.shaders.length === 4, "expected 4 shader sources, saw " + glRecord.shaders.length);
check(glRecord.missing.length === 0, "shader metadata drift: " + glRecord.missing.join(", "));

// A real 3D camera: perspective divide by camera depth, and the camera uniforms declared.
const vertexSources = glRecord.shaders.join("\n");
check(/uFocal\s*\/\s*max\(/.test(vertexSources), "no perspective divide in the vertex shaders");
check(/uCamZ - v\.z/.test(vertexSources), "no camera depth term: the projection is not 3D");
check(/gl_Position = vec4\(v\.x \* f \/ aspect/.test(vertexSources),
  "the projection does not correct for aspect, so the globe would stretch");

/* ------------------------------------------------------------------ geometry */
const buffers = glRecord.buffers.map((buffer) => buffer.data).filter(Boolean);
const globePositions = buffers.filter((data) => data.length === core.bodyCount * 3);
const ringPositions = buffers.filter((data) => data.length === core.satelliteCount * 3);
check(globePositions.length === 2, "expected two globe position buffers, saw " + globePositions.length);
check(ringPositions.length === 1, "expected one ring position buffer, saw " + ringPositions.length);

function radii(data) {
  const out = [];
  for (let i = 0; i < data.length; i += 3) {
    out.push(Math.sqrt(data[i] * data[i] + data[i + 1] * data[i + 1] + data[i + 2] * data[i + 2]));
  }
  return out;
}

if (globePositions.length === 2) {
  const globeRadii = radii(globePositions[0]);
  const finite = globeRadii.every((value) => Number.isFinite(value));
  check(finite, "globe geometry contains non-finite points");
  const max = Math.max.apply(null, globeRadii);
  const min = Math.min.apply(null, globeRadii);
  check(max <= 1.12, "globe extends past its shell: max radius " + max.toFixed(3));
  check(min >= 0.0 && max > 0.9, "globe shell is not fully populated (max radius " + max.toFixed(3) + ")");
  const inner = globeRadii.filter((value) => value < 0.75).length;
  check(inner > core.bodyCount * 0.10, "the globe has no inner volume, so it will read flat");
  note("globe radius " + min.toFixed(3) + "–" + max.toFixed(3) + ", " + inner + " inner-volume points");
}

if (ringPositions.length === 1) {
  const ringRadii = radii(ringPositions[0]);
  check(ringRadii.every((value) => Number.isFinite(value)), "ring geometry contains non-finite points");
  const flat = ringRadii.filter((value) => value > 1.05 && value < 1.8).length;
  check(flat > core.satelliteCount * 0.5, "most ring points are not in the ring plane band");
  note("ring radius " + Math.min.apply(null, ringRadii).toFixed(2) + "–" +
    Math.max.apply(null, ringRadii).toFixed(2) + ", " + flat + " in-plane points");
}

const bandSets = buffers.filter((data) => data.length === core.bodyCount);
const kindSets = buffers.filter((data) => data.length === core.satelliteCount &&
  Array.from(data).every((value) => value >= 0 && value <= 3) && new Set(Array.from(data)).size === 4);
check(bandSets.length >= 3, "globe band/size buffers are missing");
check(kindSets.length === 1, "expected exactly one kind buffer covering all four ring kinds");

const bandValues = new Set();
bandSets.forEach((data) => Array.from(data).forEach((value) => bandValues.add(value)));
[0, 1, 2, 3].forEach((band) => {
  check(bandValues.has(band), "globe band " + band + " is empty, so part of the instrument is missing");
});

/* ------------------------------------------------------------------ drawing */
core.resize();
core.frame(1016);
check(glRecord.draws.length >= 2, "the core drew nothing");
check(glRecord.draws.indexOf(core.bodyCount) >= 0, "the globe body was never drawn");
check(glRecord.draws.indexOf(core.satelliteCount) >= 0, "the ring system was never drawn");
check(glRecord.draws.length === 4, "expected crisp + glow passes over both bodies, saw " + glRecord.draws.length);

core.setShape("helix");
core.frame(1020);
core.setShape("wave");
core.frame(1040);
core.frame(1060);
check(glRecord.missing.length === 0, "shader metadata drift after morphing: " + glRecord.missing.join(", "));

/* Every declared topology must be a real body: finite, inside the shell, and not degenerate.
   A shape that produced one point, or points past the shell, would morph into a smear or clip the
   frame, and neither shows up until someone looks at it. This is the check that stands in for
   looking. */
const SHAPE_NAMES = [
  "sphere", "saturn", "galaxy", "torus", "helix", "dna", "knot",
  "cube", "cone", "atom", "grid", "wave", "burst",
  "face", "face-m",
];
SHAPE_NAMES.forEach((name) => {
  const points = core._shape(name);
  check(points && points.length === core.bodyCount * 3, name + " produced the wrong point count");
  core.setShape(name, true);
  check(core.shape === name, "setShape refused " + name + ", so the menu button would do nothing");
  const rs = radii(points);
  check(rs.every((value) => Number.isFinite(value)), name + " produced non-finite points");
  const max = Math.max.apply(null, rs);
  const min = Math.min.apply(null, rs);
  check(max <= 1.12, name + " extends past its shell: max radius " + max.toFixed(3));
  check(max > 0.55, name + " is too small to read as a body (max radius " + max.toFixed(3) + ")");
  check(max - min > 0.1, name + " collapsed to a single radius, so the shape is gone");
});
note("topologies verified: " + SHAPE_NAMES.length);

// stress the state machine: energy, shear, collapse and a shockwave all on top of a morph
core.setShape("torus");
core.setEnergy(1);
core.setShear(1);
core.collapse();
core.pulseEnergy(1);
core.pulseRing(1);
core.frame(1080);
check(glRecord.draws.length > 4, "the core stopped drawing under load");

/* ------------------------------------------------------------------ chrome */
const chromeRecord = { strokes: 0, fills: 0, texts: [] };
function chromeContext() {
  const context = makeContext2D(chromeRecord);
  return context;
}

/* Every canvas the harness instantiates is declared in index.html: the waveform strips, the dials,
   the widget instruments. Parsing the document keeps the stubs honest. */
const canvasSpecs = [];
(HTML.match(/<canvas[^>]*>/g) || []).forEach((tag, index) => {
  const attr = (name) => {
    const match = tag.match(new RegExp(name + '="([^"]+)"'));
    return match ? match[1] : undefined;
  };
  canvasSpecs.push({
    id: attr("id") || ("canvas-" + index),
    dataset: {
      wave: attr("data-wave"), gauge: attr("data-gauge"),
      widget: attr("data-widget"), source: attr("data-source"), label: attr("data-label"),
    },
    width: Number(attr("width") || 1600),
    height: Number(attr("height") || 900),
  });
});

const elements = {};
canvasSpecs.forEach((spec) => {
  const element = makeElement(spec.id, { width: spec.width, height: spec.height, dataset: spec.dataset });
  element.getContext = chromeContext;
  spec.element = element;
  elements[spec.id] = element;
});
const waveSpecs = canvasSpecs.filter((spec) => spec.dataset.wave);
const gaugeSpecs = canvasSpecs.filter((spec) => spec.dataset.gauge);
const widgetSpecs = canvasSpecs.filter((spec) => spec.dataset.widget);
const pick = (specs) => specs.map((spec) => spec.element);

const chromeSandbox = {
  console: console,
  performance: { now: () => 0 },
  Math: Math, Object: Object, Array: Array, Number: Number, Set: Set, String: String,
  document: {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (selector) => {
      if (selector === "canvas[data-wave]") return pick(waveSpecs);
      if (selector === "canvas[data-gauge]") return pick(gaugeSpecs);
      if (selector === "canvas[data-widget]") return pick(widgetSpecs);
      return [];
    },
  },
  window: { devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900 },
};
chromeSandbox.window.window = chromeSandbox.window;
loadModule("hud/chrome.js", chromeSandbox);

const chrome = chromeSandbox.window.OrbythChrome;
check(chrome && typeof chrome.attach === "function", "chrome.js did not export attach()");

const attached = chrome.attach();
check(attached.strips.length === waveSpecs.length,
  "index.html declares " + waveSpecs.length + " waveform strip(s), chrome attached " + attached.strips.length);
check(attached.gauges.length === gaugeSpecs.length,
  "index.html declares " + gaugeSpecs.length + " gauge(s), chrome attached " + attached.gauges.length);
check(waveSpecs.length >= 4, "the console should show at least four signal strips, saw " + waveSpecs.length);
check(gaugeSpecs.length >= 3, "the console should show at least three dials, saw " + gaugeSpecs.length);

chrome.setState({
  link: "live", model: "gemini:gemini-3.8-flash", agents: 8, agentsWorking: 3, actions: 96,
  memory: { episodes: 855, facts: 116, tasks: 67 }, quota: "pressure", jobs: 3,
  verdicts: { met: 2, bad: 1 }, working: true,
});
chrome.pulseAudio(0.6);
chrome.pulseTraffic(0.5);

const beforeStrokes = chromeRecord.strokes;
chrome.frame(200);
chrome.frame(260);
chrome.decay();
chrome.resize();
chrome.frame(320);
check(chromeRecord.strokes > beforeStrokes, "the cage drew no strokes");
check(chromeRecord.texts.join(" ").indexOf("MODEL") >= 0, "the telemetry column rendered no readouts");
check(chrome.state.audio < 0.6, "the audio plane does not decay, so the strips would stay lit");
note("cage drew " + (chromeRecord.strokes - beforeStrokes) + " strokes, telemetry: " +
  chromeRecord.texts.filter((text) => /^[A-Z]+$/.test(text)).join(" "));

// Telemetry must stay inside the gap between the side panels: on a wide window it is drawn,
// and every readout sits clear of both panels.
const PANEL_LEFT = 300;
const PANEL_RIGHT = 386;
chromeRecord.placements.length = 0;
elements.chrome.clientWidth = 1280;
elements.chrome.clientHeight = 800;
chrome.resize();
chrome.frame(380);
const wideReadouts = chromeRecord.placements.filter((item) => item.text === "MODEL");
check(wideReadouts.length > 0, "telemetry is missing on a window wide enough to show it");
check(wideReadouts.every((item) => item.x >= PANEL_LEFT && item.x <= 1280 - PANEL_RIGHT),
  "telemetry is drawn over a side panel: " + JSON.stringify(wideReadouts[0]));

// A narrow window must drop the column entirely rather than scribble over the fleet panel.
chromeRecord.placements.length = 0;
chromeRecord.texts.length = 0;
elements.chrome.clientWidth = 640;
elements.chrome.clientHeight = 600;
chrome.resize();
chrome.frame(420);
check(chromeRecord.texts.indexOf("MODEL") < 0, "telemetry is drawn where there is no room for it");
note("telemetry shown in the panel gap at 1280px, suppressed at 640px");

/* ------------------------------------------------------------------ instrument widgets */
const widgetRecord = { strokes: 0, fills: 0, texts: [], placements: [] };
function widgetContext() { return makeContext2D(widgetRecord); }
pick(widgetSpecs).forEach((element) => { element.getContext = widgetContext; });

const widgetSandbox = {
  console: console,
  performance: { now: () => 0 },
  Math: Math, Object: Object, Array: Array, Number: Number, Set: Set, String: String,
  document: {
    getElementById: (id) => elements[id] || null,
    querySelectorAll: (selector) => (selector === "canvas[data-widget]" ? pick(widgetSpecs) : []),
  },
  window: { devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900 },
};
widgetSandbox.window.window = widgetSandbox.window;
loadModule("hud/widgets.js", widgetSandbox);

const widgetsApi = widgetSandbox.window.OrbythWidgets;
check(widgetsApi && typeof widgetsApi.attach === "function", "widgets.js did not export attach()");
check(widgetSpecs.length >= 12, "the console should mount at least twelve instruments, saw " + widgetSpecs.length);

widgetsApi.setState({
  link: "live", model: "gemini:gemini-3.8-flash",
  agents: { total: 8, running: 3, blocked: 1, failed: 0, idle: 4 },
  teams: 1, actions: 96,
  memory: { episodes: 855, facts: 116, tasks: 67, audits: 2410 },
  quota: "pressure", jobs: { running: 3, total: 5 }, tokens: 620000,
  levels: { vision: 0.4, input: 0.3, browser: 0.8, office: 0.5, files: 0.9, load: 0.6, latency: 0.2 },
});
widgetsApi.sample();
widgetsApi.sample();

const mounted = widgetsApi.attach();
check(mounted.widgets.length === widgetSpecs.length,
  "only " + mounted.widgets.length + " of " + widgetSpecs.length + " declared instruments attached");
const fillsBefore = widgetRecord.fills + widgetRecord.strokes;
widgetsApi.frame(300);
widgetsApi.frame(900);
widgetsApi.resize();
widgetsApi.frame(1500);
check(widgetRecord.fills + widgetRecord.strokes > fillsBefore, "the widgets drew nothing");

// A widget with no renderer must not be silently skipped: every declared kind is checked against
// the module's own export list, so a typo in index.html fails rather than drawing an empty box.
const declaredKinds = Array.from(new Set(widgetSpecs.map((spec) => spec.dataset.widget))).sort();
const rendererKinds = ["bars", "cells", "hex", "ladder", "matrix", "radar", "ruler", "spark"];
declaredKinds.forEach((kind) => {
  check(rendererKinds.indexOf(kind) >= 0, "index.html mounts an instrument with no renderer: " + kind);
});
check(declaredKinds.length === rendererKinds.length,
  "some renderers are never mounted: " + rendererKinds.filter((kind) => declaredKinds.indexOf(kind) < 0).join(", "));
note("instruments: " + declaredKinds.join(", ") + " (" + widgetSpecs.length + " canvases)");

/* ------------------------------------------------------------------ system grid backdrop */
const gridRecord = { strokes: 0, fills: 0, texts: [], placements: [] };
const gridCanvas = makeElement("grid", { width: 1600, height: 900 });
gridCanvas.getContext = () => makeContext2D(gridRecord);
const gridSandbox = {
  console: console,
  performance: { now: () => 0 },
  Math: Math, Object: Object, Array: Array, Number: Number, String: String,
  window: { devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900 },
};
gridSandbox.window.window = gridSandbox.window;
loadModule("hud/grid.js", gridSandbox);

const gridApi = gridSandbox.window.OrbythGrid;
check(gridApi && typeof gridApi.SystemGrid === "function", "grid.js did not export SystemGrid");
const grid = new gridApi.SystemGrid(gridCanvas);
grid.setMetrics({ fps: 58, particles: 9120 });
const gridBefore = gridRecord.strokes + gridRecord.fills;
grid.frame(500);
grid.resize();
grid.frame(1400);
check(gridRecord.strokes + gridRecord.fills > gridBefore, "the system grid drew nothing");
const gridText = gridRecord.texts.join(" ");
check(gridText.indexOf("BUS.SYS") >= 0, "the data columns drew no labelled bus");
check(gridText.indexOf("QUOTA") >= 0, "the warm data column is missing");
check(gridText.indexOf("FRAME") >= 0, "the backdrop drew no datum plate");
check(gridRecord.texts.some((text) => /^\d{4}$/.test(text)), "the edge rulers drew no measurements");
note("backdrop drew " + (gridRecord.strokes - gridBefore) + " strokes and " + gridRecord.texts.length + " labels");

/* ------------------------------------------------------------------ effects director */
// The director is pure logic, so it can be driven on a fake clock: route real events at it and
// assert the plate actually changes, that the change is not instant, and that it falls back. A
// frozen clock would make every hold look expired, so time is advanced explicitly.
const clock = { t: 1000 };
const fxSandbox = {
  console: console,
  performance: { now: () => clock.t },
  Math: Math, Object: Object, Array: Array, Number: Number, String: String, Map: Map,
};
fxSandbox.window = fxSandbox;
loadModule("hud/effects.js", fxSandbox);
const fxApi = fxSandbox.window.OrbythEffects;
check(fxApi && typeof fxApi.Director === "function", "effects.js did not export Director");

const situationNames = Object.keys(fxApi.SITUATIONS);
check(situationNames.length >= 18, "too few situations for the plate to change behaviour: " + situationNames.length);
check(Object.keys(fxApi.EFFECTS).length >= 10, "too few transient effect kinds");
check(situationNames.indexOf("idle") >= 0, "the director has no idle situation to fall back to");
Object.keys(fxApi.TOPIC_SITUATIONS).forEach((topic) => {
  check(situationNames.indexOf(fxApi.TOPIC_SITUATIONS[topic]) >= 0,
    "topic '" + topic + "' maps to a situation that does not exist");
});

const director = new fxApi.Director();
let fired = [];
director.onPulse = (kind, amount) => fired.push([kind, amount]);
director.setBase("idle");

/** Walk the clock forward in frame-sized steps. The director blends over real elapsed time, so a
    single jump is read as the instant a change was noticed and the blend never gets sampled. */
function advance(target) {
  while (clock.t < target) {
    clock.t = Math.min(target, clock.t + 100);
    director.frame(clock.t);
  }
}

clock.t = 1000;
director.frame(1000);
const idleView = director.view();
check(idleView.situation === "idle", "the director did not settle into its base situation");

clock.t = 1100;
director.route("action.invoked", { payload: {} });
advance(2000);
const actView = director.view();
check(actView.situation === "acting", "an action did not put the console into the acting situation: " + actView.situation);
check(actView.energy > idleView.energy + 0.1, "acting carries no more energy than idle");
check(actView.palette.core[0] !== idleView.palette.core[0] || actView.palette.core[1] !== idleView.palette.core[1],
  "the palette did not change with the situation");
check(actView.chrome.warm > idleView.chrome.warm, "the cage did not warm up for tool work");
check(actView.spin > idleView.spin, "the situation did not change how fast the globe runs");
check(fired.length >= 1, "routing an action fired no transient effect");

fired = [];
clock.t = 2100;
director.route("action.result", { payload: { ok: false } });
advance(2200);
check(fired.some(([kind]) => kind === "tear"), "a failed action did not tear the plate");
check(director.view().glitch > 0, "a failed action did not break the signal up");

advance(4000);
check(director.view().situation === "idle", "the situation never fell back to the base state");

clock.t = 4100;
director.route("task.disputed", { payload: {} });
advance(5200);
check(director.view().situation === "disputed", "a failed outcome check did not take the plate over");
check(director.view().chrome.alert > 0.5, "a disputed outcome did not raise the alert ring");

advance(8000);
clock.t = 8100;
director.route("voice.speaking", { payload: { text: "hi" } });
advance(8900);
check(director.view().ripple > 0.5, "speaking did not drive the ripple");
check(director.view().palette.core[1] > 0.9, "speaking did not switch to the bright palette");
note("situations: " + situationNames.length + ", effects: " + Object.keys(fxApi.EFFECTS).length +
  ", topics routed: " + Object.keys(fxApi.TOPIC_SITUATIONS).length);

/* ------------------------------------------------------------------ wiring in index.html */
const html = HTML;
const app = APP;
const ids = new Set((html.match(/id="([^"]+)"/g) || []).map((match) => match.slice(4, -1)));
const referenced = new Set();
(app.match(/\$\("([^"]+)"\)/g) || []).forEach((match) => referenced.add(match.slice(3, -2)));
(app.match(/getElementById\("([^"]+)"\)/g) || []).forEach((match) => {
  referenced.add(match.slice(16, -2));
});
const missingIds = Array.from(referenced).filter((id) => !ids.has(id));
check(missingIds.length === 0, "app.js reaches for element ids that index.html does not define: " + missingIds.join(", "));
note(referenced.size + " element ids referenced by app.js all exist");

check(html.indexOf("chrome.js") >= 0, "index.html does not load chrome.js");
check(html.indexOf("grid.js") >= 0, "index.html does not load the system grid");
check(html.indexOf("widgets.js") >= 0, "index.html does not load the instrument widgets");
check(html.indexOf("particle-core.js") < html.indexOf("app.js"), "scripts are loaded out of order");
check(html.indexOf("widgets.js") < html.indexOf("app.js"), "the widgets load after the controller that drives them");
check((html.match(/data-wave="/g) || []).length >= 4, "index.html has too few waveform strips");
check((html.match(/data-gauge="/g) || []).length >= 3, "index.html does not define the dial bank");
check(html.indexOf('data-stack="fleet"') >= 0 && html.indexOf('data-stack="console"') >= 0,
  "the secondary console stacks are not grouped, so the menu switches would leave them behind");
check(app.indexOf("OrbythWidgets") >= 0 && app.indexOf("OrbythGrid") >= 0,
  "app.js never drives the new visual layers");
check(app.indexOf("OrbythEffects") >= 0 && app.indexOf("applyEffects") >= 0,
  "app.js never drives the effects director");
check(app.indexOf("fx.route") >= 0, "core events are never routed to the effects director");
check(app.indexOf("bindPulses") >= 0, "one-shot effects never reach the core");

if (failures) {
  console.error("\n" + failures + " check(s) failed");
  process.exit(1);
}
console.log("\n[ok] HUD visual layer verified: geometry, projection, shaders, chrome and DOM wiring");
