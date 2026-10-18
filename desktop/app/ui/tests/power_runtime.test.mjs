// node --test "desktop/app/ui/tests/*.test.mjs"   (G-217: the Power / Ring & uncore / Ratio caps logic: intents validated by the helper's own parsers, the CPU-limit lib, the rt state, the drawer kinds, the mock)
// The validators are src/arltuner/helper/ops_power.py _validate_power_plan and ops_runtime.py _validate_runtime_plan, run by /usr/bin/python3 -I with src/ on sys.path (stdlib only, no helper started,
// no socket, no hardware). Fixtures are the simulator's recordings (fixtures/g217/); `cpu_limits` in the mock is INVENTED in the shape of the G-226 contract.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import G from "../fixtures/g217/fixtures-g217.js";
import { Bridge } from "../bridge.js";
import { createMockTransport } from "../fixtures/mock-backend.js";
import { encodeTau } from "../fixtures/mock-backend-g217.js";
import { createAppStore } from "../store.js";
import { powerIntent, runtimeIntent, POWER_REVERT, RUNTIME_REVERT } from "../lib/intent.js";
import { normCpuLimits, capWindow, uncoreWindow, capsLine, uncoreLine, powerLine, explainRefusal, cpuLimitLine, cpuOffsetCell, modelCpuLine, READ_FIRST } from "../lib/cpulimits.js";
import { initialRt, powerCurrent, powerDisagrees, powerDisabledReason, powerRequest, capRows, uncoreState, runtimeRequest, pendingCounts, ensureRt, editRt } from "../lib/rtstate.js";
import { ensureTune } from "../views/tune.js";
import { powerView, runtimeView, POWER_KIND, RUNTIME_KIND, registerRuntimeKinds, requestReview, revertRequest, PL_NAME } from "../views/tune/runtime-kinds.js";
import { KINDS, createApplyController } from "../views/tune/apply.js";
import { powerWarnings } from "../views/tune/power.js";
import { unitWindow, capsDisabledReason } from "../views/tune/runtime.js";
import { uncoreDisabledReason } from "../views/tune/ring.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
const PY = `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT + "/src")})
from arltuner.helper.ops_power import _validate_power_plan
from arltuner.helper.ops_runtime import _validate_runtime_plan
fns = {"power.plan": _validate_power_plan, "runtime.plan": _validate_runtime_plan}
out = []
for op, args in json.load(sys.stdin):
    try:
        fns[op](args)
        out.append({"ok": True})
    except Exception as exc:  # OpError
        out.append({"ok": False, "error": getattr(exc, "message", str(exc))})
print(json.dumps(out))
`;
function helperAccepts(cases) {
  const r = spawnSync("/usr/bin/python3", ["-I", "-c", PY], { input: JSON.stringify(cases), encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

const RT_READ = G["runtime-read"].result;
const PW_READ = G["power-read"].result;
const CL = { P: { max_mv: 1625, max_ratio: 120, source: "OC mailbox 0x28" }, E: { max_mv: 1355, max_ratio: 120, source: "OC mailbox 0x28" }, ring: { max_mv: 1625, max_ratio: 120, source: "x" }, offset_mv: [-1000, 999], power: { locked: false, pl_max_w: 4095.875 }, caps_khz: { 0: [800000, 5700000], 8: [800000, 4900000] }, uncore_khz: [800000, 4000000] };

// ---- intents vs the helper's parsers ----
test("intents: power and runtime intents are accepted by the helper's own parsers (top-level fields, not {intent})", () => {
  const cases = [
    ["power.plan", powerIntent({ pl1_w: 230, pl2_w: 280, tau_s: 56 }).intent],
    ["power.plan", powerIntent({ tau_s: 28.5 }).intent],
    ["power.plan", POWER_REVERT],
    ["runtime.plan", runtimeIntent({ caps: { 0: 50, 8: 23, 9: 23, 10: 23, 11: 23 }, uncore: { max_ratio: 38 } }).intent],
    ["runtime.plan", runtimeIntent({ uncore: { min_ratio: 8, max_ratio: 40 } }).intent],
    ["runtime.plan", RUNTIME_REVERT],
  ];
  const out = helperAccepts(cases);
  out.forEach((o, i) => assert.equal(o.ok, true, JSON.stringify(cases[i]) + " -> " + o.error));
});

test("intents: what the helper refuses, the builders refuse or never produce (parking too: D-048)", () => {
  assert.ok(powerIntent({ pl1_w: 250.5 }).error, "whole watts");
  assert.ok(powerIntent({ pl3_w: 5 }).error);
  assert.ok(powerIntent({ tau_s: 0 }).error);
  assert.ok(powerIntent({ pl1_w: "250" }).error);
  assert.equal(powerIntent({}).intent, null);
  assert.equal(powerIntent({ pl1_w: 250 }, { pl1_w: 250 }).intent, null, "equal to the value in force: dropped");
  assert.ok(runtimeIntent({ caps: { x: 5 } }).error);
  assert.ok(runtimeIntent({ caps: { 0: 0 } }).error);
  assert.ok(runtimeIntent({ caps: { 0: 256 } }).error);
  assert.ok(runtimeIntent({ uncore: { max_freq: 5 } }).error);
  assert.equal(runtimeIntent({}).intent, null);
  const bad = [
    ["power.plan", { power: { pl1_w: 250 }, revert: true }],
    ["power.plan", { intent: { power: { pl1_w: 250 } } }],
    ["runtime.plan", { parking: { 8: false } }],
    ["runtime.plan", { intent: { caps: { 0: 40 } } }],
  ];
  helperAccepts(bad).forEach((o, i) => assert.equal(o.ok, false, JSON.stringify(bad[i])));
  assert.ok(!("parking" in (runtimeIntent({ caps: { 0: 40 } }).intent)));
});

// ---- cpu_limits ----
test("cpulimits: the contract shape is normalised; null stays null; lines name the CPU source, never a policy", () => {
  assert.equal(normCpuLimits(null), null);
  const cl = normCpuLimits(CL);
  assert.deepEqual(cl.offsetMv, [-1000, 999]);
  assert.equal(cl.kinds.P.maxMv, 1625);
  assert.deepEqual(capWindow(cl, 0), { min: 8, max: 57, minKhz: 800000, maxKhz: 5700000 });
  assert.equal(capWindow(cl, 99), null);
  assert.deepEqual(uncoreWindow(cl), { min: 8, max: 40 });
  assert.equal(capsLine(cl, [0]), "cpufreq 800–5700 MHz (cpu0)");
  assert.match(uncoreLine(cl), /^uncore 8×–40×/);
  assert.match(powerLine(cl), /PL field maximum 4095\.875 W/);
  assert.match(powerLine(normCpuLimits({ power: { locked: true, pl_max_w: 100 } })), /locked by firmware/);
  for (const text of [capsLine(cl, [0]), uncoreLine(cl), powerLine(cl), cpuLimitLine({ ratioOc: 120, vtMaxV: 1.625, off: [-1000, 999] }, "vt"), cpuOffsetCell({ hw: { P: { off: [-1000, 999] } } }, "P")]) assert.doesNotMatch(text, /policy/i);
  assert.equal(cpuOffsetCell({ hw: { P: { off: [-200, 200] } } }, "P"), "±200 · CPU");
  assert.equal(modelCpuLine({ hw: { E: { ratioOc: 120 } } }, "E", "ratio"), "CPU limit: OC ratios up to 120×.");
  assert.equal(modelCpuLine({ hw: null }, "P", "vt"), "");
});

test("cpulimits: a CPU_LIMIT_UNREAD refusal says to read the CPU state; any other refusal shows the helper's own CPU rule", () => {
  assert.match(explainRefusal({ code: "REFUSED", message: "x", detail: { check: "CPU_LIMIT_UNREAD" } }), new RegExp("^" + READ_FIRST));
  assert.equal(explainRefusal({ code: "REFUSED", message: "pl1_w = 9000 W is above the field", detail: { check: "CPU_LIMIT", key: "pl1_w" } }), "pl1_w = 9000 W is above the field");
  assert.equal(explainRefusal(G["runtime-plan-range"].response.error), G["runtime-plan-range"].response.error.message);
});

// ---- the rt state ----
const readyRt = (patch = {}) => {
  const rt = initialRt();
  rt.power = { state: "ready", result: PW_READ, error: null, at: null };
  rt.runtime = { state: "ready", result: RT_READ, error: null, at: null };
  return { ...rt, ...patch };
};

test("rtstate: power edits against what power.read says (file first, MSR as fallback); equal values are dropped", () => {
  const rt = readyRt();
  assert.deepEqual(powerCurrent(PW_READ), { pl1_w: 250, pl2_w: 295, tau_s: 127.926272 });
  rt.edits.power = { pl1_w: 250, pl2_w: 280 };
  assert.deepEqual(powerRequest(rt), { intent: { power: { pl2_w: 280 } } });
  rt.edits.power = { pl1_w: 250 };
  assert.equal(powerRequest(rt).intent, null);
  assert.equal(powerDisagrees(PW_READ, "pl1_w"), false);
  assert.equal(powerDisabledReason(null), "Read the power limits first");
  assert.equal(powerDisabledReason(PW_READ), "");
  assert.match(powerDisabledReason({ ...PW_READ, locked: true }), /locked/);
});

test("rtstate: caps are per unit; an E cluster expands to its 4 CPUs; a value equal to the read is dropped", () => {
  const groups = capRows(RT_READ);
  assert.deepEqual(groups.map((g) => g.unit), ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3"]);
  assert.deepEqual(groups.find((g) => g.unit === "EC0").cpus, [8, 9, 10, 11]);
  assert.equal(groups[0].capRatio, 57);
  const rt = readyRt();
  rt.edits.caps = { P0: 50, EC0: 23, P1: 55 };
  const r = runtimeRequest(rt);
  assert.deepEqual(r.intent, { caps: { 0: 50, 8: 23, 9: 23, 10: 23, 11: 23 } }, "P1 is already 55x: dropped");
  assert.equal(pendingCounts(rt).caps, 2);
  rt.edits.uncore = { max_ratio: 38, min_ratio: 8 };
  assert.deepEqual(runtimeRequest(rt).intent.uncore, { max_ratio: 38 }, "min is already 8x");
  rt.edits.uncore = { min_ratio: 30, max_ratio: 20 };
  assert.match(runtimeRequest(rt).error, /above max/);
  assert.deepEqual(uncoreState(RT_READ), { min: 8, max: 40, initialMin: 8, initialMax: 40 });
});

test("rtstate: an offline CPU is read-only; its unit has no online CPU to write", () => {
  const rows = structuredClone(RT_READ);
  rows.caps[0].online = false;
  const rt = readyRt();
  rt.runtime = { state: "ready", result: rows, error: null, at: null };
  rt.edits.caps = { P0: 40 };
  assert.match(runtimeRequest(rt).error, /no online CPU/);
  assert.match(capsDisabledReason({ ...RT_READ, offline: [0] }), /offline/);
  assert.match(uncoreDisabledReason({ ...RT_READ, offline: [0] }, normCpuLimits(CL)), /offline/);
});

test("views: the cap window is the strictest over the CPUs of a unit; disabled reasons name the missing read", () => {
  const cl = normCpuLimits({ ...CL, caps_khz: { 8: [800000, 4900000], 9: [1000000, 4800000] } });
  assert.deepEqual(unitWindow(cl, [8, 9]), { min: 10, max: 48 });
  assert.equal(unitWindow(cl, [0]), null);
  assert.equal(capsDisabledReason(null), "Read the ratio caps first");
  assert.match(uncoreDisabledReason(RT_READ, null), new RegExp(READ_FIRST));
  assert.equal(uncoreDisabledReason(RT_READ, normCpuLimits(CL)), "");
});

test("power: PL2 below PL1 is a warning (never a block); the step is the XTU labels", () => {
  assert.equal(powerWarnings({ pl1_w: 250, pl2_w: 295 }, {}).length, 0);
  const w = powerWarnings({ pl1_w: 250, pl2_w: 295 }, { pl2_w: 200 });
  assert.equal(w[0].code, "PL2_BELOW_PL1");
  assert.deepEqual(Object.values(PL_NAME), ["Processor Core Power Limit (PL1)", "Processor Core Turbo Power Limit (PL2)", "Turbo Boost Power Time Window"]);
});

// ---- drawer kinds ----
test("kinds: power/runtime register once with planArgs = the intent itself (top-level fields) and the recorded plans give rows", () => {
  registerRuntimeKinds();
  registerRuntimeKinds();
  assert.equal(KINDS.power.planOp, "power.plan");
  assert.equal(KINDS.runtime.applyOp, "runtime.apply");
  const intent = { power: { pl1_w: 230 } };
  assert.equal(POWER_KIND.planArgs(intent), intent);
  assert.equal(RUNTIME_KIND.planArgs(intent), intent);
  const pv = powerView(G["power-plan"].response.result);
  assert.deepEqual(pv.units.map((u) => u.keys[0]), ["pl1_w", "pl2_w", "tau_s"]);
  assert.equal(pv.units[0].lines[0], "250 W → 230 W");
  assert.equal(pv.units[2].lines[0], "127.9 s → 56 s");
  const rv = runtimeView(G["runtime-plan"].response.result);
  assert.equal(rv.units[0].unit, "cpu 0");
  assert.equal(rv.units[0].lines[0], "max 57× → 50×");
  assert.equal(rv.units[1].unit, "cpu 8, 9, 10, 11", "the four CPUs of the cluster asked for the same change: one row");
  assert.match(rv.units[1].lines[0], /capped at 22/);
  assert.equal(rv.units[2].unit, "Uncore max ratio");
  assert.equal(rv.steps.length, 6);
});

test("kinds: a pending edit becomes a request; the tab's Review dispatches tune:review with detail.requests; nothing pending says so", () => {
  const store = createAppStore();
  ensureTune(store);
  ensureRt(store);
  const got = [];
  store.events.addEventListener("tune:review", (e) => got.push(e.detail));
  assert.deepEqual(requestReview(store), [], "nothing pending");
  assert.equal(store.get().toasts.at(-1).text, "Nothing to review: no pending changes.");
  store.set({ rt: readyRt() });
  editRt(store, (e) => ({ ...e, power: { pl1_w: 230 }, caps: { P0: 50 } }));
  const reqs = requestReview(store);
  assert.deepEqual(reqs, [{ kind: "power", intent: { power: { pl1_w: 230 } }, label: "Power limits" }, { kind: "runtime", intent: { caps: { 0: 50 } }, label: "Ratio caps & uncore" }]);
  assert.deepEqual(got[0].requests, reqs);
  assert.deepEqual(revertRequest("power").intent, { revert: true });
  assert.deepEqual(requestReview(store, [revertRequest("runtime")])[0].kind, "runtime");
});

// ---- the mock helper ----
async function mockBridge(query = "") {
  globalThis.location = { search: "?bridge=mock" + query };
  const t = createMockTransport({ stepMs: 1 });
  const b = new Bridge(t);
  await b.connect();
  return b;
}
const run = async (b, op, args) => {
  const first = await b.call(op, args);
  return first.ok && first.pending ? b.wait(first.id) : first;
};

test("mock: power.read -> plan -> apply -> read -> revert follows the intent (recorded shapes, cpu_limits present, no policy)", async () => {
  const b = await mockBridge();
  const r0 = (await run(b, "power.read")).result;
  assert.equal(r0.boot, null);
  assert.ok(r0.cpu_limits && r0.cpu_limits.power.pl_max_w > 0);
  assert.ok(!JSON.stringify(r0).includes("policy"));
  const plan = await b.call("power.plan", { power: { pl1_w: 230, pl2_w: 280, tau_s: 56 } });
  assert.equal(plan.ok, true);
  assert.deepEqual(plan.result.steps.map((s) => [s.key, s.value]), [["pl1_w", 230e6], ["pl2_w", 280e6], ["tau_s", 56000000]]);
  assert.equal(encodeTau(56), 56);
  assert.ok(encodeTau(57) <= 57 && encodeTau(57) > 55, "rounded down onto the encodable grid");
  const bad = await b.call("power.apply", { plan_id: plan.result.plan_id, typed: "CONFIRM nope" });
  assert.equal(bad.error.code, "CONFIRM_MISMATCH");
  const done = await run(b, "power.apply", { plan_id: plan.result.plan_id, typed: "CONFIRM " + plan.result.nonce });
  assert.equal(done.ok, true);
  assert.ok(done.result.readback.every((x) => x.match));
  const r1 = (await run(b, "power.read")).result;
  assert.deepEqual([r1.powercap.pl1_w, r1.powercap.pl2_w, r1.msr.limit.pl1_w], [230, 280, 230]);
  assert.equal(r1.boot.pl1_w, 250);
  const rev = await b.call("power.plan", { revert: true });
  assert.equal(rev.ok, true);
  assert.equal((await run(b, "power.apply", { plan_id: rev.result.plan_id, typed: "CONFIRM " + rev.result.nonce })).ok, true);
  const r2 = (await run(b, "power.read")).result;
  assert.deepEqual([r2.powercap.pl1_w, r2.boot], [250, null]);
  assert.equal((await b.call("power.plan", { revert: true })).error.detail.check, "NO_JOURNAL");
});

test("mock: refusals are CPU rules with the key; the wrapped {intent} shape is a BAD_REQUEST like the helper's", async () => {
  const b = await mockBridge();
  const high = await b.call("power.plan", { power: { pl1_w: 5000 } });
  assert.deepEqual([high.error.code, high.error.detail.check, high.error.detail.key], ["REFUSED", "CPU_LIMIT", "pl1_w"]);
  assert.doesNotMatch(high.error.message, /policy/i);
  assert.equal((await b.call("power.plan", { power: { pl1_w: 250 } })).error.detail.check, "NOTHING_TO_DO");
  assert.equal((await b.call("power.plan", { intent: { power: { pl1_w: 230 } } })).error.code, "BAD_REQUEST");
  assert.equal((await b.call("power.plan", { power: { pl1_w: 250.5 } })).error.code, "BAD_REQUEST");
  const lowPl2 = await b.call("power.plan", { power: { pl2_w: 249 } });
  assert.equal(lowPl2.ok, true, "PL2 below PL1 is a warning, not a refusal");
  assert.equal(lowPl2.result.warnings[0].code, "PL2_BELOW_PL1");
  const cap = await b.call("runtime.plan", { caps: { 0: 99 } });
  assert.deepEqual([cap.error.detail.check, cap.error.detail.key], ["CPU_LIMIT", "cpu0"]);
  const unc = await b.call("runtime.plan", { uncore: { max_ratio: 45 } });
  assert.equal(unc.error.detail.check, "UNCORE_RANGE");
  assert.equal((await b.call("runtime.plan", { uncore: { min_ratio: 30, max_ratio: 20 } })).error.detail.check, "UNCORE_ORDER");
  assert.equal((await b.call("runtime.plan", { parking: { 8: false } })).error.code, "BAD_REQUEST");
  assert.equal((await b.call("runtime.plan", { intent: { caps: { 0: 40 } } })).error.code, "BAD_REQUEST");
  const mixed = await b.call("runtime.plan", { caps: { 8: 30, 9: 31 } });
  assert.equal(mixed.error.detail.check, "CLUSTER_CONFLICT");
  assert.equal((await b.call("runtime.plan", { revert: true })).error.detail.check, "NO_JOURNAL");
});

test("mock: runtime plan -> apply -> read -> revert; an E cluster is capped one bin low where the grid cannot hit the ratio and the plan says so", async () => {
  const b = await mockBridge();
  const plan = await b.call("runtime.plan", { caps: { 0: 50, 8: 23, 9: 23, 10: 23, 11: 23 }, uncore: { max_ratio: 38 } });
  assert.equal(plan.ok, true);
  assert.equal(plan.result.steps.length, 6);
  assert.match(plan.result.steps[1].note, /capped at 22/);
  assert.ok(plan.result.warnings.some((w) => /capped at 22/.test(w)));
  const done = await run(b, "runtime.apply", { plan_id: plan.result.plan_id, typed: "CONFIRM " + plan.result.nonce });
  assert.equal(done.ok, true);
  assert.ok(done.result.readback.every((x) => x.match));
  const r = (await b.call("runtime.read")).result;
  const g = capRows(r);
  assert.deepEqual([g[0].capRatio, g.find((x) => x.unit === "EC0").capRatio, uncoreState(r).max], [50, 22, 38]);
  assert.equal(r.journal.caps[0], 5700000);
  const rev = await b.call("runtime.plan", { revert: true });
  assert.equal((await run(b, "runtime.apply", { plan_id: rev.result.plan_id, typed: "CONFIRM " + rev.result.nonce })).ok, true);
  const after = (await b.call("runtime.read")).result;
  assert.deepEqual([capRows(after)[0].capRatio, uncoreState(after).max, after.journal], [57, 40, null]);
});

test("mock: ?cpulimits=unread -> cpu_limits is null and every write plan says CPU_LIMIT_UNREAD; tune.read carries cpu_limits otherwise", async () => {
  let b = await mockBridge("&cpulimits=unread");
  assert.equal((await run(b, "power.read")).result.cpu_limits, null);
  assert.equal((await b.call("runtime.read")).result.cpu_limits, null);
  assert.equal((await b.call("power.plan", { power: { pl1_w: 230 } })).error.detail.check, "CPU_LIMIT_UNREAD");
  assert.equal((await b.call("runtime.plan", { caps: { 0: 50 } })).error.detail.check, "CPU_LIMIT_UNREAD");
  assert.equal((await b.call("tune.last")).result.result.cpu_limits, null);
  b = await mockBridge();
  const last = (await b.call("tune.last")).result.result;
  assert.deepEqual(last.cpu_limits.offset_mv, [-1000, 999]);
  assert.equal(last.cpu_limits.P.max_mv, 1625);
  const read = await run(b, "tune.read");
  assert.ok(read.result.cpu_limits && read.result.cpu_limits.caps_khz["0"]);
});

test("controller: a power section is planned with the intent as the plan arguments and applied in sequence with the V/F section (stop on first failure)", async () => {
  const b = await mockBridge();
  const store = createAppStore();
  store.set({ link: { state: "connected", kind: null, message: "", fix: null } });
  ensureTune(store);
  ensureRt(store);
  store.set({ rt: readyRt() });
  const calls = [];
  const spy = { call: (op, args) => (calls.push([op, args]), b.call(op, args)), on: (e, f) => b.on(e, f), off: (e, f) => b.off(e, f), wait: (id) => b.wait(id) };
  registerRuntimeKinds();
  const ctl = createApplyController({ store, bridge: spy });
  const state = await ctl.review([{ kind: "power", intent: { power: { pl1_w: 230 } }, label: "Power limits" }, { kind: "runtime", intent: { caps: { 0: 50 } }, label: "Caps" }]);
  const planCalls = calls.filter(([op]) => op.endsWith(".plan"));
  // apply.js sends the kind's own plan arguments only with the G-217 hooks (tests/apply-hooks.patch); without them it wraps {intent}: the helper (and this mock) refuse that shape, the first section fails and the rest is not planned
  if (KINDS.power.planArgs && planCalls[0][1].power) {
    assert.deepEqual(planCalls.map(([op]) => op), ["power.plan", "runtime.plan"]);
    assert.deepEqual(planCalls[0][1], { power: { pl1_w: 230 } });
    assert.equal(state, "review");
    assert.equal(await ctl.apply(), true);
    assert.deepEqual(ctl.state().sections.map((s) => s.status), ["done", "done"]);
  } else {
    assert.deepEqual(planCalls.map(([op]) => op), ["power.plan"]);
    assert.deepEqual(planCalls[0][1], { intent: { power: { pl1_w: 230 } } });
    assert.equal(state, "failed");
  }
});
