// node --test desktop/app/ui/tests/   (G-205: store, bridge, mock backend, pure formatting; no DOM, no network, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, extname, relative } from "node:path";

import {
  createStore, createAppStore, initialState, SERIES, SCREEN_IDS, HISTORY_CAP, applyHelperEvent, deriveMode, deriveAccepted, limitBadges, pushSample,
  setScreen, setPending, toggleSeries, setStrip, pushToast, dismissToast, MAX_TOASTS, openDrawer, closeDrawer, setLastRead, setPreflight,
} from "../store.js";
import { Bridge, normalizeLink, selectBridge, FIXES } from "../bridge.js";
import { createMockTransport, FIXTURES } from "../fixtures/mock-backend.js";
import { fmtNum, fmtWithUnit, fmtClock, sparkSegments, plural } from "../components/fmt.js";
import { SCREENS, screenForKey } from "../components/nav-rail.js";
import { pendingText } from "../components/pending-bar.js";
import { acceptedLabel, linkLabel } from "../components/status-bar.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const FAST = { intervalMs: 10, stepMs: 1 };

async function connected(opts = {}) {
  const bridge = new Bridge(createMockTransport({ ...FAST, ...opts }), "mock");
  await bridge.connect();
  return bridge;
}

// ---------------------------------------------------------------- store
test("store: set/update/subscribe/watch", () => {
  const s = createStore({ a: 1, b: 2 });
  const seen = [];
  const un = s.subscribe((st, prev) => seen.push([prev.a, st.a]));
  const watched = [];
  s.watch((st) => st.b, (b) => watched.push(b));
  s.set({ a: 2 });
  s.update((st) => ({ a: st.a + 1 }));
  s.update(() => null); // null patch = no change, no notification
  s.set({ b: 9 });
  assert.deepEqual(seen, [[1, 2], [2, 3], [3, 3]]);
  assert.deepEqual(watched, [2, 9]); // initial value, then only when b changed
  un();
  s.set({ a: 99 });
  assert.equal(seen.length, 3);
  assert.deepEqual(s.get(), { a: 99, b: 9 });
});

test("store: initial state has the seven screens and an unconnected link", () => {
  const st = initialState();
  assert.deepEqual(SCREEN_IDS, ["dashboard", "tune", "curves", "stress", "profiles", "system", "audit"]);
  assert.deepEqual(SCREENS.map((s) => s.id), SCREEN_IDS);
  assert.equal(st.link.state, "connecting");
  assert.equal(st.pending.changes, 0);
  assert.equal(st.strip.series.length, SERIES.length);
});

test("store: setScreen refuses unknown screens", () => {
  const s = createAppStore();
  assert.equal(setScreen(s, "tune"), true);
  assert.equal(s.get().screen, "tune");
  assert.equal(setScreen(s, "nope"), false);
  assert.equal(s.get().screen, "tune");
});

test("store: pending counts never go negative and are integers", () => {
  const s = createAppStore();
  setPending(s, 3.9, 2);
  assert.deepEqual(s.get().pending, { changes: 3, units: 2 });
  setPending(s, -4, -1);
  assert.deepEqual(s.get().pending, { changes: 0, units: 0 });
  assert.equal(pendingText({ changes: 1, units: 1 }), "1 change on 1 unit");
  assert.equal(pendingText({ changes: 5, units: 2 }), "5 changes on 2 units");
});

test("store: toasts: ids, caps, ttl defaults, dismiss", () => {
  const s = createAppStore();
  const a = pushToast(s, { kind: "ok", text: "done" });
  const e = pushToast(s, { kind: "error", text: "bad" });
  const bogus = pushToast(s, { kind: "nonsense", text: "x" });
  const t = s.get().toasts;
  assert.deepEqual(t.map((x) => x.kind), ["ok", "error", "info"]);
  assert.equal(t[1].ttlMs, 0); // errors stay until dismissed
  assert.ok(t[0].ttlMs > 0);
  dismissToast(s, e);
  assert.deepEqual(s.get().toasts.map((x) => x.id), [a, bogus]);
  for (let i = 0; i < 10; i++) pushToast(s, { text: `n${i}` });
  assert.equal(s.get().toasts.length, MAX_TOASTS);
  assert.equal(s.get().toasts.at(-1).text, "n9");
  let calls = 0;
  s.subscribe(() => calls++);
  dismissToast(s, 99999); // unknown id: no notification
  assert.equal(calls, 0);
});

test("store: drawer open/close keeps the title", () => {
  const s = createAppStore();
  openDrawer(s, "Review & Apply");
  assert.deepEqual(s.get().drawer, { open: true, title: "Review & Apply" });
  closeDrawer(s);
  assert.deepEqual(s.get().drawer, { open: false, title: "Review & Apply" });
});

test("store: toggleSeries keeps canonical order and can empty the strip", () => {
  const s = createAppStore();
  toggleSeries(s, "clock");
  assert.equal(s.get().strip.series.includes("clock"), false);
  toggleSeries(s, "clock");
  assert.deepEqual(s.get().strip.series, SERIES.map((x) => x.key));
  for (const x of SERIES) toggleSeries(s, x.key);
  assert.deepEqual(s.get().strip.series, []);
  setStrip(s, { collapsed: true });
  assert.equal(s.get().strip.collapsed, true);
});

test("store: deriveMode explains read-only from the failing/unknown check", () => {
  assert.equal(deriveMode(null).label, "unknown");
  const pre = FIXTURES.preflight.result;
  const m = deriveMode(pre);
  assert.equal(pre.writes_allowed, false);
  assert.equal(m.writes, false);
  assert.equal(m.label, "read-only");
  assert.match(m.reason, /^msr_allow_writes: /); // the first non-pass check in the recorded preflight
  assert.deepEqual(deriveMode({ writes_allowed: true, checks: [] }), { writes: true, label: "writes allowed", reason: "" });
});

test("store: deriveAccepted", () => {
  assert.equal(deriveAccepted(null).kind, "unknown");
  assert.deepEqual(deriveAccepted([{ cpu: 0, match: true }]).kind, "matches");
  assert.equal(deriveAccepted([{ cpu: 0, match: true, equivalent: true }]).kind, "equivalent");
  const d = deriveAccepted([{ cpu: 0, match: false }, { cpu: 1, match: false }, { cpu: 2, match: true }]);
  assert.deepEqual([d.kind, d.n], ["differs", 2]);
  assert.equal(acceptedLabel(d), "accepted: differs: 2");
  assert.equal(acceptedLabel(deriveAccepted(null)), "accepted: unknown");
});

test("store: setLastRead / setPreflight", () => {
  const s = createAppStore();
  setPreflight(s, FIXTURES.preflight.result);
  assert.equal(s.get().mode.label, "read-only");
  setLastRead(s, "2026-10-18T10:00:00Z", [{ cpu: 0, match: true }]);
  assert.equal(s.get().lastReadAt, "2026-10-18T10:00:00Z");
  assert.equal(s.get().accepted.kind, "matches");
  setLastRead(s, "2026-10-18T10:05:00Z"); // accepted untouched when not given
  assert.equal(s.get().accepted.kind, "matches");
});

// ---- telemetry -> series
const raw = FIXTURES.telemetry.events.filter((e) => e.event === "telemetry");

test("fixture telemetry (unprivileged simulator) is all n/a with reasons, never a guessed number", () => {
  assert.ok(raw.length >= 5);
  const s = createAppStore();
  applyHelperEvent(s, "telemetry", raw[0]);
  const smp = s.get().telemetry.samples;
  for (const def of SERIES) {
    assert.equal(smp[def.key].value, null, def.key);
    assert.ok(smp[def.key].reason, `${def.key} carries its reason`);
  }
  assert.equal(s.get().telemetry.limits.state, "na");
});

test("series pickers: max of cores/sensors, summed DRAM, unavailable stays unavailable", () => {
  const values = {
    cores: { 0: { mhz: { value: 5100, status: "ok", unit: "MHz" } }, 1: { mhz: { value: 5300, status: "ok", unit: "MHz" } }, 2: { mhz: { value: null, status: "unavailable", reason: "x" } } },
    temps: { Package: { value: 61, status: "ok" }, "P-Core 3": { value: 67, status: "ok" } },
    power: { package: { value: 90.5, status: "ok", unit: "W", source: "powercap" }, core: { value: null, status: "unavailable", reason: "no 'core' subdomain" } },
    vcore: { value: 1.21, status: "ok", unit: "V" },
    dram: { read: { value: 10, status: "ok" }, write: { value: null, status: "unavailable", reason: "nope" } },
    limit_reasons_raw: { value: 0x10, status: "ok", source: "msr 0x6B0" },
  };
  const s = createAppStore();
  applyHelperEvent(s, "telemetry", { values, t: 1 });
  const smp = s.get().telemetry.samples;
  assert.equal(smp.clock.value, 5300);
  assert.equal(smp.temp.value, 67);
  assert.equal(smp.pkg_power.value, 90.5);
  assert.equal(smp.core_power.value, null);
  assert.equal(smp.core_power.reason, "no 'core' subdomain");
  assert.equal(smp.vcore.value, 1.21);
  assert.equal(smp.dram.value, 10);
  assert.deepEqual([s.get().telemetry.limits.state, s.get().telemetry.limits.raw], ["raw", 16]);
  assert.equal(limitBadges({ limit_reasons_raw: { value: 0, status: "ok" } }).state, "none");
});

test("history is capped and keeps gaps as null", () => {
  const s = createAppStore();
  for (let i = 0; i < HISTORY_CAP + 30; i++) {
    const values = { power: { package: i % 5 === 0 ? { value: null, status: "unavailable", reason: "r" } : { value: i, status: "ok" } } };
    applyHelperEvent(s, "telemetry", { values });
  }
  const h = s.get().telemetry.history.pkg_power;
  assert.equal(h.length, HISTORY_CAP);
  assert.equal(h.at(-1), HISTORY_CAP + 29);
  assert.ok(h.includes(null));
  assert.deepEqual(pushSample({}, Object.fromEntries(SERIES.map((d) => [d.key, { value: 1 }])), 1).pkg_power, [1]);
});

test("heartbeat/state/progress events update the helper op; unknown events are ignored", () => {
  const s = createAppStore();
  assert.equal(applyHelperEvent(s, "heartbeat", { state: "idle", op_id: null, ops_used: 7, ops_budget: 100 }), true);
  assert.deepEqual(s.get().helperOp, { state: "idle", opId: null, opsUsed: 7, opsBudget: 100 });
  applyHelperEvent(s, "state", { op_id: "x1", state: "applying" });
  assert.equal(s.get().helperOp.state, "applying");
  assert.equal(s.get().helperOp.opsUsed, 7); // kept
  applyHelperEvent(s, "progress", { op_id: "x1", step: 1, of: 6, ops_used: 9, ops_budget: 100 });
  assert.equal(s.get().helperOp.opsUsed, 9);
  assert.equal(applyHelperEvent(s, "vf.point", {}), false);
});

// ---------------------------------------------------------------- formatting
test("fmt: n/a, units, clock, sparkline segments", () => {
  assert.equal(fmtNum(null), "n/a");
  assert.equal(fmtNum(NaN), "n/a");
  assert.equal(fmtNum(3.14159, 2), "3.14");
  assert.equal(fmtWithUnit(1.2346, "V", 3), "1.235 V");
  assert.equal(fmtWithUnit(undefined, "V"), "n/a");
  assert.equal(fmtClock(null), "never");
  assert.match(fmtClock(0), /^\d\d:\d\d:\d\d$/);
  assert.equal(plural(1, "unit"), "1 unit");
  assert.equal(plural(2, "unit"), "2 units");
  assert.deepEqual(sparkSegments([], 100, 20), []);
  assert.deepEqual(sparkSegments([null, null], 100, 20), []);
  const segs = sparkSegments([1, 2, null, 3, 4], 100, 20);
  assert.equal(segs.length, 2);
  assert.equal(segs[0].length, 2);
  assert.equal(sparkSegments([5], 100, 20)[0].length, 1);
  assert.ok(sparkSegments([5, 5, 5], 100, 20)[0].every((p) => !p.includes("NaN"))); // flat series must not divide by zero
});

test("nav: keys 1..7 map to the seven screens; modifiers and typing are ignored", () => {
  assert.deepEqual("1234567".split("").map((k) => screenForKey(k)), SCREEN_IDS);
  assert.equal(screenForKey("8"), null);
  assert.equal(screenForKey("2", { typing: true }), null);
  assert.equal(screenForKey("2", { ctrl: true }), null);
  assert.equal(screenForKey("a"), null);
});

// ---------------------------------------------------------------- bridge: link
test("bridge: normalizeLink maps helper-link kinds to named states and fixes", () => {
  assert.equal(normalizeLink({ connected: true }).state, "connected");
  assert.equal(normalizeLink({ kind: "busy" }).state, "busy");
  assert.equal(normalizeLink({ kind: "missing" }).fix, FIXES.not_installed);
  assert.equal(normalizeLink({ kind: "denied" }).state, "not_allowed");
  assert.match(normalizeLink({ kind: "not_allowed" }).fix, /--add-user/);
  assert.equal(normalizeLink({ kind: "wat" }).state, "error");
  assert.equal(normalizeLink(null).state, "error");
  assert.equal(normalizeLink({ state: "connected" }).fix, null);
  assert.equal(linkLabel({ state: "busy" }), "helper busy (another client)");
});

for (const [helper, state] of [["busy", "busy"], ["not_installed", "not_installed"], ["not_allowed", "not_allowed"], ["error", "error"]]) {
  test(`bridge(mock): helper=${helper} reports ${state} with a named fix, and refuses calls`, async () => {
    const b = await connected({ helper });
    const link = await b.status();
    assert.equal(link.state, state);
    if (state !== "error") assert.ok(link.fix, "has a fix text");
    const r = await b.call("preflight");
    assert.equal(r.ok, false);
    assert.equal(r.error.code, "NOT_CONNECTED");
    await b.close();
  });
}

test("bridge(mock): connect, status, short ops return the recorded responses", async () => {
  const b = await connected();
  assert.equal(b.name, "mock");
  assert.equal((await b.status()).state, "connected");
  const pre = await b.call("preflight");
  assert.equal(pre.ok, true);
  assert.deepEqual(pre.result.topology.units.map((u) => u.id).slice(0, 2), ["P0", "P1"]);
  assert.equal(pre.result.board, "Simulated board");
  for (const op of ["tune.last", "profiles.list", "audit.tail"]) assert.equal((await b.call(op)).ok, true, op);
  const unk = await b.call("vf.measure", {});
  assert.deepEqual([unk.ok, unk.error.code], [false, "UNKNOWN_OP"]);
  await assert.rejects(() => b.call("", {}), TypeError);
  await assert.rejects(() => b.call("preflight", [1]), TypeError);
  await b.close();
});

test("bridge(mock): tune.plan needs an intent; tune.apply checks plan id and nonce", async () => {
  const b = await connected();
  assert.equal((await b.call("tune.plan", {})).error.code, "BAD_REQUEST");
  const plan = await b.call("tune.plan", { intent: { scope: "unit", rows: [] } });
  assert.equal(plan.ok, true);
  assert.match(plan.result.nonce, /^[A-Z2-9]{6}$/);
  assert.equal((await b.call("tune.apply", { plan_id: "0".repeat(64), typed: `CONFIRM ${plan.result.nonce}` })).error.code, "PLAN_STALE");
  assert.equal((await b.call("tune.apply", { plan_id: plan.result.plan_id, typed: "CONFIRM WRONG1" })).error.code, "CONFIRM_MISMATCH");
  await b.close();
});

test("bridge(mock): long op tune.read answers {id}, streams progress, ends with a result event", async () => {
  const b = await connected();
  const events = [];
  b.on("progress", (p) => events.push(["progress", p]));
  b.on("state", (p) => events.push(["state", p]));
  const results = [];
  b.on("result", (r) => results.push(r));
  const first = await b.call("tune.read");
  assert.equal(first.ok, true);
  assert.equal(first.pending, true);
  assert.ok(first.id);
  const final = await b.wait(first.id);
  assert.equal(final.id, first.id);
  assert.equal(final.ok, true);
  assert.ok(final.result.tune.P0, "decoded tune table");
  assert.match(final.result.read_at, /^\d{4}-\d\d-\d\dT/);
  assert.equal(results.length, 1);
  assert.equal(events[0][0], "state");
  assert.ok(events.filter((e) => e[0] === "progress").length > 50);
  assert.ok(events.every((e) => e[1].op_id === first.id));
  assert.deepEqual(events.at(-1), ["state", { op_id: first.id, state: "idle" }]);
  assert.equal((await b.wait(first.id)).id, first.id, "wait() after the event still resolves");
  await b.close();
});

test("bridge(mock): a second long op while one runs is BUSY; apply works with the right nonce", async () => {
  const b = await connected();
  const read = await b.call("tune.read");
  assert.equal((await b.call("tune.read")).error.code, "BUSY");
  await b.wait(read.id);
  const plan = (await b.call("tune.plan", { intent: { scope: "unit", rows: [] } })).result;
  const applied = await b.run("tune.apply", { plan_id: plan.plan_id, typed: `CONFIRM ${plan.nonce}` });
  assert.equal(applied.ok, true);
  assert.equal(applied.result.complete, true);
  assert.ok(applied.result.readback.every((r) => r.match === true));
  await b.close();
});

test("bridge(mock): telemetry.start streams telemetry + heartbeat on a timer; off() stops delivery; stop ends the stream", async () => {
  const b = await connected();
  const got = [];
  const beats = [];
  const cb = (p) => got.push(p);
  b.on("telemetry", cb);
  b.on("heartbeat", (p) => beats.push(p));
  const start = await b.call("telemetry.start", { interval_s: 0.01 });
  assert.equal(start.result.running, true);
  await sleep(120);
  assert.ok(got.length >= 4, `got ${got.length}`);
  assert.ok(beats.length >= 1);
  assert.ok(got.every((p) => p.values && p.values.cores && typeof p.t === "number"));
  b.off("telemetry", cb);
  const n = got.length;
  await sleep(50);
  assert.equal(got.length, n);
  const stop = await b.call("telemetry.stop");
  assert.equal(stop.result.stopped, true);
  await b.close();
});

test("bridge(mock): motion=1 fills the invented waveform but keeps DRAM and core power n/a", async () => {
  const b = await connected({ motion: true });
  const s = createAppStore();
  b.on("telemetry", (p) => applyHelperEvent(s, "telemetry", p));
  await b.call("telemetry.start", {});
  await sleep(60);
  const smp = s.get().telemetry.samples;
  assert.ok(smp.pkg_power.value > 50 && smp.pkg_power.value < 130);
  assert.ok(smp.clock.value >= 4000);
  assert.equal(smp.dram.value, null);
  assert.equal(smp.core_power.value, null);
  await b.close();
  // the checked-in fixture itself stays raw
  assert.ok(raw.every((e) => e.values.power.package.value === null));
});

test("bridge: a throwing listener does not break others; a throwing transport becomes BRIDGE_ERROR", async () => {
  const t = createMockTransport(FAST);
  const b = new Bridge(t, "mock");
  await b.connect();
  const ok = [];
  b.on("telemetry", () => { throw new Error("boom"); });
  b.on("telemetry", (p) => ok.push(p));
  const orig = console.error;
  console.error = () => {};
  try {
    await b.call("telemetry.start", {});
    await sleep(40);
  } finally {
    console.error = orig;
  }
  assert.ok(ok.length >= 1);
  await b.close();
  const bad = new Bridge({ connect: async () => ({ state: "connected" }), call: async () => { throw new Error("pipe closed"); }, listen: () => () => {} }, "x");
  await bad.connect();
  const r = await bad.call("preflight");
  assert.deepEqual([r.ok, r.error.code, r.error.message], [false, "BRIDGE_ERROR", "pipe closed"]);
});

test("bridge: link event fires on change only; connect failure is reported as error state", async () => {
  const links = [];
  const b = new Bridge({ connect: async () => { throw new Error("no socket"); }, call: async () => ({}), listen: () => () => {} }, "x");
  b.on("link", (l) => links.push(l.state));
  assert.equal((await b.connect()).state, "error");
  await b.connect();
  assert.deepEqual(links, ["error"]);
});

test("bridge: selectBridge picks mock from ?bridge=mock, and an honest 'no runtime' backend otherwise", async () => {
  const m = await selectBridge("?bridge=mock&motion=1&tick=50", undefined);
  assert.equal(m.name, "mock");
  assert.equal((await m.connect()).state, "connected");
  await m.close();
  const none = await selectBridge("", undefined);
  assert.equal(none.name, "none");
  const link = await none.connect();
  assert.equal(link.state, "error");
  assert.match(link.message, /bridge=mock/);
  const fakeTauri = { core: { invoke: async (cmd, a) => (cmd === "helper_call" ? { id: "r1", ok: true, result: { echo: a } } : { state: "connected" }) }, event: { listen: async () => () => {} } };
  const real = await selectBridge("", fakeTauri);
  assert.equal(real.name, "tauri");
  assert.equal((await real.connect()).state, "connected");
  assert.deepEqual((await real.call("preflight", { x: 1 })).result.echo, { op: "preflight", args: { x: 1 } });
});

test("tauri transport: events are subscribed as helper://<event> and unsubscribed when the last listener leaves", async () => {
  const subs = [];
  const unlistened = [];
  const fakeTauri = {
    core: { invoke: async () => ({ state: "connected" }) },
    event: { listen: async (name, cb) => { subs.push([name, cb]); return () => unlistened.push(name); } },
  };
  const b = await selectBridge("", fakeTauri);
  await b.connect();
  const got = [];
  const cb = (p) => got.push(p);
  b.on("telemetry", cb);
  await sleep(5);
  const entry = subs.find(([n]) => n === "helper://telemetry");
  assert.ok(entry, "listened to helper://telemetry");
  assert.ok(subs.find(([n]) => n === "helper://result"), "result is always listened to");
  entry[1]({ payload: { t: 1 } });
  assert.deepEqual(got, [{ t: 1 }]);
  b.off("telemetry", cb);
  await sleep(5);
  assert.deepEqual(unlistened, ["helper://telemetry"]);
});

test("fixtures are the simulator's own: no host leakage", () => {
  const text = JSON.stringify(FIXTURES);
  assert.ok(!text.includes("/home/"), "no home paths");
  assert.ok(!/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/.test(text.replace(/00000000-0000-4000-8000-000000000000/g, "")), "no real boot id");
  assert.equal(FIXTURES.tuneRead.result.simulator, true);
});

const UI = fileURLToPath(new URL("..", import.meta.url));

test("fixtures.js (what the browser loads) equals the recorded *.json files (run gen_fixtures.py after changing them)", () => {
  const dir = join(UI, "fixtures");
  const names = readdirSync(dir).filter((n) => n.endsWith(".json"));
  assert.ok(names.length >= 12);
  const bundle = Object.values(FIXTURES);
  assert.equal(bundle.length, names.length);
  for (const n of names) {
    const json = JSON.parse(readFileSync(join(dir, n), "utf8"));
    assert.ok(bundle.some((b) => JSON.stringify(b) === JSON.stringify(json)), `${n} is in fixtures.js`);
  }
});

// ---------------------------------------------------------------- strict-CSP hygiene (PLAN §2), static

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === "tests" || name === "node_modules") continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

test("hygiene: no eval, innerHTML, document.write, inline handlers or remote URLs in the UI sources", () => {
  const files = walk(UI).filter((p) => [".js", ".html", ".css"].includes(extname(p)));
  assert.ok(files.length >= 15);
  const banned = [
    /\beval\s*\(/, /new Function\s*\(/, /\.innerHTML\b/, /\.outerHTML\b/, /insertAdjacentHTML/, /document\.write/, /\.style\.cssText/,
    /\ssetAttribute\(\s*["']style["']/, /\sstyle\s*=\s*["']/, /\son[a-z]+\s*=\s*["']/i, /(?:https?:)?\/\/(?!www\.w3\.org)[a-z0-9.-]+\.[a-z]{2,}\//i,
  ];
  const bad = [];
  for (const f of files) {
    const text = readFileSync(f, "utf8").replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const re of banned) if (re.test(text)) bad.push(`${relative(UI, f)}: ${re}`);
  }
  assert.deepEqual(bad, []);
});

test("hygiene: index.html has one external module script and no inline script or style", () => {
  const html = readFileSync(join(UI, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  assert.equal(scripts.length, 1);
  assert.match(scripts[0][1], /type="module"/);
  assert.match(scripts[0][1], /src="app\.js"/);
  assert.equal(scripts[0][2].trim(), "");
  assert.ok(!/<style\b/.test(html));
});

test("hygiene: every icon named in icons.css exists, and the shipped CSP is the strict one", () => {
  const css = readFileSync(join(UI, "styles/icons.css"), "utf8");
  for (const m of css.matchAll(/url\("\.\.\/icons\/([a-z-]+\.svg)"\)/g)) assert.doesNotThrow(() => statSync(join(UI, "icons", m[1])), m[1]);
  for (const s of SCREEN_IDS) assert.match(css, new RegExp(`data-icon="${s}"`));
  const conf = JSON.parse(readFileSync(join(UI, "../src-tauri/tauri.conf.json"), "utf8"));
  assert.match(conf.app.security.csp, /^default-src 'self'; script-src 'self'; style-src 'self'/);
});
