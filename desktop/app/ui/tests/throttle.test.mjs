// node --test "desktop/app/ui/tests/*.test.mjs"   (G-229: throttling indicators — the store derivation, chip/popover/event models. No DOM, no helper, no hardware.)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { createAppStore, applyHelperEvent, deriveThrottle, accumulateThrottle, freshThrottleSession, resetThrottle, setThrottleEvents, THROTTLE_GROUPS } from "../store.js";
import { chipModel, popoverModel, eventModel, visibleEvents, fmtSeconds } from "../components/throttle.js";

const rd = (value, source = "msr 0x64F") => ({ value, status: "ok", unit: "", source });
const gone = (reason) => ({ value: null, status: "unavailable", reason, unit: "", source: "msr 0x64F" });
const word = (raw, status = [], log = [], unknown_bits = []) => rd({ raw, status, log, unknown_bits });
const grp = (label, active, duty, bits) => ({ label, active, duty, bits });
const windowOf = (over = {}) => ({
  samples: 10, failed: 0, window_s: 1.02, or_word: 0,
  groups: { power: grp("Power limit", false, 0, []), current: grp("Current / EDP", false, 0, []), thermal: grp("Thermal", false, 0, []), vr_thermal: grp("VR thermal", false, 0, []), max_turbo: grp("Max turbo cap", false, 0, []) },
  bit_duty: {}, info_bits: [], unknown_bits: [], ...over,
});
const withGroups = (changes, bit_duty, extra = {}) => {
  const w = windowOf(extra);
  for (const [k, v] of Object.entries(changes)) w.groups[k] = { ...w.groups[k], ...v };
  w.bit_duty = bit_duty;
  return w;
};

test("groups and labels are exactly the XTU ones, in chip order", () => {
  assert.deepEqual(THROTTLE_GROUPS.map((g) => g.label), ["Power limit", "Current / EDP", "Thermal", "VR thermal", "Max turbo cap"]);
  assert.deepEqual(THROTTLE_GROUPS.map((g) => g.chip), ["Power", "Current/EDP", "Thermal", "VR thermal", "Max turbo"]);
});

test("no telemetry / no MSR access: every group n/a with the reason, never a guessed No", () => {
  const none = deriveThrottle(null);
  assert.equal(none.state, "na");
  assert.equal(none.reason, "no telemetry yet");
  const nr = deriveThrottle({ throttle: gone("no MSR access (needs root)"), limit_reasons: gone("no MSR access (needs root)") });
  assert.equal(nr.state, "na");
  assert.match(nr.reason, /no MSR access/);
  assert.deepEqual(nr.groups.map((g) => chipModel(g).text), ["Power: n/a", "Current/EDP: n/a", "Thermal: n/a", "VR thermal: n/a", "Max turbo: n/a"]);
  assert.equal(nr.alert, false);
});

test("10 Hz window: Yes with the duty, amber for power/current, red for thermal, grey for max turbo", () => {
  const w = withGroups(
    { power: { active: true, duty: 0.3, bits: ["pl1", "pl2"] }, current: { active: true, duty: 0.1, bits: ["other"] }, thermal: { active: true, duty: 1, bits: ["thermal"] }, max_turbo: { active: true, duty: 1, bits: ["max_turbo_limit"] } },
    { pl1: 0.2, pl2: 0.1, other: 0.1, thermal: 1, max_turbo_limit: 1 },
  );
  const m = deriveThrottle({ throttle: rd(w), limit_reasons: word(0x1C02) });
  assert.equal(m.state, "ok");
  const chips = Object.fromEntries(m.groups.map((g) => [g.key, chipModel(g)]));
  assert.deepEqual([chips.power.text, chips.power.state], ["Power: Yes · 30 %", "warn"]);
  assert.deepEqual([chips.current.text, chips.current.state], ["Current/EDP: Yes · 10 %", "warn"]);
  assert.deepEqual([chips.thermal.text, chips.thermal.state], ["Thermal: Yes · 100 %", "error"]);
  assert.deepEqual([chips.vr_thermal.text, chips.vr_thermal.state], ["VR thermal: No", "ok"]);
  assert.deepEqual([chips.max_turbo.text, chips.max_turbo.state], ["Max turbo: Yes · 100 %", "info"]);
  assert.equal(m.alert, true);
});

test("max turbo alone never raises the alert and its No chip is grey too", () => {
  const w = withGroups({ max_turbo: { active: true, duty: 1, bits: ["max_turbo_limit"] } }, { max_turbo_limit: 1 });
  const m = deriveThrottle({ throttle: rd(w) });
  assert.equal(m.alert, false);
  assert.equal(chipModel(m.groups.find((g) => g.key === "max_turbo")).state, "info");
  const idle = deriveThrottle({ throttle: rd(windowOf()) });
  assert.equal(chipModel(idle.groups.find((g) => g.key === "max_turbo")).state, "info");
  assert.equal(chipModel(idle.groups.find((g) => g.key === "power")).state, "ok");
});

test("an older helper (no throttle key): the point-in-time 0x64F status drives the chips, without a duty", () => {
  const m = deriveThrottle({ limit_reasons: word(0x800, ["pl2"], ["pl2", "thermal"]) });
  const power = m.groups.find((g) => g.key === "power");
  assert.deepEqual([power.state, power.duty, chipModel(power).text], ["yes", null, "Power: Yes"]);
  assert.equal(m.samples, 1);
  // the log bits are history, not a current limit: they never light a chip
  const hist = deriveThrottle({ limit_reasons: word(0x1DC20000, [], ["thermal", "pl1"]) });
  assert.equal(hist.alert, false);
});

test("the retired limit_reasons_raw (0x6B0 graphics) can no longer light anything", () => {
  const m = deriveThrottle({ limit_reasons_raw: rd(0x01000000, "msr 0x6B0 (raw)") });
  assert.equal(m.state, "na");
});

test("popover: every active bit with its SDM name, group and source word; unknown bits by number; sticky log flagged", () => {
  const w = withGroups({ power: { active: true, duty: 0.5, bits: ["pl1"] }, thermal: { active: true, duty: 0.2, bits: ["prochot"] }, max_turbo: { active: true, duty: 1, bits: ["max_turbo_limit"] } },
    { pl1: 0.5, prochot: 0.2, max_turbo_limit: 1 }, { unknown_bits: [14], info_bits: ["turbo_transition_attenuation"] });
  const values = {
    throttle: rd(w), limit_reasons: word(0x1401, ["prochot", "max_turbo_limit"], ["thermal", "pl1"]),
    limit_reasons_ring: word(0x400, ["pl1"], [], []), limit_reasons_gfx: word(0x01000000, [], ["other"]),
    pkg_therm_status: word(0x80000000, [], ["thermal"]), kernel_thermal: rd({ core_count: 3, package_count: 1, core_delta: 1, package_delta: 0 }),
  };
  values.limit_reasons_ring.source = "msr 0x6B1 (decoded)";
  const pm = popoverModel(deriveThrottle(values), freshThrottleSession(0));
  assert.equal(pm.state, "ok");
  const lines = pm.active.map((a) => a.line);
  assert.ok(lines.includes("bit 10 · Package power limit PL1 · Power limit · 50 % of samples · 0x64F"), lines.join("|"));
  assert.ok(lines.includes("bit 0 · PROCHOT · Thermal · 20 % of samples · 0x64F"));
  assert.ok(lines.some((l) => l.startsWith("bit 12 · Max turbo limit (multi-core) · Max turbo cap")));
  assert.deepEqual(pm.unknown, ["bit 14 · not named by the SDM"]);
  assert.deepEqual(pm.others, ["0x6B1 Ring limit reasons: Package power limit PL1"]);
  const gfx = pm.sticky.find((s) => s.head.startsWith("0x6B0"));
  assert.match(gfx.note, /cleared by XTU on Windows/);
  assert.match(pm.sticky.find((s) => s.head.startsWith("0x1B1")).note, /kernel clears/);
  assert.deepEqual(pm.words.map((x) => [x.msr, x.rawText]), [["0x64F", "0x1401"], ["0x6B1", "0x400"], ["0x6B0", "0x1000000"], ["0x1B1", "0x80000000"]]);
  assert.match(pm.kernel, /core 3, package 1 \(\+1 this tick\)/);
  assert.match(pm.footer, /10 samples over the last 1\.0 s/);
});

test("session counters add duty x elapsed, skip gaps, and reset clears only the app's counters", () => {
  const w = withGroups({ power: { active: true, duty: 0.5, bits: ["pl1"] } }, { pl1: 0.5 });
  const model = deriveThrottle({ throttle: rd(w) });
  let s = freshThrottleSession(0);
  s = accumulateThrottle(s, model, 100); // first tick: no elapsed time yet
  s = accumulateThrottle(s, model, 101);
  s = accumulateThrottle(s, model, 103);
  assert.equal(s.seconds.power, 1.5);
  s = accumulateThrottle(s, model, 200); // the helper was away: no time is invented
  assert.equal(s.seconds.power, 1.5);
  assert.equal(s.seconds.thermal, 0);
  const store = createAppStore();
  applyHelperEvent(store, "telemetry", { t: 10, values: { throttle: rd(w) } });
  applyHelperEvent(store, "telemetry", { t: 12, values: { throttle: rd(w) } });
  assert.equal(store.get().throttleSession.seconds.power, 1);
  resetThrottle(store, 55);
  assert.deepEqual([store.get().throttleSession.seconds.power, store.get().throttleSession.resetAt], [0, 55]);
  assert.equal(store.get().telemetry.throttle.state, "ok"); // live indicators untouched
});

test("events: model cells, newest first, and the list since the last reset", () => {
  const rec = { group: "power", label: "Power limit", bits: ["pl1", "pl2"], t_on: 1_700_000_000, t_off: 1_700_000_062, dur_ms: 62000, duty: 0.4, peak_temp_c: 91, peak_pkg_w: 253.2, peak_mhz: 5700, kernel_thermal_delta: 0, tuning_id: "accepted-2026-10-15" };
  const m = eventModel(rec);
  assert.deepEqual([m.group, m.bits, m.duration, m.duty, m.tuning], ["Power limit", "pl1, pl2", "1 min 02 s", "40 %", "accepted-2026-10-15"]);
  assert.equal(m.peaks, "91 °C · 253.2 W · 5700 MHz");
  assert.equal(eventModel({ ...rec, tuning_id: null }).tuning, "none recorded");
  assert.equal(eventModel({ unparsable: "xx" }).bad, true);
  const older = { ...rec, t_off: 100 };
  assert.deepEqual(visibleEvents([older, rec], null, false), [rec, older]);
  assert.deepEqual(visibleEvents([older, rec], 1_699_999_999, false), [rec]);
  assert.deepEqual(visibleEvents([older, rec], 1_699_999_999, true), [rec, older]);
  assert.deepEqual(visibleEvents(undefined, null, false), []);
  const store = createAppStore();
  setThrottleEvents(store, { state: "ok", records: [rec] });
  assert.equal(store.get().throttleEvents.records.length, 1);
  assert.equal(fmtSeconds(4.25), "4.3 s");
  assert.equal(fmtSeconds(3725), "1 h 02 min");
});

test("hygiene: the throttle UI never names a write path (no wrmsr, no clear-log op)", () => {
  const here = fileURLToPath(new URL("..", import.meta.url));
  for (const f of ["components/throttle.js", "store.js", "views/dashboard.js", "components/monitor-strip.js"]) {
    const src = readFileSync(join(here, f), "utf8");
    assert.ok(!/wrmsr|clear_log|throttle\.clear|throttle\.reset/.test(src), f);
  }
});
