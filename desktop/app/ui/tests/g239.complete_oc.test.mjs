// node --test "desktop/app/ui/tests/*.test.mjs"   (G-239: finish an interrupted OC ratio; pure draft/intent/note logic, recorded simulator fixture patched like the live read of 2026-10-19, no DOM, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { modelFromRead } from "../lib/units.js";
import { emptyDraft, isEmpty, ocIncomplete, completeOcRatio, setOcRatio, setVt, clearOcEdits, discardUnits, diff } from "../lib/draft.js";
import { buildIntent } from "../lib/intent.js";
import { ocIncompleteInfo, ocIncompleteText } from "../views/tune/oc.js";
import { patchTune, ensureTune } from "../views/tune.js";
import { createAppStore } from "../store.js";

const READ = FIX["tune-read"].result;

// cpu 0 as the tripped apply left it: OC word 0x0005b43a (58x, adaptive 1.4258 V), 0x1C[7:0] still 57 (the fixture's P0 favored ratio).
function interrupted() {
  const r = structuredClone(READ);
  Object.assign(r.tune.P0.cpus[0].points[0], { ratio: 58, v_target_v: 1.4258 });
  return modelFromRead(r);
}
const m = interrupted();
const ids = ["P0"];

test("detection: a P unit whose OC word ratio is above its 0x1C[7:0] ratio is incomplete; the others are not", () => {
  assert.equal(m.units.P0.favored, 57);
  assert.equal(ocIncomplete(m.units.P0), true);
  assert.equal(ocIncomplete(m.units.P3), false, "no OC word");
  assert.equal(ocIncomplete(m.units.EC0), false, "E clusters carry no 0x1C ratio in the model");
  const done = structuredClone(READ);
  Object.assign(done.tune.P0.cpus[0].points[0], { ratio: 57, v_target_v: null });
  assert.equal(ocIncomplete(modelFromRead(done).units.P0), false, "OC word equal to 0x1C: complete");
  const no1c = structuredClone(READ);
  Object.assign(no1c.tune.P0.cpus[0].points[0], { ratio: 58, v_target_v: 1.4258 });
  delete no1c.turbo.favored;
  assert.equal(ocIncomplete(modelFromRead(no1c).units.P0), false, "0x1C not read: nothing is claimed");
  assert.equal(ocIncompleteInfo(modelFromRead(READ), emptyDraft(), ["P0", "P3"]), null);
});

test("note: the operator's wording for one core, ids and both ratios for several", () => {
  const info = ocIncompleteInfo(m, emptyDraft(), ids);
  assert.deepEqual(info, { units: [{ id: "P0", ratio: 58, max: 57 }], queued: false });
  assert.equal(ocIncompleteText(info), "The OC word says 58× but this core's maximum ratio (0x1C) is 57×: the apply that set it stopped early.");
  const r = structuredClone(READ);
  Object.assign(r.tune.P0.cpus[0].points[0], { ratio: 58, v_target_v: 1.4258 });
  Object.assign(r.tune.P3.cpus[0].points[0], { ratio: 59, v_target_v: 1.44 });
  const two = ocIncompleteInfo(modelFromRead(r), emptyDraft(), ["P0", "P3", "EC0"]);
  assert.deepEqual(two.units.map((u) => u.id), ["P0", "P3"]);
  assert.match(ocIncompleteText(two), /P0 \(58× vs 57×\), P3 \(59× vs 57×\)/);
});

test("button: Complete the OC ratio puts the ratio in force into the draft with the flag; one change; the intent carries ratio 58 and nothing else", () => {
  const d = completeOcRatio(m, emptyDraft(), ids);
  assert.deepEqual(d.units.P0, { pts: {}, ratio: 58, restate: true });
  const df = diff(m, d);
  assert.equal(df.total, 1);
  assert.equal(df.unitCount, 1);
  assert.deepEqual(df.items, [{ unit: "P0", field: "ratio", key: null, from: 58, to: 58, restate: true }]);
  assert.deepEqual(buildIntent(m, d), { intent: { scope: "unit", rows: [{ target: "P0", offset: null, v_target_v: null, ratio: 58 }] } }, "no turbo table, no voltage");
  assert.equal(ocIncompleteInfo(m, d, ids).queued, true);
  assert.equal(ocIncompleteInfo(m, emptyDraft(), ids).queued, false, "the input draft is not mutated");
});

test("button: units that are complete are left alone; a plain ratio equal to the read is still no edit", () => {
  const ok = modelFromRead(structuredClone(READ));
  assert.ok(isEmpty(completeOcRatio(ok, emptyDraft(), ["P0", "P3", "EC0"])));
  assert.ok(isEmpty(setOcRatio(m, emptyDraft(), ids, 58)), "setOcRatio drops a ratio equal to what was read (only the button restates)");
});

test("draft: any later ratio edit or Back to read removes the flag; other edits keep it", () => {
  const d = completeOcRatio(m, emptyDraft(), ids);
  assert.ok(isEmpty(setOcRatio(m, d, ids, 58)), "58 again = back to what was read");
  assert.ok(isEmpty(clearOcEdits(m, d, ids)));
  assert.ok(isEmpty(discardUnits(d, ids)));
  const other = setOcRatio(m, d, ids, 59);
  assert.equal(other.units.P0.ratio, 59);
  assert.equal("restate" in other.units.P0, false);
  assert.equal(ocIncompleteInfo(m, other, ids).queued, true, "a drafted ratio already makes the intent carry one: nothing to complete");
  assert.equal(ocIncompleteInfo(m, setVt(m, emptyDraft(), ids, 1.43), ids).queued, false, "a target alone does not");
  const withVt = setVt(m, d, ids, 1.43);
  assert.equal(withVt.units.P0.restate, true);
  assert.equal(diff(m, withVt).total, 2, "ratio restate + target");
  assert.deepEqual(buildIntent(m, withVt).intent.rows[0], { target: "P0", offset: null, v_target_v: 1.43, ratio: 58 });
});

test("pending bar: the restate counts as 1 change on 1 unit", () => {
  const store = createAppStore();
  ensureTune(store);
  patchTune(store, { model: m });
  patchTune(store, { draft: completeOcRatio(m, store.get().tune.draft, ids) });
  assert.deepEqual(store.get().pending, { changes: 1, units: 1 });
});
