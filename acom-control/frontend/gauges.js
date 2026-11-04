// Bargraph vs needle-meter widget rendering, theme-to-widget lookup,
// re-mount on theme switch. See PI_CONTROL_PLAN.md "UI Design - Themes"
// / "Component architecture".

// Modern's Forward Power stays a bargraph "tape" (app.js forces the hero's
// widget outside Classic); its other gauges are arc dials.
const GAUGE_WIDGET_BY_THEME = {
  acom: "bargraph",
  classic: "needle",
  modern: "arc",
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
// A gauge can instead set its own breakpoints as values (opts.warnFrom /
// opts.dangerFrom) - see zoneFractions().
const ZONE_WARN_FRACTION = 0.6;
const ZONE_DANGER_FRACTION = 0.85;

// Non-linear scales, picked by opts.scale (default: linear min..max).
const SCALES = {
  // SWR meters - cross-needle meters, SWR bridges, the meter on most rigs -
  // are scaled by reflection coefficient, position = (SWR-1)/(SWR+1),
  // because that's what the bridge actually measures. 1:1 sits at the left
  // end, 3:1 in the middle and infinity at the right end, with the high
  // values crowding together toward it. Not log, but it gives the same
  // "spread out at the low end, squeezed at the top" look.
  swr: {
    toFraction: (v) => (v === Infinity ? 1 : (v - 1) / (v + 1)),
    labels: [1, 1.5, 2, 3, 5, Infinity],
    minorTicks: [1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 1.8, 1.9, 2.5, 4, 10],
    // Bargraph's three numbers under the track (min / middle / max).
    barTicks: [1, 3, Infinity],
  },
};

function zoneFractions(opts = {}) {
  return {
    warn: opts.warnFrom != null ? valueFraction(opts, opts.warnFrom) : ZONE_WARN_FRACTION,
    danger: opts.dangerFrom != null ? valueFraction(opts, opts.dangerFrom) : ZONE_DANGER_FRACTION,
  };
}

function zoneForFraction(frac, opts) {
  const zones = zoneFractions(opts);
  if (frac >= zones.danger) return "danger";
  if (frac >= zones.warn) return "warn";
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
  } else if (widget === "arc") {
    bodyEl = renderArcGauge(opts);
  } else if (widget === "plain") {
    bodyEl = renderPlainGauge();
  } else {
    bodyEl = renderBargraphGauge(opts, themeStyle);
  }
  el.appendChild(bodyEl);

  const valueEl = document.createElement("div");
  valueEl.className = "gauge-value";
  valueEl.textContent = formatValue(opts.value, opts.unit);
  valueEl.dataset.zone = zoneForFraction(fraction(opts), opts);
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
  mounted.valueEl.dataset.zone = zoneForFraction(fraction(mounted.opts), mounted.opts);
  if (mounted.widget === "needle") {
    updateNeedleGauge(mounted.bodyEl, mounted.opts);
  } else if (mounted.widget === "arc") {
    updateArcGauge(mounted.bodyEl, mounted.opts);
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
  return valueFraction(opts, opts.value);
}

// Where `value` sits along this gauge's scale, 0..1.
function valueFraction(opts, value) {
  const { min = 0, max = 100 } = opts;
  if (value === null || value === undefined || Number.isNaN(value)) return 0;
  const scale = SCALES[opts.scale];
  const frac = scale ? scale.toFraction(value) : (value - min) / (max - min);
  return Math.min(1, Math.max(0, frac));
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
    fill.className = "fill zone-" + zoneForFraction(frac, opts);
    fill.style.width = `${frac * 100}%`;
    // Lets a theme size a gradient to the whole scale rather than to the
    // fill, so the fill's color reflects how far up the scale it reaches
    // (Modern's green-amber-red tape - see themes/modern.css).
    fill.style.setProperty("--fill-scale", frac > 0 ? `${Math.round(10000 / frac) / 100}%` : "100%");
    track.appendChild(fill);
  } else {
    const filledCount = Math.round(frac * BARGRAPH_SEGMENT_COUNT);
    const dangerFrac = zoneFractions(opts).danger;
    for (let i = 0; i < BARGRAPH_SEGMENT_COUNT; i++) {
      const seg = document.createElement("div");
      seg.className = "segment";
      if (i < filledCount) seg.classList.add("filled");
      if (i / BARGRAPH_SEGMENT_COUNT >= dangerFrac - 1e-9) seg.classList.add("danger-zone");
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
  const values = SCALES[opts.scale]?.barTicks || [min, min + (max - min) / 2, max].map(Math.round);
  values.forEach((v) => {
    const span = document.createElement("span");
    span.textContent = formatScaleNumber(v);
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

// --- Arc dial (Modern: "glass cockpit") ---------------------------------------
// A 240-degree arc like an avionics engine gauge, with the reading in a boxed
// window at its center and a magenta peak bug on the arc. Colors are the
// --arc-* variables in themes/modern.css. The filled part of the arc is one
// path with pathLength=100, so the fill is just a stroke-dasharray; the peak
// bug is drawn at 12 o'clock and rotated about the arc's center.

const ARC_W = 200;
const ARC_H = 150;
const ARC_CX = 100;
const ARC_CY = 86;
const ARC_R = 64;
const ARC_SWEEP = 120; // degrees each side of 12 o'clock

function arcPoint(r, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180;
  const round = (v) => Math.round(v * 10) / 10;
  return [round(ARC_CX + r * Math.sin(rad)), round(ARC_CY - r * Math.cos(rad))];
}

function renderArcGauge(opts) {
  const { min = 0, max = 100 } = opts;
  const [x1, y1] = arcPoint(ARC_R, -ARC_SWEEP);
  const [x2, y2] = arcPoint(ARC_R, ARC_SWEEP);
  const d = `M ${x1} ${y1} A ${ARC_R} ${ARC_R} 0 1 1 ${x2} ${y2}`;
  // Red arc just outside the scale from the danger point to the top end -
  // an engine gauge's red range, matching ACOM's red segments and
  // Classic's redline.
  const redR = ARC_R + 9;
  const redFrom = -ARC_SWEEP + zoneFractions(opts).danger * 2 * ARC_SWEEP;
  const [rx1, ry1] = arcPoint(redR, redFrom);
  const [rx2, ry2] = arcPoint(redR, ARC_SWEEP);
  const redD = `M ${rx1} ${ry1} A ${redR} ${redR} 0 ${ARC_SWEEP - redFrom > 180 ? 1 : 0} 1 ${rx2} ${ry2}`;
  const unit = METER_UNIT_WORDS[opts.unit] ?? opts.unit ?? "";
  const wrapper = document.createElement("div");
  wrapper.className = "gauge-arc";
  wrapper.innerHTML = `
    <svg viewBox="0 0 ${ARC_W} ${ARC_H}" role="img" aria-label="${opts.label || "Gauge"}" style="--meter-ratio: ${Math.round((ARC_W / ARC_H) * 1000) / 1000}">
      <path class="arc-track" d="${d}" />
      <path class="arc-redline" d="${redD}" />
      <path class="arc-fill" d="${d}" pathLength="100" data-arc-fill />
      <line class="arc-peak" x1="${ARC_CX}" y1="${ARC_CY - ARC_R - 9}" x2="${ARC_CX}" y2="${ARC_CY - ARC_R + 9}"
            style="transform-origin: ${ARC_CX}px ${ARC_CY}px" data-arc-peak />
      <rect class="arc-window" x="${ARC_CX - 40}" y="${ARC_CY - 19}" width="80" height="32" rx="3" />
      <text class="arc-value" x="${ARC_CX}" y="${ARC_CY + 5}" data-arc-value></text>
      <text class="arc-unit" x="${ARC_CX}" y="${ARC_CY + 34}">${unit}</text>
      <text class="arc-end" x="${x1 - 2}" y="${y1 + 20}">${formatScaleNumber(min)}</text>
      <text class="arc-end" x="${x2 + 2}" y="${y2 + 20}">${formatScaleNumber(max)}</text>
    </svg>`;
  updateArcGauge(wrapper, opts);
  return wrapper;
}

function updateArcGauge(bodyEl, opts) {
  const frac = fraction(opts);
  const fill = bodyEl.querySelector("[data-arc-fill]");
  if (fill) {
    fill.style.strokeDasharray = `${Math.max(frac * 100, 0.5)} 100`;
    fill.dataset.zone = zoneForFraction(frac, opts);
  }
  const peak = bodyEl.querySelector("[data-arc-peak]");
  if (peak) {
    const show = opts.peak !== null && opts.peak !== undefined && !Number.isNaN(opts.peak);
    peak.style.visibility = show ? "visible" : "hidden";
    if (show) peak.style.transform = `rotate(${-ARC_SWEEP + fraction({ ...opts, value: opts.peak }) * 2 * ARC_SWEEP}deg)`;
  }
  const valueEl = bodyEl.querySelector("[data-arc-value]");
  if (valueEl) valueEl.textContent = formatValue(opts.value, "");
}

// --- Window meter (Classic) --------------------------------------------------
// A rectangular, backlit edge meter like 50s-70s station gear (Heath SB,
// Hallicrafters, FT-101): the scale is an arc whose pivot sits *below* the
// window, so only the swept part of the needle shows. Colors all come from
// --meter-* CSS variables (classic.css), so light/dark mode restyles a
// mounted meter without re-rendering it.
//
// Two shapes (opts.wide picks): the standard meter for half/quarter-width
// panels, and a wide, short edgewise meter for full-width panels - styled
// like the band dial - whose pivot is so far below the window that the
// needle sweeps nearly flat across it (per Tyler: the standard meter was
// too big at full width).
//
// The needle, peak and average marks are drawn pointing straight up at the
// pivot and rotated by a CSS transform about that one pivot (transform-box:
// view-box; transform-origin in viewBox units). The previous dial combined
// an SVG rotate(angle cx cy) with a CSS transform-origin, which applied two
// pivots and could swing the needle off the visible face.
//
// Geometry: W/H = window; cy = pivot (below the window); sweep = degrees
// each side of vertical; scale = scale-arc radius; the rest are radial
// offsets from the scale arc, in viewBox units. unitY = baseline of the
// unit word and reading (bottom corners on the standard meter, top corners
// on the wide one, where the bottom corners hold the scale's ends).
// numberSize/unitSize/valueSize are font sizes in viewBox units - the SVG
// scales down with its panel, so these are sized for the Pi's 800x480
// touchscreen, where a half-width meter is drawn ~170px wide (per Tyler:
// the earlier 12-unit numbers were unreadable there).
const METER_GEOMETRY = {
  standard: {
    W: 320, H: 156, cy: 214, sweep: 48, scale: 162,
    red: 8, minor: 12, major: 20, number: 30, needle: 24, peakFrom: -64, avgFrom: -20, avgTo: 14,
    unitY: 148, numberSize: 27, unitSize: 18, valueSize: 34, endNumberInset: 5,
  },
  wide: {
    W: 600, H: 120, cy: 818, sweep: 20, scale: 762,
    red: 7, minor: 9, major: 16, number: 28, needle: 20, peakFrom: -40, avgFrom: -16, avgTo: 12,
    unitY: 32, numberSize: 24, unitSize: 18, valueSize: 32, endNumberInset: 0,
  },
};

// Printed on the meter face like the legend on a real panel meter.
const METER_UNIT_WORDS = { W: "WATTS", A: "AMPS", "°C": "°C", ":1": "SWR" };

let meterInstanceCounter = 0;

function meterPolar(g, r, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180;
  const round = (v) => Math.round(v * 10) / 10;
  return [round(g.W / 2 + r * Math.sin(rad)), round(g.cy - r * Math.cos(rad))];
}

function meterArc(g, r, a1, a2) {
  const [x1, y1] = meterPolar(g, r, a1);
  const [x2, y2] = meterPolar(g, r, a2);
  return `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`;
}

function formatScaleNumber(v) {
  if (v === Infinity) return "∞";
  return Math.abs(v) >= 10 || Number.isInteger(v) ? String(Math.round(v)) : String(Math.round(v * 10) / 10);
}

function renderNeedleGauge(opts) {
  const wrapper = document.createElement("div");
  wrapper.className = "gauge-needle" + (opts.wide ? " wide" : "");
  wrapper.innerHTML = buildMeterSvg(opts, METER_GEOMETRY[opts.wide ? "wide" : "standard"]);
  updateNeedleGauge(wrapper, opts);
  return wrapper;
}

function buildMeterSvg(opts, g) {
  const id = meterInstanceCounter++;
  const clipId = `meter-clip-${id}`;
  const glowId = `meter-glow-${id}`;
  const { min = 0, max = 100 } = opts;
  const cx = g.W / 2;
  const angleAt = (frac) => -g.sweep + frac * 2 * g.sweep;

  let scale = `<path class="meter-scale" d="${meterArc(g, g.scale, -g.sweep, g.sweep)}" />`;
  scale += `<path class="meter-redline" d="${meterArc(g, g.scale + g.red, angleAt(zoneFractions(opts).danger), g.sweep)}" />`;
  // Linear: 20 even steps, every 5th numbered. A SCALES entry lists its
  // own numbered and minor tick values instead.
  const scaleDef = SCALES[opts.scale];
  const ticks = scaleDef
    ? [
        ...scaleDef.labels.map((v) => ({ frac: valueFraction(opts, v), label: v })),
        ...scaleDef.minorTicks.map((v) => ({ frac: valueFraction(opts, v) })),
      ]
    : Array.from({ length: 21 }, (_, k) => ({ frac: k / 20, label: k % 5 === 0 ? min + (k / 20) * (max - min) : undefined }));
  for (const { frac, label } of ticks) {
    const angle = angleAt(frac);
    const major = label !== undefined;
    const [x1, y1] = meterPolar(g, g.scale, angle);
    const [x2, y2] = meterPolar(g, g.scale + (major ? g.major : g.minor), angle);
    scale += `<line class="meter-tick${major ? " major" : ""}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" />`;
    if (major) {
      // The end numbers sit near the window's side edges; anchor them
      // inward so they can't run off it, and (standard meter) nudge them
      // along the scale, up off the redline.
      const inset = frac === 0 ? g.endNumberInset : frac === 1 ? -g.endNumberInset : 0;
      const [tx, ty] = meterPolar(g, g.scale + g.number, angle + inset);
      const anchor = frac === 0 ? "start" : frac === 1 ? "end" : "middle";
      scale +=
        `<text class="meter-number" x="${tx}" y="${ty}" style="font-size:${g.numberSize}px; text-anchor:${anchor}">` +
        `${formatScaleNumber(label)}</text>`;
    }
  }

  // Each pointer is a vertical line up from the pivot; updateNeedleGauge()
  // rotates it to its value.
  const pointer = (cls, from, to) =>
    `<line class="${cls}" x1="${cx}" y1="${g.cy - from}" x2="${cx}" y2="${g.cy - to}"` +
    ` style="transform-origin: ${cx}px ${g.cy}px" />`;
  const unitWord = METER_UNIT_WORDS[opts.unit] ?? opts.unit ?? "";

  return `
    <svg viewBox="0 0 ${g.W} ${g.H}" role="img" aria-label="${opts.label || "Meter"}" data-sweep="${g.sweep}"
         style="--meter-ratio: ${Math.round((g.W / g.H) * 1000) / 1000}">
      <defs>
        <clipPath id="${clipId}"><rect x="0" y="0" width="${g.W}" height="${g.H}" /></clipPath>
        <radialGradient id="${glowId}" cx="50%" cy="62%" r="70%">
          <stop offset="0%" class="meter-glow-hot" />
          <stop offset="55%" class="meter-glow-warm" />
          <stop offset="100%" class="meter-glow-edge" />
        </radialGradient>
      </defs>
      <g clip-path="url(#${clipId})">
        <rect class="meter-face" x="0" y="0" width="${g.W}" height="${g.H}" style="fill:url(#${glowId})" />
        ${scale}
        ${pointer("meter-avg", g.scale + g.avgFrom, g.scale + g.avgTo)}
        ${pointer("meter-peak", g.scale + g.peakFrom, g.scale + g.needle - 2)}
        ${pointer("meter-needle", 0, g.scale + g.needle)}
        <text class="meter-unit" x="10" y="${g.unitY}" style="font-size:${g.unitSize}px">${unitWord}</text>
        <text class="meter-value" x="${g.W - 10}" y="${g.unitY}" style="font-size:${g.valueSize}px" data-meter-value></text>
        <polygon class="meter-glass" points="${g.W * 0.06},0 ${g.W * 0.4},0 ${g.W * 0.2},${g.H} ${-g.W * 0.14},${g.H}" />
      </g>
      <rect class="meter-bezel" x="0" y="0" width="${g.W}" height="${g.H}" />
    </svg>`;
}

function setPointer(el, opts, value, sweep) {
  if (!el) return;
  const show = value !== null && value !== undefined && !Number.isNaN(value);
  el.style.visibility = show ? "visible" : "hidden";
  if (show) el.style.transform = `rotate(${-sweep + fraction({ ...opts, value }) * 2 * sweep}deg)`;
}

function updateNeedleGauge(bodyEl, opts) {
  const sweep = Number(bodyEl.querySelector("svg")?.dataset.sweep) || METER_GEOMETRY.standard.sweep;
  setPointer(bodyEl.querySelector(".meter-needle"), opts, opts.value ?? opts.min ?? 0, sweep);
  setPointer(bodyEl.querySelector(".meter-peak"), opts, opts.peak, sweep);
  setPointer(bodyEl.querySelector(".meter-avg"), opts, opts.avg, sweep);
  const valueEl = bodyEl.querySelector("[data-meter-value]");
  // The unit is already printed on the face (METER_UNIT_WORDS), so just the number.
  if (valueEl) valueEl.textContent = formatValue(opts.value, "");
}

