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

export function updateGauge(el, value, peak) {
  const mounted = mountedWidgets.get(el);
  if (!mounted) return;
  mounted.opts.value = value;
  if (peak !== undefined) mounted.opts.peak = peak;
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
  appendPeakMarker(track, opts);
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

// --- Needle meter (Classic) --------------------------------------------------

const NEEDLE_SWEEP_START_DEG = -60;
const NEEDLE_SWEEP_END_DEG = 60;

function renderNeedleGauge(opts) {
  const wrapper = document.createElement("div");
  wrapper.className = "gauge-needle";
  wrapper.innerHTML = buildNeedleSvg(opts);
  return wrapper;
}

// Classic theme only (bargraph is used by the other two themes), so this
// SVG can carry rich skeuomorphic styling - bezel, dial-face gradient,
// glass highlight, colored scale zones - without any [data-theme="..."]
// guarding. Multiple needle gauges are mounted on the page at once, so
// every <defs> id below gets a unique per-instance suffix to avoid SVG id
// collisions between gauges.
let needleInstanceCounter = 0;

function needlePolar(cx, cy, r, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180;
  return { x: cx + r * Math.sin(rad), y: cy - r * Math.cos(rad) };
}

function buildNeedleSvg(opts) {
  const instanceId = needleInstanceCounter++;
  const bezelGradId = `needle-bezel-grad-${instanceId}`;
  const faceGradId = `needle-face-grad-${instanceId}`;
  const glassGradId = `needle-glass-grad-${instanceId}`;

  const ticks = [];
  const tickCount = 8;
  for (let i = 0; i <= tickCount; i++) {
    const angle = NEEDLE_SWEEP_START_DEG + (i / tickCount) * (NEEDLE_SWEEP_END_DEG - NEEDLE_SWEEP_START_DEG);
    const rad = (angle * Math.PI) / 180;
    const x1 = 50 + 38 * Math.sin(rad);
    const y1 = 50 - 38 * Math.cos(rad);
    const x2 = 50 + 44 * Math.sin(rad);
    const y2 = 50 - 44 * Math.cos(rad);
    ticks.push(`<line class="needle-tick" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" />`);
  }

  // Green/yellow/red scale-zone arcs just inside the ticks, for the
  // classic VU-meter look.
  const zoneArc = (fromFrac, toFrac, colorVar) => {
    const a1 = NEEDLE_SWEEP_START_DEG + fromFrac * (NEEDLE_SWEEP_END_DEG - NEEDLE_SWEEP_START_DEG);
    const a2 = NEEDLE_SWEEP_START_DEG + toFrac * (NEEDLE_SWEEP_END_DEG - NEEDLE_SWEEP_START_DEG);
    const p1 = needlePolar(50, 50, 36, a1);
    const p2 = needlePolar(50, 50, 36, a2);
    return `<path class="needle-zone" d="M ${p1.x} ${p1.y} A 36 36 0 0 1 ${p2.x} ${p2.y}" style="stroke:${colorVar}" />`;
  };
  const zones = [
    zoneArc(0, 0.6, "var(--needle-zone-good, #5a8f3c)"),
    zoneArc(0.6, 0.85, "var(--needle-zone-warn, #d9a53c)"),
    zoneArc(0.85, 1, "var(--needle-zone-danger, #a3331a)"),
  ].join("\n");

  return `
    <svg viewBox="0 0 100 60">
      <defs>
        <radialGradient id="${bezelGradId}" cx="50%" cy="30%" r="75%">
          <stop offset="0%" stop-color="var(--needle-bezel-highlight, #d8d8d8)" />
          <stop offset="55%" stop-color="var(--needle-bezel-mid, #8a8a7a)" />
          <stop offset="100%" stop-color="var(--needle-bezel-shadow, #2a2a22)" />
        </radialGradient>
        <radialGradient id="${faceGradId}" cx="50%" cy="25%" r="80%">
          <stop offset="0%" stop-color="var(--needle-face-highlight, var(--needle-face))" />
          <stop offset="100%" stop-color="var(--needle-face)" />
        </radialGradient>
        <radialGradient id="${glassGradId}" cx="40%" cy="15%" r="55%">
          <stop offset="0%" stop-color="rgba(255,255,255,0.35)" />
          <stop offset="100%" stop-color="rgba(255,255,255,0)" />
        </radialGradient>
      </defs>
      <path class="needle-bezel" d="M 3 55 A 47 47 0 0 1 97 55 L 50 55 Z" style="fill:url(#${bezelGradId})" />
      <path class="needle-face" d="M 8 55 A 42 42 0 0 1 92 55 L 50 55 Z" style="fill:url(#${faceGradId})" />
      ${zones}
      ${ticks.join("\n")}
      <line class="needle-pointer" data-needle-pointer x1="50" y1="55" x2="50" y2="18"
            transform="rotate(${needleAngle(opts)} 50 55)" />
      <circle class="needle-hub" cx="50" cy="55" r="3" />
      <ellipse class="needle-glass" cx="38" cy="28" rx="20" ry="12" style="fill:url(#${glassGradId})" />
    </svg>`;
}

function needleAngle(opts) {
  const frac = fraction(opts);
  return NEEDLE_SWEEP_START_DEG + frac * (NEEDLE_SWEEP_END_DEG - NEEDLE_SWEEP_START_DEG);
}

function updateNeedleGauge(bodyEl, opts) {
  const pointer = bodyEl.querySelector("[data-needle-pointer]");
  if (pointer) {
    pointer.setAttribute("transform", `rotate(${needleAngle(opts)} 50 55)`);
  }
}
