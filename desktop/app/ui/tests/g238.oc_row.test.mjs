// node --test "desktop/app/ui/tests/*.test.mjs"   (G-238: the OC point as the last row of the V/F table; pure curveRows(), recorded simulator fixture, no hardware, no DOM)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { modelFromRead, sections } from "../lib/units.js";
import { emptyDraft, setPoint, setVt, setOcRatio, clearOcEdits, setCurve } from "../lib/draft.js";
import { curveRows, OC_KEY } from "../views/tune/curve-editor.js";
import { baselineFromRecord } from "../views/tune.js";

const READ = FIX["tune-read"].result;
const m = modelFromRead(structuredClone(READ));
const secOf = (ids) => sections(m, ids)[0];

// The fixture has no unit with a read OC point; patch the decoded copy like the live read of 2026-10-19 (P2=0: 58x + adaptive 1.4258 V; P2=9: ratio 58, V field 0).
function ocModel() {
  const r = structuredClone(READ);
  const p0 = r.tune.P0.cpus[0].points;
  Object.assign(p0[0], { ratio: 58, v_target_v: 1.4258 });
  p0.push({ point: 9, ratio: 58, offset_mv: 0, v_target_v: null, oc_point: true, mode: "adaptive" });
  const e0 = r.tune.EC0.cpus[0].points;
  Object.assign(e0[0], { ratio: 48, v_target_v: 1.2 });
  e0.push({ point: 7, ratio: 48, offset_mv: 0, v_target_v: null, oc_point: true, mode: "adaptive" });
  return modelFromRead(r);
}
const mo = ocModel();
const secO = (ids) => sections(mo, ids)[0];

test("oc row: a read P OC point is the last row 'Point 9 · 58×', base V = the OC word's target, predicted = max(target, top), nothing pending", () => {
  assert.equal(mo.units.P0.ocPoint.point, 9);
  const rows = curveRows(mo, emptyDraft(), secO(["P0"]), null);
  assert.equal(rows.length, 9);
  assert.deepEqual(rows.slice(0, 8).map((r) => r.point), [1, 2, 3, 4, 5, 6, 7, 8]);
  const oc = rows[8];
  assert.deepEqual([oc.key, oc.oc, oc.point, oc.ratio, oc.label, oc.shared, oc.pending, oc.ocSlot], [OC_KEY, true, 9, 58, "Point 9 · 58×", true, false, false]);
  assert.equal(oc.baseV.value, 1.4258);
  assert.equal(oc.baseSrc, "oc");
  assert.equal(oc.pred.value, 1.4258, "target = the fused top voltage here");
  assert.equal(oc.readback.value, 1.4258);
  assert.equal(oc.clampedBy, null);
  assert.deepEqual(oc.agg, { state: "same", value: 0 }, "the OC word's offset field");
  assert.ok(rows.slice(0, 8).every((r) => !r.oc), "the other rows are untouched");
});

test("oc row: a target below the top point's voltage is raised to it (clamp reported), a target above stands", () => {
  const r = structuredClone(READ);
  Object.assign(r.tune.P0.cpus[0].points[0], { ratio: 58, v_target_v: 1.3 });
  const low = modelFromRead(r);
  let oc = curveRows(low, emptyDraft(), sections(low, ["P0"])[0], null).at(-1);
  assert.equal(oc.baseV.value, 1.3);
  assert.equal(oc.pred.value, 1.4258);
  assert.deepEqual(oc.clampedBy, { ratio: 57, point: 8 });
  const d = setVt(low, emptyDraft(), ["P0"], 1.5);
  oc = curveRows(low, d, sections(low, ["P0"])[0], null).at(-1);
  assert.equal(oc.pred.value, 1.5);
  assert.equal(oc.clampedBy, null);
  assert.equal(oc.pending, true);
  assert.equal(oc.readback.value, 1.4258, "the read-back is the read target raised to the top point");
});

test("oc row: with a baseline, the top point's predicted voltage (draft offsets) is the floor of the OC point", () => {
  const base = baselineFromRecord({ units: { P0: { points: [8, 20, 30, 48, 53, 55, 56, 57].map((r, i) => ({ ratio: r, status: "ok", vid_v: 0.9 + i * 0.05 })) } } });
  const d = setPoint(mo, emptyDraft(), ["P0"], 57, 600);
  const rows = curveRows(mo, d, secO(["P0"]), base);
  const top = rows.find((r) => r.ratio === 57);
  const oc = rows.at(-1);
  assert.ok(top.pred.value > 1.4258, "the draft raises the top point above the target");
  assert.ok(Math.abs(oc.pred.value - top.pred.value) < 1e-9);
  assert.deepEqual(oc.clampedBy, { ratio: 57, point: 8 });
});

test("oc row: an E cluster has it as point 7", () => {
  const rows = curveRows(mo, emptyDraft(), secO(["EC0"]), null);
  assert.equal(rows.length, 7);
  assert.deepEqual([rows[6].point, rows[6].ratio, rows[6].label, rows[6].oc], [7, 48, "Point 7 · 48×", true]);
  assert.equal(rows[6].baseV.value, 1.2);
});

test("oc row: a drafted OC point shows as a pending row, read-back none (nothing was read)", () => {
  const d = setOcRatio(m, emptyDraft(), ["P1"], 58);
  const rows = curveRows(m, d, sections(m, ["P1"])[0], null);
  assert.equal(rows.length, 9);
  const oc = rows[8];
  assert.deepEqual([oc.oc, oc.point, oc.ratio, oc.pending], [true, 9, 58, true]);
  assert.equal(oc.baseV.state, "same", "setOcRatio drafts the fused top voltage as the target");
  assert.equal(oc.readback.state, "none");
  assert.equal(oc.pred.state, "same");
  assert.equal(curveRows(m, clearOcEdits(m, d, ["P1"]), sections(m, ["P1"])[0], null).length, 8, "dropping the draft removes the row again");
  const e = curveRows(m, setOcRatio(m, emptyDraft(), ["EC0"], 49), secOf(["EC0"]), null);
  assert.deepEqual([e.length, e[6].point, e[6].ratio, e[6].pending], [7, 7, 49, true]);
});

test("oc row: only the units that have one are in it; the whole-curve edit never touches it", () => {
  const sec = sections(mo, ["P0", "P1"])[0];
  const rows = curveRows(mo, emptyDraft(), sec, null);
  const oc = rows.at(-1);
  assert.deepEqual([oc.ids, oc.shared], [["P0"], false]);
  assert.equal(rows.length, 9);
  const d = setCurve(mo, emptyDraft(), ["P0", "P1"], -20);
  const after = curveRows(mo, d, sec, null);
  assert.equal(after.at(-1).pending, false);
  assert.deepEqual(after.at(-1).agg, { state: "same", value: 0 });
  assert.ok(after.slice(0, 8).every((r) => r.pending));
});

test("oc row: no OC point, no row; a boot-set ratio above the top (EC0 48x, fused 46x) and the ring have none", () => {
  assert.equal(curveRows(m, emptyDraft(), secOf(["P0"]), null).length, 8);
  assert.equal(curveRows(m, emptyDraft(), secOf(["EC0"]), null).length, 6);
  assert.equal(curveRows(m, emptyDraft(), secOf(["P0", "P1"]), null).length, 8);
  assert.ok(curveRows(m, emptyDraft(), secOf(["EC0"]), null).every((r) => r.oc === undefined));
  const ring = curveRows(m, emptyDraft(), sections(m, ["ring"])[0], null);
  assert.equal(ring.length, 9);
  assert.ok(ring.every((r) => !r.oc && r.key !== OC_KEY));
});
