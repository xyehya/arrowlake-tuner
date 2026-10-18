// node --test "desktop/app/ui/tests/*.test.mjs"   (G-207: units model + limits + formatting; recorded simulator fixture, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import G from "../fixtures/g217/fixtures-g217.js";
import { modelFromRead, sections, unitsWithPoint, favoredIds, quickSelections, aboveTop, ocClass, ocRatioWindow, fusedTopOf, stockMaxOf, turboFloor, aggregate, sameMv, RING_ID } from "../lib/units.js";
import { limitsFor, clampOffset, offsetWindow, vtWindow, snapGrid } from "../lib/limits.js";
import { modelCpuLine, cpuOffsetCell, READ_FIRST } from "../lib/cpulimits.js";
import { fmtMv, fmtV, fmtAgg, fmtRatio, MINUS } from "../lib/format.js";

const READ = FIX["tune-read"].result;
const model = (patch = {}) => modelFromRead({ ...structuredClone(READ), ...patch });
/** The helper's cpu_limits of a boot without a tune.read: the voltage ceilings / OC ratio limits are null (recorded by the simulator, g217 fixtures). */
const unreadModel = () => model({ cpu_limits: structuredClone(G["power-read-unread"].result.cpu_limits) });

test("model: P0..P7, EC0..EC3 and the ring from the recorded read", () => {
  const m = model();
  assert.deepEqual(m.order, ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3", RING_ID]);
  assert.deepEqual(m.units.P0.points.map((p) => p.ratio), [8, 20, 30, 48, 53, 55, 56, 57]);
  assert.deepEqual(m.units.P0.points.map((p) => p.point), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(m.units.EC1.points.map((p) => p.ratio), [8, 15, 23, 30, 40, 46]);
  assert.deepEqual(m.units.EC1.cpus, [12, 13, 14, 15]);
  assert.equal(m.units.EC1.cpu, 12);
  assert.equal(m.units.P0.fused.ratio, 57);
  assert.equal(m.units.EC0.fused.v, 1.1563);
  assert.equal(m.units.P0.oc.ratio, 57); // P0 reads an OC word equal to its fused top
  assert.equal(m.units.P1.oc.ratio, null); // a word that reads 0 has no ratio
  assert.equal(m.units.P1.oc.v_target_v, null);
  assert.equal(m.units.P0.favored, 57);
  assert.equal(m.units.P1.favored, 55);
  assert.equal(m.complete, true);
  assert.equal(m.simulator, true);
});

test("model: the ring is keyed by P2 point number; its OC slot (P2 = 9) is marked, the OC word is not a point", () => {
  const ring = model().units[RING_ID];
  assert.equal(ring.kind, "R");
  assert.deepEqual(ring.points.map((p) => p.point), [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.deepEqual(ring.points.map((p) => p.ratio), [8, 14, 20, 28, 34, 37, 38, 39, 40]);
  assert.equal(ring.points[8].ocSlot, true);
  assert.equal(ring.points[7].ocSlot, false);
  assert.equal(ring.oc.ratio, 40);
});

test("model: the ring intent feature comes from intent_features, never assumed (G-212)", () => {
  assert.deepEqual(READ.intent_features, ["ring"], "the fixture is re-recorded from a G-212 helper (G-208)");
  assert.equal(model().features.ring, true);
  assert.equal(model({ intent_features: undefined }).features.ring, false, "a helper that reports no features: no ring editing");
  assert.equal(model({ intent_features: ["ring"] }).features.ring, true);
  assert.equal(model({ intent_features: ["ring"], ring: null }).features.ring, false, "feature without a ring read: nothing to edit");
  assert.deepEqual(model({ intent_features: ["ring", "x"] }).intentFeatures, ["ring", "x"]);
});

test("model: nothing to model is null, missing pieces are null not guesses", () => {
  assert.equal(modelFromRead(null), null);
  assert.equal(modelFromRead({}), null);
  assert.equal(modelFromRead({ units: [] }), null);
  const bare = model({ turbo: null, tables: null, fused: null, cpu_limits: null, ring: null });
  assert.equal(bare.turbo, null);
  assert.equal(bare.cpuLimits, null);
  assert.equal(bare.units.P0.fused, null);
  assert.equal(fusedTopOf(bare.units.P0), 57, "without 0x09 the highest read point stands in");
  assert.equal(stockMaxOf(bare.units.P0), null, "without the favored ratio the stock max is unknown");
  assert.equal(bare.order.includes(RING_ID), false);
});

test("model: a raw read without the decoded copy gives the same points; a point above the fused top is the OC point (G-125)", () => {
  const raw = structuredClone(READ);
  delete raw.tune;
  const ec0 = raw.units.find((u) => u.id === "EC0");
  ec0.cpus[0].points.push({ point: 7, ratio: 48, offset_mv: 0, word: "0x00000030" });
  const m = modelFromRead(raw);
  assert.deepEqual(m.units.EC0.points.map((p) => p.ratio), [8, 15, 23, 30, 40, 46]);
  assert.equal(m.units.EC0.ocPoint.ratio, 48);
  assert.equal(m.units.EC0.ocPoint.point, 7);
});

test("selection: quick chips and favored (only when the cores differ)", () => {
  const m = model();
  const q = quickSelections(m);
  assert.equal(q.P.length, 8);
  assert.equal(q.E.length, 4);
  assert.equal(q.all.length, 12);
  assert.equal(q.all.includes(RING_ID), false);
  assert.deepEqual(favoredIds(m), ["P0", "P3"]);
  const flat = modelFromRead({ ...structuredClone(READ), turbo: { ...structuredClone(READ.turbo), favored: Object.fromEntries(Object.keys(READ.turbo.favored).map((k) => [k, 57])) } });
  assert.deepEqual(favoredIds(flat), []);
});

test("multi-select: P and E together are two sections; an edit by ratio reaches only the units that have the point (PLAN 4.2)", () => {
  const m = model();
  const s = sections(m, ["P0", "P3", "EC0", RING_ID]);
  assert.deepEqual(s.map((x) => x.kind), ["P", "E", "R"]);
  assert.deepEqual(s[0].ratios, [8, 20, 30, 48, 53, 55, 56, 57]);
  assert.deepEqual(s[1].ratios, [8, 15, 23, 30, 40, 46]);
  assert.deepEqual(s[2].points, [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.deepEqual(unitsWithPoint(m, ["P0", "EC0"], 48), ["P0"]);
  assert.deepEqual(unitsWithPoint(m, ["P0", "EC0"], 40), ["EC0"]);
  assert.deepEqual(unitsWithPoint(m, ["P0", "EC0"], 30), ["P0", "EC0"]);
  assert.deepEqual(unitsWithPoint(m, ["P0", "EC0"], 33), []);
  assert.deepEqual(unitsWithPoint(m, [RING_ID], 28), [], "the ring is not edited by ratio");
});

test("aggregate: same / mixed / none, and the 1/1024 V raw equality of offsets", () => {
  assert.deepEqual(aggregate([]), { state: "none" });
  assert.deepEqual(aggregate([-20, -20]), { state: "same", value: -20 });
  assert.deepEqual(aggregate([-35, -50, -35]), { state: "mixed", min: -50, max: -35, count: 3 });
  assert.deepEqual(aggregate([null, 5]), { state: "same", value: 5 });
  assert.equal(sameMv(-19.531, -20), true);
  assert.equal(sameMv(-19.531, -25), false);
  assert.equal(sameMv(0, null), false);
});

test("OC model: E runs 48x above its 46x fused top with no OC point; OC ratio window per kind", () => {
  const m = model();
  const ec0 = m.units.EC0;
  assert.equal(fusedTopOf(ec0), 46);
  assert.equal(stockMaxOf(ec0), 46);
  assert.equal(ocClass(ec0, 46), "none");
  assert.equal(ocClass(ec0, 48), "oc");
  assert.deepEqual(aboveTop(m, ec0, null), { ft: 46, max: 48 });
  assert.equal(ocClass(m.units.P1, 56), "middle", "P1 favored 55: 56x is above stock but on a fused point");
  assert.equal(ocClass(m.units.P1, 58), "oc");
  const e = ocRatioWindow(m, ["EC0", "EC1"]);
  assert.deepEqual([e.min, e.max, e.reason], [47, 120, null]);
  const p = ocRatioWindow(m, ["P0", "P1"]);
  assert.deepEqual([p.min, p.max], [58, 120], "a group starts above its highest stock max");
  assert.match(ocRatioWindow(m, ["P0", "EC0"]).reason, /separate/);
  assert.match(ocRatioWindow(m, [RING_ID]).reason, /no unit/);
  assert.match(ocRatioWindow(model({ cpu_limits: null }), ["P0"]).reason, /did not report the CPU limits/);
  assert.match(ocRatioWindow(unreadModel(), ["P0"]).reason, new RegExp(READ_FIRST), "an unread OC ratio limit says Read CPU state first");
});

test("turbo floor comes from the read's lowest point, not a literal", () => {
  const m = model();
  assert.equal(turboFloor(m, "P"), 8);
  assert.equal(turboFloor(m, "E"), 8);
  assert.deepEqual(m.turbo.P.ratios, [57, 57, 55, 55, 55, 55, 55, 55]);
  assert.deepEqual(m.turbo.E.counts, [1, 2, 3, 4, 6, 8, 12, 16]);
});

// ---------------------------------------------------------------- limits
test("limits: the CPU's own limits (cpu_limits), the ring has its own entry", () => {
  const m = model();
  const p = limitsFor(m, "P");
  assert.equal(p.known, true);
  assert.equal(p.read, true);
  assert.deepEqual([p.offset.min, p.offset.max], [-1000, 999]);
  assert.match(p.offset.source, /CPU/);
  assert.equal(p.vt.max, 1.625);
  assert.match(p.vt.source, /0x28/);
  assert.equal(p.ratio.max, 120);
  assert.match(p.ratio.source, /0x01/);
  assert.equal(limitsFor(m, "E").vt.max, 1.355);
  const r = limitsFor(m, "R");
  assert.equal(r.known, true);
  assert.equal(r.vt.max, 1.999, "the ring's own voltage ceiling, not the P-cores'");
  assert.equal(r.ratio.max, 85);
});

test("limits: an unread limit is null with the reason 'Read CPU state first'; negative offsets stay editable, positive ones and raises are not", () => {
  const m = unreadModel();
  const p = limitsFor(m, "P");
  assert.equal(p.known, true);
  assert.equal(p.read, false);
  assert.deepEqual([p.offset.min, p.offset.max], [-1000, 0]);
  assert.match(p.offset.positive, new RegExp(READ_FIRST));
  assert.equal(p.vt, null);
  assert.equal(p.ratio, null);
  assert.match(p.vtWhy, new RegExp(READ_FIRST));
  assert.match(p.ratioWhy, new RegExp(READ_FIRST));
  assert.equal(clampOffset(m, "P", -20), -20, "a negative offset is editable");
  assert.equal(clampOffset(m, "P", 30), 0, "a positive one is clamped to 0 until the ceiling is read");
  assert.equal(vtWindow(m, "P"), null);
  const none = model({ cpu_limits: null });
  assert.equal(limitsFor(none, "P").known, false);
  assert.equal(offsetWindow(none, "P"), null);
  assert.equal(clampOffset(none, "P", -20), null);
  assert.equal(vtWindow(none, "P"), null);
});

test("limits: clampOffset rounds to whole mV and clamps to the field; vt window and grid", () => {
  const m = model();
  assert.equal(clampOffset(m, "P", -20.4), -20);
  assert.equal(clampOffset(m, "P", -5000), -1000);
  assert.equal(clampOffset(m, "E", 5000), 999);
  assert.equal(clampOffset(m, "P", NaN), null);
  assert.deepEqual(vtWindow(m, "E"), { min: 0.9, max: 1.355, step: 0.005, source: vtWindow(m, "E").source });
  assert.equal(snapGrid(1.1563), 1.155);
  assert.equal(snapGrid(1.1), 1.1, "on the grid stays");
});

test("limits: the lines name the CPU register and never a policy", () => {
  const m = model();
  assert.equal(modelCpuLine(m, "P", "offset"), "CPU limit: " + MINUS + "1000…+999 mV (V/F offset field).");
  assert.equal(modelCpuLine(m, "P", "vt"), "CPU limit: up to 1.625 V (CPU max voltage, mailbox 0x28).");
  assert.equal(modelCpuLine(m, "E", "ratio"), "CPU limit: OC ratios up to 120× (OC ratio limit, mailbox 0x01).");
  assert.match(modelCpuLine(unreadModel(), "P", "vt"), new RegExp("^" + READ_FIRST));
  assert.match(modelCpuLine(unreadModel(), "P", "offset"), /Positive offsets: Read CPU state first/);
  assert.equal(cpuOffsetCell(m, "P"), MINUS + "1000…+999 · CPU");
  assert.equal(cpuOffsetCell(unreadModel(), "P"), MINUS + "1000…0 · CPU");
  assert.equal(modelCpuLine(model({ cpu_limits: null }), "P", "offset"), "");
  for (const t of [modelCpuLine(m, "P", "offset"), modelCpuLine(unreadModel(), "E", "ratio"), limitsFor(unreadModel(), "P").vtWhy]) assert.doesNotMatch(t, /polic/i);
});

// ---------------------------------------------------------------- format
test("format: signed mV with U+2212, whole when within a raw step, mixed text", () => {
  assert.equal(fmtMv(-20), MINUS + "20 mV");
  assert.equal(fmtMv(-19.531), MINUS + "19.5 mV", "a raw-quantised read keeps its decimal");
  assert.equal(fmtMv(-19.97), MINUS + "20 mV");
  assert.equal(fmtMv(-12.3), MINUS + "12.3 mV");
  assert.equal(fmtMv(5), "+5 mV");
  assert.equal(fmtMv(0), "0 mV");
  assert.equal(fmtMv(null), "–");
  assert.equal(fmtV(1.155), "1.155 V");
  assert.equal(fmtV(1.1563), "1.1563 V");
  assert.equal(fmtRatio(48), "48×");
  assert.equal(fmtAgg({ state: "mixed", min: -50, max: -35 }, fmtMv), "mixed (−50 mV…−35 mV)");
  assert.equal(fmtAgg({ state: "same", value: -20 }, fmtMv), "−20 mV");
  assert.equal(fmtAgg({ state: "none" }, fmtMv), "–");
});
