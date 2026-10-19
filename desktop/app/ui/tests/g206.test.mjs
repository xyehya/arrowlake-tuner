// node --test "desktop/app/ui/tests/*.test.mjs"   (G-206: pure logic of the Dashboard, System and Audit screens and their components; no DOM, no network, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { createAppStore, applyHelperEvent, SERIES } from "../store.js";
import { FIXTURES } from "../fixtures/mock-backend.js";
import G from "../fixtures/g217/fixtures-g217.js";
import { sparkPath } from "../components/spark.js";
import { niceNum, niceTicks, padSpan, dataRange, chartPaths, fmtSpan, nearestIndex, MIN_SPAN, CHART_W, CHART_H } from "../components/chart.js";
import {
  LEVELS, METRICS, levelOf, autoRange, rampAt, rampCss, tempsByCpu, coreReading, readingValue, clockCeiling, buildCoreMap, clamp,
} from "../components/coremap.js";
import { deriveThrottle } from "../store.js";
import { tileModel, throttleTileModel, appendChartSample, chartReasons, opsModel, memModel, ageText, HISTORY_S } from "../views/dashboard.js";
import {
  sortChecks, checkState, mergeCpuLimits, limitRows, limitsNote, preflightSummary, countStatuses, topologyRows, fmtSigned, MINUS,
} from "../views/system.js";
import { auditRequest, auditKeep, auditRow, auditRows, auditNote, auditTime, MB_NAMES, hex } from "../views/audit.js";

const UI = fileURLToPath(new URL("..", import.meta.url));
const pre = FIXTURES.preflight.result;
const units = pre.topology.units;
const rawFrame = FIXTURES.telemetry.events.find((e) => e.event === "telemetry");

/** A telemetry `values` object with live numbers for every core (what the helper would send on a machine with MSR access). */
function liveValues() {
  const cores = {};
  units.forEach((u) =>
    u.cpus.forEach((cpu) => {
      const leaf = (value, unit) => ({ value, unit, status: "ok", source: "test" });
      cores[String(cpu)] = { name: `${u.kind}-Core ${cpu}`, mhz: leaf(cpu < 8 ? 5400 - cpu * 100 : 4400 + cpu, "MHz"), busy: leaf(cpu * 4, "%"), vid: leaf(0.9 + cpu / 100, "V"), ratio: leaf(50, "x") };
    }),
  );
  return {
    cores,
    temps: { Package: { value: 61, unit: "C", status: "ok", source: "hwmon", cpu: null }, "Core 2": { value: 70, unit: "C", status: "ok", source: "hwmon", cpu: 2 } },
    power: { package: { value: 88.2, unit: "W", status: "ok", source: "powercap" }, core: { value: null, unit: "W", status: "unavailable", reason: "no 'core' subdomain", source: "powercap" } },
    vcore: { value: 1.2, unit: "V", status: "ok", source: "hwmon" },
    dram: { read: { value: null, status: "unavailable", reason: "IMC sampling not enabled", source: "perf stat", unit: "GB/s" }, write: { value: null, status: "unavailable", reason: "IMC sampling not enabled", source: "perf stat", unit: "GB/s" } },
    limit_reasons_raw: { value: 0x10, unit: "raw", status: "ok", source: "msr 0x6B0" },
  };
}

// ---------------------------------------------------------------- spark
test("spark: path data breaks at gaps, draws a lone sample as a dot and nothing for no data", () => {
  assert.equal(sparkPath([], 100, 20), "");
  assert.equal(sparkPath([null, null], 100, 20), "");
  const lone = sparkPath([5], 100, 20);
  assert.match(lone, /^M50\.0,\d+\.\d L50\.0,\d+\.\d$/); // centred, zero-length segment so the round cap shows a dot
  const gap = sparkPath([1, 2, null, 3, 4], 100, 20);
  assert.equal((gap.match(/M/g) || []).length, 2);
  const flat = sparkPath([3, 3, 3], 100, 20);
  assert.ok(!flat.includes("NaN"));
});

// ---------------------------------------------------------------- chart scales
test("chart: niceNum / niceTicks give 1-2-5 ticks that cover the data", () => {
  assert.equal(niceNum(97, false), 100);
  assert.equal(niceNum(0.06, true), 0.05);
  assert.equal(niceNum(0.07, true), 0.1);
  assert.equal(niceNum(0, true), 1);
  const t = niceTicks(3, 97, 4);
  assert.deepEqual(t.ticks, [0, 20, 40, 60, 80, 100]);
  assert.equal(t.lo, 0);
  assert.equal(t.hi, 100);
  const small = niceTicks(1.1, 1.2, 4);
  assert.ok(small.lo <= 1.1 && small.hi >= 1.2);
  assert.ok(small.ticks.every((x) => Number.isFinite(x)));
  const flat = niceTicks(5, 5);
  assert.ok(flat.lo < 5 && flat.hi > 5);
  assert.deepEqual(niceTicks(NaN, 1).ticks, [0, 1]);
  assert.deepEqual(niceTicks(10, 2).ticks[0] <= 2, true); // swapped bounds are handled
});

test("chart: tick values are free of floating point dust", () => {
  const t = niceTicks(0.9, 1.5, 4);
  for (const v of t.ticks) assert.equal(v, Number(v.toFixed(6)));
});

test("chart: padSpan widens a narrow range around its middle (1 degree of noise is not a cliff)", () => {
  assert.deepEqual(padSpan(40, 41, MIN_SPAN.temp), [35.5, 45.5]);
  assert.deepEqual(padSpan(10, 100, 20), [10, 100]);
  assert.deepEqual(padSpan(5, 5, 0), [5, 5]);
});

test("chart: dataRange ignores null/NaN, widens to minSpan and is null with no number", () => {
  assert.equal(dataRange([[null, NaN], []], 5), null);
  assert.deepEqual(dataRange([[null, 70], [72, null]], 0), [70, 72]);
  assert.deepEqual(dataRange([[100]], 20), [90, 110]);
});

test("chart: chartPaths maps time to x and value to a flipped y, and cuts the line at nulls and stalls", () => {
  const opts = { t0: 0, t1: 10, lo: 0, hi: 100, w: 100, h: 50, gapS: 3 };
  const d = chartPaths([{ t: 0, v: 0 }, { t: 10, v: 100 }], { ...opts, gapS: 20 });
  assert.equal(d, "M0.0,50.0 L100.0,0.0");
  const nulls = chartPaths([{ t: 1, v: 10 }, { t: 2, v: null }, { t: 3, v: 20 }], opts);
  assert.equal((nulls.match(/M/g) || []).length, 2);
  const stall = chartPaths([{ t: 1, v: 10 }, { t: 2, v: 10 }, { t: 9, v: 10 }, { t: 10, v: 10 }], opts);
  assert.equal((stall.match(/M/g) || []).length, 2, "a 7 s hole in the stream is a hole in the line");
  assert.equal(chartPaths([], opts), "");
  assert.equal(chartPaths([{ t: 5, v: 50 }], opts), "M50.0,25.0 L50.0,25.0");
  assert.ok(CHART_W > 0 && CHART_H > 0);
});

test("chart: fmtSpan and nearestIndex", () => {
  assert.equal(fmtSpan(30), "30 s");
  assert.equal(fmtSpan(60), "1 min");
  assert.equal(fmtSpan(900), "15 min");
  assert.equal(fmtSpan(7200), "2 h");
  assert.equal(nearestIndex([], 3), -1);
  assert.equal(nearestIndex([1, 2, 3, 4], 2.4), 1);
  assert.equal(nearestIndex([1, 2, 3, 4], 2.6), 2);
  assert.equal(nearestIndex([1, 2, 3, 4], -50), 0);
  assert.equal(nearestIndex([1, 2, 3, 4], 99), 3);
});

// ---------------------------------------------------------------- core map
test("coremap: levelOf buckets into 0..9 and clamps", () => {
  assert.equal(LEVELS, 10);
  assert.equal(levelOf(0, 0, 100), 0);
  assert.equal(levelOf(100, 0, 100), 9);
  assert.equal(levelOf(55, 0, 100), 5);
  assert.equal(levelOf(-5, 0, 100), 0);
  assert.equal(levelOf(500, 0, 100), 9);
  assert.equal(levelOf(null, 0, 100), 0);
  assert.equal(levelOf(5, 10, 10), 0); // degenerate scale
  assert.equal(clamp(5, 0, 3), 3);
});

test("coremap: autoRange spans the data, widens to minSpan, null without data", () => {
  assert.equal(autoRange([null, NaN], 10), null);
  assert.deepEqual(autoRange([60, 70, null], 5), [60, 70]);
  assert.deepEqual(autoRange([65, 65], 10), [60, 70]);
});

test("coremap: rampAt interpolates the ramp ends and flags light colours", () => {
  const r = METRICS.mhz.ramp;
  assert.equal(rampAt(r, 0).hex, r[0]);
  assert.equal(rampAt(r, 1).hex, r[r.length - 1]);
  assert.equal(rampAt(r, 0).light, false);
  assert.equal(rampAt(r, 1).light, true);
  assert.match(rampAt(r, 0.37).hex, /^#[0-9a-f]{6}$/);
  assert.equal(rampAt(r, 7).hex, rampAt(r, 1).hex); // clamped
});

test("coremap: rampCss emits ten level rules per metric and no inline-style tricks", () => {
  const css = rampCss();
  assert.equal((css.match(/\.lv-\d/g) || []).length, 4 * LEVELS);
  for (const k of Object.keys(METRICS)) assert.ok(css.includes(`data-metric="${k}"`));
  assert.ok(!/url\(|@import|expression/.test(css));
});

test("coremap: per-core temperature needs a sensor entry that carries its cpu", () => {
  assert.deepEqual(tempsByCpu(null), {});
  assert.deepEqual(Object.keys(tempsByCpu({ Package: { cpu: null }, a: { cpu: 3, value: 1 }, b: { cpu: "4" }, c: { cpu: "x" } })).sort(), ["3", "4"]);
  const v = liveValues();
  assert.equal(coreReading(v, 2, "temp").value, 70);
  assert.equal(coreReading(v, 3, "temp").status, "unavailable"); // no mapping for cpu 3
  assert.match(coreReading(v, 3, "temp").reason, /per-core temperature mapping/);
  assert.equal(coreReading(v, 1, "mhz").value, 5300);
  assert.equal(coreReading(null, 1, "mhz").status, "unavailable");
  assert.equal(coreReading(v, 99, "busy").status, "unavailable");
});

test("coremap: readingValue keeps unavailable unavailable (no zero, no guess)", () => {
  assert.deepEqual(readingValue({ value: 0, status: "ok", source: "s" }), { value: 0, reason: null, source: "s" }); // a real 0 stays a value
  assert.equal(readingValue({ value: null, status: "unavailable", reason: "no MSR access (needs root)" }).value, null);
  assert.equal(readingValue({ value: 5, status: "unavailable", reason: "x" }).value, null);
  assert.equal(readingValue(undefined).reason, "no reading");
  assert.equal(readingValue({ value: NaN, status: "ok" }).value, null);
});

test("coremap: clock ceiling comes from the live topology, not a constant", () => {
  assert.equal(clockCeiling(units), 5700);
  assert.equal(clockCeiling([{ max_ratio: 62 }]), 6200);
  assert.equal(clockCeiling([]), METRICS.mhz.hi);
});

test("coremap: the raw simulator frame is a map of n/a tiles with the reason, favored stars from preflight", () => {
  const m = buildCoreMap(units, rawFrame.values, "mhz");
  assert.equal(m.tiles.length, 12);
  assert.deepEqual(m.tiles.filter((t) => t.favored).map((t) => t.id), ["P0", "P3"]);
  assert.deepEqual(m.tiles.slice(8).map((t) => t.id), ["EC0", "EC1", "EC2", "EC3"]);
  assert.ok(m.tiles.slice(8).every((t) => t.cells.length === 4));
  assert.equal(m.anyData, false);
  assert.ok(m.tiles.every((t) => t.head.text === "n/a" && t.head.level === null && t.head.reason));
  assert.deepEqual(m.notes, ["no MSR access (needs root)"]);
  assert.equal(m.hi, 5700);
});

test("coremap: no topology yet gives an empty map, never a made-up 8+16 layout (D-036)", () => {
  assert.equal(buildCoreMap(undefined, rawFrame.values).tiles.length, 0);
  assert.equal(buildCoreMap([], null).tiles.length, 0);
});

test("coremap: live clock levels on the fixed scale; E clusters headline with their highest CPU", () => {
  const m = buildCoreMap(units, liveValues(), "mhz", "fixed");
  assert.equal(m.anyData, true);
  const p0 = m.tiles[0];
  assert.equal(p0.cells[0].value, 5400);
  assert.equal(p0.head.text, "5400");
  assert.equal(p0.head.level, levelOf(5400, 400, 5700));
  const ec0 = m.tiles.find((t) => t.id === "EC0");
  assert.equal(ec0.head.value, Math.max(...ec0.cells.map((c) => c.value)));
  assert.deepEqual(ec0.cells.map((c) => c.cpu), [8, 9, 10, 11]);
  assert.deepEqual(m.tiles[1].cells[0].others.map((o) => o.key), ["busy", "vid"]);
  assert.equal(m.notes.length, 0);
});

test("coremap: auto scale stretches over the data so differences show", () => {
  const fixed = buildCoreMap(units, liveValues(), "busy", "fixed");
  const auto = buildCoreMap(units, liveValues(), "busy", "auto");
  assert.equal(fixed.lo, 0);
  assert.equal(fixed.hi, 100);
  assert.equal(auto.lo, 0);
  assert.equal(auto.hi, 92); // cpu 23 -> 92 %
  assert.equal(auto.tiles[0].head.level, 0);
  assert.equal(auto.tiles.find((t) => t.id === "EC3").head.level, LEVELS - 1);
});

test("coremap: temperature is per core only where a sensor names the cpu", () => {
  const m = buildCoreMap(units, liveValues(), "temp");
  const p2 = m.tiles.find((t) => t.id === "P2");
  assert.equal(p2.head.text, "70");
  const p3 = m.tiles.find((t) => t.id === "P3");
  assert.equal(p3.head.text, "n/a");
  assert.match(p3.head.reason, /per-core temperature mapping/);
  assert.equal(m.unit, "°C");
});

// ---------------------------------------------------------------- dashboard models
test("dashboard: tileModel shows value+unit, or n/a + reason with no unit", () => {
  const power = SERIES.find((s) => s.key === "pkg_power");
  assert.deepEqual(
    (({ na, text, unit }) => ({ na, text, unit }))(tileModel(power, { value: 77.84, reason: null, source: "powercap", unit: "W" }, [1, 2])),
    { na: false, text: "77.8", unit: "W" },
  );
  const na = tileModel(power, { value: null, reason: "energy_uj missing", source: "powercap intel-rapl:0/energy_uj", unit: "W" }, [null]);
  assert.equal(na.na, true);
  assert.equal(na.text, "n/a");
  assert.equal(na.unit, "");
  assert.equal(na.reason, "energy_uj missing");
  assert.match(na.title, /n\/a · energy_uj missing/);
  assert.equal(tileModel(power, null).reason, "no telemetry yet");
  assert.equal(tileModel(power, { value: 0, reason: null, source: "", unit: "W" }).text, "0.0"); // 0 W is a reading, not n/a
  assert.equal(tileModel(power, { value: null }).reason, "not reported");
});

test("dashboard: every tile of the raw fixture frame is n/a with its reason (through the store, like the app)", () => {
  const store = createAppStore();
  applyHelperEvent(store, "telemetry", { t: rawFrame.t, values: rawFrame.values });
  const t = store.get().telemetry;
  for (const def of SERIES) {
    const m = tileModel(def, t.samples[def.key], t.history[def.key]);
    assert.equal(m.na, true, def.key);
    assert.ok(m.reason.length > 3, def.key);
  }
  assert.equal(throttleTileModel(t.throttle).na, true);
  assert.match(throttleTileModel(t.throttle).sub, /no MSR access/);
});

test("dashboard: throttling tile for n/a, none and an active group (no hex anywhere)", () => {
  assert.equal(throttleTileModel(null).sub, "n/a · no telemetry yet");
  const mk = (word) => deriveThrottle({ limit_reasons: { status: "ok", value: word } });
  const none = throttleTileModel(mk({ raw: 0, status: [], log: [], unknown_bits: [] }));
  assert.deepEqual([none.tone, none.na, none.sub], ["ok", false, "no limit active"]);
  const turbo = throttleTileModel(mk({ raw: 0x1000, status: ["max_turbo_limit"], log: [], unknown_bits: [] }));
  assert.deepEqual([turbo.tone, turbo.sub], ["ok", "no limit active (all-core turbo cap only)"]); // grey info, never amber
  const hot = throttleTileModel(mk({ raw: 0x402, status: ["thermal", "pl1"], log: [], unknown_bits: [] }));
  assert.equal(hot.tone, "warn");
  assert.match(hot.sub, /Power limit, Thermal/);
  assert.doesNotMatch(JSON.stringify([none.sub, turbo.sub, hot.sub]), /0x[0-9A-F]+/);
});

test("dashboard: the chart buffer takes one entry per sample time and keeps HISTORY_S seconds", () => {
  const store = createAppStore();
  let buf = [];
  const feed = (t, w) => {
    const values = liveValues();
    values.power.package.value = w;
    applyHelperEvent(store, "telemetry", { t, values });
    buf = appendChartSample(buf, store.get().telemetry);
  };
  feed(1000, 50);
  feed(1001, 60);
  feed(1001, 99); // same sample time again: ignored
  assert.deepEqual(buf.map((p) => p.v.pkg_power), [50, 60]);
  assert.equal(buf[0].v.core_power, null); // unavailable stays null in the buffer
  feed(1000 + HISTORY_S - 1, 65);
  assert.equal(buf.length, 3, "inside the window nothing is dropped");
  feed(1001 + HISTORY_S + 5, 70);
  assert.deepEqual(buf.map((p) => p.t), [1001, 1000 + HISTORY_S - 1, 1001 + HISTORY_S + 5].filter((t) => t >= 1001 + HISTORY_S + 5 - HISTORY_S), "samples older than the window are dropped");
  feed(1001 + 3 * HISTORY_S, 71);
  assert.equal(buf.length, 1);
  assert.equal(appendChartSample(buf, { t: null, samples: null }), buf);
});

test("dashboard: chartReasons collects the reason of every unavailable series", () => {
  const store = createAppStore();
  applyHelperEvent(store, "telemetry", { t: 1, values: liveValues() });
  const r = chartReasons(store.get().telemetry);
  assert.equal(r.core_power, "no 'core' subdomain");
  assert.equal(r.pkg_power, undefined);
  assert.equal(r.dram, "IMC sampling not enabled");
  assert.deepEqual(chartReasons(null), {});
});

test("dashboard: opsModel", () => {
  assert.deepEqual(opsModel({ opsUsed: 50, opsBudget: 200, state: "idle" }), { used: 50, budget: 200, fraction: 0.25, usedText: "50", capText: "per-boot cap 200", state: "idle" });
  const none = opsModel({ opsUsed: 0, opsBudget: 0, state: "idle" });
  assert.equal(none.fraction, null);
  assert.equal(none.capText, "per-boot cap n/a");
  assert.equal(opsModel({ opsUsed: 500, opsBudget: 100 }).fraction, 1);
  assert.equal(opsModel(null).usedText, "n/a");
});

test("dashboard: memModel is n/a with the helper's reason, or the decoded card", () => {
  assert.equal(memModel(null).available, false);
  assert.equal(memModel({ available: false, reason: "no IMC found" }).reason, "no IMC found");
  assert.equal(memModel({ available: false }).reason, "the helper gave no reason");
  const m = memModel({
    available: true, mts: 6400, gear: 2, cmd_rate: "2N", timings: { tCL: 32, tRCD: 39, tRP: 39, tRAS: 102, tRFC: 560 }, qclk_mhz: 3200.04, bclk_mhz: 100.0, max_bandwidth_mbs: 102400,
    cross_check: { agree: true }, instances_ok: 2, dimms: [{ locator: "DIMM_A1", size: "16 GB", part: "XYZ", manufacturer: "Acme", temp_c: { value: 41.2, status: "ok" } }, { locator: "DIMM_B1", temp_c: { value: null, status: "unavailable", reason: "no sensor" } }],
  });
  assert.equal(m.headline, "DDR5-6400 · Gear 2 · 2N");
  assert.equal(m.primary, "32-39-39-102");
  assert.deepEqual(m.rows.find((r) => r[0] === "tREFI"), ["tREFI", "n/a"]); // missing timing = n/a, not 0
  assert.deepEqual(m.rows.find((r) => r[0] === "Peak bandwidth"), ["Peak bandwidth", "102.4 GB/s"]);
  assert.equal(m.dimms[0].temp, 41.2);
  assert.equal(m.dimms[1].temp, null);
  assert.equal(m.dimms[1].tempReason, "no sensor");
  assert.equal(m.summary, "2 modules, read from 2 controller instances");
});

test("dashboard: ageText", () => {
  assert.equal(ageText(null, 100), "no samples yet");
  assert.equal(ageText(99.5, 100), "just now");
  assert.equal(ageText(90, 100), "10 s ago");
  assert.equal(ageText(0, 300), "5 min ago");
});

// ---------------------------------------------------------------- system
test("system: sortChecks puts the worst first and is stable", () => {
  const s = sortChecks(pre.checks);
  assert.equal(s[0].name, "msr_allow_writes");
  assert.equal(s[0].status, "unknown");
  assert.equal(s.length, pre.checks.length);
  assert.deepEqual(s.slice(1).map((c) => c.name), pre.checks.filter((c) => c.status === "pass").map((c) => c.name)); // passes keep their order
  const mixed = sortChecks([{ status: "pass", name: "a" }, { status: "refuse", name: "b" }, { status: "weird", name: "c" }, null, { status: "read-only", name: "d" }]);
  assert.deepEqual(mixed.map((c) => c && c.name), ["b", "d", "a", "c", null]);
  assert.deepEqual(sortChecks(undefined), []);
});

test("system: checkState uses only the three accents", () => {
  assert.deepEqual(["pass", "unknown", "read-only", "warn", "fail", "refuse", "zzz"].map(checkState), ["ok", "warn", "warn", "warn", "error", "error", "warn"]);
});

test("system: preflightSummary explains why writes are off", () => {
  const s = preflightSummary(pre);
  assert.equal(s.mode, "write");
  assert.equal(s.writes, false);
  assert.equal(s.platform, "arl-s");
  assert.match(s.banner, /does not permit mailbox requests/);
  assert.deepEqual(s.counts, { pass: 13, unknown: 1 });
  assert.equal(preflightSummary(null), null);
  assert.equal(preflightSummary({ mode: "x" }), null);
  const ro = preflightSummary({ mode: "read-only", checks: [{ name: "lockdown", status: "read-only", reason: "integrity" }] });
  assert.match(ro.banner, /Read-only mode.*lockdown: integrity/);
  assert.equal(preflightSummary({ mode: "write", writes_allowed: true, checks: [] }).banner, "");
  assert.deepEqual(countStatuses(undefined), {});
});

test("system: the limits are the CPU's own (cpu_limits of the kept read), with the register each came from", () => {
  const last = FIXTURES.tuneLast.result;
  const cl = mergeCpuLimits(last.result.cpu_limits);
  const rows = limitRows(cl);
  const row = (what, kind) => rows.find((r) => r.what === what && r.kind === kind);
  assert.deepEqual(row("CPU max voltage", "P-cores"), { what: "CPU max voltage", kind: "P-cores", cpu: "1.625 V", source: "OC mailbox 0x28 [11:0] − 1 LSB" });
  assert.equal(row("OC ratio limit", "P-cores").cpu, "120×");
  assert.equal(row("OC ratio limit", "P-cores").source, "OC mailbox 0x01 [7:0]");
  assert.equal(row("CPU max voltage", "E-clusters").cpu, "1.355 V");
  assert.equal(row("CPU max voltage", "Ring").cpu, "1.999 V", "the ring has its own entry");
  assert.equal(row("V/F offset field", "P, E, ring").cpu, `${MINUS}1000…+999 mV`);
  assert.match(row("Package power limits", "Package").cpu, /^n\/a · Read CPU state first/, "tune.last leaves the power part null: it comes from the power read");
  assert.match(row("Ratio cap range", "All CPUs").cpu, /^n\/a · Read CPU state first/);
  assert.match(row("Uncore ratio range", "Ring").cpu, /^n\/a · Read CPU state first/);
  assert.equal(limitRows(null).length, 0);
  assert.ok(rows.every((r) => !/polic/i.test(JSON.stringify(r))), "no policy anywhere in the table");
});

test("system: the power, cpufreq and uncore limits come from the Power / Ratio caps / Ring & uncore reads when those ran", () => {
  const last = FIXTURES.tuneLast.result;
  const cl = mergeCpuLimits(last.result.cpu_limits, { power: G["power-read"].result.cpu_limits, runtime: G["runtime-read"].result.cpu_limits });
  const rows = limitRows(cl);
  assert.equal(rows.find((r) => r.what === "Package power limits").cpu, "writable, field maximum 4095.875 W");
  assert.equal(rows.find((r) => r.what === "Ratio cap range").cpu, "800–5700 MHz (24 CPUs)");
  assert.equal(rows.find((r) => r.what === "Uncore ratio range").cpu, "8×–40×");
  const locked = mergeCpuLimits(last.result.cpu_limits, { power: { ...G["power-read"].result.cpu_limits, power: { locked: true, pl_max_w: 4095.875, source: "" } } });
  assert.match(limitRows(locked).find((r) => r.what === "Package power limits").cpu, /^locked by firmware/);
});

test("system: a limit that was not read says Read CPU state first (the recorded unread shape)", () => {
  const cl = mergeCpuLimits(G["power-read-unread"].result.cpu_limits);
  const rows = limitRows(cl);
  const v = rows.find((r) => r.what === "CPU max voltage" && r.kind === "P-cores");
  assert.match(v.cpu, /^n\/a · Read CPU state first/);
  assert.match(rows.find((r) => r.what === "OC ratio limit" && r.kind === "E-clusters").cpu, /^n\/a · Read CPU state first/);
  assert.equal(mergeCpuLimits(null), null);
});

test("system: limitsNote", () => {
  assert.match(limitsNote(null, null), /read the ratios and voltages in Tune/);
  assert.match(limitsNote({ available: false }, null), /Not read yet/);
  assert.match(limitsNote({ available: true, result: {} }, null), /did not report the CPU limits/);
  const ok = limitsNote({ available: true, result: {} }, { kinds: {} });
  assert.match(ok, /limits of your CPU/);
  assert.doesNotMatch(ok, /polic/i);
});

test("system: topologyRows from preflight, favored flag and cpu ranges", () => {
  const rows = topologyRows(pre.topology);
  assert.equal(rows.length, 12);
  assert.deepEqual(rows[0], { id: "P0", kind: "P-core", cpus: "0", maxRatio: "57×", perf: "92", favored: true });
  assert.deepEqual(rows[8], { id: "EC0", kind: "E-cluster", cpus: "8–11", maxRatio: "49×", perf: "68", favored: false });
  assert.deepEqual(topologyRows(null), []);
});

test("system: fmtSigned uses a real minus sign", () => {
  assert.equal(fmtSigned(-35), `${MINUS}35`);
  assert.equal(fmtSigned(5), "+5");
  assert.equal(fmtSigned(0), "0");
});

// ---------------------------------------------------------------- audit
test("audit: request names follow registers.md and unknown commands show as hex", () => {
  assert.equal(auditRequest({ cmd: "0x11", p1: "0x00", p2: "0x05" }), "0x11 V/F word write (0x0, 0x5)");
  assert.equal(auditRequest({ cmd: 0x10 }), "0x10 V/F word read");
  assert.equal(auditRequest({ cmd: "0x7f", p1: "0x01" }), "0x7f (0x1, -)");
  assert.equal(auditRequest({ msr: 0x198 }), "MSR 0x198 perf status");
  assert.equal(auditRequest({ msr: "0x150", msr_kind: "write" }), "MSR 0x150 write");
  assert.equal(auditRequest({ event: "refused" }), "");
  assert.equal(MB_NAMES[0x28], "max voltage read");
  assert.equal(hex(255), "0xff");
  assert.equal(hex(null), "");
});

test("audit: the writes filter keeps writes, read-backs, refusals, errors and trips; reads drop out", () => {
  const keep = (o) => auditKeep(o, "writes");
  assert.equal(keep({ event: "result", cmd: "0x11" }), true);
  assert.equal(keep({ event: "attempt", cmd: 0x27 }), true);
  assert.equal(keep({ event: "attempt", msr: 0x150, msr_kind: "write" }), true);
  assert.equal(keep({ event: "readback", cmd: "0x10" }), true);
  for (const e of ["refused", "error", "trip"]) assert.equal(keep({ event: e }), true, e);
  assert.equal(keep({ event: "result", cmd: "0x10" }), false);
  assert.equal(keep({ event: "attempt", cmd: "0x14" }), false);
  assert.equal(auditKeep({ event: "result", cmd: "0x10" }, "all"), true);
});

test("audit: auditRow flags mismatches and non-pass results, and survives an unparsable line", () => {
  const ok = auditRow({ event: "result", cmd: "0x10", rc: 0, rc_name: "Pass", seq: 5, cpu: 0, purpose: "p", ts: "2026-10-18T08:58:27.045+00:00" });
  assert.equal(ok.resultKind, "ok");
  assert.equal(ok.detail, "p");
  assert.equal(ok.seq, "5");
  const bad = auditRow({ event: "result", rc: 3, rc_name: "UNKNOWN_0x11" });
  assert.equal(bad.resultKind, "warn");
  // G-230: rc 0x11 at the end of a V/F read walk is the expected end of the table (grey); the same rc on a write stays amber
  const end = auditRow({ event: "result", cmd: "0x10", p1: "0x02", p2: "0x0A", rc: 17, rc_name: "UNKNOWN_0x11" });
  assert.deepEqual([end.result, end.resultKind], ["end of table (0x11)", ""]);
  const badWrite = auditRow({ event: "result", cmd: "0x11", rc: 17, rc_name: "UNKNOWN_0x11" });
  assert.deepEqual([badWrite.result, badWrite.resultKind], ["UNKNOWN_0x11", "warn"]);
  assert.equal(auditRow({ event: "result", cmd: "0x10", rc: 3, rc_name: "RATIO_LIMIT" }).resultKind, "warn");
  const mm = auditRow({ event: "readback", verified: false, rc_name: "Pass" });
  assert.deepEqual([mm.verdict, mm.resultKind, mm.eventKind], ["MISMATCH", "error", "error"]);
  assert.equal(auditRow({ event: "readback", verified: true, rc_name: "Pass" }).verdict, "verified");
  const refused = auditRow({ event: "refused", reason: "policy: out of range" });
  assert.deepEqual([refused.eventKind, refused.detail], ["error", "policy: out of range"]);
  const junk = auditRow("{not json");
  assert.deepEqual([junk.event, junk.detail], ["unparsable", "{not json"]);
  assert.equal(auditRow({ event: "result", value: 12 }).result, "12");
  assert.equal(auditRow({}).cpu, "");
});

test("audit: G-235 summary records show under Writes & refusals, the detail column prefers o.detail, a run summary names its files", () => {
  const keep = (o) => auditKeep(o, "writes");
  const start = { event: "runtime_attempt", op: "vf.measure", kind: "cpufreq", file_count: 48, detail: "vf.measure: 48 cpufreq file(s) journaled; the caps and the restore follow", seq: 7 };
  const end = { event: "result", op: "vf.measure", kind: "cpufreq", file_count: 48, restored: true, outcome: "complete", detail: "vf.measure: 48 files, restored ok", seq: 8 };
  const bad = { event: "result", op: "stress.start", file_count: 4, restored: false, outcome: "error" };
  const err = { event: "error", op: "vf.measure", path: "cpu3/cpufreq/scaling_max_freq", reason: "Device busy", detail: "cpufreq write cpu3 scaling_max_freq -> 3000000 FAILED: Device busy" };
  for (const o of [start, end, bad, err, { event: "result", op: "profiles.save" }, { event: "powercap_attempt" }]) assert.equal(keep(o), true, o.event + " " + (o.op ?? ""));
  assert.equal(keep({ event: "result", cmd: "0x10" }), false, "a mailbox read result still drops out");
  const row = auditRow(end);
  assert.deepEqual([row.event, row.request, row.result, row.resultKind, row.eventKind, row.detail], ["result", "vf.measure", "complete", "ok", "", "vf.measure: 48 files, restored ok"]);
  assert.equal(auditRow(start).detail, start.detail);
  const failed = auditRow(bad);
  assert.deepEqual([failed.detail, failed.resultKind, failed.eventKind], ["stress.start: 4 files, NOT restored", "error", "error"]);
  assert.equal(auditRow(err).detail, err.detail, "o.detail wins over o.reason");
  assert.equal(auditRow({ event: "refused", reason: "why" }).detail, "why", "reason is the fallback");
  assert.equal(auditRow({ event: "result", op: "curves.delete", curve_id: "x" }).request, "curves.delete");
  assert.equal(auditRows([start, end, { event: "result", cmd: "0x10", rc: 0 }], { filter: "writes" }).length, 2);
});

test("audit: auditRows on the fixture tail: newest first, filters, text search", () => {
  const recs = FIXTURES.auditTail.result.records;
  const all = auditRows(recs);
  assert.equal(all.length, 20);
  assert.deepEqual(all.map((r) => Number(r.seq)), recs.map((r) => r.seq).reverse());
  const writes = auditRows(recs, { filter: "writes" });
  assert.deepEqual(writes.map((r) => r.seq), ["316", "315", "314"]);
  assert.equal(auditRows(recs, { text: "point 5" }).length, 7); // ring point 5 (2), the baseline read (2), the write (2) and its read-back
  assert.equal(auditRows(recs, { text: "  READBACK " }).length, 1);
  assert.equal(auditRows(recs, { text: "zzz" }).length, 0);
  assert.equal(auditRows(recs, { filter: "writes", text: "attempt" }).length, 1);
  assert.deepEqual(auditRows(null), []);
});

test("audit: auditNote and auditTime", () => {
  assert.equal(auditNote(0, 0, { filter: "all", text: "" }, false), "The audit log is empty.");
  assert.equal(auditNote(20, 20, { filter: "all", text: "" }, false), "Last 20 records, newest first, local time.");
  assert.equal(auditNote(200, 3, { filter: "writes", text: "" }, true), "3 write-related of the last 200 records, newest first, local time (older records not shown).");
  assert.match(auditNote(20, 5, { filter: "all", text: "x" }, false), /^5 of the last 20 records match/);
  assert.equal(auditTime("not a date"), "not a date");
  assert.equal(auditTime(undefined), "");
  assert.notEqual(auditTime("2026-10-18T08:58:27.045+00:00"), "2026-10-18T08:58:27.045+00:00");
});

// ---------------------------------------------------------------- hygiene (PLAN §2): the screens carry no inline style/handler/innerHTML/eval
test("the G-206 sources use no innerHTML, inline style, inline handlers, eval or remote URLs", () => {
  const files = ["views/dashboard.js", "views/system.js", "views/audit.js", "components/coremap.js", "components/chart.js", "components/spark.js"];
  for (const f of files) {
    const src = readFileSync(join(UI, f), "utf8");
    assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/.test(src), `${f}: dangerous sink`);
    assert.ok(!/\.style\.|setAttribute\(\s*["']style["']|\bstyle:\s/.test(src.replace(/`[^`]*`/g, "")), `${f}: inline style`); // CSS text in template literals is the constructed stylesheet
    assert.ok(!/https?:\/\//.test(src.replace(/\/\/.*$/gm, "")), `${f}: remote URL`);
    assert.ok(!/\bon(click|input|change)\s*=/.test(src), `${f}: inline handler attribute`);
    assert.ok(!/^import .* from ["']https?:/m.test(src), `${f}: remote import`);
  }
});
