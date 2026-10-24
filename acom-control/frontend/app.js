// WebSocket client, REST calls, UI update logic. See PI_CONTROL_PLAN.md
// step 6 "Frontend dashboard" and "Backend vs. frontend logging".

import { initThemeSwitcher, loadTheme } from "./theme.js";
import { mountGauge, updateGauge, resolveWidget } from "./gauges.js";

const BAND_NAMES = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"];
const ANTENNA_TYPES = ["Sharp", "Regular", "Wide", "Fixed"];

// Mirrors protocol.py's CAT_*_NAMES dicts exactly - these are fixed,
// protocol-defined values, not backend config, so hardcoding here
// matches BAND_NAMES's own pattern above rather than round-tripping a
// static list through an API call. Command Set only names 0-5 per the
// amp's own protocol doc (section 5.1) - 6-15 are valid 4-bit values
// the amp accepts, just never given a name in that document (the
// amp's own front-panel menu almost certainly has friendlier names for
// all of them, since it postdates this 2018 doc).
const CAT_INTERFACE_TYPES = ["None", "RS232", "TTL", "BCD", "Analog"];
const CAT_COMMAND_SETS = [
  "None", "ICOM", "Yaesu FT-450 family", "Yaesu FT-817 / FT-897 family",
  "Yaesu FT-1000MP family", "Elecraft K3 / Kenwood TS-480",
];
const CAT_BAUD_RATES = ["None", "1200", "4800", "9600", "19200", "38400", "57600"];

// Exact strings protocol.py's mode_name() emits for these three categories
// (see MODE_CATEGORY_NAMES) - the OPR/STB toggle button keys off these.
const MODE_NAME_STB = "STB Mode";
const MODE_NAME_OPR_RX = "OPR/RX Mode";
const MODE_NAME_OPR_TX = "OPR/TX Mode";

// Exact string protocol.py's ATU_LABELS[ATU_LABEL_BYPASS] emits - the ATU
// Bypass button and status chip key off this, same pattern as the mode
// names above.
const ATU_LABEL_NAME_BYPASS = "ATU In Bypass";

// Forward power is rendered separately as a full-width "hero" bar (see
// #power-out-hero in index.html) rather than inside the regular grid.
const HERO_GAUGE_DEF = { id: "forward_power_w", label: "Forward Power", unit: "W", min: 0, max: () => appState.maxPowerW || 1500 };

// Ordered and flagged to match the real Basic Screen's own layout (PA Temp +
// Reflected Power as metered bargraphs; Input Power + PA DC Current as bare
// digit readouts with no bargraph - see Figure 3-2 in the 1200S manual).
// swr_calc has no real-screen counterpart (it's computed, not read from the
// amp - see computeSwr()) so it gets its own full-width row in the grid.
const GAUGE_DEFS = [
  { id: "temp_pam1_c", label: "PA Temperature", unit: "°C", min: 0, max: () => 100 },
  { id: "reflected_power_w", label: "Reflected Power", unit: "W", min: 0, max: () => Math.max(50, (appState.maxPowerW || 1500) * 0.2) },
  { id: "input_power_w", label: "Input Power", unit: "W", min: 0, max: () => 100, plain: true },
  // Shown in A, not raw mA, to match the manual's own spec and the amp's own
  // front panel ("PA DC CURRENT must be between 32 and 36 A" at 1000W) - see
  // id1_a derived in renderGauges(). A max of 40A gives headroom above the
  // documented 32-36A full-power range.
  { id: "id1_a", label: "PA DC Current", unit: "A", min: 0, max: () => 40, plain: true },
  { id: "swr_calc", label: "SWR", unit: ":1", min: 1, max: () => 5 },
];

const GAUGE_DEF_BY_ID = Object.fromEntries([HERO_GAUGE_DEF, ...GAUGE_DEFS].map((d) => [d.id, d]));

// Gauges whose peak resets the instant the amp *enters* OPR/TX (rising edge
// of mode_name becoming "OPR/TX Mode" - see updateTxEdgeState()), rather
// than at idle. Per Tyler: "peak hold for everything, hold until reset...
// it should only reset when it first goes into transmit" - so the peak of
// a just-finished transmission stays on screen through the idle period
// instead of being erased the moment power drops back to ~0. Temperature
// isn't in this set, so it keeps its original behavior: peak only ever
// grows, manual reset only (click the badge).
const PEAK_RESET_ON_TX_IDS = new Set(["forward_power_w", "reflected_power_w", "swr_calc", "input_power_w", "id1_a"]);

// Average power during the in-progress (or just-finished) transmission -
// same hold-until-next-tx lifecycle as the peak above: accumulates while
// appState.inTx is true, freezes once TX ends, resets on the next TX's
// rising edge. Scoped to forward_power_w per the request ("it would be nice
// to see that I averaged 400 watts but peak was 560") - add more ids here
// if other gauges ever want the same treatment.
const TX_AVERAGE_IDS = new Set(["forward_power_w"]);

const peaks = {};
const txAverageAccumulators = {}; // id -> { sum, count }
const txAverages = {}; // id -> last computed average (frozen once TX ends)

function computeSwr(forwardW, reflectedW) {
  if (!forwardW || forwardW <= 0 || reflectedW == null) return 1.0;
  const rho = Math.sqrt(Math.min(0.98, Math.max(0, reflectedW / forwardW)));
  return (1 + rho) / (1 - rho);
}

function updatePeak(id, value) {
  if (value === null || value === undefined || Number.isNaN(value)) return peaks[id] ?? null;
  if (PEAK_RESET_ON_TX_IDS.has(id) && appState.justEnteredTx) {
    peaks[id] = value;
  } else {
    peaks[id] = Math.max(peaks[id] ?? value, value);
  }
  return peaks[id];
}

function updateTxAverage(id, value) {
  if (!TX_AVERAGE_IDS.has(id)) return null;
  if (appState.justEnteredTx) {
    txAverageAccumulators[id] = { sum: 0, count: 0 };
  }
  if (appState.inTx && value !== null && value !== undefined && !Number.isNaN(value)) {
    const acc = txAverageAccumulators[id] || (txAverageAccumulators[id] = { sum: 0, count: 0 });
    acc.sum += value;
    acc.count += 1;
    txAverages[id] = acc.sum / acc.count;
  }
  return txAverages[id] ?? null;
}

function formatPeakBadge(id, peakValue, avgValue) {
  const unit = GAUGE_DEF_BY_ID[id]?.unit;
  const fmt = (v) => (v === null || v === undefined || Number.isNaN(v) ? "--" : `${Math.round(v * 100) / 100}${unit ? " " + unit : ""}`);
  const text = `Pk: ${fmt(peakValue)}`;
  return TX_AVERAGE_IDS.has(id) ? `${text}   Avg: ${fmt(avgValue)}` : text;
}

function ensurePeakBadge(el, id) {
  let badge = el.querySelector(".gauge-peak");
  if (!badge) {
    badge = document.createElement("div");
    badge.className = "gauge-peak";
    badge.title = "Click to reset";
    badge.addEventListener("click", () => {
      peaks[id] = null;
      if (TX_AVERAGE_IDS.has(id)) {
        txAverageAccumulators[id] = { sum: 0, count: 0 };
        txAverages[id] = null;
      }
      badge.textContent = formatPeakBadge(id, null, null);
    });
    el.appendChild(badge);
  }
  return badge;
}

const appState = {
  maxPowerW: 1500,
  lastStatus: null,
  // Cached rather than re-read from localStorage on every telemetry tick
  // (several times a second over the WebSocket) - only updated via the
  // "acom-theme-change" event, which fires on the rare actual change.
  theme: loadTheme(),
  // TX edge-detection state shared by the peak-hold reset and TX average
  // above - see updateTxEdgeState().
  prevModeName: null,
  inTx: false,
  justEnteredTx: false,
  // Settings (the gear icon) can be opened from either the normal dashboard
  // or the power-off screen - see updateScreenVisibility().
  setupOpen: false,
  // Software update screen (the header's update icon) - same deal as
  // setupOpen, mutually exclusive with it (see openSetupScreen()/
  // openUpdateScreen()).
  updateScreenOpen: false,
};

let ws = null;
let wsReconnectDelayMs = 1000;
const WS_RECONNECT_MAX_MS = 15000;

// =============================================================================
// Error reporting (fire-and-forget, never blocks the UI)
// =============================================================================

function reportFrontendError(level, message, stack) {
  try {
    fetch("/api/frontend-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, message: String(message), stack: stack ? String(stack) : null, url: location.href }),
      keepalive: true,
    }).catch(() => {});
  } catch {
    // swallow - logging must never itself throw
  }
}

window.addEventListener("error", (event) => {
  reportFrontendError("error", event.message, event.error && event.error.stack);
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  reportFrontendError("error", reason && reason.message ? reason.message : String(reason), reason && reason.stack);
});

async function apiFetch(path, options) {
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (err) {
    reportFrontendError("error", `API ${options?.method || "GET"} ${path} threw: ${err.message}`, err.stack);
    throw err;
  }
  if (!resp.ok) {
    const bodyText = await resp.text().catch(() => "");
    reportFrontendError("warning", `API ${options?.method || "GET"} ${path} failed: ${resp.status} ${bodyText}`);
    const err = new Error(`${options?.method || "GET"} ${path} failed (${resp.status}): ${bodyText || resp.statusText}`);
    err.status = resp.status;
    err.body = bodyText;
    throw err;
  }
  return resp;
}

// =============================================================================
// User-visible action errors (distinct from reportFrontendError, which is
// silent/for diagnostics - this is what tells the operator a click didn't
// actually do anything).
// =============================================================================

let actionErrorNextId = 0;
const actionErrorEntries = [];

/** Multiple calls stack rather than overwrite one another - each entry
 * gets its own row and its own 6s timeout, so a burst of errors is all
 * visible at once instead of the newest silently replacing the last. */
function showActionError(message, kind = "error") {
  const id = actionErrorNextId++;
  actionErrorEntries.push({ id, message, kind });
  setTimeout(() => {
    const idx = actionErrorEntries.findIndex((e) => e.id === id);
    if (idx !== -1) actionErrorEntries.splice(idx, 1);
    renderActionErrors();
  }, 6000);
  renderActionErrors();
}

function renderActionErrors() {
  const el = document.getElementById("action-error-banner");
  if (!el) return;
  el.innerHTML = "";
  actionErrorEntries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "action-error-item" + (entry.kind === "info" ? " action-error-info" : "");
    row.textContent = entry.message;
    el.appendChild(row);
  });
  el.hidden = actionErrorEntries.length === 0;
}

/** Wraps a click handler so a thrown/rejected apiFetch surfaces to the
 * operator via showActionError() instead of failing silently. */
function withErrorBanner(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (err) {
      showActionError(err.message || String(err));
    }
  };
}

// =============================================================================
// WebSocket + status rendering
// =============================================================================

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/status`);

  ws.addEventListener("open", () => {
    wsReconnectDelayMs = 1000;
  });

  ws.addEventListener("message", (event) => {
    try {
      const state = JSON.parse(event.data);
      render(state);
    } catch (err) {
      reportFrontendError("error", "Failed to parse WebSocket status payload: " + err.message, err.stack);
    }
  });

  ws.addEventListener("error", () => {
    reportFrontendError("warning", "WebSocket error on /ws/status");
  });

  ws.addEventListener("close", () => {
    setTimeout(connectWebSocket, wsReconnectDelayMs);
    wsReconnectDelayMs = Math.min(wsReconnectDelayMs * 2, WS_RECONNECT_MAX_MS);
  });
}

function render(state) {
  appState.lastStatus = state;
  appState.maxPowerW = state.max_power_w || appState.maxPowerW;
  updateTxEdgeState(state);

  renderStatusRow(state);
  renderGauges(state);
  renderFreqReadout(state);
  renderWarningBanner(state);
  renderOprToggle(state);
  renderAtuBypassToggle(state);
  renderPowerOnButton(state);
  updateScreenVisibility();
  if (appState.setupOpen) updateAntennaSetupVisibility();
}

// Three mutually-exclusive top-level screens: the normal dashboard, Setup
// (opened via the header's gear icon), and a reduced power-off screen (just
// the logo + Power On) shown whenever the amp is confirmed off. Setup takes
// priority over power-off so Settings stays reachable either way - per
// Tyler: close all the normal dashboard windows while off, but keep Setup
// accessible from the header regardless of power state.
function currentScreen() {
  if (appState.setupOpen) return "setup";
  if (appState.updateScreenOpen) return "update";
  return appState.lastStatus?.power_state === "off" ? "power-off" : "dashboard";
}

function updateScreenVisibility() {
  const screen = currentScreen();
  document.getElementById("dashboard").hidden = screen !== "dashboard";
  document.getElementById("setup-screen").hidden = screen !== "setup";
  document.getElementById("update-screen").hidden = screen !== "update";
  document.getElementById("power-off-screen").hidden = screen !== "power-off";
}

// Single shared "did a transmission just start" computation, used by both
// the peak-hold reset (updatePeak()) and the TX average (updateTxAverage())
// above - both reset on the same rising edge of mode_name becoming
// "OPR/TX Mode", so this runs once per telemetry update rather than each
// of them independently comparing against the previous mode.
function updateTxEdgeState(state) {
  const modeName = state.telemetry ? state.telemetry.mode_name : null;
  appState.inTx = modeName === MODE_NAME_OPR_TX;
  appState.justEnteredTx = appState.inTx && appState.prevModeName !== MODE_NAME_OPR_TX;
  appState.prevModeName = modeName;
}

// Colors the merged OPR/STB button from the amp's own reported mode rather
// than click-tracked UI state - see data-opr-state in components.css for
// the color mapping, and the button's click handler in wireControls() for
// the toggle direction this same mode name drives. Per Tyler: just two
// looks - green covers "Operate" broadly (OPR/RX or OPR/TX), red is
// Standby. No separate tx/rx distinction.
function renderOprToggle(state) {
  const btn = document.getElementById("btn-opr-toggle");
  if (!btn) return;
  const modeName = state.telemetry ? state.telemetry.mode_name : null;
  let oprState = "unknown";
  if (modeName === MODE_NAME_STB) oprState = "stb";
  else if (modeName === MODE_NAME_OPR_RX || modeName === MODE_NAME_OPR_TX) oprState = "operate";
  btn.dataset.oprState = oprState;
}

// Hides the main button-bar's Power On button once the amp is confirmed
// on - clicking it then does nothing useful (the mode command it would
// otherwise send is only meaningful from STB, see /api/power's own
// on:true branch), so it's just a dead button to leave visible. Power
// Off is left alone - always needed while on. The power-off screen's own
// standalone Power On button (see updateScreenVisibility()) is a
// separate element, unaffected by this.
function renderPowerOnButton(state) {
  const btn = document.getElementById("btn-power-on");
  if (!btn) return;
  btn.hidden = state.power_state === "on";
}

// Colors the ATU Bypass button from the amp's own last-reported atu_label
// (0x27) rather than click-tracked UI state, same pattern as
// renderOprToggle() above - green while the ATU is actively engaged
// (normal tuned operation), yellow while bypassed (see data-bypass-state
// in components.css for the color mapping, and the button's click handler
// in wireControls() for the direction this same label drives). Per Tyler:
// bypass isn't the "good"/default state, so it shouldn't read as green.
function renderAtuBypassToggle(state) {
  const btn = document.getElementById("btn-atu-bypass");
  if (!btn) return;
  const labelName = state.antenna_band_status ? state.antenna_band_status.atu_label_name : null;
  btn.dataset.bypassState = labelName === ATU_LABEL_NAME_BYPASS ? "bypass" : "active";
}

function renderStatusRow(state) {
  const row = document.getElementById("status-row");
  if (!row) return;
  const t = state.telemetry;
  const bandStatus = state.antenna_band_status;
  const atuActive = bandStatus ? bandStatus.atu_label_name !== ATU_LABEL_NAME_BYPASS : false;
  const chips = [
    chip("Comms", state.comms_ok),
    chip("Mode", true, t ? t.mode_name : "unknown"),
    // On (green) when the ATU is actively engaged, same direction as the
    // ATU Bypass button (see renderAtuBypassToggle()) rather than
    // atu_assigned - "assigned" vs. "none" wasn't a useful distinction
    // here. Text is the configured switch/ATU model (state.tuner_model),
    // not the amp-reported atu_identity, which stays empty on real
    // hardware for the 06AT (see CLAUDE.md open question 1/2). NOTE: this
    // predates updateAntennaSetupVisibility()/renderSetupDetectionNotes()
    // relying on atu_identity.present to auto-show Antenna Setup - if this
    // comment's claim holds on real hardware, that auto-detection won't
    // fire for a 06AT either (falls back to the manual dropdown, same as
    // today) - NEEDS REAL-HARDWARE VERIFICATION, see the plan note.
    chip("ATU", atuActive, state.tuner_model || "none"),
    // Antenna number, not the default_antenna_selected flag - that flag's
    // meaning (whether this antenna is marked "default" in the band/antenna
    // assignment table, not whether it's currently active) wasn't a useful
    // or self-explanatory thing to show here.
    chip("Antenna", true, bandStatus ? bandStatus.antenna_number : "unknown"),
  ];
  row.innerHTML = "";
  chips.forEach((c) => row.appendChild(c));
}

// Two sub-elements (label bar + LED cell) rather than one flat chip, so the
// ACOM theme can stack them into the real status strip's layout - a row of
// amber label segments with a small indicator LED on a black strip directly
// beneath each one (see Figure 3-2 in the 1200S manual). Classic/Modern keep
// the simpler inline pill look via CSS (see components.css).
function chip(label, on, text) {
  const el = document.createElement("span");
  el.className = "status-chip";

  const labelEl = document.createElement("span");
  labelEl.className = "status-chip-label";
  labelEl.textContent = text ? `${label}: ${text}` : label;
  el.appendChild(labelEl);

  const ledCell = document.createElement("span");
  ledCell.className = "status-chip-led";
  const led = document.createElement("span");
  led.className = "led-dot" + (on ? " on" : "");
  ledCell.appendChild(led);
  el.appendChild(ledCell);

  return el;
}

function renderGauges(state) {
  const heroEl = document.getElementById("power-out-hero");
  const grid = document.getElementById("gauge-grid");
  if (!grid && !heroEl) return;
  const theme = appState.theme.style;
  const t = { ...(state.telemetry || {}) };
  t.swr_calc = computeSwr(t.forward_power_w, t.reflected_power_w);
  t.id1_a = t.id1_ma != null ? t.id1_ma / 1000 : null;

  // The hero bar is always a bargraph, regardless of theme - a full-width
  // needle sweep doesn't make sense here.
  mountOrUpdateGauge(heroEl, HERO_GAUGE_DEF, t, theme, "bargraph");

  if (!grid) return;
  GAUGE_DEFS.forEach((def) => {
    let el = grid.querySelector(`[data-gauge-id="${def.id}"]`);
    if (!el) {
      el = document.createElement("div");
      el.className = "gauge-wrapper";
      el.dataset.gaugeId = def.id;
      grid.appendChild(el);
    }
    mountOrUpdateGauge(el, def, t, theme, null);
  });
}

function mountOrUpdateGauge(el, def, telemetry, theme, forceWidget) {
  if (!el) return;
  const value = telemetry[def.id] ?? null;
  // Computed before mount/update so the peak can be threaded into opts.peak
  // for the bargraph's high-water marker (see gauges.js's appendPeakMarker())
  // in the same pass that updates the badge below it.
  const peak = updatePeak(def.id, value);
  const avg = updateTxAverage(def.id, value);
  const opts = { label: def.label, unit: def.unit, value, min: def.min, max: typeof def.max === "function" ? def.max() : def.max, plain: !!def.plain, peak };
  const widget = resolveWidget(theme, opts, forceWidget);

  if (el.dataset.mountedWidget !== widget) {
    el.dataset.mountedWidget = widget;
    mountGauge(el, theme, opts, forceWidget);
  } else {
    updateGauge(el, value, peak);
  }

  const badge = ensurePeakBadge(el, def.id);
  badge.textContent = formatPeakBadge(def.id, peak, avg);
}

function remountAllGauges() {
  const grid = document.getElementById("gauge-grid");
  if (!grid) return;
  grid.innerHTML = "";
  if (appState.lastStatus) renderGauges(appState.lastStatus);
}

document.addEventListener("acom-theme-change", (event) => {
  appState.theme = event.detail;
  remountAllGauges();
});

function renderFreqReadout(state) {
  const el = document.getElementById("freq-readout");
  if (!el) return;
  const khz = state.telemetry ? state.telemetry.carrier_freq_khz : null;
  const mhz = khz ? (khz / 1000).toFixed(3) : "--.---";
  el.innerHTML = `<span class="value">${mhz}</span><span class="unit">MHz</span>`;
}

// Cycling state for the warning banner - module-level rather than inside
// renderWarningBanner() because the rotation needs its own timer, decoupled
// from how often telemetry ticks arrive (several times/sec), not reset on
// every render() call.
const WARNING_ROTATION_MS = 2500;
let warningActiveNames = [];
let warningRotationIndex = 0;
let warningRotationTimer = null;

function stopWarningRotation() {
  if (warningRotationTimer) {
    clearInterval(warningRotationTimer);
    warningRotationTimer = null;
  }
}

// Shows all of the amp's currently active fault names, one at a time,
// cycling through the list when there's more than one - rather than the
// previous "first fault (+N more)" summary, which never surfaced what the
// other faults actually were. Driven directly by state.error_flags on every
// telemetry tick (not a timeout), so it still disappears the instant the
// amp's own active_names list goes empty - same real-time clearing as
// before, just now also handling the multi-fault case.
function renderWarningBanner(state) {
  const el = document.getElementById("warning-banner");
  if (!el) return;
  const errors = state.error_flags;
  const names = errors && errors.active_names ? errors.active_names : [];

  if (names.length === 0) {
    el.hidden = true;
    el.textContent = "";
    warningActiveNames = [];
    warningRotationIndex = 0;
    stopWarningRotation();
    return;
  }

  el.hidden = false;
  // A fault clearing or a new one appearing changes which names are in the
  // list - restart the cycle from the front rather than keep whatever index
  // happened to be mid-rotation, which could now point past the end of a
  // shorter list or land on a stale position.
  const namesChanged =
    names.length !== warningActiveNames.length || names.some((n, i) => n !== warningActiveNames[i]);
  warningActiveNames = names;
  if (namesChanged) warningRotationIndex = 0;

  if (names.length === 1) {
    stopWarningRotation();
    el.textContent = names[0];
    return;
  }

  el.textContent = names[warningRotationIndex % names.length];
  if (!warningRotationTimer) {
    warningRotationTimer = setInterval(() => {
      warningRotationIndex = (warningRotationIndex + 1) % warningActiveNames.length;
      el.textContent = warningActiveNames[warningRotationIndex];
    }, WARNING_ROTATION_MS);
  }
}

// =============================================================================
// Controls
// =============================================================================

function wireControls() {
  document.getElementById("btn-power-on")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/power", postJson({ on: true }))));
  document.getElementById("btn-power-on-standalone")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/power", postJson({ on: true }))));
  document.getElementById("btn-power-off")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/power", postJson({ on: false }))));
  document.getElementById("btn-opr-toggle")?.addEventListener("click", withErrorBanner(() => {
    // Mirrors the real amp's own physical OPR/STB button: toggles between
    // the two states using the amp's own last-reported mode, not a
    // click-tracked guess. Anything other than a confirmed STB reading
    // (including OPR/RX, OPR/TX, or no comms yet) goes to the safe
    // direction (STB).
    const modeName = appState.lastStatus?.telemetry?.mode_name;
    const nextMode = modeName === MODE_NAME_STB ? "opr_rx" : "stb";
    return apiFetch("/api/mode", postJson({ mode: nextMode }));
  }));
  document.getElementById("btn-atu-bypass")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/atu/bypass", { method: "POST" })));
  document.getElementById("btn-atu-tune")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/atu/tune", { method: "POST" })));
  document.getElementById("btn-band-next")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/band", postJson({ direction: "next" }))));
  document.getElementById("btn-band-prev")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/band", postJson({ direction: "prev" }))));
  document.getElementById("btn-antenna-next")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/antenna", postJson({ action: "next" }))));
  document.getElementById("btn-buzzer-test")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/buzzer", postJson({ signal: 1, volume: 50 }))));

  document.getElementById("btn-setup-open")?.addEventListener("click", withErrorBanner(openSetupScreen));
  document.getElementById("btn-setup-close")?.addEventListener("click", closeSetupScreen);
  document.getElementById("btn-setup-save")?.addEventListener("click", withErrorBanner(saveSetup));
  document.getElementById("btn-setup-refresh")?.addEventListener("click", withErrorBanner(refreshFromAmp));
  document.getElementById("btn-cat-settings-save")?.addEventListener("click", withErrorBanner(saveCatSettings));
  document.getElementById("setup-tuner-select")?.addEventListener("change", updateAntennaSetupVisibility);
  document.getElementById("btn-download-logs")?.addEventListener("click", () => window.open("/api/logs", "_blank"));
  document.getElementById("btn-amp-snapshot")?.addEventListener("click", () => window.open("/api/amp-snapshot", "_blank"));
  document.getElementById("btn-update-available")?.addEventListener("click", openUpdateScreen);
  document.getElementById("btn-update-close")?.addEventListener("click", closeUpdateScreen);
  document.getElementById("btn-update-now")?.addEventListener("click", withErrorBanner(applyUpdateNow));
  document.getElementById("update-beta-toggle")?.addEventListener("change", setBetaUpdates);
  document.getElementById("btn-update-check")?.addEventListener("click", checkForUpdateNow);
  document.getElementById("btn-clear-logs")?.addEventListener("click", withErrorBanner(async () => {
    if (!window.confirm("Clear the log view? Downloaded logs will only include entries from this point forward - nothing is deleted from the system log itself.")) return;
    await apiFetch("/api/logs/clear", { method: "POST" });
    await loadSetup();
  }));

  wireAtuDangerZone();
}

function postJson(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

// =============================================================================
// Setup screen (amp/tuner model + Antenna Setup matrix)
// =============================================================================

function openSetupScreen() {
  appState.setupOpen = true;
  appState.updateScreenOpen = false;
  updateScreenVisibility();
  loadSetup();
}

function closeSetupScreen() {
  appState.setupOpen = false;
  updateScreenVisibility();
}

async function refreshFromAmp() {
  // "Refresh from Amp" previously just called loadSetup(), which only
  // re-reads whatever the backend's AmpState already has cached - if the
  // amp never answered the one-shot 0x18/0x1B/0x2A requests sent during
  // the initial startup handshake (observed on real hardware - see
  // CLAUDE.md open question 6), that cache stays empty forever and this
  // button did nothing. POST /api/resync re-sends those requests now, so
  // a later retry can succeed even if the first attempt (fired the
  // instant the serial port opened) was too early for the amp/ATU to
  // answer. resync() only enqueues the request frames and returns
  // immediately - it doesn't wait for the amp's actual reply - so give
  // the round trip + backend parsing a moment before re-reading.
  await apiFetch("/api/resync", { method: "POST" });
  await new Promise((resolve) => setTimeout(resolve, 800));
  await loadSetup();
}

async function loadSetup() {
  const [configResp, antennaResp, portsResp] = await Promise.all([
    apiFetch("/api/config").then((r) => r.json()),
    apiFetch("/api/antenna/config").then((r) => r.json()),
    apiFetch("/api/serial-ports").then((r) => r.json()),
  ]);
  populateConfigForm(configResp);
  populateSerialPortOptions(portsResp, configResp.serial_device);
  populateAntennaMatrix(antennaResp.antennas);
  populateSegmentDeleteBandOptions();
  renderAtuStatusPanel(appState.lastStatus);
  populateCatSettingsOptions();
  renderCatSettingsForm(appState.lastStatus);
  updateAntennaSetupVisibility();
}

// =============================================================================
// CAT / Rig Control (amp's own rear-panel CAT port config)
// =============================================================================

function populateCatSettingsOptions() {
  const interfaceSelect = document.getElementById("cat-interface-type");
  const commandSetSelect = document.getElementById("cat-command-set");
  const baudSelect = document.getElementById("cat-baud-rate");
  if (interfaceSelect && !interfaceSelect.options.length) {
    interfaceSelect.innerHTML = CAT_INTERFACE_TYPES.map((label, i) => `<option value="${i}">${label}</option>`).join("");
  }
  if (commandSetSelect && !commandSetSelect.options.length) {
    const named = CAT_COMMAND_SETS.map((label, i) => `<option value="${i}">${label}</option>`);
    const unnamed = Array.from({ length: 16 - CAT_COMMAND_SETS.length }, (_, j) => {
      const value = CAT_COMMAND_SETS.length + j;
      return `<option value="${value}">Commands Set ${value}</option>`;
    });
    commandSetSelect.innerHTML = named.concat(unnamed).join("");
  }
  if (baudSelect && !baudSelect.options.length) {
    baudSelect.innerHTML = CAT_BAUD_RATES.map((label, i) => `<option value="${i}">${label}</option>`).join("");
  }
}

function renderCatSettingsForm(state) {
  const settings = state && state.amp_settings;
  const interfaceSelect = document.getElementById("cat-interface-type");
  const commandSetSelect = document.getElementById("cat-command-set");
  const baudSelect = document.getElementById("cat-baud-rate");
  const byteSpacingInput = document.getElementById("cat-byte-spacing");
  const pollingTimeInput = document.getElementById("cat-polling-time");
  if (!settings) return; // leave selects at their default (first) option until a real reading arrives
  if (interfaceSelect) interfaceSelect.value = String(settings.cat_interface_type);
  if (commandSetSelect) commandSetSelect.value = String(settings.cat_command_set);
  if (baudSelect) baudSelect.value = String(settings.cat_baud_rate);
  if (byteSpacingInput) byteSpacingInput.value = settings.cat_byte_spacing_us;
  if (pollingTimeInput) pollingTimeInput.value = settings.cat_polling_time_ms;
}

async function saveCatSettings() {
  const statusEl = document.getElementById("cat-settings-status");
  const body = {
    interface_type: Number(document.getElementById("cat-interface-type").value),
    command_set: Number(document.getElementById("cat-command-set").value),
    baud_rate: Number(document.getElementById("cat-baud-rate").value),
    byte_spacing_us: Number(document.getElementById("cat-byte-spacing").value) || 0,
    polling_time_ms: Number(document.getElementById("cat-polling-time").value) || 0,
  };
  await apiFetch("/api/cat-settings", postJson(body));
  if (statusEl) {
    // Deliberately doesn't re-render the form from appState.lastStatus
    // here - the amp's fresh 0x12 echo (enqueued by the backend, not
    // awaited) hasn't necessarily arrived yet, so doing that would just
    // redisplay the stale pre-save values right under a "Saved."
    // message. Click "Refresh from Amp" for the confirmed values, same
    // as Antenna Setup's own save flow.
    statusEl.hidden = false;
    statusEl.textContent = "Saved. Click \"Refresh from Amp\" to confirm the amp's new reading.";
    setTimeout(() => { statusEl.hidden = true; }, 6000);
  }
}

function renderAtuStatusPanel(state) {
  const el = document.getElementById("atu-status-panel");
  if (!el) return;
  const info = state && state.atu_service_info;
  if (!info) {
    el.innerHTML = "<p>No ATU status received yet.</p>";
    return;
  }
  el.innerHTML = `
    <p>Working hours: ${info.working_hours}</p>
    <p>Last tune: CT ${info.tune_ct_pct}% / L ${info.tune_l_pct}% / CA ${info.tune_ca_pct}%</p>
    <p>Serial: ${info.serial_number}</p>
  `;
}

function populateSegmentDeleteBandOptions() {
  const select = document.getElementById("segment-delete-band");
  if (!select) return;
  select.innerHTML = BAND_NAMES.map((b, i) => `<option value="${i + 1}">${b}</option>`).join("");
}

function populateConfigForm(cfg) {
  const ampSelect = document.getElementById("setup-amp-select");
  const tunerSelect = document.getElementById("setup-tuner-select");
  if (ampSelect) {
    ampSelect.innerHTML = cfg.available_amp_models.map((m) => `<option value="${m}">${m}</option>`).join("");
    ampSelect.value = cfg.amp_model;
  }
  if (tunerSelect) {
    tunerSelect.innerHTML =
      `<option value="">None</option>` + cfg.available_tuner_models.map((m) => `<option value="${m}">${m}</option>`).join("");
    tunerSelect.value = cfg.tuner_model || "";
  }
  const debugCheckbox = document.getElementById("setup-debug-logging");
  if (debugCheckbox) debugCheckbox.checked = !!cfg.debug_logging_enabled;
  const logMarkerNote = document.getElementById("setup-log-marker-note");
  if (logMarkerNote) {
    logMarkerNote.hidden = !cfg.log_marker;
    logMarkerNote.textContent = cfg.log_marker ? `Log view cleared at ${cfg.log_marker} - downloads only include entries since then.` : "";
  }
  renderSetupDetectionNotes(appState.lastStatus);
}

// Amp model is auto-detected from the amp's own 0x11 reply when possible
// (see app.py's _maybe_apply_detected_amp_model, which already applies the
// detected model to config/AmpState - this note just surfaces that it
// happened, or that detection hasn't landed yet so the dropdown above is
// still manual). Tuner model can't be auto-detected beyond presence (ACOM's
// own 06AT/20AT manual: both report as "04AT", same control board) - this
// only cross-checks presence against whatever's manually selected.
function renderSetupDetectionNotes(state) {
  const ampNote = document.getElementById("setup-amp-detect-note");
  if (ampNote) {
    const detected = state && state.detected_amp_model;
    if (detected) {
      ampNote.hidden = false;
      ampNote.textContent = `Auto-detected from amp: ${detected}`;
    } else {
      ampNote.hidden = false;
      ampNote.textContent = "Not yet detected from the amp - using manual selection.";
    }
  }

  const tunerWarning = document.getElementById("setup-tuner-mismatch-warning");
  const tunerSelect = document.getElementById("setup-tuner-select");
  if (tunerWarning && tunerSelect) {
    const atuPresent = state && state.atu_identity ? state.atu_identity.present : null;
    if (atuPresent === true && !tunerSelect.value) {
      tunerWarning.hidden = false;
      tunerWarning.textContent = "ATU/ASEL detected on the amp, but no tuner model is selected above - pick one for display purposes.";
    } else if (atuPresent === false && tunerSelect.value) {
      tunerWarning.hidden = false;
      tunerWarning.textContent = "No ATU/ASEL detected on the amp right now - check it's connected and assigned, or this label may be wrong.";
    } else {
      tunerWarning.hidden = true;
    }
  }
}

function populateSerialPortOptions(portsResp, currentDevice) {
  const select = document.getElementById("setup-serial-select");
  if (!select) return;
  const ports = portsResp.ports || [];
  const known = ports.some((p) => p.device === currentDevice);
  const options = known || !currentDevice ? ports : [...ports, { device: currentDevice, description: "currently configured - not detected" }];
  select.innerHTML = options.length
    ? options.map((p) => `<option value="${p.device}">${p.device}${p.description ? " — " + p.description : ""}</option>`).join("")
    : `<option value="" disabled>No serial ports detected</option>`;
  if (currentDevice) select.value = currentDevice;
  select.dataset.originalValue = currentDevice || "";
}

function populateAntennaMatrix(antennas) {
  const tbody = document.getElementById("antenna-matrix-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  antennas.forEach((ant) => {
    const row = document.createElement("tr");
    row.dataset.antennaNumber = ant.number;

    const numCell = document.createElement("td");
    numCell.textContent = ant.number;
    row.appendChild(numCell);

    const nameCell = document.createElement("td");
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.maxLength = 12;
    nameInput.value = ant.name || "";
    nameInput.dataset.field = "name";
    nameCell.appendChild(nameInput);
    row.appendChild(nameCell);

    const typeCell = document.createElement("td");
    const typeSelect = document.createElement("select");
    typeSelect.dataset.field = "type";
    typeSelect.innerHTML = ANTENNA_TYPES.map((t) => `<option value="${t}">${t}</option>`).join("");
    typeSelect.value = ant.type || "Sharp";
    typeCell.appendChild(typeSelect);
    row.appendChild(typeCell);

    BAND_NAMES.forEach((band) => {
      const cell = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.field = "band";
      checkbox.dataset.band = band;
      checkbox.checked = (ant.bands || []).includes(band);
      cell.appendChild(checkbox);
      row.appendChild(cell);
    });

    tbody.appendChild(row);
  });
}

function updateAntennaSetupVisibility() {
  const tunerSelect = document.getElementById("setup-tuner-select");
  const section = document.getElementById("antenna-setup-section");
  if (!tunerSelect || !section) return;
  const atuPresent = appState.lastStatus?.atu_identity?.present;
  section.hidden = !tunerSelect.value && !atuPresent;
  renderSetupDetectionNotes(appState.lastStatus);
}

function readAntennaMatrix() {
  const rows = document.querySelectorAll("#antenna-matrix-body tr");
  return Array.from(rows).map((row) => {
    const number = parseInt(row.dataset.antennaNumber, 10);
    const name = row.querySelector('[data-field="name"]').value;
    const type = row.querySelector('[data-field="type"]').value;
    const bands = Array.from(row.querySelectorAll('[data-field="band"]'))
      .filter((cb) => cb.checked)
      .map((cb) => cb.dataset.band);
    return { number, name, type, bands };
  });
}

async function saveSetup() {
  const ampModel = document.getElementById("setup-amp-select").value;
  const tunerModel = document.getElementById("setup-tuner-select").value || null;
  const debugLogging = document.getElementById("setup-debug-logging")?.checked;
  const serialSelect = document.getElementById("setup-serial-select");
  const serialDevice = serialSelect ? serialSelect.value : undefined;
  const serialChanged = !!serialSelect && serialDevice !== (serialSelect.dataset.originalValue || "");

  if (serialChanged) {
    if (!window.confirm("Changing the serial port may toggle the amplifier's power state. Continue?")) {
      return;
    }
  }

  await apiFetch(
    "/api/config",
    postJson({
      amp_model: ampModel,
      tuner_model: tunerModel,
      logging_level: debugLogging ? "DEBUG" : "INFO",
      ...(serialChanged ? { serial_device: serialDevice, confirm: true } : {}),
    })
  );

  if (tunerModel) {
    const antennas = readAntennaMatrix();
    await apiFetch("/api/antenna/config", postJson({ antennas }));
  }

  await loadSetup();
}

// =============================================================================
// Antenna Setup "Danger Zone": ATU memory erase + delete-segment-tuning.
// Rarely used and destructive - gated behind a collapsed <details>, the
// backend's own confirm:true requirement, AND a native confirm() dialog
// here, so nothing in Antenna Setup is a single misclick away from this.
// =============================================================================

function wireAtuDangerZone() {
  const eraseSelect = document.getElementById("atu-erase-selection");
  const antennaLabel = document.getElementById("atu-erase-antenna-label");
  eraseSelect?.addEventListener("change", () => {
    if (antennaLabel) antennaLabel.hidden = eraseSelect.value !== "specific";
  });

  document.getElementById("btn-atu-erase")?.addEventListener("click", withErrorBanner(async () => {
    const selection = eraseSelect.value;
    const antennaNumber = parseInt(document.getElementById("atu-erase-antenna-number").value, 10);
    const descriptions = {
      current: "the current tuning",
      specific: `antenna ${antennaNumber}'s tunings`,
      all: "ALL tunings",
      whole_memory: "the ENTIRE ATU memory",
    };
    const description = descriptions[selection] || selection;
    if (!window.confirm(`Erase ${description}? This cannot be undone.`)) return;
    await apiFetch("/api/atu/erase-memory", postJson({
      selection,
      antenna_number: selection === "specific" ? antennaNumber : undefined,
      confirm: true,
    }));
    showActionError(`Erased ${description}.`, "info");
  }));

  document.getElementById("btn-segment-delete")?.addEventListener("click", withErrorBanner(async () => {
    const antennaNumber = parseInt(document.getElementById("segment-delete-antenna").value, 10);
    const bandSelect = document.getElementById("segment-delete-band");
    const bandNumber = parseInt(bandSelect.value, 10);
    const bandName = bandSelect.options[bandSelect.selectedIndex]?.textContent || bandNumber;
    if (!window.confirm(`Delete the stored tuning for antenna ${antennaNumber}, band ${bandName}? This cannot be undone.`)) return;
    await apiFetch("/api/atu/delete-segment-tuning", postJson({
      antenna_number: antennaNumber,
      band_number: bandNumber,
      confirm: true,
    }));
    showActionError(`Deleted tuning for antenna ${antennaNumber}, band ${bandName}.`, "info");
  }));
}

// =============================================================================
// Self-update: a header button that opens a full screen (same pattern as
// Setup's gear icon) showing current vs. new version, "Update Now" if
// something's newer, a link to the full changelog, and - if a check
// finds a new release - the actual changelog entries between the
// running version and the new one, pulled straight from CHANGELOG.md
// at the new release's tag (not the working tree, which hasn't been
// updated yet - see app.py's _read_changelog_at_ref()). On top of the
// on-click check, the backend's own daily background check
// (journalctl-visible) drives the icon's appearance between clicks: a
// plain icon normally, a highlighted + gently bouncing one once that
// check has found something, so it can get your attention without
// requiring a click first. Visible to anyone with the dashboard open,
// same as every other control here - there's no auth in this app
// (per-user login is a known future item, not built yet).
// =============================================================================

const UPDATE_STATUS_POLL_MS = 10 * 60 * 1000; // just re-reads the backend's cached daily-check result
let updateStatus = null;

function renderUpdateButtonHighlight() {
  const btn = document.getElementById("btn-update-available");
  if (btn) btn.classList.toggle("has-update", !!updateStatus?.available);
}

async function refreshUpdateStatus() {
  try {
    const resp = await fetch("/api/update/status");
    if (!resp.ok) return;
    updateStatus = await resp.json();
    renderUpdateButtonHighlight();
  } catch {
    // Silent - this is a passive background poll, not a user action.
  }
}

function openUpdateScreen() {
  appState.setupOpen = false;
  appState.updateScreenOpen = true;
  updateScreenVisibility();
  checkForUpdateNow();
}

function closeUpdateScreen() {
  appState.updateScreenOpen = false;
  updateScreenVisibility();
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/** Minimal, tailored to this project's own CHANGELOG.md shape (not a
 * general markdown renderer): "### Heading" lines and "- bullet" lines,
 * everything else as plain text. Good enough since we're the only
 * author of the content being rendered. */
function renderChangelogMarkdown(markdown) {
  let html = "";
  let inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const rawLine of markdown.split("\n")) {
    const line = rawLine.trim();
    if (!line) { closeList(); continue; }
    if (line.startsWith("### ")) {
      closeList();
      html += `<h4>${escapeHtml(line.slice(4))}</h4>`;
    } else if (line.startsWith("- ")) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${escapeHtml(line.slice(2))}</li>`;
    } else {
      closeList();
      html += `<p>${escapeHtml(line)}</p>`;
    }
  }
  closeList();
  return html;
}

function renderUpdateChanges() {
  const section = document.getElementById("update-changes-section");
  const body = document.getElementById("update-changes-body");
  if (!section || !body) return;
  const changes = updateStatus?.changes || [];
  section.hidden = changes.length === 0;
  if (changes.length === 0) return;
  body.innerHTML = changes.map((entry) => {
    // Beta-channel entries carry a ready-made title (commit count) instead
    // of a changelog version/date - see app.py's _read_commit_subjects().
    const heading = entry.title || `v${entry.version}${entry.date ? ` - ${entry.date}` : ""}`;
    return `<h3 class="changelog-version">${escapeHtml(heading)}</h3>${renderChangelogMarkdown(entry.body)}`;
  }).join("");
}

async function checkForUpdateNow() {
  await runUpdateCheck(() => apiFetch("/api/update/check", { method: "POST" }));
}

/** Beta toggle: saves the choice server-side and returns a fresh check
 * against the newly selected channel, so this reuses the same rendering. */
async function setBetaUpdates(event) {
  const beta = event.target.checked;
  await runUpdateCheck(() => apiFetch("/api/update/beta", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ beta }),
  }));
}

async function runUpdateCheck(request) {
  const headline = document.getElementById("update-headline");
  const betaToggle = document.getElementById("update-beta-toggle");
  const currentEl = document.getElementById("update-current-version");
  const latestEl = document.getElementById("update-latest-version");
  const updateNowButton = document.getElementById("btn-update-now");
  const statusEl = document.getElementById("update-status");
  if (headline) headline.textContent = "Checking for updates...";
  if (currentEl) currentEl.textContent = "";
  if (latestEl) latestEl.textContent = "";
  if (statusEl) statusEl.hidden = true;
  // Hidden until a check actually confirms there's something to install -
  // nothing to click/no reason to restart the service otherwise.
  if (updateNowButton) { updateNowButton.hidden = true; updateNowButton.disabled = false; }
  document.getElementById("update-changes-section")?.setAttribute("hidden", "");
  if (betaToggle) betaToggle.disabled = true;
  const checkButton = document.getElementById("btn-update-check");
  if (checkButton) checkButton.disabled = true;

  try {
    const resp = await request();
    updateStatus = await resp.json();
    if (betaToggle) betaToggle.checked = !!updateStatus.beta;
    if (currentEl) currentEl.textContent = updateStatus.current_version || "unknown";
    if (latestEl) latestEl.textContent = updateStatus.latest_version || "no tagged release yet";
    if (headline) {
      headline.textContent = updateStatus.available
        ? "Update available"
        : updateStatus.error ? `Check failed: ${updateStatus.error}` : "Already up to date";
    }
    if (updateNowButton) updateNowButton.hidden = !updateStatus.available;
    renderUpdateChanges();
    renderUpdateButtonHighlight();
  } catch (err) {
    if (headline) headline.textContent = `Check failed: ${err.message}`;
  } finally {
    if (betaToggle) betaToggle.disabled = false;
    if (checkButton) checkButton.disabled = false;
  }
}

async function applyUpdateNow() {
  if (!window.confirm(
    "Update now? The backend will restart - amp control will be briefly " +
    "unavailable (usually just a few seconds)."
  )) return;
  const statusEl = document.getElementById("update-status");
  const button = document.getElementById("btn-update-now");
  // Raw commit hash, not updateStatus.current_version (that's the
  // tag-based display string) - waitForUpdateRestart() just needs
  // something that changes once the new process comes up.
  let beforeCommit = null;
  try {
    beforeCommit = (await (await fetch("/api/config")).json()).git_commit;
  } catch {
    // If this fails, waitForUpdateRestart() below will just run out its
    // timeout without detecting a restart - acceptable degradation.
  }
  if (button) button.disabled = true;
  if (statusEl) {
    statusEl.hidden = false;
    statusEl.textContent = "Updating and restarting...";
  }
  try {
    await apiFetch("/api/update/apply", { method: "POST" });
  } catch (err) {
    if (statusEl) statusEl.textContent = `Failed to start update: ${err.message}`;
    if (button) button.disabled = false;
    return;
  }
  await waitForUpdateRestart(beforeCommit, statusEl, button);
}

/** Polls /api/config (cheap, already-existing endpoint) for its
 * git_commit field to change - that's the real confirmation the restart
 * landed, not just a connectivity blip that could also be caused by
 * something unrelated restarting the service. If it never changes
 * within the timeout, hedge rather than claim a false "failed" or
 * "succeeded" - could mean already up to date, or a genuine failure. */
async function waitForUpdateRestart(beforeCommit, statusEl, button) {
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    try {
      const resp = await fetch("/api/config");
      if (resp.ok) {
        const cfg = await resp.json();
        if (cfg.git_commit && cfg.git_commit !== beforeCommit) {
          if (statusEl) statusEl.textContent = `Updated to ${cfg.git_commit} - reloading...`;
          setTimeout(() => window.location.reload(), 1000);
          return;
        }
      }
    } catch {
      // Expected while the service is restarting - keep polling.
    }
  }
  if (statusEl) {
    statusEl.textContent =
      "No restart detected yet - may already be up to date, or the update " +
      "may have failed. Check the logs if you expected a change.";
  }
  if (button) button.disabled = false;
}

// =============================================================================
// Boot
// =============================================================================

async function boot() {
  initThemeSwitcher();
  wireControls();
  try {
    const resp = await apiFetch("/api/status");
    render(await resp.json());
  } catch {
    // WebSocket connect below will retry; initial render just stays empty.
  }
  connectWebSocket();
  refreshUpdateStatus();
  setInterval(refreshUpdateStatus, UPDATE_STATUS_POLL_MS);
}

boot();
