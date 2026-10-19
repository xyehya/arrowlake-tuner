// node --test "desktop/app/ui/tests/*.test.mjs"   (G-210: the apply drawer's pure helpers and its controller against the mock bridge; recorded simulator fixtures, no DOM, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { Bridge } from "../bridge.js";
import { createMockTransport } from "../fixtures/mock-backend.js";
import { createAppStore } from "../store.js";
import { modelFromRead } from "../lib/units.js";
import { emptyDraft, setPoint } from "../lib/draft.js";
import * as draftLib from "../lib/draft.js";
import { tileFace } from "../views/tune/grid.js";
import { buildIntent } from "../lib/intent.js";
import { ensureTune, patchTune } from "../views/tune.js";
import {
  planRows, describeRow, stepRows, unitOrder, unitStatuses, STATUS_TEXT, eta, progressText, summarizeResult, resultHeadline, planWarnings, planWrites, locateError, confirmText,
  createApplyController, KINDS, registerKind,
} from "../views/tune/apply.js";

const PLAN = FIX["tune-plan"].response.result;
const APPLY = FIX["tune-apply"].response.result;
const READ = FIX["tune-read"].result;

// ---- plan -> rows ----
test("planRows/describeRow: the recorded plan is one changed P0 point, before 0 mV -> after -19.5 mV", () => {
  const rows = planRows(PLAN);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].unit, "P0");
  assert.equal(rows[0].changes, 1);
  assert.deepEqual([rows[0].rows[0].point, rows[0].rows[0].changed], [5, true]);
  const text = describeRow(rows[0].rows[0]);
  assert.match(text, /^pt 5 · 53×/);
  assert.match(text, /0 mV → −19\.5 mV/);
});

test("planRows: unchanged points are marked unchanged; arrays, unread (null) and OC entries are accepted", () => {
  const plan = {
    units: ["EC0"],
    before: { EC0: [{ oc: true, ratio: 48, v_target_v: 0, offset_mv: 0 }, { ratio: 20, offset_mv: 0 }, null] },
    after: { EC0: [{ oc: true, ratio: 50, v_target_v: 1.1, offset_mv: 0 }, { ratio: 20, offset_mv: 0 }, { ratio: 30, offset_mv: -10 }] },
  };
  const [u] = planRows(plan);
  assert.deepEqual(u.rows.map((r) => [r.point, r.changed]), [[0, true], [1, false], [2, true]]);
  assert.match(describeRow(u.rows[0]), /^OC point · 50×/);
  assert.match(describeRow(u.rows[0]), /ratio 48× → 50×/);
  assert.match(describeRow(u.rows[2]), /offset unread → −10 mV/);
  assert.deepEqual(planRows({ before: {}, after: {} }), []);
});

test("stepRows keeps the helper's order and labels each kind; unitOrder follows the first write", () => {
  const rows = stepRows(PLAN);
  assert.deepEqual(rows.map((s) => [s.n, s.kind, s.conditional]), [[1, "write_percore_enable", true], [2, "write_vf_point", false]]);
  assert.match(rows[0].label, /only if not already on/);
  assert.match(rows[1].label, /^P0 point 5 offset −20 mV \(cpu 0\)$/);
  const plan = { steps: [{ kind: "write_vf_point", unit: "EC1", point: 2, cpu: 9 }, { kind: "write_vf_point", unit: "P3", point: 1, cpu: 3 }, { kind: "write_msr", table: "P", n: 3 }, { kind: "write_vf_point", unit: "EC1", point: 3 }], units: ["P3", "EC1", "P9"] };
  assert.deepEqual(unitOrder(plan), ["EC1", "P3", "turbo P", "P9"]);
  assert.equal(planWrites(PLAN), 2);
  assert.equal(planWrites({ steps: [{}, {}, {}] }), 3);
});

test("planWarnings: D-050 TURBO_BELOW_OC is an amber warning with its own label (not a NOTE)", () => {
  const [w] = planWarnings({ warnings: [{ code: "TURBO_BELOW_OC", text: "P0 OC ratio 58x is above the P turbo table" }] });
  assert.deepEqual([w.code, w.cls, w.label], ["TURBO_BELOW_OC", "warn", "OC above turbo table"]);
});

test("planWarnings: codes decide the class, bare strings are NOTEs, empty ones are dropped", () => {
  const w = planWarnings({ warnings: [{ code: "E40_OFFSET", text: "a" }, { code: "NO_BASELINE", text: "b" }, "plain", { code: "ZZZ", text: "c" }, { code: "NOTE", text: "" }, 5] });
  assert.deepEqual(w.map((x) => [x.code, x.cls]), [["E40_OFFSET", "bad"], ["NO_BASELINE", "warn"], ["NOTE", ""], ["ZZZ", ""]]);
  assert.equal(planWarnings(PLAN).length, PLAN.warnings.length);
});

// ---- progress -> unit status ----
const plan3 = { steps: [{ kind: "write_percore_enable", conditional: true }, { kind: "write_vf_point", unit: "P0", point: 1 }, { kind: "write_vf_point", unit: "P0", point: 2 }, { kind: "write_vf_point", unit: "EC0", point: 1 }, { kind: "write_vf_point", unit: "EC1", point: 1 }] };

test("unitStatuses while running is positional over the ordered writes", () => {
  const at = (step, of = 8) => unitStatuses(plan3, { step, of }, null);
  assert.deepEqual(at(0), { P0: "queued", EC0: "queued", EC1: "queued" });
  assert.deepEqual(at(1), { P0: "writing", EC0: "queued", EC1: "queued" });
  assert.deepEqual(at(5), { P0: "written", EC0: "writing", EC1: "queued" });
  assert.deepEqual(at(8), { P0: "written", EC0: "written", EC1: "writing" });
  assert.deepEqual(unitStatuses(plan3, null, null), { P0: "queued", EC0: "queued", EC1: "queued" });
});

test("unitStatuses at the end comes from the read-back only; after a stop the current unit is interrupted", () => {
  const rb = [{ unit: "P0", point: 1, match: true }, { unit: "P0", point: 2, match: true }, { unit: "EC0", point: 1, match: false }];
  assert.deepEqual(unitStatuses(plan3, { step: 8, of: 8 }, { ok: true, readback: rb }), { P0: "ok", EC0: "mismatch", EC1: "unverified" });
  assert.deepEqual(unitStatuses(plan3, { step: 5, of: 8 }, { ok: false }), { P0: "unverified", EC0: "interrupted", EC1: "not_started" });
  const turbo = { steps: [{ kind: "write_msr", table: "E" }] };
  assert.deepEqual(unitStatuses(turbo, null, { ok: true, readback: [{ unit: "E", point: "turbo", match: true }] }), { "turbo E": "ok" });
});

test("G-230: units with nothing written show no change, not 'not read back'", () => {
  // P1 / P2 are in plan.units but have no write step; a turbo table equal to the CPU's is listed by the helper (table "e") but never written, so it has no read-back row
  const plan = { units: ["P0", "P1", "P2"], steps: [{ kind: "write_vf_point", unit: "P0", point: 5 }, { kind: "write_msr", table: "e" }, { kind: "write_msr", table: "p" }] };
  const rb = [{ unit: "P0", point: 5, match: true }, { unit: "P", point: "turbo", match: true }];
  assert.deepEqual(unitStatuses(plan, { step: 3, of: 3 }, { ok: true, readback: rb, complete: true }), { P0: "ok", P1: "no_change", P2: "no_change", "turbo E": "no_change", "turbo P": "ok" });
  assert.equal(STATUS_TEXT.no_change, "no change");
  // a written V/F word with no read-back is still amber
  assert.equal(unitStatuses(plan, null, { ok: true, readback: [], complete: true }).P0, "unverified");
  // a partial result does not claim "no change" for a listed turbo table
  assert.equal(unitStatuses(plan, null, { ok: true, readback: rb, complete: false })["turbo E"], "unverified");
  // after a stop the unwritten units are no change too
  assert.equal(unitStatuses(plan, { step: 1, of: 3 }, { ok: false }).P1, "no_change");
});

test("eta uses the measured per-op time; nothing before the first step", () => {
  assert.deepEqual(eta(1000, 0, 10), { remainingMs: null, text: "" });
  assert.equal(eta(2000, 4, 10).remainingMs, 3000);
  assert.equal(eta(2000, 4, 10).text, "~3.0 s left");
  assert.equal(eta(100, 10, 10).remainingMs, 0);
  assert.match(progressText({ step: 3, of: 6 }, 1500), /^step 3 of 6 · 1\.5 s elapsed · ~1\.5 s left$/);
  assert.equal(progressText(null, 0), "waiting for the first step");
});

// ---- result ----
test("summarizeResult/resultHeadline: all matched, mismatch, partial", () => {
  const ok = summarizeResult(APPLY);
  assert.deepEqual([ok.total, ok.matched, ok.allMatch, ok.complete, ok.opsUsed], [1, 1, true, true, 4]);
  assert.equal(resultHeadline(ok), "1/1 words read back as written");
  const bad = summarizeResult({ ...APPLY, readback: [...APPLY.readback, { ...APPLY.readback[0], match: false, actual: "0x1" }] });
  assert.equal(bad.mismatches.length, 1);
  assert.equal(bad.allMatch, false);
  assert.equal(resultHeadline(bad), "1 of 2 words differ on read-back");
  const part = summarizeResult({ ...APPLY, complete: false, stop: { reason: "guard tripped", tripped: true }, writes_sent: 3 });
  assert.equal(part.allMatch, false);
  assert.match(resultHeadline(part), /^Partial apply: it stopped after 3 writes\. guard tripped/);
  const extras = summarizeResult({ ...APPLY, cpufreq: [{ cpu: 0, before_khz: 5, after_khz: 6 }, { cpu: "x" }], residual: ["P0"], ring_refused: ["why"] });
  assert.deepEqual([extras.cpufreq.length, extras.residual, extras.ringRefused], [1, ["P0"], ["why"]]);
  assert.equal(summarizeResult(null).total, 0);
});

// ---- plan errors -> field ----
test("locateError: the message prefix names the unit, the intent maps the value back to the ratio", () => {
  const intent = { rows: [{ target: "P0", offset: { kind: "points", mv: { 48: -250, 53: -20 } } }, { target: "EC1", offset: { kind: "curve", mv: -300 } }] };
  const msg = "P0: offset -250 mV (raw -256) is outside the policy window -150.0..+150.0 mV (policy x). Nothing was sent.";
  assert.deepEqual(locateError({ code: "REFUSED", message: msg, detail: { check: "POLICY_RANGE" } }, intent), { unit: "P0", field: "point", ratio: 48, point: null, message: msg, check: "POLICY_RANGE" });
  assert.equal(locateError({ message: "EC1: offset -300 mV (raw x) is outside" }, intent).field, "curve");
  assert.equal(locateError({ message: "P0: OC-point voltage 1300 mV is above the CPU limit" }, intent).field, "vt");
  assert.equal(locateError({ message: "P0: ratio 60 is above the policy ratio ceiling" }, intent).field, "ratio");
  assert.deepEqual(locateError({ message: "ring point 3: offset +200 mV (raw 1) is outside" }, intent).point, 3);
  assert.equal(locateError({ message: "ring point 3: offset +200 mV" }, intent).unit, "ring");
  assert.deepEqual(locateError({ message: "turbo.P: ratio 60 is above x" }, intent).unit, "turbo P");
  const order = locateError({ code: "REFUSED", message: "TURBO bucket", detail: { check: "TURBO_ORDER", table: "p", bucket: 3 } }, intent);
  assert.deepEqual([order.unit, order.field, order.point], ["turbo P", "turbo", 3]);
  assert.equal(locateError({ message: "something else", detail: { check: "NO_READ" } }, intent).unit, null);
  assert.equal(locateError({ message: "P0: weird" }, intent).field, null);
  assert.equal(confirmText("ABC123"), "CONFIRM ABC123");
});

// ---- the controller, against the mock bridge ----
const FAST = { intervalMs: 5, stepMs: 1, applyMode: null };
async function rig(opts = {}, mutate = (m, d) => setPoint(m, d, ["P0"], 53, -20)) {
  const bridge = new Bridge(createMockTransport({ ...FAST, ...opts }), "mock");
  await bridge.connect();
  const store = createAppStore();
  store.set({ link: { state: "connected", kind: null, message: "", fix: null } });
  ensureTune(store);
  const model = modelFromRead(structuredClone(READ));
  patchTune(store, { model, draft: mutate(model, emptyDraft()) });
  const toasts = [];
  const ctl = createApplyController({ store, bridge, toast: (k, t) => toasts.push([k, t]) });
  return { bridge, store, ctl, toasts, model };
}

test("controller: review plans the draft, Apply sends CONFIRM <nonce>, progress fills the section, the draft clears and tiles re-read", async () => {
  const { bridge, store, ctl } = await rig();
  const sent = [];
  const call = bridge.call.bind(bridge);
  bridge.call = (op, args) => (sent.push([op, args]), call(op, args));
  assert.equal(await ctl.review(["tune"]), "review");
  const a = store.get().apply;
  assert.equal(a.state, "review");
  assert.equal(a.sections.length, 1);
  assert.deepEqual(sent[0], ["tune.plan", { intent: buildIntent(store.get().tune.model, store.get().tune.draft).intent }]);
  const plan = a.sections[0].plan;
  assert.equal(ctl.gate(), "");
  const steps = [];
  store.subscribe((s) => s.apply.sections[0] && s.apply.sections[0].progress && steps.push(s.apply.sections[0].progress.step));
  assert.equal(await ctl.apply(), true);
  assert.deepEqual(sent.find((x) => x[0] === "tune.apply")[1], { plan_id: plan.plan_id, typed: "CONFIRM " + plan.nonce });
  assert.ok(steps.length >= 3 && steps.includes(plan.ops_needed), "progress events reached the section: " + steps);
  const done = store.get().apply;
  assert.equal(done.state, "done");
  assert.equal(done.sections[0].status, "done");
  assert.equal(done.sections[0].summary.allMatch, true);
  assert.deepEqual(store.get().tune.draft, emptyDraft(), "a clean apply clears the draft");
  assert.equal(store.get().pending.changes, 0);
  assert.ok(sent.some((x) => x[0] === "tune.last"), "tune.last is re-read after the apply");
  assert.equal(store.get().tune.read.fromLast, true);
});

// G-227: the operator's report: after Apply the tiles/inspector kept the OLD offsets until "Read CPU state". The mock's tune.last now follows the applied words (as the helper's does).
const offAt = (model, id, ratio) => model.units[id].points.find((p) => p.ratio === ratio).offset_mv;
test("controller: after Apply the model, tile faces and pending count hold the applied offsets (x57 on P0, x55 on P1), no re-read", async () => {
  const { store, ctl, model } = await rig({}, (m, d) => setPoint(m, setPoint(m, d, ["P0"], 57, -20), ["P1"], 55, -15));
  assert.equal(offAt(model, "P0", 57), 0);
  assert.equal(await ctl.review(["tune"]), "review");
  assert.equal(await ctl.apply(), true);
  const t = store.get().tune;
  assert.ok(Math.abs(offAt(t.model, "P0", 57) + 20) < 0.5, "P0 x57 reads back -20 mV: " + offAt(t.model, "P0", 57));
  assert.ok(Math.abs(offAt(t.model, "P1", 55) + 15) < 0.5, "P1 x55 reads back -15 mV: " + offAt(t.model, "P1", 55));
  assert.equal(offAt(t.model, "P2", 55), 0, "an untouched unit is unchanged");
  assert.equal(store.get().pending.changes, 0);
  assert.ok(Math.abs(tileFace(t.model, t.draft, "P0", []).deepest + 20) < 0.5, "the tile's deepest offset follows the model");
});

test("controller: a CPU read-back that differs from the plan (clamped) shows the CPU's value and drops the applied draft entries", async () => {
  const { store, ctl } = await rig({ applyMode: "mismatch" }, (m, d) => setPoint(m, setPoint(m, d, ["P0"], 57, -20), ["P1"], 55, -15));
  assert.equal(await ctl.review(["tune"]), "review");
  await ctl.apply();
  const a = store.get().apply;
  assert.equal(a.sections[0].summary.allMatch, false);
  const t = store.get().tune;
  assert.ok(Math.abs(offAt(t.model, "P0", 57) + 20) < 0.5 && Math.abs(offAt(t.model, "P1", 55) + 15) < 0.5, "the model holds what the helper read back");
  assert.equal(draftLib.isEmpty(t.draft), true, "applied entries are not kept as pending edits over the CPU's value");
});

// the live bug: the helper had no usable tune.last (nothing stored for this boot, an error, or a record that predates the apply): the draft was cleared, the old model stayed
const NO_LAST = {
  unavailable: () => ({ id: "x", ok: true, result: { available: false } }),
  error: () => ({ id: "x", ok: false, error: { code: "BUSY", message: "the helper is busy" } }),
  "predates the apply": (r) => ({ ...r, result: { ...r.result, applied_at: null } }),
};
for (const [name, answer] of Object.entries(NO_LAST)) {
  test(`controller: tune.last ${name} -> the model follows the plan and the matched read-back, the draft clears, the user is told`, async () => {
    const { store, ctl, bridge, toasts } = await rig({}, (m, d) => setPoint(m, setPoint(m, d, ["P0"], 57, -20), ["P1"], 55, -15));
    const call = bridge.call.bind(bridge);
    bridge.call = async (op, args) => (op === "tune.last" ? answer(await call(op, args)) : call(op, args));
    assert.equal(await ctl.review(["tune"]), "review");
    assert.equal(await ctl.apply(), true);
    const t = store.get().tune;
    assert.ok(Math.abs(offAt(t.model, "P0", 57) + 20) < 0.5, "P0 x57: " + offAt(t.model, "P0", 57));
    assert.ok(Math.abs(offAt(t.model, "P1", 55) + 15) < 0.5, "P1 x55: " + offAt(t.model, "P1", 55));
    assert.equal(offAt(t.model, "P2", 55), 0);
    assert.equal(draftLib.isEmpty(t.draft), true);
    assert.equal(store.get().pending.changes, 0);
    assert.ok(toasts.some(([k, text]) => k === "warn" && /no stored read/.test(text)), "the user is told the tiles follow the plan: " + JSON.stringify(toasts));
  });
}

test("controller: tune.last unavailable + a turbo-table apply -> the model's turbo ratios follow the intent", async () => {
  const { store, ctl, bridge, model } = await rig({}, (m, d) => d);
  const bucket = (model.turbo.P.ratios.length - 1);
  const want = model.turbo.P.ratios.slice();
  want[bucket] -= 1;
  const call = bridge.call.bind(bridge);
  bridge.call = async (op, args) => (op === "tune.last" ? NO_LAST.unavailable() : call(op, args));
  const { setBucket } = draftLib;
  patchTune(store, { draft: setBucket(model, emptyDraft(), "P", bucket, want[bucket]) });
  assert.equal(await ctl.review(["tune"]), "review");
  assert.equal(await ctl.apply(), true);
  assert.deepEqual(store.get().tune.model.turbo.P.ratios, want);
  assert.equal(store.get().pending.changes, 0);
});

test("controller: a plan refusal fails the review, names the unit/ratio, and nothing is applied", async () => {
  const { store, ctl, bridge } = await rig({}, (m, d) => setPoint(m, d, ["P0"], 48, -180));
  assert.equal(await ctl.review(["tune"]), "failed");
  const a = store.get().apply;
  assert.equal(a.state, "failed");
  assert.match(a.error.message, /^P0: offset -180 mV/);
  assert.deepEqual([a.error.located.unit, a.error.located.field, a.error.located.ratio], ["P0", "point", 48]);
  assert.equal(await ctl.apply(), false);
  assert.equal((await bridge.call("tune.apply", { plan_id: "x", typed: "CONFIRM X" })).error.code, "PLAN_STALE");
});

test("controller: nothing pending and no read are request-level errors", async () => {
  const { store, ctl } = await rig({}, (m, d) => d);
  assert.equal(await ctl.review(["tune"]), "failed");
  assert.match(store.get().apply.error.message, /Nothing to apply/);
  const s2 = createAppStore();
  const ctl2 = createApplyController({ store: s2, bridge: new Bridge(createMockTransport(FAST), "mock") });
  assert.equal(await ctl2.review(["tune"]), "failed");
  assert.match(s2.get().apply.error.message, /Read the CPU state first/);
});

test("controller: mismatch result is shown, the written unit's draft entries give way to the CPU's words (G-227) and the sequence stops", async () => {
  const { store, ctl } = await rig({ applyMode: "mismatch" });
  await ctl.review(["tune"]);
  assert.equal(await ctl.apply(), false);
  const s = store.get().apply.sections[0];
  assert.equal(s.status, "done");
  assert.equal(s.summary.mismatches.length, 1);
  assert.equal(store.get().apply.state, "failed");
  assert.equal(store.get().tune.draft.units.P0, undefined, "the draft would otherwise show the wanted value over the CPU's read-back");
});

test("controller: PLAN_STALE marks the section stale; replan issues a fresh plan that applies", async () => {
  const { store, ctl } = await rig({ applyMode: "stale" });
  await ctl.review(["tune"]);
  const first = store.get().apply.sections[0].plan.plan_id;
  assert.equal(await ctl.apply(), false);
  assert.equal(store.get().apply.sections[0].status, "stale");
  assert.equal(store.get().apply.sections[0].error.code, "PLAN_STALE");
  assert.equal(await ctl.replan(), "review");
  const second = store.get().apply.sections[0].plan.plan_id;
  assert.notEqual(second, first);
  assert.equal(await ctl.apply(), true);
  assert.equal(store.get().apply.state, "done");
});

test("controller: an edit after the plan makes the plan outdated (Apply gated until Re-plan)", async () => {
  const { store, ctl, model } = await rig();
  await ctl.review(["tune"]);
  assert.equal(ctl.gate(), "");
  patchTune(store, { draft: setPoint(model, store.get().tune.draft, ["P0"], 53, -25) });
  assert.match(ctl.gate(), /Re-plan/);
  assert.equal(await ctl.apply(), false);
  store.set({ link: { state: "busy", kind: null, message: "", fix: null } });
  assert.match(ctl.gate(), /Re-plan|not connected/);
});

test("controller: Cancel sends {op_id} to the helper and the section ends cancelled", async () => {
  const { store, ctl, bridge } = await rig({ stepMs: 15 });
  const sent = [];
  const call = bridge.call.bind(bridge);
  bridge.call = (op, args) => (sent.push([op, args]), call(op, args));
  await ctl.review(["tune"]);
  const run = ctl.apply();
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(store.get().apply.state, "applying");
  assert.equal(ctl.isBusy(), true);
  assert.equal(await ctl.cancel(), true);
  assert.equal(await run, false);
  const cancel = sent.find((x) => x[0] === "cancel");
  assert.equal(cancel[1].op_id, store.get().apply.sections[0].opId);
  assert.equal(store.get().apply.sections[0].status, "cancelled");
  assert.equal(store.get().apply.sections[0].error.code, "CANCELLED");
  assert.equal(ctl.isBusy(), false);
});

test("controller: sections apply in order, each with its own nonce; a failure skips the rest", async () => {
  const log = [];
  const fakeKind = (name, failApply) => ({
    label: name, planOp: name + ".plan", applyOp: name + ".apply",
    request: () => ({ intent: { from: name } }),
  });
  const kinds = { a: fakeKind("a"), b: fakeKind("b"), c: fakeKind("c") };
  const bridge = {
    on() {}, off() {},
    async call(op, args) {
      log.push([op, args]);
      const [name, what] = op.split(".");
      if (what === "plan") return { ok: true, result: { plan_id: "id-" + name, nonce: "N" + name.toUpperCase(), writes: 1, steps: [] } };
      if (name === "b") return { ok: false, error: { code: "REFUSED", message: "b refused" } };
      return { ok: true, result: { complete: true, readback: [{ unit: "U", point: 1, match: true }] } };
    },
    wait: async () => ({ ok: true, result: {} }),
  };
  const store = createAppStore();
  store.set({ link: { state: "connected", kind: null, message: "", fix: null } });
  const ctl = createApplyController({ store, bridge, kinds });
  assert.equal(await ctl.review(["a", "b", "c"]), "review");
  assert.deepEqual(store.get().apply.sections.map((s) => s.kind), ["a", "b", "c"]);
  assert.equal(await ctl.apply(), false);
  assert.deepEqual(log.filter((x) => x[0].endsWith(".apply")).map((x) => [x[0], x[1].typed]), [["a.apply", "CONFIRM NA"], ["b.apply", "CONFIRM NB"]]);
  assert.deepEqual(store.get().apply.sections.map((s) => s.status), ["done", "failed", "skipped"]);
  assert.equal(store.get().apply.state, "failed");
});

test("registerKind validates and a registered kind can be reviewed", async () => {
  assert.throws(() => registerKind("x", {}), TypeError);
  registerKind("zz", { planOp: "zz.plan", applyOp: "zz.apply", request: () => ({ intent: { a: 1 } }) });
  assert.equal(KINDS.zz.label, "zz");
  delete KINDS.zz;
});
