// predict.js: predicted voltage per V/F point from a baseline voltage + the (drafted) offset, with the hardware's V(n) >= V(n-1) clamp (G-207).
// Ported from web/app.js baseVid / predictUnit / predAt / predFlat (F4, G-133, G-150): web/app.js:2777-2846, 4391-4399.
//
// The tune.read alone knows ONE voltage per unit: the fused top point (0x09 word: ratio + volts). Lower points have no readable voltage, so without a baseline they are `null`
// (never guessed). A baseline is injected: `base(unitId, ratio)` -> {v, src} | null, or {unitId: {ratio: {v, src}}} (e.g. from a measured stock curve, src "baseline").
// Rules [MEASURED lnx 2026-10-02/03/11, platform-arl-s.md §3]: effective V(n) = max(base(n) + offset(n), V(n-1)); between points the voltage is linear; above the top point it is flat;
// from the OC point's ratio up a unit asks for its adaptive target (never below the top fused point's voltage).
import { isNum } from "./format.js";
import { effPointMv, effRatio, effVt } from "./draft.js";
import { fusedTopOf, ocClass } from "./units.js";

const baseOf = (base, unit, ratio) => {
  const own = typeof base === "function" ? base(unit.id, ratio) : base && base[unit.id] ? base[unit.id][ratio] : null;
  if (own && isNum(own.v)) return { v: own.v, src: own.src || "baseline" };
  return unit.fused && unit.fused.ratio === ratio && isNum(unit.fused.v) ? { v: unit.fused.v, src: "fused" } : null; // the fused top stands in where nothing else is known
};

/**
 * Per fused point, lowest ratio first: {ratio, point, base, src, want, v, clampedBy: {ratio, point} | null} or {ratio, point, v: null} (no baseline voltage).
 * `any` is false when nothing can be predicted. `want` = base + offset before the clamp, `v` after it.
 */
export function predictUnit(model, draft, id, base = null) {
  const unit = model.units[id];
  const out = { id, points: [], any: false };
  if (!unit) return out;
  let prev = null;
  for (const p of unit.points.slice().sort((a, b) => a.ratio - b.ratio)) {
    const b = baseOf(base, unit, p.ratio);
    if (!b) {
      out.points.push({ ratio: p.ratio, point: p.point, v: null });
      continue;
    }
    const want = b.v + effPointMv(draft, unit, p) / 1000;
    const clamped = prev !== null && want < prev.v - 1e-9;
    out.points.push({ ratio: p.ratio, point: p.point, base: b.v, src: b.src, want, v: clamped ? prev.v : want, clampedBy: clamped ? { ratio: prev.ratio, point: prev.point } : null });
    out.any = true;
    prev = { v: clamped ? prev.v : want, ratio: p.ratio, point: p.point };
  }
  return out;
}

/** Predicted point of `ratio` of a unit, or null. */
export const predictedAt = (pred, ratio) => pred.points.find((q) => q.ratio === ratio) || null;

/** Effective voltage at any ratio: linear between predicted points, flat below the first and above the top one (E 47-48x follow point 6); null with no prediction. */
export function voltageAt(model, draft, id, ratio, base = null) {
  const unit = model.units[id];
  const pred = predictUnit(model, draft, id, base);
  const pts = pred.points.filter((q) => q.v !== null);
  if (!unit || !pts.length) return null;
  const flat = (r) => {
    if (r <= pts[0].ratio) return pts[0].v;
    if (r >= pts[pts.length - 1].ratio) return pts[pts.length - 1].v;
    const i = pts.findIndex((q) => q.ratio >= r);
    const a = pts[i - 1];
    const b = pts[i];
    return a.v + ((b.v - a.v) * (r - a.ratio)) / (b.ratio - a.ratio);
  };
  const v = flat(ratio);
  const t = effVt(draft, unit);
  const r0 = effRatio(draft, unit);
  const ft = fusedTopOf(unit);
  if (ocClass(unit, r0) !== "oc" || !isNum(t) || t <= 0 || !isNum(ft) || ratio <= ft) return v;
  // between the fused top and the OC ratio the voltage is linear between the top point and the target [MEASURED lnx 2026-10-12: 47x = midpoint of 46x and the 48x OC point]
  const top = flat(ft);
  const hi = Math.max(top, t);
  return ratio >= r0 ? hi : top + ((hi - top) * (ratio - ft)) / (r0 - ft);
}
