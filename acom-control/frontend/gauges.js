// Bargraph vs needle-meter widget rendering, theme-to-widget lookup,
// re-mount on theme switch. See PI_CONTROL_PLAN.md "UI Design - Themes"
// / "Component architecture".

const GAUGE_WIDGET_BY_THEME = {
  acom: "bargraph",
  classic: "needle",
  modern: "bargraph",
};

const BARGRAPH_SEGMENT_COUNT = 20;
const DANGER_ZONE_FRACTION = 0.9; // last 10% of the scale is the "do not enter" zone

// Tracks which widget is currently mounted in each gauge slot so updateGauge()
// knows how to update it without a full re-render on every data tick.
const mountedWidgets = new WeakMap();

export function currentWidgetForTheme(themeStyle) {
  return GAUGE_WIDGET_BY_THEME[themeStyle] || "bargraph";
}

export function mountGauge(el, themeStyle, opts) {
  const widget = currentWidgetForTheme(themeStyle);
  el.innerHTML = "";
  const labelEl = document.createElement("div");
  labelEl.className = "gauge-label";
  labelEl.textContent = opts.label;
  el.appendChild(labelEl);

  let bodyEl;
  if (widget === "needle") {
    bodyEl = renderNeedleGauge(opts);
  } else {
    bodyEl = renderBargraphGauge(opts);
  }
  el.appendChild(bodyEl);

  const valueEl = document.createElement("div");
  valueEl.className = "gauge-value";
  valueEl.textContent = formatValue(opts.value, opts.unit);
  el.appendChild(valueEl);

  mountedWidgets.set(el, { widget, opts, bodyEl, valueEl });
}

export function updateGauge(el, value) {
  const mounted = mountedWidgets.get(el);
  if (!mounted) return;
  mounted.opts.value = value;
  mounted.valueEl.textContent = formatValue(value, mounted.opts.unit);
  if (mounted.widget === "needle") {
    updateNeedleGauge(mounted.bodyEl, mounted.opts);
  } else {
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

function renderBargraphGauge(opts) {
  const wrapper = document.createElement("div");
  wrapper.className = "gauge-bargraph";
  const track = document.createElement("div");
  track.className = "track";
  wrapper.appendChild(track);
  buildBargraphSegments(track, opts);
  return wrapper;
}

function buildBargraphSegments(track, opts) {
  track.innerHTML = "";
  const frac = fraction(opts);
  const filledCount = Math.round(frac * BARGRAPH_SEGMENT_COUNT);
  for (let i = 0; i < BARGRAPH_SEGMENT_COUNT; i++) {
    const seg = document.createElement("div");
    seg.className = "segment";
    if (i < filledCount) seg.classList.add("filled");
    if (i / BARGRAPH_SEGMENT_COUNT >= DANGER_ZONE_FRACTION) seg.classList.add("danger-zone");
    track.appendChild(seg);
  }
}

function updateBargraphGauge(bodyEl, opts) {
  const track = bodyEl.querySelector(".track");
  if (track) buildBargraphSegments(track, opts);
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

function buildNeedleSvg(opts) {
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
  return `
    <svg viewBox="0 0 100 60">
      <path class="needle-face" d="M 8 55 A 42 42 0 0 1 92 55 L 50 55 Z" />
      ${ticks.join("\n")}
      <line class="needle-pointer" data-needle-pointer x1="50" y1="55" x2="50" y2="18"
            transform="rotate(${needleAngle(opts)} 50 55)" />
      <circle class="needle-hub" cx="50" cy="55" r="3" />
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
