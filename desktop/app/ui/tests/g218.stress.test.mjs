// node --test "desktop/app/ui/tests/*.test.mjs"   (G-218 Stress: args, gate, wording, the DOM-free controller against the recorded simulator answers; no DOM, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { Bridge } from "../bridge.js";
import { createAppStore, setLink, setPreflight } from "../store.js";
import { createMockTransport, FIXTURES } from "../fixtures/mock-backend.js";
import { wrapMockTransport, checkArgs, FIXTURES_G218 as G } from "../fixtures/mock-backend-g218.js";
import {
  BANNER_HEAD, BANNER_TEXT, stressArgs, initialStress, stressDuration, selectedUnits, opGate, estimateText, stSummary, resultFlags, achievedText, screenText, unitsText,
  historyRow, unitModel, quickSets, favMark, cpuSub, fmtLen, mmss, ringFill, unitMhz, errText, createStressController, DUR_MIN, DUR_MAX, CAP_MIN, CAP_MAX, MEM_MB,
} from "../views/stress.js";

const UI = fileURLToPath(new URL("..", import.meta.url));
const pre = FIXTURES.preflight.result;
const model = unitModel(pre);
const IDS = model.ids;

async function rig({ seed = "none", stepMs = 1, connected = true } = {}) {
  const bridge = new Bridge(wrapMockTransport(createMockTransport({ intervalMs: 10_000 }), { stepMs, seed }), "mock");
  const store = createAppStore();
  if (connected) {
    setLink(store, await bridge.connect());
    setPreflight(store, pre);
  }
  return { bridge, store };
}
const until = async (fn, ms = 4000) => {
  const end = Date.now() + ms;
  while (!fn()) {
    if (Date.now() > end) throw new Error("timed out: " + fn);
    await new Promise((r) => setTimeout(r, 5));
  }
};

// ---------------------------------------------------------------- units

test("unitModel reads preflight.topology: 8 P + 4 E clusters, favored from the helper, nothing invented", () => {
  assert.equal(IDS.length, 12);
  assert.deepEqual(IDS.slice(0, 2), ["P0", "P1"]);
  assert.deepEqual(unitModel(null).ids, []);
  assert.deepEqual(unitModel({ topology: {} }).ids, []);
  assert.equal(favMark(model, "P0"), " ★");
  assert.equal(favMark(model, "P1"), "");
  assert.equal(cpuSub(model, "P0"), "CPU 0");
  assert.equal(cpuSub(model, "EC0"), "CPUs 8–11");
  const sets = Object.fromEntries(quickSets(model));
  assert.equal(sets["All"].length, 12);
  assert.equal(sets["P-cores"].length, 8);
  assert.equal(sets["E-clusters"].length, 4);
  assert.deepEqual(sets["None"], []);
});

// ---------------------------------------------------------------- args (ops.rs stress_start)

/** The argument rules of src-tauri/src/helper/ops.rs `stress_start`, restated; the mock wrapper's checkArgs is the same thing. */
function opsRsStressStart(a) {
  const keys = ["kind", "method", "units", "ratio_cap", "duration_s", "memory_mb"];
  if (!Object.keys(a).every((k) => keys.includes(k)) || !["cpu", "memory"].includes(a.kind)) return false;
  const int = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
  return int(a.duration_s, 30, 3600) && (a.method === undefined || ["integer", "vector-math", "cache"].includes(a.method)) && (a.units === undefined || (Array.isArray(a.units) && a.units.length > 0 && a.units.every((u) => /^(P|EC)\d{1,3}$/.test(u))))
    && (a.ratio_cap === undefined || a.ratio_cap === null || int(a.ratio_cap, 8, 60)) && (a.memory_mb === undefined || int(a.memory_mb, 256, 1 << 20));
}

test("stressArgs: units are omitted when every unit is loaded, listed otherwise; cap and memory only when set", () => {
  const st = initialStress();
  st.sel = new Set(IDS);
  assert.deepEqual(stressArgs(st, IDS), { kind: "cpu", duration_s: 300, method: "integer" });
  st.sel = new Set(["P3", "EC1", "P0"]);
  st.method = "cache";
  st.cap = 48;
  st.dur = 900;
  assert.deepEqual(stressArgs(st, IDS), { kind: "cpu", duration_s: 900, method: "cache", units: ["P0", "P3", "EC1"], ratio_cap: 48 }); // topology order, not click order
  st.kind = "memory";
  st.memMb = 4096;
  assert.deepEqual(stressArgs(st, IDS), { kind: "memory", duration_s: 900, ratio_cap: 48, memory_mb: 4096 }); // no method, no units
  st.memMb = null;
  st.cap = null;
  assert.deepEqual(stressArgs(st, IDS), { kind: "memory", duration_s: 900 });
});

test("stressArgs always satisfies the ops.rs shape and the mock's checkArgs, over every control value", () => {
  const caps = [null, CAP_MIN, 33, CAP_MAX];
  for (const kind of ["cpu", "memory"]) {
    for (const method of ["integer", "vector-math", "cache"]) {
      for (const dur of [60, 300, 900, 3600, "custom"]) {
        for (const cap of caps) {
          for (const memMb of [null, ...MEM_MB]) {
            const st = { ...initialStress(), kind, method, cap, memMb, sel: new Set(["P1", "P2"]), custom: dur === "custom", customS: 100_000 /* clamped */, dur: dur === "custom" ? 300 : dur };
            const args = stressArgs(st, IDS);
            assert.ok(opsRsStressStart(args), JSON.stringify(args));
            assert.equal(checkArgs("stress.start", args), null, JSON.stringify(args));
          }
        }
      }
    }
  }
});

test("custom length is clamped to 30..3600 s and rounded", () => {
  const st = initialStress();
  st.custom = true;
  for (const [given, want] of [[0, DUR_MIN], [29, DUR_MIN], [30, 30], [90.4, 90], [5000, DUR_MAX], ["abc", DUR_MIN]]) {
    st.customS = given;
    assert.equal(stressDuration(st), want, String(given));
  }
});

test("selectedUnits: no selection yet = every unit; unknown ids in a selection are ignored", () => {
  assert.deepEqual(selectedUnits({ sel: null }, ["P0", "P1"]), ["P0", "P1"]);
  assert.deepEqual(selectedUnits({ sel: new Set(["P1", "ZZ"]) }, ["P0", "P1"]), ["P1"]);
});

// ---------------------------------------------------------------- gate + wording

test("opGate: not connected, refused system, busy, helper not idle; empty when fine", () => {
  const ok = { link: { state: "connected" }, preflight: pre, helperOp: { state: "idle" } };
  assert.equal(opGate(ok), "");
  assert.match(opGate({ ...ok, link: { state: "busy" } }), /not connected \(busy\)/);
  assert.match(opGate({ ...ok, link: { state: "not_installed" } }), /not installed/);
  assert.match(opGate({ ...ok, preflight: { mode: "refuse" } }), /refused/);
  assert.match(opGate(ok, true), /operation is running/);
  assert.match(opGate({ ...ok, helperOp: { state: "measuring" } }), /helper is measuring/);
});

test("estimateText: ready line says what raises power and heat; gate / empty selection / running win", () => {
  const st = { ...initialStress(), sel: new Set(IDS) };
  assert.match(estimateText(st, IDS, {}), /^Ready: integer load on all 12 units for 5 min\. It raises power and heat/);
  st.sel = new Set(["P0"]);
  st.cap = 50;
  assert.match(estimateText(st, IDS, {}), /integer load on 1 unit for 5 min, capped at 50×/);
  st.sel = new Set();
  assert.equal(estimateText(st, IDS, {}), "Select at least one unit.");
  assert.equal(estimateText(st, IDS, { gate: "The helper is busy." }), "The helper is busy.");
  assert.match(estimateText(st, IDS, { running: true }), /^Running\. Stop ends it early/);
  assert.match(estimateText(st, IDS, { running: true, stopping: true }), /^Stopping/);
  assert.match(estimateText({ ...st, kind: "memory", memMb: null }, IDS, {}), /memory test of 3\/4 of free memory on all CPUs/);
  assert.match(estimateText(st, [], {}), /topology is not known/);
});

test("result words: 'no errors detected in N min', never pass / stable (D-019)", () => {
  const base = { errors: 0, elapsed_s: 300, duration_s: 300, stopped_by: "time", health: "clean", mce_new: 0 };
  assert.equal(stSummary(base), "No errors detected in 5 min.");
  assert.equal(stSummary({ ...base, errors: 1 }), "1 error detected in 5 min. Treat this setting as unstable.");
  assert.equal(stSummary({ ...base, errors: 3 }), "3 errors detected in 5 min. Treat this setting as unstable.");
  assert.equal(stSummary({ ...base, stopped_by: "cancel", elapsed_s: 90 }), "Stopped after 1:30 of 5 min. No errors detected in that time.");
  assert.equal(stSummary({ ...base, stopped_by: "cancel", elapsed_s: 120, errors: 2 }), "Stopped after 2 min of 5 min. 2 errors detected.");
  assert.match(stSummary({ ...base, stopped_by: "error" }), /tool error after 5 min/);
  assert.match(stSummary({ ...base, health: "delta" }), /health check changed/);
  const forbidden = /\bpass|(?<!un)stable|proven|\bsafe/i;
  for (const r of [base, { ...base, errors: 1 }, { ...base, health: "delta" }, { ...base, stopped_by: "cancel" }, { ...base, stopped_by: "error" }]) assert.doesNotMatch(stSummary(r), forbidden);
});

test("resultFlags: bad for errors / tool error / health change / machine-check; cap pills follow cap_valid and cap_held", () => {
  const clean = { errors: 0, stopped_by: "time", health: "clean", mce_new: 0, cap_valid: null };
  assert.equal(resultFlags(clean).bad, false);
  assert.deepEqual(resultFlags(clean).pills.map((p) => p.text), ["ran to the end", "health clean"]);
  assert.equal(resultFlags({ ...clean, errors: 2 }).bad, true);
  assert.equal(resultFlags({ ...clean, mce_new: 1 }).bad, true);
  assert.ok(resultFlags({ ...clean, mce_new: 2 }).pills.some((p) => p.text === "2 new machine-check events"));
  assert.equal(resultFlags({ ...clean, health: "delta" }).pills[1].tone, "bad");
  assert.equal(resultFlags({ ...clean, stopped_by: "cancel" }).pills[0].text, "stopped by you");
  assert.equal(resultFlags({ ...clean, cap_valid: true, achieved: { P0: { cap_held: true } } }).pills.at(-1).text, "cap held");
  assert.equal(resultFlags({ ...clean, cap_valid: true, achieved: { P0: { cap_held: true }, P1: { cap_held: false } } }).pills.at(-1).tone, "warn");
  assert.equal(resultFlags({ ...clean, cap_valid: false }).pills.at(-1).text, "cap not held");
});

test("achievedText, screenText, unitsText, historyRow", () => {
  assert.deepEqual(achievedText({ mhz_mean: 4475, ratio_mode: 57 }), { sub: "57× mostly", mhz: "4475 MHz" });
  assert.equal(achievedText({ ratio_mode: 55, cap_ratio: 48, cap_held: false }).sub, "ran 55× (cap 48×)");
  assert.equal(achievedText({ ratio_mode: 48, cap_ratio: 48, cap_held: true }).sub, "cap held at 48×");
  assert.deepEqual(achievedText({}), { sub: "ratio n/a", mhz: "n/a" });
  assert.equal(screenText({ kind: "cpu", method: "vector-math", ratio_cap: 48 }), "CPU vector math, cap 48×");
  assert.equal(screenText({ kind: "memory", memory_mb: 2048 }), "Memory 2 GB");
  assert.equal(screenText({ kind: "memory", memory_mb: 512 }), "Memory 512 MB");
  assert.equal(unitsText({ kind: "memory" }, IDS), "all CPUs");
  assert.equal(unitsText({ kind: "cpu", units: IDS }, IDS), "all 12");
  assert.equal(unitsText({ kind: "cpu", units: ["P0", "EC0"] }, IDS), "P0 EC0");
  const row = historyRow({ id: "x", kind: "cpu", method: "integer", started: "2026-10-18T10:00:00Z", duration_s: 300, elapsed_s: 125, stopped_by: "cancel", errors: 2, health: "delta", mce_new: 1, max_temp_c: 71.4, max_pkg_w: 201.6 }, IDS);
  assert.deepEqual([row.ran, row.asked, row.ended.text, row.errors, row.health.text, row.mce, row.temp, row.power], ["2:05", "asked for 5 min", "stopped", 2, "changed", "1 MCE", "71 °C", "202 W"]);
  assert.equal(historyRow({ stopped_by: "error" }, IDS).ended.tone, "bad");
  assert.equal(historyRow({}, IDS).temp, "n/a");
});

test("fmtLen / mmss / ringFill / unitMhz / errText", () => {
  assert.deepEqual([30, 60, 90, 300, 3600, 7200, NaN].map(fmtLen), ["30 s", "1 min", "1:30", "5 min", "60 min", "120 min", "n/a"]);
  assert.deepEqual([0, 5, 65, 3599, -3, null].map(mmss), ["0:00", "0:05", "1:05", "59:59", "0:00", "0:00"]);
  assert.equal(ringFill(150, 300), 50);
  assert.equal(ringFill(400, 300), 100);
  assert.equal(ringFill(1, 0), 0);
  assert.equal(ringFill(null, 300), 0);
  const leaf = (v) => ({ value: v, status: v === null ? "unavailable" : "ok", reason: v === null ? "no msr" : undefined });
  assert.deepEqual(unitMhz({ cores: { 8: { mhz: leaf(3800) }, 9: { mhz: leaf(3900) }, 10: { mhz: leaf(null) } } }, [8, 9, 10]), { value: 3850, text: "3850 MHz" });
  assert.deepEqual(unitMhz(null, [0]), { value: null, text: "n/a" });
  assert.equal(errText({ code: "BUSY", message: "busy", detail: { state: "measuring" } }), "busy (measuring)");
  assert.equal(errText(undefined), "no answer from the helper");
});

test("the D-019 banner text is verbatim", () => {
  assert.equal(BANNER_HEAD, "Quick screen — NOT an acceptance gate.");
  assert.match(BANNER_TEXT, /cannot show that a setting is stable/);
  const src = readFileSync(join(UI, "views", "stress.js"), "utf8");
  assert.match(src, /id: "st-banner", role: "note"/);
  assert.doesNotMatch(src, /st-banner[^\n]*hidden/); // the banner is never hidden
});

// ---------------------------------------------------------------- controller against the recorded simulator answers

test("controller: start -> progress -> result card data -> history refreshed", async () => {
  const { bridge, store } = await rig();
  const ctl = createStressController({ bridge, store });
  ctl.st.sel = new Set(["P0", "EC0"]);
  let paints = 0;
  ctl.subscribe(() => paints++);
  assert.equal(ctl.gate(), "");
  assert.equal(await ctl.start(), true);
  assert.ok(ctl.view.run && ctl.view.run.of === 300 && /^Started/.test(ctl.view.status));
  await until(() => ctl.view.result);
  assert.equal(ctl.view.run, null);
  assert.equal(ctl.view.result.kind, "cpu");
  assert.equal(ctl.view.result.errors, 0);
  assert.equal(ctl.view.result.stopped_by, "time");
  assert.equal(ctl.view.result.duration_s, 300); // the asked length, not the recorded 30 s
  await until(() => ctl.view.list.length === 1);
  assert.equal(ctl.view.list[0].id, ctl.view.result.id);
  assert.equal(ctl.view.list[0].method, "integer");
  assert.deepEqual(ctl.view.list[0].units, ["P0", "EC0"]);
  assert.ok(paints > 3);
  await bridge.close();
});

test("controller: progress events move the ring (elapsed, of_s); a measurement's progress (step/of) is not a stress run", async () => {
  const { bridge, store } = await rig({ stepMs: 30 });
  const ctl = createStressController({ bridge, store });
  await ctl.start();
  await until(() => ctl.view.run && ctl.view.run.elapsed > 0);
  assert.ok(ctl.view.run.elapsed > 0 && ctl.view.run.elapsed <= 300);
  assert.equal(ctl.view.run.of, 300); // the mock stretches the recorded 30 s clock to the length asked for
  await until(() => ctl.view.result);
  await bridge.close();
  // a vf.measure progress event (carries `step`) never creates a run
  let emit = () => {};
  const b2 = new Bridge({ connect: async () => ({ state: "connected" }), call: async () => ({ ok: true, result: { results: [] } }), listen: (ev, cb) => { if (ev === "progress") emit = cb; return () => {}; } });
  const ctl2 = createStressController({ bridge: b2, store });
  emit({ op_id: "m1", step: 1, of: 20, unit: "P0", ratio: 8, eta_s: 2 });
  emit({ op_id: "m1", step: 2, of_s: 20, elapsed_s: 3 });
  assert.equal(ctl2.view.run, null);
});

test("controller: a second start while one runs is refused locally; the helper's BUSY is shown, not hidden", async () => {
  const { bridge, store } = await rig({ stepMs: 40 });
  const ctl = createStressController({ bridge, store });
  assert.equal(await ctl.start(), true);
  assert.equal(await ctl.start(), false); // gate: a run is in flight
  const direct = await bridge.call("stress.start", { kind: "cpu", duration_s: 30 });
  assert.equal(direct.ok, false);
  assert.equal(direct.error.code, "BUSY");
  assert.equal(errText(direct.error).includes("(stressing)"), true);
  const measure = await bridge.call("vf.measure", { mode: "quick" });
  assert.equal(measure.error.code, "BUSY");
  await ctl.stop();
  await until(() => ctl.view.result);
  await bridge.close();
});

test("controller: Stop -> stopped_by cancel with the elapsed time; the stored row says 'stopped'", async () => {
  const { bridge, store } = await rig({ stepMs: 25 });
  const ctl = createStressController({ bridge, store });
  ctl.st.dur = 3600;
  await ctl.start();
  await until(() => ctl.view.run && ctl.view.run.elapsed >= 0 && ctl.view.run.elapsed !== undefined);
  assert.equal(await ctl.stop(), true);
  assert.equal(ctl.view.stopping, true);
  assert.match(ctl.view.status, /Stop requested/);
  await until(() => ctl.view.result);
  assert.equal(ctl.view.result.stopped_by, "cancel");
  assert.equal(ctl.view.result.duration_s, 3600);
  assert.match(stSummary(ctl.view.result), /^Stopped after /);
  assert.equal(ctl.view.stopping, false);
  await until(() => ctl.view.list.length === 1);
  assert.equal(historyRow(ctl.view.list[0], IDS).ended.text, "stopped");
  await bridge.close();
});

test("controller: stop with nothing running is a no-op; the helper's idle answer is the recorded one", async () => {
  const { bridge, store } = await rig();
  const ctl = createStressController({ bridge, store });
  assert.equal(await ctl.stop(), false);
  const r = await bridge.call("stress.stop", {});
  assert.deepEqual(r.result, G["stress-stop-idle"].response.result);
  assert.equal(r.result.cancelled, false);
});

test("controller: gated by the link and the helper state; nothing is sent when the helper is not idle", async () => {
  const { bridge, store } = await rig({ connected: false });
  const ctl = createStressController({ bridge, store });
  assert.match(ctl.gate(), /not connected/);
  assert.equal(await ctl.start(), false);
  setLink(store, await bridge.connect());
  setPreflight(store, pre);
  assert.equal(ctl.gate(), "");
  store.update((s) => ({ helperOp: { ...s.helperOp, state: "applying" } }));
  assert.match(ctl.gate(), /helper is applying/);
  assert.equal(await ctl.start(), false);
  assert.equal(ctl.view.run, null);
});

test("controller: no unit selected for a CPU screen = nothing started; a memory screen needs none", async () => {
  const { bridge, store } = await rig();
  const ctl = createStressController({ bridge, store });
  ctl.st.sel = new Set();
  assert.equal(await ctl.start(), false);
  ctl.st.kind = "memory";
  ctl.st.memMb = 2048;
  assert.equal(await ctl.start(), true);
  await until(() => ctl.view.result);
  assert.equal(ctl.view.result.kind, "memory");
  await until(() => ctl.view.list.length === 1);
  assert.equal(ctl.view.list[0].memory_mb, 2048);
  assert.equal(screenText(ctl.view.list[0]), "Memory 2 GB");
});

test("controller: a run begun elsewhere is adopted from its progress events and its result is kept", async () => {
  const { bridge, store } = await rig({ stepMs: 10 });
  const ctl = createStressController({ bridge, store });
  const first = await bridge.call("stress.start", { kind: "cpu", duration_s: 60 }); // not through the controller (as after a reload)
  assert.equal(first.pending, true);
  await until(() => ctl.view.run && ctl.view.run.adopted);
  assert.equal(ctl.view.run.id, first.id);
  await until(() => ctl.view.result);
  assert.equal(ctl.view.run, null);
  assert.equal(ctl.view.result.kind, "cpu");
  // a late progress event of the finished op does not start a second run
  assert.equal(ctl.view.done.has(first.id), true);
});

test("controller: a failed result is shown as Failed: ... and there is no result card", async () => {
  let emit = () => {};
  const bridge = new Bridge({
    connect: async () => ({ state: "connected" }),
    call: async (op) => (op === "stress.start" ? { id: "x1" } : { ok: true, result: { results: [] } }),
    listen: (ev, cb) => {
      if (ev === "result") emit = cb;
      return () => {};
    },
  });
  const store = createAppStore();
  setLink(store, await bridge.connect());
  setPreflight(store, pre);
  const ctl = createStressController({ bridge, store });
  assert.equal(await ctl.start(), true);
  emit({ id: "x1", ok: false, error: { code: "REFUSED", message: "stress-ng is not installed" } });
  await until(() => ctl.view.run === null);
  assert.equal(ctl.view.result, null);
  assert.equal(ctl.view.status, "Failed: stress-ng is not installed");
});

test("stress.list: the loaded rows come back newest first and survive a refresh", async () => {
  const { bridge, store } = await rig({ seed: "recorded" });
  const ctl = createStressController({ bridge, store });
  await ctl.loadList();
  assert.equal(ctl.view.loaded, true);
  assert.equal(ctl.view.list.length, G["stress-list"].result.results.length);
  assert.ok(ctl.view.list.length >= 1);
  for (const r of ctl.view.list) assert.equal(typeof historyRow(r, IDS).screen, "string");
});

test("mock ops refuse arguments ops.rs refuses", async () => {
  const { bridge } = await rig();
  for (const bad of [{ kind: "cpu" }, { kind: "cpu", duration_s: 29 }, { kind: "cpu", duration_s: 3601 }, { kind: "disk", duration_s: 60 }, { kind: "cpu", duration_s: 60, method: "fft" }, { kind: "cpu", duration_s: 60, ratio_cap: 7 }, { kind: "cpu", duration_s: 60, units: [] }, { kind: "cpu", duration_s: 60, units: ["X1"] }, { kind: "memory", duration_s: 60, memory_mb: 255 }, { kind: "cpu", duration_s: 60, extra: 1 }]) {
    const r = await bridge.call("stress.start", bad);
    assert.equal(r.ok, false, JSON.stringify(bad));
    assert.equal(r.error.code, "BAD_REQUEST");
  }
});

// ---------------------------------------------------------------- fixtures

test("g218 fixtures are the simulator's own: no host paths, no real boot id; fixtures-g218.js equals the JSON files", () => {
  const dir = join(UI, "fixtures", "g218");
  const names = readdirSync(dir).filter((n) => n.endsWith(".json"));
  assert.ok(names.length >= 14);
  assert.equal(Object.keys(G).length, names.length);
  for (const n of names) {
    const text = readFileSync(join(dir, n), "utf8");
    assert.ok(!text.includes("/home/"), `${n}: no home paths`);
    assert.ok(!text.includes("/tmp/"), `${n}: no temp paths`);
    assert.ok(!/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/.test(text.replace(/00000000-0000-4000-8000-000000000000/g, "")), `${n}: no real boot id`);
    assert.deepEqual(G[n.replace(/\.json$/, "")], JSON.parse(text), `${n} is in fixtures-g218.js (run gen_fixtures_g218.py after changing them)`);
  }
});
