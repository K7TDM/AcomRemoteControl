// WebSocket client, REST calls, UI update logic. See PI_CONTROL_PLAN.md
// step 6 "Frontend dashboard" and "Backend vs. frontend logging".

import { initThemeSwitcher, loadTheme } from "./theme.js";
import { mountGauge, updateGauge, currentWidgetForTheme } from "./gauges.js";

const BAND_NAMES = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"];
const ANTENNA_TYPES = ["Sharp", "Regular", "Wide", "Fixed"];

const GAUGE_DEFS = [
  { id: "forward_power_w", label: "Forward Power", unit: "W", min: 0, max: () => appState.maxPowerW || 1500 },
  { id: "reflected_power_w", label: "Reflected Power", unit: "W", min: 0, max: () => Math.max(50, (appState.maxPowerW || 1500) * 0.2) },
  { id: "temp_pam1_c", label: "PA Temperature", unit: "°C", min: 0, max: () => 100 },
  { id: "input_power_w", label: "Input Power", unit: "W", min: 0, max: () => 100 },
  { id: "id1_ma", label: "PA DC Current", unit: "mA", min: 0, max: () => 2000 },
];

const appState = {
  maxPowerW: 1500,
  lastStatus: null,
  // Cached rather than re-read from localStorage on every telemetry tick
  // (several times a second over the WebSocket) - only updated via the
  // "acom-theme-change" event, which fires on the rare actual change.
  theme: loadTheme(),
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

let actionErrorTimeout = null;

function showActionError(message, kind = "error") {
  const el = document.getElementById("action-error-banner");
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("action-error-info", kind === "info");
  el.hidden = false;
  if (actionErrorTimeout) clearTimeout(actionErrorTimeout);
  actionErrorTimeout = setTimeout(() => {
    el.hidden = true;
  }, 6000);
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

  renderStatusRow(state);
  renderGauges(state);
  renderFreqReadout(state);
  renderWarningBanner(state);
}

function renderStatusRow(state) {
  const row = document.getElementById("status-row");
  if (!row) return;
  const t = state.telemetry;
  const chips = [
    chip("Comms", state.comms_ok),
    chip("Mode", true, t ? t.mode_name : "unknown"),
    chip("ATU", t ? t.atu_assigned : false, state.atu_identity ? state.atu_identity.type_name : "none"),
    chip("Default Ant", t ? t.default_antenna_selected : false),
  ];
  row.innerHTML = "";
  chips.forEach((c) => row.appendChild(c));
}

function chip(label, on, text) {
  const el = document.createElement("span");
  el.className = "status-chip";
  const led = document.createElement("span");
  led.className = "led-dot" + (on ? " on" : "");
  el.appendChild(led);
  const span = document.createElement("span");
  span.textContent = text ? `${label}: ${text}` : label;
  el.appendChild(span);
  return el;
}

function renderGauges(state) {
  const grid = document.getElementById("gauge-grid");
  if (!grid) return;
  const theme = appState.theme.style;
  const t = state.telemetry || {};

  GAUGE_DEFS.forEach((def) => {
    let el = grid.querySelector(`[data-gauge-id="${def.id}"]`);
    const value = t[def.id] ?? null;
    const opts = { label: def.label, unit: def.unit, value, min: def.min, max: typeof def.max === "function" ? def.max() : def.max };

    if (!el) {
      el = document.createElement("div");
      el.className = "gauge-wrapper";
      el.dataset.gaugeId = def.id;
      el.dataset.mountedWidget = currentWidgetForTheme(theme);
      grid.appendChild(el);
      mountGauge(el, theme, opts);
      return;
    }

    if (el.dataset.mountedWidget !== currentWidgetForTheme(theme)) {
      el.dataset.mountedWidget = currentWidgetForTheme(theme);
      mountGauge(el, theme, opts);
    } else {
      updateGauge(el, value);
    }
  });
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

function renderWarningBanner(state) {
  const el = document.getElementById("warning-banner");
  if (!el) return;
  const errors = state.error_flags;
  if (errors && errors.active_names && errors.active_names.length > 0) {
    el.textContent = errors.active_names[0] + (errors.active_names.length > 1 ? ` (+${errors.active_names.length - 1} more)` : "");
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// =============================================================================
// Controls
// =============================================================================

function wireControls() {
  document.getElementById("btn-power-on")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/power", postJson({ on: true }))));
  document.getElementById("btn-power-off")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/power", postJson({ on: false }))));
  document.getElementById("btn-mode-stb")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/mode", postJson({ mode: "stb" }))));
  document.getElementById("btn-mode-opr-rx")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/mode", postJson({ mode: "opr_rx" }))));
  document.getElementById("btn-mode-opr-tx")?.addEventListener("click", openTxConfirmDialog);
  document.getElementById("btn-band-next")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/band", postJson({ direction: "next" }))));
  document.getElementById("btn-band-prev")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/band", postJson({ direction: "prev" }))));
  document.getElementById("btn-antenna-next")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/antenna", postJson({ action: "next" }))));
  document.getElementById("btn-buzzer-test")?.addEventListener("click", withErrorBanner(() => apiFetch("/api/buzzer", postJson({ signal: 1, volume: 50 }))));

  document.getElementById("btn-setup-open")?.addEventListener("click", withErrorBanner(openSetupScreen));
  document.getElementById("btn-setup-close")?.addEventListener("click", closeSetupScreen);
  document.getElementById("btn-setup-save")?.addEventListener("click", withErrorBanner(saveSetup));
  document.getElementById("btn-setup-refresh")?.addEventListener("click", withErrorBanner(loadSetup));
  document.getElementById("setup-tuner-select")?.addEventListener("change", updateAntennaSetupVisibility);

  document.getElementById("tx-confirm-ok")?.addEventListener("click", confirmTxArmAndFire);
  document.getElementById("tx-confirm-cancel")?.addEventListener("click", () => {
    document.getElementById("tx-confirm-dialog")?.close();
  });

  wireAtuDangerZone();
}

function postJson(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function openTxConfirmDialog() {
  const dialog = document.getElementById("tx-confirm-dialog");
  if (dialog && typeof dialog.showModal === "function") dialog.showModal();
}

async function confirmTxArmAndFire() {
  const dialog = document.getElementById("tx-confirm-dialog");
  try {
    const armResp = await apiFetch("/api/mode/arm_tx", { method: "POST" });
    const { arm_token } = await armResp.json();
    await apiFetch("/api/mode", postJson({ mode: "opr_tx", arm_token }));
    // Only close the dialog - and only report success - once the amp has
    // actually confirmed the mode change via send_command()'s OK/ERR wait.
    dialog?.close();
  } catch (err) {
    // Leave the dialog open: closing it here would tell the operator TX
    // fired when it didn't (e.g. the arm window expired, or the amp sent
    // ERR) - they need to see the failure and decide whether to retry.
    showActionError(`Failed to key OPR/TX: ${err.message || err}`);
  }
}

// =============================================================================
// Setup screen (amp/tuner model + Antenna Setup matrix)
// =============================================================================

function openSetupScreen() {
  document.getElementById("dashboard").hidden = true;
  document.getElementById("setup-screen").hidden = false;
  loadSetup();
}

function closeSetupScreen() {
  document.getElementById("setup-screen").hidden = true;
  document.getElementById("dashboard").hidden = false;
}

async function loadSetup() {
  const [configResp, antennaResp] = await Promise.all([
    apiFetch("/api/config").then((r) => r.json()),
    apiFetch("/api/antenna/config").then((r) => r.json()),
  ]);
  populateConfigForm(configResp);
  populateAntennaMatrix(antennaResp.antennas);
  populateSegmentDeleteBandOptions();
  renderAtuStatusPanel(appState.lastStatus);
  updateAntennaSetupVisibility();
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
  section.hidden = !tunerSelect.value;
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

  await apiFetch("/api/config", postJson({ amp_model: ampModel, tuner_model: tunerModel }));

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
}

boot();
