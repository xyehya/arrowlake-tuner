// draft.js: the pending edits over a units model (G-207). Ported from web/app.js T.draft / setCurve / setPoint / setVt / setRatio / setOcOffset / setBucket / unitsChanged:
// web/app.js:2655-2690 (effective values, aggregates), 2847-2944 (turbo, editing, 48x offset), 3086-3112 (ratio options).
//
// A draft is plain data and every edit returns a NEW draft (store-friendly, no mutation):
//   { units: { <id>: { curveMv?, pts: { <ratio | ring point no>: mV }, ratio?, vt?, vtAuto? } }, turbo: { P?: [8 ratios], E?: [8 ratios] } }
// An edit equal to what was read is dropped (offsets compare as the 1/1024 V raw word), so "pending" always means "differs from the CPU". Multi-unit edits (PLAN §4.2): an edit on
// point ratio R applies to every selected unit that HAS a point at R and to no other; P and E selected together are two sections. Nothing here talks to the helper.
import { isNum, fmtMvNum } from "./format.js";
import { limitsFor, clampOffset, vtWindow, snapGrid } from "./limits.js";
import {
  RING_ID, unitKind, sameMv, sameV, aggregate, pointAt, ringPoint, unitsWithPoint, fusedTopOf, ocClass, aboveTop, ocRatioWindow, turboFloor,
} from "./units.js";

export const emptyDraft = () => ({ units: {}, turbo: {} });
export const isEmpty = (draft) => !Object.keys(draft.units).length && !Object.keys(draft.turbo).length;

function clone(draft) {
  const units = {};
  for (const [id, d] of Object.entries(draft.units)) units[id] = { ...d, pts: { ...d.pts } };
  const turbo = {};
  for (const [k, t] of Object.entries(draft.turbo)) turbo[k] = t.slice();
  return { units, turbo };
}
const ensure = (next, id) => next.units[id] || (next.units[id] = { pts: {} });
function tidy(next, id) {
  const d = next.units[id];
  if (d && d.curveMv === undefined && !Object.keys(d.pts).length && d.vt === undefined && d.ratio === undefined) delete next.units[id];
}
const liveIds = (model, ids) => ids.filter((id) => model.units[id]);

// ---- effective (draft over read) and current values -----------------------------------------------------------------------------------------
export const curRatio = (unit) => (unit.oc && isNum(unit.oc.ratio) && unit.oc.ratio > 0 ? unit.oc.ratio : null);
export const curVt = (unit) => (unit.oc && isNum(unit.oc.v_target_v) ? unit.oc.v_target_v : null);

/** The offset of a fused point (ratio-keyed for P/E, point-number-keyed for the ring) with the draft applied. */
export function effPointMv(draft, unit, p) {
  const d = draft.units[unit.id];
  const key = unit.kind === "R" ? p.point : p.ratio;
  if (d && isNum(d.curveMv)) return d.curveMv;
  if (d && d.pts[key] !== undefined) return d.pts[key];
  return p.offset_mv;
}
export function effRatio(draft, unit) {
  const d = draft.units[unit.id];
  return d && d.ratio !== undefined ? d.ratio : curRatio(unit);
}
export function effVt(draft, unit) {
  const d = draft.units[unit.id];
  return d && d.vt !== undefined ? d.vt : curVt(unit);
}
export const turboCur = (model, kind) => (model.turbo && model.turbo[kind] ? model.turbo[kind].ratios : null);
export const turboEff = (model, draft, kind) => draft.turbo[kind] || turboCur(model, kind);

// ---- aggregates for the editors ("mixed") ---------------------------------------------------------------------------------------------------
const mvAgg = (values) => aggregate(values, sameMv);

/** Whole-curve value over every point of the P/E units in `ids`: read (eff false) or as edited. */
export function curveValue(model, draft, ids, eff = true) {
  const vals = liveIds(model, ids).filter((id) => unitKind(id) !== "R").flatMap((id) => model.units[id].points.map((p) => (eff ? effPointMv(draft, model.units[id], p) : p.offset_mv)));
  return mvAgg(vals);
}
/** Value of the point at `ratio` over the selected units that have it: {agg, ids (the units it applies to), pending}. */
export function pointValue(model, draft, ids, ratio, eff = true) {
  const us = unitsWithPoint(model, liveIds(model, ids), ratio);
  const read = mvAgg(us.map((id) => pointAt(model.units[id], ratio).offset_mv));
  const now = mvAgg(us.map((id) => effPointMv(draft, model.units[id], pointAt(model.units[id], ratio))));
  return { agg: eff ? now : read, ids: us, pending: us.some((id) => !sameMv(pointAt(model.units[id], ratio).offset_mv, effPointMv(draft, model.units[id], pointAt(model.units[id], ratio)))) };
}
export function ringValue(model, draft, no, eff = true) {
  const u = model.units[RING_ID];
  const p = ringPoint(u, no);
  if (!p) return { agg: { state: "none" }, ids: [], pending: false };
  const now = effPointMv(draft, u, p);
  return { agg: { state: "same", value: eff ? now : p.offset_mv }, ids: [RING_ID], pending: !sameMv(p.offset_mv, now) };
}
export function ocRatioValue(model, draft, ids, eff = true) {
  return aggregate(liveIds(model, ids).filter((id) => unitKind(id) !== "R").map((id) => (eff ? effRatio(draft, model.units[id]) : curRatio(model.units[id]))));
}
export function vtValue(model, draft, ids, eff = true) {
  return aggregate(liveIds(model, ids).filter((id) => unitKind(id) !== "R").map((id) => (eff ? effVt(draft, model.units[id]) : curVt(model.units[id]))), sameV);
}

// ---- editing --------------------------------------------------------------------------------------------------------------------------------
/** Whole-curve offset for the P/E units of `ids`. `mv` is rounded to a whole mV and clamped into the window of each unit's kind; a kind with no window is left alone. */
export function setCurve(model, draft, ids, mv) {
  const next = clone(draft);
  for (const id of liveIds(model, ids)) {
    const unit = model.units[id];
    const v = clampOffset(model, unit.kind, mv);
    if (v === null || (unit.kind === "R" && !model.features.ring)) continue;
    const d = ensure(next, id);
    d.pts = {};
    delete d.curveMv;
    if (unit.kind === "R") {
      for (const p of unit.points) if (!sameMv(p.offset_mv, v)) d.pts[p.point] = v; // the ring intent has no whole-curve field: expand to its points
    } else if (!(unit.points.length && unit.points.every((p) => sameMv(p.offset_mv, v)))) d.curveMv = v;
    tidy(next, id);
  }
  return next;
}

/** One point (by ratio) on every selected P/E unit that has it. Leaving whole-curve editing spreads the curve value over the points first. */
export function setPoint(model, draft, ids, ratio, mv) {
  const next = clone(draft);
  for (const id of unitsWithPoint(model, liveIds(model, ids), ratio)) {
    const unit = model.units[id];
    const v = clampOffset(model, unit.kind, mv);
    if (v === null) continue;
    const d = ensure(next, id);
    if (isNum(d.curveMv)) {
      for (const p of unit.points) if (!sameMv(p.offset_mv, d.curveMv)) d.pts[p.ratio] = d.curveMv;
      delete d.curveMv;
    }
    if (sameMv(pointAt(unit, ratio).offset_mv, v)) delete d.pts[ratio];
    else d.pts[ratio] = v;
    tidy(next, id);
  }
  return next;
}

/** A ring point by P2 point number. Refused (draft unchanged) unless the helper reported the "ring" intent feature (G-212). */
export function setRingPoint(model, draft, no, mv) {
  const unit = model.units[RING_ID];
  if (!model.features.ring || !unit || !ringPoint(unit, no)) return draft;
  const v = clampOffset(model, "R", mv);
  if (v === null) return draft;
  const next = clone(draft);
  const d = ensure(next, RING_ID);
  if (sameMv(ringPoint(unit, no).offset_mv, v)) delete d.pts[no];
  else d.pts[no] = v;
  tidy(next, RING_ID);
  return next;
}

/** The adaptive target (V) of the OC point, snapped to the 5 mV grid unless `snap` is false. Only units whose effective ratio creates an OC point take one (D-035: a target without an OC point is not retained). */
export function setVt(model, draft, ids, volts, { snap = true } = {}) {
  const next = clone(draft);
  for (const id of liveIds(model, ids)) {
    const unit = model.units[id];
    if (unit.kind === "R" || ocClass(unit, effRatio(next, unit)) !== "oc") continue;
    const w = vtWindow(model, unit.kind);
    if (!w || !isNum(volts)) continue;
    const v = Math.round(Math.min(Math.max(snap ? snapGrid(volts) : volts, w.min), w.max) * 10000) / 10000;
    const d = ensure(next, id);
    if (sameV(curVt(unit), v)) delete d.vt;
    else d.vt = v;
    delete d.vtAuto;
    tidy(next, id);
  }
  return next;
}

/**
 * The OC ratio. `ratio` null = back to what was read. A ratio above a unit's top fused point creates the OC point, which needs a target: where the unit has none the draft takes the
 * fused top voltage (0x09) on the target grid, marked `vtAuto` (shown as changed; dropping the ratio takes it back). A ratio outside the unit's window is refused for that unit.
 */
export function setOcRatio(model, draft, ids, ratio) {
  const next = clone(draft);
  for (const id of liveIds(model, ids)) {
    const unit = model.units[id];
    if (unit.kind === "R") continue; // the ring OC word is never written (D-045)
    if (ratio !== null) {
      const w = ocRatioWindow(model, [id]);
      if (!Number.isInteger(ratio) || (ratio !== curRatio(unit) && (w.min === null || ratio < w.min || ratio > w.max))) continue;
    }
    const d = ensure(next, id);
    if (ratio === null || curRatio(unit) === ratio) delete d.ratio;
    else d.ratio = ratio;
    const cls = ocClass(unit, effRatio(next, unit));
    if (cls === "oc" && !(isNum(effVt(next, unit)) && effVt(next, unit) > 0)) {
      const w = vtWindow(model, unit.kind);
      if (unit.fused && isNum(unit.fused.v) && w) {
        d.vt = Math.round(Math.min(Math.max(snapGrid(unit.fused.v), w.min), w.max) * 10000) / 10000;
        d.vtAuto = true;
      }
    } else if (cls !== "oc" && d.vtAuto) {
      delete d.vt;
      delete d.vtAuto;
    }
    tidy(next, id);
  }
  return next;
}

/** G-208: back to what was read for the OC ratio AND its target of these units (a target with no OC ratio is not retained, D-035, so one is never left behind). */
export function clearOcEdits(model, draft, ids) {
  const next = clone(draft);
  for (const id of liveIds(model, ids)) {
    const d = next.units[id];
    if (!d) continue;
    delete d.ratio;
    delete d.vt;
    delete d.vtAuto;
    tidy(next, id);
  }
  return next;
}

// G-150 "48x offset": the OC ratio's voltage as an offset from the fused top point's voltage (0x09). A target below the top fused point is raised to it by the CPU
// [MEASURED lnx 2026-10-11, 2026-10-13], so offsets below the top point's own are not offered (clamped). Everything goes through setOcRatio / setVt.
function ocOffUnit(model, draft, unit) {
  const f = unit.fused;
  const ft = fusedTopOf(unit);
  const top = unit.points.find((p) => p.ratio === ft);
  const r = effRatio(draft, unit);
  const ab = aboveTop(model, unit, r);
  const ratio = ocClass(unit, r) === "oc" ? r : ab ? ab.max : null;
  return f && isNum(f.v) && top && isNum(ratio) ? { unit, f, ft, ratio, topMv: effPointMv(draft, unit, top) } : null;
}
/** The "OC offset" of a unit as the number XTU shows (target - fused V, mV), or null when it does not apply. */
export function ocOffsetOf(model, draft, id) {
  const unit = model.units[id];
  const c = unit && unit.kind !== "R" ? ocOffUnit(model, draft, unit) : null;
  const vt = c ? effVt(draft, unit) : null;
  return c && isNum(vt) ? Math.round((vt - c.f.v) * 1000) : null;
}
/** x (mV above the fused top voltage) or null = "same voltage as the top point". Units with no OC ratio above the fused top are skipped. */
export function setOcOffset(model, draft, ids, x) {
  let next = draft;
  for (const id of liveIds(model, ids)) {
    const unit = model.units[id];
    const c = unit.kind === "R" ? null : ocOffUnit(model, next, unit);
    if (!c) continue;
    const read = ocClass(unit, curRatio(unit)) === "oc";
    if (x === null && !read) {
      next = setOcRatio(model, next, [id], null);
      const d = next.units[id];
      if (d) {
        next = clone(next);
        delete next.units[id].vt;
        delete next.units[id].vtAuto;
        tidy(next, id);
      }
    } else {
      next = setOcRatio(model, next, [id], c.ratio);
      next = setVt(model, next, [id], c.f.v + Math.max(x === null ? c.topMv : x, c.topMv) / 1000, { snap: false }); // exact: fused V + x, as XTU's offset reads
    }
  }
  return next;
}

/** Turbo bucket i of a kind. Keeps the table non-increasing: a value above the bucket to its left is clamped, the buckets to its right come down with it. */
export function setBucket(model, draft, kind, i, value) {
  const cur = turboCur(model, kind);
  if (!cur || !Number.isInteger(i) || i < 0 || i >= cur.length || !isNum(value)) return draft;
  const lim = limitsFor(model, kind);
  const eff = turboEff(model, draft, kind).slice();
  const ceiling = i === 0 ? Math.max(cur[0], lim.ratio ? lim.ratio.max : 0) : eff[i - 1]; // no ceiling stated = no raise above what was read (G-133)
  const floor = turboFloor(model, kind) ?? 1;
  const v = Math.min(Math.max(Math.round(value), floor), ceiling);
  eff[i] = v;
  for (let j = i + 1; j < eff.length; j++) eff[j] = Math.min(eff[j], v);
  const next = clone(draft);
  if (eff.every((x, j) => x === cur[j])) delete next.turbo[kind];
  else next.turbo[kind] = eff;
  return next;
}

export function discardUnits(draft, ids) {
  const next = clone(draft);
  for (const id of ids) delete next.units[id];
  return next;
}
export function discardTurbo(draft, kind) {
  const next = clone(draft);
  delete next.turbo[kind];
  return next;
}

// ---- diff: what is pending ------------------------------------------------------------------------------------------------------------------
/**
 * Pending changes vs the read: {total, unitCount, perUnit: {id: n}, turbo: {P?: n, E?: n}, items: [{unit, field, key, from, to}]}.
 * Counts: the whole-curve offset is one change, each point one, the OC ratio one, the target one, each changed turbo bucket one. `unitCount` counts units + changed turbo tables.
 */
export function diff(model, draft) {
  const items = [];
  const perUnit = {};
  for (const id of model.order) {
    const d = draft.units[id];
    const unit = model.units[id];
    if (!d) continue;
    if (isNum(d.curveMv)) items.push({ unit: id, field: "curve", key: null, from: null, to: d.curveMv });
    for (const key of Object.keys(d.pts).sort((a, b) => a - b)) {
      const p = unit.kind === "R" ? ringPoint(unit, Number(key)) : pointAt(unit, Number(key));
      items.push({ unit: id, field: unit.kind === "R" ? "ring-point" : "point", key: Number(key), from: p ? p.offset_mv : null, to: d.pts[key] });
    }
    if (d.ratio !== undefined) items.push({ unit: id, field: "ratio", key: null, from: curRatio(unit), to: d.ratio });
    if (d.vt !== undefined) items.push({ unit: id, field: "vt", key: null, from: curVt(unit), to: d.vt });
    perUnit[id] = items.filter((x) => x.unit === id).length;
  }
  const turbo = {};
  for (const kind of ["P", "E"]) {
    const t = draft.turbo[kind];
    const cur = turboCur(model, kind);
    if (!t || !cur) continue;
    t.forEach((v, i) => v !== cur[i] && items.push({ unit: "turbo:" + kind, field: "bucket", key: i, from: cur[i], to: v }));
    turbo[kind] = items.filter((x) => x.unit === "turbo:" + kind).length;
  }
  const total = items.length;
  return { total, unitCount: Object.keys(perUnit).length + Object.keys(turbo).length, perUnit, turbo, items };
}

/** Hints that are not refusals: the editor shows them in place, the plan still decides. */
export function warnings(model, draft) {
  const out = [];
  for (const id of model.order) {
    const unit = model.units[id];
    if (unit.kind !== "E" || !draft.units[id]) continue;
    const p = pointAt(unit, 40);
    if (p && !sameMv(effPointMv(draft, unit, p), 0) && !sameMv(effPointMv(draft, unit, p), p.offset_mv)) {
      out.push({ code: "E40_OFFSET", unit: id, text: id + ": an offset on the 40x point (" + fmtMvNum(effPointMv(draft, unit, p)) + " mV) caused OCCT errors on one chip [MEASURED lnx 2026-10-02]; a hint from one CPU, not a limit" });
    }
  }
  return out;
}
