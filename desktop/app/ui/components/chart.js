// components/chart.js: the dashboard's time-series chart (package/core power, hottest sensor, clock, Vcore): y axis with round ticks and a minimum span,
// time axis from real sample timestamps (a pause in the stream is a gap, never an interpolated line), hover readout, honest "n/a + reason" when a series
// has no readings. Port of web/app.js makeChart() (:721-758), MIN_SPAN (:760-761) and the readout painting around it; the scaling maths is pure and
// exported for `node --test` (niceTicks, padSpan, dataRange, chartPaths, fmtSpan, nearestIndex).
import { h, svg } from "./dom.js";
import { fmtNum, NA } from "./fmt.js";
import { installStyles } from "./spark.js";

export const CHART_W = 600;
export const CHART_H = 150;

/** Minimum y-spans (web/app.js:761): one degree or one watt of noise must not draw as a cliff. A narrower range is widened around its middle. */
export const MIN_SPAN = { temp: 10, power: 20, mhz: 500, volt: 0.05, gbs: 2 };

/** Round a value to a "nice" 1/2/5 x 10^n number (Heckbert); `round` picks the nearest instead of the next one up. */
export function niceNum(range, round) {
  if (!(range > 0) || !Number.isFinite(range)) return 1;
  const exp = Math.floor(Math.log10(range));
  const f = range / 10 ** exp;
  let nf;
  if (round) nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
  else nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return nf * 10 ** exp;
}

/** Widen [lo, hi] around its middle to at least `minSpan`. */
export function padSpan(lo, hi, minSpan = 0) {
  if (hi - lo >= minSpan) return [lo, hi];
  const mid = (lo + hi) / 2;
  return [mid - minSpan / 2, mid + minSpan / 2];
}

/** {lo, hi, step, ticks} with round tick values covering [lo, hi]; about `count` intervals. */
export function niceTicks(lo, hi, count = 4) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { lo: 0, hi: 1, step: 1, ticks: [0, 1] };
  if (hi < lo) [lo, hi] = [hi, lo];
  if (hi - lo < 1e-12) {
    lo -= 0.5;
    hi += 0.5;
  }
  const step = niceNum(niceNum(hi - lo, false) / count, true);
  const nlo = Math.floor(lo / step + 1e-9) * step;
  const nhi = Math.ceil(hi / step - 1e-9) * step;
  const dec = Math.max(0, 1 - Math.floor(Math.log10(step))); // decimals of the step: keeps 1.2 from becoming 1.2000000000000002
  const first = Math.round(nlo / step);
  const last = Math.round(nhi / step);
  const ticks = [];
  for (let i = first; i <= last; i++) ticks.push(Number((i * step).toFixed(dec)));
  return { lo: ticks[0], hi: ticks[ticks.length - 1], step, ticks };
}

/** Min/max over arrays of numbers-or-null, widened to `minSpan`; null when there is no number at all. */
export function dataRange(arrays, minSpan = 0) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const a of arrays) {
    for (const v of a) {
      if (v === null || v === undefined || !Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (lo === Infinity) return null;
  return padSpan(lo, hi, minSpan);
}

/**
 * SVG path data for `points` [{t, v}] (v null = no reading). x is time inside [t0, t1], y is value inside [lo, hi] flipped into 0..h. A null value or a
 * time jump larger than `gapS` ends the segment, so a stalled stream is drawn as a hole.
 */
export function chartPaths(points, { t0, t1, lo, hi, w = CHART_W, h: height = CHART_H, gapS = Infinity }) {
  const span = t1 - t0 || 1;
  const rng = hi - lo || 1;
  const x = (t) => ((t - t0) / span) * w;
  const y = (v) => height - ((v - lo) / rng) * height;
  const segs = [];
  let cur = [];
  let prevT = null;
  for (const p of points) {
    const bad = p.v === null || p.v === undefined || !Number.isFinite(p.v);
    if (bad || (prevT !== null && p.t - prevT > gapS)) {
      if (cur.length) segs.push(cur);
      cur = [];
    }
    if (!bad) cur.push(`${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`);
    prevT = p.t;
  }
  if (cur.length) segs.push(cur);
  return segs.map((s) => (s.length === 1 ? `M${s[0]} L${s[0]}` : "M" + s.join(" L"))).join(" ");
}

/** "30 s" / "1 min" / "15 min" / "2 h": the label of a window length given in seconds. */
export function fmtSpan(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.round(s / 3600)} h`;
}

/** Index of the entry of `ts` (ascending) nearest to `t`; -1 for an empty list. */
export function nearestIndex(ts, t) {
  if (!ts.length) return -1;
  let lo = 0;
  let hi = ts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(ts[lo - 1] - t) <= Math.abs(ts[lo] - t)) return lo - 1;
  return lo;
}

const CSS = `
.chart { margin: 0; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.chart-head { display: flex; align-items: baseline; gap: var(--space-3); flex-wrap: wrap; }
.chart-title { font-size: var(--fs-md); font-weight: 600; }
.chart-read { display: flex; gap: var(--space-3); margin-left: auto; font-size: var(--fs-sm); color: var(--text-dim); }
.chart-val { display: inline-flex; align-items: baseline; gap: 4px; }
.chart-val b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
.chart-val.na b { color: var(--text-faint); font-weight: 500; }
.chart-sw { width: 10px; height: 3px; border-radius: 2px; align-self: center; background: var(--pending); }
.chart-sw.s2 { background: var(--text-dim); }
.chart-body { position: relative; display: grid; grid-template-columns: 38px minmax(0, 1fr); gap: 6px; height: 132px; }
.chart-y { display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end; font-size: var(--fs-xs); color: var(--text-faint); font-variant-numeric: tabular-nums; padding: 0; line-height: 1; }
.chart-svg { width: 100%; height: 100%; display: block; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); }
.chart-grid line { stroke: var(--border); stroke-width: 1; vector-effect: non-scaling-stroke; }
.chart-line { fill: none; stroke: var(--pending); stroke-width: 1.5; vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round; }
.chart-line.s2 { stroke: var(--text-dim); stroke-dasharray: 4 3; }
.chart-cross { stroke: var(--text-faint); stroke-width: 1; vector-effect: non-scaling-stroke; }
.chart-empty { position: absolute; inset: 1px 1px 1px 45px; margin: 0; display: flex; align-items: center; justify-content: center; text-align: center; padding: 0 var(--space-3); color: var(--text-faint); font-size: var(--fs-sm); pointer-events: none; background: var(--bg); }
.chart-empty[hidden] { display: none; }
.chart-x { display: flex; justify-content: space-between; padding-left: 44px; font-size: var(--fs-xs); color: var(--text-faint); }
`;

/**
 * createChart({title, unit, digits, series: [{key, label, cls}], minSpan, windowS}) -> {el, setWindow(seconds), update(samples, now, reasons), windowS}
 * samples: [{t (epoch s), v: {key: number|null}}] ascending; reasons: {key: string} explains a series with no readings (shown with "n/a").
 */
export function createChart({ title, unit = "", digits = 1, series, minSpan = 0, windowS = 60, name = "" }) {
  installStyles("chart", CSS);
  const state = { windowS, samples: [], now: 0, reasons: {}, hover: null };

  const readEls = series.map((s, i) => {
    const b = h("b", { text: NA });
    const el = h("span", { class: ["chart-val", "na"], dataset: { series: s.key } }, h("span", { class: ["chart-sw", `s${i + 1}`] }), s.label ? h("span", { text: s.label }) : null, b, h("span", { text: unit }));
    return { el, b };
  });
  const head = h("div", { class: "chart-head" }, h("h3", { class: "chart-title", text: title }), h("div", { class: "chart-read" }, ...readEls.map((r) => r.el)));

  const yLabels = [h("span"), h("span"), h("span")];
  const yAxis = h("div", { class: "chart-y", "aria-hidden": "true" }, ...yLabels);
  const grid = svg("g", { class: "chart-grid" });
  for (let i = 0; i <= 4; i++) grid.append(svg("line", { x1: 0, x2: CHART_W, y1: (i * CHART_H) / 4, y2: (i * CHART_H) / 4 }));
  const paths = series.map((_, i) => svg("path", { class: `chart-line s${i + 1}`, d: "" }));
  const cross = svg("line", { class: "chart-cross", x1: -10, x2: -10, y1: 0, y2: CHART_H });
  const plot = svg("svg", { class: "chart-svg", viewBox: `0 0 ${CHART_W} ${CHART_H}`, preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" }, grid, ...paths, cross);
  const empty = h("p", { class: "chart-empty", text: NA });
  const xLeft = h("span", { text: fmtSpan(windowS) + " ago" });
  const el = h("figure", { class: "chart", dataset: { chart: name || title } }, head, h("div", { class: "chart-body" }, yAxis, plot, empty), h("div", { class: "chart-x", "aria-hidden": "true" }, xLeft, h("span", { text: "now" })));

  const fmt = (v) => fmtNum(v, digits);

  function paintReadout(visible, hoverIdx) {
    series.forEach((s, i) => {
      const idx = hoverIdx !== null && hoverIdx >= 0 ? hoverIdx : lastWithValue(visible, s.key);
      const v = idx >= 0 && visible[idx] ? visible[idx].v[s.key] : null;
      readEls[i].b.textContent = v === null || v === undefined ? NA : fmt(v);
      readEls[i].el.classList.toggle("na", v === null || v === undefined);
      readEls[i].el.title = v === null || v === undefined ? `${s.label || title}: n/a${state.reasons[s.key] ? " · " + state.reasons[s.key] : ""}` : "";
    });
  }

  function lastWithValue(visible, key) {
    for (let i = visible.length - 1; i >= 0; i--) if (Number.isFinite(visible[i].v[key])) return i;
    return -1;
  }

  function draw() {
    const { samples, now, windowS: win } = state;
    const t1 = now || (samples.length ? samples[samples.length - 1].t : 0);
    const t0 = t1 - win;
    const visible = samples.filter((p) => p.t >= t0 && p.t <= t1 + 1);
    const range = dataRange(series.map((s) => visible.map((p) => p.v[s.key])), minSpan);
    const interval = visible.length > 1 ? (visible[visible.length - 1].t - visible[0].t) / (visible.length - 1) : 1;
    const gapS = Math.max(3 * interval, 2.5);
    if (!range) {
      const why = [...new Set(series.map((s) => state.reasons[s.key]).filter(Boolean))].join("; ");
      empty.textContent = why ? `${NA} · ${why}` : `${NA} · no readings in the last ${fmtSpan(win)}`;
      empty.hidden = false;
      paths.forEach((p) => p.setAttribute("d", ""));
      yLabels.forEach((l) => (l.textContent = ""));
    } else {
      empty.hidden = true;
      const tk = niceTicks(range[0], range[1], 4);
      series.forEach((s, i) => paths[i].setAttribute("d", chartPaths(visible.map((p) => ({ t: p.t, v: p.v[s.key] })), { t0, t1, lo: tk.lo, hi: tk.hi, gapS })));
      yLabels[0].textContent = fmt(tk.hi);
      yLabels[1].textContent = fmt((tk.hi + tk.lo) / 2);
      yLabels[2].textContent = fmt(tk.lo);
    }
    xLeft.textContent = `${fmtSpan(win)} ago`;
    const hi = state.hover === null || !visible.length ? null : nearestIndex(visible.map((p) => p.t), t0 + state.hover * (t1 - t0));
    const cx = hi === null ? -10 : ((visible[hi].t - t0) / (t1 - t0 || 1)) * CHART_W;
    cross.setAttribute("x1", cx.toFixed(1));
    cross.setAttribute("x2", cx.toFixed(1));
    paintReadout(visible, hi);
  }

  plot.addEventListener("pointermove", (e) => {
    const b = plot.getBoundingClientRect();
    if (b.width > 0) {
      state.hover = Math.min(1, Math.max(0, (e.clientX - b.left) / b.width));
      draw();
    }
  });
  plot.addEventListener("pointerleave", () => {
    state.hover = null;
    draw();
  });

  return {
    el,
    get windowS() {
      return state.windowS;
    },
    setWindow(seconds) {
      state.windowS = seconds;
      draw();
    },
    update(samples, now, reasons = {}) {
      state.samples = samples;
      state.now = now;
      state.reasons = reasons;
      draw();
    },
  };
}
