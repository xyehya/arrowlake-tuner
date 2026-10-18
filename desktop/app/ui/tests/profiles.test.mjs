// node --test "desktop/app/ui/tests/*.test.mjs"   (G-213: the pure parts of the Profiles screen and the mock's profiles.* ops; recorded simulator fixtures, no DOM, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { Bridge } from "../bridge.js";
import { createMockTransport } from "../fixtures/mock-backend.js";
import { createAppStore } from "../store.js";
import { modelFromRead } from "../lib/units.js";
import { emptyDraft, setPoint, setRingPoint, setBucket, isEmpty, diff } from "../lib/draft.js";
import { MINUS, TIMES, DASH } from "../lib/format.js";
import { ensureTune } from "../views/tune.js";
import {
  summaryText, offsetText, offsetShort, kindOf, compareRows, draftFromContents, saveGate, acceptConfirmText, fmtCreated, reviewIntent, rowsOf, profilesView, NO_ACCEPTED_TEXT,
  isPreviousAccepted, previousStamp, savedOrder, stateNameGate, readRefusal, helperDiffRows, unreadableRefusal, diffByUnit, diffSummary, previousKeptText,
} from "../views/profiles.js";

const READ = FIX["tune-read"].result;
const GET_P0 = FIX["profiles-get-sim-p0-undervolt"].result;
const GET_E = FIX["profiles-get-sim-e-light"].result;
const model = () => modelFromRead(structuredClone(READ));
const FAST = { intervalMs: 5, stepMs: 1, applyMode: null, profiles: null, accept: null, fingerprint: null };
async function connected(opts = {}) {
  const bridge = new Bridge(createMockTransport({ ...FAST, ...opts }), "mock");
  await bridge.connect();
  return bridge;
}

// ---- text ----
test("summaryText: units, deepest offset, OC ratios, target voltages, turbo; a missing summary is empty", () => {
  assert.equal(summaryText(GET_P0.summary), `12 units · deepest ${MINUS}25 mV · turbo table`);
  assert.equal(summaryText({ units: 1, deepest_offset_mv: null, oc_ratios: 2, v_targets: 1, turbo: false }), "1 unit · 2 OC ratios · 1 target voltage");
  assert.equal(summaryText(null), "");
});

test("offsetText / offsetShort: a curve, few points in full, many points as a count and range", () => {
  assert.equal(offsetText({ kind: "curve", mv: -15 }), `${MINUS}15 mV (whole curve)`);
  assert.equal(offsetShort({ kind: "curve", mv: -15 }), `${MINUS}15 mV all`);
  assert.equal(offsetShort({ kind: "points", mv: { 53: -25, 48: -20 } }), `48× ${MINUS}20 mV, 53× ${MINUS}25 mV`);
  assert.equal(offsetShort(GET_P0.contents.rows[0].offset), `8 points, ${MINUS}25 mV to 0 mV`);
  assert.equal(offsetText(null), "unchanged");
  assert.equal(offsetShort(null), "–");
});

test("kindOf: the list row wins, else profiles.get, else unknown", () => {
  assert.equal(kindOf({ complete: true }, null), "complete");
  assert.equal(kindOf({ complete: false }, { complete: true }), "partial");
  assert.equal(kindOf({}, { complete: false }), "partial");
  assert.equal(kindOf({}, null), null);
  assert.equal(kindOf(null, null), null);
});

test("fmtCreated / acceptConfirmText", () => {
  assert.equal(fmtCreated("2026-10-18T11:10:55Z"), "2026-10-18 11:10 UTC");
  assert.equal(fmtCreated(null), "");
  assert.match(acceptConfirmText('"a"', []), /There is no accepted state yet, so nothing is replaced\./);
  assert.match(acceptConfirmText('"a"', ["old"]), /replaces the current accepted state \(old\)\./);
  assert.match(acceptConfirmText("The state of the last read", []), /Nothing is written to the CPU\.$/);
});

// ---- compare ----
test("compareRows: P0 points vs a whole curve, then EC0, in unit order; second minus first", () => {
  const rows = compareRows(GET_P0.contents, GET_E.contents);
  assert.deepEqual(rows.map((r) => [r.unit, r.label, r.a, r.b]), [
    ["P0", "Offset at 48×", -20, 0], ["P0", "Offset at 53×", -25, 0], ["EC0", "Offset, whole curve", 0, -15],
  ]);
  assert.equal(rows[0].delta(rows[0].a, rows[0].b), "+20 mV");
  assert.equal(rows[2].delta(rows[2].a, rows[2].b), `${MINUS}15 mV`);
  assert.deepEqual(compareRows(GET_P0.contents, GET_P0.contents), []);
});

test("compareRows: OC ratio, target voltage, turbo buckets; a value one side does not set is null", () => {
  const a = { rows: [{ target: "P1", offset: null, ratio: 58, v_target_v: 1.3 }], turbo: { P: [57, 57, 55, 55, 55, 55, 55, 55] } };
  const b = { rows: [{ target: "P1", offset: null, ratio: 60, v_target_v: null }], turbo: { P: [57, 56, 55, 55, 55, 55, 55, 54] } };
  const rows = compareRows(a, b);
  assert.deepEqual(rows.map((r) => [r.unit, r.label, r.a, r.b]), [
    ["P1", "OC target voltage", 1.3, null], ["P1", "OC ratio", 58, 60], ["P turbo", "Bucket 1", 57, 56], ["P turbo", "Bucket 7", 55, 54],
  ]);
  assert.equal(rows[1].delta(58, 60), "+2×");
  assert.equal(rows[2].delta(57, 56), `${MINUS}1×`);
  assert.equal(rows[0].delta(1.3, null), "–");
});

test("compareRows ignores units outside P0..P7 / EC0..EC3 / ring and tolerates empty contents", () => {
  assert.deepEqual(compareRows({ rows: [{ target: "XX", offset: { kind: "curve", mv: -5 } }] }, null), []);
  assert.deepEqual(compareRows(null, undefined), []);
  assert.deepEqual(rowsOf({ rows: [1, null, { target: "P0" }] }), [{ target: "P0" }]);
});

// ---- load into Tune ----
test("draftFromContents: a complete profile only leaves what differs from the read", () => {
  const m = model();
  const { draft, skipped, clamped } = draftFromContents(m, GET_P0.contents);
  assert.deepEqual(skipped, []);
  assert.deepEqual(clamped, []);
  assert.deepEqual(Object.keys(draft.units), ["P0"]);
  assert.deepEqual(draft.units.P0.pts, { 48: -20, 53: -25 });
  assert.deepEqual(draft.turbo, {});
  assert.deepEqual([diff(m, draft).total, diff(m, draft).unitCount], [2, 1]);
});

test("draftFromContents: a whole-curve row becomes a curve edit; a profile equal to the read gives an empty draft", () => {
  const m = model();
  const { draft } = draftFromContents(m, GET_E.contents);
  assert.equal(draft.units.EC0.curveMv, -15);
  assert.deepEqual(Object.keys(draft.units), ["EC0"]);
  const same = draftFromContents(m, { rows: GET_E.contents.rows.map((r) => ({ ...r, offset: { kind: "curve", mv: 0 } })), turbo: GET_E.contents.turbo });
  assert.equal(isEmpty(same.draft), true);
});

test("draftFromContents: units and points the read does not have are skipped and named; values outside the editor's limits are named as moved", () => {
  const m = model();
  const contents = { rows: [
    { target: "P0", offset: { kind: "points", mv: { 47: -10, 48: -20 } }, ratio: null, v_target_v: null },
    { target: "P9", offset: { kind: "curve", mv: -5 }, ratio: null, v_target_v: null },
    { target: "EC1", offset: { kind: "curve", mv: -9999 }, ratio: null, v_target_v: null },
  ] };
  const r = draftFromContents(m, contents);
  assert.deepEqual(r.skipped, [`P0 47×`, "P9"]);
  assert.equal(r.draft.units.P0.pts[48], -20);
  assert.equal(r.clamped.length, 1);
  assert.match(r.clamped[0], /^EC1 /);
});

test("draftFromContents: turbo tables load bucket by bucket, non-increasing; a table the read lacks is skipped", () => {
  const m = model();
  const cur = m.turbo.P.ratios;
  const want = cur.slice();
  want[5] = cur[5] - 2;
  const r = draftFromContents(m, { rows: [], turbo: { P: want, E: m.turbo.E.ratios } });
  assert.deepEqual(r.draft.turbo.P.slice(0, 5), cur.slice(0, 5));
  assert.equal(r.draft.turbo.P[5], want[5]);
  assert.ok(r.draft.turbo.P[6] <= want[5], "buckets to the right come down with it");
  assert.equal(r.draft.turbo.E, undefined);
  const noTurbo = { ...m, turbo: null };
  assert.deepEqual(draftFromContents(noTurbo, { rows: [], turbo: { P: want } }).skipped, ["P turbo table (not read)"]);
});

// ---- save ----
test("saveGate: name rules, taken names, no read yet", () => {
  const m = model();
  const ok = { taken: ["old"], model: m, draft: emptyDraft() };
  assert.match(saveGate({ ...ok, name: "" }).note, /^Lowercase letters/);
  assert.equal(saveGate({ ...ok, name: "" }).ok, false);
  assert.match(saveGate({ ...ok, name: "Bad Name" }).note, /^Not allowed/);
  assert.match(saveGate({ ...ok, name: "x".repeat(65) }).note, /^Not allowed/);
  assert.match(saveGate({ ...ok, name: "old" }).note, /Names are never reused/);
  assert.match(saveGate({ ...ok, name: "fresh", model: null }).note, /Read the CPU state first/);
  const good = saveGate({ ...ok, name: "fresh.v-1_a" });
  assert.equal(good.ok, true);
  assert.deepEqual(good.intent, { scope: "unit", rows: [] }, "no edits = the read as it is");
  assert.match(good.note, /no changes from Tune/);
});

test("saveGate: row edits are sent as {scope, rows}; turbo and ring edits are named and refused, never sent", () => {
  const m = model();
  const rows = saveGate({ name: "p", taken: [], model: m, draft: setPoint(m, emptyDraft(), ["P0"], 53, -20) });
  assert.equal(rows.ok, true);
  assert.deepEqual(Object.keys(rows.intent), ["scope", "rows"]);
  assert.equal(rows.intent.rows[0].target, "P0");
  assert.match(rows.note, /with 1 changed unit from Tune over it/);
  const turbo = saveGate({ name: "p", taken: [], model: m, draft: setBucket(m, emptyDraft(), "P", 5, m.turbo.P.ratios[5] - 1) });
  assert.equal(turbo.ok, false);
  assert.match(turbo.note, /apply the turbo edit first, then save/);
  const m2 = modelFromRead({ ...structuredClone(READ), intent_features: ["ring"] });
  const ring = saveGate({ name: "p", taken: [], model: m2, draft: setRingPoint(m2, emptyDraft(), 2, -10) });
  assert.equal(ring.ok, false);
  assert.match(ring.note, /apply the ring edit first, then save/);
  assert.equal(ring.intent, null);
});

// ---- Review and apply opens the drawer through the shared event ----
test("reviewIntent: a whole-state intent travels on store.events as tune:review detail.requests", () => {
  const store = createAppStore();
  ensureTune(store);
  const seen = [];
  store.events.addEventListener("tune:review", (e) => seen.push(e.detail));
  reviewIntent(store, { profile: "sim-e-light" }, "Apply profile sim-e-light");
  reviewIntent(store, { restore: "accepted" }, "Restore accepted: x");
  assert.deepEqual(seen, [
    { requests: [{ kind: "tune", intent: { profile: "sim-e-light" }, label: "Apply profile sim-e-light" }] },
    { requests: [{ kind: "tune", intent: { restore: "accepted" }, label: "Restore accepted: x" }] },
  ]);
});

test("profilesView: the contract the app wires (id, mount, unmount)", () => {
  const v = profilesView();
  assert.equal(v.id, "profiles");
  assert.equal(typeof v.mount, "function");
  assert.equal(typeof v.unmount, "function");
  v.unmount();
  assert.match(NO_ACCEPTED_TEXT, /^No accepted state yet/);
});

// ---- the mock's profiles.* ops (recorded saves + invented accepted state) ----
test("mock: profiles.list / get answer the recorded saves; no accepted state until one is made; the library can start empty", async () => {
  const b = await connected();
  const list = await b.call("profiles.list");
  assert.deepEqual(list.result.profiles.map((p) => [p.name, p.source, p.complete]), [["sim-p0-undervolt", "saved", true], ["sim-e-light", "saved", true]]);
  assert.equal(list.result.accepted, false);
  assert.match(list.result.note, /no accepted state directory/);
  const got = await b.call("profiles.get", { name: "sim-p0-undervolt" });
  assert.deepEqual(got.result.contents, GET_P0.contents);
  assert.deepEqual(got.result.summary.units, 12);
  assert.equal((await b.call("profiles.get", { name: "nope" })).error.detail.check, "PROFILE_NOT_FOUND");
  assert.equal((await b.call("profiles.get", { name: "Bad Name" })).error.code, "BAD_REQUEST");
  const none = await connected({ profiles: "none" });
  assert.deepEqual((await none.call("profiles.list")).result.profiles, []);
});

test("mock: profiles.save never overwrites, takes rows only, lays the rows over the held words", async () => {
  const b = await connected();
  const rows = [{ target: "P1", offset: { kind: "points", mv: { 48: -10 } }, v_target_v: null, ratio: null }];
  const exists = await b.call("profiles.save", { name: "sim-e-light", intent: { scope: "unit", rows } });
  assert.deepEqual([exists.ok, exists.error.detail.check], [false, "EXISTS"]);
  assert.equal((await b.call("profiles.save", { name: "t", intent: { scope: "unit", rows, turbo: { P: [57, 57, 55, 55, 55, 55, 55, 55] } } })).error.code, "BAD_REQUEST");
  const saved = await b.call("profiles.save", { name: "mine", intent: { scope: "unit", rows } });
  assert.equal(saved.ok, true);
  assert.equal(saved.result.complete, true);
  const got = await b.call("profiles.get", { name: "mine" });
  const p1 = got.result.contents.rows.find((r) => r.target === "P1");
  assert.equal(p1.offset.mv["48"], -10);
  assert.equal(p1.offset.mv["53"], 0);
  assert.equal(got.result.contents.rows.length, 12);
  assert.equal((await b.call("profiles.list")).result.profiles.length, 3);
});

test("mock: accept {name} refuses a profile the CPU does not hold (NOT_APPLIED); applying its plan makes it acceptable; from read always works", async () => {
  const b = await connected();
  const refused = await b.call("profiles.accept", { name: "sim-p0-undervolt" });
  assert.deepEqual([refused.ok, refused.error.detail.check], [false, "NOT_APPLIED"]);
  assert.equal((await b.call("tune.plan", { intent: { restore: "accepted" } })).error.detail.check, "NO_ACCEPTED_STATE");
  const plan = await b.call("tune.plan", { intent: { profile: "sim-p0-undervolt" } });
  assert.equal(plan.ok, true);
  assert.equal(plan.result.writes, 3, "the conditional enable + the two points that differ: only what differs is written");
  assert.deepEqual(plan.result.units, ["P0"]);
  const started = await b.call("tune.apply", { plan_id: plan.result.plan_id, typed: `CONFIRM ${plan.result.nonce}` });
  const done = await b.wait(started.id);
  assert.equal(done.ok, true);
  const accepted = await b.call("profiles.accept", { name: "sim-p0-undervolt" });
  assert.equal(accepted.ok, true);
  assert.deepEqual(accepted.result.replaced, []);
  const list = await b.call("profiles.list");
  assert.deepEqual(list.result.profiles[0], { name: "sim-p0-undervolt", source: "accepted", created: null, complete: true });
  assert.equal(list.result.accepted, true);
  assert.equal((await b.call("tune.last")).result.result.accepted_match[0].match, true);
  const again = await b.call("profiles.accept", { from: "read" });
  assert.deepEqual(again.result.replaced, ["sim-p0-undervolt"]);
  assert.match(again.result.name, /^read-\d{8}-\d{6}$/);
  const restore = await b.call("tune.plan", { intent: { restore: "accepted" } });
  assert.equal(restore.ok, true);
  assert.equal(restore.result.writes, 0, "the CPU already holds the accepted state: nothing to write");
});

test("mock: a profile that equals what the CPU holds plans no writes; an unknown profile is PROFILE_NOT_FOUND", async () => {
  const b = await connected({ profiles: "accepted" });
  const plan = await b.call("tune.plan", { intent: { profile: "read-20261018-120000" } });
  assert.equal(plan.result.writes, 0);
  assert.deepEqual(plan.result.steps, []);
  assert.equal((await b.call("tune.plan", { intent: { profile: "ghost" } })).error.detail.check, "PROFILE_NOT_FOUND");
});

test("mock: fingerprint needs an accepted state; knobs make it incomplete or unavailable", async () => {
  const b = await connected();
  assert.equal((await b.call("profiles.fingerprint")).error.detail.check, "NO_ACCEPTED_STATE");
  const acc = await connected({ profiles: "accepted" });
  const fp = await acc.call("profiles.fingerprint");
  assert.deepEqual([fp.ok, fp.result.fingerprint, fp.result.accepted], [true, "complete", ["read-20261018-120000"]]);
  const inc = await connected({ profiles: "accepted", fingerprint: "incomplete" });
  assert.match((await inc.call("profiles.fingerprint")).result.fingerprint, /^incomplete: /);
  const off = await connected({ accept: "unavailable" });
  assert.equal((await off.call("profiles.accept", { from: "read" })).error.detail.check, "NO_ACCEPTED_DIR");
  assert.equal((await off.call("profiles.fingerprint")).error.detail.check, "NO_ACCEPTED_DIR");
});

// ---- G-228: save the full state; an accept keeps the state it replaces ----
test("previous accepted states: recognised, stamped, listed after the saved profiles newest first (same second: -3 before -2)", () => {
  assert.equal(isPreviousAccepted("accepted-before-20261018-120000"), true);
  assert.equal(isPreviousAccepted("mine"), false);
  assert.equal(previousStamp("accepted-before-20261018-120000-2"), "2026-10-18 12:00:00 UTC");
  assert.equal(previousStamp("mine"), "");
  const list = [
    { name: "accepted-before-20261015-090000", source: "saved" },
    { name: "mine", source: "saved" },
    { name: "accepted-before-20261018-120000-2", source: "saved" },
    { name: "accepted-before-20261018-120000", source: "saved" },
    { name: "now", source: "accepted" },
    { name: "zeta", source: "saved" },
  ];
  assert.deepEqual(savedOrder(list).map((p) => p.name), ["now", "mine", "zeta", "accepted-before-20261018-120000-2", "accepted-before-20261018-120000", "accepted-before-20261015-090000"]);
});

test("stateNameGate / readRefusal / previousKeptText", () => {
  assert.equal(stateNameGate({ name: "", taken: [] }).ok, false);
  assert.equal(stateNameGate({ name: "Bad Name", taken: [] }).ok, false);
  assert.equal(stateNameGate({ name: "a".repeat(65), taken: [] }).ok, false);
  assert.match(stateNameGate({ name: "mine", taken: ["mine"] }).note, /exists/);
  assert.equal(stateNameGate({ name: "known-good.1_x", taken: ["mine"] }).ok, true);
  assert.equal(readRefusal({ detail: { check: "STALE_READ" } }), "STALE_READ");
  assert.equal(readRefusal({ detail: { check: "NO_READ" } }), "NO_READ");
  assert.equal(readRefusal({ detail: { check: "EXISTS" } }), null);
  assert.equal(readRefusal(null), null);
  assert.match(previousKeptText(["old"]), /saved first as a profile/);
  assert.match(previousKeptText([]), /no accepted state/);
});

test("profiles.diff (mock): the accepted state equals the read it was made from, so the confirm diff has no false differences and names what it compared", async () => {
  const b = await connected({ profiles: "accepted" });
  const r = await b.call("profiles.diff", { a: "accepted", b: "read" });
  assert.equal(r.ok, true);
  assert.deepEqual(r.result.rows, []);
  assert.deepEqual([r.result.a.source, r.result.b.source], ["accepted", "read"]);
  assert.match(diffSummary(helperDiffRows(r.result), r.result), /^No differences in what was compared: each unit's OC word .*every fused point's offset.*the turbo tables\. Not compared: the ring \(neither state records it\)\./);
});

test("diffByUnit / diffSummary: the per-unit list of the confirm step", () => {
  const a = { rows: [{ target: "P0", offset: { kind: "points", mv: { 48: 0, 53: 0 } }, ratio: null, v_target_v: null }, { target: "EC0", offset: { kind: "curve", mv: -10 }, ratio: null, v_target_v: null }], turbo: null };
  const bb = { rows: [{ target: "P0", offset: { kind: "points", mv: { 48: -20, 53: -25 } }, ratio: null, v_target_v: null }, { target: "EC0", offset: { kind: "curve", mv: -10 }, ratio: null, v_target_v: null }], turbo: null };
  const rows = compareRows(a, bb);
  assert.deepEqual(diffByUnit(rows), [{ unit: "P0", n: 2 }]);
  assert.match(diffSummary(rows), /2 differences in 1 unit from the current accepted state: P0 \(2\)/);
  assert.match(diffSummary([]), /^No differences/);
});

test("mock: profiles.save {name, from: 'read'} saves the complete held state without accepting; never overwrites; refuses a stale read until a read", async () => {
  const b = await connected();
  const r = await b.call("profiles.save", { name: "known-good", from: "read" });
  assert.equal(r.ok, true);
  assert.equal(r.result.from, "read");
  assert.equal(r.result.complete, true);
  const list = await b.call("profiles.list");
  assert.equal(list.result.accepted, false);
  assert.ok(list.result.profiles.some((p) => p.name === "known-good" && p.source === "saved"));
  assert.equal((await b.call("profiles.get", { name: "known-good" })).result.contents.rows.length, 12);
  assert.equal((await b.call("profiles.save", { name: "known-good", from: "read" })).error.detail.check, "EXISTS");
  assert.equal((await b.call("profiles.save", { name: "x", from: "read", intent: { scope: "unit", rows: [] } })).error.code, "BAD_REQUEST");
  assert.equal((await b.call("profiles.save", { name: "x", from: "elsewhere" })).error.code, "BAD_REQUEST");
  const stale = await connected({ read: "stale" });
  assert.equal((await stale.call("profiles.save", { name: "x", from: "read" })).error.detail.check, "STALE_READ");
  assert.equal((await stale.call("profiles.accept", { from: "read" })).error.detail.check, "STALE_READ");
  const started = await stale.call("tune.read", {});
  await stale.wait(started.id);
  assert.equal((await stale.call("profiles.save", { name: "x", from: "read" })).ok, true);
});

test("mock: accept keeps the replaced state as accepted-before-<stamp> (a saved profile with the same contents); the first accept keeps nothing", async () => {
  const b = await connected();
  const first = await b.call("profiles.accept", { from: "read" });
  assert.equal(first.result.previous_saved_as, null);
  const before = (await b.call("profiles.get", { name: first.result.name })).result.contents;
  const second = await b.call("profiles.accept", { from: "read" });
  assert.match(second.result.previous_saved_as, /^accepted-before-\d{8}-\d{6}$/);
  assert.deepEqual(second.result.replaced, [first.result.name]);
  const kept = await b.call("profiles.get", { name: second.result.previous_saved_as });
  assert.equal(kept.result.source, "saved");
  assert.deepEqual(kept.result.contents, before);
  const third = await b.call("profiles.accept", { from: "read" });
  assert.match(third.result.previous_saved_as, /^accepted-before-\d{8}-\d{6}(-\d+)?$/);
  assert.notEqual(third.result.previous_saved_as, second.result.previous_saved_as);
  // undo = accepting the kept profile: the CPU holds that state here, so it works (the real helper refuses NOT_APPLIED when it does not: covered in tests/helper/test_g228.py)
  assert.equal((await b.call("profiles.accept", { name: second.result.previous_saved_as })).ok, true);
});

test("profiles.diff rows: a ring-only and an OC-word-offset-only change are listed, with the unit counts and the readable cells", async () => {
  const b = await connected({ profiles: "accepted", diff: "extras" });
  const r = await b.call("profiles.diff", { a: "accepted", b: "read" });
  const rows = helperDiffRows(r.result);
  assert.deepEqual(rows.map((x) => [x.unit, x.label]), [["P1", "OC word: whole-curve offset"], ["ring", "Offset at 14" + TIMES]]);
  assert.deepEqual(rows.map((x) => [x.a, x.b]), [[0, -5], [null, -25]]);
  assert.deepEqual(rows.map((x) => x.fmt(x.b)), [MINUS + "5 mV", MINUS + "25 mV"]);
  assert.equal(rows[0].delta(rows[0].a, rows[0].b), MINUS + "5 mV");
  assert.equal(rows[1].delta(rows[1].a, rows[1].b), DASH);
  assert.deepEqual(diffByUnit(rows), [{ unit: "P1", n: 1 }, { unit: "ring", n: 1 }]);
  assert.match(diffSummary(rows, r.result), /^2 differences in 2 units from the current accepted state: P1 \(1\), ring \(1\)\./);
});

test("helperDiffRows: every kind has a formatter (mv, v, x, hex); unknown shapes give no rows", () => {
  const rows = helperDiffRows({ rows: [
    { unit: "EC0", label: "OC target voltage", kind: "v", a: null, b: 1.1 },
    { unit: "EC0", label: "OC ratio", kind: "x", a: 48, b: 49 },
    { unit: "ring", label: "OC word (BIOS-owned)", kind: "hex", a: "0x00000028", b: "0x00000027" },
    { unit: "P3", label: "Offset at 53x", kind: "mv", a: -60, b: -50 },
  ] });
  assert.equal(rows[0].fmt(1.1), "1.100 V");
  assert.equal(rows[1].fmt(49), "49" + TIMES);
  assert.equal(rows[1].delta(48, 49), "+1" + TIMES);
  assert.equal(rows[2].fmt("0x00000027"), "0x00000027");
  assert.equal(rows[2].delta("0x00000028", "0x00000027"), DASH);
  assert.equal(rows[3].label, "Offset at 53" + TIMES);
  assert.equal(rows[3].delta(-60, -50), "+10 mV");
  assert.deepEqual(helperDiffRows(null), []);
  assert.deepEqual(helperDiffRows({ rows: "x" }), []);
});

test("diffSummary: an empty diff without an answer from the helper does not claim anything was compared", () => {
  assert.equal(diffSummary([]), "No differences were found.");
  assert.match(diffSummary([{ unit: "P0", label: "x", a: 1, b: 2 }], { truncated: 3 }), /\(3 more are not listed\)/);
});

test("unreadableRefusal keys on the check code, never on the message", () => {
  assert.equal(unreadableRefusal({ detail: { check: "ACCEPTED_UNREADABLE" }, message: "x" }), true);
  assert.equal(unreadableRefusal({ detail: { check: "ACCEPTED_CHAIN" }, message: "cannot be read" }), false);
  assert.equal(unreadableRefusal(null), false);
});

test("mock: profiles.diff refuses a stale read, an unknown profile and bad shapes; profiles.accept offers set_aside_unreadable only for an unreadable accepted state", async () => {
  const stale = await connected({ profiles: "accepted", read: "stale" });
  assert.equal((await stale.call("profiles.diff", { a: "accepted", b: "read" })).error.detail.check, "STALE_READ");
  const b = await connected({ profiles: "accepted" });
  assert.equal((await b.call("profiles.diff", { a: "accepted", b: "nope" })).error.code, "BAD_REQUEST");
  assert.equal((await b.call("profiles.diff", { a: "accepted" })).error.code, "BAD_REQUEST");
  assert.equal((await b.call("profiles.diff", { a: "Bad", b: "read" })).error.code, "BAD_REQUEST");
  const u = await connected({ accept: "unreadable" });
  const refused = await u.call("profiles.accept", { from: "read" });
  assert.equal(refused.error.detail.check, "ACCEPTED_UNREADABLE");
  assert.doesNotMatch(refused.error.message, /\.\)/);
  assert.equal((await u.call("profiles.accept", { from: "read", set_aside_unreadable: false })).error.code, "BAD_REQUEST");
  const ok = await u.call("profiles.accept", { from: "read", set_aside_unreadable: true });
  assert.equal(ok.ok, true);
  assert.match(ok.result.set_aside_as, /accepted\.unreadable-/);
  assert.equal(ok.result.previous_saved_as, null);
});
