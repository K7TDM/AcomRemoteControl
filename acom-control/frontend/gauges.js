// Bargraph vs needle-meter widget rendering, theme-to-widget lookup,
// re-mount on theme switch. See PI_CONTROL_PLAN.md "UI Design - Themes"
// / "Component architecture".

const GAUGE_WIDGET_BY_THEME = {
  acom: "bargraph",
  classic: "needle",
  modern: "bargraph",
};

// Segmented (ACOM) vs. smooth-gradient (Modern) fill for the shared bargraph
// widget - see PI_CONTROL_PLAN.md's palette table ("gauge style" row).
const BARGRAPH_STYLE_BY_THEME = {
  acom: "segmented",
  classic: "segmented",
  modern: "smooth",
};

const BARGRAPH_SEGMENT_COUNT = 20;

// Shared good/warn/danger breakpoints - drive the needle meter's scale-zone
// arcs, the bargraph's "do not enter" red zone, and the digit color on every
// widget (incl. plain readouts), so a value's color always means the same
// thing regardless of which widget is rendering it. Matches the thresholds
// visible on the real amp's LCD (see e.g. Figure 4-4 in the 1200S manual,
// where PA Temperature's amber digit and Reflected Power's red digit line up
// with how far each bargraph is filled).
const ZONE_WARN_FRACTION = 0.6;
const ZONE_DANGER_FRACTION = 0.85;

function zoneForFraction(frac) {
  if (frac >= ZONE_DANGER_FRACTION) return "danger";
  if (frac >= ZONE_WARN_FRACTION) return "warn";
  return "good";
}

// Tracks which widget is currently mounted in each gauge slot so updateGauge()
// knows how to update it without a full re-render on every data tick.
const mountedWidgets = new WeakMap();

export function currentWidgetForTheme(themeStyle) {
  return GAUGE_WIDGET_BY_THEME[themeStyle] || "bargraph";
}

// Single source of truth for "which widget actually gets mounted", shared
// with app.js so its own mounted-widget bookkeeping (used for both deciding
// when to remount and as a CSS hook - see components.css) agrees with what
// mountGauge() below actually renders. "Plain" readouts (e.g. Input Power,
// PA DC Current on the real Basic Screen) skip the bargraph/track entirely
// and just show a bigger digit - except in Classic, which re-interprets
// every reading as its own VU-style dial regardless, so a plain request
// there is ignored.
export function resolveWidget(themeStyle, opts, forceWidget) {
  const baseWidget = forceWidget || currentWidgetForTheme(themeStyle);
  return opts.plain && baseWidget !== "needle" ? "plain" : baseWidget;
}

export function mountGauge(el, themeStyle, opts, forceWidget) {
  const widget = resolveWidget(themeStyle, opts, forceWidget);
  el.innerHTML = "";
  const labelEl = document.createElement("div");
  labelEl.className = "gauge-label";
  labelEl.textContent = opts.label;
  el.appendChild(labelEl);

  let bodyEl;
  if (widget === "needle") {
    bodyEl = renderNeedleGauge(opts);
  } else if (widget === "plain") {
    bodyEl = renderPlainGauge();
  } else {
    bodyEl = renderBargraphGauge(opts, themeStyle);
  }
  el.appendChild(bodyEl);

  const valueEl = document.createElement("div");
  valueEl.className = "gauge-value";
  valueEl.textContent = formatValue(opts.value, opts.unit);
  valueEl.dataset.zone = zoneForFraction(fraction(opts));
  el.appendChild(valueEl);

  mountedWidgets.set(el, { widget, opts, bodyEl, valueEl });
}

export function updateGauge(el, value, peak, avg) {
  const mounted = mountedWidgets.get(el);
  if (!mounted) return;
  mounted.opts.value = value;
  if (peak !== undefined) mounted.opts.peak = peak;
  if (avg !== undefined) mounted.opts.avg = avg;
  mounted.valueEl.textContent = formatValue(value, mounted.opts.unit);
  mounted.valueEl.dataset.zone = zoneForFraction(fraction(mounted.opts));
  if (mounted.widget === "needle") {
    updateNeedleGauge(mounted.bodyEl, mounted.opts);
  } else if (mounted.widget === "bargraph") {
    updateBargraphGauge(mounted.bodyEl, mounted.opts);
  }
}

function formatValue(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  const rounded = Math.round(value * 100) / 100;
  return `${rounded}${unit ? " " + unit : ""}`;
}

function fraction(opts) {
  const { value, min = 0, max = 100 } = opts;
  if (value === null || value === undefined || Number.isNaN(value)) return 0;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

// --- Bargraph (ACOM: segmented, Modern: smooth gradient) -------------------

function renderBargraphGauge(opts, themeStyle) {
  const wrapper = document.createElement("div");
  wrapper.className = "gauge-bargraph";
  wrapper.dataset.style = BARGRAPH_STYLE_BY_THEME[themeStyle] || "segmented";
  const track = document.createElement("div");
  track.className = "track";
  wrapper.appendChild(track);
  buildBargraphFill(track, opts, wrapper.dataset.style);
  wrapper.appendChild(buildTicks(opts));
  return wrapper;
}

function buildBargraphFill(track, opts, style) {
  track.innerHTML = "";
  const frac = fraction(opts);
  if (style === "smooth") {
    const fill = document.createElement("div");
    fill.className = "fill zone-" + zoneForFraction(frac);
    fill.style.width = `${frac * 100}%`;
    track.appendChild(fill);
  } else {
    const filledCount = Math.round(frac * BARGRAPH_SEGMENT_COUNT);
    for (let i = 0; i < BARGRAPH_SEGMENT_COUNT; i++) {
      const seg = document.createElement("div");
      seg.className = "segment";
      if (i < filledCount) seg.classList.add("filled");
      if (i / BARGRAPH_SEGMENT_COUNT >= ZONE_DANGER_FRACTION) seg.classList.add("danger-zone");
      track.appendChild(seg);
    }
  }
  appendAvgMarker(track, opts);
  appendPeakMarker(track, opts);
}

// Yellow mark at the average over the last transmission (forward power
// only - see TX_AVERAGE_IDS in app.js, the same value as the "Avg:" badge).
// Drawn before the peak marker so the red peak stays on top if they meet.
function appendAvgMarker(track, opts) {
  if (opts.avg === null || opts.avg === undefined || Number.isNaN(opts.avg)) return;
  const marker = document.createElement("div");
  marker.className = "avg-marker";
  marker.style.left = `${fraction({ ...opts, value: opts.avg }) * 100}%`;
  track.appendChild(marker);
}

// Thin red high-water mark at the gauge's current peak - a classic analog
// meter's peak-hold needle, left behind at the highest point the fill has
// reached. Reflects whatever app.js's updatePeak() already tracked for this
// gauge (same value the "Pk:" badge below the track shows), so it follows
// the exact same reset rules (grows-only, or resets on the next TX's rising
// edge - see PEAK_RESET_ON_TX_IDS in app.js) with no logic duplicated here.
function appendPeakMarker(track, opts) {
  if (opts.peak === null || opts.peak === undefined || Number.isNaN(opts.peak)) return;
  const peakFrac = fraction({ ...opts, value: opts.peak });
  const marker = document.createElement("div");
  marker.className = "peak-marker";
  marker.style.left = `${peakFrac * 100}%`;
  track.appendChild(marker);
}

// Three evenly-spaced numeric ticks (min / midpoint / max) under the track -
// the real Basic Screen prints scale numbers (e.g. "0 ... 600 ... 1200")
// below each bargraph; min/max come from the gauge's own range rather than
// hardcoded amp-model numbers, since this app's gauges rescale per amp model.
function buildTicks(opts) {
  const { min = 0, max = 100 } = opts;
  const row = document.createElement("div");
  row.className = "gauge-ticks";
  [min, min + (max - min) / 2, max].forEach((v) => {
    const span = document.createElement("span");
    span.textContent = Math.round(v);
    row.appendChild(span);
  });
  return row;
}

function updateBargraphGauge(bodyEl, opts) {
  const track = bodyEl.querySelector(".track");
  if (track) buildBargraphFill(track, opts, bodyEl.dataset.style);
  const ticksEl = bodyEl.querySelector(".gauge-ticks");
  if (ticksEl) ticksEl.replaceWith(buildTicks(opts));
}

// --- Plain readout (no track - e.g. Input Power / PA DC Current on the real
// Basic Screen, which show as bare digits with no bargraph) ----------------

function renderPlainGauge() {
  const el = document.createElement("div");
  el.className = "gauge-plain";
  return el;
}

// --- Window meter (Classic) --------------------------------------------------
// A rectangular, backlit edge meter like 50s-70s station gear (Heath SB,
// Hallicrafters, FT-101): the scale is an arc whose pivot sits *below* the
// window, so only the swept part of the needle shows. Colors all come from
// --meter-* CSS variables (classic.css), so light/dark mode restyles a
// mounted meter without re-rendering it.
//
// The needle, peak and average marks are drawn pointing straight up at the
// pivot and rotated by a CSS transform about that one pivot (transform-box:
// view-box; transform-origin in viewBox units). The previous dial combined
// an SVG rotate(angle cx cy) with a CSS transform-origin, which applied two
// pivots and could swing the needle off the visible face.

const METER_W = 320;
const METER_H = 156;
const METER_SWEEP_DEG = 48; // each side of vertical
const METER_R = 198;
const METER_CX = METER_W / 2;
const METER_CY = 22 + 0.97 * METER_R; // pivot, below the window

// Printed on the meter face like the legend on a real panel meter.
const METER_UNIT_WORDS = { W: "WATTS", A: "AMPS", "°C": "°C", ":1": "SWR" };

let meterInstanceCounter = 0;

function meterPolar(r, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180;
  const round = (v) => Math.round(v * 10) / 10;
  return [round(METER_CX + r * Math.sin(rad)), round(METER_CY - r * Math.cos(rad))];
}

function meterArc(r, a1, a2) {
  const [x1, y1] = meterPolar(r, a1);
  const [x2, y2] = meterPolar(r, a2);
  return `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`;
}

function meterAngle(opts, value) {
  const frac = fraction({ ...opts, value });
  return -METER_SWEEP_DEG + frac * 2 * METER_SWEEP_DEG;
}

function formatScaleNumber(v) {
  return Math.abs(v) >= 10 || Number.isInteger(v) ? String(Math.round(v)) : String(Math.round(v * 10) / 10);
}

function renderNeedleGauge(opts) {
  const wrapper = document.createElement("div");
  wrapper.className = "gauge-needle";
  wrapper.innerHTML = buildMeterSvg(opts);
  updateNeedleGauge(wrapper, opts);
  return wrapper;
}

function buildMeterSvg(opts) {
  const id = meterInstanceCounter++;
  const clipId = `meter-clip-${id}`;
  const glowId = `meter-glow-${id}`;
  const { min = 0, max = 100 } = opts;
  const danger = ZONE_DANGER_FRACTION;

  let scale = `<path class="meter-scale" d="${meterArc(METER_R * 0.82, -METER_SWEEP_DEG, METER_SWEEP_DEG)}" />`;
  scale += `<path class="meter-redline" d="${meterArc(METER_R * 0.86, -METER_SWEEP_DEG + danger * 2 * METER_SWEEP_DEG, METER_SWEEP_DEG)}" />`;
  for (let k = 0; k <= 20; k++) {
    const angle = -METER_SWEEP_DEG + (k / 20) * 2 * METER_SWEEP_DEG;
    const major = k % 5 === 0;
    const [x1, y1] = meterPolar(METER_R * 0.82, angle);
    const [x2, y2] = meterPolar(METER_R * (major ? 0.92 : 0.88), angle);
    scale += `<line class="meter-tick${major ? " major" : ""}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" />`;
    if (major) {
      const [tx, ty] = meterPolar(METER_R * 0.97, angle);
      scale += `<text class="meter-number" x="${tx}" y="${ty}">${formatScaleNumber(min + (k / 20) * (max - min))}</text>`;
    }
  }

  // Each pointer is a vertical line from the pivot; data-angle-of says which
  // value it tracks, updateNeedleGauge() rotates it.
  const pointer = (cls, from, to) =>
    `<line class="${cls}" x1="${METER_CX}" y1="${METER_CY - METER_R * from}" x2="${METER_CX}" y2="${METER_CY - METER_R * to}"` +
    ` style="transform-origin: ${METER_CX}px ${METER_CY}px" />`;

  return `
    <svg viewBox="0 0 ${METER_W} ${METER_H}" role="img" aria-label="${opts.label || "Meter"}">
      <defs>
        <clipPath id="${clipId}"><rect x="0" y="0" width="${METER_W}" height="${METER_H}" /></clipPath>
        <radialGradient id="${glowId}" cx="50%" cy="62%" r="70%">
          <stop offset="0%" class="meter-glow-hot" />
          <stop offset="55%" class="meter-glow-warm" />
          <stop offset="100%" class="meter-glow-edge" />
        </radialGradient>
      </defs>
      <g clip-path="url(#${clipId})">
        <rect class="meter-face" x="0" y="0" width="${METER_W}" height="${METER_H}" style="fill:url(#${glowId})" />
        ${scale}
        ${pointer("meter-avg", 0.72, 0.9)}
        ${pointer("meter-peak", 0.5, 0.93)}
        ${pointer("meter-needle", 0, 0.94)}
        <text class="meter-unit" x="10" y="${METER_H - 10}">${METER_UNIT_WORDS[opts.unit] ?? opts.unit ?? ""}</text>
        <text class="meter-value" x="${METER_W - 10}" y="${METER_H - 10}" data-meter-value></text>
        <polygon class="meter-glass" points="${METER_W * 0.06},0 ${METER_W * 0.4},0 ${METER_W * 0.2},${METER_H} ${-METER_W * 0.14},${METER_H}" />
      </g>
      <rect class="meter-bezel" x="0" y="0" width="${METER_W}" height="${METER_H}" />
    </svg>`;
}

function setPointer(el, opts, value) {
  if (!el) return;
  const show = value !== null && value !== undefined && !Number.isNaN(value);
  el.style.visibility = show ? "visible" : "hidden";
  if (show) el.style.transform = `rotate(${meterAngle(opts, value)}deg)`;
}

function updateNeedleGauge(bodyEl, opts) {
  setPointer(bodyEl.querySelector(".meter-needle"), opts, opts.value ?? opts.min ?? 0);
  setPointer(bodyEl.querySelector(".meter-peak"), opts, opts.peak);
  setPointer(bodyEl.querySelector(".meter-avg"), opts, opts.avg);
  const valueEl = bodyEl.querySelector("[data-meter-value]");
  // The unit is already printed on the face (METER_UNIT_WORDS), so just the number.
  if (valueEl) valueEl.textContent = formatValue(opts.value, "");
}

