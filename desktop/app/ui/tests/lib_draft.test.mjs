// node --test "desktop/app/ui/tests/*.test.mjs"   (G-207: draft edits, diff, prediction; recorded simulator fixture, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import G from "../fixtures/g217/fixtures-g217.js";
import { modelFromRead, RING_ID } from "../lib/units.js";
import {
  emptyDraft, isEmpty, setCurve, setPoint, setRingPoint, setVt, setOcRatio, setOcOffset, ocOffsetOf, setBucket, discardUnits, discardTurbo, curveValue, pointValue, ringValue,
  ocRatioValue, vtValue, diff, warnings, effPointMv,
} from "../lib/draft.js";
import { predictUnit, predictedAt, voltageAt } from "../lib/predict.js";
import { fmtAgg, fmtMv } from "../lib/format.js";

const READ = FIX["tune-read"].result;
const model = (patch = {}) => modelFromRead({ ...structuredClone(READ), ...patch });
const E = ["EC0", "EC1", "EC2", "EC3"];
const P = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"];

test("draft: edits are immutable and an edit equal to the read is not pending", () => {
  const m = model();
  const d0 = emptyDraft();
  const d1 = setPoint(m, d0, ["P0"], 53, -20);
  assert.equal(isEmpty(d0), true, "the old draft is untouched");
  assert.deepEqual(d1.units.P0.pts, { 53: -20 });
  assert.deepEqual(diff(m, setPoint(m, d1, ["P0"], 53, 0)).items, [], "back to the read value = no change");
  assert.equal(isEmpty(setPoint(m, d1, ["P0"], 53, 0)), true);
});

test("draft: 1/1024 V raw equality: -20 over a read of -19.531 is not pending", () => {
  const read = structuredClone(READ);
  read.tune.P0.cpus[0].points.find((p) => p.point === 5).offset_mv = -19.531;
  const m = modelFromRead(read);
  assert.equal(m.units.P0.points[4].offset_mv, -19.531);
  assert.equal(isEmpty(setPoint(m, emptyDraft(), ["P0"], 53, -20)), true);
  assert.equal(setPoint(m, emptyDraft(), ["P0"], 53, -25).units.P0.pts[53], -25);
});

test("draft: values are whole mV inside the CPU's offset field", () => {
  const m = model();
  assert.deepEqual(setPoint(m, emptyDraft(), ["P0"], 53, -20.4).units.P0.pts, { 53: -20 });
  assert.deepEqual(setPoint(m, emptyDraft(), ["P0"], 53, -5000).units.P0.pts, { 53: -1000 });
  assert.deepEqual(setPoint(m, emptyDraft(), ["P0"], 53, 5000).units.P0.pts, { 53: 999 });
  assert.equal(isEmpty(setPoint(m, emptyDraft(), ["P0"], 53, NaN)), true);
  assert.equal(isEmpty(setPoint(model({ cpu_limits: null }), emptyDraft(), ["P0"], 53, -20)), true, "no limits reported = nothing may be set");
  const unread = model({ cpu_limits: structuredClone(G["power-read-unread"].result.cpu_limits) });
  assert.deepEqual(setPoint(unread, emptyDraft(), ["P0"], 53, -20).units.P0.pts, { 53: -20 }, "the CPU voltage ceiling is unread: a negative offset is still editable");
  assert.equal(isEmpty(setPoint(unread, emptyDraft(), ["P0"], 53, 20)), true, "...a positive one is clamped to 0 (= no change)");
  assert.equal(isEmpty(setPoint(m, emptyDraft(), ["P0"], 54, -20)), true, "no such point");
});

test("multi-select: one ratio reaches only units that have it; differing values show mixed", () => {
  const m = model();
  let d = setPoint(m, emptyDraft(), ["P0", "P3", "EC0"], 53, -20); // EC0 has no 53x
  assert.deepEqual(Object.keys(d.units), ["P0", "P3"]);
  d = setPoint(m, d, ["P3"], 53, -35);
  const v = pointValue(m, d, ["P0", "P3", "EC0"], 53);
  assert.deepEqual(v.ids, ["P0", "P3"]);
  assert.equal(v.agg.state, "mixed");
  assert.equal(fmtAgg(v.agg, fmtMv), "mixed (−35 mV…−20 mV)");
  assert.equal(v.pending, true);
  assert.equal(pointValue(m, d, ["P0", "P3"], 53, false).agg.value, 0, "the read value, no draft");
  // typing sets all
  const all = setPoint(m, d, ["P0", "P3"], 53, -25);
  assert.deepEqual(pointValue(m, all, ["P0", "P3"], 53).agg, { state: "same", value: -25 });
  // a ratio both kinds have (30x) reaches P and E
  const both = setPoint(m, emptyDraft(), ["P0", "EC0"], 30, -10);
  assert.deepEqual(Object.keys(both.units), ["P0", "EC0"]);
});

test("whole curve: all-E sets one number on every cluster; reading it back is 'same'; discard works", () => {
  const m = model();
  const d = setCurve(m, emptyDraft(), E, -30);
  assert.deepEqual(Object.keys(d.units), E);
  assert.equal(d.units.EC2.curveMv, -30);
  assert.deepEqual(curveValue(m, d, E), { state: "same", value: -30 });
  assert.deepEqual(curveValue(m, d, E, false), { state: "same", value: 0 });
  assert.equal(curveValue(m, d, [...E, "P0"]).state, "mixed");
  assert.equal(isEmpty(setCurve(m, d, E, 0)), true, "curve back to what every point reads");
  assert.equal(isEmpty(discardUnits(d, E)), true);
});

test("whole curve then a point: the curve spreads over the points first", () => {
  const m = model();
  let d = setCurve(m, emptyDraft(), ["P0"], -30);
  d = setPoint(m, d, ["P0"], 57, -10);
  assert.equal(d.units.P0.curveMv, undefined);
  assert.equal(Object.keys(d.units.P0.pts).length, 8);
  assert.equal(d.units.P0.pts[57], -10);
  assert.equal(d.units.P0.pts[8], -30);
  // and a curve over per-point edits replaces them
  d = setCurve(m, d, ["P0"], -5);
  assert.deepEqual(d.units.P0, { pts: {}, curveMv: -5 });
});

test("ring: refused (draft unchanged) without the intent feature, by point number with it", () => {
  const off = model({ intent_features: undefined }); // the fixture carries "ring" since G-208
  const d0 = emptyDraft();
  assert.equal(setRingPoint(off, d0, 2, -5), d0);
  assert.equal(setCurve(off, d0, [RING_ID], -5).units[RING_ID], undefined);
  const m = model({ intent_features: ["ring"] });
  const d = setRingPoint(m, d0, 2, -5);
  assert.deepEqual(d.units[RING_ID].pts, { 2: -5 });
  assert.deepEqual(ringValue(m, d, 2), { agg: { state: "same", value: -5 }, ids: [RING_ID], pending: true });
  assert.equal(isEmpty(setRingPoint(m, d, 2, 0)), true);
  assert.equal(setRingPoint(m, d0, 12, -5), d0, "no such ring point");
  const curve = setCurve(m, d0, [RING_ID], -3);
  assert.equal(Object.keys(curve.units[RING_ID].pts).length, 9, "the ring has no curve field: expanded to its points");
});

test("OC ratio on E: a ratio above the fused top creates the OC point with an automatic target (D-035), dropping it takes the target back", () => {
  const m = model();
  let d = setOcRatio(m, emptyDraft(), ["EC0"], 48);
  assert.deepEqual(d.units.EC0, { pts: {}, ratio: 48, vt: 1.155, vtAuto: true }, "fused 1.1563 V on the 5 mV grid");
  assert.deepEqual(ocRatioValue(m, d, ["EC0"]), { state: "same", value: 48 });
  assert.equal(vtValue(m, d, ["EC0"]).value, 1.155);
  d = setVt(m, d, ["EC0"], 1.1612);
  assert.deepEqual(d.units.EC0, { pts: {}, ratio: 48, vt: 1.16 });
  assert.equal(isEmpty(setOcRatio(m, setOcRatio(m, emptyDraft(), ["EC0"], 48), ["EC0"], null)), true);
  assert.equal(isEmpty(setOcRatio(m, emptyDraft(), ["EC0"], 121)), true, "above the CPU's OC ratio limit (120x)");
  assert.equal(isEmpty(setOcRatio(model({ cpu_limits: structuredClone(G["power-read-unread"].result.cpu_limits) }), emptyDraft(), ["EC0"], 48)), true, "the OC ratio limit is unread: no OC ratio");
  assert.equal(isEmpty(setOcRatio(m, emptyDraft(), ["EC0"], 45)), true, "not an OC ratio");
  assert.equal(setVt(m, d, ["EC0"], 2).units.EC0.vt, 1.355, "clamped to the target ceiling");
  assert.equal(isEmpty(setVt(m, emptyDraft(), ["EC0"], 1.2)), true, "a target needs an OC point");
});

test("48x offset (G-150): target = fused V + x, exact, not below the top point", () => {
  const m = model();
  const d = setOcOffset(m, emptyDraft(), ["EC0"], 10);
  assert.deepEqual([d.units.EC0.ratio, d.units.EC0.vt], [48, 1.1663]);
  assert.equal(ocOffsetOf(m, d, "EC0"), 10);
  const below = setOcOffset(m, emptyDraft(), ["EC0"], -50);
  assert.equal(below.units.EC0.vt, 1.1563, "never below the top fused point's voltage");
  assert.equal(isEmpty(setOcOffset(m, d, ["EC0"], null)), true, "x = null where the CPU has no OC point: no OC point, the draft goes away");
  assert.equal(isEmpty(setOcOffset(m, emptyDraft(), ["P0"], 5)), true, "P0 runs no ratio above its fused top: not applicable");
  // a unit that already has an OC point cannot drop it: x = null puts its target on the top point's voltage
  const read = structuredClone(READ);
  Object.assign(read.tune.EC1.cpus[0].points[0], { ratio: 48, v_target_v: 1.2 });
  const m2 = modelFromRead(read);
  assert.equal(ocOffsetOf(m2, emptyDraft(), "EC1"), 63, "1.2 V - 1.1367 V (EC1's own fused V), in mV");
  const same = setOcOffset(m2, emptyDraft(), ["EC1"], null);
  assert.deepEqual([same.units.EC1.ratio, same.units.EC1.vt], [undefined, 1.1367]);
});

test("turbo: non-increasing while typing; lowering a bucket brings its right neighbours down; equal to the read drops the edit", () => {
  const m = model();
  assert.deepEqual(m.turbo.P.ratios, [57, 57, 55, 55, 55, 55, 55, 55]);
  let d = setBucket(m, emptyDraft(), "P", 2, 56);
  assert.deepEqual(d.turbo.P, [57, 57, 56, 55, 55, 55, 55, 55]);
  d = setBucket(m, d, "P", 2, 58);
  assert.deepEqual(d.turbo.P, [57, 57, 57, 55, 55, 55, 55, 55], "not above the bucket on its left");
  d = setBucket(m, emptyDraft(), "P", 0, 50);
  assert.deepEqual(d.turbo.P, [50, 50, 50, 50, 50, 50, 50, 50]);
  assert.equal(setBucket(m, d, "P", 0, 999).turbo.P[0], 120, "raise stops at the CPU's OC ratio limit");
  assert.equal(setBucket(m, emptyDraft(), "P", 7, 1).turbo.P[7], 8, "floor = the read's lowest point");
  assert.equal(isEmpty(setBucket(m, setBucket(m, emptyDraft(), "P", 2, 56), "P", 2, 55)), true);
  assert.equal(isEmpty(discardTurbo(d, "P")), true);
  const noLimits = model({ cpu_limits: structuredClone(G["power-read-unread"].result.cpu_limits) });
  assert.equal(setBucket(noLimits, emptyDraft(), "P", 0, 70).turbo.P, undefined, "OC ratio limit unread = no raise above the read");
  assert.equal(setBucket(model({ turbo: null, tables: null }), emptyDraft(), "P", 0, 50).turbo.P, undefined, "no table read, no edit");
});

test("diff: pending counts per unit, turbo, and the items for the review", () => {
  const m = model({ intent_features: ["ring"] });
  let d = setPoint(m, emptyDraft(), ["P0", "P3"], 53, -20);
  d = setPoint(m, d, ["P0"], 57, -10);
  d = setOcRatio(m, d, ["EC0"], 48); // ratio + automatic target
  d = setBucket(m, d, "E", 0, 47); // all eight buckets come down to 47: 8 changes
  d = setRingPoint(m, d, 2, -5);
  const f = diff(m, d);
  assert.deepEqual(f.perUnit, { P0: 2, P3: 1, EC0: 2, ring: 1 });
  assert.deepEqual(f.turbo, { E: 8 });
  assert.equal(f.total, 14);
  assert.equal(f.unitCount, 5);
  assert.deepEqual(f.items.find((x) => x.unit === "P0" && x.key === 53), { unit: "P0", field: "point", key: 53, from: 0, to: -20 });
  assert.deepEqual(f.items.find((x) => x.field === "ratio"), { unit: "EC0", field: "ratio", key: null, from: null, to: 48 });
  assert.equal(f.items.find((x) => x.field === "ring-point").key, 2);
  assert.deepEqual(diff(m, emptyDraft()), { total: 0, unitCount: 0, perUnit: {}, turbo: {}, items: [] });
});

test("warnings: an E 40x offset is a hint, not a refusal", () => {
  const m = model();
  assert.deepEqual(warnings(m, setPoint(m, emptyDraft(), ["EC0"], 46, -20)), []);
  const w = warnings(m, setPoint(m, emptyDraft(), ["EC0", "EC2"], 40, -20));
  assert.deepEqual(w.map((x) => [x.code, x.unit]), [["E40_OFFSET", "EC0"], ["E40_OFFSET", "EC2"]]);
  assert.equal(effPointMv(emptyDraft(), m.units.EC0, m.units.EC0.points[4]), 0);
});

// ---------------------------------------------------------------- prediction
test("predict: from the read alone only the fused top has a voltage; lower points are null, not guessed", () => {
  const m = model();
  const d = setPoint(m, emptyDraft(), ["P0"], 57, -50);
  const pred = predictUnit(m, d, "P0");
  assert.equal(pred.any, true);
  assert.equal(pred.points.filter((q) => q.v !== null).length, 1);
  const top = predictedAt(pred, 57);
  assert.equal(top.src, "fused");
  assert.ok(Math.abs(top.v - (1.4258 - 0.05)) < 1e-9);
  assert.equal(predictedAt(pred, 53).v, null);
  assert.equal(predictUnit(m, emptyDraft(), "P0").points.find((q) => q.ratio === 57).v, 1.4258);
  assert.equal(predictUnit(m, emptyDraft(), "nope").any, false);
});

test("predict: V(n) >= V(n-1) clamp names the point that holds it (platform-arl-s.md §3 monotonic clamp)", () => {
  const m = model();
  const base = { P0: { 48: { v: 1.0 }, 53: { v: 1.02 }, 55: { v: 1.1, src: "baseline" } } };
  let d = setPoint(m, emptyDraft(), ["P0"], 48, 0);
  d = setPoint(m, d, ["P0"], 53, -50); // wants 0.97 < 1.0
  const pred = predictUnit(m, d, "P0", base);
  const p48 = predictedAt(pred, 48);
  const p53 = predictedAt(pred, 53);
  assert.deepEqual([p48.v, p48.clampedBy, p48.src], [1.0, null, "baseline"]);
  assert.ok(Math.abs(p53.want - 0.97) < 1e-9);
  assert.equal(p53.v, 1.0);
  assert.deepEqual(p53.clampedBy, { ratio: 48, point: 4 });
  assert.equal(predictedAt(pred, 55).clampedBy, null);
  const asFn = predictUnit(m, d, "P0", (id, r) => (id === "P0" && base.P0[r] ? base.P0[r] : null));
  assert.deepEqual(asFn.points.map((q) => q.v), pred.points.map((q) => q.v));
});

test("predict: voltageAt is linear between points, flat above the top, and follows the OC target from the OC ratio up", () => {
  const m = model();
  const base = { P0: { 48: { v: 1.0 }, 53: { v: 1.1 } } };
  assert.ok(Math.abs(voltageAt(m, emptyDraft(), "P0", 50, base) - 1.04) < 1e-9);
  assert.equal(voltageAt(m, emptyDraft(), "P0", 40, base), 1.0, "flat below the first predicted point");
  assert.equal(voltageAt(m, emptyDraft(), "P0", 70, base), 1.4258, "flat at the top (fused) above");
  assert.equal(voltageAt(m, emptyDraft(), "P1", 50), 1.3047, "only the fused top is known: flat");
  const d = setVt(m, setOcRatio(m, emptyDraft(), ["EC0"], 48), ["EC0"], 1.2);
  assert.equal(voltageAt(m, d, "EC0", 48), 1.2);
  assert.ok(Math.abs(voltageAt(m, d, "EC0", 47) - (1.1563 + (1.2 - 1.1563) / 2)) < 1e-9, "47x = midpoint of the top point and the OC target");
  assert.equal(voltageAt(m, emptyDraft(), "EC0", 48), 1.1563, "no OC point: the top point's voltage");
});
