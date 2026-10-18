// fixtures/mock-backend-g218.js: the ops the Curves and Stress screens need (G-218), on top of the G-205 mock transport, which answers UNKNOWN_OP for them.
//   const transport = wrapMockTransport(createMockTransport({...}), {seed: "recorded"});   // same interface: connect, status, call, listen, close
// Everything it answers is a recording of the SIMULATOR helper (g218/gen_fixtures_g218.py, see fixtures/README.md): the measurement and the screens are replayed,
// not performed. What is NOT recorded is the user's choice: a measurement always yields the recorded points whatever `units` were asked (the fresh curve gets
// the asked name, a new id and the time of the call), and a stress run replays the recorded 30 s run. INVENTED (not simulator output): the replayed
// stress clock is stretched to the length that was asked for (`elapsed_s` / `of_s` x duration/30), `duration_s` of the result is the asked length, and
// a Stop answers with the recorded cancelled result carrying the elapsed time of the replay so far. The curve store and the stress history are in memory and
// change like the helper's (list / get / update / delete, new entries newest first). Wiring at merge: in mock-backend.js `createMockTransport` return
// `wrapMockTransport(<its object>, options)`; until then the Curves / Stress tests build the wrapper themselves.
// options: stepMs (gap between replayed events, default 12), seed ("none" (empty library, default) | "recorded" (the stock + second curves)), now.
import G from "./g218/fixtures-g218.js";

export const FIXTURES_G218 = G;

const clone = (x) => JSON.parse(JSON.stringify(x));
const ok = (id, result) => ({ id, ok: true, result });
const fail = (id, code, message, detail) => ({ id, ok: false, error: detail ? { code, message, detail } : { code, message } });
const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

const CURVE_ID = /^\d{8}T\d{6}Z-[a-z0-9]{4}$/;
const UNIT = /^(P|EC)\d{1,3}$/;
const unitList = (u) => Array.isArray(u) && u.length >= 1 && u.length <= 256 && new Set(u).size === u.length && u.every((x) => typeof x === "string" && UNIT.test(x));
const nameOk = (n) => typeof n === "string" && n.trim().length >= 1 && n.trim().length <= 64;

/** The argument checks of ops.rs for the ops handled here (vf.measure, stress.start, curves.*): null when fine, else the message. */
export function checkArgs(op, a) {
  const only = (keys) => Object.keys(a).every((k) => keys.includes(k));
  if (op === "vf.measure") {
    if (!only(["mode", "units", "name", "baseline"]) || (a.mode !== undefined && a.mode !== "quick" && a.mode !== "dense")) return "bad vf.measure arguments";
    if ((a.baseline !== undefined && typeof a.baseline !== "boolean") || (a.units !== undefined && !unitList(a.units)) || (a.name !== undefined && !nameOk(a.name))) return "bad vf.measure arguments";
  } else if (op === "stress.start") {
    const int = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
    if (!only(["kind", "method", "units", "ratio_cap", "duration_s", "memory_mb"]) || (a.kind !== "cpu" && a.kind !== "memory") || !int(a.duration_s, 30, 3600)) return "bad stress.start arguments";
    if ((a.method !== undefined && !["integer", "vector-math", "cache"].includes(a.method)) || (a.units !== undefined && !unitList(a.units))) return "bad stress.start arguments";
    if ((a.ratio_cap !== undefined && a.ratio_cap !== null && !int(a.ratio_cap, 8, 60)) || (a.memory_mb !== undefined && !int(a.memory_mb, 256, 1 << 20))) return "bad stress.start arguments";
  } else if (op === "curves.get") {
    if (!only(["id", "units"]) || !CURVE_ID.test(a.id || "") || (a.units !== undefined && !unitList(a.units))) return "bad curves.get arguments";
  } else if (op === "curves.update") {
    if (!only(["id", "name", "baseline"]) || !CURVE_ID.test(a.id || "") || (a.name === undefined && a.baseline === undefined)) return "bad curves.update arguments";
    if ((a.name !== undefined && !nameOk(a.name)) || (a.baseline !== undefined && typeof a.baseline !== "boolean")) return "bad curves.update arguments";
  } else if (op === "curves.delete") {
    if (Object.keys(a).length !== 1 || !CURVE_ID.test(a.id || "")) return "bad curves.delete arguments";
  }
  return null;
}

const isoZ = (ms) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
let idSeq = 0;
/** A store id: UTC stamp + 4 characters of [a-z0-9] (the shape curves.get / stress results use). */
const freshId = (ms) => `${isoZ(ms).replace(/[-:]/g, "")}-m${(idSeq++ % 46656).toString(36).padStart(3, "0")}`;

/** curves.list row of a record. */
function summary(rec) {
  return { id: rec.id, name: rec.name, created: rec.created, mode: rec.mode, complete: rec.complete !== false, tuning_state: clone(rec.tuning_state), units: Object.keys(rec.units), baseline: rec.baseline === true };
}

/** stress.list row of a stress.start result. */
const STRESS_ROW_KEYS = ["id", "kind", "started", "ended", "duration_s", "elapsed_s", "stopped_by", "errors", "cap_valid", "health", "mce_new", "max_temp_c", "max_pkg_w"];
function stressRow(res, args) {
  const row = {};
  for (const k of STRESS_ROW_KEYS) row[k] = res[k] === undefined ? null : res[k];
  row.method = args.kind === "cpu" ? args.method || "integer" : null;
  row.units = args.kind === "cpu" && Array.isArray(args.units) ? args.units.slice() : null;
  row.ratio_cap = args.ratio_cap === undefined ? null : args.ratio_cap;
  row.memory_mb = args.kind === "memory" && args.memory_mb !== undefined ? args.memory_mb : null;
  return row;
}

export function wrapMockTransport(inner, { stepMs = 12, seed = "none", now = () => Date.now() } = {}) {
  const listeners = new Map();
  const timers = new Set();
  const store = new Map(); // curve id -> record
  const history = []; // stress.list rows, newest first
  let seq = 0;
  let running = null; // {id, kind: "measure" | "stress", timers: Set, args, startedMs, finish(kind), lastElapsed}

  if (seed === "recorded") {
    for (const k of ["vf-measure-stock", "vf-measure-dense"]) {
      const rec = clone(G[k].response.result.record);
      store.set(rec.id, rec);
    }
    store.get(G["vf-measure-stock"].response.result.record.id).baseline = true;
    history.push(...clone(G["stress-list"].result.results));
  }

  const emit = (event, payload) => {
    for (const cb of [...(listeners.get(event) || [])]) cb(payload);
  };
  const later = (fn, ms, run) => {
    const t = setTimeout(() => {
      timers.delete(t);
      if (run) run.timers.delete(t);
      fn();
    }, ms);
    if (t.unref) t.unref();
    timers.add(t);
    if (run) run.timers.add(t);
    return t;
  };

  const rows = () => [...store.values()].sort((a, b) => (a.created < b.created ? 1 : a.created > b.created ? -1 : 0)).map(summary);

  function record(op, args, id) {
    // the recorded record, as the asked-for run would have stored it
    const rec = clone(G[op === "dense" ? "vf-measure-dense" : "vf-measure-stock"].response.result.record);
    return Object.assign(rec, { id, name: args.name ? args.name.trim() : rec.name, created: isoZ(now()), mode: args.mode || "quick", baseline: args.baseline === true });
  }

  function replay(run, events, finalFn) {
    let delay = 0;
    for (const ev of events) {
      const { event, ...payload } = ev;
      delay += stepMs;
      later(() => {
        if (event === "progress" && typeof payload.elapsed_s === "number") {
          // the recording is a 30 s run: its clock is stretched to the length that was asked for
          const k = run.scale || 1;
          payload.elapsed_s = Math.round(payload.elapsed_s * k * 10) / 10;
          if (typeof payload.of_s === "number") payload.of_s = Math.round(payload.of_s * k);
          run.lastElapsed = payload.elapsed_s;
        }
        emit(event, { ...payload, op_id: run.id });
      }, delay, run);
    }
    later(() => finalFn(), delay + stepMs, run);
  }

  function end(run, response) {
    if (running !== run) return;
    running = null;
    emit("state", { op_id: run.id, state: "idle" });
    emit("result", response);
  }

  const own = {
    "curves.list": (id) => ok(id, { curves: rows(), no_turbo: false }),
    "curves.get": (id, a) => {
      const rec = store.get(a.id);
      if (!rec) return fail(id, "NOT_FOUND", `no stored curve ${a.id}`);
      const r = clone(rec);
      if (a.units) r.units = Object.fromEntries(Object.entries(r.units).filter(([u]) => a.units.includes(u)));
      return ok(id, r);
    },
    "curves.update": (id, a) => {
      const rec = store.get(a.id);
      if (!rec) return fail(id, "NOT_FOUND", `no stored curve ${a.id}`);
      if (a.name !== undefined) rec.name = a.name.trim();
      if (a.baseline !== undefined) {
        if (a.baseline) for (const o of store.values()) o.baseline = false; // one baseline at a time
        rec.baseline = a.baseline;
      }
      return ok(id, summary(rec));
    },
    "curves.delete": (id, a) => {
      if (!store.delete(a.id)) return fail(id, "NOT_FOUND", `no stored curve ${a.id}`);
      return ok(id, { deleted: a.id });
    },
    "stress.list": (id) => ok(id, { results: clone(history) }),
  };

  return {
    connect: () => inner.connect(),
    status: () => inner.status(),
    async call(op, args = {}) {
      if (own[op]) {
        const bad = checkArgs(op, args);
        const id = `g${++seq}`;
        if (bad) return fail(id, "BAD_REQUEST", bad);
        const st = await inner.status();
        if (st.state !== "connected") return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
        return own[op](id, args);
      }
      if (op === "vf.measure" || op === "stress.start") {
        const id = `g${++seq}`;
        const bad = checkArgs(op, args);
        if (bad) return fail(id, "BAD_REQUEST", bad);
        const st = await inner.status();
        if (st.state !== "connected") return fail(id, "NOT_CONNECTED", "the mock helper is not connected");
        if (running) return fail(id, "BUSY", `the helper is ${running.kind === "measure" ? "measuring" : "stressing"} (${running.id}); conflicting request refused`, { state: running.kind === "measure" ? "measuring" : "stressing", op_id: running.id });
        const run = { id, kind: op === "vf.measure" ? "measure" : "stress", timers: new Set(), args, lastElapsed: 0 };
        running = run;
        if (op === "vf.measure") {
          const rec = record(args.mode, args, freshId(now()));
          const src = G[args.mode === "dense" ? "vf-measure-dense" : "vf-measure-stock"];
          replay(run, src.events, () => {
            store.set(rec.id, rec);
            const problems = src.response.result.problems || [];
            end(run, ok(id, { curve_id: rec.id, record: clone(rec), problems: clone(problems) }));
          });
        } else {
          const src = G[args.kind === "memory" ? "stress-start-memory" : "stress-start"];
          run.scale = args.duration_s / src.request.args.duration_s;
          emit("state", { op_id: id, state: "stressing" });
          replay(run, src.events, () => {
            const res = clone(src.response.result);
            res.id = freshId(now());
            res.started = isoZ(now());
            res.ended = res.started;
            res.duration_s = args.duration_s;
            res.elapsed_s = Math.round(res.elapsed_s * run.scale * 10) / 10;
            history.unshift(stressRow(res, args));
            end(run, ok(id, res));
          });
        }
        return { id };
      }
      if (op === "stress.stop" || (op === "cancel" && running && running.kind === "stress" && (args.op_id === undefined || args.op_id === running.id))) {
        const id = `g${++seq}`;
        if (!running || running.kind !== "stress") return ok(id, clone(G["stress-stop-idle"].response.result));
        const run = running;
        for (const t of run.timers) clearTimeout(t);
        run.timers.clear();
        later(() => {
          const res = clone(G["stress-start-cancelled"].response.result);
          res.id = freshId(now());
          res.started = isoZ(now());
          res.ended = res.started;
          res.duration_s = run.args.duration_s;
          res.elapsed_s = run.lastElapsed;
          history.unshift(stressRow(res, run.args));
          end(run, ok(run.id, res));
        }, stepMs, run);
        return ok(id, { cancelled: true, op_id: run.id, state: "stressing" });
      }
      if (op === "cancel" && running && running.kind === "measure" && (args.op_id === undefined || args.op_id === running.id)) {
        const id = `g${++seq}`;
        const run = running;
        for (const t of run.timers) clearTimeout(t);
        run.timers.clear();
        later(() => {
          const detail = clone(G["vf-measure-cancelled"].response.error.detail);
          const rec = detail.partial;
          rec.id = freshId(now());
          rec.created = isoZ(now());
          rec.name = run.args.name ? run.args.name.trim() : rec.name;
          store.set(rec.id, rec);
          detail.curve_id = rec.id;
          end(run, fail(run.id, "CANCELLED", "the measurement was cancelled", detail));
        }, stepMs, run);
        return ok(id, { cancelled: true, op_id: run.id, state: "measuring" });
      }
      return inner.call(op, args);
    },
    listen(event, cb) {
      let set = listeners.get(event);
      if (!set) listeners.set(event, (set = new Set()));
      set.add(cb);
      const off = inner.listen(event, cb);
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
