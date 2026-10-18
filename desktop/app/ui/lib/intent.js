// intent.js: the draft -> `tune.plan` intent, exactly the helper's schema (G-207). Source of truth: src/arltuner/helper/ops_tune.py canonical_intent / _canonical_row /
// _canonical_turbo / _canonical_ring (G-212; ops_tune.py:1222-1350). Ported from web/app.js buildIntent / turboIntent (web/app.js:2850-2857, 2945-2970).
//
//   { scope: "unit", rows: [{target: "P0"|"EC1", offset: null | {kind:"curve", mv:int} | {kind:"points", mv:{"<ratio>": int}}, v_target_v: null|volts, ratio: null|int}],
//     turbo?: {P?: [8 ratios], E?: [8 ratios]}, ring?: {points: {"<P2 point number>": int mV}} }
// Always the "unit" scope: the desktop editor is selection based, so a P/E merge ("simple" scope) is never needed; per-unit rows say the same thing. Offsets are whole mV (the helper
// rejects fractions). `ring` is emitted only when `tune.read` said the helper understands it (`intent_features` contains "ring"); a ring edit without that is an error, never a silent drop.
// `scope`/`rows` come together: a draft with only turbo or ring edits has neither.
import { RING_ID, unitKind } from "./units.js";
import { turboCur } from "./draft.js";

const MAX_ROWS = 32; // helper: "rows must be a list of at most 32 rows"
const MAX_POINTS_PER_ROW = 12;
export const PROFILE_NAME_RE = /^[a-z0-9._-]{1,64}$/; // profiles/store.py NAME_RE

const mv = (x) => Math.round(x);
const volts = (x) => Math.round(x * 10000) / 10000;

function rowOf(id, d) {
  let offset = null;
  if (typeof d.curveMv === "number") offset = { kind: "curve", mv: mv(d.curveMv) };
  else if (Object.keys(d.pts).length) {
    const points = {};
    for (const r of Object.keys(d.pts).sort((a, b) => a - b)) points[String(Number(r))] = mv(d.pts[r]);
    offset = { kind: "points", mv: points };
  }
  return { target: id, offset, v_target_v: d.vt === undefined ? null : volts(d.vt), ratio: d.ratio === undefined ? null : d.ratio };
}

/** -> {intent} (null when nothing is pending: the helper would answer NOTHING_TO_DO) or {error} (a draft that cannot be sent as it is). */
export function buildIntent(model, draft) {
  const rows = [];
  for (const id of model.order) {
    const d = draft.units[id];
    if (!d || unitKind(id) === "R") continue;
    const row = rowOf(id, d);
    if (row.offset === null && row.v_target_v === null && row.ratio === null) continue; // the helper drops an all-null row
    if (row.offset && row.offset.kind === "points" && Object.keys(row.offset.mv).length > MAX_POINTS_PER_ROW) return { error: id + " has more than " + MAX_POINTS_PER_ROW + " point edits" };
    rows.push(row);
  }
  if (rows.length > MAX_ROWS) return { error: "more than " + MAX_ROWS + " rows" };
  const intent = {};
  if (rows.length) {
    intent.scope = "unit";
    intent.rows = rows;
  }
  const turbo = {};
  for (const kind of ["P", "E"]) {
    const t = draft.turbo[kind];
    const cur = turboCur(model, kind);
    if (t && cur && t.some((x, i) => x !== cur[i])) turbo[kind] = t.slice();
  }
  if (Object.keys(turbo).length) intent.turbo = turbo;
  const ring = draft.units[RING_ID];
  if (ring && Object.keys(ring.pts).length) {
    if (!model.features.ring) return { error: "this helper does not understand ring edits (tune.read reported no \"ring\" intent feature)" };
    const points = {};
    for (const n of Object.keys(ring.pts).sort((a, b) => a - b)) points[String(Number(n))] = mv(ring.pts[n]);
    intent.ring = { points };
  }
  return { intent: Object.keys(intent).length ? intent : null };
}

/** Whole-state intents of the helper (never mixed with edits). */
export const resetIntent = () => ({ reset: "stock" });
export const restoreIntent = () => ({ restore: "accepted" });
export function profileIntent(name) {
  return typeof name === "string" && PROFILE_NAME_RE.test(name) ? { profile: name } : { error: "a profile name is 1..64 characters of a-z 0-9 . _ -" };
}

// ---- power / runtime (G-217) -----------------------------------------------------------------------------------------------------------------------
// The helper's power.plan / runtime.plan take their fields at the top level, NOT wrapped in {intent} like tune.plan (ops_power.py _validate_power_plan: {power:{pl1_w?, pl2_w?, tau_s?}} | {revert:true};
// ops_runtime.py _validate_runtime_plan: {caps?:{"<cpu>": ratio}, uncore?:{min_ratio?, max_ratio?}} | {revert:true}). The functions below return exactly those argument objects as `intent`.
// Parking is never built: the helper refuses it (D-048).
export const POWER_KEYS = Object.freeze(["pl1_w", "pl2_w", "tau_s"]); // controls/power.py KEYS
export const UNCORE_KEYS = Object.freeze(["min_ratio", "max_ratio"]); // controls/uncore.py KEYS
export const POWER_REVERT = Object.freeze({ revert: true });
export const RUNTIME_REVERT = Object.freeze({ revert: true });
const MAX_CPU = 1024; // ops_runtime.py _CPU_MAX
const isWhole = (x) => typeof x === "number" && Number.isInteger(x);

/**
 * `edit` = {pl1_w?, pl2_w?, tau_s?} (only what was edited; a value equal to `cur` (the same keys, as in force) is dropped, the helper would answer NOTHING_TO_DO).
 * -> {intent: {power: {...}}} | {intent: null} (nothing pending) | {error}. The PLs are whole watts (ops_power.py: "whole watts"), tau any positive number of seconds.
 */
export function powerIntent(edit, cur = {}) {
  const power = {};
  for (const key of Object.keys(edit || {})) {
    if (!POWER_KEYS.includes(key)) return { error: "unknown power key " + key };
    const v = edit[key];
    if (v === undefined || v === null) continue;
    if (typeof v !== "number" || !Number.isFinite(v)) return { error: key + " must be a number" };
    if (key === "tau_s" ? v <= 0 : !isWhole(v) || v < 1) return { error: key === "tau_s" ? "tau_s must be positive seconds" : key + " must be whole watts" };
    if (cur && typeof cur[key] === "number" && Math.abs(cur[key] - v) < 1e-9) continue;
    power[key] = v;
  }
  return { intent: Object.keys(power).length ? { power } : null };
}

/**
 * `edit` = {caps?: {cpu: ratio}, uncore?: {min_ratio?, max_ratio?}} (cpu = CPU number, whole ratio 1..255; 1 ratio = 100 MHz). -> {intent: {caps?: {"<cpu>": ratio}, uncore?: {...}}} | {intent: null} | {error}.
 * An E cluster's cap is given for every CPU of the cluster by the caller (the helper expands it anyway; two CPUs of one cluster asking differently is a refusal).
 */
export function runtimeIntent(edit) {
  const intent = {};
  const caps = (edit && edit.caps) || {};
  const capKeys = Object.keys(caps).sort((a, b) => Number(a) - Number(b));
  if (capKeys.length) {
    const out = {};
    for (const k of capKeys) {
      if (!/^[0-9]{1,4}$/.test(k) || Number(k) >= MAX_CPU) return { error: "caps keys must be CPU numbers, got " + k };
      const r = caps[k];
      if (!isWhole(r) || r < 1 || r > 255) return { error: "caps." + k + " must be a whole ratio 1..255" };
      out[String(Number(k))] = r;
    }
    intent.caps = out;
  }
  const uncore = {};
  for (const k of Object.keys((edit && edit.uncore) || {})) {
    if (!UNCORE_KEYS.includes(k)) return { error: "unknown uncore key " + k };
    const r = edit.uncore[k];
    if (r === undefined || r === null) continue;
    if (!isWhole(r) || r < 1 || r > 255) return { error: "uncore." + k + " must be a whole ratio 1..255" };
    uncore[k] = r;
  }
  if (Object.keys(uncore).length) intent.uncore = uncore;
  return { intent: Object.keys(intent).length ? intent : null };
}
