// views/curves.js: Curves screen (PLAN §4.4): measure V/F curves (vf.measure quick | dense, cancel), keep them (curves.list/get/update/delete) and compare them
// (chart, table, library). Ops: vf.measure, cancel, curves.list, curves.get, curves.update, curves.delete, tune.last (read-only: the fused top points for the baseline's diamonds).
// Port of web/app.js: reasonText/interpText :1884-1902, CV + presets :1906-1943, recPoints..ensureRecord :1945-2051, curvesModel/unmeasuredOf/captions/curvePins/curveNotes
// :2056-2224, renderCurves/legend/selects :2226-2291, table :2294-2362, library :2365-2444, measuring :2466-2609, initCurves :2611-2636, and the V/F chart engine makeVfChart
// :1644-1880. Differences from the page: the units come from preflight.topology (D-036), the run state lives in a controller keyed by the bridge (a result that arrives while another
// screen is open is kept), Delete is an inline two-step instead of a dialog, Export copies the JSON to the clipboard first (the shipped webview has no download handler; a plain
// download link stays as a second button), and every rule (args, model, captions, table, layout, tooltip) is pure and unit-tested.
// `createVfChart` is a hoist candidate (components/vfchart.js): the Tune inspector preview needs the same chart.
import { h, svg, clear } from "../components/dom.js";
import { NA } from "../components/fmt.js";
import { installStyles, installScreenStyles, segmented } from "../components/spark.js";
import { MIN_SPAN } from "../components/chart.js";
import { isNum, fmtMv, MINUS, TIMES } from "../lib/format.js";
import { modelFromRead } from "../lib/units.js";
import { UNIT_CSS, unitModel, unitPicker, favMark, unitTitle, favoredIds, idsOfKind, opGate, errText, fmtDate } from "./stress.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isStr = (x) => typeof x === "string";
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

export const TURBO_OFF_NOTE = "Turbo is off (power-saver profile?): ratios above base cannot be reached — switch to Balanced or Performance";
export const MEASURE_SECONDS = { quick: 120, dense: 600 }; // for the 12 reference units (SPEC-v3 §3): the estimate scales per unit
export const MEASURE_REF_UNITS = 12;

// ---------------------------------------------------------------- reasons and wording

/** Why a V/F point could not be measured (contract 9 addendum). `p` (optional) is the point record: cap_granularity names the ratios the caps did run (`neighbours`). */
export function reasonText(reason, p) {
  if (reason === "max_turbo") return "the point exists but this core's max turbo ratio is lower, so it never runs here";
  if (reason === "cap_granularity" || reason === "cpufreq_granularity") {
    const n = isObj(p) && Array.isArray(p.neighbours) ? p.neighbours.filter(isNum) : [];
    const at = isObj(p) && isNum(p.ratio) ? `${p.ratio}${TIMES}` : "this ratio";
    if (reason === "cpufreq_granularity") return "cpufreq caps cannot pin this ratio (the cap lands on a neighbour)";
    return `Linux cannot cap this core to exactly ${at} (frequency rounding)${n.length === 2 ? `; the nearest are ${n[0]}${TIMES} and ${n[1]}${TIMES}` : n.length === 1 ? `; the nearest is ${n[0]}${TIMES}` : ""}`;
  }
  if (reason === "active_cores") return "reachable only with few active cores, and the measurement loads the whole unit";
  if (reason === "turbo_off") return "turbo is off (power-saver profile?): ratios above base cannot be reached; switch to Balanced or Performance";
  if (reason === "user_cap") return "your own scaling_max_freq cap is below this ratio, so it was not attempted";
  return "not reachable on this core";
}

/** A point whose voltage is estimated: the straight line between the measured V/F points `from`. */
export function interpText(p) {
  const f = isObj(p) && Array.isArray(p.from) ? p.from.filter(isNum) : [];
  if (isObj(p) && p.exact === true) return `exact: both neighbours equal${f.length === 2 ? ` (${f[0]}${TIMES} and ${f[1]}${TIMES})` : ""}`;
  return `interpolated${f.length === 2 ? ` between ${f[0]}${TIMES} and ${f[1]}${TIMES}` : ""}`;
}

/** The tuning state recorded with a curve: "words" (read this boot), "stock" (every offset 0) or "unknown". */
export function tuningWords(kind) {
  return kind === "words" ? "Tuning at the time: read from the CPU" : kind === "stock" ? "Tuning at the time: stock = fused table as applied (every offset 0)" : "Tuning state unknown (no 'Read ratios & voltages' this boot)";
}
export function tuningKind(c) {
  const t = isObj(c) ? c.tuning_state : null;
  if (isObj(t) && isStr(t.kind)) return t.kind;
  if (isStr(t)) return t;
  return isObj(c) && isStr(c.tuning_state_kind) ? c.tuning_state_kind : "unknown";
}
export function unitCount(c) {
  return Array.isArray(c.units) ? c.units.length : isObj(c.units) ? Object.keys(c.units).length : 0;
}
export const curveTitle = (c) => `${c.name || c.id} · ${fmtDate(c.created)} · ${c.mode || "?"}`;

// ---------------------------------------------------------------- records

// G-118: a measured point carries fused_point (1-based int, or "oc") when its ratio is one of the unit's fused V/F points; the key is absent for the ratios in between.
export const isFusedPoint = (x) => x === "oc" || (isNum(x) && x >= 1);
export const fusedLabel = (x) => (x === "oc" ? "OC point" : `fused point ${x}`);

/** Measured points of one unit: [{ratio, mhz, v, vmin, vmax, kept, total, status, from, exact, fused}] sorted by frequency; unmeasured / unreachable ones are left out. */
export function recPoints(rec, unit) {
  const u = isObj(rec) && isObj(rec.units) ? rec.units[unit] : null;
  const pts = isObj(u) && Array.isArray(u.points) ? u.points : [];
  return pts
    .filter((p) => isObj(p) && p.status !== "mismatch" && p.status !== "unreachable" && isNum(p.vid_v) && isNum(p.ratio))
    .map((p) => ({ ratio: p.ratio, mhz: isNum(p.mhz) ? p.mhz : p.ratio * 100, v: p.vid_v, vmin: p.vid_min, vmax: p.vid_max, kept: p.kept, total: p.total, status: p.status, from: p.from, exact: p.exact, fused: isFusedPoint(p.fused_point) ? p.fused_point : null }))
    .sort((a, b) => a.mhz - b.mhz);
}
/** G-132 item 8: the chart plots the unit's V/F points only (the fused points and the OC point). A record without any fused_point (an older curve) draws every measured ratio. */
export function recChartPoints(rec, unit) {
  const all = recPoints(rec, unit);
  return all.some((q) => isFusedPoint(q.fused)) ? all.filter((q) => isFusedPoint(q.fused)) : all;
}
export function recAllPoints(rec, unit) {
  const u = isObj(rec) && isObj(rec.units) ? rec.units[unit] : null;
  return isObj(u) && Array.isArray(u.points) ? u.points.filter(isObj) : [];
}
export const sumPoints = (rec) => (isObj(rec) && isObj(rec.units) ? Object.values(rec.units).reduce((n, u) => n + (isObj(u) && Array.isArray(u.points) ? u.points.length : 0), 0) : 0);

/** {v, exact} at `mhz` on points sorted by frequency (a straight line between neighbours; exact within 50 MHz of a point), null outside the measured range. */
export function interpAt(pts, mhz) {
  if (!pts.length || mhz < pts[0].mhz - 1e-6 || mhz > pts[pts.length - 1].mhz + 1e-6) return null;
  for (let i = 0; i < pts.length; i++) {
    if (Math.abs(pts[i].mhz - mhz) < 50) return { v: pts[i].v, exact: true };
    if (pts[i].mhz > mhz && i > 0) {
      const a = pts[i - 1];
      const b = pts[i];
      return { v: a.v + ((b.v - a.v) * (mhz - a.mhz)) / (b.mhz - a.mhz), exact: false };
    }
  }
  return null;
}

export const baselineId = (list) => {
  const b = list.find((c) => c.baseline === true);
  return b ? b.id : null;
};
/** The curve shown first (G-123): the newest baseline, else the newest complete curve of every unit, else the newest (the list is newest first). */
export function defaultCurveId(list, ids) {
  const b = baselineId(list);
  if (b) return b;
  const all = list.find((c) => c.complete !== false && Array.isArray(c.units) && ids.length > 0 && ids.every((u) => c.units.includes(u)));
  if (all) return all.id;
  return list.length ? list[0].id : null;
}

// ---- not measured / interpolated captions ----

export function notMeasuredLong(kind, max, p) {
  if (kind === "max_turbo") return `above this core's max ratio${max === null ? "" : ` ${max}${TIMES}`}`;
  if (kind === "mismatch") return "the core ran at another ratio than the target, so no voltage was kept";
  return reasonText(kind, p);
}
export function notMeasuredShort(kind, max) {
  if (kind === "max_turbo") return `above core max${max === null ? "" : ` ${max}${TIMES}`}`;
  if (kind === "cpufreq_granularity" || kind === "cap_granularity") return "cpufreq cannot pin this ratio";
  if (kind === "turbo_off") return "turbo is off";
  if (kind === "user_cap") return "above your cpufreq cap";
  if (kind === "active_cores") return "needs few active cores";
  if (kind === "mismatch") return "did not settle at the target";
  return "not reachable";
}
/** "P1, P2, P4–P7": consecutive units of one kind (three or more) collapse to a range. */
export function unitRanges(units) {
  const parts = [];
  const num = (u) => parseInt(u.replace(/^[A-Z]+/, ""), 10);
  const pre = (u) => u.replace(/[0-9]+$/, "");
  for (let i = 0; i < units.length; ) {
    let j = i;
    while (j + 1 < units.length && pre(units[j + 1]) === pre(units[i]) && num(units[j + 1]) === num(units[j]) + 1) j++;
    if (j - i >= 2) parts.push(`${units[i]}–${units[j]}`);
    else for (let k = i; k <= j; k++) parts.push(units[k]);
    i = j + 1;
  }
  return parts.join(", ");
}
/** "56–57×", "23, 29×" */
export function ratiosText(ratios) {
  const rs = [...new Set(ratios)].sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < rs.length; ) {
    let j = i;
    while (j + 1 < rs.length && rs[j + 1] === rs[j] + 1) j++;
    parts.push(j > i ? `${rs[i]}–${rs[j]}` : String(rs[i]));
    i = j + 1;
  }
  return `${parts.join(", ")}${TIMES}`;
}

/** Points that exist but were not reached (unreachable) or did not settle (mismatch), for the units drawn: [{unit, ratio, mhz, kind, max, title}]. Never drawn inside the plot. */
export function unmeasuredOf(rec, drawn, ids) {
  const out = [];
  if (!isObj(rec)) return out;
  for (const u of ids) {
    if (!drawn.has(u)) continue;
    const pts = recAllPoints(rec, u);
    const okRatios = pts.filter((p) => p.status === "ok" && isNum(p.ratio)).map((p) => p.ratio);
    const vfOnly = pts.some((p) => isFusedPoint(p.fused_point)); // G-132: only V/F points that have no value at all; in-between ratios live in the Table view
    for (const p of pts) {
      if (!isNum(p.ratio) || (p.status !== "unreachable" && p.status !== "mismatch")) continue;
      if (vfOnly && !isFusedPoint(p.fused_point)) continue;
      const below = okRatios.filter((r) => r < p.ratio);
      const kind = p.status === "mismatch" ? "mismatch" : isStr(p.reason) ? p.reason : "other";
      const max = below.length ? Math.max(...below) : null;
      out.push({ unit: u, ratio: p.ratio, mhz: isNum(p.mhz) ? p.mhz : p.ratio * 100, kind, max, title: `${u} ${p.ratio}${TIMES}: ${notMeasuredLong(kind, max, p)}` });
    }
  }
  return out;
}
/** The caption line: the not-measured points grouped by reason, then by the units sharing the same ratios. */
export function notMeasuredCaption(list, ids) {
  const byWhy = new Map();
  for (const m of list) {
    const why = notMeasuredShort(m.kind, m.max);
    if (!byWhy.has(why)) byWhy.set(why, new Map());
    const units = byWhy.get(why);
    if (!units.has(m.unit)) units.set(m.unit, []);
    units.get(m.unit).push(m.ratio);
  }
  const groups = [];
  for (const [why, units] of byWhy) {
    const bySig = new Map();
    for (const u of ids) {
      if (!units.has(u)) continue;
      const sig = ratiosText(units.get(u));
      if (!bySig.has(sig)) bySig.set(sig, []);
      bySig.get(sig).push(u);
    }
    for (const [sig, us] of bySig) groups.push({ first: ids.indexOf(us[0]), text: `${unitRanges(us)} at ${sig} (${why})` });
  }
  groups.sort((a, b) => a.first - b.first);
  return groups.length ? `not measured: ${groups.map((g) => g.text).join(" · ")}` : "";
}
/** G-132: V/F points drawn as hollow markers because no cap could run them: their voltage is the straight line between the measured V/F points beside them. */
export function interpolatedCaption(rec, drawn, ids) {
  const bySig = new Map();
  for (const u of ids) {
    if (!drawn.has(u)) continue;
    const rs = recAllPoints(rec, u).filter((p) => p.status === "interpolated" && isNum(p.ratio)).map((p) => p.ratio);
    if (!rs.length) continue;
    const sig = ratiosText(rs);
    if (!bySig.has(sig)) bySig.set(sig, []);
    bySig.get(sig).push(u);
  }
  return bySig.size ? `interpolated (hollow markers): ${[...bySig].map(([sig, us]) => `${unitRanges(us)} at ${sig}`).join(" · ")}` : "";
}

/** On the baseline, the fused top point (0x09) as a diamond at its voltage. `fusedOf(unit)` -> {ratio, v} | null (from the kept tune read). */
export function curvePins(rec, { drawn, ids, baseline, fusedOf }) {
  const pins = [];
  if (!isObj(rec) || rec.id !== baseline) return pins;
  for (const u of ids) {
    if (!drawn.has(u)) continue;
    const meas = recPoints(rec, u);
    const f = fusedOf(u);
    if (f && meas.length) {
      const top = meas.find((q) => q.ratio === f.ratio);
      pins.push({ unit: u, kind: "fused", mhz: f.ratio * 100, v: f.v, title: `${u} fused top point (cmd 0x09): ${f.v.toFixed(4)} V at ${f.ratio}${TIMES}${top ? `; measured ${top.v.toFixed(4)} V (${fmtMv((top.v - f.v) * 1000)}).` : "; not measured on this curve."}` });
    }
  }
  return pins;
}

/**
 * The chart model: {series: [{key, unit, label, pts: [{mhz, v, fused?, interp?}], dash?, live?, fav?}], markers, band, ymode, empty, pins?, unmeasured?}.
 * s = {a (primary record | null), b (compare record | null), loaded, listLen, drawn, delta, band, live (bool), liveHead, ids, baseline, fusedOf, favored (Set)}
 */
export function curvesModel(s) {
  const { a, b, drawn, ids } = s;
  const model = { series: [], markers: [], band: null, ymode: s.delta ? "mv" : "v", empty: null };
  if (!a) {
    model.empty = !s.loaded ? "Loading stored curves." : s.listLen ? "Loading the selected curve." : "No curve yet. Run a Quick measurement above; the chart fills as each point arrives.";
    return model;
  }
  if (s.delta && !b) {
    model.empty = "Choose a second curve under Compare with to see the difference.";
    return model;
  }
  for (const u of ids) {
    if (!drawn.has(u)) continue;
    const pa = recChartPoints(a, u);
    const label = u + (s.favored && s.favored.has(u) ? " ★" : "");
    const fav = !!s.favored && s.favored.has(u);
    if (s.delta) {
      const pb = recChartPoints(b, u);
      const pts = pa.map((p) => {
        const at = interpAt(pb, p.mhz);
        return at ? { mhz: p.mhz, v: (p.v - at.v) * 1000 } : null;
      }).filter(Boolean);
      if (pts.length) model.series.push({ key: u, unit: u, label, pts, fav });
    } else {
      if (pa.length) model.series.push({ key: u, unit: u, label, fav, pts: pa.map((p) => ({ mhz: p.mhz, v: p.v, fused: p.fused, interp: p.status === "interpolated" ? p : null })), live: s.live === true && s.liveHead === u });
      if (b) {
        const pb = recChartPoints(b, u);
        if (pb.length) model.series.push({ key: `${u}:cmp`, unit: u, label: `${u} (compare)`, pts: pb.map((p) => ({ mhz: p.mhz, v: p.v })), dash: true });
      }
    }
  }
  if (s.band) {
    const solid = model.series.filter((x) => !x.dash);
    const xs = [...new Set(solid.flatMap((x) => x.pts.map((p) => p.mhz)))].sort((x, y) => x - y);
    const band = [];
    for (const x of xs) {
      const vs = solid.map((x2) => interpAt(x2.pts, x)).filter(Boolean).map((r) => r.v);
      if (vs.length > 1) band.push({ mhz: x, lo: Math.min(...vs), hi: Math.max(...vs) });
    }
    model.band = band.length > 1 ? band : null;
  }
  if (!model.series.length) model.empty = drawn.size ? "No measured points for the units drawn." : "No units drawn. Press a unit below, or pick a preset.";
  else if (!s.delta) {
    model.pins = curvePins(a, { drawn, ids, baseline: s.baseline, fusedOf: s.fusedOf });
    model.unmeasured = unmeasuredOf(a, drawn, ids);
  }
  return model;
}

/** The notes under the chart: {base, stock: {show, text, title}, unreach}. */
export function curveNotes({ a, list, live, baseline, drawn, ids, wordsRead }) {
  const isBase = !!a && a.id === baseline && !live;
  let base;
  if (isBase) {
    base = `Stock baseline: this curve is the fused V/F table as applied (offsets 0). ${wordsRead ? "Diamonds mark the top-point voltage read from the CPU (cmd 0x09); they should sit on the curve where it reaches that ratio." : "Read ratios & voltages (Tune) to draw the top-point voltage from the CPU (cmd 0x09) and cross-check it."}`;
  } else if (baseline && a) base = `Stock baseline: ${(list.find((c) => c.id === baseline) || {}).name}. Predictions in Tune are made on it.`;
  else base = "No curve is marked as the stock baseline yet. Tune predicts the effective voltage on it; set one in the Library.";
  const isStock = !!a && !live && tuningKind(a) === "stock";
  return {
    base,
    stock: { show: isStock, text: "stock = fused table as applied", title: "Measured with every offset at 0: the voltages are the CPU's fused V/F table as the CPU applies it." },
    unreach: a ? [notMeasuredCaption(unmeasuredOf(a, drawn, ids), ids), interpolatedCaption(a, drawn, ids)].filter(Boolean).join(" · ") : "",
  };
}

// ---------------------------------------------------------------- presets, library, table, measuring

/** The "Units drawn" presets; the Favored one exists only when the topology marks some (not all) P-cores favored. */
export function presets(m) {
  const list = [{ value: "all", label: "All", units: m.ids }, { value: "P", label: "P-cores", units: idsOfKind(m, "P") }, { value: "E", label: "E-clusters", units: idsOfKind(m, "E") }];
  const fav = favoredIds(m);
  if (fav.length && fav.length < idsOfKind(m, "P").length) list.push({ value: "fav", label: "Favored ★", units: fav, title: `The favored P-cores: ${fav.join(", ")}${m.favoredSource ? ` (${m.favoredSource})` : ""}` });
  return list;
}
export function presetOf(list, set) {
  for (const p of list) if (p.units.length === set.size && p.units.every((u) => set.has(u))) return p.value;
  return "";
}

export function measureEstimate(mode, n) {
  const secs = (MEASURE_SECONDS[mode] * n) / MEASURE_REF_UNITS;
  return `${mode === "quick" ? "Quick" : "Dense"}, ${n} ${n === 1 ? "unit" : "units"}, about ${secs < 90 ? `${Math.round(secs)} s` : `${Math.round(secs / 60)} min`}`;
}
/** The vf.measure arguments (ops.rs vf_measure): mode, units (omitted when every unit is measured), name (trimmed, 1..64, omitted when empty). */
export function measureArgs(cv, ids) {
  const args = { mode: cv.mode };
  const sel = cv.sel ? ids.filter((u) => cv.sel.has(u)) : ids.slice();
  if (sel.length < ids.length) args.units = sel;
  const name = String(cv.name || "").trim().slice(0, 64);
  if (name) args.name = name;
  return args;
}
export function measureText(cv, ids, { running, gate }) {
  const n = (cv.sel ? ids.filter((u) => cv.sel.has(u)) : ids).length;
  if (running) return "Measuring. Each unit is capped one ratio at a time and loaded; limits are restored when it ends or is cancelled.";
  if (gate) return gate;
  if (!ids.length) return "The CPU topology is not known yet (preflight).";
  if (n === 0) return "Select at least one unit.";
  return `Ready: ${measureEstimate(cv.mode, n)}. Raises the load on the selected cores for that time.`;
}
/** The progress line of a vf.measure progress event, or null. */
export function progressText(ev) {
  if (!isObj(ev) || !isNum(ev.step) || !isNum(ev.of) || ev.of <= 0) return null;
  const bits = [];
  if (isStr(ev.unit)) bits.push(ev.unit);
  if (isNum(ev.ratio)) bits.push(`${ev.ratio}${TIMES}`);
  bits.push(`point ${ev.step} of ${ev.of}`);
  if (isNum(ev.eta_s)) bits.push(`about ${ev.eta_s < 90 ? `${Math.round(ev.eta_s)} s` : `${Math.round(ev.eta_s / 60)} min`} left`);
  return { step: Math.min(ev.step, ev.of), of: ev.of, text: bits.join(" · ") };
}

/** A curves.list row as library cells. */
export function libraryRow(c, { primary = null } = {}) {
  const kind = tuningKind(c);
  return {
    id: c.id,
    current: c.id === primary,
    name: c.name || c.id,
    baseline: c.baseline === true,
    partial: c.complete === false,
    created: fmtDate(c.created),
    mode: c.mode || NA,
    tuning: { text: kind === "words" ? "tuned" : kind, tone: kind === "stock" ? "ok" : kind === "words" ? "pending" : "neutral", title: tuningWords(kind) },
    units: unitCount(c),
  };
}

const bucket = (f, n) => clamp(Math.floor(f * n), 0, n - 1);

/**
 * The Table view: {ratios, rows: [{unit, cells: [{kind: "none" | "na" | "val", text, cls, title}]}], note, canDelta, delta, empty}.
 * `a` = shown record, `base` = the baseline record, `delta` = show mV difference to the baseline.
 */
export function tableModel(a, base, { delta, ids, live = false, primaryIsBaseline = false }) {
  const canDelta = Boolean(base) && a !== null && (live || !primaryIsBaseline);
  const useDelta = delta && canDelta;
  if (!a) return { ratios: [], rows: [], note: "", canDelta, delta: useDelta, empty: "n/a: no curve selected yet." };
  const ratios = [...new Set(ids.flatMap((u) => recAllPoints(a, u).map((p) => p.ratio)).filter(isNum))].sort((x, y) => x - y);
  const val = (u, p) => {
    if ((p.status !== "ok" && p.status !== "interpolated") || !isNum(p.vid_v)) return null;
    const guess = p.status === "interpolated" && p.exact !== true; // an estimated point is marked "~" (an exact one is not)
    if (!useDelta) return { v: p.vid_v, est: guess };
    const at = interpAt(recPoints(base, u), isNum(p.mhz) ? p.mhz : p.ratio * 100);
    return at ? { v: (p.vid_v - at.v) * 1000, est: !at.exact || guess } : null;
  };
  let lo = Infinity;
  let hi = -Infinity;
  let big = 0;
  for (const u of ids) for (const p of recAllPoints(a, u)) {
    const x = val(u, p);
    if (x) {
      lo = Math.min(lo, x.v);
      hi = Math.max(hi, x.v);
      big = Math.max(big, Math.abs(x.v));
    }
  }
  const rows = ids.filter((u) => isObj(a.units) && isObj(a.units[u])).map((u) => {
    const byRatio = {};
    for (const p of recAllPoints(a, u)) byRatio[p.ratio] = p;
    const cells = ratios.map((r) => {
      const p = byRatio[r];
      if (!p) return { kind: "none", text: "", cls: "vt-none", title: "" };
      const x = val(u, p);
      if (!x) {
        const why = p.status === "unreachable" ? "unreachable" : p.status === "mismatch" ? "mismatch" : useDelta ? "no baseline value" : NA;
        return { kind: "na", text: p.status === "ok" ? NA : "—", cls: "vt-na", title: `${u} at ${r}${TIMES}: ${why}${p.status === "unreachable" ? `: ${reasonText(p.reason, p)}${isStr(p.reason) ? ` (${p.reason})` : ""}` : ""}` };
      }
      let cls;
      let text;
      if (useDelta) {
        text = (x.v > 0 ? "+" : x.v < 0 ? MINUS : "") + Math.abs(x.v).toFixed(0);
        const f = big > 0 ? x.v / big : 0;
        cls = `vt dv-${f < 0 ? "m" : "p"}${bucket(Math.abs(f), 4)}`;
      } else {
        text = x.v.toFixed(3);
        cls = `vt hv-${bucket(hi > lo ? (x.v - lo) / (hi - lo) : 0.5, 10)}`;
      }
      const kept = isNum(p.kept) && isNum(p.total) ? `, ${p.kept} of ${p.total} samples at target` : "";
      const range = isNum(p.vid_min) && isNum(p.vid_max) ? `, range ${p.vid_min.toFixed(3)} to ${p.vid_max.toFixed(3)} V` : "";
      const interp = p.status === "interpolated" ? `, ${interpText(p)}` : "";
      return { kind: "val", text: text + (x.est ? "~" : ""), cls, title: `${u} at ${r}${TIMES} (${isNum(p.mhz) ? p.mhz.toFixed(0) : r * 100} MHz): ${p.vid_v.toFixed(3)} V${range}${kept}${interp}` };
    });
    return { unit: u, cells };
  });
  return { ratios, rows, canDelta, delta: useDelta, note: useDelta ? `mV difference to the baseline curve (${base ? base.name : ""}). Negative means lower voltage.` : "Volts requested by the core at each ratio. Darker is lower.", empty: "" };
}

// ---------------------------------------------------------------- V/F chart geometry (pure)

export const VC = { L: 66, R: 26, T: 18, B: 56 }; // plot margins; the width follows the container, the height follows the width

export function niceStep(span, target) {
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const f = raw / mag;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
}

/** Axis domains and sizes of a model at width W. -> {W, H, B, nmH, lanes, empty} or, with data, also {xDom, yDom, step, sx(mhz), sy(v)}. */
export function vfLayout(model, W) {
  const nm = Array.isArray(model.unmeasured) ? model.unmeasured : [];
  const lanes = [...new Set(nm.map((m) => m.unit))];
  const nmH = nm.length ? 16 + lanes.length * 5 : 0; // the "not measured" row: tick marks under the x axis, one lane per unit; the plot keeps its height
  const B = VC.B + nmH;
  const H = clamp(Math.round(W * 0.38), 300, 520) + nmH;
  const all = [];
  for (const s of model.series) for (const p of s.pts) all.push(p);
  for (const m of model.markers || []) all.push(m);
  for (const m of model.pins || []) all.push(m);
  const base = { W, H, B, nmH, lanes, nm };
  if (!all.length) return { ...base, empty: true };
  const xs = all.concat(nm).map((p) => p.mhz); // the not-measured ticks only widen the x axis, never the voltage range
  const xlo = Math.floor((Math.min(...xs) - 50) / 500) * 500;
  const xhi = Math.ceil((Math.max(...xs) + 50) / 500) * 500;
  const xDom = [xlo, Math.max(xhi, xlo + 1000)];
  const ys = all.map((p) => p.v);
  let ylo = Math.min(...ys);
  let yhi = Math.max(...ys);
  if (model.band) for (const b of model.band) {
    ylo = Math.min(ylo, b.lo);
    yhi = Math.max(yhi, b.hi);
  }
  if (model.ymode === "mv") {
    ylo = Math.min(ylo, 0);
    yhi = Math.max(yhi, 0);
  }
  if (yhi - ylo < (model.ymode === "mv" ? 4 : MIN_SPAN.volt)) {
    const m = (yhi + ylo) / 2;
    const d = model.ymode === "mv" ? 2 : MIN_SPAN.volt / 2;
    ylo = m - d;
    yhi = m + d;
  }
  const step = niceStep(yhi - ylo, 6);
  const yDom = [Math.floor(ylo / step) * step, Math.ceil(yhi / step) * step];
  if (yDom[1] - yDom[0] < step) yDom[1] += step;
  return {
    ...base,
    empty: false,
    xDom,
    yDom,
    step,
    sx: (mhz) => VC.L + ((mhz - xDom[0]) / (xDom[1] - xDom[0])) * (W - VC.L - VC.R),
    sy: (v) => VC.T + ((yDom[1] - v) / (yDom[1] - yDom[0])) * (H - VC.T - B),
  };
}

/** The tooltip rows at `mhz`: every drawn series with a value there, highest first. -> [{s, v, exact}] */
export function tipRows(model, mhz) {
  const rows = [];
  for (const s of model.series) {
    const at = interpAt(s.pts, mhz);
    if (at) rows.push({ s, v: at.v, exact: at.exact });
  }
  return rows.sort((a, b) => b.v - a.v);
}
export const tipValue = (model, v) => (model.ymode === "mv" ? fmtMv(v) : `${v.toFixed(3)} V`);

// ---------------------------------------------------------------- controller (no DOM; one per bridge)

/** A large record comes back within the helper's 64 KiB line with omitted_units: [unit, ...]; the rest is fetched with curves.get {id, units} and merged. */
export async function mergeOmitted(bridge, rec) {
  for (let round = 0; round < 12 && isObj(rec) && Array.isArray(rec.omitted_units) && rec.omitted_units.length; round++) {
    const want = rec.omitted_units.filter(isStr);
    const res = await bridge.call("curves.get", { id: rec.id, units: want });
    if (!(res.ok && isObj(res.result) && isObj(res.result.units))) break;
    if (!isObj(rec.units)) rec.units = {};
    Object.assign(rec.units, res.result.units);
    const still = Array.isArray(res.result.omitted_units) ? res.result.omitted_units.filter((u) => want.includes(u) && !(u in res.result.units)) : [];
    rec.omitted_units = still;
    if (still.length === want.length) break; // no progress: stop asking
  }
  return rec;
}

const controllers = new WeakMap();
export function curvesController(ctx) {
  const cached = controllers.get(ctx.bridge);
  if (cached) return cached;
  const c = createCurvesController(ctx);
  controllers.set(ctx.bridge, c);
  return c;
}

export function createCurvesController({ bridge, store }) {
  const cv = {
    noTurbo: false,
    list: [], // curves.list rows, newest first
    cache: {}, // id -> full record (curves.get)
    loaded: false,
    primary: null, // id shown in the chart / table
    compare: null, // id drawn dashed (or subtracted in difference mode)
    view: "chart", // chart | table | library
    drawn: null, // Set of unit ids drawn; null = every unit
    delta: false,
    band: false,
    mode: "quick",
    sel: null, // Set of unit ids to measure; null = every unit
    name: "",
    live: null, // the record being filled by a running vf.measure
    liveHead: null,
    tableDelta: false,
    renaming: null,
    renameText: null, // what has been typed into the rename box (null = untouched)
    confirmDelete: null,
    words: null, // modelFromRead of the kept tune read (fused top points), or null
  };
  const view = { run: null, status: "", problems: [], progress: null, submitting: false, cancelling: false, done: new Set(), listProblem: "" };
  const painters = new Set();
  const paint = () => painters.forEach((fn) => fn());
  const ids = () => unitModel(store.get().preflight).ids;
  const drawnSet = () => cv.drawn || new Set(ids());
  const unitsOf = (rec) => (rec && isObj(rec.units) ? Object.keys(rec.units) : []);

  const primaryRec = () => cv.live || (cv.primary ? cv.cache[cv.primary] : null) || null;
  const compareRec = () => (cv.compare ? cv.cache[cv.compare] || null : null);
  const fusedOf = (u) => (cv.words && cv.words.units[u] && cv.words.units[u].fused ? cv.words.units[u].fused : null);

  function model() {
    const m = unitModel(store.get().preflight);
    return curvesModel({
      a: primaryRec(), b: compareRec(), loaded: cv.loaded, listLen: cv.list.length, drawn: drawnSet(), delta: cv.delta, band: cv.band, live: cv.live !== null, liveHead: cv.liveHead,
      ids: m.ids, baseline: baselineId(cv.list), fusedOf, favored: new Set(favoredIds(m)),
    });
  }

  async function ensureRecord(id) {
    if (!id) return null;
    if (cv.cache[id]) return cv.cache[id];
    const res = await bridge.call("curves.get", { id });
    if (res.ok && isObj(res.result)) {
      cv.cache[id] = await mergeOmitted(bridge, res.result);
      return cv.cache[id];
    }
    return null;
  }

  async function loadCurves(selectNewest = false) {
    const res = await bridge.call("curves.list", {});
    if (!(res.ok && isObj(res.result) && Array.isArray(res.result.curves))) {
      cv.list = [];
      cv.loaded = true;
      view.listProblem = `Stored curves unavailable: ${errText(res.error)}`;
      paint();
      return;
    }
    view.listProblem = "";
    cv.list = res.result.curves.filter(isObj);
    cv.loaded = true;
    cv.noTurbo = res.result.no_turbo === true; // G-132: the power-saver profile turns turbo off; a run cannot reach ratios above base
    const have = new Set(cv.list.map((c) => c.id));
    for (const k of Object.keys(cv.cache)) if (!have.has(k)) delete cv.cache[k];
    if (cv.compare && !have.has(cv.compare)) {
      cv.compare = null;
      cv.delta = false;
    }
    if (selectNewest) cv.primary = cv.list.length ? cv.list[0].id : null; // a measurement just ended: show it
    else if (!cv.primary || !have.has(cv.primary)) cv.primary = defaultCurveId(cv.list, ids());
    paint();
    await Promise.all([cv.primary, cv.compare, baselineId(cv.list)].filter(Boolean).map(ensureRecord));
    paint();
  }

  /** The kept tune read, for the baseline's fused top points. tune.last is a read with no mailbox traffic. */
  async function loadWords() {
    const res = await bridge.call("tune.last", {});
    if (res.ok && isObj(res.result) && res.result.available === true) cv.words = modelFromRead(res.result.result);
    paint();
  }

  const watching = new Set();
  function watch(id) {
    if (watching.has(id)) return;
    watching.add(id);
    bridge.wait(id).then(onResult);
  }

  function adopt(id) {
    if (!isStr(id) || view.done.has(id)) return false;
    view.run = { id };
    watch(id);
    cv.live = { id: "live", name: "Measuring", mode: "?", units: {}, live: true };
    cv.liveHead = null;
    cv.view = "chart";
    return true;
  }

  function onVfPoint(ev) {
    if (!isObj(ev)) return;
    if (!view.run && !adopt(ev.op_id)) return;
    if (view.run.id !== ev.op_id) return;
    if (!cv.live) cv.live = { id: "live", name: "Measuring", mode: "?", units: {}, live: true };
    const known = ids();
    if (!isStr(ev.unit) || !known.includes(ev.unit) || !isNum(ev.ratio)) return;
    const u = cv.live.units[ev.unit] || (cv.live.units[ev.unit] = { points: [] });
    if (ev.retry === true) {
      const i = u.points.findIndex((q) => q.ratio === ev.ratio);
      if (i >= 0) u.points.splice(i, 1); // G-118: a retry replaces the first attempt
    }
    u.points.push({ ratio: ev.ratio, mhz: isNum(ev.mhz) ? ev.mhz : ev.ratio * 100, vid_v: isNum(ev.vid_v) ? ev.vid_v : null, status: isStr(ev.status) ? ev.status : "ok" });
    if (ev.status === "ok") cv.liveHead = ev.unit;
    paint();
  }

  function onProgress(ev) {
    if (!isObj(ev) || ev.step === undefined) return; // a measurement's progress carries `step`/`of` (a stress run's `elapsed_s`)
    if (!view.run && !adopt(ev.op_id)) return;
    if (view.run.id !== ev.op_id) return;
    const p = progressText(ev);
    if (p) {
      view.progress = p;
      paint();
    }
  }

  async function onResult(fin) {
    if (fin && isStr(fin.id)) {
      view.done.add(fin.id);
      watching.delete(fin.id);
    }
    view.run = null;
    view.cancelling = false;
    cv.live = null;
    cv.liveHead = null;
    const err = fin && isObj(fin.error) ? fin.error : null;
    const problemsOf = (x) => [...new Set((Array.isArray(x) ? x : []).filter(isStr))];
    if (fin && fin.ok === true && isObj(fin.result)) {
      const rec = isObj(fin.result.record) ? fin.result.record : null;
      const id = isStr(fin.result.curve_id) ? fin.result.curve_id : rec && isStr(rec.id) ? rec.id : null;
      if (rec && id) {
        if (!rec.id) rec.id = id;
        cv.cache[id] = await mergeOmitted(bridge, rec);
      }
      cv.primary = id;
      const problems = problemsOf(fin.result.problems);
      view.problems = problems;
      view.status = `Measured ${rec ? sumPoints(cv.cache[id] || rec) : 0} points. Stored as "${rec && rec.name ? rec.name : id}". ${rec && isObj(rec.tuning_state) ? `${tuningWords(rec.tuning_state.kind)}. ` : ""}${problems.length ? `${problems.length} ${problems.length === 1 ? "note" : "notes"} — see below` : ""}`.trim();
      await loadCurves(false);
      return;
    }
    if (err && isObj(err.detail) && Array.isArray(err.detail.problems)) view.problems = problemsOf(err.detail.problems); // a failed or cancelled run: its notes too
    const cid = err && isObj(err.detail) && isStr(err.detail.curve_id) ? err.detail.curve_id : null;
    if (err && err.code === "CANCELLED") {
      view.status = "Cancelled. The points measured so far were kept as a partial curve; CPU limits are restored.";
      if (cid) {
        cv.primary = cid;
        await loadCurves(false);
      } else await loadCurves(true);
    } else {
      view.status = `Failed: ${errText(err)}${cid ? " The partial curve was kept." : ""}`;
      if (cid) {
        cv.primary = cid;
        await loadCurves(false);
      } else paint();
    }
  }

  async function startMeasure() {
    const all = ids();
    const sel = cv.sel ? all.filter((u) => cv.sel.has(u)) : all;
    if (view.run || view.submitting || opGate(store.get(), false) || sel.length === 0) return false;
    const args = measureArgs(cv, all);
    view.submitting = true;
    view.problems = [];
    view.progress = null;
    view.status = "Submitting…";
    paint();
    const first = await bridge.call("vf.measure", args);
    view.submitting = false;
    if (!first.ok) {
      view.status = `Not started: ${errText(first.error)}`;
      paint();
      return false;
    }
    if (!view.run) view.run = { id: first.id }; // events may have arrived (and adopted the run) before the answer did
    cv.live = { ...(cv.live || { units: {} }), id: "live", name: args.name || "Measuring", mode: args.mode, live: true };
    cv.liveHead = cv.liveHead || null;
    cv.view = "chart";
    view.progress = view.progress || { step: 0, of: 1, text: "starting" };
    view.status = `Measuring (operation ${first.id}). The chart fills as each point arrives.`;
    paint();
    if (first.pending) watch(first.id);
    else await onResult(first);
    return true;
  }

  async function cancel() {
    if (!view.run || view.cancelling) return false;
    view.cancelling = true;
    paint();
    const res = await bridge.call("cancel", { op_id: view.run.id });
    if (!res.ok) {
      view.cancelling = false;
      view.status = `Cancel not accepted: ${errText(res.error)}`;
    } else view.status = "Cancel requested. The points measured so far are kept as a partial curve.";
    paint();
    return res.ok;
  }

  async function select(id) {
    if (!id || id === "live") return;
    cv.primary = id;
    if (cv.compare === id) {
      cv.compare = null;
      cv.delta = false;
    }
    paint();
    await ensureRecord(id);
    paint();
  }
  async function setCompare(id) {
    cv.compare = id || null;
    if (!cv.compare) cv.delta = false;
    paint();
    await ensureRecord(cv.compare);
    paint();
  }
  async function rename(id, name) {
    const n = String(name || "").trim();
    if (!n) {
      view.status = "A name cannot be empty.";
      paint();
      return false;
    }
    const res = await bridge.call("curves.update", { id, name: n.slice(0, 64) });
    cv.renaming = null;
    cv.renameText = null;
    view.status = res.ok ? "Renamed." : `Rename failed: ${errText(res.error)}`;
    delete cv.cache[id]; // the stored record carries the name
    await loadCurves(false);
    return res.ok;
  }
  async function toggleBaseline(c) {
    const res = await bridge.call("curves.update", { id: c.id, baseline: c.baseline !== true });
    view.status = res.ok ? (c.baseline === true ? "Baseline cleared." : `"${c.name || c.id}" is now the stock baseline.`) : `Failed: ${errText(res.error)}`;
    await loadCurves(false);
    return res.ok;
  }
  async function remove(c) {
    const res = await bridge.call("curves.delete", { id: c.id });
    cv.confirmDelete = null;
    view.status = res.ok ? "Deleted." : `Delete failed: ${errText(res.error)}`;
    if (cv.primary === c.id) cv.primary = null;
    await loadCurves(false);
    return res.ok;
  }
  /** The JSON text of a stored curve (every unit merged in), or null. */
  async function recordJson(c) {
    const rec = await ensureRecord(c.id);
    return rec ? JSON.stringify(rec, null, 2) : null;
  }

  bridge.on("vf.point", onVfPoint);
  bridge.on("progress", onProgress);
  store.watch((s) => s.link.state, (state) => {
    if (state === "connected" && !cv.loaded) {
      loadCurves(false);
      loadWords();
    }
  });

  return {
    cv,
    view,
    ids,
    drawnSet,
    model,
    primaryRec,
    compareRec,
    fusedOf,
    loadCurves,
    loadWords,
    ensureRecord,
    startMeasure,
    cancel,
    select,
    setCompare,
    rename,
    toggleBaseline,
    remove,
    recordJson,
    unitsOf,
    subscribe(fn) {
      painters.add(fn);
      return () => painters.delete(fn);
    },
    paint,
    gate: () => opGate(store.get(), view.submitting || !!view.run),
  };
}

// ---------------------------------------------------------------- V/F chart (DOM)

export const VFCHART_CSS = `
.vfchart-host { position: relative; min-width: 0; }
.vfsvg { width: 100%; height: auto; display: block; border-radius: var(--radius); background: var(--bg); border: 1px solid var(--border); }
.vfsvg:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
.vc-grid line { stroke: var(--border); stroke-width: 1; }
.vc-grid .vc-zero { stroke: var(--text-dim); stroke-dasharray: 5 5; }
.vc-grid .vc-vgrid { stroke: color-mix(in srgb, var(--border) 60%, transparent); }
.vc-tick { font: 11.5px var(--font-ui); fill: var(--text-dim); }
.vc-ratio { font-size: 10.5px; fill: color-mix(in srgb, var(--text-dim) 70%, transparent); }
.plot-axis { font: 10.5px var(--font-ui); fill: var(--text-dim); }
.plot-empty { font: 14px var(--font-ui); fill: var(--text-dim); }
.vc-band { fill: color-mix(in srgb, var(--pending) 14%, transparent); stroke: none; }
.vc-line { fill: none; stroke: var(--u, var(--pending)); stroke-width: 2.2; stroke-linejoin: round; stroke-linecap: round; opacity: 0.92; }
.vc-line.fav { stroke-width: 2.8; }
.vc-line.dash { stroke-dasharray: 7 6; stroke-width: 2; opacity: 0.8; }
.has-hot .vc-line:not(.hot) { opacity: 0.22; }
.vc-line.hot { stroke-width: 3.8; opacity: 1; }
.vc-dot { fill: var(--u, var(--pending)); stroke: var(--surface); stroke-width: 1.5; }
.vc-dot.between { stroke-width: 0.8; fill-opacity: 0.55; }
.vc-dot.fused { stroke: var(--text); stroke-width: 1.2; }
.vc-dot.interp { fill: var(--surface); stroke: var(--u, var(--pending)); stroke-width: 2.2; }
.vc-head { fill: var(--u, var(--pending)); fill-opacity: 0.25; stroke: var(--u, var(--pending)); stroke-width: 2; }
.vc-cross { stroke: var(--text-dim); stroke-width: 1; stroke-dasharray: 3 4; }
.vc-hit { fill: transparent; cursor: crosshair; }
.vc-tipbg { fill: color-mix(in srgb, var(--bg) 92%, transparent); stroke: var(--border-strong); stroke-width: 1; }
.vc-tiphead { font: 600 13px var(--font-ui); fill: var(--text); }
.vc-tiprow { font: 12px var(--font-ui); fill: var(--text-dim); }
.vc-tiprow.mono { font-family: var(--font-mono); fill: var(--text); }
.vc-tiprow.strong { fill: #ffffff; font-weight: 700; }
.vc-tiprow.muted { fill: var(--text-faint); }
.vc-tipsw { fill: var(--u, var(--pending)); }
.vc-tipsw.dash { opacity: 0.55; }
.vc-nm line { stroke: var(--u, var(--pending)); stroke-width: 2; stroke-linecap: round; }
.vc-nm rect { fill: transparent; cursor: help; }
.vc-nm-label { font-size: 10.5px; }
.vc-pin.fused path { fill: var(--u, var(--pending)); fill-opacity: 0.25; stroke: var(--text); stroke-width: 1.5; cursor: help; }
.vc-pin.clamp circle { fill: var(--surface); stroke: var(--u, var(--pending)); stroke-width: 2.5; cursor: help; }
`;

/**
 * createVfChart(host, {label}) -> {render(model), hover(mhz, v), destroy(), get model}. Geometry goes through SVG attributes only; the tooltip is SVG drawn inside the chart (no HTML
 * positioning, so no inline style). Arrow keys on the focused chart move the crosshair by 100 MHz, Escape clears it.
 */
export function createVfChart(host, { label = "V/F curves" } = {}) {
  installStyles("vfchart", VFCHART_CSS);
  const ch = { model: null, hoverMhz: null, lay: null, svg: null, cross: null, tip: null, lineEls: {}, hit: null };

  function render(model) {
    ch.model = model;
    const W = clamp(Math.round(host.clientWidth || 960), 560, 1800);
    const lay = vfLayout(model, W);
    ch.lay = lay;
    const root = svg("svg", { class: "vfsvg", viewBox: `0 0 ${lay.W} ${lay.H}`, role: "img", tabindex: 0, focusable: "true", "aria-label": `${label}. Use the left and right arrow keys to read values.` });
    ch.svg = root;
    ch.lineEls = {};
    if (lay.empty) {
      root.append(svg("text", { x: lay.W / 2, y: lay.H / 2, "text-anchor": "middle", class: "plot-empty" }, model.empty || "Nothing to draw yet."));
      clear(host).append(root);
      ch.hit = null;
      return;
    }
    const { xDom, yDom, step, sx, sy, B, H, nm, lanes } = lay;
    const nodes = [];
    const grid = svg("g", { class: "vc-grid" });
    for (let v = yDom[0]; v <= yDom[1] + step * 1e-6; v += step) {
      const y = sy(v);
      grid.append(svg("line", { x1: VC.L, x2: lay.W - VC.R, y1: y, y2: y, class: model.ymode === "mv" && Math.abs(v) < step * 1e-6 ? "vc-zero" : "" }));
      grid.append(svg("text", { x: VC.L - 10, y: y + 4, "text-anchor": "end", class: "vc-tick" }, model.ymode === "mv" ? (v > 0 ? "+" : v < 0 ? MINUS : "") + Math.abs(v).toFixed(step < 1 ? 1 : 0) : v.toFixed(step < 0.1 ? 2 : 1)));
    }
    for (let m = xDom[0]; m <= xDom[1]; m += 500) {
      const x = sx(m);
      grid.append(svg("line", { x1: x, x2: x, y1: VC.T, y2: H - B, class: "vc-vgrid" }));
      grid.append(svg("text", { x, y: H - B + 18, "text-anchor": "middle", class: "vc-tick" }, String(m)));
      grid.append(svg("text", { x, y: H - B + 33, "text-anchor": "middle", class: "vc-tick vc-ratio" }, `${m / 100}${TIMES}`));
    }
    nodes.push(grid, svg("text", { x: (VC.L + lay.W - VC.R) / 2, y: H - 6, "text-anchor": "middle", class: "plot-axis" }, "frequency (MHz) and ratio"));
    const ymid = (VC.T + H - B) / 2;
    nodes.push(svg("text", { x: 14, y: ymid, "text-anchor": "middle", transform: `rotate(-90 14 ${ymid})`, class: "plot-axis" }, model.ymode === "mv" ? "difference (mV)" : "VID (V)"));
    if (nm.length) {
      const top = H - B + 40;
      nodes.push(svg("text", { x: 6, y: top + 9, "text-anchor": "start", class: "vc-tick vc-nm-label" }, "not measured"));
      for (const m of nm) {
        const g = svg("g", { class: `vc-nm u-${m.unit}` });
        const x = sx(m.mhz);
        const y = top + lanes.indexOf(m.unit) * 5;
        g.append(svg("line", { x1: x.toFixed(1), x2: x.toFixed(1), y1: y + 0.5, y2: y + 4.5 }), svg("rect", { x: (x - 5).toFixed(1), y: y - 0.5, width: 10, height: 6 }), svg("title", {}, m.title));
        nodes.push(g);
      }
    }
    if (model.band && model.band.length > 1) {
      const up = model.band.map((b, i) => `${i ? "L" : "M"}${sx(b.mhz).toFixed(1)} ${sy(b.hi).toFixed(1)}`).join("");
      const dn = model.band.slice().reverse().map((b) => `L${sx(b.mhz).toFixed(1)} ${sy(b.lo).toFixed(1)}`).join("");
      nodes.push(svg("path", { d: `${up}${dn}Z`, class: "vc-band" }));
    }
    const lines = svg("g", { class: "vc-lines" });
    for (const s of model.series) {
      if (!s.pts.length) continue;
      const p = svg("path", { d: s.pts.map((q, i) => `${i ? "L" : "M"}${sx(q.mhz).toFixed(1)} ${sy(q.v).toFixed(1)}`).join(""), class: `vc-line u-${s.unit}${s.dash ? " dash" : ""}${s.fav ? " fav" : ""}`, "data-key": s.key });
      lines.append(p);
      ch.lineEls[s.key] = p;
      const marked = s.pts.some((q) => isFusedPoint(q.fused)); // fused points filled and larger, in-between ratios small and quiet (even on a dense run)
      if ((marked || s.pts.length <= 24) && !s.dash) {
        for (const q of s.pts) {
          const fused = isFusedPoint(q.fused);
          const interp = isObj(q.interp);
          const dot = svg("circle", { cx: sx(q.mhz).toFixed(1), cy: sy(q.v).toFixed(1), r: !marked ? 3 : fused ? 4.5 : 2, class: `vc-dot u-${s.unit}${marked ? (fused ? " fused" : " between") : ""}${interp ? " interp" : ""}` });
          if (interp) dot.append(svg("title", {}, `${s.unit} ${q.mhz / 100}${TIMES}: ${fused ? `${fusedLabel(q.fused)}, ` : ""}${q.v.toFixed(3)} V, ${interpText(q.interp)}`));
          else if (marked && fused) dot.append(svg("title", {}, `${s.unit} ${q.mhz / 100}${TIMES}: ${fusedLabel(q.fused)}`));
          lines.append(dot);
        }
      }
      if (s.live) {
        const last = s.pts[s.pts.length - 1];
        lines.append(svg("circle", { cx: sx(last.mhz).toFixed(1), cy: sy(last.v).toFixed(1), r: 7, class: `vc-head u-${s.unit}` }));
      }
    }
    nodes.push(lines);
    ch.cross = svg("line", { x1: -10, x2: -10, y1: VC.T, y2: H - B, class: "vc-cross" });
    ch.tip = svg("g", { transform: "translate(-999 -999)" });
    const hit = svg("rect", { x: VC.L, y: VC.T, width: lay.W - VC.L - VC.R, height: H - VC.T - B, class: "vc-hit" });
    ch.hit = hit;
    nodes.push(ch.cross, hit, ch.tip);
    // pins sit above the hit area so the browser shows their <title> on hover: diamond = the fused top-point value read from the CPU, small ring = a clamped point;
    // points that were not measured are drawn as ticks in the "not measured" row under the axis
    for (const m of model.pins || []) {
      const g = svg("g", { class: `vc-pin u-${m.unit} ${m.kind}` });
      const cx = sx(m.mhz);
      const cy = sy(m.v);
      if (m.kind === "fused") g.append(svg("path", { d: `M${cx.toFixed(1)} ${(cy - 7).toFixed(1)}L${(cx + 7).toFixed(1)} ${cy.toFixed(1)}L${cx.toFixed(1)} ${(cy + 7).toFixed(1)}L${(cx - 7).toFixed(1)} ${cy.toFixed(1)}Z` }));
      else g.append(svg("circle", { cx: cx.toFixed(1), cy: cy.toFixed(1), r: 5.5 }));
      g.append(svg("title", {}, m.title));
      nodes.push(g);
    }
    root.append(...nodes);
    clear(host).append(root);
    hit.addEventListener("pointermove", (e) => {
      const b = hit.getBoundingClientRect();
      if (b.width <= 0) return;
      const fx = (e.clientX - b.left) / b.width;
      const fy = (e.clientY - b.top) / b.height;
      hover(Math.round((xDom[0] + fx * (xDom[1] - xDom[0])) / 100) * 100, yDom[1] - fy * (yDom[1] - yDom[0]));
    });
    hit.addEventListener("pointerleave", () => hover(null, null));
    root.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Escape") return;
      e.preventDefault();
      if (e.key === "Escape") return hover(null, null);
      const cur = ch.hoverMhz === null ? Math.round((xDom[0] + xDom[1]) / 200) * 100 : ch.hoverMhz;
      hover(clamp(cur + (e.key === "ArrowRight" ? 100 : -100), xDom[0], xDom[1]), null);
    });
    root.addEventListener("blur", () => hover(null, null));
    if (ch.hoverMhz !== null) hover(ch.hoverMhz, null);
  }

  /** Move the crosshair; the tooltip lists every drawn unit at that frequency (an exact measurement, or "~" for a straight-line estimate). */
  function hover(mhz, yv) {
    ch.hoverMhz = mhz;
    if (!ch.hit || !ch.model) return;
    for (const k of Object.keys(ch.lineEls)) ch.lineEls[k].classList.remove("hot");
    ch.svg.classList.remove("has-hot");
    if (mhz === null) {
      ch.cross.setAttribute("x1", -10);
      ch.cross.setAttribute("x2", -10);
      ch.tip.setAttribute("transform", "translate(-999 -999)");
      ch.tip.replaceChildren();
      return;
    }
    const { sx, W } = ch.lay;
    const x = sx(mhz);
    ch.cross.setAttribute("x1", x.toFixed(1));
    ch.cross.setAttribute("x2", x.toFixed(1));
    const rows = tipRows(ch.model, mhz);
    let best = null;
    if (yv !== null) for (const r of rows) {
      const d = Math.abs(r.v - yv);
      if (best === null || d < best.d) best = { d, r };
    }
    if (best && ch.lineEls[best.r.s.key]) {
      ch.lineEls[best.r.s.key].classList.add("hot");
      ch.svg.classList.add("has-hot");
    }
    const w = 196;
    const lh = 17;
    const nodes = [svg("rect", { x: 0, y: 0, width: w, height: 30 + Math.max(rows.length, 1) * lh, rx: 8, class: "vc-tipbg" }), svg("text", { x: 12, y: 19, class: "vc-tiphead" }, `${mhz / 100}${TIMES} · ${mhz} MHz`)];
    if (!rows.length) nodes.push(svg("text", { x: 12, y: 40, class: "vc-tiprow muted" }, "no data at this frequency"));
    rows.forEach((r, i) => {
      const y = 38 + i * lh;
      const strong = best && best.r === r ? " strong" : "";
      nodes.push(
        svg("rect", { x: 12, y: y - 9, width: 10, height: 4, rx: 2, class: `vc-tipsw u-${r.s.unit}${r.s.dash ? " dash" : ""}` }),
        svg("text", { x: 28, y, class: `vc-tiprow${strong}` }, r.s.label || r.s.unit),
        svg("text", { x: w - 12, y, "text-anchor": "end", class: `vc-tiprow mono${strong}` }, (r.exact ? "" : "~") + tipValue(ch.model, r.v)),
      );
    });
    ch.tip.replaceChildren(...nodes);
    const tx = x + 14 + w > W - VC.R ? x - 14 - w : x + 14;
    ch.tip.setAttribute("transform", `translate(${tx.toFixed(1)} ${VC.T + 6})`);
  }

  let observer = null;
  let timer = null;
  if (typeof ResizeObserver === "function") {
    let lastW = 0;
    observer = new ResizeObserver(() => {
      const w = Math.round(host.clientWidth);
      if (!ch.model || Math.abs(w - lastW) < 24) return;
      lastW = w;
      clearTimeout(timer);
      timer = setTimeout(() => render(ch.model), 120);
    });
    observer.observe(host);
  }
  return {
    render,
    hover,
    get model() {
      return ch.model;
    },
    /** Stop watching the host's size (call when the screen goes away). */
    destroy() {
      if (observer) observer.disconnect();
      observer = null;
      clearTimeout(timer);
    },
  };
}

// ---------------------------------------------------------------- view

const CSS = `
.cv-measure-grid { display: grid; grid-template-columns: max-content minmax(180px, 260px) max-content; gap: var(--space-3) var(--space-5); align-items: end; }
.cv-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.cv-label { font-size: var(--fs-sm); color: var(--text-dim); }
.cv-field input[type="text"], .cv-field select, .cv-tools select, .cv-pick select, .inline-edit { padding: 3px 8px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); font: inherit; min-width: 0; }
.cv-buttons { display: flex; gap: var(--space-2); }
.cv-units { margin-top: var(--space-3); }
.cv-est { margin: var(--space-2) 0 0; color: var(--text-dim); font-size: var(--fs-sm); }
.cv-run { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2) var(--space-4); margin: var(--space-2) 0; }
.cv-run[hidden] { display: none; }
.cv-run progress { flex: 0 1 360px; accent-color: var(--pending); }
.cv-status { margin: var(--space-2) 0 0; color: var(--text-dim); font-size: var(--fs-sm); min-height: 1.2em; }
.cv-problems { margin: var(--space-2) 0 0; padding-left: var(--space-5); color: var(--warn); font-size: var(--fs-sm); }
.cv-problems[hidden] { display: none; }
.cv-turbo { margin: var(--space-2) 0 0; color: var(--warn); font-size: var(--fs-sm); }
.cv-turbo[hidden] { display: none; }
.cv-bar { display: flex; align-items: center; gap: var(--space-4); flex-wrap: wrap; margin-bottom: var(--space-3); }
.cv-pick { display: flex; align-items: center; gap: var(--space-2); min-width: 0; margin-left: auto; }
.cv-pick select { max-width: 72ch; text-overflow: ellipsis; }
.cv-tools { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin-bottom: var(--space-2); }
.cv-tools select { max-width: 34ch; }
.legend-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 var(--space-3); }
.lg-chip { display: inline-flex; align-items: center; gap: 7px; padding: 2px 11px 2px 9px; font: inherit; font-size: var(--fs-sm); border-radius: 999px; border: 1px solid var(--border); background: transparent; color: var(--text-dim); cursor: pointer; }
.lg-chip .sw { width: 14px; height: 4px; border-radius: 2px; background: var(--u, var(--pending)); opacity: 0.35; }
.lg-chip[aria-pressed="true"] { color: var(--text); background: color-mix(in srgb, var(--u, var(--pending)) 13%, var(--surface-2)); border-color: color-mix(in srgb, var(--u, var(--pending)) 50%, var(--border)); }
.lg-chip[aria-pressed="true"] .sw { opacity: 1; }
.seg-btn { padding: 1px 9px; }
.cv-note { max-width: 110ch; margin: 6px 0 0; color: var(--text-dim); font-size: var(--fs-sm); }
.cv-note:empty { display: none; }
.cv-stock { display: inline-block; margin-right: 6px; }
.cv-stock[hidden] { display: none; }
.cv-wrap[hidden] { display: none; }
.cv-scroll { overflow: auto; }
.vf-table td.vt { font-family: var(--font-mono); font-size: 12.5px; text-align: center; padding: 4px 6px; min-width: 52px; }
.vf-table th { text-align: center; }
.vf-table th[scope="row"] { text-align: left; font-weight: 600; color: var(--text); position: sticky; left: 0; background: var(--surface); }
.vf-table .vt-na { color: var(--text-faint); text-align: center; }
.hv-0 { background: #17305a; } .hv-1 { background: #1c4272; } .hv-2 { background: #22558a; } .hv-3 { background: #2a69a1; } .hv-4 { background: #337db6; }
.hv-5 { background: #3f91c8; } .hv-6 { background: #54a6d6; color: #08142e; } .hv-7 { background: #72bbe2; color: #08142e; } .hv-8 { background: #98d0ec; color: #08142e; } .hv-9 { background: #c4e6f6; color: #08142e; }
.dv-m0 { background: #1c3f6e; } .dv-m1 { background: #25609a; } .dv-m2 { background: #3b88c4; } .dv-m3 { background: #78b9e6; color: #08142e; }
.dv-p0 { background: #5e3b18; } .dv-p1 { background: #8d5619; } .dv-p2 { background: #c47a22; } .dv-p3 { background: #f2ab4e; color: #08142e; }
.lib-table td { vertical-align: middle; }
.lib-name { white-space: nowrap; }
.lib-name .pill { margin-left: 6px; }
.lib-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.lib-actions .btn, .lib-name .btn { padding: 1px 9px; font-size: var(--fs-sm); }
.lib-actions .btn.danger { color: var(--danger); border-color: rgba(239, 107, 95, 0.5); }
.lib-table tr.is-current td:first-child { box-shadow: inset 3px 0 0 var(--pending); }
.inline-edit { width: 22ch; margin-right: 6px; }
`;

function pill(text, tone, title = "") {
  return h("span", { class: "pill", dataset: { tone }, title, text });
}

/** Copy `text` to the clipboard (the shipped webview has no download handler). Resolves true when it worked. */
export async function copyText(text, nav = globalThis.navigator) {
  try {
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
      await nav.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* falls through */
  }
  return false;
}
export const exportName = (c) => `arltuner-curve-${String(c.id).replace(/[^A-Za-z0-9._-]/g, "_")}.json`;

export function curvesView() {
  const subs = [];
  return {
    id: "curves",
    mount(root, ctx) {
      installScreenStyles();
      installStyles("units", UNIT_CSS);
      installStyles("curves", CSS);
      const { store } = ctx;
      const ctl = curvesController(ctx);
      const { cv, view } = ctl;
      // The topology comes from preflight, which may arrive after this screen was mounted (a start on #/curves): picker, presets and legend are rebuilt when it does.
      let m = unitModel(store.get().preflight);
      let ids = m.ids;
      let presetList = presets(m);
      const topoOf = (x) => x.ids.join(",") + "|" + favoredIds(x).join(",");
      let topoKey = topoOf(m);

      // ---- measure card ----
      const modeSeg = segmented([{ value: "quick", label: "Quick", title: "The V/F point ratios of each unit, about 2 minutes for all units" }, { value: "dense", label: "Dense", title: "Every ratio from 8 to the unit's top, about 10 minutes for all units" }], cv.mode, (v) => { cv.mode = v; render(); }, { label: "Resolution" });
      const nameInput = h("input", { type: "text", id: "cv-name", maxlength: 64, placeholder: "optional, e.g. stock", autocomplete: "off", spellcheck: "false", "aria-label": "Name" });
      nameInput.value = cv.name;
      nameInput.addEventListener("input", () => { cv.name = nameInput.value; });
      const measureBtn = h("button", { type: "button", class: "btn primary", id: "btn-measure", text: "Measure", on: { click: () => ctl.startMeasure() } });
      const cancelBtn = h("button", { type: "button", class: "btn", id: "btn-measure-cancel", text: "Cancel", on: { click: () => ctl.cancel() } });
      const buildPicker = () => unitPicker(m, cv.sel || new Set(ids), (s) => { cv.sel = s; render(); }, { label: "Units to measure" });
      let picker = buildPicker();
      const est = h("p", { class: "cv-est", id: "cv-est", role: "status" });
      const turbo = h("p", { class: "cv-turbo", id: "cv-turbo-note", role: "status", hidden: true, text: TURBO_OFF_NOTE });
      const bar = h("progress", { id: "cv-bar", max: 1, value: 0, "aria-label": "Measurement progress" });
      const progress = h("span", { class: "muted", id: "cv-progress" });
      const run = h("div", { class: "cv-run", id: "cv-run", hidden: true }, bar, progress);
      const status = h("p", { class: "cv-status", id: "cv-status", role: "status" });
      const problems = h("ul", { class: "cv-problems", id: "cv-problems", hidden: true });
      const field = (label, el) => h("div", { class: "cv-field" }, h("span", { class: "cv-label", text: label }), el);
      const measure = h("section", { class: "dcard", id: "cv-measure" },
        h("div", { class: "dcard-head" }, h("h2", { text: "Measure" }), h("span", { class: "muted", text: "Caps each unit one ratio at a time, loads it and reads the voltage it asks for: the V/F curve as the CPU applies it right now." })),
        h("div", { class: "cv-measure-grid" }, field("Resolution", modeSeg.el), field("Name", nameInput), h("div", { class: "cv-buttons" }, measureBtn, cancelBtn)),
        h("div", { class: "cv-units" }, field("Units", picker.el)), est, turbo, run, status, problems);

      // ---- curves card ----
      const viewSeg = segmented([{ value: "chart", label: "Chart" }, { value: "table", label: "Table" }, { value: "library", label: "Library" }], cv.view, (v) => { cv.view = v; render(); }, { label: "Curve view" });
      viewSeg.el.id = "cv-view";
      const primarySel = h("select", { id: "cv-primary", "aria-label": "Curve" });
      primarySel.addEventListener("change", () => ctl.select(primarySel.value));
      const compareSel = h("select", { id: "cv-compare", "aria-label": "Compare with" });
      compareSel.addEventListener("change", () => ctl.setCompare(compareSel.value));
      const buildPresets = () => segmented(presetList.map((p) => ({ value: p.value, label: p.label, title: p.title || "" })), "all", (v) => { cv.drawn = new Set(presetList.find((p) => p.value === v).units); render(); }, { label: "Units drawn" });
      let presetSeg = buildPresets();
      const bandBtn = h("button", { type: "button", class: "chip toggle seg-btn", id: "cv-band", text: "Spread band", title: "Shade the lowest to highest voltage across the units drawn", on: { click: () => { cv.band = !cv.band; render(); } } });
      const deltaBtn = h("button", { type: "button", class: "chip toggle seg-btn", id: "cv-delta", text: "Δ mV", on: { click: () => { if (cv.compare) { cv.delta = !cv.delta; render(); } } } });
      const legend = h("div", { class: "legend-chips", id: "cv-legend", role: "group", "aria-label": "Units drawn. Press to toggle; hold Shift to show only one." });
      const chartHost = h("div", { class: "vfchart-host", id: "cv-chart" });
      const chart = createVfChart(chartHost, { label: "Measured V/F curves, volts against frequency" });
      const stockPill = h("span", { class: "pill cv-stock", id: "cv-stock", hidden: true, dataset: { tone: "ok" } });
      const baseNote = h("span", { id: "cv-base-note" });
      const unreach = h("p", { class: "cv-note", id: "cv-unreach" });
      const chartWrap = h("div", { class: "cv-wrap", id: "cv-chart-wrap" },
        h("div", { class: "cv-tools" }, presetSeg.el, h("span", { class: "cv-label", text: "Compare with" }), compareSel, h("div", { class: "seg", id: "cv-opts", role: "group", "aria-label": "Chart options" }, bandBtn, deltaBtn)),
        legend, chartHost, h("p", { class: "cv-note" }, stockPill, baseNote), unreach);
      const tableSeg = segmented([{ value: "v", label: "Volts" }, { value: "delta", label: "Δ vs baseline (mV)" }], "v", (v) => { cv.tableDelta = v === "delta"; render(); }, { label: "Table values" });
      const tableNote = h("p", { class: "cv-note", id: "cv-table-note" });
      const thead = h("thead");
      const tbody = h("tbody");
      const tableWrap = h("div", { class: "cv-wrap", id: "cv-table-wrap", hidden: true },
        h("div", { class: "cv-tools" }, tableSeg.el, tableNote),
        h("div", { class: "cv-scroll" }, h("table", { class: "tbl vf-table", id: "cv-table" }, h("caption", { class: "muted", hidden: true, text: "Measured voltage per unit and ratio" }), thead, tbody)));
      const libEmpty = h("p", { class: "dcard-note", id: "cv-lib-empty", text: "No stored curves yet. Run a Quick measurement to keep the first one." });
      const libBody = h("tbody", { id: "cv-lib-body" });
      const libTable = h("table", { class: "tbl lib-table", id: "cv-lib", hidden: true }, h("thead", {}, h("tr", {}, ...["Name", "Measured", "Mode", "Tuning", "Units", "Actions"].map((t, i) => h("th", { class: i === 4 ? "n" : "", text: t })))), libBody);
      const libWrap = h("div", { class: "cv-wrap", id: "cv-lib-wrap", hidden: true }, libEmpty, h("div", { class: "cv-scroll" }, libTable));
      const curves = h("section", { class: "dcard", id: "cv-card" },
        h("div", { class: "cv-bar" }, viewSeg.el, h("div", { class: "cv-pick" }, h("span", { class: "cv-label", text: "Curve" }), primarySel)),
        chartWrap, tableWrap, libWrap);

      root.append(h("div", { class: "sc curves" }, h("header", { class: "screen-head" }, h("h1", { text: "Curves" }), h("span", { class: "sc-meta", text: "measured V/F curves: what the CPU really requests at each ratio" })), measure, curves));

      // ---- painting ----
      let selectKey = "";
      function renderSelects() {
        const opts = [];
        if (cv.live) opts.push({ v: "live", t: "Live measurement" });
        for (const c of cv.list) opts.push({ v: c.id, t: curveTitle(c) + (c.baseline ? " · baseline" : "") });
        if (!opts.length) opts.push({ v: "", t: "No stored curves" });
        const others = cv.list.filter((c) => c.id !== cv.primary);
        const key = JSON.stringify([opts, others.map((c) => [c.id, curveTitle(c)])]);
        if (key !== selectKey) { // rebuilt only when the list changed: a repaint must not close an open drop-down
          selectKey = key;
          clear(primarySel).append(...opts.map((o) => h("option", { value: o.v, text: o.t })));
          clear(compareSel).append(h("option", { value: "", text: "None" }), ...others.map((c) => h("option", { value: c.id, text: curveTitle(c) })));
        }
        primarySel.value = cv.live ? "live" : cv.primary || "";
        primarySel.disabled = !cv.list.length && !cv.live;
        compareSel.value = cv.compare && others.some((c) => c.id === cv.compare) ? cv.compare : "";
        compareSel.disabled = others.length === 0 || cv.live !== null;
      }
      function renderLegend() {
        const drawn = ctl.drawnSet();
        clear(legend).append(...ids.map((u) => {
          const b = h("button", { type: "button", class: ["lg-chip", `u-${u}`], dataset: { unit: u }, title: `${unitTitle(m, u)}. Press to toggle; Shift+press shows only this one.`, "aria-pressed": String(drawn.has(u)) }, h("span", { class: "sw" }), u + favMark(m, u));
          b.addEventListener("click", (e) => {
            const next = new Set(drawn);
            if (e.shiftKey) {
              next.clear();
              next.add(u);
            } else if (next.has(u)) next.delete(u);
            else next.add(u);
            cv.drawn = next;
            render();
          });
          return b;
        }));
      }
      function renderTable() {
        const a = ctl.primaryRec();
        const baseRec = baselineId(cv.list) ? cv.cache[baselineId(cv.list)] || null : null;
        const tm = tableModel(a, baseRec, { delta: cv.tableDelta, ids, live: cv.live !== null, primaryIsBaseline: cv.primary === baselineId(cv.list) });
        if (cv.tableDelta && !tm.canDelta) cv.tableDelta = false;
        tableSeg.set(tm.delta ? "delta" : "v");
        const dBtn = tableSeg.el.querySelector("[data-value='delta']");
        dBtn.disabled = !tm.canDelta;
        dBtn.title = tm.canDelta ? "Show mV difference to the stock baseline" : baseRec ? "This is the baseline curve itself" : "Mark one stored curve as the stock baseline in the Library first";
        if (!a) {
          clear(thead);
          clear(tbody).append(h("tr", {}, h("td", { class: "na", colspan: 8, text: tm.empty })));
          tableNote.textContent = "";
          return;
        }
        clear(thead).append(h("tr", {}, h("th", { scope: "col", text: "Unit" }), ...tm.ratios.map((r) => h("th", { scope: "col", class: "n", text: `${r}${TIMES}` }))));
        clear(tbody).append(...tm.rows.map((row) => h("tr", {}, h("th", { scope: "row", text: row.unit }), ...row.cells.map((c) => h("td", { class: ["n", c.cls], title: c.title, text: c.text })))));
        tableNote.textContent = tm.note;
      }
      function renderLibrary() {
        libEmpty.hidden = cv.list.length > 0;
        libTable.hidden = cv.list.length === 0;
        clear(libBody).append(...cv.list.map((c) => {
          const r = libraryRow(c, { primary: cv.primary });
          const nameCell = h("td", { class: "lib-name", title: c.id });
          if (cv.renaming === c.id) {
            const inp = h("input", { type: "text", class: "inline-edit", maxlength: 64, "aria-label": `New name for ${r.name}` });
            inp.value = cv.renameText === null ? c.name || "" : cv.renameText; // a repaint must not throw away what was typed
            inp.addEventListener("input", () => { cv.renameText = inp.value; });
            const commit = () => ctl.rename(c.id, inp.value);
            const stop = () => { cv.renaming = null; cv.renameText = null; render(); };
            inp.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); else if (e.key === "Escape") stop(); });
            nameCell.append(inp, h("button", { type: "button", class: "btn primary", dataset: { act: "save" }, text: "Save", on: { click: commit } }), h("button", { type: "button", class: "btn", text: "Cancel", on: { click: stop } }));
            setTimeout(() => inp.focus(), 0);
          } else {
            nameCell.append(h("span", { class: "lib-title", text: r.name }));
            if (r.baseline) nameCell.append(pill("stock baseline", "ok"));
            if (r.partial) nameCell.append(pill("partial", "warn"));
          }
          const act = h("td", { class: "lib-actions" });
          const btn = (label, title, fn, extra = {}) => act.append(h("button", { type: "button", class: ["btn", extra.cls], title, dataset: { act: extra.act || label.toLowerCase() }, text: label, on: { click: fn } }));
          btn("View", "Show this curve in the chart", () => { cv.view = "chart"; ctl.select(c.id); render(); });
          btn("Compare", "Overlay this curve on the one in the chart", () => { if (c.id !== cv.primary) { cv.view = "chart"; ctl.setCompare(c.id); render(); } });
          btn(c.baseline === true ? "Clear baseline" : "Set baseline", "A stock baseline is what the table can subtract (one at a time)", () => ctl.toggleBaseline(c), { act: "baseline" });
          btn("Rename", "Rename this curve", () => { cv.renaming = c.id; cv.renameText = null; render(); });
          btn("Export", "Copy this curve as JSON to the clipboard", async () => {
            const text = await ctl.recordJson(c);
            if (!text) view.status = "Could not load the curve to export it.";
            else view.status = (await copyText(text)) ? `Copied ${exportName(c)} (${text.length} characters of JSON) to the clipboard.` : "The clipboard is not available here; use Download.";
            ctl.paint();
          });
          btn("Download", "Save this curve as a JSON file (needs a webview that allows downloads)", async () => {
            const text = await ctl.recordJson(c);
            if (!text) {
              view.status = "Could not load the curve to export it.";
              return ctl.paint();
            }
            const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
            const a = h("a", { href: url, download: exportName(c) });
            document.body.append(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 2000);
            view.status = `Download of ${exportName(c)} started.`;
            ctl.paint();
          });
          if (cv.confirmDelete === c.id) {
            act.append(h("span", { class: "muted", text: `Delete "${r.name}"? This cannot be undone.` }));
            btn("Confirm delete", "Remove this stored curve", () => ctl.remove(c), { cls: "danger", act: "confirm-delete" });
            btn("Keep", "Do not delete", () => { cv.confirmDelete = null; render(); }, { act: "keep" });
          } else btn("Delete", "Delete this stored curve", () => { cv.confirmDelete = c.id; render(); }, { cls: "danger" });
          return h("tr", { class: r.current ? "is-current" : "", dataset: { id: c.id } },
            nameCell, h("td", { text: r.created }), h("td", { text: r.mode }), h("td", {}, pill(r.tuning.text, r.tuning.tone, r.tuning.title)), h("td", { class: "n", text: String(r.units) }), act);
        }));
      }
      function syncTopology() {
        const next = unitModel(store.get().preflight);
        if (topoOf(next) === topoKey) return;
        topoKey = topoOf(next);
        m = next;
        ids = next.ids;
        presetList = presets(m);
        const fresh = buildPicker();
        picker.el.replaceWith(fresh.el);
        picker = fresh;
        const seg = buildPresets();
        presetSeg.el.replaceWith(seg.el);
        presetSeg = seg;
      }

      function render() {
        syncTopology();
        const running = !!view.run;
        const gate = ctl.gate();
        const n = (cv.sel ? ids.filter((u) => cv.sel.has(u)) : ids).length;
        modeSeg.set(cv.mode);
        for (const b of modeSeg.el.children) b.disabled = running;
        nameInput.disabled = running;
        picker.set(cv.sel || new Set(ids));
        picker.setDisabled(running);
        measureBtn.disabled = running || !!gate || n === 0;
        cancelBtn.disabled = !running || view.cancelling;
        est.textContent = measureText(cv, ids, { running, gate });
        turbo.hidden = !cv.noTurbo;
        run.hidden = !running;
        if (view.progress) {
          bar.max = view.progress.of;
          bar.value = view.progress.step;
          progress.textContent = view.progress.text;
        }
        status.textContent = view.listProblem || view.status;
        problems.hidden = view.problems.length === 0;
        clear(problems).append(...view.problems.map((x) => h("li", { text: x })));
        viewSeg.set(cv.view);
        chartWrap.hidden = cv.view !== "chart";
        tableWrap.hidden = cv.view !== "table";
        libWrap.hidden = cv.view !== "library";
        renderSelects();
        presetSeg.set(presetOf(presetList, ctl.drawnSet()));
        bandBtn.setAttribute("aria-pressed", String(cv.band));
        deltaBtn.setAttribute("aria-pressed", String(cv.delta));
        deltaBtn.disabled = !cv.compare;
        deltaBtn.title = cv.compare ? "Plot the difference between the two curves, in mV" : "Choose a second curve to compare with first";
        if (cv.view === "chart") {
          renderLegend();
          chart.render(ctl.model());
          const notes = curveNotes({ a: ctl.primaryRec(), list: cv.list, live: cv.live !== null, baseline: baselineId(cv.list), drawn: ctl.drawnSet(), ids, wordsRead: !!cv.words });
          baseNote.textContent = notes.base;
          stockPill.hidden = !notes.stock.show;
          stockPill.textContent = notes.stock.text;
          stockPill.title = notes.stock.title;
          unreach.textContent = notes.unreach;
        } else if (cv.view === "table") renderTable();
        else renderLibrary();
      }

      // vf.point events arrive many times a second on a dense run: repaint at most once per frame
      let queued = false;
      const paintSoon = () => {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => {
          queued = false;
          if (root.isConnected) render();
        });
      };
      subs.push(() => chart.destroy());
      subs.push(ctl.subscribe(paintSoon));
      // heartbeats replace store.helperOp several times a second: repaint only when what the screen shows from it (the gate text, the topology) changed
      let lastGate = ctl.gate();
      subs.push(store.subscribe((s, prev) => {
        const gate = ctl.gate();
        if (s.link !== prev.link || s.preflight !== prev.preflight || gate !== lastGate) {
          lastGate = gate;
          paintSoon();
        }
      }));
      render();
      if (store.get().link.state === "connected") {
        if (!cv.loaded) ctl.loadCurves(false);
        if (!cv.words) ctl.loadWords();
      }
    },
    unmount() {
      subs.splice(0).forEach((u) => u());
    },
  };
}
