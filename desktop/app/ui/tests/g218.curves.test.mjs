// node --test "desktop/app/ui/tests/*.test.mjs"   (G-218 Curves: records, chart model, captions, table, layout, args, the DOM-free controller against the recorded simulator answers; no DOM, no hardware)
import test from "node:test";
import assert from "node:assert/strict";

import { Bridge } from "../bridge.js";
import { createAppStore, setLink, setPreflight } from "../store.js";
import { createMockTransport, FIXTURES } from "../fixtures/mock-backend.js";
import { wrapMockTransport, checkArgs, FIXTURES_G218 as G } from "../fixtures/mock-backend-g218.js";
import { unitModel } from "../views/stress.js";
import {
  reasonText, interpText, tuningWords, tuningKind, unitCount, curveTitle, isFusedPoint, fusedLabel, recPoints, recChartPoints, recAllPoints, sumPoints, interpAt, baselineId,
  defaultCurveId, notMeasuredLong, notMeasuredShort, unitRanges, ratiosText, unmeasuredOf, notMeasuredCaption, interpolatedCaption, curvePins, curvesModel, curveNotes, presets,
  presetOf, measureEstimate, measureArgs, measureText, progressText, libraryRow, tableModel, niceStep, vfLayout, tipRows, tipValue, VC, mergeOmitted, createCurvesController,
  exportName, copyText, TURBO_OFF_NOTE,
} from "../views/curves.js";

const pre = FIXTURES.preflight.result;
const model = unitModel(pre);
const IDS = model.ids;
const stock = G["vf-measure-stock"].response.result.record;
const dense = G["vf-measure-dense"].response.result.record;

async function rig({ seed = "none", stepMs = 1 } = {}) {
  const bridge = new Bridge(wrapMockTransport(createMockTransport({ intervalMs: 10_000 }), { stepMs, seed }), "mock");
  const store = createAppStore();
  setLink(store, await bridge.connect());
  setPreflight(store, pre);
  return { bridge, store };
}
const until = async (fn, ms = 5000) => {
  const end = Date.now() + ms;
  while (!fn()) {
    if (Date.now() > end) throw new Error("timed out: " + fn);
    await new Promise((r) => setTimeout(r, 5));
  }
};
const pt = (ratio, v, extra = {}) => ({ ratio, mhz: ratio * 100, vid_v: v, status: "ok", ...extra });
const rec = (units, extra = {}) => ({ id: "r1", name: "r1", units: Object.fromEntries(Object.entries(units).map(([u, points]) => [u, { points }])), ...extra });

// ---------------------------------------------------------------- recorded records

test("the recorded simulator record has no fused_point: every measured ratio is a chart point (the 'older curve' branch)", () => {
  const all = recAllPoints(stock, "P0");
  assert.ok(all.length >= 8);
  assert.equal(all.some((p) => p.fused_point !== undefined), false);
  const chart = recChartPoints(stock, "P0");
  assert.equal(chart.length, recPoints(stock, "P0").length);
  assert.deepEqual(chart.map((p) => p.mhz), [...chart.map((p) => p.mhz)].sort((a, b) => a - b));
  assert.equal(sumPoints(stock), Object.values(stock.units).reduce((n, u) => n + u.points.length, 0));
  assert.deepEqual(Object.keys(stock.units).sort(), ["EC0", "P0", "P1"]);
  assert.ok(Object.keys(dense.units).length === 2);
  assert.ok(sumPoints(dense) > sumPoints(stock)); // dense = every ratio
});

test("fused points: a record that marks some draws only those (G-132 item 8); the in-between ratios stay in the table", () => {
  const r = rec({ P0: [pt(8, 0.7, { fused_point: 1 }), pt(9, 0.71), pt(10, 0.72, { fused_point: 2 }), pt(11, 0.73), pt(12, 0.75, { fused_point: "oc" })] });
  assert.deepEqual(recChartPoints(r, "P0").map((p) => p.ratio), [8, 10, 12]);
  assert.equal(recPoints(r, "P0").length, 5);
  assert.equal(isFusedPoint("oc"), true);
  assert.equal(isFusedPoint(0), false);
  assert.equal(isFusedPoint(undefined), false);
  assert.equal(fusedLabel("oc"), "OC point");
  assert.equal(fusedLabel(3), "fused point 3");
  assert.deepEqual(recPoints(r, "P0").map((p) => p.fused), [1, null, 2, null, "oc"]);
});

test("recPoints leaves out mismatch / unreachable / valueless points and sorts by frequency", () => {
  const r = rec({ P0: [pt(12, 0.8), { ratio: 10, status: "unreachable", reason: "max_turbo" }, pt(8, 0.7), { ratio: 9, status: "mismatch", vid_v: 0.71 }, { ratio: 11, status: "ok" }] });
  assert.deepEqual(recPoints(r, "P0").map((p) => p.ratio), [8, 12]);
  assert.deepEqual(recPoints(null, "P0"), []);
  assert.deepEqual(recPoints(r, "ZZ"), []);
});

test("interpAt: exact within 50 MHz, a straight line between points, null outside", () => {
  const pts = [{ mhz: 800, v: 0.7 }, { mhz: 1200, v: 0.8 }];
  assert.deepEqual(interpAt(pts, 800), { v: 0.7, exact: true });
  assert.deepEqual(interpAt(pts, 1200), { v: 0.8, exact: true });
  assert.equal(interpAt(pts, 1230), null); // beyond the last point, even within 50 MHz of it
  const mid = interpAt(pts, 1000);
  assert.equal(mid.exact, false);
  assert.ok(Math.abs(mid.v - 0.75) < 1e-9);
  assert.equal(interpAt(pts, 700), null);
  assert.equal(interpAt(pts, 1300), null);
  assert.equal(interpAt([], 800), null);
});

// ---------------------------------------------------------------- chart model

const modelArgs = (over = {}) => ({ a: stock, b: null, loaded: true, listLen: 1, drawn: new Set(IDS), delta: false, band: false, live: false, liveHead: null, ids: IDS, baseline: null, fusedOf: () => null, favored: new Set(["P0"]), ...over });

test("curvesModel: one solid line per drawn unit that has points; the favored unit is marked; compare adds a dashed twin", () => {
  const m = curvesModel(modelArgs());
  assert.deepEqual(m.series.map((s) => s.key), ["P0", "P1", "EC0"]); // the record holds three units; the other nine have no points
  assert.equal(m.series[0].label, "P0 ★");
  assert.equal(m.series[0].fav, true);
  assert.equal(m.series[1].fav, false);
  assert.equal(m.ymode, "v");
  assert.deepEqual(m.pins, []); // not the baseline
  assert.deepEqual(m.unmeasured, []);
  const c = curvesModel(modelArgs({ b: dense }));
  assert.deepEqual(c.series.map((s) => s.key), ["P0", "P0:cmp", "P1", "EC0", "EC0:cmp"]); // dense holds P0 and EC0
  assert.ok(c.series.filter((s) => s.dash).every((s) => s.key.endsWith(":cmp") && s.label.endsWith("(compare)")));
  const few = curvesModel(modelArgs({ drawn: new Set(["EC0"]) }));
  assert.deepEqual(few.series.map((s) => s.key), ["EC0"]);
});

test("curvesModel: empty states say why (loading, no curve, nothing drawn, delta without compare)", () => {
  assert.match(curvesModel(modelArgs({ a: null, loaded: false })).empty, /Loading stored curves/);
  assert.match(curvesModel(modelArgs({ a: null, listLen: 0 })).empty, /No curve yet\. Run a Quick measurement/);
  assert.match(curvesModel(modelArgs({ a: null, listLen: 2 })).empty, /Loading the selected curve/);
  assert.match(curvesModel(modelArgs({ drawn: new Set() })).empty, /No units drawn/);
  assert.match(curvesModel(modelArgs({ drawn: new Set(["P5"]) })).empty, /No measured points for the units drawn/);
  const d = curvesModel(modelArgs({ delta: true }));
  assert.match(d.empty, /Choose a second curve/);
  assert.equal(d.ymode, "mv");
});

test("curvesModel: difference mode plots mV of (primary - compare) at the primary's frequencies; points outside the other curve's range are dropped", () => {
  const a = rec({ P0: [pt(10, 0.80), pt(20, 0.90), pt(60, 1.2)] });
  const b = rec({ P0: [pt(10, 0.78), pt(30, 0.94)] });
  const m = curvesModel(modelArgs({ a, b, delta: true, drawn: new Set(["P0"]) }));
  assert.equal(m.series.length, 1);
  assert.deepEqual(m.series[0].pts.map((p) => p.mhz), [1000, 2000]); // 6000 MHz is beyond b's range
  assert.ok(Math.abs(m.series[0].pts[0].v - 20) < 1e-6); // +20 mV
  assert.ok(Math.abs(m.series[0].pts[1].v - (0.9 - (0.78 + (0.94 - 0.78) * 0.5)) * 1000) < 1e-6);
  assert.equal(m.pins, undefined); // no pins / not-measured row on a difference plot
});

test("curvesModel: spread band = lowest..highest voltage over the solid lines, at every frequency that two or more units share", () => {
  const a = rec({ P0: [pt(10, 0.8), pt(20, 0.9)], P1: [pt(10, 0.82), pt(20, 0.88)], EC0: [pt(40, 1.0)] });
  const m = curvesModel(modelArgs({ a, band: true, ids: ["P0", "P1", "EC0"], drawn: new Set(["P0", "P1", "EC0"]) }));
  assert.ok(m.band && m.band.length >= 2);
  assert.deepEqual(m.band[0], { mhz: 1000, lo: 0.8, hi: 0.82 });
  assert.equal(curvesModel(modelArgs({ a, band: true, drawn: new Set(["P0"]), ids: ["P0"] })).band, null);
});

test("a live record marks the head of the unit that just got a point", () => {
  const live = { id: "live", units: { P0: { points: [pt(8, 0.7), pt(13, 0.72)] }, P1: { points: [pt(8, 0.7)] } } };
  const m = curvesModel(modelArgs({ a: live, live: true, liveHead: "P1" }));
  assert.deepEqual(m.series.map((s) => [s.key, s.live]), [["P0", false], ["P1", true]]);
});

// ---------------------------------------------------------------- not measured / interpolated / pins

const messy = rec({
  P0: [pt(8, 0.7, { fused_point: 1 }), { ratio: 56, mhz: 5600, status: "unreachable", reason: "max_turbo", fused_point: 7 }, { ratio: 57, status: "unreachable", reason: "max_turbo", fused_point: "oc" }, pt(50, 0.9, { fused_point: 6 })],
  P1: [pt(8, 0.7, { fused_point: 1 }), { ratio: 56, status: "unreachable", reason: "max_turbo", fused_point: 7 }, { ratio: 57, status: "unreachable", reason: "max_turbo", fused_point: "oc" }, pt(50, 0.9, { fused_point: 6 })],
  P2: [pt(8, 0.7, { fused_point: 1 }), { ratio: 23, status: "unreachable", reason: "cap_granularity", neighbours: [22, 24], fused_point: 3 }, { ratio: 30, status: "mismatch", fused_point: 4 }],
  P3: [pt(8, 0.7, { fused_point: 1 }), { ratio: 14, status: "interpolated", vid_v: 0.71, from: [13, 15], fused_point: 2 }, { ratio: 13, status: "ok", vid_v: 0.705, fused_point: 1 }],
  P4: [pt(8, 0.7, { fused_point: 1 }), { ratio: 22, status: "unreachable", reason: "turbo_off" }], // not a V/F point, so it is left out once the record marks fused points
});
const P_IDS = ["P0", "P1", "P2", "P3", "P4"];
const allP = new Set(P_IDS);

test("unmeasuredOf: unreachable and mismatch V/F points, with the highest measured ratio below them", () => {
  const nm = unmeasuredOf(messy, allP, P_IDS);
  assert.deepEqual(nm.map((m) => [m.unit, m.ratio, m.kind, m.max]), [["P0", 56, "max_turbo", 50], ["P0", 57, "max_turbo", 50], ["P1", 56, "max_turbo", 50], ["P1", 57, "max_turbo", 50], ["P2", 23, "cap_granularity", 8], ["P2", 30, "mismatch", 8]]);
  assert.match(nm[0].title, /^P0 56×: above this core's max ratio 50×/);
  assert.match(nm[4].title, /nearest are 22× and 24×/);
  assert.match(nm[5].title, /another ratio than the target/);
  assert.deepEqual(unmeasuredOf(messy, new Set(["P2"]), P_IDS).map((m) => m.unit), ["P2", "P2"]); // only the units drawn
  assert.deepEqual(unmeasuredOf(null, allP, P_IDS), []);
  // an older record (no fused_point anywhere) lists every unreachable ratio
  const old = rec({ P0: [pt(8, 0.7), { ratio: 22, status: "unreachable", reason: "turbo_off" }] });
  assert.equal(unmeasuredOf(old, new Set(["P0"]), ["P0"]).length, 1);
});

test("notMeasuredCaption groups by reason, then by the units sharing the same ratios, in unit order", () => {
  const caption = notMeasuredCaption(unmeasuredOf(messy, allP, P_IDS), P_IDS);
  assert.equal(caption, "not measured: P0, P1 at 56–57× (above core max 50×) · P2 at 23× (cpufreq cannot pin this ratio) · P2 at 30× (did not settle at the target)");
  assert.equal(notMeasuredCaption([], P_IDS), "");
});

test("interpolatedCaption lists the hollow-marker V/F points", () => {
  assert.equal(interpolatedCaption(messy, allP, P_IDS), "interpolated (hollow markers): P3 at 14×");
  assert.equal(interpolatedCaption(stock, new Set(IDS), IDS), "");
});

test("wording helpers", () => {
  assert.equal(unitRanges(["P1", "P2", "P4", "P5", "P6", "P7"]), "P1, P2, P4–P7");
  assert.equal(unitRanges(["P0", "P1"]), "P0, P1");
  assert.equal(unitRanges(["EC0", "EC1", "EC2", "P0"]), "EC0–EC2, P0");
  assert.equal(ratiosText([57, 56, 23, 29]), "23, 29, 56–57×");
  assert.equal(notMeasuredShort("max_turbo", null), "above core max");
  assert.equal(notMeasuredShort("user_cap"), "above your cpufreq cap");
  assert.equal(notMeasuredShort("whatever"), "not reachable");
  assert.equal(notMeasuredLong("max_turbo", 55), "above this core's max ratio 55×");
  assert.match(reasonText("turbo_off"), /switch to Balanced or Performance/);
  assert.match(reasonText("active_cores"), /few active cores/);
  assert.match(reasonText("cpufreq_granularity"), /cpufreq caps cannot pin/);
  assert.match(reasonText("cap_granularity", { ratio: 23, neighbours: [22] }), /exactly 23×.*nearest is 22×/);
  assert.equal(reasonText("nope"), "not reachable on this core");
  assert.equal(interpText({ from: [13, 15] }), "interpolated between 13× and 15×");
  assert.equal(interpText({ exact: true, from: [13, 15] }), "exact: both neighbours equal (13× and 15×)");
  assert.equal(interpText(null), "interpolated");
  assert.equal(TURBO_OFF_NOTE.startsWith("Turbo is off"), true);
});

test("curvePins: the fused top point diamond, only on the baseline, only for drawn units that were measured", () => {
  const base = rec({ P0: [pt(8, 0.7), pt(57, 1.2)], P1: [pt(8, 0.7)] }, { id: "B" });
  const fusedOf = (u) => ({ P0: { ratio: 57, v: 1.19 }, P1: { ratio: 55, v: 1.18 }, P2: { ratio: 55, v: 1.1 } }[u] || null);
  const pins = curvePins(base, { drawn: new Set(["P0", "P1", "P2"]), ids: ["P0", "P1", "P2"], baseline: "B", fusedOf });
  assert.deepEqual(pins.map((p) => [p.unit, p.kind, p.mhz, p.v]), [["P0", "fused", 5700, 1.19], ["P1", "fused", 5500, 1.18]]); // P2 has no points on this curve
  assert.match(pins[0].title, /measured 1\.2000 V \(\+10(\.0)? mV\)/);
  assert.match(pins[1].title, /not measured on this curve/);
  assert.deepEqual(curvePins(base, { drawn: new Set(["P0"]), ids: ["P0"], baseline: "other", fusedOf }), []);
  assert.deepEqual(curvePins(base, { drawn: new Set(["P0"]), ids: ["P0"], baseline: "B", fusedOf: () => null }), []);
  const m = curvesModel(modelArgs({ a: base, baseline: "B", fusedOf, ids: ["P0", "P1"], drawn: new Set(["P0", "P1"]) }));
  assert.equal(m.pins.length, 2);
});

test("curveNotes: baseline wording follows what is known; the stock pill needs a stock-tuned curve", () => {
  const list = [{ id: "B", name: "stock", baseline: true }, { id: "C", name: "other" }];
  const base = { ...stock, id: "B" };
  const n1 = curveNotes({ a: base, list, live: false, baseline: "B", drawn: new Set(IDS), ids: IDS, wordsRead: false });
  assert.match(n1.base, /^Stock baseline: this curve is the fused V\/F table as applied.*Read ratios & voltages \(Tune\)/);
  assert.match(curveNotes({ a: base, list, live: false, baseline: "B", drawn: new Set(IDS), ids: IDS, wordsRead: true }).base, /Diamonds mark the top-point voltage/);
  assert.equal(curveNotes({ a: { ...stock, id: "C" }, list, live: false, baseline: "B", drawn: new Set(), ids: IDS, wordsRead: false }).base, "Stock baseline: stock. Predictions in Tune are made on it.");
  assert.match(curveNotes({ a: stock, list: [], live: false, baseline: null, drawn: new Set(), ids: IDS, wordsRead: false }).base, /No curve is marked as the stock baseline yet/);
  assert.equal(n1.stock.show, false); // tuning state unknown in the simulator record
  assert.equal(curveNotes({ a: { ...stock, tuning_state: { kind: "stock" } }, list, live: false, baseline: null, drawn: new Set(), ids: IDS, wordsRead: false }).stock.show, true);
  assert.equal(curveNotes({ a: { ...stock, tuning_state: { kind: "stock" } }, list, live: true, baseline: null, drawn: new Set(), ids: IDS, wordsRead: false }).stock.show, false);
  assert.equal(curveNotes({ a: null, list, live: false, baseline: null, drawn: new Set(), ids: IDS, wordsRead: false }).unreach, "");
});

// ---------------------------------------------------------------- library, default curve, presets

test("defaultCurveId: the baseline, else the newest curve of every unit, else the newest", () => {
  const all = IDS.slice();
  const list = [{ id: "n", complete: true, units: ["P0"] }, { id: "full", complete: true, units: all }, { id: "old", complete: true, units: all, baseline: false }];
  assert.equal(defaultCurveId(list, IDS), "full");
  assert.equal(defaultCurveId([{ id: "b", baseline: true, units: [] }, ...list], IDS), "b");
  assert.equal(defaultCurveId([{ id: "p", complete: false, units: all }, { id: "x", units: ["P0"] }], IDS), "p"); // newest, as nothing is both complete and whole
  assert.equal(defaultCurveId([], IDS), null);
  assert.equal(baselineId(list), null);
  assert.equal(baselineId([{ id: "z", baseline: true }]), "z");
});

test("presets: Favored exists only when the topology marks some, not all, P-cores", () => {
  const p = presets(model);
  assert.deepEqual(p.map((x) => x.value), ["all", "P", "E", "fav"]);
  assert.deepEqual(p[3].units, ["P0", "P3"]);
  assert.match(p[3].title, /P0, P3.*acpi_cppc/);
  assert.equal(presetOf(p, new Set(IDS)), "all");
  assert.equal(presetOf(p, new Set(["P0", "P3"])), "fav");
  assert.equal(presetOf(p, new Set(["P1"])), "");
  const none = unitModel({ topology: { units: pre.topology.units.map((u) => ({ ...u, favored: false })) } });
  assert.deepEqual(presets(none).map((x) => x.value), ["all", "P", "E"]);
});

test("libraryRow / titles / tuning words", () => {
  const c = { id: "20261018T101500Z-ab12", name: "stock", created: "2026-10-18T10:15:00Z", mode: "quick", complete: false, baseline: true, tuning_state: { kind: "words" }, units: ["P0", "P1"] };
  const r = libraryRow(c, { primary: c.id });
  assert.deepEqual([r.current, r.name, r.baseline, r.partial, r.mode, r.units, r.tuning.text, r.tuning.tone], [true, "stock", true, true, "quick", 2, "tuned", "pending"]);
  assert.equal(libraryRow({ id: "x" }).name, "x");
  assert.equal(libraryRow({ id: "x", tuning_state: { kind: "stock" } }).tuning.tone, "ok");
  assert.equal(tuningKind({ tuning_state: "stock" }), "stock");
  assert.equal(tuningKind({}), "unknown");
  assert.match(tuningWords("unknown"), /Tuning state unknown/);
  assert.match(tuningWords("words"), /read from the CPU/);
  assert.equal(unitCount({ units: { P0: {}, P1: {} } }), 2);
  assert.match(curveTitle(c), /^stock · .* · quick$/);
  assert.equal(exportName({ id: "a/b c" }), "arltuner-curve-a_b_c.json");
});

// ---------------------------------------------------------------- table

test("tableModel (volts): ratios are the union over units, a missing ratio is empty, heat classes span 0..9", () => {
  const a = rec({ P0: [pt(8, 0.70), pt(9, 0.75), pt(10, 0.80)], EC0: [pt(8, 0.65), { ratio: 9, status: "unreachable", reason: "max_turbo" }, { ratio: 12, status: "mismatch" }] });
  const t = tableModel(a, null, { delta: false, ids: ["P0", "EC0"] });
  assert.deepEqual(t.ratios, [8, 9, 10, 12]);
  assert.equal(t.delta, false);
  assert.equal(t.canDelta, false);
  const p0 = t.rows[0].cells;
  assert.deepEqual(p0.map((c) => c.kind), ["val", "val", "val", "none"]);
  assert.deepEqual(p0.map((c) => c.text), ["0.700", "0.750", "0.800", ""]);
  assert.equal(p0[2].cls, "vt hv-9");
  assert.equal(t.rows[1].cells[0].cls, "vt hv-0");
  assert.equal(t.rows[1].cells[1].text, "—");
  assert.match(t.rows[1].cells[1].title, /EC0 at 9×: unreachable: above this core's max ratio|max turbo ratio is lower/);
  assert.match(t.rows[1].cells[3].title, /mismatch/);
  assert.equal(t.note, "Volts requested by the core at each ratio. Darker is lower.");
});

test("tableModel (difference to the baseline): mV, signed, buckets by the largest difference, '~' where the baseline is interpolated", () => {
  const base = rec({ P0: [pt(8, 0.70), pt(12, 0.90)] }, { name: "stock" });
  const a = rec({ P0: [pt(8, 0.66), pt(10, 0.82), pt(12, 0.90)] });
  const t = tableModel(a, base, { delta: true, ids: ["P0"] });
  assert.equal(t.delta, true);
  assert.deepEqual(t.rows[0].cells.map((c) => c.text), ["−40", "+20~", "0"]);
  assert.equal(t.rows[0].cells[0].cls, "vt dv-m3");
  assert.match(t.rows[0].cells[2].cls, /dv-p0/);
  assert.equal(t.note, "mV difference to the baseline curve (stock). Negative means lower voltage.");
  // the baseline itself cannot be compared with itself; a live record can
  assert.equal(tableModel(base, base, { delta: true, ids: ["P0"], primaryIsBaseline: true }).canDelta, false);
  assert.equal(tableModel(base, base, { delta: true, ids: ["P0"], primaryIsBaseline: false, live: true }).canDelta, true);
  assert.equal(tableModel(a, null, { delta: true, ids: ["P0"] }).delta, false); // no baseline: volts
  assert.equal(tableModel(null, base, { delta: false, ids: ["P0"] }).empty, "n/a: no curve selected yet.");
  // interpolated points are marked "~" unless exact
  const ip = rec({ P0: [pt(8, 0.7), { ratio: 9, status: "interpolated", vid_v: 0.71, from: [8, 10] }, { ratio: 10, status: "interpolated", vid_v: 0.72, exact: true }] });
  assert.deepEqual(tableModel(ip, null, { delta: false, ids: ["P0"] }).rows[0].cells.map((c) => c.text), ["0.700", "0.710~", "0.720"]);
});

test("tableModel on the recorded dense curve: a value or an explicit n/a, never 0", () => {
  const t = tableModel(dense, null, { delta: false, ids: IDS });
  assert.deepEqual(t.rows.map((r) => r.unit), ["P0", "EC0"]);
  assert.ok(t.ratios.length >= 20);
  for (const row of t.rows) for (const c of row.cells) {
    if (c.kind === "val") assert.ok(Number(c.text.replace("~", "")) > 0.5, `${row.unit} ${c.text}`);
    else assert.ok(c.kind === "none" || c.kind === "na", `${row.unit} ${c.kind}`); // a ratio the unit never reached is "—", never 0
  }
});

// ---------------------------------------------------------------- chart layout and tooltip

test("niceStep", () => {
  assert.equal(niceStep(0.6, 6), 0.1);
  assert.equal(niceStep(60, 6), 10);
  assert.equal(niceStep(0.35, 6), 0.05);
});

test("vfLayout: the x axis is rounded to 500 MHz, the voltage range has a minimum span, the height follows the width", () => {
  const m = curvesModel(modelArgs());
  const lay = vfLayout(m, 960);
  assert.equal(lay.empty, false);
  assert.equal(lay.xDom[0] % 500, 0);
  assert.equal(lay.xDom[1] % 500, 0);
  assert.ok(lay.xDom[0] <= 800 && lay.xDom[1] >= 5700);
  assert.equal(lay.H, Math.round(960 * 0.38));
  assert.ok(lay.yDom[0] <= 0.69 && lay.yDom[1] >= 1.04);
  assert.ok(lay.sx(lay.xDom[0]) === VC.L && Math.abs(lay.sx(lay.xDom[1]) - (960 - VC.R)) < 1e-9);
  assert.ok(lay.sy(lay.yDom[1]) === VC.T);
  assert.equal(vfLayout(m, 100).W, 100); // the clamp to 560..1800 is the DOM's (host width), not the geometry's
  const flat = vfLayout({ series: [{ pts: [{ mhz: 1000, v: 0.9 }, { mhz: 2000, v: 0.9 }] }], ymode: "v" }, 960);
  assert.ok(flat.yDom[1] - flat.yDom[0] >= 0.05 - 1e-9);
  assert.equal(vfLayout({ series: [], markers: [], ymode: "v" }, 960).empty, true);
  assert.equal(vfLayout(curvesModel(modelArgs({ a: null, loaded: false })), 960).empty, true);
});

test("vfLayout: a difference plot always contains 0 mV; the not-measured row grows the chart by one lane per unit and widens only the x axis", () => {
  const a = rec({ P0: [pt(10, 0.8), pt(20, 0.9)] });
  const b = rec({ P0: [pt(10, 0.78), pt(20, 0.88)] });
  const d = vfLayout(curvesModel(modelArgs({ a, b, delta: true, ids: ["P0"], drawn: new Set(["P0"]) })), 960);
  assert.ok(d.yDom[0] <= 0 && d.yDom[1] >= 20);
  const plain = vfLayout({ series: [{ pts: [{ mhz: 1000, v: 0.8 }, { mhz: 2000, v: 0.9 }] }], ymode: "v" }, 960);
  const nm = vfLayout({ series: [{ pts: [{ mhz: 1000, v: 0.8 }, { mhz: 2000, v: 0.9 }] }], ymode: "v", unmeasured: [{ unit: "P0", mhz: 5600 }, { unit: "P1", mhz: 5600 }] }, 960);
  assert.equal(nm.lanes.length, 2);
  assert.equal(nm.H - plain.H, 16 + 2 * 5);
  assert.equal(nm.B - plain.B, 16 + 2 * 5);
  assert.deepEqual(nm.yDom, plain.yDom); // never the voltage range
  assert.ok(nm.xDom[1] > plain.xDom[1]);
});

test("tipRows: every series with a value at that frequency, highest first; '~' for an estimate; mV mode formats signed", () => {
  const m = { ymode: "v", series: [{ key: "a", unit: "P0", pts: [{ mhz: 1000, v: 0.8 }, { mhz: 2000, v: 0.9 }] }, { key: "b", unit: "P1", pts: [{ mhz: 1500, v: 1.0 }, { mhz: 2500, v: 1.1 }] }] };
  const rows = tipRows(m, 1500);
  assert.deepEqual(rows.map((r) => [r.s.key, r.exact]), [["b", true], ["a", false]]);
  assert.deepEqual(tipRows(m, 2400).map((r) => r.s.key), ["b"]);
  assert.deepEqual(tipRows(m, 100), []);
  assert.equal(tipValue(m, 0.9), "0.900 V");
  assert.equal(tipValue({ ymode: "mv" }, -12.34), "−12.3 mV");
  assert.equal(tipValue({ ymode: "mv" }, 20), "+20 mV");
});

// ---------------------------------------------------------------- measuring: args, text, progress

test("measureEstimate scales the 12-unit reference time per unit", () => {
  assert.equal(measureEstimate("quick", 12), "Quick, 12 units, about 2 min");
  assert.equal(measureEstimate("quick", 1), "Quick, 1 unit, about 10 s");
  assert.equal(measureEstimate("dense", 12), "Dense, 12 units, about 10 min");
  assert.equal(measureEstimate("dense", 6), "Dense, 6 units, about 5 min");
});

test("measureArgs: units omitted when every unit is measured, topology order otherwise; name trimmed, omitted when empty; always valid for ops.rs", () => {
  const cv = { mode: "quick", sel: null, name: "" };
  assert.deepEqual(measureArgs(cv, IDS), { mode: "quick" });
  cv.sel = new Set(["EC1", "P3", "P0"]);
  cv.mode = "dense";
  cv.name = "  my curve  ";
  assert.deepEqual(measureArgs(cv, IDS), { mode: "dense", units: ["P0", "P3", "EC1"], name: "my curve" });
  cv.name = "   ";
  assert.equal("name" in measureArgs(cv, IDS), false);
  cv.name = "x".repeat(100);
  assert.equal(measureArgs(cv, IDS).name.length, 64);
  for (const mode of ["quick", "dense"]) {
    for (const sel of [null, new Set(IDS), new Set(["P0"]), new Set(["P1", "EC3"])]) {
      for (const name of ["", " a ", "x".repeat(70), "é ü"]) {
        const args = measureArgs({ mode, sel, name }, IDS);
        assert.equal(checkArgs("vf.measure", args), null, JSON.stringify(args));
      }
    }
  }
  assert.ok(checkArgs("vf.measure", { mode: "fast" }));
  assert.ok(checkArgs("vf.measure", { mode: "quick", units: [] }));
});

test("measureText: running / gate / topology / empty selection / ready", () => {
  const cv = { mode: "quick", sel: null };
  assert.match(measureText(cv, IDS, {}), /^Ready: Quick, 12 units, about 2 min\. Raises the load/);
  assert.match(measureText({ mode: "dense", sel: new Set(["P0"]) }, IDS, {}), /Dense, 1 unit, about 50 s/);
  assert.equal(measureText({ mode: "quick", sel: new Set() }, IDS, {}), "Select at least one unit.");
  assert.equal(measureText(cv, IDS, { gate: "The helper is busy." }), "The helper is busy.");
  assert.match(measureText(cv, IDS, { running: true, gate: "x" }), /^Measuring\. Each unit is capped one ratio at a time/);
  assert.match(measureText(cv, [], {}), /topology is not known/);
});

test("progressText", () => {
  assert.deepEqual(progressText({ step: 3, of: 20, unit: "P0", ratio: 18, eta_s: 40 }), { step: 3, of: 20, text: "P0 · 18× · point 3 of 20 · about 40 s left" });
  assert.equal(progressText({ step: 0, of: 20, unit: null, ratio: null, eta_s: 150 }).text, "point 0 of 20 · about 3 min left");
  assert.equal(progressText({ step: 25, of: 20 }).step, 20);
  assert.equal(progressText({ elapsed_s: 3, of_s: 30 }), null);
  assert.equal(progressText({ step: 1, of: 0 }), null);
  assert.equal(progressText(null), null);
});

// ---------------------------------------------------------------- mergeOmitted (the simulator never omits units: synthetic)

test("mergeOmitted fetches the omitted units with curves.get {id, units} until none are left or no progress is made", async () => {
  const calls = [];
  const full = { P0: { points: [1] }, P1: { points: [2] }, P2: { points: [3] } };
  const bridge = {
    call: async (op, args) => {
      calls.push([op, args]);
      const want = args.units.slice(0, 2); // the helper's line limit: two units per answer
      return { ok: true, result: { id: args.id, units: Object.fromEntries(want.map((u) => [u, full[u]])), omitted_units: args.units.filter((u) => !want.includes(u)) } };
    },
  };
  const r = await mergeOmitted(bridge, { id: "r", units: { P0: full.P0 }, omitted_units: ["P1", "P2"] });
  assert.deepEqual(Object.keys(r.units).sort(), ["P0", "P1", "P2"]);
  assert.deepEqual(r.omitted_units, []);
  assert.equal(calls.length, 1);
  // no progress: the helper keeps omitting; one round, then stop
  const stuck = await mergeOmitted({ call: async () => ({ ok: true, result: { id: "r", units: {}, omitted_units: ["P1"] } }) }, { id: "r", units: {}, omitted_units: ["P1"] });
  assert.deepEqual(stuck.omitted_units, ["P1"]);
  // an error leaves the record as it is
  const err = await mergeOmitted({ call: async () => ({ ok: false, error: { code: "X", message: "no" } }) }, { id: "r", units: {}, omitted_units: ["P1"] });
  assert.deepEqual(err.omitted_units, ["P1"]);
  assert.deepEqual(await mergeOmitted(bridge, { id: "plain", units: {} }), { id: "plain", units: {} });
});

// ---------------------------------------------------------------- controller against the recorded simulator answers

test("controller: Quick measure -> live record filled point by point -> result cached, listed, selected, status says what was stored", async () => {
  const { bridge, store } = await rig();
  const ctl = createCurvesController({ bridge, store });
  await ctl.loadCurves();
  assert.equal(ctl.cv.loaded, true);
  assert.deepEqual(ctl.cv.list, []);
  assert.equal(ctl.cv.primary, null);
  ctl.cv.name = "  mine ";
  let sawLive = 0;
  ctl.subscribe(() => { if (ctl.cv.live && sumPoints(ctl.cv.live) > 0) sawLive = Math.max(sawLive, sumPoints(ctl.cv.live)); });
  assert.equal(await ctl.startMeasure(), true);
  assert.ok(ctl.cv.live && ctl.cv.live.name === "mine" && ctl.cv.view === "chart");
  assert.match(ctl.view.status, /^Measuring \(operation /);
  await until(() => ctl.view.run === null && ctl.cv.list.length === 1);
  assert.ok(sawLive > 5, "the live record grew while measuring");
  assert.equal(ctl.cv.live, null);
  assert.equal(ctl.cv.list[0].name, "mine");
  assert.equal(ctl.cv.primary, ctl.cv.list[0].id);
  assert.ok(ctl.cv.cache[ctl.cv.primary]);
  assert.match(ctl.view.status, /^Measured \d+ points\. Stored as "mine"\. Tuning state unknown/);
  assert.equal(sumPoints(ctl.cv.cache[ctl.cv.primary]), sumPoints(stock));
  assert.deepEqual(ctl.view.progress.step, ctl.view.progress.of);
  const m = ctl.model();
  assert.ok(m.series.length >= 3);
  await bridge.close();
});

test("controller: the live record is a model of its own points (unit, ratio, mhz, vid_v), a retry replaces the first attempt", async () => {
  let emit = () => {};
  const bridge = new Bridge({
    connect: async () => ({ state: "connected" }),
    call: async (op) => (op === "vf.measure" ? { id: "m1" } : { ok: true, result: { curves: [] } }),
    listen: (ev, cb) => {
      if (ev === "vf.point") emit = cb;
      return () => {};
    },
  });
  const store = createAppStore();
  setLink(store, await bridge.connect());
  setPreflight(store, pre);
  const ctl = createCurvesController({ bridge, store });
  assert.equal(await ctl.startMeasure(), true);
  emit({ op_id: "m1", unit: "P0", ratio: 8, mhz: 800, vid_v: 0.69, status: "ok" });
  emit({ op_id: "m1", unit: "P0", ratio: 13, vid_v: 0.7, status: "ok" });
  emit({ op_id: "m1", unit: "P0", ratio: 13, mhz: 1300, vid_v: 0.71, status: "ok", retry: true });
  emit({ op_id: "other", unit: "P1", ratio: 8, vid_v: 0.7, status: "ok" }); // another op
  emit({ op_id: "m1", unit: "ZZ", ratio: 8, vid_v: 0.7, status: "ok" }); // not a unit of this CPU
  emit({ op_id: "m1", unit: "P0", ratio: 20, status: "mismatch" });
  assert.deepEqual(ctl.cv.live.units.P0.points, [
    { ratio: 8, mhz: 800, vid_v: 0.69, status: "ok" },
    { ratio: 13, mhz: 1300, vid_v: 0.71, status: "ok" },
    { ratio: 20, mhz: 2000, vid_v: null, status: "mismatch" },
  ]);
  assert.equal(ctl.cv.liveHead, "P0");
  assert.equal(ctl.cv.live.units.P1, undefined);
  assert.deepEqual(recPoints(ctl.cv.live, "P0").map((p) => p.ratio), [8, 13]); // the mismatch is not drawn
});

test("controller: Dense measure for the chosen units; the units go in the request (topology order)", async () => {
  const { bridge, store } = await rig();
  const ctl = createCurvesController({ bridge, store });
  ctl.cv.mode = "dense";
  ctl.cv.sel = new Set(["EC0", "P0"]);
  let asked = null;
  const real = bridge.call.bind(bridge);
  bridge.call = (op, args) => {
    if (op === "vf.measure") asked = args;
    return real(op, args);
  };
  assert.equal(await ctl.startMeasure(), true);
  assert.deepEqual(asked, { mode: "dense", units: ["P0", "EC0"] });
  await until(() => ctl.cv.list.length === 1);
  assert.equal(ctl.cv.list[0].mode, "dense");
  assert.ok(sumPoints(ctl.cv.cache[ctl.cv.primary]) > sumPoints(stock));
  await bridge.close();
});

test("controller: Cancel mid-run -> CANCELLED with detail.curve_id -> the partial curve is kept, listed as partial and selected", async () => {
  const { bridge, store } = await rig({ stepMs: 8 });
  const ctl = createCurvesController({ bridge, store });
  ctl.cv.mode = "dense";
  await ctl.startMeasure();
  await until(() => ctl.cv.live && sumPoints(ctl.cv.live) > 3);
  assert.equal(await ctl.cancel(), true);
  assert.equal(ctl.view.cancelling, true);
  assert.match(ctl.view.status, /Cancel requested/);
  await until(() => ctl.view.run === null && ctl.cv.list.length === 1);
  assert.equal(ctl.view.cancelling, false);
  assert.match(ctl.view.status, /^Cancelled\. The points measured so far were kept as a partial curve/);
  assert.equal(ctl.cv.list[0].complete, false);
  assert.equal(libraryRow(ctl.cv.list[0]).partial, true);
  assert.equal(ctl.cv.primary, ctl.cv.list[0].id);
  assert.equal(ctl.cv.live, null);
  assert.ok(sumPoints(ctl.cv.cache[ctl.cv.primary]) > 0);
  await bridge.close();
});

test("controller: a second measure while one runs is refused (locally and by the helper's BUSY); nothing is sent when the helper is not idle or no unit is chosen", async () => {
  const { bridge, store } = await rig({ stepMs: 20 });
  const ctl = createCurvesController({ bridge, store });
  assert.equal(await ctl.startMeasure(), true);
  assert.equal(await ctl.startMeasure(), false);
  const direct = await bridge.call("vf.measure", { mode: "quick" });
  assert.equal(direct.error.code, "BUSY");
  assert.match(ctl.gate(), /operation is running/);
  await ctl.cancel();
  await until(() => ctl.view.run === null);
  ctl.cv.sel = new Set();
  assert.equal(await ctl.startMeasure(), false);
  ctl.cv.sel = null;
  store.update((s) => ({ helperOp: { ...s.helperOp, state: "stressing" } }));
  assert.match(ctl.gate(), /helper is stressing/);
  assert.equal(await ctl.startMeasure(), false);
  await bridge.close();
});

test("controller: a run begun elsewhere is adopted from its vf.point / progress events and its result is kept", async () => {
  const { bridge, store } = await rig({ stepMs: 6 });
  const ctl = createCurvesController({ bridge, store });
  const first = await bridge.call("vf.measure", { mode: "quick", name: "elsewhere" });
  await until(() => ctl.view.run && ctl.cv.live);
  assert.equal(ctl.view.run.id, first.id);
  await until(() => ctl.view.run === null && ctl.cv.list.length === 1);
  assert.equal(ctl.cv.list[0].name, "elsewhere");
});

test("controller: a tune.read / tune.apply in progress (recorded events) is never adopted as a measurement; step/of progress without a 'measuring' state is ignored", async () => {
  const emitters = {};
  const bridge = new Bridge({ connect: async () => ({ state: "connected" }), call: async () => ({ ok: true, result: { curves: [] } }), listen: (ev, cb) => { emitters[ev] = cb; return () => {}; } });
  const store = createAppStore();
  setLink(store, await bridge.connect());
  setPreflight(store, pre);
  const ctl = createCurvesController({ bridge, store });
  for (const events of [FIXTURES.tuneReadEvents, FIXTURES.tuneApplyEvents]) {
    for (const { event, ...payload } of events) emitters[event](payload);
    assert.equal(ctl.view.run, null);
    assert.equal(ctl.cv.live, null);
    assert.equal(ctl.view.progress, null);
    assert.equal(ctl.cv.view, "chart");
  }
  emitters.progress({ op_id: "x9", step: 1, of: 20, unit: "P0", ratio: 8 }); // no state seen for this op at all
  assert.equal(ctl.view.run, null);
  emitters.heartbeat({ op_id: "x9", state: "measuring", ops_used: 1, ops_budget: 100 }); // a heartbeat names the op's state too
  emitters.progress({ op_id: "x9", step: 2, of: 20, unit: "P0", ratio: 13 });
  assert.equal(ctl.view.run.id, "x9");
  assert.equal(ctl.view.progress.step, 2);
});

test("controller: failed result with a partial curve keeps it; one without shows Failed:", async () => {
  let emit = () => {};
  const list = [];
  let n = 0;
  const bridge = new Bridge({
    connect: async () => ({ state: "connected" }),
    call: async (op, args) => {
      if (op === "vf.measure") return { id: `m${++n}` };
      if (op === "curves.list") return { ok: true, result: { curves: list, no_turbo: true } };
      if (op === "curves.get") return { ok: true, result: { id: args.id, name: "p", units: { P0: { points: [pt(8, 0.7)] } } } };
      return { ok: true, result: {} };
    },
    listen: (ev, cb) => {
      if (ev === "result") emit = cb;
      return () => {};
    },
  });
  const store = createAppStore();
  setLink(store, await bridge.connect());
  setPreflight(store, pre);
  const ctl = createCurvesController({ bridge, store });
  await ctl.startMeasure();
  emit({ id: "m1", ok: false, error: { code: "REFUSED", message: "the CPU is too hot", detail: { problems: ["a", "a", "b"] } } });
  await until(() => ctl.view.run === null);
  assert.equal(ctl.view.status, "Failed: the CPU is too hot");
  assert.deepEqual(ctl.view.problems, ["a", "b"]); // each note once
  assert.equal(ctl.cv.live, null);
  list.push({ id: "20261018T101500Z-ab12", name: "p", units: ["P0"], complete: false });
  await ctl.startMeasure();
  emit({ id: "m2", ok: false, error: { code: "IO", message: "stress-ng died", detail: { curve_id: "20261018T101500Z-ab12" } } });
  await until(() => ctl.view.run === null && ctl.cv.primary === "20261018T101500Z-ab12");
  assert.equal(ctl.view.status, "Failed: stress-ng died The partial curve was kept.");
  assert.equal(ctl.cv.noTurbo, true); // curves.list says the power-saver profile turned turbo off
});

test("controller: rename, baseline (one at a time), delete, compare and the difference toggle", async () => {
  const { bridge, store } = await rig({ seed: "recorded" });
  const ctl = createCurvesController({ bridge, store });
  await ctl.loadCurves();
  assert.equal(ctl.cv.list.length, 2);
  const baseId = G["vf-measure-stock"].response.result.record.id;
  const denseId = G["vf-measure-dense"].response.result.record.id;
  assert.equal(ctl.cv.primary, baseId); // the baseline is shown first
  assert.ok(ctl.cv.cache[baseId]);
  await ctl.setCompare(denseId);
  assert.equal(ctl.cv.compare, denseId);
  assert.ok(ctl.cv.cache[denseId]);
  ctl.cv.delta = true;
  await ctl.select(denseId); // choosing the compared curve as primary clears the comparison
  assert.equal(ctl.cv.compare, null);
  assert.equal(ctl.cv.delta, false);
  assert.equal(await ctl.rename(denseId, "   "), false);
  assert.equal(ctl.view.status, "A name cannot be empty.");
  assert.equal(await ctl.rename(denseId, " renamed "), true);
  assert.equal(ctl.view.status, "Renamed.");
  assert.equal(ctl.cv.list.find((c) => c.id === denseId).name, "renamed");
  assert.equal(ctl.cv.cache[denseId].name, "renamed"); // refetched, not stale
  const dense_ = ctl.cv.list.find((c) => c.id === denseId);
  assert.equal(await ctl.toggleBaseline(dense_), true);
  assert.equal(ctl.cv.list.filter((c) => c.baseline).length, 1);
  assert.equal(ctl.cv.list.find((c) => c.baseline).id, denseId);
  assert.match(ctl.view.status, /"renamed" is now the stock baseline/);
  assert.equal(await ctl.toggleBaseline(ctl.cv.list.find((c) => c.id === denseId)), true);
  assert.equal(ctl.view.status, "Baseline cleared.");
  assert.equal(await ctl.remove(ctl.cv.list.find((c) => c.id === denseId)), true);
  assert.equal(ctl.view.status, "Deleted.");
  assert.equal(ctl.cv.list.length, 1);
  assert.equal(ctl.cv.primary, baseId); // the deleted curve was shown: the default is picked again
  const gone = await bridge.call("curves.get", { id: denseId });
  assert.equal(gone.error.code, "NOT_FOUND");
  await bridge.close();
});

test("controller: compare is cleared when the compared curve is deleted; export gives the stored record as JSON", async () => {
  const { bridge, store } = await rig({ seed: "recorded" });
  const ctl = createCurvesController({ bridge, store });
  await ctl.loadCurves();
  const baseId = G["vf-measure-stock"].response.result.record.id;
  const denseId = G["vf-measure-dense"].response.result.record.id;
  await ctl.setCompare(denseId);
  const text = await ctl.recordJson({ id: baseId });
  const parsed = JSON.parse(text);
  assert.equal(parsed.id, baseId);
  assert.deepEqual(Object.keys(parsed.units).sort(), ["EC0", "P0", "P1"]);
  assert.equal(await ctl.recordJson({ id: "20261018T000000Z-zzzz" }), null);
  await ctl.remove({ id: denseId });
  assert.equal(ctl.cv.compare, null);
  await bridge.close();
});

test("controller: tune.last gives the fused top points for the baseline's diamonds (read-only; the mock has the recorded read)", async () => {
  const { bridge, store } = await rig({ seed: "recorded" });
  const ctl = createCurvesController({ bridge, store });
  assert.equal(ctl.fusedOf("P0"), null);
  await ctl.loadWords();
  assert.ok(ctl.cv.words && ctl.cv.words.order.length >= 8);
  const f = ctl.fusedOf("P0");
  assert.ok(f && f.ratio > 40 && f.v > 0.5, JSON.stringify(f));
  assert.equal(ctl.fusedOf("ZZ"), null);
  await ctl.loadCurves();
  const m = ctl.model();
  assert.ok(m.pins.length >= 1); // the seeded stock curve is the baseline
  assert.ok(m.pins.every((p) => p.kind === "fused" && p.title.includes("cmd 0x09")));
  await bridge.close();
});

test("controller: no helper connected -> the gate says so and nothing is started", async () => {
  const bridge = new Bridge(wrapMockTransport(createMockTransport({ intervalMs: 10_000 }), { stepMs: 1 }), "mock");
  const store = createAppStore();
  const ctl = createCurvesController({ bridge, store });
  assert.match(ctl.gate(), /not connected/);
  assert.equal(await ctl.startMeasure(), false);
});

test("controller: curves.list failing is shown as 'Stored curves unavailable'", async () => {
  const bridge = new Bridge({ connect: async () => ({ state: "connected" }), call: async () => ({ ok: false, error: { code: "BUSY", message: "slow ops are running" } }), listen: () => () => {} });
  const store = createAppStore();
  setLink(store, await bridge.connect());
  const ctl = createCurvesController({ bridge, store });
  await ctl.loadCurves();
  assert.equal(ctl.cv.loaded, true);
  assert.equal(ctl.view.listProblem, "Stored curves unavailable: slow ops are running");
});

test("mock ops refuse the curve arguments ops.rs refuses", async () => {
  const { bridge } = await rig({ seed: "recorded" });
  for (const [op, args] of [["curves.get", { id: "nope" }], ["curves.get", { id: "20261018T101500Z-ab12", units: ["X"] }], ["curves.update", { id: "20261018T101500Z-ab12" }], ["curves.update", { id: "20261018T101500Z-ab12", name: "  " }], ["curves.delete", { id: "20261018T101500Z-ab12", name: "x" }], ["vf.measure", { mode: "slow" }], ["vf.measure", { units: [] }]]) {
    const r = await bridge.call(op, args);
    assert.equal(r.ok, false, `${op} ${JSON.stringify(args)}`);
    assert.equal(r.error.code, "BAD_REQUEST");
  }
});

test("copyText uses the clipboard when there is one and says false when there is not", async () => {
  let got = null;
  assert.equal(await copyText("abc", { clipboard: { writeText: async (t) => { got = t; } } }), true);
  assert.equal(got, "abc");
  assert.equal(await copyText("abc", {}), false);
  assert.equal(await copyText("abc", { clipboard: { writeText: async () => { throw new Error("denied"); } } }), false);
  assert.equal(await copyText("abc", undefined), false);
});
