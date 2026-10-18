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

/** options: intervalMs (telemetry period, default 1000), stepMs (gap between replayed progress events, default 12), motion (see invent), helper ('ok' | key of LINK_FAILURES), now,
 *  baseline (curves.* answer the invented baseline; default on, `?baseline=none` turns it off). */
export function createMockTransport({ intervalMs = 1000, stepMs = 12, motion = false, helper = "ok", now = () => Date.now(), baseline = !noBaselineParam() } = {}) {
  const listeners = new Map(); // event -> Set(cb)
  const timers = new Set();
  let link = { state: "connecting", kind: null, message: "" };
  let seq = 0;
  let telemetryTimer = null;
  let tick = 0;
  let busyOp = null;

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
    let delay = 0;
    for (const ev of events) {
      const { event, ...payload } = ev;
      delay += stepMs;
      later(() => emit(event, { ...payload, op_id: id }), delay);
    }
    later(() => {
      busyOp = null;
      emit("state", { op_id: id, state: "idle" });
      emit("result", finalResponse);
    }, delay + stepMs);
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
          return ok(id, clone(tunePlan.response.result)); // always the recorded one-point plan: the mock does not plan
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
          const plan = tunePlan.response.result;
          if (args.plan_id !== plan.plan_id) return fail(id, "PLAN_STALE", "no such plan (the mock only knows the recorded one)");
          if (args.typed !== `CONFIRM ${plan.nonce}`) return fail(id, "CONFIRM_MISMATCH", "the typed confirmation does not match the plan's nonce");
          replayLong(id, tuneApplyEvents, ok(id, clone(tuneApply.response.result)));
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
