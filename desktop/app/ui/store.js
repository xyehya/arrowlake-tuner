// store.js: the single state object + subscribe (PLAN §3.2), and the pure functions that turn helper data into state.
// No DOM, no Tauri: runs unchanged under `node --test`. Views read state and call the actions below; they never mutate it.

export const HISTORY_CAP = 120; // samples kept per monitoring series (~2 min at the 1 s default interval)

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  const store = {
    get: () => state,
    set(patch) {
      return store.update(() => patch);
    },
    update(fn) {
      const patch = fn(state);
      if (!patch) return state;
      const prev = state;
      state = { ...state, ...patch };
      for (const cb of [...subs]) cb(state, prev);
      return state;
    },
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
    /** Call `cb(value, state)` now and whenever `selector(state)` changes (Object.is). Returns an unsubscribe function. */
    watch(selector, cb) {
      let last = selector(state);
      cb(last, state);
      return store.subscribe((s) => {
        const next = selector(s);
        if (!Object.is(next, last)) {
          last = next;
          cb(next, s);
        }
      });
    },
  };
  return store;
}

// ---- monitoring series (PLAN §4.1: package power, highest clock, hottest sensor, Vcore, core power, DRAM GB/s) ----
// Each `pick` takes the helper's telemetry `values` and returns {value|null, reason|null, source, unit}. Unavailable stays unavailable: no guessing.
const NO_DATA = { value: null, reason: "not reported", source: "", unit: "" };

function leaf(r, unit = "") {
  if (!r || typeof r !== "object") return { ...NO_DATA, unit };
  if (r.value === null || r.value === undefined || r.status === "unavailable") {
    return { value: null, reason: r.reason || "unavailable", source: r.source || "", unit: r.unit || unit };
  }
  return { value: Number(r.value), reason: null, source: r.source || "", unit: r.unit || unit };
}

function maxOf(readings, unit, what) {
  const ok = readings.filter((r) => r.value !== null);
  if (ok.length === 0) {
    const first = readings[0] || { ...NO_DATA, unit };
    return { value: null, reason: first.reason || `no ${what}`, source: first.source, unit };
  }
  return { value: Math.max(...ok.map((r) => r.value)), reason: null, source: ok[0].source, unit };
}

export const SERIES = [
  { key: "pkg_power", label: "Package power", unit: "W", digits: 1, pick: (v) => leaf(v?.power?.package, "W") },
  { key: "clock", label: "Highest clock", unit: "MHz", digits: 0, pick: (v) => maxOf(Object.values(v?.cores || {}).map((c) => leaf(c?.mhz, "MHz")), "MHz", "core clock") },
  { key: "temp", label: "Hottest sensor", unit: "°C", digits: 0, pick: (v) => maxOf(Object.values(v?.temps || {}).map((t) => leaf(t, "C")), "°C", "temperature sensor") },
  { key: "vcore", label: "Vcore", unit: "V", digits: 3, pick: (v) => leaf(v?.vcore, "V") },
  { key: "core_power", label: "Core power", unit: "W", digits: 1, pick: (v) => leaf(v?.power?.core, "W") },
  {
    key: "dram",
    label: "DRAM",
    unit: "GB/s",
    digits: 1,
    pick: (v) => {
      const r = leaf(v?.dram?.read, "GB/s");
      const w = leaf(v?.dram?.write, "GB/s");
      if (r.value === null && w.value === null) return r;
      return { value: (r.value ?? 0) + (w.value ?? 0), reason: null, source: r.source || w.source, unit: "GB/s" };
    },
  },
];

export function seriesByKey(key) {
  return SERIES.find((s) => s.key === key);
}

/** limit_reasons_raw -> {state:'na'|'none'|'raw', raw, reason}. Named badges arrive with the 0x64F/0x6B0 decode (PLAN §6.1); until then the raw word. */
export function limitBadges(values) {
  const r = values?.limit_reasons_raw;
  if (!r || r.value === null || r.value === undefined || r.status === "unavailable") {
    return { state: "na", raw: null, reason: (r && r.reason) || "not reported", source: (r && r.source) || "" };
  }
  const raw = Number(r.value);
  return { state: raw === 0 ? "none" : "raw", raw, reason: null, source: r.source || "" };
}

export function pushSample(history, samples, cap = HISTORY_CAP) {
  const next = {};
  for (const s of SERIES) {
    const arr = (history[s.key] || []).slice(-(cap - 1));
    arr.push(samples[s.key].value);
    next[s.key] = arr;
  }
  return next;
}

// ---- derived header state ----
/** Mode chip from a preflight result: writes allowed / read-only + the first reason (PLAN §4.1). */
export function deriveMode(pre) {
  if (!pre) return { writes: false, label: "mode unknown", reason: "preflight not read yet" };
  if (pre.writes_allowed === true) return { writes: true, label: "writes allowed", reason: "" };
  const checks = Array.isArray(pre.checks) ? pre.checks : [];
  const bad = checks.find((c) => c.status === "fail") || checks.find((c) => c.status === "unknown");
  const reason = bad ? `${bad.name}: ${bad.reason}` : pre.mode === "write" ? "the helper has not confirmed write access" : `mode ${pre.mode || "unknown"}`;
  return { writes: false, label: "read-only", reason };
}

/** Applied-vs-accepted chip from `accepted_match` of a tune.read / vf.dump result ([{cpu, match, equivalent?, diffs}] or null). */
export function deriveAccepted(match, note = "") {
  if (!Array.isArray(match)) return { kind: "unknown", n: 0, note: note || "no accepted state to compare with" };
  const differing = match.filter((m) => m && m.match === false && !m.equivalent);
  if (differing.length) return { kind: "differs", n: differing.length, note: `${differing.length} unit(s) differ from the accepted state` };
  if (match.some((m) => m && m.equivalent)) return { kind: "equivalent", n: 0, note: "equal after the hardware's own V(n) >= V(n-1) rule" };
  return { kind: "matches", n: 0, note: "every read-back value equals the accepted state" };
}

export function initialState() {
  return {
    screen: "dashboard",
    link: { state: "connecting", kind: null, message: "connecting to the helper", fix: null },
    preflight: null,
    mode: deriveMode(null),
    accepted: deriveAccepted(null),
    lastReadAt: null, // ISO string of the last tune.read / tune.last, or null (no read this boot)
    helperOp: { state: "idle", opId: null, opsUsed: 0, opsBudget: 0 },
    telemetry: { values: null, t: null, samples: null, history: {}, limits: { state: "na", raw: null, reason: "no telemetry yet", source: "" } },
    strip: { collapsed: false, series: SERIES.map((s) => s.key) },
    pending: { changes: 0, units: 0 },
    drawer: { open: false, title: "" },
    toasts: [],
    nextToastId: 1,
  };
}

export function createAppStore() {
  return createStore(initialState());
}

// ---- actions (pure state transitions on a store) ----
export const SCREEN_IDS = ["dashboard", "tune", "curves", "stress", "profiles", "system", "audit"];

export function setScreen(store, id) {
  if (!SCREEN_IDS.includes(id)) return false;
  store.set({ screen: id });
  return true;
}

export function setLink(store, link) {
  store.set({ link: { state: link.state, kind: link.kind ?? null, message: link.message ?? "", fix: link.fix ?? null } });
}

export function setPreflight(store, result) {
  store.set({ preflight: result, mode: deriveMode(result) });
}

export function setLastRead(store, iso, acceptedMatch, note) {
  const patch = { lastReadAt: iso || null };
  if (acceptedMatch !== undefined) patch.accepted = deriveAccepted(acceptedMatch, note);
  store.set(patch);
}

export function setPending(store, changes, units) {
  store.set({ pending: { changes: Math.max(0, changes | 0), units: Math.max(0, units | 0) } });
}

export function setStrip(store, patch) {
  store.update((s) => ({ strip: { ...s.strip, ...patch } }));
}

export function toggleSeries(store, key) {
  store.update((s) => {
    const on = s.strip.series.includes(key);
    const series = on ? s.strip.series.filter((k) => k !== key) : SERIES.map((x) => x.key).filter((k) => k === key || s.strip.series.includes(k));
    return { strip: { ...s.strip, series } };
  });
}

export function openDrawer(store, title) {
  store.set({ drawer: { open: true, title: title || "" } });
}

export function closeDrawer(store) {
  store.set({ drawer: { open: false, title: store.get().drawer.title } });
}

export const MAX_TOASTS = 4;
export const TOAST_KINDS = ["info", "ok", "warn", "error"];

/** Returns the new toast id. ttlMs 0 = stays until dismissed (errors default to that). */
export function pushToast(store, { kind = "info", text, ttlMs } = {}) {
  const k = TOAST_KINDS.includes(kind) ? kind : "info";
  const ttl = ttlMs === undefined ? (k === "error" ? 0 : k === "warn" ? 8000 : 4000) : ttlMs;
  let id = 0;
  store.update((s) => {
    id = s.nextToastId;
    const toasts = [...s.toasts, { id, kind: k, text: String(text ?? ""), ttlMs: ttl }].slice(-MAX_TOASTS);
    return { toasts, nextToastId: id + 1 };
  });
  return id;
}

export function dismissToast(store, id) {
  store.update((s) => (s.toasts.some((t) => t.id === id) ? { toasts: s.toasts.filter((t) => t.id !== id) } : null));
}

// ---- helper events (heartbeat state progress telemetry vf.point result), the same payloads the helper sends (contract 9) ----
export function applyHelperEvent(store, name, payload) {
  const p = payload || {};
  if (name === "telemetry") {
    const values = p.values || null;
    const samples = {};
    for (const s of SERIES) samples[s.key] = s.pick(values);
    store.update((st) => ({
      telemetry: { values, t: p.t ?? null, samples, history: pushSample(st.telemetry.history, samples), limits: limitBadges(values) },
    }));
    return true;
  }
  if (name === "heartbeat" || name === "state") {
    store.update((st) => ({
      helperOp: {
        state: p.state ?? st.helperOp.state,
        opId: p.op_id ?? null,
        opsUsed: p.ops_used ?? st.helperOp.opsUsed,
        opsBudget: p.ops_budget ?? st.helperOp.opsBudget,
      },
    }));
    return true;
  }
  if (name === "progress") {
    store.update((st) => ({
      helperOp: { ...st.helperOp, opId: p.op_id ?? st.helperOp.opId, opsUsed: p.ops_used ?? st.helperOp.opsUsed, opsBudget: p.ops_budget ?? st.helperOp.opsBudget },
    }));
    return true;
  }
  return false; // vf.point / result are for the views that asked for them
}
