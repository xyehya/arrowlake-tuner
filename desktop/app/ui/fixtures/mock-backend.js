// fixtures/mock-backend.js: the `mock` transport of bridge.js. Replays the JSON fixtures recorded from the simulator helper
// (gen_fixtures.py; see README.md). Same transport interface as the Tauri backend: connect, status, call, listen (+ close).
// Short ops answer with the recorded response; long ops (tune.read, tune.apply) answer {id}, replay the recorded state/progress events on a timer
// and finish with a `result` event, exactly as PLAN §3.1 describes the real link.
// Loaded lazily by bridge.js only for ?bridge=mock, so the AppImage never parses it. The data comes from fixtures.js (the *.json files bundled as one ES
// module by gen_fixtures.py): Chrome checks JSON module imports and fetch() against connect-src, which the strict CSP restricts to ipc:, whereas a JS
// module is governed by script-src 'self'. So the mock runs under the exact CSP of tauri.conf.json.

import F from "./fixtures.js";

export const FIXTURES = {
  preflight: F["preflight"], telemetryStart: F["telemetry-start"], telemetryStop: F["telemetry-stop"], telemetry: F["telemetry-events"], tuneRead: F["tune-read"],
  tuneReadEvents: F["tune-read-events"], tunePlan: F["tune-plan"], tuneApply: F["tune-apply"], tuneApplyEvents: F["tune-apply-events"], profilesList: F["profiles-list"],
  auditTail: F["audit-tail"], tuneLast: F["tune-last"],
};
const { preflight, telemetryStart, telemetryStop, telemetry, tuneRead, tuneReadEvents, tunePlan, tuneApply, tuneApplyEvents, profilesList, auditTail, tuneLast } = FIXTURES;

const clone = (x) => JSON.parse(JSON.stringify(x));
const ok = (id, result) => ({ id, ok: true, result });
const fail = (id, code, message, detail) => ({ id, ok: false, error: detail ? { code, message, detail } : { code, message } });

const LINK_FAILURES = {
  busy: { state: "busy", kind: "busy", message: "another client is connected to the helper (another arltuner GUI?)" },
  not_installed: { state: "not_installed", kind: "missing", message: "the helper is not installed or not started: /run/arltuner/helper.sock does not exist" },
  not_allowed: { state: "not_allowed", kind: "not_allowed", message: "the helper refused this user: not in the allowlist (/etc/arltuner/helper.toml)" },
  error: { state: "error", kind: "closed", message: "the helper closed the connection right away" },
};

// INVENTED, not from any hardware and not from the simulator: with `motion` on, the telemetry leaves that the unprivileged simulator reports as
// "unavailable" get a deterministic waveform so sparklines and tiles can be seen moving. DRAM and core power stay unavailable on purpose (n/a path).
function invent(values, tick) {
  const v = clone(values);
  const put = (leaf, value) => {
    leaf.value = Math.round(value * 1000) / 1000;
    leaf.status = "ok";
    delete leaf.reason;
  };
  for (const [cpu, core] of Object.entries(v.cores || {})) {
    const n = Number(cpu);
    const busy = 50 + 45 * Math.sin(tick * 0.7 + n * 0.9);
    const ratio = Math.round((n < 8 ? 55 : 46) - 3 * (1 - busy / 100));
    put(core.busy, busy);
    put(core.ratio, ratio);
    put(core.mhz, ratio * 100);
    put(core.vid, 0.95 + 0.0035 * busy);
  }
  if (v.temps && v.temps.Package) put(v.temps.Package, 48 + 10 * Math.sin(tick * 0.5));
  if (v.power && v.power.package) put(v.power.package, 85 + 30 * Math.sin(tick * 0.6));
  if (v.vcore) put(v.vcore, 1.15 + 0.05 * Math.sin(tick * 0.4));
  if (v.limit_reasons_raw) put(v.limit_reasons_raw, tick % 8 === 5 ? 0x10 : 0);
  return v;
}

// INVENTED, not from any hardware and not from the simulator (the simulator's curve store is empty): a stock-baseline curve for the Tune predictions (G-208).
// Per unit a straight ramp that ends at the fused top voltage of the recorded read (0.0105 V per ratio step), `status: "ok"` on every read point. It is what
// `curves.list` / `curves.get` answer in the mock; `?bridge=mock&baseline=none` answers an empty library (the "no baseline curve" path).
function inventedBaseline() {
  const units = {};
  for (const u of tuneRead.result.units) {
    const cpu = u.cpus[0];
    const top = tuneRead.result.fused[String(cpu.cpu)];
    const ratios = cpu.points.map((p) => p.ratio);
    const topRatio = top ? top.ratio : Math.max(...ratios);
    const topV = top ? top.v : 1.1;
    units[u.id] = { points: ratios.map((r) => ({ ratio: r, status: "ok", vid_v: Math.round((topV - (topRatio - r) * 0.0105) * 10000) / 10000 })) };
  }
  return { id: "mock-baseline", name: "Mock stock baseline (invented)", baseline: true, created: "2026-01-01T00:00:00Z", units };
}
const noBaselineParam = () => new URLSearchParams(globalThis.location?.search ?? "").get("baseline") === "none";

// INVENTED, not from any hardware and not from the simulator (G-210): the mock plans from the intent so the apply drawer has something to show. It is the recorded plan
// (warnings, ops left, expiry) with `steps` / `before` / `after` / `units` derived from the intent rows and the recorded read, in the helper's step order (the conditional
// per-core enable first, then one write per point). Knobs (query string, like `baseline=none`): `apply=mismatch` (the last read-back word differs), `apply=stale` (the first
// tune.apply answers PLAN_STALE and the plan is forgotten), `step=<ms>` (gap between replayed progress events; a slow apply for the cancel test).
const searchParam = (name) => new URLSearchParams(globalThis.location?.search ?? "").get(name);
const stepParam = () => {
  const n = Number(searchParam("step"));
  return Number.isFinite(n) && n >= 1 ? n : null;
};
const POLICY_FLOOR_MV = -150; // tighter than the editor's own -200 clamp, so a refusal is reachable from the UI
const POLICY_CEIL_MV = 150;
const rawOf = (mv) => Math.round(mv * 1.024);
const mvOfRaw = (raw) => Math.round((raw / 1.024) * 1000) / 1000;
const wordOf = (mv, ratio) => "0x" + ((((rawOf(mv) & 0x7ff) << 21) | ratio) >>> 0).toString(16).padStart(8, "0");
const readUnit = (id) => tuneRead.result.units.find((u) => u.id === id) || null;
const signed = (n) => (n >= 0 ? "+" : "-") + Math.abs(n);
const outside = (mv) => mv < POLICY_FLOOR_MV || mv > POLICY_CEIL_MV;
const WINDOW = `${POLICY_FLOOR_MV}.0..+${POLICY_CEIL_MV}.0 mV`;

/** The helper's POLICY_RANGE message (ops_tune.py _check_range shape) for the first offset of the intent outside the mock policy, else null. */
function mockRefusal(intent) {
  for (const row of intent.rows || []) {
    const o = row.offset;
    if (!o) continue;
    for (const mv of o.kind === "curve" ? [o.mv] : Object.values(o.mv || {})) {
      if (outside(mv)) {
        return `${row.target}: offset ${signed(mv)} mV (raw ${rawOf(mv)}) is outside the policy window ${WINDOW} (policy effective_offset_floor_mv = ${POLICY_FLOOR_MV}, offset_ceiling_mv = ${POLICY_CEIL_MV}; mock policy; the field encoding allows -1000..999 mV). Nothing was sent.`;
      }
    }
  }
  for (const [no, mv] of Object.entries((intent.ring && intent.ring.points) || {})) {
    if (outside(mv)) return `ring point ${no}: offset ${signed(mv)} mV (raw ${rawOf(mv)}) is outside the policy window ${WINDOW} (mock policy). Nothing was sent.`;
  }
  return null;
}

/** intent -> {steps, before, after, units, writes}; `writes` 0 = nothing to synthesize from (the caller answers the recorded plan). */
function synthPlan(intent) {
  const steps = [{ conditional: true, kind: "write_percore_enable", n: 1 }];
  const before = {};
  const after = {};
  const units = [];
  const add = (unit, point, ratio, fromMv, toMv, cpu) => {
    (before[unit] ||= {})[point] = { clamped_by: null, effective_v: null, offset_mv: fromMv, ratio, v_target_v: null };
    (after[unit] ||= {})[point] = { clamped_by: null, effective_v: null, offset_mv: mvOfRaw(rawOf(toMv)), ratio, v_target_v: null };
    if (!units.includes(unit)) units.push(unit);
    steps.push({ cpu, kind: "write_vf_point", n: steps.length + 1, offset_mv: toMv, point, unit });
  };
  for (const row of intent.rows || []) {
    const u = readUnit(row.target);
    if (!u || !row.offset) continue;
    const cpu = u.cpus[0];
    if (row.offset.kind === "curve") {
      for (const p of cpu.points) add(row.target, p.point, p.ratio, p.offset_mv, row.offset.mv, cpu.cpu);
    } else {
      for (const [ratio, mv] of Object.entries(row.offset.mv || {})) {
        const p = cpu.points.find((q) => q.ratio === Number(ratio));
        add(row.target, p ? p.point : 1, Number(ratio), p ? p.offset_mv : 0, mv, cpu.cpu);
      }
    }
  }
  for (const [no, mv] of Object.entries((intent.ring && intent.ring.points) || {})) add("ring", Number(no), null, 0, mv, 0);
  for (const kind of Object.keys(intent.turbo || {})) {
    steps.push({ kind: "write_msr", n: steps.length + 1, table: kind, unit: "turbo " + kind });
    units.push("turbo " + kind);
  }
  return { steps, before, after, units, writes: steps.length - 1 };
}

/** options: intervalMs (telemetry period, default 1000), stepMs (gap between replayed progress events, default 12; `?step=<ms>`), motion (see invent), helper ('ok' | key of LINK_FAILURES), now,
 *  baseline (curves.* answer the invented baseline; default on, `?baseline=none` turns it off), applyMode ('mismatch' | 'stale' | null; `?apply=`; see synthPlan above). */
function createBaseMockTransport({ intervalMs = 1000, stepMs = stepParam() ?? 12, motion = false, helper = "ok", now = () => Date.now(), baseline = !noBaselineParam(), applyMode = searchParam("apply") } = {}) {
  const listeners = new Map(); // event -> Set(cb)
  const timers = new Set();
  let link = { state: "connecting", kind: null, message: "" };
  let seq = 0;
  let telemetryTimer = null;
  let tick = 0;
  let busyOp = null;
  let running = null; // the replay of the long op in flight ({id, cancelled, done, sent})

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
    return t;
  };
  const connected = () => link.state === "connected";

  function startTelemetry(id) {
    if (telemetryTimer) clearInterval(telemetryTimer);
    const frames = telemetry.events.filter((e) => e.event === "telemetry");
    const beats = telemetry.events.filter((e) => e.event === "heartbeat");
    telemetryTimer = setInterval(() => {
      const i = tick++;
      const src = frames[i % frames.length];
      const values = motion ? invent(src.values, i) : clone(src.values);
      values.ts = now() / 1000;
      emit("telemetry", { op_id: id, t: now() / 1000, values });
      if (i % 2 === 0 && beats.length) emit("heartbeat", { ...clone(beats[(i / 2) % beats.length | 0]), op_id: busyOp });
    }, intervalMs);
    if (telemetryTimer.unref) telemetryTimer.unref();
  }

  /** Replay recorded state/progress events for a long op, then the final `result`. Resolves nothing; the caller already answered {id}. */
  function replayLong(id, events, finalResponse) {
    busyOp = id;
    const run = { id, cancelled: false, done: false, sent: 0 };
    running = run;
    const finish = (response) => {
      if (run.done) return;
      run.done = true;
      busyOp = null;
      running = null;
      emit("state", { op_id: id, state: "idle" });
      emit("result", response);
    };
    // `cancel` takes effect at the next boundary between events, like the helper's between-transactions check (G-210)
    const cancelled = () => finish(fail(id, "CANCELLED", `cancelled between transactions after ${run.sent} progress step(s); the requests already sent are in the audit log`));
    let delay = 0;
    for (const ev of events) {
      const { event, ...payload } = ev;
      delay += stepMs;
      later(() => {
        if (run.done) return;
        if (run.cancelled) return cancelled();
        if (event === "progress") run.sent++;
        emit(event, { ...payload, op_id: id });
      }, delay);
    }
    later(() => (run.cancelled ? cancelled() : finish(finalResponse)), delay + stepMs);
  }

  // ---- plans (G-210): the recorded one plus the ones synthesized from an intent; a plan is single use and `apply=stale` forgets them all once ----
  const plans = new Map(); // plan_id -> plan result
  let planSeq = 0;
  let staleFired = false;
  let recordedGone = false; // the recorded plan was applied or forgotten: the next plan of an empty intent gets a fresh id
  const nonceOf = (n) => "M" + n.toString(8).padStart(5, "0").replace(/\d/g, (d) => String(Number(d) + 2));
  function issuePlan(intent) {
    const synth = synthPlan(intent);
    const recorded = tunePlan.response.result;
    if (!synth.writes && !recordedGone) {
      if (!plans.has(recorded.plan_id)) plans.set(recorded.plan_id, clone(recorded)); // the recorded one-point plan
      return plans.get(recorded.plan_id);
    }
    const n = ++planSeq;
    const plan = clone(recorded);
    plan.plan_id = String(n).padStart(8, "0") + "c0ffee".repeat(9).slice(0, 56);
    plan.nonce = nonceOf(n);
    plan.name = "tune-mock" + n;
    if (synth.writes) {
      const writes = synth.steps.length;
      Object.assign(plan, { steps: synth.steps, before: synth.before, after: synth.after, units: synth.units, writes, reads: 2, ops_needed: writes * 2 + 2, rounds: 1 });
      // INVENTED prediction (the recorded plan has none): P0 sets the maximum
      plan.vccia = { predicted_v: 1.28, label: "prediction, not a measurement", set_by: { unit: "P0", ratio: 57 }, active_cores: "all", margin_v: 0.05, before_v: 1.29 };
      const mvs = synth.steps.map((s) => s.offset_mv).filter((x) => typeof x === "number");
      if (mvs.some((x) => x > 0)) plan.warnings.unshift({ code: "POSITIVE_OFFSET", text: "a positive offset raises the voltage of the point: more heat, not more stability headroom" });
      if ((intent.rows || []).some((r) => r.target.startsWith("EC") && r.offset && r.offset.kind === "points" && "40" in r.offset.mv)) plan.warnings.unshift({ code: "E40_OFFSET", text: "an offset on the E-cluster 40x point has been reported to destabilise the cluster [COMMUNITY]" });
    }
    plans.set(plan.plan_id, plan);
    return plan;
  }
  /** The final response and progress events of an apply of `plan` (the recorded ones for the recorded plan). */
  function applyOutcome(id, plan) {
    if (plan.plan_id === tunePlan.response.result.plan_id) return { events: tuneApplyEvents, response: ok(id, clone(tuneApply.response.result)) };
    const done = plan.steps.filter((s) => !s.conditional);
    const readback = done.map((s) => {
      const mv = s.offset_mv;
      const ratio = plan.after[s.unit]?.[s.point]?.ratio ?? 0;
      const word = s.kind === "write_msr" ? "0x" + (0x3a3a3a3a).toString(16) : wordOf(mv, ratio ?? 0);
      return { actual: word, cpu: s.cpu ?? 0, expected: word, match: true, point: s.kind === "write_msr" ? "turbo" : s.point, unit: s.table || s.unit };
    });
    if (applyMode === "mismatch" && readback.length) {
      const last = readback[readback.length - 1];
      last.match = false;
      last.actual = "0x" + ((parseInt(last.expected, 16) ^ 0x20) >>> 0).toString(16).padStart(8, "0");
    }
    const result = {
      ...clone(tuneApply.response.result),
      plan_id: plan.plan_id,
      readback,
      steps: done.map((s) => ({ cpu: s.cpu ?? 0, kind: s.kind, n: s.n, point: s.point ?? "turbo", status: "ok", unit: s.unit })),
      ops_used: plan.ops_needed,
      writes_sent: done.length,
    };
    const events = [{ event: "state", state: "applying" }];
    for (let step = 1; step <= plan.ops_needed; step++) events.push({ event: "progress", of: plan.ops_needed, ops_budget: 100000, ops_used: 152 + step, step });
    return { events, response: ok(id, result) };
  }

  return {
    async connect() {
      const fail_ = LINK_FAILURES[helper];
      link = fail_ ? { ...fail_ } : { state: "connected", kind: null, message: "simulated helper (mock fixtures, no hardware)" };
      return link;
    },
    async status() {
      return link;
    },
    async call(op, args = {}) {
      const id = `m${++seq}`;
      if (!connected()) return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
      switch (op) {
        case "preflight":
          return ok(id, clone(preflight.result));
        case "telemetry.start":
          startTelemetry(id);
          return ok(id, { ...clone(telemetryStart.result), op_id: id, interval_s: intervalMs / 1000 });
        case "telemetry.stop":
          if (telemetryTimer) clearInterval(telemetryTimer);
          telemetryTimer = null;
          return ok(id, { ...clone(telemetryStop.result) });
        case "tune.last": {
          const r = clone(tuneLast.result);
          if (r.available) r.at = now() / 1000 - 60; // pretend the recorded read happened a minute ago
          return ok(id, r);
        }
        case "curves.list":
          return ok(id, { curves: baseline ? [{ id: "mock-baseline", name: "Mock stock baseline (invented)", baseline: true, created: "2026-01-01T00:00:00Z" }] : [], no_turbo: false });
        case "curves.get":
          if (!baseline || args.id !== "mock-baseline") return fail(id, "NOT_FOUND", "no such curve in the mock");
          return ok(id, inventedBaseline());
        case "profiles.list":
          return ok(id, clone(profilesList.result));
        case "audit.tail":
          return ok(id, clone(auditTail.result));
        case "tune.plan": {
          if (!args.intent || typeof args.intent !== "object") return fail(id, "BAD_REQUEST", "tune.plan needs {intent: {...}}");
          const refused = mockRefusal(args.intent);
          if (refused) return fail(id, "REFUSED", refused, { check: "POLICY_RANGE" });
          return ok(id, clone(issuePlan(args.intent))); // an empty intent answers the recorded one-point plan; anything else is synthesized (synthPlan)
        }
        case "cancel": {
          if (args.op_id !== undefined && args.op_id !== null && args.op_id !== busyOp) return fail(id, "BAD_REQUEST", `${JSON.stringify(args.op_id)} is not the running operation`, { state: busyOp ? "applying" : "idle", op_id: busyOp });
          if (!running) return ok(id, { cancelled: false, state: "idle" });
          running.cancelled = true;
          return ok(id, { cancelled: true, op_id: running.id, state: "applying" });
        }
        case "tune.read": {
          if (busyOp) return fail(id, "BUSY", "another long op is running", { state: "applying", op_id: busyOp });
          const result = clone(tuneRead.result);
          result.read_at = new Date(now()).toISOString().replace(/\.\d+Z$/, "Z");
          replayLong(id, tuneReadEvents, ok(id, result));
          return { id };
        }
        case "tune.apply": {
          if (busyOp) return fail(id, "BUSY", "another long op is running", { state: "applying", op_id: busyOp });
          const plan = plans.get(args.plan_id);
          if (!plan) return fail(id, "PLAN_STALE", "no such plan (the mock only knows the plans it issued, once each)");
          if (applyMode === "stale" && !staleFired) {
            staleFired = true;
            recordedGone = true;
            plans.clear();
            return fail(id, "PLAN_STALE", "a word changed since this plan was made, or the plan is gone. Nothing was written. Plan again.");
          }
          if (args.typed !== `CONFIRM ${plan.nonce}`) return fail(id, "CONFIRM_MISMATCH", "the typed confirmation does not match the plan's nonce");
          plans.delete(plan.plan_id); // single use
          if (plan.plan_id === tunePlan.response.result.plan_id) recordedGone = true;
          const outcome = applyOutcome(id, plan);
          replayLong(id, outcome.events, outcome.response);
          return { id };
        }
        default:
          return fail(id, "UNKNOWN_OP", `the mock bridge has no fixture for ${op}`);
      }
    },
    listen(event, cb) {
      let set = listeners.get(event);
      if (!set) listeners.set(event, (set = new Set()));
      set.add(cb);
      return () => set.delete(cb);
    },
    close() {
      if (telemetryTimer) clearInterval(telemetryTimer);
      telemetryTimer = null;
      for (const t of timers) clearTimeout(t);
      timers.clear();
      listeners.clear();
    },
  };
}

// G-218: Curves/Stress fixtures and run simulation are layered on top of the base transport.
import { wrapMockTransport } from "./mock-backend-g218.js";
export function createMockTransport(opts = {}) {
  // the recorded simulator curves seed the library (incl. a baseline for Tune predictions) unless ?nobaseline asks for none
  const seed = opts.seed ?? (noBaselineParam() ? "none" : "recorded");
  return wrapMockTransport(createBaseMockTransport(opts), { ...opts, seed });
}
