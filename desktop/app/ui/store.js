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

// ---- throttle indicators (G-229; docs/knowledge/throttling.md section B): the XTU-style Power / Current-EDP / Thermal chips from the 0x64F STATUS bits ----
// The helper's sampler oversamples 0x64F at ~10 Hz and publishes `throttle` (per group {active, duty, bits}); when that key is absent (an older helper, a recorded
// fixture) the point-in-time `limit_reasons` word is used with no duty. Nothing here is a hardware write, and "reset" only clears the app's own counters.
// group: which chip a status bit lights. Bits 4 / 13 are informational (popover only); a bit missing here is shown by number, never named.
export const LIMIT_BITS = {
  0: { key: "prochot", label: "PROCHOT", group: "thermal" },
  1: { key: "thermal", label: "Thermal", group: "thermal" },
  4: { key: "residency_state_regulation", label: "Residency state regulation", group: null },
  5: { key: "ratl", label: "Running average thermal limit", group: "thermal" },
  6: { key: "vr_therm_alert", label: "VR thermal alert", group: "vr_thermal" },
  7: { key: "vr_tdc", label: "VR thermal design current", group: "current" },
  8: { key: "other", label: "Other (electrical or other)", group: "current" },
  10: { key: "pl1", label: "Package power limit PL1", group: "power" },
  11: { key: "pl2", label: "Package power limit PL2", group: "power" },
  12: { key: "max_turbo_limit", label: "Max turbo limit (multi-core)", group: "max_turbo" },
  13: { key: "turbo_transition_attenuation", label: "Turbo transition attenuation", group: null },
};
const BIT_OF_KEY = Object.fromEntries(Object.entries(LIMIT_BITS).map(([b, v]) => [v.key, Number(b)]));
// names of the keys the other words use (0x6B0 graphics adds inefficient_operation; 0x1B1 has its own SDM names)
const KEY_LABEL = {
  ...Object.fromEntries(Object.values(LIMIT_BITS).map((v) => [v.key, v.label])),
  inefficient_operation: "Inefficient operation",
  critical_temp: "Critical temperature",
  threshold1: "Thermal threshold 1",
  threshold2: "Thermal threshold 2",
  power_limit: "Package power limit notification",
  pmax_log: "Pmax log (unverified on Arrow Lake)",
};
export const THROTTLE_GROUPS = [
  { key: "power", label: "Power limit", chip: "Power", severity: "warn" },
  { key: "current", label: "Current / EDP", chip: "Current/EDP", severity: "warn" },
  { key: "thermal", label: "Thermal", chip: "Thermal", severity: "error" },
  { key: "vr_thermal", label: "VR thermal", chip: "VR thermal", severity: "error" },
  { key: "max_turbo", label: "Max turbo cap", chip: "Max turbo", severity: "info", info: true },
];
// the words the popover lists: telemetry key, MSR, scope, and whether Windows XTU can clear its sticky log bits (the kernel clears the 0x1B1 ones itself)
export const LIMIT_WORDS = [
  { value: "limit_reasons", msr: "0x64F", name: "Core limit reasons", xtuClears: true },
  { value: "limit_reasons_ring", msr: "0x6B1", name: "Ring limit reasons", xtuClears: true },
  { value: "limit_reasons_gfx", msr: "0x6B0", name: "Graphics limit reasons (iGPU)", xtuClears: true },
  { value: "pkg_therm_status", msr: "0x1B1", name: "Package thermal status", xtuClears: false },
];

const readingOk = (r) => r && typeof r === "object" && r.status !== "unavailable" && r.value !== null && r.value !== undefined && typeof r.value === "object";
const keyLabel = (k) => KEY_LABEL[k] || k;
const hex = (n) => `0x${Number(n).toString(16).toUpperCase()}`;

/** helper `values` -> the throttle model the chips, strip and popover draw. Unavailable stays unavailable ('na' + the reason); never a guess. */
export function deriveThrottle(values) {
  const thr = values?.throttle;
  const core = values?.limit_reasons;
  const win = readingOk(thr) && thr.value.groups ? thr.value : null;
  const point = !win && readingOk(core) ? core.value : null;
  const words = [];
  for (const w of LIMIT_WORDS) {
    const r = values?.[w.value];
    if (!readingOk(r)) continue;
    const status = (r.value.status || []).map((k) => ({ key: k, label: keyLabel(k) }));
    const log = (r.value.log || []).map((k) => ({ key: k, label: keyLabel(k) }));
    words.push({ ...w, raw: Number(r.value.raw), rawText: hex(r.value.raw), status, log, unknown: r.value.unknown_bits || [] });
  }
  const simulated = /simulated/i.test(String(core?.source || ""));
  const base = { words, simulated, kernel: readingOk(values?.kernel_thermal) ? values.kernel_thermal.value : null, logFile: win?.log || null };
  if (!win && !point) {
    const why = !values ? "no telemetry yet" : (thr && thr.reason) || (core && core.reason) || "not reported";
    return { ...base, state: "na", reason: why, source: (thr && thr.source) || (core && core.source) || "msr 0x64F", groups: THROTTLE_GROUPS.map((g) => ({ ...g, state: "na", duty: null, bits: [] })), alert: false, active: [], info: [], unknown: [], samples: 0, windowS: null };
  }
  const bitKeys = win ? Object.keys(win.bit_duty || {}) : point.status || [];
  const dutyOf = (k) => (win && win.bit_duty && typeof win.bit_duty[k] === "number" ? win.bit_duty[k] : null);
  const active = bitKeys.filter((k) => BIT_OF_KEY[k] !== undefined).map((k) => ({ bit: BIT_OF_KEY[k], key: k, label: keyLabel(k), group: LIMIT_BITS[BIT_OF_KEY[k]].group, duty: dutyOf(k), msr: "0x64F" }));
  const groups = THROTTLE_GROUPS.map((g) => {
    const wg = win?.groups?.[g.key];
    const mine = active.filter((a) => a.group === g.key);
    const on = wg ? !!wg.active : mine.length > 0;
    return { ...g, state: on ? "yes" : "no", duty: wg && typeof wg.duty === "number" ? wg.duty : null, bits: mine };
  });
  const info = active.filter((a) => a.group === null || a.group === "max_turbo");
  const unknown = win ? win.unknown_bits || [] : point.unknown_bits || [];
  return {
    ...base, state: "ok", reason: null, source: win ? thr.source || "msr 0x64F" : core.source || "msr 0x64F", groups, active,
    alert: groups.some((g) => g.state === "yes" && !g.info), info, unknown: unknown.filter((b) => b < 16), samples: win ? win.samples : 1, windowS: win ? win.window_s : null,
  };
}

export const THROTTLE_SESSION_GAP_S = 10; // a gap between telemetry events longer than this (helper paused) adds no time

export function freshThrottleSession(now = Date.now() / 1000) {
  return { since: now, lastT: null, seconds: Object.fromEntries(THROTTLE_GROUPS.map((g) => [g.key, 0])), resetAt: null };
}

/** Add this telemetry tick to the session counters: seconds ~ duty x elapsed (a point-in-time word without a duty counts as the whole tick). */
export function accumulateThrottle(session, model, t) {
  if (!Number.isFinite(t)) return session;
  const dt = session.lastT !== null && t > session.lastT && t - session.lastT <= THROTTLE_SESSION_GAP_S ? t - session.lastT : 0;
  const seconds = { ...session.seconds };
  if (model.state === "ok") for (const g of model.groups) if (g.state === "yes") seconds[g.key] = (seconds[g.key] || 0) + dt * (g.duty ?? 1);
  return { ...session, lastT: t, seconds };
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
    telemetry: { values: null, t: null, samples: null, history: {}, throttle: deriveThrottle(null) },
    throttleSession: freshThrottleSession(),
    throttleEvents: { state: "idle", records: [], error: null, path: null, truncated: false, at: null },
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

/** "Reset indicators": clears the app's own session counters and hides older events in the list. It writes no register (throttling.md section C). */
export function resetThrottle(store, now = Date.now() / 1000) {
  store.update((s) => ({ throttleSession: { ...freshThrottleSession(now), lastT: s.throttleSession.lastT, resetAt: now } }));
}

/** state: 'loading' | 'ok' | 'error'; `records` are throttle.jsonl events, oldest first. */
export function setThrottleEvents(store, patch) {
  store.update((s) => ({ throttleEvents: { ...s.throttleEvents, ...patch } }));
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
    const throttle = deriveThrottle(values);
    store.update((st) => ({
      telemetry: { values, t: p.t ?? null, samples, history: pushSample(st.telemetry.history, samples), throttle },
      throttleSession: accumulateThrottle(st.throttleSession, throttle, p.t),
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
