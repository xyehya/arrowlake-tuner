// units.js: the units model of a tune.read result (G-207). Ported from web/app.js normWords / fusedTopOf / stockMaxOf / ocClass / runsUpTo (G-112, G-125, G-133, G-150):
// web/app.js:1156-1234 (units), 2664-2745 (curPts, OC model), 3086-3112 (OC ratio window), 3619-3680 (normPoint, normWords).
//
// Units: P0..P7 (kind "P"), EC0..EC3 (kind "E", four CPUs sharing one ratio), and the ring (id "ring", kind "R", package-level; its points are keyed by the P2 point NUMBER,
// not by ratio). `modelFromRead(result)` is pure and total: a missing piece (fused words, turbo tables, ring, limits) becomes null, never a guess; the editors then disable.
// Hardware rules this model carries (docs/knowledge/platform-arl-s.md §3): fused points take offsets only; the OC point is the P2 = 0 word (ratio + adaptive target) and cannot be
// cleared at runtime; an OC ratio is any ratio above a unit's stock maximum; E clusters share one ratio.
import { isNum } from "./format.js";
import { limitsFor } from "./limits.js";
import { normCpuLimits } from "./cpulimits.js";

export const RING_ID = "ring";
/** Offsets compare equal when they are the same 1/1024 V raw word (the hardware cannot tell -19.5 from -20 mV); voltages within a quarter of a raw step. */
export const MV_TOL = 0.05;
export const V_TOL = 0.0004;
export const sameMv = (a, b) => isNum(a) && isNum(b) && Math.round(a * 1.024) === Math.round(b * 1.024);
export const sameV = (a, b) => isNum(a) && isNum(b) && Math.abs(a - b) <= V_TOL;

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

export function unitKind(id) {
  return id === RING_ID ? "R" : String(id).charAt(0) === "P" ? "P" : "E";
}
export function unitLabel(id) {
  const k = unitKind(id);
  return k === "R" ? "Ring" : k === "P" ? "P-core " + id.slice(1) : "E-cluster " + id.slice(2);
}

/** Aggregate of numbers: {state:"none"} (no value), {state:"same", value}, or {state:"mixed", min, max, count}. `eq` decides sameness against the first value. */
export function aggregate(values, eq = (a, b) => a === b) {
  const vs = values.filter(isNum);
  if (!vs.length) return { state: "none" };
  if (vs.every((v) => eq(v, vs[0]))) return { state: "same", value: vs[0] };
  return { state: "mixed", min: Math.min(...vs), max: Math.max(...vs), count: vs.length };
}

// ---- reading a tune.read result ------------------------------------------------------------------------------------------------------------
function normPoint(p, fallbackNo) {
  if (!isObj(p)) return null;
  return {
    point: isNum(p.point) ? p.point : fallbackNo,
    ratio: isNum(p.ratio) && p.ratio > 0 ? p.ratio : null,
    offset_mv: isNum(p.offset_mv) ? p.offset_mv : null,
    v_target_v: isNum(p.v_target_v) && p.v_target_v > 0 ? p.v_target_v : null, // a field that reads 0 is unset
    word: typeof p.word === "string" ? p.word : null,
    oc_point: p.oc_point === true,
  };
}

/** [point 0 = the OC word, point 1..n] of a cpus[]-shaped entry: the decoded `tune` copy when present (points with point 0), else the raw {oc, points}. */
function pointsOf(decodedCpu, rawCpu) {
  if (isObj(decodedCpu) && Array.isArray(decodedCpu.points)) return decodedCpu.points.map((p, i) => normPoint(p, i)).filter(Boolean);
  if (!isObj(rawCpu)) return null;
  const oc = isObj(rawCpu.oc) ? [normPoint({ ...rawCpu.oc, point: 0 }, 0)] : [];
  const pts = Array.isArray(rawCpu.points) ? rawCpu.points.map((p, i) => normPoint(p, i + 1)) : [];
  return [...oc, ...pts].filter(Boolean);
}

function buildUnit(id, kind, info, pts, fused, favored) {
  const ocWord = pts.find((p) => p.point === 0) || null;
  const body = pts.filter((p) => p.point !== 0);
  const ft = fused && isNum(fused.ratio) ? fused.ratio : null;
  // The OC point is the helper's `oc_point` flag; a raw read without it: any point above the fused top (G-125). The ring has no fused top: all its points are editable.
  const isOc = (p) => kind !== "R" && (p.oc_point === true || (ft !== null && isNum(p.ratio) && p.ratio > ft));
  const fusedPts = body.filter((p) => !isOc(p) && isNum(p.ratio) && isNum(p.offset_mv));
  const ocPt = body.find(isOc) || null;
  const ocRatio = ocWord && ocWord.ratio !== null ? ocWord.ratio : null;
  const unit = {
    id,
    kind,
    label: unitLabel(id),
    sub: info.cpus.length === 1 ? "cpu " + info.cpus[0] : info.cpus.length ? "cpus " + info.cpus[0] + "–" + info.cpus[info.cpus.length - 1] : "",
    cpus: info.cpus,
    cpu: info.cpu,
    favored: favored,
    fused: fused,
    points: fusedPts.map((p) => ({ point: p.point, ratio: p.ratio, offset_mv: p.offset_mv, word: p.word, ocSlot: false })),
    oc: ocWord ? { ratio: ocRatio, v_target_v: ocWord.v_target_v, offset_mv: isNum(ocWord.offset_mv) ? ocWord.offset_mv : 0, word: ocWord.word } : null,
    ocPoint: ocPt ? { point: ocPt.point, ratio: ocPt.ratio, offset_mv: ocPt.offset_mv } : null, // the point read after the table (P 9 / E 7), when the unit has an OC ratio
  };
  if (kind === "R" && unit.points.length > 1 && ocRatio !== null) {
    const last = unit.points[unit.points.length - 1];
    if (last.ratio === ocRatio) last.ocSlot = true; // ring P2 = 9 is the OC ratio's slot: an offset written there moves it [MEASURED lnx 2026-10-15]
  }
  return unit;
}

const tb = (o) => (isObj(o) && Array.isArray(o.ratios) && o.ratios.length === 8 && o.ratios.every(isNum) ? { ratios: o.ratios.slice(), counts: Array.isArray(o.counts) ? o.counts.filter(isNum) : null } : null);

/**
 * tune.read result -> model, or null when it holds no unit. Model:
 *   order [ids], units {id: unit}, turbo {P:{ratios,counts}, E:{...}, favored:{unit: ratio}} | null, cpuLimits (the CPU's own limits, lib/cpulimits.js),
 *   features {ring: bool} (from `intent_features`: the ring field is only built when the helper says it understands it), readAt, complete, simulator.
 */
export function modelFromRead(result) {
  if (!isObj(result) || !Array.isArray(result.units)) return null;
  const tune = isObj(result.tune) ? result.tune : {};
  const fusedByCpu = isObj(result.fused) ? result.fused : {};
  const unitOfCpu = {};
  const infos = [];
  for (const u of result.units) {
    if (!isObj(u) || !Array.isArray(u.cpus) || !isObj(u.cpus[0])) continue;
    const kind = u.kind === "P" || u.kind === "E" ? u.kind : null;
    const first = u.cpus[0].cpu;
    const id = typeof u.id === "string" ? u.id : kind === "P" ? "P" + first : kind === "E" && isNum(u.cluster) ? "EC" + u.cluster : null;
    if (!id || unitKind(id) === "R") continue;
    const cpus = Array.isArray(u.cluster_cpus) && u.cluster_cpus.every(isNum) ? u.cluster_cpus.slice() : u.cpus.map((c) => c.cpu).filter(isNum);
    for (const c of cpus) unitOfCpu[String(c)] = id;
    infos.push({ id, info: { cpus, cpu: first }, raw: u.cpus[0] });
  }
  const favRaw = isObj(result.turbo) && isObj(result.turbo.favored) ? result.turbo.favored : {};
  const favoredBy = {};
  for (const k of Object.keys(favRaw)) {
    const id = unitOfCpu[k];
    if (id && unitKind(id) === "P" && isNum(favRaw[k])) favoredBy[id] = favRaw[k];
  }
  const units = {};
  const order = [];
  for (const { id, info, raw } of infos) {
    const decoded = isObj(tune[id]) && Array.isArray(tune[id].cpus) ? tune[id].cpus[0] : null;
    const pts = pointsOf(decoded, raw);
    if (!pts) continue;
    const f = fusedByCpu[String(info.cpu)];
    const fused = isObj(f) && isNum(f.ratio) && isNum(f.v) ? { ratio: f.ratio, v: f.v, word: typeof f.word === "string" ? f.word : null } : null;
    units[id] = buildUnit(id, unitKind(id), info, pts, fused, isNum(favoredBy[id]) ? favoredBy[id] : null);
    order.push(id);
  }
  order.sort((a, b) => (unitKind(a) === unitKind(b) ? Number(a.replace(/\D/g, "")) - Number(b.replace(/\D/g, "")) : unitKind(a) === "P" ? -1 : 1));
  if (!order.length) return null;
  if (isObj(result.ring)) {
    const pts = pointsOf(null, { oc: result.ring.oc, points: result.ring.points });
    if (pts && pts.some((p) => p.point > 0)) {
      units[RING_ID] = buildUnit(RING_ID, "R", { cpus: [], cpu: isNum(result.ring.cpu) ? result.ring.cpu : null }, pts, null, null);
      units[RING_ID].sub = "cache / ring";
      order.push(RING_ID);
    }
  }
  const tables = isObj(result.tables) && Array.isArray(result.tables.p) && Array.isArray(result.tables.e) ? result.tables : null;
  let turbo = null;
  if (isObj(result.turbo) && tb(result.turbo.P) && tb(result.turbo.E)) turbo = { P: tb(result.turbo.P), E: tb(result.turbo.E), favored: favoredBy };
  else if (tables && tables.p.length === 8 && tables.e.length === 8) turbo = { P: { ratios: tables.p.slice(), counts: null }, E: { ratios: tables.e.slice(), counts: null }, favored: favoredBy };
  const features = Array.isArray(result.intent_features) ? result.intent_features.filter((x) => typeof x === "string") : [];
  return {
    order,
    units,
    turbo,
    cpuLimits: normCpuLimits(result.cpu_limits),
    intentFeatures: features,
    features: { ring: features.includes("ring") && !!units[RING_ID] },
    readAt: typeof result.read_at === "string" ? result.read_at : null,
    complete: result.complete !== false,
    simulator: result.simulator === true,
  };
}

// ---- selection ---------------------------------------------------------------------------------------------------------------------------
export const idsOfKind = (model, kind) => model.order.filter((id) => unitKind(id) === kind);

/** The P-cores running the highest favored ratio, only when the cores differ (`[]` otherwise). */
export function favoredIds(model) {
  const fav = model.turbo ? model.turbo.favored : {};
  const ps = idsOfKind(model, "P").filter((id) => isNum(fav[id]));
  if (!ps.length) return [];
  const top = Math.max(...ps.map((id) => fav[id]));
  const at = ps.filter((id) => fav[id] === top);
  return at.length === ps.length ? [] : at;
}

/** The quick chips: All P, All E, Favored, All (the ring is never in "All": it is a different kind of unit and is selected on its own). */
export function quickSelections(model) {
  const P = idsOfKind(model, "P");
  const E = idsOfKind(model, "E");
  return { P, E, favored: favoredIds(model), all: [...P, ...E] };
}

/**
 * A selection as editor sections, one per kind present (P and E selected together are two sections: their points differ).
 *   P/E: {kind, ids, ratios: sorted union of the units' fused point ratios, shared: ratios every unit of the section has}
 *   R:   {kind:"R", ids:["ring"], points: sorted point numbers}
 */
export function sections(model, ids) {
  const out = [];
  for (const kind of ["P", "E", "R"]) {
    const us = ids.filter((id) => unitKind(id) === kind && model.units[id]);
    if (!us.length) continue;
    if (kind === "R") {
      out.push({ kind, ids: us, points: model.units[RING_ID].points.map((p) => p.point) });
      continue;
    }
    const sets = us.map((id) => new Set(model.units[id].points.map((p) => p.ratio)));
    const all = [...new Set(sets.flatMap((s) => [...s]))].sort((a, b) => a - b);
    out.push({ kind, ids: us, ratios: all, shared: all.filter((r) => sets.every((s) => s.has(r))) });
  }
  return out;
}

export const pointAt = (unit, ratio) => (unit ? unit.points.find((p) => p.ratio === ratio) || null : null);
export const ringPoint = (unit, no) => (unit ? unit.points.find((p) => p.point === no) || null : null);
/** The selected units that have a fused point at `ratio` (an edit on a point ratio applies to exactly these, PLAN §4.2). */
export const unitsWithPoint = (model, ids, ratio) => ids.filter((id) => unitKind(id) !== "R" && pointAt(model.units[id], ratio));

// ---- the OC model (G-133 / G-150) --------------------------------------------------------------------------------------------------------
/** Ratio of the top fused point (0x09); the highest read point when the fused word was not read; null = not read, nothing is claimed. */
export function fusedTopOf(unit) {
  if (!unit || unit.kind === "R") return null;
  if (unit.fused && isNum(unit.fused.ratio)) return unit.fused.ratio;
  const rs = unit.points.map((p) => p.ratio);
  return rs.length ? Math.max(...rs) : null;
}
/** The highest ratio a unit runs without an OC ratio: a P-core's own 0x1C ratio capped at the fused top; an E-cluster's fused top. Null = not read. */
export function stockMaxOf(unit) {
  const ft = fusedTopOf(unit);
  if (!unit || ft === null) return null;
  if (unit.kind !== "P") return ft;
  return isNum(unit.favored) ? Math.min(unit.favored, ft) : null;
}
/** "none" (no OC ratio) | "middle" (above stock, on a fused point: offset only) | "oc" (above the fused top: creates the OC point) | "unknown". */
export function ocClass(unit, ratio) {
  if (!isNum(ratio) || ratio <= 0) return "none";
  const sm = stockMaxOf(unit);
  const ft = fusedTopOf(unit);
  if (sm === null || ft === null) return "unknown";
  return ratio <= sm ? "none" : ratio <= ft ? "middle" : "oc";
}
/** The highest ratio a unit runs now: the turbo table's top bucket, for a P-core also bounded by its own 0x1C ratio or a configured OC ratio. Null when not read. */
export function runsUpTo(model, unit) {
  const t = model.turbo ? model.turbo[unit.kind === "P" ? "P" : "E"] : null;
  if (!t || !t.ratios.length) return null;
  if (unit.kind !== "P") return t.ratios[0];
  const sm = stockMaxOf(unit);
  return sm === null ? null : Math.min(t.ratios[0], Math.max(sm, unit.oc && isNum(unit.oc.ratio) ? unit.oc.ratio : 0));
}
/** A unit that runs above its fused top with no OC point (the BIOS raised the max ratio / the E turbo table): {ft, max}, else null. */
export function aboveTop(model, unit, ratio) {
  const ft = fusedTopOf(unit);
  const max = runsUpTo(model, unit);
  const cls = ocClass(unit, ratio);
  return isNum(ft) && isNum(max) && max > ft && (cls === "none" || cls === "middle") ? { ft, max } : null;
}

/** Choosable OC ratios for a set of units: stock max (highest of the group) + 1 ... the effective ratio ceiling. `reason` says why the range is empty or unknown. */
export function ocRatioWindow(model, ids) {
  const us = ids.map((id) => model.units[id]).filter((u) => u && u.kind !== "R");
  if (!us.length) return { min: null, max: null, reason: "no unit selected" };
  const kinds = [...new Set(us.map((u) => u.kind))];
  if (kinds.length > 1) return { min: null, max: null, reason: "P and E have separate OC ratios: edit them one section at a time" };
  const lim = limitsFor(model, kinds[0]);
  const sms = us.map(stockMaxOf);
  if (!lim.known) return { min: null, max: null, reason: "the helper did not report the CPU limits" };
  if (sms.some((x) => x === null)) return { min: null, max: null, reason: "this unit's stock ratio (0x1C) was not read" };
  if (!lim.ratio) return { min: null, max: null, reason: lim.ratioWhy };
  const min = Math.max(...sms) + 1;
  if (lim.ratio.max < min) return { min, max: lim.ratio.max, reason: "OC ratios above " + lim.ratio.max + "× are not allowed (" + lim.ratio.source + ")" };
  return { min, max: lim.ratio.max, reason: null, source: lim.ratio.source };
}

/** Lowest ratio the turbo table of a kind may hold: the lowest fused point ratio of that kind's units (read live), never a literal. */
export function turboFloor(model, kind) {
  const rs = idsOfKind(model, kind).flatMap((id) => model.units[id].points.map((p) => p.ratio)).filter(isNum);
  return rs.length ? Math.min(...rs) : null;
}
