"use strict";

/**
 * DIY-ECG Frontend, built with p5.js.
 *
 * index.html stays empty; every DOM element (control panel, metrics, chart)
 * is created below in setup(). Data arrives over Socket.IO as three message
 * types (see README): `ecg_frame` (full buffer, sent once on connect),
 * `ecg_delta` (incremental updates), and `ecg_meta` (status/BPM/filters).
 */

// ---- Config ----
const RING_CAPACITY = 2000; // ~10s of samples at 200 Hz
const DRAW_FPS = 10; // matches the backend's emit rate; no need to draw faster
const GRID_SPACING_PX = 100;
const PERF_SAMPLE_SIZE = 30; // rolling average window for drawSignal() timing

const COLOR_LINE = "#2dd4bf";
const COLOR_GRID = "rgba(255, 255, 255, 0.12)";
const COLOR_THRESHOLD = "#fbbf24";
const COLOR_TEXT = "#f3f4f6";
const COLOR_CHART_BG = "#252f3c";

// ---- Signal state ----
let socket;
let buffer = []; // [{t: relativeMs, y: value}, ...] oldest -> newest
let baseT0 = null; // first absolute t0 seen; used to compute relative time
let threshold = null;
let lastYRange = null; // {min, max} of the most recently drawn frame
let paused = false;
let filters = { hp: true, no: true, tp: true, am: true };
const filterCheckboxes = {};
let drawSignalTimes = []; // rolling buffer of drawSignal() durations in ms

// ---- UI elements (created in setup()) ----
let chartHolder;
let connectionLabel;
let statusValue, sampleCountValue, bpmValue, polarityValue, samplingRateValue;
let showThresholdCheckbox;
let showPerfCheckbox;
let pauseButton;

function setup() {
  buildUI();
  frameRate(DRAW_FPS);
  connectSocket();
}

function draw() {
  if (paused) return; // leave the last rendered frame on screen

  background(COLOR_CHART_BG);
  drawGrid();

  if (buffer.length < 2) {
    drawPlaceholderText();
    return;
  }

  const t0 = performance.now();
  drawSignal();
  recordDrawSignalTime(performance.now() - t0);

  if (showThresholdCheckbox.checked() && threshold !== null) {
    drawThresholdLine();
  }
  drawYLabels();

  if (showPerfCheckbox.checked()) {
    drawPerfOverlay();
  }
}

function windowResized() {
  resizeCanvas(chartHolder.elt.clientWidth, chartHolder.elt.clientHeight);
}

/* ==================== UI construction ==================== */

function buildUI() {
  const app = createDiv().id("app");
  buildControlPanel(app);
  buildMainPanel(app);
}

function buildControlPanel(parent) {
  const panel = createDiv().class("control-panel").parent(parent);

  createElement("h2", "Signal Filters").parent(panel);
  buildFilterCheckbox(panel, "hp", "High-pass");
  buildFilterCheckbox(panel, "no", "Notch");
  buildFilterCheckbox(panel, "tp", "Low-pass");
  buildFilterCheckbox(panel, "am", "Adaptive mean");

  createElement("h2", "Display").parent(panel);
  showThresholdCheckbox = createCheckbox("Show threshold", false)
    .class("control-row")
    .parent(panel);
  showPerfCheckbox = createCheckbox("Show performance", false)
    .class("control-row")
    .parent(panel);

  createButton("Clear buffer").parent(panel).mousePressed(clearBuffer);
  createButton("Save buffer as CSV").parent(panel).mousePressed(saveBufferAsCsv);
  pauseButton = createButton("Pause display").parent(panel).mousePressed(togglePause);
}

function buildFilterCheckbox(parent, key, label) {
  const box = createCheckbox(label, filters[key]).class("control-row").parent(parent);
  box.changed(() => {
    filters[key] = box.checked();
    socket.emit("set_filters", filters);
  });
  filterCheckboxes[key] = box;
}

function buildMainPanel(parent) {
  const main = createElement("main").parent(parent);

  const statusBar = createDiv().class("status-bar").parent(main);
  const titleBlock = createDiv().parent(statusBar);
  createElement("h1", "DIY-ECG Live Viewer").parent(titleBlock);
  createElement("p", "WebSocket-/p5.js-based rendering of UNO Q Bridge data").parent(titleBlock);
  connectionLabel = createDiv("Disconnected").class("connection").parent(statusBar);

  const metrics = createDiv().class("metrics").parent(main);
  statusValue = buildMetric(metrics, "Status", "–");
  sampleCountValue = buildMetric(metrics, "Samples (frame)", "0");
  bpmValue = buildMetric(metrics, "BPM", "–");
  polarityValue = buildMetric(metrics, "Polarity", "–");
  samplingRateValue = buildMetric(metrics, "Sampling rate", "–");

  chartHolder = createDiv().id("chart-holder").parent(main);
  createCanvas(chartHolder.elt.clientWidth, chartHolder.elt.clientHeight).parent(chartHolder);
}

function buildMetric(parent, label, initial) {
  const metric = createDiv().class("metric").parent(parent);
  createElement("span", label).class("label").parent(metric);
  return createElement("span", initial).class("value").parent(metric);
}

/* ==================== Chart drawing ==================== */

function drawGrid() {
  stroke(COLOR_GRID);
  strokeWeight(1);
  for (let x = 0; x <= width; x += GRID_SPACING_PX) line(x, 0, x, height);
  for (let y = 0; y <= height; y += GRID_SPACING_PX) line(0, y, width, y);
}

function drawSignal() {
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const s of buffer) {
    if (s.y < yMin) yMin = s.y;
    if (s.y > yMax) yMax = s.y;
  }
  if (yMin === yMax) {
    yMin -= 1;
    yMax += 1;
  }
  const pad = (yMax - yMin) * 0.12;
  yMin -= pad;
  yMax += pad;
  lastYRange = { min: yMin, max: yMax };

  const tMin = buffer[0].t;
  const tMax = buffer[buffer.length - 1].t;

  noFill();
  stroke(COLOR_LINE);
  strokeWeight(2);
  beginShape();
  for (const s of buffer) {
    const x = map(s.t, tMin, tMax, 0, width);
    const y = map(s.y, yMin, yMax, height, 0);
    vertex(x, y);
  }
  endShape();
}

function drawThresholdLine() {
  if (!lastYRange) return;
  const y = map(threshold, lastYRange.min, lastYRange.max, height, 0);
  stroke(COLOR_THRESHOLD);
  strokeWeight(1.5);
  line(0, y, width, y);
}

function drawYLabels() {
  if (!lastYRange) return;
  noStroke();
  fill(COLOR_TEXT);
  textSize(12);
  textAlign(RIGHT, TOP);
  text(Math.round(lastYRange.max), width - 8, 8);
  textAlign(RIGHT, BOTTOM);
  text(Math.round(lastYRange.min), width - 8, height - 8);
}

function drawPlaceholderText() {
  noStroke();
  fill(COLOR_TEXT);
  textAlign(CENTER, CENTER);
  textSize(14);
  text("Waiting for data from the microcontroller…", width / 2, height / 2);
}

// Tracks how long drawSignal() itself takes, independent of the fixed
// frameRate() cap, so optimizations there show up even when the frame
// budget is already met.
function recordDrawSignalTime(ms) {
  drawSignalTimes.push(ms);
  if (drawSignalTimes.length > PERF_SAMPLE_SIZE) drawSignalTimes.shift();
}

function averageDrawSignalTime() {
  if (drawSignalTimes.length === 0) return 0;
  const sum = drawSignalTimes.reduce((total, ms) => total + ms, 0);
  return sum / drawSignalTimes.length;
}

function drawPerfOverlay() {
  noStroke();
  fill(COLOR_TEXT);
  textSize(12);
  textAlign(LEFT, TOP);
  const fps = frameRate().toFixed(1);
  const ms = averageDrawSignalTime().toFixed(2);
  text(`${fps} FPS · drawSignal: ${ms} ms`, 8, 8);
}

/* ==================== Buffer + controls ==================== */

function pushSample(tRel, y) {
  buffer.push({ t: tRel, y });
  if (buffer.length > RING_CAPACITY) buffer.shift();
}

function clearBuffer() {
  socket.emit("clear_buffer", {});
  buffer = [];
  baseT0 = null;
  threshold = null;
  paused = false;
  pauseButton.html("Pause display");
}

function togglePause() {
  paused = !paused;
  pauseButton.html(paused ? "Resume display" : "Pause display");
}

function saveBufferAsCsv() {
  if (buffer.length === 0) {
    alert("No buffer data available.");
    return;
  }

  const thresholdText = threshold === null ? "" : String(threshold);
  const lines = ["t_ms,ecg_mv,threshold"];
  for (const s of buffer) {
    lines.push(`${s.t},${s.y},${thresholdText}`);
  }

  const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = buildCsvFilename();
  link.click();
  URL.revokeObjectURL(url);
}

function buildCsvFilename() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  const stamp = [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    pad(now.getHours()),
    pad(now.getMinutes()),
    pad(now.getSeconds()),
  ].join("");
  return `diy-ecg-buffer-${stamp}.csv`;
}

/* ==================== Socket.IO ==================== */

function connectSocket() {
  socket = io();

  socket.on("connect", () => {
    connectionLabel.html("Connected");
    connectionLabel.addClass("online");
    socket.emit("request_status", {});
  });

  socket.on("disconnect", () => {
    connectionLabel.html("Disconnected");
    connectionLabel.removeClass("online");
    buffer = [];
    baseT0 = null;
  });

  socket.on("ecg_meta", updateMetrics);
  socket.on("ecg_frame", handleFullFrame);
  socket.on("ecg_delta", handleDelta);
}

function updateMetrics(meta) {
  statusValue.html(meta.status ?? "–");
  sampleCountValue.html(meta.last_count ?? 0);
  bpmValue.html(meta.bpm ?? "–");
  polarityValue.html(meta.polarity ?? "–");
  samplingRateValue.html(
    Number.isFinite(meta.sampling_rate_hz) ? `${meta.sampling_rate_hz.toFixed(1)} Hz` : "–"
  );

  if (meta.filters) {
    for (const key in filterCheckboxes) {
      if (key in meta.filters) filterCheckboxes[key].checked(!!meta.filters[key]);
    }
  }
}

// Full buffer snapshot; only used to seed the chart right after connecting.
function handleFullFrame(payload) {
  if (paused || buffer.length > 0 || !payload || !payload.signal) return;

  const sig = payload.signal;
  baseT0 = sig.t0 ?? baseT0;
  for (let i = 0; i < sig.y.length; i++) pushSample(sig.t[i], sig.y[i]);
  threshold = sig.threshold ?? threshold;
}

// Incremental update: payload = { t0, y[], dt[], threshold? }
function handleDelta(payload) {
  if (paused || !payload || !payload.y || !payload.dt) return;
  if (baseT0 === null) baseT0 = payload.t0;

  let absoluteT = payload.t0;
  for (let i = 0; i < payload.y.length; i++) {
    if (i > 0) absoluteT += payload.dt[i];
    pushSample(absoluteT - baseT0, payload.y[i]);
  }
  if (payload.threshold != null) threshold = payload.threshold;
}
