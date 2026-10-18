// components/coremap.js: the dashboard's core map. One tile per P-core (★ = favored by the system) and one per E-cluster with its four CPU cells, coloured by
// the chosen metric (Clock / Busy / VID / Temp). Port of web/app.js: METRICS + rampAt (:284-300), tempsByCpu (:326-337), coreReading (:339-349), the core
// map drawing and its probe line (:640-700). Differences: the layout comes from preflight.topology.units (live, D-036), never from a fixed 8+16 grid; colours
// are ten level classes (lv-0..lv-9) generated from the same ramps, because inline styles are forbidden (PLAN §2); an unavailable reading is a neutral
// "n/a" tile with its reason, never a colour.
// The pure model (buildCoreMap, levelOf, autoRange, tempsByCpu, ...) is exported for `node --test`.
import { h, clear } from "./dom.js";
import { fmtNum, NA } from "./fmt.js";
import { installStyles } from "./spark.js";

export const LEVELS = 10;

/** lo/hi are the "fixed" scale; mhz's hi is replaced by the live top ratio x 100 MHz when the topology gives one (D-036). `ramp` runs dark to bright. */
export const METRICS = {
  mhz: { key: "mhz", label: "Clock", unit: "MHz", lo: 400, hi: 6000, digits: 0, minSpan: 200, ramp: ["#132a4a", "#1d4f7c", "#2a7fb0", "#45b4dc", "#9fe6ff"] },
  busy: { key: "busy", label: "Busy", unit: "%", lo: 0, hi: 100, digits: 0, minSpan: 10, ramp: ["#221a46", "#3f3285", "#6852c4", "#9a80f0", "#d9ccff"] },
  vid: { key: "vid", label: "VID", unit: "V", lo: 0.6, hi: 1.5, digits: 3, minSpan: 0.05, ramp: ["#0f302a", "#185a4a", "#238a70", "#40bf98", "#a3efd2"] },
  temp: { key: "temp", label: "Temp", unit: "°C", lo: 25, hi: 105, digits: 0, minSpan: 10, ramp: ["#33200f", "#6e3812", "#b5591a", "#ec8a2c", "#ffd391"] },
};
export const METRIC_KEYS = ["mhz", "busy", "vid", "temp"];

const NO_CORE_TEMP = { value: null, status: "unavailable", reason: "no per-core temperature mapping in this sample", unit: "C" };
const NO_READING = { value: null, status: "unavailable", reason: "not reported", unit: "" };

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isNum = (x) => typeof x === "number" && Number.isFinite(x);

export function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}

/** Level 0..n-1 of `value` inside [lo, hi] (clamped). */
export function levelOf(value, lo, hi, n = LEVELS) {
  if (!isNum(value) || !(hi > lo)) return 0;
  return Math.min(n - 1, Math.floor(clamp((value - lo) / (hi - lo), 0, 1) * n));
}

/** [lo, hi] spanning the numbers in `values`, widened to `minSpan`; null when there are none (the "auto" scale). */
export function autoRange(values, minSpan = 0) {
  const nums = values.filter(isNum);
  if (!nums.length) return null;
  let lo = Math.min(...nums);
  let hi = Math.max(...nums);
  if (hi - lo < minSpan) {
    const mid = (lo + hi) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  return [lo, hi];
}

/** Interpolated colour on a ramp at fraction f (0..1): {hex, light} where `light` means dark text reads better on it. */
export function rampAt(ramp, f) {
  const hexRgb = (c) => [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)];
  const x = clamp(f, 0, 1) * (ramp.length - 1);
  const i = Math.min(Math.floor(x), ramp.length - 2);
  const u = x - i;
  const a = hexRgb(ramp[i]);
  const b = hexRgb(ramp[i + 1]);
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * u));
  return { hex: "#" + c.map((v) => v.toString(16).padStart(2, "0")).join(""), light: (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255 > 0.5 };
}

/** Temperature readings keyed by CPU number (a sensor entry carries `cpu`); sensors without a cpu (Package, ...) are not per-core. */
export function tempsByCpu(temps) {
  const out = {};
  if (!isObj(temps)) return out;
  for (const k of Object.keys(temps)) {
    const t = temps[k];
    if (!isObj(t)) continue;
    const cpu = isNum(t.cpu) ? String(t.cpu) : typeof t.cpu === "string" && /^\d+$/.test(t.cpu) ? t.cpu : null;
    if (cpu !== null) out[cpu] = t;
  }
  return out;
}

/** The raw Reading of `metric` for `cpu` in a telemetry `values` object. */
export function coreReading(values, cpu, metric) {
  const key = String(cpu);
  if (metric === "temp") return tempsByCpu(values && values.temps)[key] || NO_CORE_TEMP;
  const core = values && isObj(values.cores) && isObj(values.cores[key]) ? values.cores[key] : null;
  const r = core ? core[metric] : null;
  return isObj(r) ? r : NO_READING;
}

/** {value:number|null, reason:string|null, source} from a Reading; unavailable stays unavailable. */
export function readingValue(r) {
  if (isObj(r) && isNum(r.value) && r.status !== "unavailable") return { value: r.value, reason: null, source: r.source || "" };
  return { value: null, reason: isObj(r) && typeof r.reason === "string" && r.reason ? r.reason : "no reading", source: isObj(r) && r.source ? r.source : "" };
}

/** Live top-of-scale for the clock metric: the highest max_ratio of the topology x 100 MHz, else the static fallback. */
export function clockCeiling(units) {
  const top = Math.max(0, ...(units || []).map((u) => (isNum(u.max_ratio) ? u.max_ratio : 0)));
  return top > 0 ? top * 100 : METRICS.mhz.hi;
}

/**
 * Pure model of the map. units = preflight.topology.units [{id, kind, cpus, favored, max_ratio}], values = telemetry `values` (or null),
 * metric = "mhz"|"busy"|"vid"|"temp", scale = "fixed"|"auto".
 * -> {metric, scale, lo, hi, unit, digits, tiles:[{id, kind, favored, maxRatio, cells:[{cpu, name, value, text, level, reason, source, others}], head:{value,text,level,reason}}], notes:[reason...], anyData}
 */
export function buildCoreMap(units, values, metric = "mhz", scale = "fixed") {
  const m = METRICS[metric] || METRICS.mhz;
  const list = Array.isArray(units) ? units : [];
  const cells = list.map((u) =>
    (Array.isArray(u.cpus) ? u.cpus : []).map((cpu) => {
      const r = readingValue(coreReading(values, cpu, m.key));
      const core = values && isObj(values.cores) && isObj(values.cores[String(cpu)]) ? values.cores[String(cpu)] : null;
      const others = METRIC_KEYS.filter((k) => k !== m.key && k !== "temp")
        .slice(0, 2)
        .map((k) => ({ key: k, ...readingValue(coreReading(values, cpu, k)) }));
      return { cpu, name: core && typeof core.name === "string" ? core.name : `CPU ${cpu}`, value: r.value, reason: r.reason, source: r.source, others };
    }),
  );
  let lo = m.lo;
  let hi = m.key === "mhz" ? clockCeiling(list) : m.hi;
  if (scale === "auto") {
    const r = autoRange(cells.flat().map((c) => c.value), m.minSpan);
    if (r) [lo, hi] = r;
  }
  const level = (v) => (isNum(v) ? levelOf(v, lo, hi) : null);
  const notes = [];
  const note = (why) => {
    if (why && !notes.includes(why)) notes.push(why);
  };
  const tiles = list.map((u, i) => {
    const cs = cells[i].map((c) => ({ ...c, text: isNum(c.value) ? fmtNum(c.value, m.digits) : NA, level: level(c.value) }));
    cs.forEach((c) => note(c.reason));
    const have = cs.filter((c) => isNum(c.value));
    const headValue = have.length ? Math.max(...have.map((c) => c.value)) : null;
    return {
      id: u.id,
      kind: u.kind,
      favored: u.favored === true,
      maxRatio: isNum(u.max_ratio) ? u.max_ratio : null,
      cells: cs,
      head: { value: headValue, text: headValue === null ? NA : fmtNum(headValue, m.digits), level: level(headValue), reason: have.length ? null : (cs.find((c) => c.reason) || {}).reason || "no reading" },
    };
  });
  return { metric: m.key, scale, lo, hi, unit: m.unit, digits: m.digits, label: m.label, tiles, notes, anyData: tiles.some((t) => t.head.value !== null) };
}

/** The ten `lv-N` colour rules per metric, generated from the ramps (the page may not carry inline styles). */
export function rampCss() {
  const rules = [];
  for (const m of Object.values(METRICS)) {
    for (let i = 0; i < LEVELS; i++) {
      const { hex, light } = rampAt(m.ramp, i / (LEVELS - 1));
      rules.push(`.cmap[data-metric="${m.key}"] .lv-${i} { background: ${hex}; color: ${light ? "#0b0f14" : "#eaf0f7"}; }`);
    }
  }
  return rules.join("\n");
}

const CSS = `
.cmap { display: flex; flex-direction: column; gap: var(--space-3); }
.cmap-row { display: grid; gap: var(--space-2); }
.cmap-row.p { grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); }
.cmap-row.e { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.cm-tile { position: relative; display: flex; flex-direction: column; gap: 2px; min-width: 0; padding: 6px 8px; border-radius: var(--radius); border: 1px solid var(--border-strong); background: var(--surface-2); color: var(--text); }
.cm-tile.na { background: var(--surface-2); color: var(--text-dim); border-style: dashed; }
.cm-name { display: flex; align-items: center; gap: 4px; font-size: var(--fs-xs); letter-spacing: 0.06em; text-transform: uppercase; opacity: 0.9; }
.cm-star { color: var(--warn); font-size: var(--fs-sm); line-height: 1; }
.cm-tile[class*="lv-"] .cm-star { color: inherit; }
.cm-ratio { margin-left: auto; text-transform: none; letter-spacing: 0; opacity: 0.75; }
.cm-read { display: flex; align-items: baseline; gap: 3px; font-variant-numeric: tabular-nums; }
.cm-value { font-size: var(--fs-xl); font-weight: 600; line-height: 1.1; }
.cm-tile.na .cm-value { font-size: var(--fs-lg); font-weight: 500; color: var(--text-faint); }
.cm-unit { font-size: var(--fs-xs); opacity: 0.8; }
.cm-sub { display: flex; justify-content: space-between; gap: 6px; font-size: var(--fs-xs); opacity: 0.85; font-variant-numeric: tabular-nums; white-space: nowrap; }
.cm-cells { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 3px; margin-top: 3px; }
.cm-cell { display: flex; justify-content: space-between; align-items: baseline; gap: 4px; padding: 2px 5px; border-radius: var(--radius-sm); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; border: 1px solid rgba(255, 255, 255, 0.08); }
.cm-cell.na { background: var(--surface); color: var(--text-faint); border-style: dashed; border-color: var(--border); }
.cm-cell b { font-weight: 600; font-size: var(--fs-sm); }
.cmap-foot { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; font-size: var(--fs-xs); color: var(--text-dim); }
.cmap-legend { display: inline-flex; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; }
.cmap-ramp { display: inline-flex; border-radius: 2px; overflow: hidden; border: 1px solid var(--border-strong); }
.cmap-ramp i { display: block; width: 11px; height: 8px; }
.cmap-note { color: var(--text-faint); }
.cmap-group { font-size: var(--fs-xs); letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-faint); margin: 0 0 4px; }
`;

/**
 * createCoreMap() -> {el, update(units, values, {metric, scale})}. Re-rendering replaces the tiles (about 12 elements + 16 cells: cheap at 1 Hz).
 */
export function createCoreMap() {
  installStyles("coremap", CSS + rampCss());
  const el = h("div", { class: "cmap", dataset: { metric: "mhz" } });

  function tileEl(t, model) {
    const m = METRICS[model.metric];
    const isE = t.kind === "E";
    const cls = ["cm-tile", isE ? "e" : "p", t.head.value === null ? "na" : `lv-${t.head.level}`];
    const title = t.head.value === null
      ? `${t.id}: ${m.label} n/a · ${t.head.reason}`
      : `${t.id}${t.favored ? " (favored by the system)" : ""}: ${t.head.text} ${m.unit}${isE ? " (highest of its CPUs)" : ""}${t.maxRatio ? ` · fused top ${t.maxRatio}x` : ""}`;
    const name = h("div", { class: "cm-name" }, h("span", { text: t.id }), t.favored ? h("span", { class: "cm-star", title: "Favored core (acpi_cppc)", text: "★" }) : null, t.maxRatio ? h("span", { class: "cm-ratio", text: `${t.maxRatio}x` }) : null);
    const read = h("div", { class: "cm-read" }, h("span", { class: "cm-value", text: t.head.text }), t.head.value === null ? null : h("span", { class: "cm-unit", text: m.unit }));
    const kids = [name, read];
    if (isE) {
      kids.push(
        h(
          "div",
          { class: "cm-cells" },
          ...t.cells.map((c) =>
            h(
              "span",
              { class: ["cm-cell", c.value === null ? "na" : `lv-${c.level}`], title: c.value === null ? `CPU ${c.cpu}: ${m.label} n/a · ${c.reason}` : `CPU ${c.cpu}: ${c.text} ${m.unit}`, dataset: { cpu: c.cpu } },
              h("span", { text: c.cpu }),
              h("b", { text: c.text }),
            ),
          ),
        ),
      );
    } else if (t.cells[0]) {
      const c = t.cells[0];
      kids.push(h("div", { class: "cm-sub" }, ...c.others.map((o) => h("span", { text: o.value === null ? NA :`${fmtNum(o.value, METRICS[o.key].digits)} ${METRICS[o.key].unit}` }))));
    }
    return h("div", { class: cls, title, dataset: { unit: t.id, kind: t.kind } }, ...kids);
  }

  return {
    el,
    update(units, values, { metric = "mhz", scale = "fixed" } = {}) {
      const model = buildCoreMap(units, values, metric, scale);
      el.dataset.metric = model.metric;
      clear(el);
      if (!model.tiles.length) {
        el.append(h("p", { class: "muted", text: `${NA} · no topology from the helper yet (preflight)` }));
        return model;
      }
      const group = (kind, label) => {
        const ts = model.tiles.filter((t) => t.kind === kind);
        if (!ts.length) return null;
        return h("div", {}, h("p", { class: "cmap-group", text: label }), h("div", { class: ["cmap-row", kind === "E" ? "e" : "p"] }, ...ts.map((t) => tileEl(t, model))));
      };
      const ramp = h("span", { class: "cmap-ramp", "aria-hidden": "true" }, ...Array.from({ length: LEVELS }, (_, i) => h("i", { class: `lv-${i}` })));
      const fmtEnd = (v) => fmtNum(v, model.digits);
      el.append(
        group("P", "Performance cores"),
        group("E", "Efficient clusters"),
        h(
          "div",
          { class: "cmap-foot" },
          h("span", { class: "cmap-legend" }, h("span", { text: fmtEnd(model.lo) }), ramp, h("span", { text: `${fmtEnd(model.hi)} ${model.unit}` })),
          h("span", { text: model.scale === "auto" ? "auto scale" : "fixed scale" }),
          model.notes.length ? h("span", { class: "cmap-note", text: `${model.label} n/a · ${model.notes.join("; ")}` }) : null,
        ),
      );
      return model;
    },
  };
}
