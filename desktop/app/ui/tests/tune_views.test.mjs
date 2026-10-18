// node --test "desktop/app/ui/tests/*.test.mjs"   (G-208: the pure parts of the Tune views; recorded simulator fixture, no hardware, no DOM)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { modelFromRead, sections, quickSelections, favoredIds } from "../lib/units.js";
import { emptyDraft, setPoint, setVt, setOcRatio, clearOcEdits, setCurve, diff } from "../lib/draft.js";
import { MINUS } from "../lib/format.js";
import { parseInput, gridStep, mixedText } from "../components/stepper.js";
import { nextSelection, tileFace, liveFace, sameIds } from "../views/tune/grid.js";
import { curveRows, sectionCurve, disabledReason, limitCell, NO_BASELINE } from "../views/tune/curve-editor.js";
import { ocInfo, offsetUnits, ocPointNo } from "../views/tune/oc.js";
import { selectionTitle, sectionFacts } from "../views/tune/inspector.js";
import { baselineFromRecord, initialTune, syncPending, patchTune, discardDraft, requestReview, ensureTune, fmtSeconds } from "../views/tune.js";
import { createAppStore } from "../store.js";

const READ = FIX["tune-read"].result;
const model = (patch = {}) => modelFromRead({ ...structuredClone(READ), ...patch });
const m = model();

// ---- stepper ----
test("stepper: parseInput takes typed mV with signs, the unicode minus and a unit, nothing else", () => {
  assert.equal(parseInput("-35"), -35);
  assert.equal(parseInput(`${MINUS}35`), -35);
  assert.equal(parseInput("+5"), 5);
  assert.equal(parseInput(" 35 mV ", { unit: "mV" }), 35);
  assert.equal(parseInput("48x"), 48);
  assert.equal(parseInput("48×"), 48);
  assert.equal(parseInput("1.5"), null, "no decimals in a whole-mV field");
  assert.equal(parseInput("abc"), null);
  assert.equal(parseInput(""), null);
  assert.equal(parseInput("1,250", { decimals: 3 }), 1.25);
  assert.equal(parseInput("1.25 V", { decimals: 3, unit: "V" }), 1.25);
  assert.equal(parseInput("--5"), null);
});

test("stepper: arrows land on the grid, in both directions, from off-grid values too", () => {
  assert.equal(gridStep(0, -1, 5), -5);
  assert.equal(gridStep(-5, -1, 5), -10);
  assert.equal(gridStep(-19, 1, 5), -15);
  assert.equal(gridStep(-19.5, -1, 5), -20);
  assert.equal(gridStep(-10, -1, 25), -25);
  assert.equal(gridStep(-25, 1, 25), 0);
  assert.equal(gridStep(1.2, 1, 0.005), 1.205);
  assert.equal(gridStep(1.2, -1, 0.005), 1.195);
  assert.equal(mixedText({ min: -35, max: 0 }, (v) => (v < 0 ? MINUS + -v : String(v)), "mV"), `mixed (${MINUS}35…0 mV)`);
});

// ---- grid ----
test("grid: click replaces, Ctrl toggles, Shift selects a range from the anchor in model order", () => {
  const sel = (cur, anchor, id, mods) => nextSelection(m, cur, anchor, id, mods);
  assert.deepEqual(sel(["P0"], "P0", "P3", {}), { ids: ["P3"], anchor: "P3" });
  assert.deepEqual(sel(["P0"], "P0", "P3", { ctrl: true }), { ids: ["P0", "P3"], anchor: "P3" });
  assert.deepEqual(sel(["P0", "P3"], "P3", "P0", { ctrl: true }), { ids: ["P3"], anchor: "P0" });
  assert.deepEqual(sel(["P2"], "P2", "P5", { shift: true }).ids, ["P2", "P3", "P4", "P5"]);
  assert.deepEqual(sel(["P5"], "P5", "P2", { shift: true }).ids, ["P2", "P3", "P4", "P5"], "backwards");
  assert.deepEqual(sel(["P0", "P6"], "P6", "P4", { shift: true, ctrl: true }).ids, ["P0", "P4", "P5", "P6"], "Ctrl+Shift adds the range");
  assert.deepEqual(sel([], null, "P4", { shift: true }), { ids: ["P4"], anchor: "P4" }, "no anchor: just the tile");
  assert.equal(sameIds(["P0"], ["P0"]), true);
});

test("grid: tile faces (ratio, OC badge, deepest offset, pending, favored) and the ring tile", () => {
  const fav = favoredIds(m);
  assert.deepEqual(fav, ["P0", "P3"]);
  const p0 = tileFace(m, emptyDraft(), "P0", fav);
  assert.deepEqual([p0.title, p0.big, p0.badge, p0.deepest, p0.pending, p0.favored], ["P0", 57, null, 0, false, true]);
  const d = setPoint(m, emptyDraft(), ["P1"], 53, -35);
  const p1 = tileFace(m, d, "P1", fav);
  assert.deepEqual([p1.deepest, p1.pending, p1.favored, p1.big], [-35, true, false, 55]);
  const oc = tileFace(m, setOcRatio(m, emptyDraft(), ["P1"], 58), "P1", fav);
  assert.equal(oc.badge, "OC 58×");
  assert.equal(oc.big, 58);
  const ec = tileFace(m, emptyDraft(), "EC0", fav);
  assert.deepEqual([ec.title, ec.big, ec.sub], ["EC0", 48, "cpu 8–11"]);
  const ring = tileFace(m, emptyDraft(), "ring", fav);
  assert.deepEqual([ring.kind, ring.sub, ring.big], ["R", "9 V/F points", null]);
});

test("grid: live face reads the unit's CPUs from telemetry and says n/a otherwise", () => {
  const values = { cores: { 8: { mhz: { value: 4600 }, vid: { value: 1.1 } }, 9: { mhz: { value: 4700 }, vid: { value: 1.12 } }, 10: { mhz: { status: "unavailable", value: null } } }, temps: { Package: { value: 50 } } };
  const f = liveFace(m.units.EC0, values);
  assert.deepEqual(f, { mhz: 4700, temp: null, vid: 1.12 });
  assert.deepEqual(liveFace(m.units.P0, values), { mhz: null, temp: null, vid: null });
  assert.deepEqual(liveFace(m.units.P0, null), { mhz: null, temp: null, vid: null });
});

// ---- the table ----
const secOf = (ids) => sections(m, ids)[0];

test("curve rows: one row per point, point numbers, read values, nothing pending on an empty draft", () => {
  const rows = curveRows(m, emptyDraft(), secOf(["P0"]), null);
  assert.deepEqual(rows.map((r) => [r.ratio, r.point]), [[8, 1], [20, 2], [30, 3], [48, 4], [53, 5], [55, 6], [56, 7], [57, 8]]);
  assert.equal(rows[3].label, "Point 4 · 48×");
  assert.ok(rows.every((r) => r.agg.state === "same" && r.agg.value === 0 && !r.pending));
  assert.equal(rows[7].pred.state, "same", "the fused top point has a voltage even without a baseline");
  assert.equal(rows[7].pred.value, 1.4258);
  assert.equal(rows[3].pred.state, "none");
  assert.equal(rows[3].why, NO_BASELINE);
});

test("curve rows: an offset changes pending/predicted, the clamp is reported, a baseline fills every row", () => {
  const base = baselineFromRecord({ units: { P0: { points: [8, 20, 30, 48, 53, 55, 56, 57].map((r, i) => ({ ratio: r, status: "ok", vid_v: 0.9 + i * 0.05 })) } } });
  let d = setPoint(m, emptyDraft(), ["P0"], 48, -20);
  let rows = curveRows(m, d, secOf(["P0"]), base);
  const r48 = rows.find((r) => r.ratio === 48);
  assert.equal(r48.pending, true);
  assert.ok(Math.abs(r48.pred.value - (1.05 - 0.02)) < 1e-9);
  assert.ok(Math.abs(r48.readback.value - 1.05) < 1e-9);
  assert.ok(Math.abs(r48.baseV.value - 1.05) < 1e-9);
  assert.equal(r48.baseSrc, "baseline");
  d = setPoint(m, d, ["P0"], 53, -200);
  rows = curveRows(m, d, secOf(["P0"]), base);
  const r53 = rows.find((r) => r.ratio === 53);
  assert.deepEqual(r53.clampedBy, { ratio: 48, point: 4 }, "53x cannot go below 48x's voltage");
  assert.ok(Math.abs(r53.pred.value - r48.pred.value) < 1e-9);
});

test("curve rows: multi-select shows mixed offsets and a range of voltages", () => {
  const d = setPoint(m, emptyDraft(), ["EC0"], 15, -35);
  const rows = curveRows(m, d, secOf(["EC0", "EC1", "EC2", "EC3"]), null);
  const r15 = rows.find((r) => r.ratio === 15);
  assert.deepEqual(r15.agg, { state: "mixed", min: -35, max: 0, count: 4 });
  assert.equal(r15.pending, true);
  assert.equal(r15.readAgg.state, "same");
  assert.equal(rows.find((r) => r.ratio === 23).agg.state, "same");
  const curve = sectionCurve(m, d, secOf(["EC0", "EC1", "EC2", "EC3"]), true);
  assert.equal(curve.state, "mixed");
  assert.equal(sectionCurve(m, d, secOf(["EC0", "EC1", "EC2", "EC3"]), false).state, "same");
  const whole = setCurve(m, emptyDraft(), ["EC0", "EC1"], -10);
  assert.equal(sectionCurve(m, whole, secOf(["EC0", "EC1"]), true).value, -10);
});

test("curve rows: ring rows are keyed by the P2 point number, the OC slot is marked, no voltages are claimed", () => {
  const mr = model();
  const rows = curveRows(mr, emptyDraft(), sections(mr, ["ring"])[0], null);
  assert.deepEqual(rows.map((r) => r.key), [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.equal(rows[8].ocSlot, true);
  assert.ok(rows.every((r) => r.pred.state === "none" && r.baseV.state === "none"));
  assert.match(rows[0].why, /ring/);
});

test("limits: the field's disabled reason and the row's limit cell name the CPU as the source", () => {
  assert.equal(disabledReason(m, "P"), "");
  assert.equal(disabledReason(m, "R"), "");
  assert.match(disabledReason(model({ intent_features: undefined }), "R"), /ring/);
  assert.match(disabledReason(model({ cpu_limits: null }), "P"), /did not report the CPU limits/);
  assert.equal(limitCell(m, "P"), "−1000…+999 · CPU (11-bit V/F offset field)");
  assert.match(limitCell(m, "R"), /^−1000…\+999 · CPU/);
});

// ---- OC ----
test("oc: the OC state wording of web G-150, 'no OC point' where the ratio is set at boot", () => {
  assert.equal(ocInfo(m, emptyDraft(), ["P0"]).text, "No OC ratio configured");
  assert.equal(ocInfo(m, emptyDraft(), ["EC0"]).text, "OC ratio 48× (fused top 46×, set at boot) · no OC point");
  const d = setOcRatio(m, emptyDraft(), ["P0"], 58);
  const info = ocInfo(m, d, ["P0"]);
  assert.equal(info.cls, "oc");
  assert.equal(info.text, "OC ratio 58× (fused top 57×) · OC point 9 · adaptive V");
  assert.equal(ocInfo(m, d, ["P0"], false).text, "No OC ratio configured", "as read");
  assert.equal(ocInfo(m, d, ["P0", "P1"]).cls, "mixed");
  assert.equal(ocPointNo([m.units.P0]), 9);
  assert.equal(ocPointNo([m.units.EC0]), 7);
  assert.deepEqual(offsetUnits(m, emptyDraft(), ["P0", "EC0"]).map((u) => u.id), ["EC0"], "only a unit with a ratio above its fused top");
  assert.deepEqual(offsetUnits(m, d, ["P0", "EC0"]).map((u) => u.id), ["P0", "EC0"]);
});

test("draft: clearOcEdits drops the ratio and its target together (no target is left without an OC ratio)", () => {
  let d = setVt(m, setOcRatio(m, emptyDraft(), ["P0"], 58), ["P0"], 1.5);
  d = setPoint(m, d, ["P0"], 53, -5);
  assert.deepEqual(Object.keys(d.units.P0).sort(), ["pts", "ratio", "vt"]);
  const c = clearOcEdits(m, d, ["P0"]);
  assert.deepEqual(c.units, { P0: { pts: { 53: -5 } } }, "the point edit stays");
  assert.deepEqual(clearOcEdits(m, setOcRatio(m, emptyDraft(), ["P0"], 58), ["P0"]).units, {});
  assert.equal(clearOcEdits(m, emptyDraft(), ["P0"]).units.P0, undefined);
});

// ---- inspector ----
test("inspector: titles and facts of a selection", () => {
  assert.equal(selectionTitle(m, ["P0"]), "P-core 0");
  assert.equal(selectionTitle(m, ["EC3"]), "E-cluster 3");
  assert.equal(selectionTitle(m, ["ring"]), "Ring");
  assert.equal(selectionTitle(m, quickSelections(m).P), "All 8 P-cores");
  assert.equal(selectionTitle(m, ["P0", "P2"]), "2 P-cores");
  assert.equal(selectionTitle(m, quickSelections(m).all), "All 8 P-cores + All 4 E-clusters");
  assert.equal(selectionTitle(m, ["P1", "EC0"]), "1 P-core + 1 E-cluster");
  assert.equal(selectionTitle(m, []), "Nothing selected");
  assert.deepEqual(sectionFacts(m, secOf(["P0"])), ["fused top 57×", "own max ratio 57× (0x1C)"]);
  assert.equal(sectionFacts(m, secOf(["P0", "P1"]))[1], "own max ratio mixed (55×…57×) (0x1C)");
});

// ---- the controller's pure parts ----
test("baseline: exact points, interpolated points, interpolation between measured neighbours only, nothing outside", () => {
  const rec = { units: { P0: { points: [{ ratio: 20, status: "ok", vid_v: 1.0 }, { ratio: 30, status: "ok", vid_v: 1.1 }, { ratio: 40, status: "interpolated", vid_v: 1.15 }, { ratio: 50, status: "unreachable", vid_v: null }, { ratio: 55, status: "ok", vid_v: 1.3 }] } } };
  const f = baselineFromRecord(rec);
  assert.deepEqual(f("P0", 20), { v: 1.0, src: "baseline" });
  assert.deepEqual(f("P0", 40), { v: 1.15, src: "interpolated" });
  const mid = f("P0", 25);
  assert.equal(mid.src, "interpolated");
  assert.ok(Math.abs(mid.v - 1.05) < 1e-9);
  assert.equal(f("P0", 10), null, "no extrapolation below");
  assert.equal(f("P0", 60), null, "no extrapolation above");
  assert.equal(f("P1", 20), null, "a unit the record does not have");
  assert.ok(f("P0", 50).v > 1.1 && f("P0", 50).v < 1.3, "a point nobody could measure is the line between its measured neighbours (30x and 55x)");
  assert.equal(baselineFromRecord(null)("P0", 20), null);
});

test("tune slice: pending is counted from the draft, discard and review work on the store alone", () => {
  const store = createAppStore();
  ensureTune(store);
  assert.deepEqual(store.get().tune.draft, { units: {}, turbo: {} });
  patchTune(store, { model: m });
  assert.deepEqual(store.get().pending, { changes: 0, units: 0 });
  const d = setPoint(m, setPoint(m, emptyDraft(), ["P0"], 53, -20), ["EC0"], 15, -5);
  patchTune(store, { draft: d });
  assert.deepEqual(store.get().pending, { changes: 2, units: 2 });
  assert.equal(diff(m, d).total, 2);
  const seen = [];
  store.events.addEventListener("tune:review", (e) => seen.push(e.detail));
  requestReview(store);
  assert.deepEqual(seen, [{ changes: 2, units: 2 }]);
  assert.equal(store.get().tune.reviewSeq, 1);
  discardDraft(store);
  assert.deepEqual(store.get().pending, { changes: 0, units: 0 });
  assert.deepEqual(store.get().tune.draft, { units: {}, turbo: {} });
  // a new read replaces the model: the count follows
  patchTune(store, { draft: d });
  patchTune(store, { model: null, draft: emptyDraft() });
  assert.deepEqual(store.get().pending, { changes: 0, units: 0 });
  syncPending(store);
  assert.equal(initialTune().subtab, "cores");
});

test("read duration text", () => {
  assert.equal(fmtSeconds(420), "420 ms");
  assert.equal(fmtSeconds(1440), "1.4 s");
  assert.equal(fmtSeconds(18200), "18.2 s");
});
