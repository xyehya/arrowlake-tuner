// fixtures/mock-backend-g217.js: power.* and runtime.* for the Power / Ring & uncore / Ratio caps tabs (G-217), layered on the mock transport like mock-backend-g218.js:
//   createMockTransport() returns wrapMockTransportG217(<the G-218 wrapper of the base mock>, options).
// RECORDED (g217/gen_fixtures_g217.py, the SIMULATOR helper): the shape of power.read / power.plan / power.apply / runtime.read / runtime.plan / runtime.apply answers, the NO_JOURNAL and
// UNCORE_RANGE refusals, the held parking BAD_REQUEST. INVENTED here (marked): the state that makes the answers follow what is asked (the mock CPU's PL1/PL2/tau, caps, uncore range and the boot
// journal) and the plans/results derived from the intent in the recorded shape. `cpu_limits` is RECORDED too (G-226 / D-049: the tune.read, power.read and runtime.read answers of the simulator,
// each with the parts that read fills). The refusals are the CPU rules of D-049 (a value outside what the CPU reports; CPU_LIMIT_UNREAD when the limits were not read); they are shaped like the
// helper's {code: REFUSED, detail: {check, key}}. There is no policy anywhere in these answers.
// Knobs (query string): `cpulimits=unread` (the recorded "not read yet" shape: P / E / ring null, power and cpufreq / uncore parts null: the "Read CPU state first" path), `offline=<cpu>` (that CPU reads offline), `rtapply=mismatch` (the last read-back word differs),
// `locked=1` (MSR 0x610 lock bit set: power is read-only).
import F from "./fixtures.js";
import G from "./g217/fixtures-g217.js";

export const FIXTURES_G217 = G;
const clone = (x) => JSON.parse(JSON.stringify(x));
const ok = (id, result) => ({ id, ok: true, result });
const fail = (id, code, message, detail) => ({ id, ok: false, error: detail ? { code, message, detail } : { code, message } });
const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isNum = (x) => typeof x === "number" && Number.isFinite(x);
const param = (name) => new URLSearchParams(globalThis.location?.search ?? "").get(name);

const PL_FIELD_MAX_W = G["power-read"].result.cpu_limits.power.pl_max_w; // recorded: MSR 0x610 PL1/PL2, 15 bits of 1/8 W (4095.875 W)
const TIME_UNIT_S = 1 / 1024; // MSR 0x606 time unit (recorded: 0.0009765625 s)
const BIN_KHZ = 100000;
const TAU_NAMES = { pl1_w: "constraint_0_power_limit_uw", pl2_w: "constraint_1_power_limit_uw", tau_s: "constraint_0_time_window_us" };
const E_UNREACHABLE = { ratio: 23, capped: 22, khz: 2201449 }; // recorded: an E cluster asked for 23x ended at 22x (2201449 kHz)

/** The window of tau the register can encode: 2^Y x (1 + Z/4) time units, rounded DOWN (the recorded plan: 56 s -> 55.967744 s). */
export function encodeTau(seconds) {
  let best = TIME_UNIT_S;
  for (let y = 0; y < 32; y++) {
    for (let z = 0; z < 4; z++) {
      const v = 2 ** y * (1 + z / 4) * TIME_UNIT_S;
      if (v <= seconds + 1e-12 && v > best) best = v;
    }
  }
  return Math.round(best * 1e6) / 1e6;
}

export function wrapMockTransportG217(inner, options = {}) {
  const { stepMs = 12, now = () => Date.now() } = options;
  const listeners = new Map();
  const timers = new Set();
  let seq = 0;
  const unread = param("cpulimits") === "unread";
  const offlineCpu = param("offline") !== null && param("offline") !== "" ? Number(param("offline")) : null;
  const mismatch = param("rtapply") === "mismatch";
  const locked = param("locked") === "1";

  const emit = (event, payload) => {
    for (const cb of [...(listeners.get(event) || [])]) cb(payload);
  };
  const later = (fn, ms) => {
    const t = setTimeout(() => {
      timers.delete(t);
      fn();
    }, ms);
    if (t.unref) t.unref();
    timers.add(t);
  };

  // ---- the mock CPU's runtime state (INVENTED; starts as the recorded reads) ----
  const rt0 = G["runtime-read"].result;
  const pw0 = G["power-read"].result;
  const state = {
    power: { pl1_w: pw0.powercap.pl1_w, pl2_w: pw0.powercap.pl2_w, tau_s: pw0.powercap.tau_s },
    bootPower: null, // the journal: the values at the first power write of this boot
    capsKhz: Object.fromEntries(rt0.caps.map((r) => [String(r.cpu), r.cpufreq.cap_khz])),
    uncore: { min_ratio: rt0.uncore.min_ratio, max_ratio: rt0.uncore.max_ratio },
    journalRt: null, // {caps: {cpu: khz}, uncore: {min_ratio?, max_ratio?}} first-write values of this boot
  };
  const plans = new Map(); // plan_id -> {kind, plan, change}
  let planSeq = 0;

  // ---- cpu_limits (RECORDED: the helper's own shape, G-226 / D-049; the simulator's numbers) ----
  // Per read, as the helper fills it: tune.read / tune.last carry P / E / ring + the offset field (power, caps_khz, uncore_khz null); power.read adds `power`; runtime.read adds
  // `caps_khz` and `uncore_khz`. `cpulimits=unread` is the shape of a boot without a tune.read (P / E / ring null, "not read yet") and of a power / cpufreq / uncore part that could not be read.
  const UNREAD = G["power-read-unread"].result.cpu_limits;
  function cpuLimits(op) {
    const base = clone(op === "tune" ? F["tune-read"].result.cpu_limits : op === "power" ? G["power-read"].result.cpu_limits : G["runtime-read"].result.cpu_limits);
    if (op === "power") base.power.locked = locked;
    if (unread) {
      for (const k of ["P", "E", "ring"]) base[k] = clone(UNREAD[k]);
      if (op === "power") base.power = { locked: null, pl_max_w: null, source: UNREAD.power.source };
      if (op === "runtime") Object.assign(base, { caps_khz: null, uncore_khz: null });
    }
    return base;
  }
  // The limits the plans check against (what the last runtime / power read carried); null = not read: the plan is refused with CPU_LIMIT_UNREAD.
  const planLimits = (op) => {
    const cl = cpuLimits(op);
    return op === "power" ? (cl.power.pl_max_w === null ? null : cl) : cl.caps_khz === null || cl.uncore_khz === null ? null : cl;
  };
  const decorateRead = (result) => {
    if (isObj(result) && Array.isArray(result.units) && isObj(result.fused)) result.cpu_limits = cpuLimits("tune");
    return result;
  };

  // ---- power ----
  function powerRead() {
    const r = clone(pw0);
    const { pl1_w: a, pl2_w: b, tau_s: c } = state.power;
    Object.assign(r.powercap, { pl1_w: a, pl2_w: b, tau_s: c, pl1_uw: a * 1e6, pl2_uw: b * 1e6, tau_us: Math.round(c * 1e6) });
    Object.assign(r.msr.limit, { pl1_w: a, pl2_w: b, tau_s: c, tau_us: Math.round(c * 1e6), locked });
    r.locked = locked;
    r.writable = !locked;
    r.read_only_reason = locked ? "MSR 0x610 is locked (bit 63): the package power limits cannot be changed until the next reset" : null;
    r.boot = state.bootPower ? { ...state.bootPower, note: "the values at the first power write of this boot (the journal), not necessarily the BIOS setup values" } : null;
    r.cpu_limits = cpuLimits("power");
    return r;
  }

  function powerPlan(id, a) {
    const keys = Object.keys(a);
    if (keys.some((k) => k !== "power" && k !== "revert") || ("power" in a) === ("revert" in a)) return fail(id, "BAD_REQUEST", "power.plan needs exactly one of 'power' ({pl1_w, pl2_w, tau_s}) or 'revert' (true)");
    if ("revert" in a && a.revert !== true) return fail(id, "BAD_REQUEST", "'revert' must be true");
    if (locked) return fail(id, "REFUSED", "MSR 0x610 is locked (bit 63): the package power limits cannot be changed. Nothing was written.", { check: "POWER_LOCKED" });
    if (!planLimits("power")) return fail(id, "REFUSED", "CPU_LIMIT_UNREAD: the CPU's power-limit bounds (MSR 0x610 / 0x606) were not read. Nothing was written.", { check: "CPU_LIMIT_UNREAD" });
    let target;
    const revert = a.revert === true;
    if (revert) {
      if (!state.bootPower) return clone({ ...G["power-revert-nojournal"].response, id });
      target = { ...state.bootPower };
    } else {
      if (!isObj(a.power) || !Object.keys(a.power).length) return fail(id, "BAD_REQUEST", "power must be an object with pl1_w, pl2_w and/or tau_s");
      target = {};
      for (const [k, v] of Object.entries(a.power)) {
        if (!(k in TAU_NAMES)) return fail(id, "BAD_REQUEST", `unknown power key ${k}`);
        if (!isNum(v)) return fail(id, "BAD_REQUEST", `power.${k} must be a number (${k === "tau_s" ? "seconds" : "whole watts"})`);
        if (k !== "tau_s" && !Number.isInteger(v)) return fail(id, "BAD_REQUEST", `power.${k} must be a number (whole watts)`);
        if (v <= 0) return fail(id, "REFUSED", `${k} = ${v} must be above zero. Nothing was written.`, { check: "CPU_LIMIT", key: k });
        if (k !== "tau_s" && v > PL_FIELD_MAX_W) return fail(id, "REFUSED", `${k} = ${v} W is above the largest value the CPU's power-limit field (MSR 0x610, 15 bits of 1/8 W) can hold: ${PL_FIELD_MAX_W} W. Nothing was written.`, { check: "CPU_LIMIT", key: k });
        target[k] = k === "tau_s" ? encodeTau(v) : v;
      }
    }
    const change = Object.entries(target).filter(([k, v]) => Math.abs(state.power[k] - v) > (k === "tau_s" ? 1e-6 : 1e-9));
    if (!change.length) return fail(id, "BAD_REQUEST", "the power intent changes nothing (set pl1_w, pl2_w and/or tau_s)", { check: "NOTHING_TO_DO" });
    const after = { ...state.power, ...Object.fromEntries(change) };
    const base = clone(G["power-plan"].response.result);
    const n = ++planSeq;
    const steps = change.map(([k, v], i) => ({ before: Math.round(state.power[k] * 1e6), expect: [Math.round(v * 1e6), Math.round(v * 1e6)], file: TAU_NAMES[k], key: k, n: i + 1, value: Math.round(v * 1e6) }));
    const warnings = base.warnings.filter((w) => revert ? !/journaled before the first write/.test(w) : true).filter((w) => change.some(([k]) => k === "tau_s") || !/^tau is rounded/.test(w));
    if (after.pl2_w < after.pl1_w) warnings.unshift(`PL2_BELOW_PL1: PL2 (${after.pl2_w} W) would be below PL1 (${after.pl1_w} W); the kernel accepts it, the CPU's behaviour is not documented`); // the helper's "CODE: text" strings (ops_power.py)
    const big = change.filter(([k, v]) => k !== "tau_s" && Math.abs(state.power[k] - v) > 50);
    if (big.length) warnings.unshift(`LARGE_CHANGE: ${big.map(([k, v]) => `${k}: ${state.power[k]} -> ${v} W`).join(", ")} is a large single change (more than 50 W); the CPU accepts it, take a stability and thermal check`);
    const plan = {
      ...base, plan_id: String(n).padStart(8, "0") + "c0ffee".repeat(9).slice(0, 56), nonce: "P" + String(n).padStart(5, "7"), revert, journal_new: !revert && !state.bootPower,
      before: { ...clone(pw0.powercap), pl1_w: state.power.pl1_w, pl2_w: state.power.pl2_w, tau_s: state.power.tau_s }, after: Object.fromEntries(Object.entries(after).map(([k, v]) => [k, v])),
      steps, writes: steps.length, warnings, locked,
    };
    plans.set(plan.plan_id, { kind: "power", plan, change, revert });
    return ok(id, clone(plan));
  }

  function powerApply(id, entry) {
    const { plan, change, revert } = entry;
    const before = { ...state.power };
    if (!revert && !state.bootPower) state.bootPower = { ...before };
    for (const [k, v] of change) state.power[k] = v;
    const res = clone(G["power-apply"].response.result);
    const readback = change.map(([k, v], i) => ({ expected: [Math.round(v * 1e6), Math.round(v * 1e6)], file: Math.round(v * 1e6), key: k, match: !(mismatch && i === change.length - 1), msr: Math.round(v * 1e6) }));
    Object.assign(res, {
      plan_id: plan.plan_id, revert, readback, writes_sent: plan.steps.length, warnings: [],
      steps: plan.steps.map((s) => ({ before: s.before, file: s.file, file_value: s.value, key: s.key, n: s.n, status: "ok", value: s.value })),
      after: { ...clone(pw0.powercap), pl1_w: state.power.pl1_w, pl2_w: state.power.pl2_w, tau_s: state.power.tau_s },
    });
    if (revert) state.bootPower = null; // the journal of the boot is consumed by a revert
    return res;
  }

  // ---- runtime ----
  const rows = () => rt0.caps.map((r) => ({ ...clone(r), online: r.cpu !== offlineCpu }));
  const ratioOf = (khz) => Math.round(khz / BIN_KHZ);
  function runtimeRead() {
    const r = clone(rt0);
    r.caps = rows().map((row) => {
      const khz = state.capsKhz[String(row.cpu)];
      row.cpufreq.cap_khz = khz;
      row.cpufreq.cap_ratio = khz / BIN_KHZ;
      return row;
    });
    r.offline = offlineCpu === null ? [] : [offlineCpu];
    const u = r.uncore;
    Object.assign(u, { min_ratio: state.uncore.min_ratio, max_ratio: state.uncore.max_ratio, min_khz: state.uncore.min_ratio * BIN_KHZ, max_khz: state.uncore.max_ratio * BIN_KHZ });
    r.journal = state.journalRt ? { caps: { ...state.journalRt.caps }, in_flight: false, uncore: { ...(state.journalRt.uncore || {}) } } : null;
    r.cpu_limits = cpuLimits("runtime");
    return r;
  }

  const clusterOf = (cpu) => rt0.caps.filter((r) => r.unit === rt0.caps.find((x) => x.cpu === cpu).unit).map((r) => r.cpu);

  function runtimePlan(id, a) {
    if ("parking" in a) return clone({ ...G["runtime-plan-parking"].response, id });
    const keys = Object.keys(a);
    const revert = a.revert === true;
    if (keys.some((k) => !["caps", "uncore", "revert"].includes(k)) || (revert && keys.length > 1) || !keys.length) return fail(id, "BAD_REQUEST", "runtime.plan needs {revert: true} alone, or caps and/or uncore");
    if (offlineCpu !== null) return fail(id, "REFUSED", `CPU ${offlineCpu} is offline: runtime writes are refused while any CPU is offline. Nothing was written.`, { check: "CPU_OFFLINE" });
    const cl = planLimits("runtime");
    if (!cl) return fail(id, "REFUSED", "CPU_LIMIT_UNREAD: the cpufreq / uncore bounds were not read. Nothing was written.", { check: "CPU_LIMIT_UNREAD" });
    const steps = [];
    const warnings = [];
    const changes = { caps: {}, uncore: {} };
    const stepFor = (domain, key, file, before, value, extra = {}) => steps.push({ before, domain, expect: [value, value], file, key, n: steps.length + 1, value, ...extra });
    if (revert) {
      if (!state.journalRt) return clone({ ...G["runtime-revert-nojournal"].response, id });
      for (const [cpu, khz] of Object.entries(state.journalRt.caps)) if (state.capsKhz[cpu] !== khz) {
        stepFor("caps", "cpu" + cpu, `cpu${cpu}/cpufreq/scaling_max_freq`, state.capsKhz[cpu], khz);
        changes.caps[cpu] = khz;
      }
      for (const [k, ratio] of Object.entries(state.journalRt.uncore || {})) if (state.uncore[k] !== ratio) {
        stepFor("uncore", k, `intel_uncore_frequency/package_00_die_00/${k === "min_ratio" ? "min" : "max"}_freq_khz`, state.uncore[k] * BIN_KHZ, ratio * BIN_KHZ);
        changes.uncore[k] = ratio;
      }
    } else {
      const want = new Map(); // cpu -> ratio (E clusters expanded)
      for (const [k, v] of Object.entries(a.caps || {})) {
        const cpu = Number(k);
        if (!/^[0-9]{1,4}$/.test(k) || !rt0.caps.some((r) => r.cpu === cpu)) return fail(id, "BAD_REQUEST", `caps keys must be CPU numbers of this machine, got ${k}`);
        if (!Number.isInteger(v)) return fail(id, "BAD_REQUEST", `caps.${k} must be a whole ratio`);
        const [lo, hi] = [Math.ceil(cl.caps_khz[k][0] / BIN_KHZ), Math.floor(cl.caps_khz[k][1] / BIN_KHZ)];
        if (v < lo || v > hi) return fail(id, "REFUSED", `cpu${cpu}: ratio ${v}x is outside the cpufreq range ${lo}x..${hi}x the kernel reports for this CPU (cpuinfo_min/max_freq). Nothing was written.`, { check: "CPU_LIMIT", key: "cpu" + cpu });
        for (const c of clusterOf(cpu)) {
          if (want.has(c) && want.get(c) !== v) return fail(id, "REFUSED", `cpu${c}: the cores of one E-cluster run one ratio, but ${want.get(c)}x and ${v}x were asked for. Nothing was written.`, { check: "CLUSTER_CONFLICT", key: "cpu" + c });
          want.set(c, v);
        }
        if (clusterOf(cpu).length > 1 && !warnings.some((w) => /E-cluster runs one ratio/.test(w))) warnings.push(`an E-cluster runs one ratio: the cap was also set on cpu ${clusterOf(cpu).filter((c) => c !== cpu).join(", ")} of the same cluster(s)`);
      }
      for (const [cpu, v] of [...want].sort((x, y) => x[0] - y[0])) {
        const isE = rt0.caps.find((r) => r.cpu === cpu).kind === "E";
        const capped = isE && v === E_UNREACHABLE.ratio;
        const khz = capped ? E_UNREACHABLE.capped * BIN_KHZ : v * BIN_KHZ;
        if (ratioOf(state.capsKhz[String(cpu)]) === (capped ? E_UNREACHABLE.capped : v) && !capped) continue;
        stepFor("caps", "cpu" + cpu, `cpu${cpu}/cpufreq/scaling_max_freq`, state.capsKhz[String(cpu)], khz, { ratio: capped ? E_UNREACHABLE.capped : v, expect: capped ? [2150000, 2250000] : [khz, khz], ...(capped ? { note: `ratio ${v} is unreachable by a cpufreq cap: capped at ${E_UNREACHABLE.capped}` } : {}) });
        changes.caps[String(cpu)] = capped ? E_UNREACHABLE.khz : khz;
      }
      if (steps.some((s) => s.note)) warnings.push("cpu " + steps.filter((s) => s.note).map((s) => s.key.replace("cpu", "")).join(", ") + ": ratio 23 is unreachable by a cpufreq cap: capped at 22");
      const [ulo, uhi] = [Math.ceil(cl.uncore_khz[0] / BIN_KHZ), Math.floor(cl.uncore_khz[1] / BIN_KHZ)];
      const un = { ...state.uncore };
      for (const [k, v] of Object.entries(a.uncore || {})) {
        if (!["min_ratio", "max_ratio"].includes(k)) return fail(id, "BAD_REQUEST", `unknown uncore key ${k}`);
        if (!Number.isInteger(v)) return fail(id, "BAD_REQUEST", `uncore.${k} must be a whole ratio`);
        if (v < ulo || v > uhi) return fail(id, "REFUSED", `${k} = ${v} is outside the hardware's own uncore range ${ulo}..${uhi} (initial_min/max_freq_khz). Nothing was written.`, { check: "UNCORE_RANGE", key: k });
        un[k] = v;
      }
      if (un.min_ratio > un.max_ratio) return fail(id, "REFUSED", `uncore min_ratio ${un.min_ratio} is above max_ratio ${un.max_ratio}. Nothing was written.`, { check: "UNCORE_ORDER", key: "min_ratio" });
      for (const k of ["max_ratio", "min_ratio"]) if (un[k] !== state.uncore[k]) {
        stepFor("uncore", k, `intel_uncore_frequency/package_00_die_00/${k === "min_ratio" ? "min" : "max"}_freq_khz`, state.uncore[k] * BIN_KHZ, un[k] * BIN_KHZ);
        changes.uncore[k] = un[k];
      }
    }
    if (!steps.length) return fail(id, "BAD_REQUEST", "the runtime intent changes nothing", { check: "NOTHING_TO_DO" });
    warnings.push(revert ? "these are runtime values: a reboot restores the firmware / kernel defaults" : "the values now in force are journaled before the first write of each key in this boot; \"revert\" puts them back", "these are runtime values: a reboot restores the firmware / kernel defaults");
    const n = ++planSeq;
    const base = clone(G["runtime-plan"].response.result);
    const plan = { ...base, plan_id: String(n).padStart(8, "0") + "c0ffee".repeat(9).slice(0, 56), nonce: "R" + String(n).padStart(5, "7"), revert, journal_new: !revert && !state.journalRt, steps, writes: steps.length, warnings: [...new Set(warnings)] };
    plans.set(plan.plan_id, { kind: "runtime", plan, changes, revert });
    return ok(id, clone(plan));
  }

  function runtimeApply(id, entry) {
    const { plan, changes, revert } = entry;
    if (!revert) {
      state.journalRt = state.journalRt || { caps: {}, uncore: {} };
      for (const cpu of Object.keys(changes.caps)) if (!(cpu in state.journalRt.caps)) state.journalRt.caps[cpu] = state.capsKhz[cpu];
      for (const k of Object.keys(changes.uncore)) if (!(k in state.journalRt.uncore)) state.journalRt.uncore[k] = state.uncore[k];
    }
    for (const [cpu, khz] of Object.entries(changes.caps)) state.capsKhz[cpu] = khz;
    for (const [k, v] of Object.entries(changes.uncore)) state.uncore[k] = v;
    if (revert) state.journalRt = null;
    const res = clone(G["runtime-apply"].response.result);
    const readback = plan.steps.map((s, i) => ({ domain: s.domain, expected: s.expect, file: s.domain === "caps" ? changes.caps[s.key.replace("cpu", "")] : s.value, key: s.key, match: !(mismatch && i === plan.steps.length - 1) }));
    Object.assign(res, {
      plan_id: plan.plan_id, revert, readback, writes_sent: plan.steps.length, warnings: [],
      steps: plan.steps.map((s) => ({ before: s.before, domain: s.domain, file: s.file, file_value: s.domain === "caps" ? changes.caps[s.key.replace("cpu", "")] : s.value, key: s.key, n: s.n, status: "ok", value: s.value })),
      after: { caps: { ...changes.caps }, uncore: runtimeRead().uncore },
    });
    return res;
  }

  // ---- the long-op replay ----
  function longOp(id, applyFn, events) {
    emit("state", { op_id: id, state: "applying" });
    let delay = 0;
    const total = Math.max(1, events);
    for (let step = 1; step <= total; step++) {
      delay += stepMs;
      later(() => emit("progress", { op_id: id, step, of: total, ops_budget: 100000, ops_used: step }), delay);
    }
    later(() => {
      emit("state", { op_id: id, state: "idle" });
      emit("result", applyFn());
    }, delay + stepMs);
  }

  async function connected() {
    const st = await inner.status();
    return st.state === "connected";
  }

  const handlers = {
    "runtime.read": (id, a) => (Object.keys(a).length ? fail(id, "BAD_REQUEST", "runtime.read takes no arguments") : ok(id, runtimeRead())),
    "runtime.plan": (id, a) => runtimePlan(id, a),
    "power.plan": (id, a) => powerPlan(id, a),
  };

  const applyOp = (kind) => async (id, a) => {
    if (!isObj(a) || Object.keys(a).some((k) => k !== "plan_id" && k !== "typed")) return fail(id, "BAD_REQUEST", "bad apply arguments");
    const entry = plans.get(a.plan_id);
    if (!entry || entry.kind !== kind) return fail(id, "PLAN_STALE", "no such plan (the mock only knows the plans it issued, once each). Nothing was written. Plan again.");
    if (a.typed !== `CONFIRM ${entry.plan.nonce}`) return fail(id, "CONFIRM_MISMATCH", "the typed confirmation does not match the plan's nonce");
    plans.delete(a.plan_id);
    longOp(id, () => ok(id, kind === "power" ? powerApply(id, entry) : runtimeApply(id, entry)), entry.plan.steps.length * 2);
    return { id };
  };

  return {
    connect: () => inner.connect(),
    status: () => inner.status(),
    async call(op, args = {}) {
      if (op === "power.read") {
        const id = `p${++seq}`;
        if (!(await connected())) return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
        later(() => emit("result", ok(id, powerRead())), stepMs);
        return { id };
      }
      if (handlers[op]) {
        const id = `p${++seq}`;
        if (!(await connected())) return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
        return handlers[op](id, args);
      }
      if (op === "power.apply" || op === "runtime.apply") {
        const id = `p${++seq}`;
        if (!(await connected())) return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
        return applyOp(op === "power.apply" ? "power" : "runtime")(id, args);
      }
      const res = await inner.call(op, args);
      if (op === "tune.last" && res && res.ok && res.result && res.result.available && isObj(res.result.result)) decorateRead(res.result.result);
      return res;
    },
    listen(event, cb) {
      let set = listeners.get(event);
      if (!set) listeners.set(event, (set = new Set()));
      // the final result of the base mock's tune.read carries the CPU limits too (G-226: tune.read / tune.last / power.read / runtime.read)
      const wrapped = event === "result" ? (p) => cb(p && p.ok && isObj(p.result) ? { ...p, result: decorateRead(p.result) } : p) : cb;
      set.add(cb);
      const off = inner.listen(event, wrapped);
      return () => {
        set.delete(cb);
        if (typeof off === "function") off();
      };
    },
    close() {
      for (const t of timers) clearTimeout(t);
      timers.clear();
      listeners.clear();
      return inner.close && inner.close();
    },
  };
}
