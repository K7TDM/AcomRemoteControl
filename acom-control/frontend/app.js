// WebSocket client, REST calls, UI update logic. See PI_CONTROL_PLAN.md
// step 6 "Frontend dashboard" and "Backend vs. frontend logging".

import { initThemeSwitcher, loadTheme } from "./theme.js";
import { mountGauge, updateGauge, resolveWidget } from "./gauges.js";

const BAND_NAMES = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"];

// Full-band edges [kHz] for the band panel's scale, indexed like
// BAND_NAMES. 0x27 only carries the current *segment's* edges, so the
// whole-band span has to come from a table. US amateur allocations per
// the ARRL "US Amateur Radio Bands" chart (rev. 1/16/2026), from Tyler.
// The amp's own segments can run past these (e.g. 18049-18200 on 17m) -
// renderFreqReadout() clips the highlight to the band.
const BAND_EDGES_KHZ = [
  [1800, 2000], [3500, 4000], [5330.5, 5405], [7000, 7300], [10100, 10150],
  [14000, 14350], [18068, 18168], [21000, 21450], [24890, 24990],
  [28000, 29700], [50000, 54000],
];

// Optional CB band (channel 1 26.965 MHz to channel 40 27.405 MHz), off
// by default - a per-browser display preference (Setup screen checkbox),
// stored like the theme. When on, a segment or carrier inside this range
// shows as "CB" with these edges instead of whatever amateur band the
// amp files it under.
const CB_EDGES_KHZ = [26965, 27405];
const CB_STORAGE_KEY = "acom.showCbBand";

function loadShowCbBand() {
  try {
    return localStorage.getItem(CB_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function saveShowCbBand(on) {
  try {
    localStorage.setItem(CB_STORAGE_KEY, on ? "1" : "0");
  } catch {
    // Display preference only - fine to lose it.
  }
}
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
const ATU_LABEL_NAME_MATCHING = "Matching Cycle";

// Forward power is rendered separately as a full-width "hero" bar (see
// #power-out-hero in index.html) rather than inside the regular grid.
const HERO_GAUGE_DEF = { id: "forward_power_w", label: "Forward Power", unit: "W", min: 0, max: () => appState.maxPowerW || 1500 };

// Ordered to match the real Basic Screen's own layout (see Figure 3-2 in
// the 1200S manual). The real screen shows Input Power + PA DC Current as
// bare digits with no bargraph, but per Tyler all four get the same
// metered bargraph (amber header bar in the ACOM theme) here - the `plain`
// flag is still supported by gauges.js if a bare readout is wanted again.
// swr_calc has no real-screen counterpart (it's computed, not read from the
// amp - see computeSwr()) so it gets its own full-width row in the grid.
const GAUGE_DEFS = [
  { id: "temp_pam1_c", label: "PA Temperature", unit: "°C", min: 0, max: () => 100 },
  { id: "reflected_power_w", label: "Reflected Power", unit: "W", min: 0, max: () => Math.max(50, (appState.maxPowerW || 1500) * 0.2) },
  { id: "input_power_w", label: "Input Power", unit: "W", min: 0, max: () => 100 },
  // Shown in A, not raw mA, to match the manual's own spec and the amp's own
  // front panel ("PA DC CURRENT must be between 32 and 36 A" at 1000W) - see
  // id1_a derived in renderGauges(). A max of 40A gives headroom above the
  // documented 32-36A full-power range.
  { id: "id1_a", label: "PA DC Current", unit: "A", min: 0, max: () => 40 },
  // SWR meter convention: scaled by reflection coefficient out to infinity
  // (gauges.js SCALES.swr), amber from 2:1, red from 3:1.
  { id: "swr_calc", label: "SWR", unit: ":1", min: 1, max: () => Infinity, scale: "swr", warnFrom: 2, dangerFrom: 3 },
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
  lastTxEndMs: 0,
  // Display layout (Setup -> Display & Sound), replaced by the Pi's saved layout
  // from /api/config at boot - see applyDisplayLayout().
  display: null,
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
    reloadIfBackendUpdated();
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
  // Keying up counts as activity, so the screen never dims mid-over.
  if (appState.inTx) noteActivity();
  checkAlarmSound(state);
  checkPowerSounds(state);
  updatePoweringDownState(state);

  renderStatusRow(state);
  renderGauges(state);
  renderFreqReadout(state);
  renderWarningBanner(state);
  renderOprToggle(state);
  renderAtuBypassToggle(state);
  renderPowerOnButton(state);
  renderPowerOffButton(state);
  updateScreenVisibility();
  if (appState.setupOpen) updateAntennaSetupVisibility();
}

// Four mutually-exclusive top-level screens: the normal dashboard, Setup
// (opened via the header's gear icon), a "Powering Down" screen shown while
// a shutdown is in progress (see isPoweringDown()), and a reduced power-off
// screen (just the logo + Power On) shown whenever the amp is confirmed
// off. Setup takes priority over both power states so Settings stays
// reachable either way - per Tyler: close all the normal dashboard windows
// while off/powering down, but keep Setup accessible from the header
// regardless of power state.
function currentScreen() {
  if (appState.setupOpen) return "setup";
  if (appState.updateScreenOpen) return "update";
  const power = appState.lastStatus?.power_state;
  if (power === "off") return "power-off";
  if (isPoweringDown(appState.lastStatus)) return "powering-down";
  return "dashboard";
}

function updateScreenVisibility() {
  const screen = currentScreen();
  document.getElementById("dashboard").hidden = screen !== "dashboard";
  document.getElementById("setup-screen").hidden = screen !== "setup";
  document.getElementById("update-screen").hidden = screen !== "update";
  document.getElementById("powering-down-screen").hidden = screen !== "powering-down";
  document.getElementById("power-off-screen").hidden = screen !== "power-off";
}

// True from the moment a shutdown starts until power_state is confirmed
// "off": either we just clicked our own Power Off button (poweringDownRequested,
// set synchronously on click so the screen appears right away rather than
// waiting out the ~1.5s DTR/RTS pulse), the amp's own telemetry shows it
// winding down (same mode names checkPowerSounds() plays the "off" Morse
// for), or - the front-panel case, where neither of those fires - comms
// have gone ambiguous (power_state "unknown") after previously being on.
// "unknown" always means "not yet sure if this is off, hung, or just
// restarting" (see amp_state.py), so this screen can flash briefly on a
// backend restart too - acceptable, since it resolves to "on" within a
// telemetry frame or two, or to "off" per the bounded timeout either way.
function isPoweringDown(state) {
  if (!state) return false;
  if (state.power_state === "unknown") return true;
  if (state.power_state !== "on") return false;
  if (poweringDownRequested) return true;
  const modeName = state.telemetry?.mode_name;
  return modeName === "Turn OFF Procedure" || modeName === "Extra Cooling State";
}

function updatePoweringDownState(state) {
  if (state.power_state === "off") poweringDownRequested = false;
}

// Single shared "did a transmission just start" computation, used by both
// the peak-hold reset (updatePeak()) and the TX average (updateTxAverage())
// above - both reset on the same rising edge of mode_name becoming
// "OPR/TX Mode", so this runs once per telemetry update rather than each
// of them independently comparing against the previous mode.
//
// "Transmission" spans short drops out of TX: real-hardware logs showed the
// amp going OPR/TX -> OPR/RX -> OPR/TX every couple of seconds within a
// single over, which used to reset the peak and average on each burst so
// "last transmission" was really "last burst". A new transmission now only
// starts when TX resumes more than TX_HANG_MS after it last ended (1.5s,
// per Tyler).
const TX_HANG_MS = 1500;

function updateTxEdgeState(state) {
  const modeName = state.telemetry ? state.telemetry.mode_name : null;
  const now = Date.now();
  const wasInTx = appState.prevModeName === MODE_NAME_OPR_TX;
  appState.inTx = modeName === MODE_NAME_OPR_TX;
  if (wasInTx && !appState.inTx) appState.lastTxEndMs = now;
  const enteringTx = appState.inTx && !wasInTx;
  appState.justEnteredTx = enteringTx && !(appState.lastTxEndMs && now - appState.lastTxEndMs <= TX_HANG_MS);
  appState.prevModeName = modeName;
}

// Colors the merged OPR/STB button from the amp's own reported mode rather
// than click-tracked UI state - see data-opr-state in components.css for
// the color mapping, and the button's click handler in wireControls() for
// the toggle direction this same mode name drives. Per Tyler: the label
// names what a click will do - "Standby" (green) while in Operate (OPR/RX
// or OPR/TX, no distinction), "Operate" (yellow) while in Standby.
function renderOprToggle(state) {
  const btn = document.getElementById("btn-opr-toggle");
  if (!btn) return;
  const modeName = state.telemetry ? state.telemetry.mode_name : null;
  let oprState = "unknown";
  let text = "OPR/STB";
  if (modeName === MODE_NAME_STB) {
    oprState = "stb";
    text = "Operate";
  } else if (modeName === MODE_NAME_OPR_RX || modeName === MODE_NAME_OPR_TX) {
    oprState = "operate";
    text = "Standby";
  }
  btn.dataset.oprState = oprState;
  if (btn.textContent !== text) btn.textContent = text;
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

// Setup's Power Off button: only does anything while the amp is confirmed
// on (/api/power refuses or no-ops otherwise), so it's disabled - with a
// note saying why - the rest of the time.
function renderPowerOffButton(state) {
  const btn = document.getElementById("btn-power-off");
  if (!btn) return;
  const on = state.power_state === "on";
  btn.disabled = !on;
  const note = document.getElementById("setup-power-note");
  if (note) note.hidden = on;
}

// Colors the ATU Bypass button from the amp's own last-reported atu_label
// (0x27) rather than click-tracked UI state, same pattern as
// renderOprToggle() above - green while the ATU is actively engaged
// (normal tuned operation), yellow while bypassed (see data-bypass-state
// in components.css for the color mapping, and the button's click handler
// in wireControls() for the direction this same label drives). Per Tyler:
// bypass isn't the "good"/default state, so it shouldn't read as green.
//
// The label also names what a click will do: "ATU Enable" while bypassed.
// While a tune is waiting for RF ("Matching Cycle" - the amp re-sends that
// 0x27 about once a second until it gets a carrier), it reads "Tuning..."
// instead: a Bypass click is ignored by the amp in that state, which is
// what made the button look stuck on green.
function renderAtuBypassToggle(state) {
  const btn = document.getElementById("btn-atu-bypass");
  if (!btn) return;
  const labelName = state.antenna_band_status ? state.antenna_band_status.atu_label_name : null;
  let bypassState = "active";
  let text = "ATU Bypass";
  if (labelName === ATU_LABEL_NAME_BYPASS) {
    bypassState = "bypass";
    text = "ATU Enable";
  } else if (labelName === ATU_LABEL_NAME_MATCHING) {
    bypassState = "tuning";
    text = "Tuning…";
  }
  btn.dataset.bypassState = bypassState;
  if (btn.textContent !== text) btn.textContent = text;
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
  const grid = document.getElementById("panel-grid");
  if (!grid) return;
  ensureGaugePanels(grid);
  const theme = appState.theme.style;
  const t = { ...(state.telemetry || {}) };
  t.swr_calc = computeSwr(t.forward_power_w, t.reflected_power_w);
  t.id1_a = t.id1_ma != null ? t.id1_ma / 1000 : null;

  // The hero is a bargraph in the ACOM/Modern themes; Classic uses its
  // window meter here too (the big backlit meter is the centerpiece of that
  // look). Hidden panels (Display settings) aren't rendered at all.
  const heroWidget = theme === "classic" ? null : "bargraph";
  if (heroEl && !heroEl.hidden) mountOrUpdateGauge(heroEl, HERO_GAUGE_DEF, t, theme, heroWidget);

  GAUGE_DEFS.forEach((def) => {
    const el = grid.querySelector(`[data-gauge-id="${def.id}"]`);
    if (el && !el.hidden) mountOrUpdateGauge(el, def, t, theme, null);
  });
}

// Creates any missing gauge panels inside #panel-grid, then re-applies the
// Display layout so they land in their configured position/width.
function ensureGaugePanels(grid) {
  let created = false;
  GAUGE_DEFS.forEach((def) => {
    if (grid.querySelector(`[data-gauge-id="${def.id}"]`)) return;
    const el = document.createElement("div");
    el.className = "gauge-wrapper";
    el.dataset.gaugeId = def.id;
    el.dataset.panelId = def.id;
    grid.appendChild(el);
    created = true;
  });
  if (created) applyDisplayLayout(appState.display);
}

// =============================================================================
// Display layout (Setup screen's "Display" section) - order, width and
// visibility of every #panel-grid panel. One layout for every device, saved
// on the Pi (config.json "display", POST /api/display) per Tyler.
// =============================================================================

const PANEL_LABELS = {
  forward_power_w: "Forward Power",
  temp_pam1_c: "PA Temperature",
  reflected_power_w: "Reflected Power",
  input_power_w: "Input Power",
  id1_a: "PA DC Current",
  swr_calc: "SWR",
  band_panel: "Band / Frequency",
};
// Mirrors config.py's DISPLAY_FULL_WIDTH_ONLY / DISPLAY_PANEL_DEFAULTS.
const PANEL_FULL_WIDTH_ONLY = new Set(["band_panel"]);
const DEFAULT_DISPLAY_PANELS = [
  { id: "forward_power_w", visible: true, width: "full" },
  { id: "temp_pam1_c", visible: true, width: "half" },
  { id: "reflected_power_w", visible: true, width: "half" },
  { id: "input_power_w", visible: true, width: "half" },
  { id: "id1_a", visible: true, width: "half" },
  { id: "swr_calc", visible: true, width: "full" },
  { id: "band_panel", visible: true, width: "full" },
];
// "double" spans both halves of a wide screen (see base.css fit-wide);
// on other screens it's the same as Full.
const PANEL_WIDTH_OPTIONS = [["eighth", "⅛"], ["quarter", "¼"], ["half", "½"], ["full", "Full"], ["double", "2× Full"]];
const WIDE_METER_WIDTHS = new Set(["full", "double"]); // Classic's wide edgewise meter / Modern's wide crescent
const DISPLAY_SAVE_DEBOUNCE_MS = 400;
let displaySaveTimer = null;

function applyDisplayLayout(panels) {
  const grid = document.getElementById("panel-grid");
  if (!grid || !panels) return;
  let unhid = false;
  panels.forEach((p) => {
    const el = grid.querySelector(`[data-panel-id="${p.id}"]`);
    if (!el) return;
    grid.appendChild(el); // appending in list order is the reorder
    el.dataset.width = PANEL_FULL_WIDTH_ONLY.has(p.id) ? "full" : p.width;
    if (el.hidden && p.visible) unhid = true;
    el.hidden = !p.visible;
  });
  // A panel that was hidden skipped rendering - draw it now rather than
  // waiting for the next telemetry tick.
  if (unhid && appState.lastStatus) renderGauges(appState.lastStatus);
  updatePanelRows();
}

// =============================================================================
// Full-screen fit layout (landscape screens: monitors, the Pi touchscreen,
// phones on their side) - see the "fit" section of base.css. The CSS does
// the structure; this sets the two html classes from the same media queries
// and sizes the grid rows.
// =============================================================================

const FIT_QUERY = "(orientation: landscape) and (min-width: 640px) and (min-height: 300px)";
const FIT_WIDE_QUERY = `${FIT_QUERY} and (min-aspect-ratio: 3/2)`;
// Grid columns per width (base.css): 8 per layout-width, 16 on wide screens.
const PANEL_SPANS = { eighth: 1, quarter: 2, half: 4, full: 8, double: 16 };

function updateFitMode() {
  const root = document.documentElement;
  const fit = window.matchMedia(FIT_QUERY).matches;
  root.classList.toggle("fit-layout", fit);
  root.classList.toggle("fit-wide", fit && window.matchMedia(FIT_WIDE_QUERY).matches);
  // Status lights live in the header in fit mode (saves a full row of
  // height), back at the top of the dashboard otherwise.
  const statusRow = document.getElementById("status-row");
  const header = document.querySelector(".app-header");
  const dashboard = document.getElementById("dashboard");
  if (statusRow && header && dashboard) {
    if (fit && statusRow.parentElement !== header) header.insertBefore(statusRow, header.querySelector(".header-controls"));
    if (!fit && statusRow.parentElement !== dashboard) dashboard.prepend(statusRow);
  }
  updatePanelRows();
}

// Roughly how much height a panel needs for a given width, so rows of big
// meters get more of the screen than the short band dial. Classic's meters
// keep a fixed aspect (standard ~2:1, wide ~5.5:1), so their need scales
// with the width they get; bargraphs are about the same height at any width.
function panelWeight(el, span, cols) {
  if (el.dataset.panelId === "band_panel") return 1;
  if (appState.theme.style === "classic") {
    const aspect = WIDE_METER_WIDTHS.has(el.dataset.width) ? 5.55 : 2.05;
    return (span / cols / aspect) * 10 + 0.6;
  }
  // Modern's arc dials work the same way (gauges.js ARC_GEOMETRY: semicircle
  // ~1.64:1, wide crescent ~5.2:1); its Forward Power stays a bar.
  if (appState.theme.style === "modern" && el.dataset.panelId !== "forward_power_w") {
    const aspect = WIDE_METER_WIDTHS.has(el.dataset.width) ? 5.17 : 1.64;
    return (span / cols / aspect) * 10 + 0.6;
  }
  // Bar panels scale their contents with their height (bargraph.css), so
  // equal rows keep the text and bars the same size across rows.
  return 1.15;
}

// Works out which panels share each grid row (CSS grid's own sparse
// auto-placement: a panel that doesn't fit the rest of a row starts the
// next one) and gives each row the height share of its tallest panel.
function updatePanelRows() {
  const grid = document.getElementById("panel-grid");
  if (!grid) return;
  const root = document.documentElement;
  if (!root.classList.contains("fit-layout")) {
    grid.style.removeProperty("--panel-rows");
    return;
  }
  const cols = root.classList.contains("fit-wide") ? 16 : 8;
  const rows = []; // { weight, onlyBand, panels }
  let col = 0;
  [...grid.children].forEach((el) => {
    delete el.dataset.rowAlone;
    if (!el.dataset.panelId || el.hidden) return;
    const span = Math.min(PANEL_SPANS[el.dataset.width] || 8, cols);
    if (col === 0 || col + span > cols) {
      rows.push({ weight: 0, onlyBand: true, panels: [] });
      col = 0;
    }
    const row = rows[rows.length - 1];
    row.weight = Math.max(row.weight, panelWeight(el, span, cols));
    row.panels.push(el);
    if (el.dataset.panelId !== "band_panel") row.onlyBand = false;
    col += span;
    if (col >= cols) col = 0;
  });
  // The band dial alone in a row spans the whole row (see base.css).
  rows.forEach((r) => {
    if (r.onlyBand && r.panels.length === 1) r.panels[0].dataset.rowAlone = "1";
  });
  // A row holding only the band dial gets exactly the height it needs
  // (it's fixed-size text and a scale); the meter rows share the rest.
  // Bar themes (ACOM/Modern) cap their rows instead of filling: bars look
  // right short and wide like the amp's own screen, and stretched to fill
  // a big monitor they were far too tall (per Tyler). minmax(0, cap) rows
  // still shrink to fit a small screen; leftover space on a big one is
  // centered (base.css align-content).
  const barTheme = appState.theme.style !== "classic";
  grid.style.setProperty(
    "--panel-rows",
    rows
      .map((r) => {
        if (r.onlyBand) return "auto";
        return barTheme ? `minmax(0, ${BAR_ROW_MAX})` : `minmax(0, ${Math.round(r.weight * 100) / 100}fr)`;
      })
      .join(" "),
  );
}

const BAR_ROW_MAX = "12.5rem";

window.matchMedia(FIT_QUERY).addEventListener("change", updateFitMode);
window.matchMedia(FIT_WIDE_QUERY).addEventListener("change", updateFitMode);

function setDisplayPanels(panels) {
  appState.display = panels;
  applyDisplayLayout(panels);
  renderDisplaySettings();
  if (displaySaveTimer) clearTimeout(displaySaveTimer);
  displaySaveTimer = setTimeout(saveDisplayPanels, DISPLAY_SAVE_DEBOUNCE_MS);
}

async function saveDisplayPanels() {
  displaySaveTimer = null;
  try {
    const resp = await apiFetch("/api/display", postJson({ panels: appState.display }));
    const saved = (await resp.json()).display;
    if (saved) {
      appState.display = saved;
      applyDisplayLayout(saved);
    }
  } catch (err) {
    showActionError(`Couldn't save the Display layout: ${err.message}`);
  }
}

function renderDisplaySettings() {
  const list = document.getElementById("display-panel-list");
  if (!list) return;
  list.innerHTML = "";
  appState.display.forEach((p, index) => {
    const li = document.createElement("li");
    li.className = "display-panel-row";
    li.dataset.panelId = p.id;

    const handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = "⠿";
    handle.tabIndex = 0;
    handle.title = "Drag to reorder (or use the arrow keys)";
    handle.setAttribute("aria-label", `Move ${PANEL_LABELS[p.id] || p.id}`);
    wireDragHandle(handle, li, list);
    handle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const to = index + (e.key === "ArrowUp" ? -1 : 1);
      if (to < 0 || to >= appState.display.length) return;
      const next = [...appState.display];
      [next[index], next[to]] = [next[to], next[index]];
      setDisplayPanels(next);
      list.querySelector(`[data-panel-id="${p.id}"] .drag-handle`)?.focus();
    });

    const name = document.createElement("span");
    name.className = "display-panel-name";
    name.textContent = PANEL_LABELS[p.id] || p.id;

    const showLabel = document.createElement("label");
    showLabel.className = "display-panel-show";
    const show = document.createElement("input");
    show.type = "checkbox";
    show.checked = p.visible;
    show.addEventListener("change", () => {
      setDisplayPanels(appState.display.map((q) => (q.id === p.id ? { ...q, visible: show.checked } : q)));
    });
    showLabel.append(show, " Show");

    const width = document.createElement("select");
    width.className = "theme-select display-panel-width";
    width.setAttribute("aria-label", `${PANEL_LABELS[p.id] || p.id} width`);
    width.innerHTML = PANEL_WIDTH_OPTIONS.map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
    width.value = PANEL_FULL_WIDTH_ONLY.has(p.id) ? "full" : p.width;
    width.disabled = PANEL_FULL_WIDTH_ONLY.has(p.id);
    if (width.disabled) width.title = "This panel is always full width";
    width.addEventListener("change", () => {
      setDisplayPanels(appState.display.map((q) => (q.id === p.id ? { ...q, width: width.value } : q)));
    });

    li.append(handle, name, showLabel, width);
    list.appendChild(li);
  });
}

// --- Raspberry Pi display (kiosk) switch --------------------------------------
// Turns the dashboard-on-the-Pi's-own-screen kiosk on/off (POST /api/kiosk,
// which runs the updater's kiosk-on/kiosk-off). The first time it's turned
// on the Pi downloads the browser, so this polls /api/config until the
// switch settles rather than waiting on the request.

const KIOSK_POLL_MS = 3000;
const KIOSK_POLL_TIMEOUT_MS = 15 * 60 * 1000;
let kioskPollTimer = null;

function renderKioskSetting(kiosk) {
  const toggle = document.getElementById("setup-kiosk-toggle");
  if (!toggle || !kiosk) return;
  const on = kiosk.busy && kiosk.wanted !== null ? kiosk.wanted : kiosk.enabled;
  toggle.checked = !!on;
  toggle.disabled = !!kiosk.busy;
  const displays = kiosk.displays || [];
  const screens = displays.length ? displays.map(friendlyDisplayName).join(" and ") : null;
  let status;
  if (kiosk.busy) {
    status = kiosk.wanted
      ? "Turning on… The first time, the Pi downloads the browser, which can take a few minutes."
      : "Turning off…";
  } else if (kiosk.failed) {
    status = "That didn't work - download the log (Setup -> Logs) to see why.";
  } else if (kiosk.enabled) {
    status = screens
      ? `On - showing on the ${screens}.`
      : "On - no display connected right now; it will show as soon as one is plugged in.";
  } else {
    status = screens ? `Off. Display connected: ${screens}.` : "Off. No display connected right now.";
  }
  setText("setup-kiosk-status", status);
}

// Setup -> Display & Sound's "Screen brightness" slider: the Pi touchscreen's
// backlight, saved on the Pi (POST /api/display/brightness). Applies as it
// moves, debounced like the gauge layout.
const BRIGHTNESS_SAVE_DEBOUNCE_MS = 250;
let brightnessSaveTimer = null;

function renderBrightnessSetting(cfg) {
  const group = document.getElementById("setup-brightness-group");
  if (group) group.hidden = !isKiosk();
  const slider = document.getElementById("setup-brightness");
  // Not while a save is pending - that would snap the slider back mid-drag.
  if (!slider || brightnessSaveTimer) return;
  if (cfg.brightness_min) slider.min = cfg.brightness_min;
  if (cfg.brightness) slider.value = cfg.brightness;
  setText("setup-brightness-value", `${slider.value}%`);
  slider.disabled = cfg.backlight_available === false;
  setText(
    "setup-brightness-status",
    cfg.backlight_available === false
      ? "The Pi's display has no adjustable backlight (an HDMI monitor, or no display) - brightness can only be set on the monitor itself. Dimming after 10 minutes idle still applies."
      : "The screen dims to 5% after 10 minutes with no touch and no key-up; a touch or key-up brings it back.",
  );
}

function onBrightnessInput(event) {
  setText("setup-brightness-value", `${event.target.value}%`);
  clearTimeout(brightnessSaveTimer);
  brightnessSaveTimer = setTimeout(saveBrightness, BRIGHTNESS_SAVE_DEBOUNCE_MS);
}

async function saveBrightness() {
  brightnessSaveTimer = null;
  const slider = document.getElementById("setup-brightness");
  if (!slider) return;
  try {
    await apiFetch("/api/display/brightness", postJson({ brightness: Number(slider.value) }));
  } catch (err) {
    showActionError(`Couldn't set the screen brightness: ${err.message}`);
  }
}

function friendlyDisplayName(name) {
  if (name.startsWith("DSI")) return "touchscreen";
  if (name.startsWith("HDMI")) return `HDMI monitor (${name})`;
  return name;
}

async function setKioskEnabled(enabled) {
  const toggle = document.getElementById("setup-kiosk-toggle");
  if (toggle) toggle.disabled = true;
  try {
    const resp = await apiFetch("/api/kiosk", postJson({ enabled }));
    renderKioskSetting((await resp.json()).kiosk);
  } catch (err) {
    if (toggle) { toggle.checked = !enabled; toggle.disabled = false; }
    throw err;
  }
  pollKioskUntilSettled();
}

function pollKioskUntilSettled() {
  clearTimeout(kioskPollTimer);
  const deadline = Date.now() + KIOSK_POLL_TIMEOUT_MS;
  const tick = async () => {
    try {
      const cfg = await (await fetch("/api/config")).json();
      renderKioskSetting(cfg.kiosk);
      if (!cfg.kiosk?.busy) return;
    } catch {
      // Keep trying until the deadline.
    }
    if (Date.now() < deadline) kioskPollTimer = setTimeout(tick, KIOSK_POLL_MS);
  };
  kioskPollTimer = setTimeout(tick, KIOSK_POLL_MS);
}

// Pointer Events rather than HTML5 drag-and-drop, which doesn't fire on
// touch screens (this gets used from a phone). The row follows the pointer
// by moving in the DOM; the new order is committed on release.
function wireDragHandle(handle, li, list) {
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    // Listeners go on window (below), so capture is only a nicety - some
    // pointers (e.g. synthetic ones) refuse it, which must not abort the drag.
    try {
      handle.setPointerCapture(e.pointerId);
    } catch {
      // fine - window listeners still see every move
    }
    li.classList.add("dragging");
    const onMove = (ev) => {
      const after = [...list.children].find((row) => {
        if (row === li) return false;
        const r = row.getBoundingClientRect();
        return ev.clientY < r.top + r.height / 2;
      });
      if (after) list.insertBefore(li, after);
      else list.appendChild(li);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      li.classList.remove("dragging");
      const order = [...list.children].map((row) => row.dataset.panelId);
      const byId = Object.fromEntries(appState.display.map((q) => [q.id, q]));
      const next = order.map((id) => byId[id]).filter(Boolean);
      if (next.some((q, i) => q.id !== appState.display[i].id)) setDisplayPanels(next);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
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
  // Full-width panels get the Classic meter's wide, short edgewise shape
  // (see gauges.js METER_GEOMETRY), or Modern's wide crescent arc
  // (ARC_GEOMETRY); a width change in Display settings remounts the meter
  // in the new shape.
  const wide = WIDE_METER_WIDTHS.has(el.dataset.width);
  const opts = { label: def.label, unit: def.unit, value, min: def.min, max: typeof def.max === "function" ? def.max() : def.max, plain: !!def.plain, peak, avg, wide, scale: def.scale, warnFrom: def.warnFrom, dangerFrom: def.dangerFrom };
  const widget = resolveWidget(theme, opts, forceWidget);
  const shape = (widget === "needle" || widget === "arc") && wide ? "wide" : "standard";

  if (el.dataset.mountedWidget !== widget || el.dataset.meterShape !== shape) {
    el.dataset.mountedWidget = widget;
    el.dataset.meterShape = shape;
    mountGauge(el, theme, opts, forceWidget);
  } else {
    updateGauge(el, value, peak, avg);
  }

  const badge = ensurePeakBadge(el, def.id);
  badge.textContent = formatPeakBadge(def.id, peak, avg);
}

// Forces every gauge to re-mount (new theme = possibly a new widget/style)
// without removing the panels themselves - #panel-grid also holds the band
// panel, and the panels' Display-layout order must survive a theme change.
function remountAllGauges() {
  document.querySelectorAll("#panel-grid [data-mounted-widget]").forEach((el) => {
    delete el.dataset.mountedWidget;
  });
  if (appState.lastStatus) renderGauges(appState.lastStatus);
}

document.addEventListener("acom-theme-change", (event) => {
  appState.theme = event.detail;
  remountAllGauges();
  updatePanelRows(); // row weights depend on the theme's widget
});

// Band panel - mirrors the middle of the amp's own Basic Screen, e.g.
// "Memory Settings   18.049 - 18.200 MHz   A1R" over a scale between the
// same two edges. Band/edges/antenna/ATU label all come from 0x27 (only
// sent on a band or segment change, so this reflects the last change);
// the marker is the live carrier frequency from 0x2F, which reads 0
// until the amp has actually seen RF. Per Tyler, this replaced the old
// big-digit carrier frequency readout entirely.
function renderFreqReadout(state) {
  const el = document.getElementById("freq-readout");
  if (!el) return;
  const khz = state.telemetry ? state.telemetry.carrier_freq_khz : null;

  const b = state.antenna_band_status;
  const scale = document.getElementById("band-scale");
  if (!b) {
    setText("band-atu-label", "");
    setText("band-range", "Band: unknown");
    setText("band-name", "");
    setText("band-antenna", "");
    if (scale) scale.hidden = true;
    return;
  }
  const low = b.low_border_khz;
  const high = b.high_border_khz;
  setText("band-atu-label", b.atu_label_name && b.atu_label_name !== "No Label" ? b.atu_label_name : "");
  setText("band-range", `${(low / 1000).toFixed(3)} - ${(high / 1000).toFixed(3)} MHz`);
  const overlaps = (lo, hi) => lo < CB_EDGES_KHZ[1] && hi > CB_EDGES_KHZ[0];
  const onCb = loadShowCbBand() && (overlaps(low, high) || (khz && overlaps(khz, khz + 0.001)));
  setText("band-name", onCb ? "CB" : BAND_NAMES[b.band_number - 1] || `Band ${b.band_number}`);
  // Same shorthand as the amp's own display: "A1R" = antenna 1, Regular.
  // Followed by the name given in Antenna Setup, if any: "A1R - DXC".
  const typeLetter = (ANTENNA_TYPES[b.antenna_type] || "?").charAt(0);
  const slot = (state.antenna_config || []).find((s) => s.number === b.antenna_number);
  const antName = slot && slot.name ? slot.name.trim() : "";
  setText("band-antenna", `A${b.antenna_number}${typeLetter}${antName ? ` - ${antName}` : ""}`);

  if (!scale) return;
  // Like the amp's own display: the scale spans the whole band, with the
  // current segment highlighted on it - clipped to the band, since the
  // amp's segments can run past the band edges. A segment entirely
  // outside the table's band (or a band missing from it) falls back to
  // scaling to the segment itself.
  let edges = onCb ? CB_EDGES_KHZ : BAND_EDGES_KHZ[b.band_number - 1];
  if (edges && !(low < edges[1] && high > edges[0])) edges = null;
  const scaleLow = edges ? edges[0] : low;
  const scaleHigh = edges ? edges[1] : high;
  scale.hidden = !(scaleHigh > scaleLow);
  if (scale.hidden) return;
  const pct = (f) => `${((f - scaleLow) / (scaleHigh - scaleLow)) * 100}%`;

  const segment = document.getElementById("band-scale-segment");
  if (segment) {
    const segLow = Math.max(low, scaleLow);
    const segHigh = Math.min(high, scaleHigh);
    segment.style.left = pct(segLow);
    segment.style.width = `${((segHigh - segLow) / (scaleHigh - scaleLow)) * 100}%`;
  }
  renderBandTicks(scaleLow, scaleHigh, pct);

  const marker = document.getElementById("band-scale-marker");
  if (marker) {
    const inBand = khz && khz >= scaleLow && khz <= scaleHigh;
    marker.hidden = !inBand;
    if (inBand) marker.style.left = pct(khz);
  }
}

// Round-number tick labels across the band, like the amp's 14000 / 14100
// / ... / 14400 - the smallest step that keeps it to about five labels,
// plus both band edges when they aren't round themselves.
const BAND_TICK_STEPS_KHZ = [10, 25, 50, 100, 200, 250, 500, 1000];

function renderBandTicks(lo, hi, pct) {
  const el = document.getElementById("band-scale-ticks");
  if (!el) return;
  const span = hi - lo;
  const step = BAND_TICK_STEPS_KHZ.find((s) => span / s <= 5) || 1000;
  const ticks = new Set([lo, hi]);
  for (let f = Math.ceil(lo / step) * step; f < hi; f += step) {
    // Skip round ticks crowding a non-round edge label.
    if (f - lo >= span * 0.1 && hi - f >= span * 0.1) ticks.add(f);
  }
  el.innerHTML = [...ticks].sort((a, b) => a - b).map((f) => {
    const edge = f === lo ? "start" : f === hi ? "end" : "";
    return `<span class="band-tick" data-edge="${edge}" style="left:${pct(f)}">${f}</span>`;
  }).join("");
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
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
  // The alarm the amp's own screen is showing right now (0x2F), not the
  // 0x21 error bitfield: 0x21 bits can stay set long after the amp has
  // cleared them from its screen (seen with 0x8E, still set minutes after
  // a tune), which left the banner showing stale faults. 0x21's full list
  // is still in /api/status and the amp snapshot.
  const alarm = state.telemetry ? state.telemetry.alarm_name : null;
  const names = alarm ? [alarm] : [];

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
  document.getElementById("btn-power-off")?.addEventListener("click", withErrorBanner(() => {
    poweringDownRequested = true;
    updateScreenVisibility();
    return apiFetch("/api/power", postJson({ on: false }));
  }));
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
  document.getElementById("btn-atu-bypass")?.addEventListener("click", withErrorBanner(async () => {
    const result = await (await apiFetch("/api/atu/bypass", { method: "POST" })).json();
    // Leaving Bypass re-sends the Bypass command as a toggle (like the
    // amp's own hold-TUNE) - see /api/atu/bypass. If the amp ignored it,
    // say so rather than leaving the click looking like it did nothing.
    if (result.still_bypassed) {
      showActionError("The amp stayed in ATU Bypass. Use Tune to re-engage the ATU (runs a full tune).", "info");
    }
  }));
  document.getElementById("btn-atu-tune")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/atu/tune", { method: "POST" })));
  document.getElementById("btn-antenna-next")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/antenna", postJson({ action: "next" }))));
  document.getElementById("btn-buzzer-test")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/buzzer/morse", postJson({ volume: 50 }))));

  document.getElementById("btn-setup-open")?.addEventListener("click", withErrorBanner(toggleSetupScreen));
  document.getElementById("btn-setup-close")?.addEventListener("click", closeSetupScreen);
  document.getElementById("btn-amp-settings-save")?.addEventListener("click", withErrorBanner(saveAmpSettings));
  document.getElementById("btn-antenna-setup-save")?.addEventListener("click", withErrorBanner(saveAntennaSetup));
  wireSetupEditTracking();
  // Expanding Antenna Setup reads names and band assignments from the amp
  // again (Settings already did on open, but the amp may have changed since).
  document.getElementById("antenna-setup-section")?.addEventListener("toggle", (event) => {
    if (event.target.id === "antenna-setup-section" && event.target.open) refreshFromAmp();
  });
  document.getElementById("btn-cat-settings-save")?.addEventListener("click", withErrorBanner(saveCatSettings));
  document.getElementById("setup-tuner-select")?.addEventListener("change", updateAntennaSetupVisibility);
  document.getElementById("btn-download-logs")?.addEventListener("click", () => window.open("/api/logs", "_blank"));
  document.getElementById("btn-amp-snapshot")?.addEventListener("click", () => window.open("/api/amp-snapshot", "_blank"));
  document.getElementById("btn-update-available")?.addEventListener("click", openUpdateScreen);
  document.getElementById("btn-update-close")?.addEventListener("click", closeUpdateScreen);
  document.getElementById("btn-update-now")?.addEventListener("click", withErrorBanner(applyUpdateNow));
  document.getElementById("update-beta-toggle")?.addEventListener("change", setBetaUpdates);
  document.getElementById("btn-display-reset")?.addEventListener("click", () => setDisplayPanels(DEFAULT_DISPLAY_PANELS));
  document.getElementById("setup-kiosk-toggle")?.addEventListener("change", withErrorBanner((event) => setKioskEnabled(event.target.checked)));
  document.getElementById("setup-brightness")?.addEventListener("input", onBrightnessInput);
  const cbCheckbox = document.getElementById("setup-show-cb-band");
  if (cbCheckbox) {
    cbCheckbox.checked = loadShowCbBand();
    // Applies immediately, like the theme switcher - no Save needed.
    cbCheckbox.addEventListener("change", () => {
      saveShowCbBand(cbCheckbox.checked);
      if (appState.lastStatus) renderFreqReadout(appState.lastStatus);
    });
  }
  document.getElementById("btn-update-check")?.addEventListener("click", checkForUpdateNow);
  document.getElementById("btn-update-rollback")?.addEventListener("click", withErrorBanner(rollbackToRelease));
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

// The gear icon toggles Settings: a second press goes back to the dashboard.
function toggleSetupScreen() {
  if (appState.setupOpen) closeSetupScreen();
  else openSetupScreen();
}

function openSetupScreen() {
  appState.setupOpen = true;
  appState.updateScreenOpen = false;
  // Fresh form on every open - nothing half-edited from last time is kept.
  document.querySelectorAll("#setup-screen [data-edited]").forEach((el) => delete el.dataset.edited);
  updateScreenVisibility();
  // Show what the backend already has right away, then ask the amp again.
  loadSetup().then(refreshFromAmp);
}

function closeSetupScreen() {
  appState.setupOpen = false;
  updateScreenVisibility();
}

let setupResyncPending = null;

async function refreshFromAmp() {
  // Replaces the old "Refresh from Amp" button: every Settings open
  // re-asks the amp for everything Setup shows. POST /api/resync re-sends
  // the 0x11/0x12/0x16/0x18/0x1B/0x2A requests one at a time and only
  // returns once they've been answered or given up on (a few seconds at
  // most), so the re-read right after it sees the results - see CLAUDE.md
  // open question 6 for why the startup handshake alone isn't enough.
  // Sections edited meanwhile keep their edits (see sectionEdited()).
  // Also runs when Antenna Setup is expanded, with a progress bar above its
  // matrix while it reads.
  if (setupResyncPending) return setupResyncPending;
  if (!appState.lastStatus?.comms_ok) return;
  const busyEls = ["setup-sync-note", "antenna-sync-progress"].map((id) => document.getElementById(id));
  busyEls.forEach((el) => { if (el) el.hidden = false; });
  setupResyncPending = (async () => {
    try {
      await apiFetch("/api/resync", { method: "POST" });
      if (appState.setupOpen) await loadSetup();
    } catch {
      // Silent - the cached values stay on screen.
    } finally {
      busyEls.forEach((el) => { if (el) el.hidden = true; });
      setupResyncPending = null;
    }
  })();
  return setupResyncPending;
}

// True once the user has changed anything in that Setup section since it
// was last opened or saved - loadSetup() then leaves it alone, so the
// automatic refresh (or saving another section) doesn't wipe their edits.
function sectionEdited(id) {
  return document.getElementById(id)?.dataset.edited === "true";
}

function wireSetupEditTracking() {
  for (const id of ["setup-amp-section", "cat-settings-section", "antenna-setup-section"]) {
    const section = document.getElementById(id);
    // The CB band checkbox applies on its own and the Danger Zone has its
    // own buttons - neither is part of a section's Save.
    const mark = (event) => {
      if (event.target.closest("#setup-show-cb-band, #antenna-danger-zone")) return;
      section.dataset.edited = "true";
    };
    section?.addEventListener("input", mark);
    section?.addEventListener("change", mark);
  }
}

function clearSectionEdited(id) {
  const section = document.getElementById(id);
  if (section) delete section.dataset.edited;
}

async function loadSetup() {
  const [configResp, antennaResp, portsResp] = await Promise.all([
    apiFetch("/api/config").then((r) => r.json()),
    apiFetch("/api/antenna/config").then((r) => r.json()),
    apiFetch("/api/serial-ports").then((r) => r.json()),
  ]);
  if (!sectionEdited("setup-amp-section")) populateConfigForm(configResp);
  // Not while a save is pending - that would snap the list back to the
  // previous layout mid-edit.
  if (Array.isArray(configResp.display) && !displaySaveTimer) {
    appState.display = configResp.display;
    applyDisplayLayout(appState.display);
  }
  renderDisplaySettings();
  renderKioskSetting(configResp.kiosk);
  renderBrightnessSetting(configResp);
  // Reopening Setup while the Pi display is still switching: keep following it.
  if (configResp.kiosk?.busy) pollKioskUntilSettled();
  if (!sectionEdited("setup-amp-section")) populateSerialPortOptions(portsResp, configResp.serial_device);
  if (!sectionEdited("antenna-setup-section")) populateAntennaMatrix(antennaResp.antennas);
  populateSegmentDeleteBandOptions();
  renderAtuStatusPanel(appState.lastStatus);
  populateCatSettingsOptions();
  if (!sectionEdited("cat-settings-section")) renderCatSettingsForm(appState.lastStatus);
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
    // message. The section stays marked edited for the same reason, so
    // loadSetup() leaves it alone until Settings is reopened, which
    // re-reads the amp's confirmed values.
    statusEl.hidden = false;
    statusEl.textContent = "Saved. Reopen Settings to see the amp's confirmed reading.";
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

// Each Setup section that needs one has its own Save, covering only that
// section's fields.
async function saveAmpSettings() {
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

  clearSectionEdited("setup-amp-section");
  await loadSetup();
}

async function saveAntennaSetup() {
  const antennas = readAntennaMatrix();
  await apiFetch("/api/antenna/config", postJson({ antennas }));
  clearSectionEdited("antenna-setup-section");
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

// Dashboard footer showing the Pi's main LAN address. Re-polled because a
// DHCP lease can change while the kiosk page stays open for days.
const NETWORK_FOOTER_POLL_MS = 60 * 1000;

async function refreshNetworkFooter() {
  const footer = document.getElementById("network-footer");
  if (!footer) return;
  try {
    const resp = await fetch("/api/network");
    if (!resp.ok) return;
    const { address, port } = await resp.json();
    const suffix = port && port !== 80 ? `:${port}` : "";
    footer.textContent = address ? `Dashboard: http://${address}${suffix}` : "No network connection";
    footer.hidden = false;
  } catch {
    // Silent - passive background poll; keep the last address shown.
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

const UPDATE_CHECK_MIN_SHOWN_MS = 600;

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
  // While the check runs, only the progress bar shows: the half-filled
  // info underneath (no version yet, beta box not yet ticked from the
  // backend's answer) looked like the real state.
  const checkingEl = document.getElementById("update-checking");
  const infoEl = document.getElementById("update-info");
  if (checkingEl) checkingEl.hidden = false;
  if (infoEl) infoEl.hidden = true;
  // Kept up at least this long, so a fast check doesn't just flicker.
  const minShown = new Promise((resolve) => setTimeout(resolve, UPDATE_CHECK_MIN_SHOWN_MS));
  if (headline) headline.textContent = "";
  if (currentEl) currentEl.textContent = "";
  if (latestEl) latestEl.textContent = "";
  if (statusEl) statusEl.hidden = true;
  const latestRow = document.getElementById("update-latest-row");
  const rollbackBox = document.getElementById("update-rollback");
  const rollbackButton = document.getElementById("btn-update-rollback");
  if (latestRow) latestRow.hidden = true;
  if (rollbackBox) rollbackBox.hidden = true;
  if (rollbackButton) rollbackButton.disabled = false;
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
    await minShown;
    if (betaToggle) betaToggle.checked = !!updateStatus.beta;
    if (currentEl) currentEl.textContent = updateStatus.current_version || "unknown";
    if (latestEl) latestEl.textContent = updateStatus.latest_version || "no tagged release yet";
    if (headline) {
      headline.textContent = updateStatus.available
        ? "Update available"
        : updateStatus.error ? `Check failed: ${updateStatus.error}` : "Already up to date";
    }
    if (updateNowButton) updateNowButton.hidden = !updateStatus.available;
    // "New version" only when there's something newer; otherwise the
    // headline's "Already up to date" says it.
    if (latestRow) latestRow.hidden = !updateStatus.available;
    // Beta updates off while running a beta build: offer the way back to
    // the latest release (the backend only sets can_rollback with beta off).
    if (rollbackBox && updateStatus.can_rollback && updateStatus.release) {
      rollbackBox.hidden = false;
      setText(
        "update-rollback-note",
        `You're running a beta build (${updateStatus.current_version}), newer than the latest release.`,
      );
      if (rollbackButton) rollbackButton.textContent = `Roll back to ${updateStatus.release}`;
    }
    renderUpdateChanges();
    renderUpdateButtonHighlight();
  } catch (err) {
    await minShown;
    if (headline) headline.textContent = `Check failed: ${err.message}`;
  } finally {
    if (betaToggle) betaToggle.disabled = false;
    if (checkButton) checkButton.disabled = false;
    if (checkingEl) checkingEl.hidden = true;
    if (infoEl) infoEl.hidden = false;
  }
}

async function applyUpdateNow() {
  if (!window.confirm(
    "Update now? The backend will restart - amp control will be briefly " +
    "unavailable (usually just a few seconds)."
  )) return;
  await launchUpdate("/api/update/apply", document.getElementById("btn-update-now"), "Updating…");
}

async function rollbackToRelease() {
  const release = updateStatus?.release || "the latest release";
  if (!window.confirm(
    `Roll back to ${release}? The backend will restart - amp control will be ` +
    "briefly unavailable (usually just a few seconds)."
  )) return;
  await launchUpdate("/api/update/rollback", document.getElementById("btn-update-rollback"), `Rolling back to ${release}…`);
}

// Shared by Update Now and Roll Back: start the helper, then wait for the
// restarted service to report a different commit.
async function launchUpdate(path, button, startingText) {
  const statusEl = document.getElementById("update-status");
  // Raw commit hash, not updateStatus.current_version (that's the
  // tag-based display string) - waitForUpdateRestart() just needs
  // something that changes once the new process comes up.
  let beforeCommit = null;
  try {
    beforeCommit = (await (await fetch("/api/config", { cache: "no-store" })).json()).git_commit;
  } catch {
    // If this fails, waitForUpdateRestart() below will just run out its
    // timeout without detecting a restart - acceptable degradation.
  }
  if (button) button.disabled = true;
  if (statusEl) statusEl.hidden = true;
  showUpdateApplying(startingText);
  try {
    await apiFetch(path, { method: "POST" });
  } catch (err) {
    hideUpdateApplying();
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.textContent = `Failed to start update: ${err.message}`;
    }
    if (button) button.disabled = false;
    return;
  }
  await waitForUpdateRestart(beforeCommit, statusEl, button);
}

// The full-screen "Updating..." cover (index.html #update-applying): stays
// up from the click until the restarted backend is seen and the page
// reloads, or until waitForUpdateRestart() gives up.
function showUpdateApplying(title) {
  setText("update-applying-title", title);
  const el = document.getElementById("update-applying");
  if (el) el.hidden = false;
}

function hideUpdateApplying() {
  const el = document.getElementById("update-applying");
  if (el) el.hidden = true;
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
      // no-store: a cached copy would still show the old commit.
      const resp = await fetch("/api/config", { cache: "no-store" });
      if (resp.ok) {
        const cfg = await resp.json();
        if (cfg.git_commit && cfg.git_commit !== beforeCommit) {
          setText("update-applying-title", "Update complete - reloading...");
          setTimeout(() => window.location.reload(), 1000);
          return;
        }
      }
    } catch {
      // Expected while the service is restarting - keep polling.
    }
  }
  hideUpdateApplying();
  if (statusEl) {
    statusEl.hidden = false;
    statusEl.textContent =
      "No restart detected yet - may already be up to date, or the update " +
      "may have failed. Check the logs if you expected a change.";
  }
  if (button) button.disabled = false;
}

// =============================================================================
// Boot
// =============================================================================

// =============================================================================
// Kiosk display (?kiosk=1 - the Pi's own screen, see setup/acom-kiosk.sh)
// =============================================================================

// How long the cursor stays visible after the mouse last moved.
const KIOSK_CURSOR_IDLE_MS = 4000;

// index.html already hid the cursor before first paint; this shows it while
// a real mouse is in use and hides it again when the mouse goes idle.
// pointerType tells a mouse apart from a finger, so touching the screen
// never brings the cursor up - and it works the same whether the mouse was
// plugged in at boot or later.
function initKioskMode() {
  const root = document.documentElement;
  if (!root.classList.contains("kiosk")) return;
  let hideTimer = null;
  document.addEventListener(
    "pointermove",
    (event) => {
      if (event.pointerType !== "mouse") return;
      root.classList.remove("kiosk-cursor-hidden");
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => root.classList.add("kiosk-cursor-hidden"), KIOSK_CURSOR_IDLE_MS);
    },
    { passive: true },
  );
  // A long press on a touchscreen would otherwise open the browser's menu.
  document.addEventListener("contextmenu", (event) => event.preventDefault());
}

// Inactivity dimming, on every device: after IDLE_DIM_AFTER_MS with no
// touch/click/key/mouse movement and no key-up (render() calls
// noteActivity() while in OPR/TX), the screen dims to 5%; any of those
// brings it back. On the kiosk that's the Pi touchscreen's real backlight
// (POST /api/display/backlight, back up to Setup -> Display & Sound's brightness);
// where there's no backlight to drive - a remote browser, or an HDMI
// monitor on the Pi - the overlay darkens the page instead. Either way the
// overlay catches the waking touch, so it doesn't also press a button.
const IDLE_DIM_AFTER_MS = 10 * 60 * 1000;
// Keeps catching taps for a moment after waking, so the rest of the waking
// tap (pointerup/click) can't land on the button under it either.
const IDLE_WAKE_GUARD_MS = 500;
let idleDimTimer = null;
let idleDimmed = false;
let piBacklightWorks = false;

function isKiosk() {
  return document.documentElement.classList.contains("kiosk");
}

async function setPiBacklight(dimmed) {
  try {
    const resp = await apiFetch("/api/display/backlight", postJson({ dimmed }));
    piBacklightWorks = !!(await resp.json()).backlight;
  } catch {
    piBacklightWorks = false;
  }
  document.documentElement.classList.toggle("idle-dim-hardware", piBacklightWorks);
}

function dimForIdle() {
  idleDimmed = true;
  const root = document.documentElement;
  root.classList.toggle("idle-dim-hardware", isKiosk() && piBacklightWorks);
  root.classList.add("idle-dimmed");
  if (isKiosk()) setPiBacklight(true);
}

function wakeFromIdle() {
  idleDimmed = false;
  const root = document.documentElement;
  root.classList.remove("idle-dimmed");
  root.classList.add("idle-waking");
  setTimeout(() => root.classList.remove("idle-waking"), IDLE_WAKE_GUARD_MS);
  if (isKiosk()) setPiBacklight(false);
}

function noteActivity() {
  clearTimeout(idleDimTimer);
  idleDimTimer = setTimeout(dimForIdle, IDLE_DIM_AFTER_MS);
  if (idleDimmed) wakeFromIdle();
}

function initIdleDim() {
  const overlay = document.getElementById("idle-dim-overlay");
  if (overlay) {
    const swallow = (event) => {
      event.preventDefault();
      event.stopPropagation();
    };
    for (const type of ["pointerdown", "pointerup", "click", "touchstart", "touchend"]) {
      overlay.addEventListener(type, swallow);
    }
  }
  // Capture phase, so activity counts even where a handler stops it.
  for (const type of ["pointerdown", "keydown", "wheel"]) {
    document.addEventListener(type, noteActivity, { capture: true, passive: true });
  }
  document.addEventListener(
    "pointermove",
    (event) => {
      if (event.pointerType === "mouse") noteActivity();
    },
    { capture: true, passive: true },
  );
  // Back to full brightness whenever the kiosk page loads - after a reload
  // or an update the backlight may still be at the dim level.
  if (isKiosk()) setPiBacklight(false);
  noteActivity();
}

// Alarm sounds: when the amp shows a new alarm, play it in Morse like the
// amp's own buzzer - telemetry.alarm_level is the protocol doc's WARNING /
// SOFT FAULT / HARD FAULT label for the live alarm code (protocol.py's
// FAULT_CODE_LEVELS). Per the 1200S manual a hard fault is "a series of F
// sent in CW"; a warning plays W (heard by Tyler on "ATU tuning outdated",
// a warning). The manual doesn't say what a soft fault plays - S is a
// stand-in. Synthesized with Web Audio, so no sound files; on/off per
// browser in Setup -> Display & Sound.
const ALARM_SOUNDS_STORAGE_KEY = "acom.alarmSounds";
const ALARM_VOLUME_STORAGE_KEY = "acom.alarmVolume";
const ALARM_MORSE = { warning: "W", soft_fault: "S", hard_fault: "FFF" };
// Power sounds, as heard from the amp itself (per Tyler) - neither the 1200S
// manual nor the protocol doc spells them out (the doc only names
// BUZZER_PWRON/BUZZER_PWROFF signals, no pattern). "TEST" while it boots
// (CLAUDE.md's "startup TEST" note), "R" once it's ready, and the AR
// prosign (.-.-., which is also Morse for "+") when it turns off.
const POWER_MORSE = { on: "TEST", ready: "R", off: "+" };
const MORSE_CODE = { E: ".", F: "..-.", R: ".-.", S: "...", T: "-", W: ".--", "+": ".-.-." };
const ALARM_TONE_HZ = 1000;
// 15 at first (a guess at the amp's own speed) - per Tyler it sounded too
// slow next to the amp, so 20.
const ALARM_WPM = 20;
// The volume slider's 100% - the level these first shipped at, which Tyler
// found a bit loud, hence the slider (default 60%, per browser).
const ALARM_MAX_GAIN = 0.3;
const ALARM_DEFAULT_VOLUME = 60;
let alarmAudioCtx = null;
let lastAlarmCode = null;
// One sound per run of alarms. After a fault the amp cycles its latched
// alarms on screen - 2026-11-03: two hard faults, 0xA9 and 0xA7,
// alternating every ~2.5s for 20s after power-up - and each switch used to
// sound again. Now a run only sounds again if a more serious level shows up
// (a warning, then a hard fault). The run ends once the alarm area has
// stayed clear for ALARM_CLEAR_RESET_MS.
const ALARM_LEVEL_RANK = { warning: 1, soft_fault: 2, hard_fault: 3 };
const ALARM_CLEAR_RESET_MS = 5000;
let soundedAlarmRank = 0;
let alarmClearSince = null;
// When the last queued Morse finishes (AudioContext time), so back-to-back
// sounds - TEST then R on power-up - play one after the other, not on top
// of each other.
let morseBusyUntil = 0;
// The last definite power_state ("on"/"off"), skipping "unknown": a real
// power-up logs off -> on (a frame right after the pulse) -> unknown (the
// amp goes quiet ~10s while it boots) -> on, which must only sound once.
// null until the first status, so loading the page doesn't play anything.
let lastKnownPower = null;
// system_clock_s of the telemetry on screen while the amp wasn't "on". The
// backend keeps the last 0x2F after the amp goes off, so straight after a
// power-up the dashboard still shows the old STB mode - telemetry only
// counts as live once its clock differs from this.
let staleTelemetryClock = null;
let awaitingReady = false;
let offSoundPlayed = false;
// Set the moment our own Power Off button is clicked, so the powering-down
// screen (see isPoweringDown() below) shows immediately rather than waiting
// on the ~1.5s DTR/RTS pulse to finish. Cleared once power_state actually
// reaches "off" - see updatePoweringDownState().
let poweringDownRequested = false;

function loadAlarmSoundsEnabled() {
  try {
    return localStorage.getItem(ALARM_SOUNDS_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

function saveAlarmSoundsEnabled(enabled) {
  try {
    localStorage.setItem(ALARM_SOUNDS_STORAGE_KEY, enabled ? "on" : "off");
  } catch {
    // Private window etc. - the checkbox still works until the page reloads.
  }
}

function loadAlarmVolume() {
  try {
    const saved = Number(localStorage.getItem(ALARM_VOLUME_STORAGE_KEY));
    return saved >= 5 && saved <= 100 ? saved : ALARM_DEFAULT_VOLUME;
  } catch {
    return ALARM_DEFAULT_VOLUME;
  }
}

function saveAlarmVolume(percent) {
  try {
    localStorage.setItem(ALARM_VOLUME_STORAGE_KEY, String(percent));
  } catch {
    // Private window etc. - the slider still works until the page reloads.
  }
}

// Squared, so the slider's lower half isn't all "barely quieter": loudness
// is heard roughly logarithmically, and a straight line puts most of the
// audible change at the bottom of the travel.
function alarmGain() {
  return ALARM_MAX_GAIN * (loadAlarmVolume() / 100) ** 2;
}

// Browsers only let a page make sound once it's been tapped or clicked, so
// the audio context is created (or resumed) on the first one. The kiosk's
// Chromium is started without that rule (acom-kiosk.sh).
function unlockAlarmAudio() {
  if (!alarmAudioCtx) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    alarmAudioCtx = new AudioCtx();
  }
  if (alarmAudioCtx.state === "suspended") alarmAudioCtx.resume().catch(() => {});
}

// Standard Morse timing: dah = 3 dits, 1 dit between elements, 3 between
// letters, 7 between words (a space).
function playMorse(text) {
  unlockAlarmAudio();
  const ctx = alarmAudioCtx;
  if (!ctx) return;
  const dit = 1.2 / ALARM_WPM;
  const volume = alarmGain();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.frequency.value = ALARM_TONE_HZ;
  gain.gain.value = 0;
  osc.connect(gain).connect(ctx.destination);
  const start = Math.max(ctx.currentTime + 0.05, morseBusyUntil + dit * 7);
  let t = start;
  for (const ch of text) {
    if (ch === " ") {
      t += dit * 4;
      continue;
    }
    for (const element of MORSE_CODE[ch] || "") {
      const len = element === "-" ? dit * 3 : dit;
      // 5ms ramps, so each tone doesn't start and stop with a click.
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(volume, t + 0.005);
      gain.gain.setValueAtTime(volume, t + len - 0.005);
      gain.gain.linearRampToValueAtTime(0, t + len);
      t += len + dit;
    }
    t += dit * 2;
  }
  osc.start(start);
  osc.stop(t);
  morseBusyUntil = t;
}

// Called on every status update, like checkAlarmSound(). Plays on the
// transitions only: TEST when the amp comes on (off -> on), R when live
// telemetry first shows STB/OPR after that, AR once when it starts turning off (or is concluded
// off - see power_state in amp_state.py - if the Turn OFF mode was missed).
function checkPowerSounds(state) {
  const power = state.power_state;
  const clock = state.telemetry ? state.telemetry.system_clock_s : null;
  if (power !== "on") staleTelemetryClock = clock;
  const live = power === "on" && clock !== null && clock !== staleTelemetryClock;
  const modeName = live ? state.telemetry.mode_name : null;
  const sounds = [];
  if (power === "on" && lastKnownPower === "off") {
    sounds.push(POWER_MORSE.on);
    awaitingReady = true;
  }
  if (modeName === "Init Mode" || modeName === "Amplifier Reset") awaitingReady = true;
  if (awaitingReady && [MODE_NAME_STB, MODE_NAME_OPR_RX, MODE_NAME_OPR_TX].includes(modeName)) {
    sounds.push(POWER_MORSE.ready);
    awaitingReady = false;
  }
  const turningOffMode = modeName === "Turn OFF Procedure" || modeName === "Extra Cooling State";
  const turningOff = turningOffMode || (power === "off" && lastKnownPower === "on");
  if (turningOff && !offSoundPlayed) {
    sounds.push(POWER_MORSE.off);
    offSoundPlayed = true;
    awaitingReady = false;
  }
  if (modeName && !turningOffMode) offSoundPlayed = false;
  if (power === "on" || power === "off") lastKnownPower = power;
  if (sounds.length && loadAlarmSoundsEnabled()) sounds.forEach(playMorse);
}

// Called on every status update; sounds once per run of alarms (see
// soundedAlarmRank), not for as long as one stays on the amp's screen or
// each time the amp cycles between them. Any new alarm also wakes a dimmed
// screen.
function checkAlarmSound(state) {
  const code = state.telemetry ? state.telemetry.alarm_code : null;
  const level = state.telemetry ? state.telemetry.alarm_level : null;
  if (!level) {
    alarmClearSince ??= Date.now();
    if (Date.now() - alarmClearSince >= ALARM_CLEAR_RESET_MS) soundedAlarmRank = 0;
  } else {
    alarmClearSince = null;
  }
  if (code !== lastAlarmCode && level) {
    noteActivity();
    const rank = ALARM_LEVEL_RANK[level] || 1;
    if (rank > soundedAlarmRank && loadAlarmSoundsEnabled()) playMorse(ALARM_MORSE[level] || "");
    soundedAlarmRank = Math.max(soundedAlarmRank, rank);
  }
  lastAlarmCode = code;
}

function initAlarmSounds() {
  for (const type of ["pointerdown", "keydown"]) {
    document.addEventListener(type, unlockAlarmAudio, { capture: true, passive: true });
  }
  const checkbox = document.getElementById("setup-alarm-sounds");
  if (checkbox) {
    checkbox.checked = loadAlarmSoundsEnabled();
    checkbox.addEventListener("change", () => saveAlarmSoundsEnabled(checkbox.checked));
  }
  const volume = document.getElementById("setup-alarm-volume");
  if (volume) {
    volume.value = loadAlarmVolume();
    setText("setup-alarm-volume-value", `${volume.value}%`);
    volume.addEventListener("input", () => {
      setText("setup-alarm-volume-value", `${volume.value}%`);
      saveAlarmVolume(Number(volume.value));
    });
    // A short W at the new level when the slider is let go, so it can be
    // set by ear.
    volume.addEventListener("change", () => playMorse("W"));
  }
  document.getElementById("btn-alarm-sound-test")?.addEventListener("click", () => playMorse("W S FFF"));
}

// The backend commit this page was loaded against. When the WebSocket
// reconnects after the backend restarts on a different commit (an update,
// from any device), reload so this page runs the matching dashboard code -
// mostly for the kiosk screen, which nobody would otherwise refresh.
let loadedBackendCommit = null;

async function reloadIfBackendUpdated() {
  if (!loadedBackendCommit) return;
  try {
    const cfg = await (await fetch("/api/config")).json();
    if (cfg.git_commit && cfg.git_commit !== loadedBackendCommit) window.location.reload();
  } catch {
    // Backend not fully up yet - the next reconnect checks again.
  }
}

async function boot() {
  initThemeSwitcher();
  initKioskMode();
  initIdleDim();
  initAlarmSounds();
  updateFitMode();
  appState.display = DEFAULT_DISPLAY_PANELS;
  wireControls();
  try {
    const cfg = await (await apiFetch("/api/config")).json();
    if (Array.isArray(cfg.display)) appState.display = cfg.display;
    loadedBackendCommit = cfg.git_commit || null;
  } catch {
    // Keep the default layout - the dashboard still works.
  }
  applyDisplayLayout(appState.display);
  try {
    const resp = await apiFetch("/api/status");
    render(await resp.json());
  } catch {
    // WebSocket connect below will retry; initial render just stays empty.
  }
  connectWebSocket();
  refreshUpdateStatus();
  setInterval(refreshUpdateStatus, UPDATE_STATUS_POLL_MS);
  refreshNetworkFooter();
  setInterval(refreshNetworkFooter, NETWORK_FOOTER_POLL_MS);
}

boot();
