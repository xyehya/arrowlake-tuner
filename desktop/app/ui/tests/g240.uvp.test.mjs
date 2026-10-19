// node --test "desktop/app/ui/tests/*.test.mjs"   (G-240: Undervolt Protection, rc 0x13: the refusal in words, the store flag, the status chip, the plan refusal located; mock bridge, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { Bridge } from "../bridge.js";
import { createMockTransport } from "../fixtures/mock-backend.js";
import { createAppStore, setLastRead, setUvp } from "../store.js";
import { modelFromRead } from "../lib/units.js";
import { emptyDraft, setPoint } from "../lib/draft.js";
import { ensureTune, patchTune } from "../views/tune.js";
import { summarizeResult, resultHeadline, locateError, createApplyController, UVP_CHECK } from "../views/tune/apply.js";
import { uvpChip } from "../components/status-bar.js";

const READ = FIX["tune-read"].result;
const FAST = { intervalMs: 5, stepMs: 1, applyMode: null };

async function rig(opts = {}, mutate = (m, d) => setPoint(m, d, ["P0"], 53, -20)) {
  const bridge = new Bridge(createMockTransport({ ...FAST, ...opts }), "mock");
  await bridge.connect();
  const store = createAppStore();
  store.set({ link: { state: "connected", kind: null, message: "", fix: null } });
  ensureTune(store);
  const model = modelFromRead(structuredClone(READ));
  patchTune(store, { model, draft: mutate(model, emptyDraft()) });
  const ctl = createApplyController({ store, bridge, toast: () => {} });
  return { bridge, store, ctl, model };
}

test("G-240 controller: an apply refused by Undervolt Protection says so in words and the store learns uvp 'on'", async () => {
  const { store, ctl } = await rig({ uvp: "on" });
  assert.equal(store.get().uvp, "unknown");
  assert.equal(await ctl.review(["tune"]), "review");
  assert.equal(await ctl.apply(), false);
  const sec = store.get().apply.sections[0];
  assert.equal(sec.status, "failed");
  assert.equal(sec.error.detail.check, UVP_CHECK);
  assert.match(sec.error.message, /^The CPU refused a negative voltage offset: Undervolt Protection is enabled in the BIOS/);
  assert.equal(store.get().uvp, "on");
});

test("G-240 controller: once on, a negative-offset plan is refused at the plan and located on the offending point", async () => {
  const { store, ctl } = await rig({ uvp: "on" });
  await ctl.review(["tune"]);
  await ctl.apply();
  assert.equal(await ctl.review(["tune"]), "failed");
  const a = store.get().apply;
  assert.match(a.error.message, /^P0: offset -20 mV: The CPU refused a negative voltage offset/);
  assert.deepEqual([a.error.located.unit, a.error.located.field, a.error.located.ratio, a.error.located.check], ["P0", "point", 53, UVP_CHECK]);
  assert.equal(store.get().uvp, "on");
});

test("G-240 controller: a positive offset still plans and applies after the refusal", async () => {
  const { store, ctl, model } = await rig({ uvp: "on" });
  await ctl.review(["tune"]);
  await ctl.apply();
  patchTune(store, { draft: setPoint(model, emptyDraft(), ["P0"], 53, 10) });
  assert.equal(await ctl.review(["tune"]), "review");
  assert.equal(await ctl.apply(), true);
  assert.equal(store.get().uvp, "on");
});

test("G-240 controller: a negative offset that is accepted moves uvp to 'off'", async () => {
  const { store, ctl } = await rig({});
  await ctl.review(["tune"]);
  assert.equal(await ctl.apply(), true);
  assert.equal(store.get().uvp, "off");
});

test("G-240 summarizeResult / resultHeadline: a partial apply stopped by Undervolt Protection leaves the message to its own box", () => {
  const s = summarizeResult({ complete: false, writes_sent: 3, uvp: "on", stop: { kind: "rc", reason: "The CPU refused a negative voltage offset: x", check: UVP_CHECK, tripped: false }, readback: [] });
  assert.equal(s.uvp, "on");
  assert.equal(resultHeadline(s), "Partial apply: it stopped after 3 writes");
  assert.equal(summarizeResult({ complete: true }).uvp, null);
});

test("G-240 locateError: the plan refusal lands on the unit / ring point like a range refusal", () => {
  const err = (message) => ({ code: "REFUSED", message, detail: { check: UVP_CHECK } });
  const intent = { scope: "unit", rows: [{ target: "P1", offset: { kind: "curve", mv: -5 } }] };
  const a = locateError(err("P1: offset -5 mV: The CPU refused a negative voltage offset"), intent);
  assert.deepEqual([a.unit, a.field, a.check], ["P1", "curve", UVP_CHECK]);
  const b = locateError(err("ring point 3: offset -5 mV: The CPU refused"), intent);
  assert.deepEqual([b.unit, b.field, b.point], ["ring", "ring", 3]);
});

test("G-240 store: setUvp / setLastRead take on | off | unknown and ignore anything else", () => {
  const s = createAppStore();
  assert.equal(s.get().uvp, "unknown");
  setUvp(s, "on");
  assert.equal(s.get().uvp, "on");
  setUvp(s, "bogus");
  setUvp(s, undefined);
  assert.equal(s.get().uvp, "on");
  setLastRead(s, "2026-10-19T10:00:00Z", undefined, undefined, "off");
  assert.equal(s.get().uvp, "off");
  setLastRead(s, "2026-10-19T10:05:00Z"); // not given: untouched
  assert.equal(s.get().uvp, "off");
});

test("G-240 mock: tune.read / tune.last / tune.plan carry uvp", async () => {
  const bridge = new Bridge(createMockTransport({ ...FAST, uvp: "off" }), "mock");
  await bridge.connect();
  assert.equal((await bridge.call("tune.last")).result.result.uvp, "off");
  const plan = await bridge.call("tune.plan", { intent: { scope: "unit", rows: [{ target: "P0", offset: { kind: "points", mv: { 53: -10 } }, v_target_v: null, ratio: null }] } });
  assert.equal(plan.result.uvp, "off");
});

test("G-240 status bar: the chip is amber 'Undervolt protection: on' and hidden for off / unknown", () => {
  assert.deepEqual([uvpChip("on").hidden, uvpChip("on").text, uvpChip("on").state], [false, "Undervolt protection: on", "warn"]);
  assert.match(uvpChip("on").title, /disable 'Undervolt Protection' in the BIOS/);
  assert.equal(uvpChip("off").hidden, true);
  assert.equal(uvpChip("unknown").hidden, true);
  assert.equal(uvpChip(undefined).hidden, true);
});
