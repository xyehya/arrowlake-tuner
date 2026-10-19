// views/tune/apply.js: the apply drawer (PLAN §4.3): Review & Apply -> plan -> ordered write list -> Apply (CONFIRM <nonce>) -> live progress -> read-back result. Ported from
// web/app.js requestPlan / planDiff / renderPlanBody / commitPlan / onCommitResult (web/app.js:3903-4388), reshaped for a non-modal drawer.
//
// WIRING (app.js is not owned by G-210): `createApplyDrawer(ctx)` after `createDrawer`, with ctx = {store, bridge, toast, drawer}; it listens for "tune:review" on store.events
// (views/tune.js requestReview) and renders into the drawer. The pending bar's onReview should shrink to `requestReview(store)` (the G-205 placeholder text would otherwise show
// for one tick before this module replaces it). The drawer opens even if the plan fails: a plan error is the drawer's content AND is landed on the offending field.
//
// MORE THAN ONE SECTION: the drawer holds a list of sections {kind, label, intent, plan, status, ...}, applied in order, each with its own plan_id / nonce, stopping at the first
// failure (later sections are "skipped"). A kind is an entry in KINDS: {label, planOp, applyOp, request(store) -> {intent}|{intent:null}|{error}, afterApply, locate}. The tune kind is
// built in; G-217 adds power.* / runtime.* with `registerKind()` and opens the drawer with `controller.review(["tune", "power"])`.
//
// PROGRESS: the helper's `progress {step, of}` counts operations (writes AND their reads) while `plan.steps` lists the writes, so the per-unit status while running is positional
// (proportional to step/of over the ordered writes) and approximate; a unit is only "read back OK" / "mismatch" once the final result's `readback` says so.
import { h, clear } from "../../components/dom.js";
import { openDrawer, closeDrawer, setScreen, setLastRead } from "../../store.js";
import { plural, fmtMv, fmtV, isNum, DASH, TIMES } from "../../lib/format.js";
import { buildIntent } from "../../lib/intent.js";
import { modelFromRead } from "../../lib/units.js";
import { emptyDraft, discardUnits } from "../../lib/draft.js";
import { ensureTune, patchTune, fmtSeconds } from "../tune.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const MV_TOL = 0.05;
const V_TOL = 0.0005;
export const confirmText = (nonce) => "CONFIRM " + nonce;

// ---- pure helpers: plan -> rows -------------------------------------------------------------------------------------------------------------------
/** before/after of a plan -> Map unit -> Map point -> entry. Accepts {unit: {point: entry}}, {unit: [entries]} and [{unit, point, ...}] (web/app.js normState). */
function normState(x) {
  const out = new Map();
  const put = (unit, e, i) => {
    if (typeof unit !== "string") return;
    const at = out.get(unit) || new Map();
    out.set(unit, at);
    if (e === null) return void at.set(i, { point: i, unread: true, ratio: null, offset_mv: null, v_target_v: null, effective_v: null, clamped_by: null });
    if (!isObj(e)) return;
    const point = e.oc === true ? 0 : isNum(e.point) ? e.point : i;
    at.set(point, {
      point, unread: false, ratio: isNum(e.ratio) ? e.ratio : null, offset_mv: isNum(e.offset_mv) ? e.offset_mv : null, v_target_v: isNum(e.v_target_v) ? e.v_target_v : null,
      effective_v: isNum(e.effective_v) ? e.effective_v : null, clamped_by: isNum(e.clamped_by) ? e.clamped_by : null,
    });
  };
  if (Array.isArray(x)) x.forEach((e, i) => isObj(e) && put(e.unit, e, i));
  else if (isObj(x)) {
    for (const [unit, v] of Object.entries(x)) {
      if (Array.isArray(v)) v.forEach((e, i) => put(unit, e, i));
      else if (isObj(v)) for (const [k, e] of Object.entries(v)) if (/^\d+$/.test(k)) put(unit, e, Number(k));
    }
  }
  return out;
}

const same = (a, b, tol) => (a === null && b === null) || (isNum(a) && isNum(b) && Math.abs(a - b) <= tol);

/** -> [{unit, changes, rows: [{point, b, a, changed}]}] for every unit the plan has before/after for, in plan.units order first. */
export function planRows(plan) {
  const before = normState(plan && plan.before);
  const after = normState(plan && plan.after);
  const ids = [...new Set([...(Array.isArray(plan && plan.units) ? plan.units : []), ...before.keys(), ...after.keys()])].filter((u) => before.has(u) || after.has(u));
  return ids.map((unit) => {
    const pts = [...new Set([...(before.get(unit) || new Map()).keys(), ...(after.get(unit) || new Map()).keys()])].sort((x, y) => x - y);
    const rows = pts.map((point) => {
      const b = (before.get(unit) || new Map()).get(point) || null;
      const a = (after.get(unit) || new Map()).get(point) || null;
      const changed = !b || !a || b.unread || b.ratio !== a.ratio || !same(b.offset_mv, a.offset_mv, MV_TOL) || !same(b.v_target_v, a.v_target_v, V_TOL);
      return { point, b, a, changed };
    });
    return { unit, rows, changes: rows.filter((r) => r.changed).length };
  });
}

/** "pt 5 · 53×  0 mV → −19.5 mV" for one changed row (parts that did not change are left out). */
export function describeRow(r) {
  const x = r.a || r.b;
  const label = r.point === 0 ? "OC point" : "pt " + r.point;
  const parts = [];
  const ratio = x && isNum(x.ratio) ? " · " + x.ratio + TIMES : "";
  const arrow = (bv, av, fmt, unread) => (bv === null && unread ? "unread → " + fmt(av) : same(bv, av, 1e-9) || bv === null ? fmt(av) : fmt(bv) + " → " + fmt(av));
  if (r.a && r.b && r.b.ratio !== r.a.ratio && isNum(r.a.ratio)) parts.push("ratio " + (isNum(r.b.ratio) ? r.b.ratio + TIMES + " → " : "") + r.a.ratio + TIMES);
  if (r.a && (isNum(r.a.offset_mv) || (r.b && isNum(r.b.offset_mv)))) parts.push("offset " + arrow(r.b ? r.b.offset_mv : null, r.a ? r.a.offset_mv : null, fmtMv, r.b && r.b.unread));
  if (r.a && (isNum(r.a.v_target_v) || (r.b && isNum(r.b.v_target_v))) && (r.point === 0 || !same(r.b ? r.b.v_target_v : null, r.a.v_target_v, V_TOL))) parts.push("target " + arrow(r.b ? r.b.v_target_v : null, r.a ? r.a.v_target_v : null, fmtV, r.b && r.b.unread));
  if (r.a && isNum(r.a.effective_v)) parts.push("≈ " + fmtV(r.a.effective_v) + (r.a.clamped_by !== null ? " (clamped to V(n−1))" : ""));
  return label + ratio + "  " + (parts.join(", ") || "no change");
}

/** The ordered write list straight from plan.steps (G-153 order; never re-sorted). */
export function stepRows(plan) {
  const steps = Array.isArray(plan && plan.steps) ? plan.steps.filter(isObj) : [];
  return steps.map((s, i) => {
    const kind = typeof s.kind === "string" ? s.kind : "step";
    const unit = typeof s.unit === "string" ? s.unit : s.table ? "turbo " + String(s.table).toUpperCase() : null; // the helper's plan says table "e" | "p"; the read-back rows say "E" | "P"
    let label;
    if (kind === "write_percore_enable") label = "Enable per-core voltage offsets" + (s.conditional ? " (only if not already on)" : "");
    else if (kind === "write_vf_point") label = (unit || "unit") + " point " + s.point + (isNum(s.offset_mv) ? " offset " + fmtMv(s.offset_mv) : "") + (isNum(s.cpu) ? " (cpu " + s.cpu + ")" : "");
    else if (kind === "write_msr") label = (unit || "table") + " ratio table (plain MSR write, read back on two CPUs)";
    else label = kind.replace(/^write_/, "").replace(/_/g, " ") + (unit ? " " + unit : "") + (isNum(s.cpu) ? " (cpu " + s.cpu + ")" : "");
    return { n: isNum(s.n) ? s.n : i + 1, kind, unit, point: isNum(s.point) ? s.point : null, cpu: isNum(s.cpu) ? s.cpu : null, conditional: s.conditional === true, label };
  });
}

/** Unit keys in the order their first write is sent (units without a write in the step list follow, then the before/after units). */
export function unitOrder(plan) {
  const ids = [];
  for (const s of stepRows(plan)) if (s.unit && !s.conditional && !ids.includes(s.unit)) ids.push(s.unit);
  for (const u of [...(Array.isArray(plan && plan.units) ? plan.units : []), ...planRows(plan).map((r) => r.unit)]) if (typeof u === "string" && !ids.includes(u)) ids.push(u);
  return ids;
}

export const STATUS_TEXT = {
  queued: "queued", writing: "writing", written: "written", ok: "read back OK", mismatch: "mismatch", unverified: "not read back", no_change: "no change", interrupted: "interrupted", not_started: "not written",
};
const rbKey = (x) => (x.point === "turbo" ? "turbo " + x.unit : x.unit);

/**
 * Per-unit status. `progress` = {step, of} | null; `outcome` = null (running / not started) | {ok:true, readback:[...], complete?} | {ok:false}.
 * G-230: "no change" (grey) = nothing was written for the unit (it has no write step, or a complete apply wrote no turbo table the plan listed because it equals the CPU's);
 * "not read back" (amber) is only for a written word that has no read-back.
 * Running: positional (see the header). Finished: only the read-back decides; after a failure or cancel the units already past the current step are "not read back", the current
 * one "interrupted" (it may or may not have been written), the later ones "not written".
 */
export function unitStatuses(plan, progress, outcome) {
  const order = unitOrder(plan);
  let writes = stepRows(plan).filter((s) => s.unit && !s.conditional).map((s) => s.unit);
  if (!writes.length) writes = order.slice();
  const out = {};
  for (const u of order) out[u] = "queued";
  if (outcome && outcome.ok) {
    const by = new Map();
    for (const x of (outcome.readback || []).filter(isObj)) by.set(rbKey(x), (by.get(rbKey(x)) || []).concat(x));
    for (const u of order) {
      const rb = by.get(u);
      out[u] = rb ? (rb.every((x) => x.match === true) ? "ok" : "mismatch") : !writes.includes(u) || (outcome.complete !== false && u.startsWith("turbo")) ? "no_change" : "unverified";
    }
    return out;
  }
  const step = progress && isNum(progress.step) ? progress.step : 0;
  const idx = step > 0 && writes.length ? Math.min(writes.length - 1, Math.floor(((step - 1) / Math.max(1, progress.of || 1)) * writes.length)) : -1;
  for (const u of order) {
    const at = writes.map((w, i) => (w === u ? i : -1)).filter((i) => i >= 0);
    if (!at.length && outcome) out[u] = "no_change";
    if (!at.length || idx < 0) continue;
    const current = at.includes(idx);
    const past = at.every((i) => i < idx);
    if (outcome) out[u] = current ? "interrupted" : past ? "unverified" : "not_started";
    else out[u] = current ? "writing" : past ? "written" : "queued";
  }
  return out;
}

/** {remainingMs, text}: remaining time from the measured per-op time; null/"" before the first step. */
export function eta(elapsedMs, step, of) {
  if (!(isNum(elapsedMs) && isNum(step) && isNum(of) && step > 0 && of > 0)) return { remainingMs: null, text: "" };
  const remainingMs = Math.max(0, (elapsedMs / step) * (of - step));
  return { remainingMs, text: "~" + fmtSeconds(remainingMs) + " left" };
}

export function progressText(progress, elapsedMs) {
  if (!progress || !progress.of) return "waiting for the first step";
  const e = eta(elapsedMs, progress.step, progress.of);
  return "step " + progress.step + " of " + progress.of + " · " + fmtSeconds(elapsedMs) + " elapsed" + (e.text ? " · " + e.text : "");
}

/** The final tune.apply result -> what the drawer shows. */
export function summarizeResult(result) {
  const r = isObj(result) ? result : {};
  const readback = (Array.isArray(r.readback) ? r.readback : []).filter(isObj);
  const mismatches = readback.filter((x) => x.match !== true);
  const complete = r.complete !== false;
  return {
    readback,
    total: readback.length,
    matched: readback.length - mismatches.length,
    mismatches,
    complete,
    stop: !complete ? (isObj(r.stop) ? r.stop : {}) : null,
    opsUsed: isNum(r.ops_used) ? r.ops_used : null,
    writesSent: isNum(r.writes_sent) ? r.writes_sent : null,
    cpufreq: (Array.isArray(r.cpufreq) ? r.cpufreq : []).filter((x) => isObj(x) && isNum(x.cpu) && isNum(x.before_khz) && isNum(x.after_khz)),
    residual: (Array.isArray(r.residual) ? r.residual : []).map(String),
    ringRefused: (Array.isArray(r.ring_refused) ? r.ring_refused : []).map(String),
    allMatch: complete && readback.length > 0 && mismatches.length === 0,
  };
}

/**
 * G-232: the cells of one read-back row. V/F rows say {unit, point, cpu, expected, actual}; the power / runtime helpers say {domain?, key, expected: [lo, hi], file: value read}, which are shown by
 * file and key ("cpu7" / "scaling_max_freq"), never as a V/F "pt".
 */
export function readbackCells(x) {
  if (typeof x.unit === "string") return { unit: x.point === "turbo" ? "turbo table " + x.unit : x.unit, point: x.point === 0 ? "OC point" : x.point === "turbo" ? "ratios" : "pt " + x.point, cpu: isNum(x.cpu) ? x.cpu : "", expected: x.expected ?? DASH, actual: x.actual ?? "not read back" };
  const key = String(x.key ?? "?");
  const m = /^cpu(\d+)$/.exec(key);
  const [lo, hi] = Array.isArray(x.expected) ? x.expected : [];
  const [unit, point] = x.domain === "caps" ? [key, "scaling_max_freq"] : x.domain === "uncore" ? ["uncore", key.replace("_ratio", "_freq_khz")] : ["power", key];
  return { unit, point, cpu: m ? Number(m[1]) : "", expected: isNum(lo) ? (lo === hi ? String(lo) : lo + "–" + hi) : DASH, actual: isNum(x.file) ? String(x.file) : "not read back" };
}

/** G-232: the red box of a partial apply: V/F words speak of the CPU's words; the runtime kinds (power / caps / uncore) of runtime settings, which a reboot restores. */
export function partialWording(runtimeKind, tripped = false) {
  const guard = tripped ? " The guard tripped." : "";
  return runtimeKind
    ? { title: "Some runtime settings were written, some not.", text: "The table shows which. Use Revert to boot values, or reboot (runtime values do not survive a reboot)." + guard }
    : { title: "The CPU holds a mix of old and new words.", text: "Do not rely on it: restore the accepted state, or cold boot. The table says which words did land." + guard };
}

export function resultHeadline(s) {
  if (!s.complete) return "Partial apply: it stopped after " + (s.writesSent !== null ? plural(s.writesSent, "write") : "some writes") + (s.stop && s.stop.reason ? ". " + s.stop.reason : "");
  if (!s.total) return "Applied; the helper returned no read-back words";
  return s.mismatches.length ? s.mismatches.length + " of " + s.total + " words differ on read-back" : s.matched + "/" + s.total + " words read back as written";
}

/** Plan warnings are {code, text} (a bare string is a NOTE). Same code table as web/app.js WARN_CODES. */
const WARN_CODES = {
  CLAMPED: ["warn", "clamped"], E40_OFFSET: ["bad", "E 40× offset"], E_TARGET_WHEA: ["warn", "WHEA risk"], POSITIVE_OFFSET: ["warn", "positive offset"], NO_BASELINE: ["warn", "no baseline"],
  TURBO_RAISE: ["warn", "turbo raise"], RING_NOT_RESTORED: ["warn", "ring not written"], LARGE_CHANGE: ["warn", "large change"], TURBO_NOT_MONOTONIC: ["warn", "turbo not monotonic"],
  TURBO_BELOW_BASE: ["warn", "turbo below base"], TURBO_BELOW_OC: ["warn", "OC above turbo table"], PL2_BELOW_PL1: ["warn", "PL2 below PL1"], DEEP_WHOLE_CURVE: ["warn", "deep whole curve"], NOTE: ["", ""],
};
const CODED = /^([A-Z][A-Z0-9_]+): ([\s\S]+)$/;
/** Warnings are never blocking: the Review shows them (amber) and Apply stays available. power.plan / runtime.plan send them as "CODE: text" strings, tune.plan as {code, text}. */
export function planWarnings(plan) {
  const out = [];
  for (const w of Array.isArray(plan && plan.warnings) ? plan.warnings : []) {
    const m = typeof w === "string" ? CODED.exec(w) : null;
    const coded = m && Object.prototype.hasOwnProperty.call(WARN_CODES, m[1]);
    const code = isObj(w) && typeof w.code === "string" ? w.code : coded ? m[1] : "NOTE";
    const text = isObj(w) ? w.text : coded ? m[2] : w;
    if (typeof text !== "string" || !text) continue;
    const [cls, label] = Object.prototype.hasOwnProperty.call(WARN_CODES, code) ? WARN_CODES[code] : WARN_CODES.NOTE;
    out.push({ code, text, cls, label });
  }
  return out;
}

export const planWrites = (plan) => (isNum(plan && plan.writes) ? plan.writes : stepRows(plan).length);

// ---- plan errors -> the field -------------------------------------------------------------------------------------------------------------------
/**
 * A plan refusal -> {unit, field, ratio, point, message, check}. `field` is "point" | "curve" | "vt" | "ratio" | "ring" | "turbo" | null (nothing to point at: the drawer says it).
 * The helper names the field in the message prefix ("P0: offset -250 mV ...", "ring point 3: ...", "turbo.P: ...") and the intent maps the quoted value back to a ratio.
 */
export function locateError(error, intent) {
  const message = String((error && (error.message || error.code)) || "");
  const check = isObj(error && error.detail) && typeof error.detail.check === "string" ? error.detail.check : null;
  const base = { unit: null, field: null, ratio: null, point: null, message, check };
  if (check === "NO_READ") return { ...base, field: null };
  if (check === "TURBO_ORDER") {
    const d = error.detail;
    return { ...base, unit: "turbo " + String(d.table || "").toUpperCase(), field: "turbo", point: isNum(d.bucket) ? d.bucket : null };
  }
  let m = /^ring point (\d+):/.exec(message);
  if (m) return { ...base, unit: "ring", field: "ring", point: Number(m[1]) };
  m = /^turbo\.([PEpe]):/.exec(message);
  if (m) return { ...base, unit: "turbo " + m[1].toUpperCase(), field: "turbo" };
  m = /^(P\d+|EC\d+|ring):/.exec(message);
  if (!m) return base;
  const unit = m[1];
  const row = isObj(intent) && Array.isArray(intent.rows) ? intent.rows.find((r) => r && r.target === unit) : null;
  const o = /offset ([+-]\d+) mV/.exec(message);
  if (o) {
    const mv = Number(o[1]);
    if (row && row.offset && row.offset.kind === "points") {
      const ratio = Object.keys(row.offset.mv).find((k) => row.offset.mv[k] === mv);
      return { ...base, unit, field: ratio !== undefined ? "point" : "curve", ratio: ratio !== undefined ? Number(ratio) : null };
    }
    return { ...base, unit, field: "curve" };
  }
  if (/OC-point voltage/.test(message)) return { ...base, unit, field: "vt" };
  if (/ratio \d+ is above/.test(message)) return { ...base, unit, field: "ratio" };
  return { ...base, unit };
}

/**
 * G-227: the units model with the plan's predicted offsets (and the intent's turbo tables) laid over the words whose read-back matched (`summary.readback` rows {unit, point, match};
 * a turbo row is {unit: "P"|"E", point: "turbo"}); null when nothing changed. Pure. Only offsets and ratios are patched (this module has no word decoder): a word that read back
 * different is left as it was, and the caller says so.
 */
export function modelAfterApply(model, section, summary) {
  if (!isObj(model) || !isObj(section) || !isObj(section.plan)) return null;
  const after = normState(section.plan.after);
  const turbo = isObj(section.intent) && isObj(section.intent.turbo) ? section.intent.turbo : {};
  let next = null;
  for (const row of summary.readback) {
    if (row.match !== true) continue;
    if (row.point === "turbo") {
      if (isObj(model.turbo) && isObj(model.turbo[row.unit]) && Array.isArray(turbo[row.unit]) && turbo[row.unit].length === 8) {
        next ||= structuredClone(model);
        next.turbo[row.unit].ratios = turbo[row.unit].slice();
      }
      continue;
    }
    const entry = after.has(row.unit) ? after.get(row.unit).get(row.point) : null;
    if (!isNum(row.point) || !model.units[row.unit] || !entry || !isNum(entry.offset_mv)) continue;
    const at = (m) => {
      const u = m.units[row.unit];
      return [row.point === 0 ? u.oc : null, u.points.find((p) => p.point === row.point), u.ocPoint && u.ocPoint.point === row.point ? u.ocPoint : null].filter(Boolean);
    };
    if (!at(model).length) continue;
    next ||= structuredClone(model);
    for (const t of at(next)) t.offset_mv = entry.offset_mv;
  }
  return next;
}

// ---- section kinds -------------------------------------------------------------------------------------------------------------------------------
export const KINDS = {
  tune: {
    label: "V/F points & turbo tables",
    planOp: "tune.plan",
    applyOp: "tune.apply",
    /** -> {intent} | {intent: null} (nothing pending) | {error} */
    request(store) {
      const t = ensureTune(store);
      if (!t.model) return { error: "Read the CPU state first (Tune → Read CPU state): a plan is made against the words of a read." };
      const built = buildIntent(t.model, t.draft);
      return built.error ? { error: built.error } : { intent: built.intent, draftRef: t.draft };
    },
    locate: locateError,
    /**
     * After an apply: re-read tune.last (no mailbox traffic) so tiles and inspector show the new words. G-227: when the helper has no usable record (unavailable, an error, or one that
     * predates this apply: applied_at null) the model is patched from the plan and the matched read-back rows, so the applied offsets show without a "Read CPU state". The draft entries
     * of the units that were written go (the model is what the CPU holds, a clamped point included) unless the draft was edited meanwhile; with no new model the draft stays.
     */
    async afterApply({ store, bridge, section, summary, toast }) {
      const last = await bridge.call("tune.last");
      const rec = last.ok && isObj(last.result) && last.result.available && isObj(last.result.result) && last.result.applied_at != null ? last.result : null;
      const patch = {};
      let model = rec ? modelFromRead(rec.result) : null;
      if (model) {
        const at = isNum(rec.at) ? new Date(rec.at * 1000).toISOString() : new Date().toISOString();
        Object.assign(patch, { model, read: { ...store.get().tune.read, state: "ready", fromLast: true, at, durationMs: null, ops: rec.result.ops_done ?? null, error: null } });
        setLastRead(store, at, rec.result.accepted_match ?? null, rec.result.accepted_note || "");
      } else if ((model = modelAfterApply(store.get().tune.model, section, summary))) {
        patch.model = model;
        if (toast) toast("warn", "The helper has no stored read to show the applied words from: the tiles follow the plan and the read-back. Re-read the CPU state to confirm.");
      }
      if (model && store.get().tune.draft === section.draftRef) {
        patch.draft = summary.allMatch ? emptyDraft() : discardUnits(section.draftRef, summary.readback.map((x) => x.unit).filter((u) => typeof u === "string"));
        if (!summary.allMatch && summary.mismatches.length && toast) toast("warn", plural(summary.mismatches.length, "word") + " read back different from the plan (clamped by the CPU): the tiles show what the CPU holds.");
      }
      if (Object.keys(patch).length) patchTune(store, patch);
    },
  },
};

export function registerKind(name, spec) {
  if (typeof name !== "string" || !spec || typeof spec.planOp !== "string" || typeof spec.applyOp !== "string" || typeof spec.request !== "function") throw new TypeError("registerKind: {planOp, applyOp, request} required");
  KINDS[name] = { label: name, ...spec };
}

// ---- the controller (no DOM) ---------------------------------------------------------------------------------------------------------------------
export const initialApply = () => ({
  state: "idle", // idle | planning | review | applying | done | failed
  sections: [], // see makeSection
  error: null, // a request-level problem (nothing to apply, no read yet, a plan refusal): {message, located}
  startedAt: null,
  seq: 0,
});

export function ensureApply(store) {
  if (!store.get().apply) store.set({ apply: initialApply() });
  return store.get().apply;
}

/** statuses: planning | review | stale (replan) | applying | done | failed | cancelled | skipped */
const makeSection = (kind, spec, req) => ({ kind, label: req.label || spec.label, intent: req.intent, draftRef: req.draftRef, plan: null, issuedAt: null, status: "planning", opId: null, progress: null, result: null, summary: null, error: null, cancelling: false });

export function createApplyController({ store, bridge, toast = () => {}, kinds = KINDS, now = () => performance.now() }) {
  ensureTune(store);
  ensureApply(store);
  const get = () => store.get().apply;
  const patch = (p) => store.update((s) => ({ apply: { ...s.apply, ...p } }));
  const setSection = (i, p) => store.update((s) => ({ apply: { ...s.apply, sections: s.apply.sections.map((x, j) => (j === i ? { ...x, ...p } : x)) } }));
  const connected = () => store.get().link.state === "connected";
  let busy = false;

  async function planSection(i) {
    const sec = get().sections[i];
    const spec = kinds[sec.kind];
    setSection(i, { status: "planning", error: null, plan: null });
    const res = await bridge.call(spec.planOp, spec.planArgs ? spec.planArgs(sec.intent) : { intent: sec.intent });
    if (res.ok && isObj(res.result) && typeof res.result.plan_id === "string") {
      setSection(i, { status: "review", plan: res.result, issuedAt: now(), error: null });
      return true;
    }
    let error = res.ok ? { code: "BAD_REPLY", message: "the helper's plan had no plan_id" } : res.error;
    if (spec.explain) error = { ...error, message: spec.explain(error) };
    setSection(i, { status: "failed", error: { ...error, located: spec.locate ? spec.locate(error, sec.intent) : null } });
    return false;
  }

  /** kinds: ["tune"] or [{kind, intent}]. Builds the requests, plans each; the state ends "review" or "failed". */
  async function review(requests = ["tune"]) {
    if (busy) return get().state; // an apply is running: the caller just reopens the drawer
    const sections = [];
    for (const r of requests) {
      const name = typeof r === "string" ? r : r.kind;
      const spec = kinds[name];
      if (!spec) throw new Error("apply: unknown section kind " + name);
      const req = typeof r === "string" ? spec.request(store) : { intent: r.intent, label: r.label };
      if (req.error) {
        patch({ state: "failed", sections: [], error: { message: req.error, located: null }, seq: get().seq + 1 });
        return "failed";
      }
      if (req.intent === null || req.intent === undefined) continue;
      sections.push(makeSection(name, spec, req));
    }
    if (!sections.length) {
      patch({ state: "failed", sections: [], error: { message: "Nothing to apply: the pending edits equal the CPU's words.", located: null }, seq: get().seq + 1 });
      return "failed";
    }
    patch({ state: "planning", sections, error: null, startedAt: null, seq: get().seq + 1 });
    for (let i = 0; i < sections.length; i++) {
      if (!(await planSection(i))) {
        const err = get().sections[i].error;
        patch({ state: "failed", error: { message: sections.length > 1 ? sections[i].label + ": " + err.message : err.message, located: err.located } });
        return "failed";
      }
    }
    patch({ state: "review", error: null });
    return "review";
  }

  /** Plan again for the same requests (after PLAN_STALE or an expired plan). Sections that were applied stay applied; the rest are re-planned. */
  async function replan() {
    if (busy) return;
    const secs = get().sections;
    patch({ state: "planning", error: null });
    for (let i = 0; i < secs.length; i++) {
      if (get().sections[i].status === "done") continue;
      if (!(await planSection(i))) {
        const err = get().sections[i].error;
        patch({ state: "failed", error: { message: err.message, located: err.located } });
        return "failed";
      }
    }
    patch({ state: "review", error: null });
    return "review";
  }

  const outdated = (sec) => {
    if (sec.draftRef === undefined) return false;
    return sec.kind === "tune" && store.get().tune.draft !== sec.draftRef;
  };
  const expired = (sec) => sec.plan && isNum(sec.plan.expires_s) && sec.issuedAt !== null && now() - sec.issuedAt > sec.plan.expires_s * 1000;

  /** Why Apply is disabled ("" = ready). */
  function gate() {
    const a = get();
    if (a.state !== "review") return a.state === "applying" ? "Applying." : "Nothing is planned.";
    if (!connected()) return "The helper is not connected.";
    for (const sec of a.sections) {
      if (outdated(sec)) return "The pending edits changed after this plan was made. Re-plan.";
      if (expired(sec)) return "This plan expired. Re-plan.";
    }
    return "";
  }

  async function runSection(i) {
    const sec = get().sections[i];
    const spec = kinds[sec.kind];
    const early = [];
    let id = null;
    const onProgress = (p) => {
      if (!isObj(p)) return;
      if (id === null) return void early.push(p);
      if (p.op_id !== undefined && p.op_id !== id) return;
      setSection(i, { progress: { step: p.step ?? 0, of: p.of ?? 0, opsUsed: p.ops_used ?? null } });
    };
    bridge.on("progress", onProgress);
    try {
      const first = await bridge.call(spec.applyOp, { plan_id: sec.plan.plan_id, typed: confirmText(sec.plan.nonce) });
      let res;
      if (first.ok && first.pending) {
        id = first.id;
        setSection(i, { opId: id });
        for (const p of early) onProgress(p);
        res = await bridge.wait(id);
      } else res = first;
      return res;
    } finally {
      bridge.off("progress", onProgress);
    }
  }

  /** Apply the planned sections in order; stop at the first failure. */
  async function apply() {
    const why = gate();
    if (why || busy) return false;
    busy = true;
    patch({ state: "applying", startedAt: now() });
    let failedAt = -1;
    try {
      const secs = get().sections;
      for (let i = 0; i < secs.length; i++) {
        if (get().sections[i].status === "done") continue;
        setSection(i, { status: "applying", progress: null, error: null, cancelling: false });
        const res = await runSection(i);
        if (res.ok) {
          const summary = summarizeResult(res.result);
          setSection(i, { status: "done", result: res.result, summary, opId: get().sections[i].opId });
          try {
            if (kinds[secs[i].kind].afterApply) await kinds[secs[i].kind].afterApply({ store, bridge, section: get().sections[i], summary, toast });
          } catch (e) {
            toast("warn", "The apply finished but refreshing the tiles failed: " + (e && e.message ? e.message : e));
          }
          if (!summary.allMatch) {
            failedAt = i; // a mismatch or a partial apply also stops the sequence: do not build on a state that did not read back
            break;
          }
        } else {
          const code = res.error && res.error.code;
          setSection(i, { status: code === "CANCELLED" ? "cancelled" : code === "PLAN_STALE" ? "stale" : "failed", error: { ...res.error, located: null } });
          failedAt = i;
          break;
        }
      }
    } finally {
      busy = false;
    }
    const a = get();
    if (failedAt >= 0) {
      store.update((s) => ({ apply: { ...s.apply, state: "failed", sections: s.apply.sections.map((x, j) => (j > failedAt && x.status === "review" ? { ...x, status: "skipped" } : x)) } }));
    } else patch({ state: "done" });
    return failedAt < 0 && a.state !== "failed";
  }

  /** Cancel the running section: takes effect between transactions, so the request in flight still completes. */
  async function cancel() {
    const i = get().sections.findIndex((s) => s.status === "applying");
    if (i < 0 || !get().sections[i].opId) return false;
    setSection(i, { cancelling: true });
    const res = await bridge.call("cancel", { op_id: get().sections[i].opId });
    if (!res.ok) setSection(i, { cancelling: false });
    return res.ok;
  }

  function reset() {
    if (busy) return;
    store.set({ apply: initialApply() });
  }

  return { review, replan, apply, cancel, reset, gate, isBusy: () => busy, state: get };
}

// ---- DOM: landing a plan error on the offending field ---------------------------------------------------------------------------------------------
const ERR_CLASS = "plan-error";

/** Remove every mark this module put in `root`. */
export function clearFieldError(root) {
  for (const n of root.querySelectorAll(".field-error-note, .plan-error-banner")) n.remove();
  for (const n of root.querySelectorAll("." + ERR_CLASS)) n.classList.remove(ERR_CLASS);
  for (const n of root.querySelectorAll("[data-plan-invalid]")) {
    n.removeAttribute("aria-invalid");
    n.removeAttribute("data-plan-invalid");
  }
}

/** Mark the field a located error names; returns the element carrying the mark (or null when only the tile/banner could be marked). */
export function landFieldError(root, located) {
  clearFieldError(root);
  if (!located || !located.unit) return null;
  const note = (text) => h("p", { class: "field-error-note", role: "alert", text });
  const tile = root.querySelector(`.tile[data-unit="${located.unit}"]`);
  if (tile) tile.classList.add(ERR_CLASS);
  let input = null;
  let host = null;
  if (located.field === "point" && located.ratio !== null) {
    const row = root.querySelector(`.insp-section tr[data-ratio="${located.ratio}"]`);
    input = row && row.querySelector("input");
    host = row && row.querySelector(".c-off");
  } else if (located.field === "ring" && located.point !== null) {
    const row = root.querySelector(`.insp-section tr[data-ring="1"][data-key="${located.point}"]`);
    input = row && row.querySelector("input");
    host = row && row.querySelector(".c-off");
  } else if (located.field === "curve") {
    const top = root.querySelector(".insp-section .curve-top");
    input = top && top.querySelector("input");
    host = top;
  } else if (located.field === "vt" || located.field === "ratio") {
    const rows = [...root.querySelectorAll(".oc-section .oc-row")];
    const want = located.field === "vt" ? "Target voltage" : "OC ratio";
    const row = rows.find((r) => (r.querySelector(".field-label") || {}).textContent === want);
    input = row && row.querySelector("input");
    host = row;
  }
  if (input) {
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("data-plan-invalid", "1");
    input.classList.add(ERR_CLASS);
    if (host) host.append(note(located.message));
    return input;
  }
  const panel = root.querySelector("#tune-panel");
  if (panel && !host) {
    const banner = note(located.message);
    banner.classList.add("plan-error-banner");
    panel.prepend(banner);
    return banner;
  }
  return tile;
}

// ---- DOM: the drawer ------------------------------------------------------------------------------------------------------------------------------
let stylePromise = null;
function ensureStyle() {
  if (stylePromise) return stylePromise;
  stylePromise = new Promise((resolve) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = new URL("../../styles/apply.css", import.meta.url).href;
    link.addEventListener("load", () => resolve());
    link.addEventListener("error", () => resolve());
    document.head.append(link);
  });
  return stylePromise;
}

const pill = (text, state) => h("span", { class: "apply-pill", dataset: { state }, text });
const SEC_STATUS = { planning: "planning…", review: "ready", stale: "plan is stale", applying: "applying", done: "applied", failed: "not applied", cancelled: "cancelled", skipped: "skipped" };
const SEC_PILL = { planning: "queued", review: "queued", stale: "mismatch", applying: "writing", done: "ok", failed: "mismatch", cancelled: "interrupted", skipped: "not_started" };

/**
 * Mount the apply drawer. ctx = {store, bridge, toast, drawer} (the app's ctx). Returns the controller plus {dispose}.
 * `kinds`/`doc` are injectable for tests.
 */
export function createApplyDrawer({ store, bridge, toast, drawer, kinds = KINDS, doc = document }) {
  const say = toast || (() => {});
  const ctl = createApplyController({ store, bridge, toast: say, kinds });
  const disposers = [];
  let ticker = null;
  let built = null; // structural key of what is on screen
  let live = null; // refs updated in place while applying
  let landed = null;
  let observer = null;
  let wantOpen = false; // the drawer is open because of a Review & Apply the user has not dismissed
  let needFocus = false; // focus the safe button once, when the plan is on screen

  const screenRoot = () => doc.getElementById("screen") || doc.body;

  function land(located) {
    landed = located || null;
    if (!located || !located.unit) return;
    const unit = located.unit;
    if (unit.startsWith("turbo")) patchTune(store, { subtab: "turbo" });
    else patchTune(store, { subtab: "cores", selection: [unit], anchor: unit });
    if (store.get().screen !== "tune") setScreen(store, "tune");
    const el = landFieldError(screenRoot(), located);
    if (el && el.focus && el.tagName === "INPUT") el.focus({ preventScroll: false });
  }

  function watchLanded() {
    if (typeof MutationObserver === "undefined" || observer) return;
    let queued = false;
    observer = new MutationObserver(() => {
      if (!landed || queued) return;
      queued = true;
      queueMicrotask(() => {
        queued = false;
        if (landed && !screenRoot().querySelector(".field-error-note")) {
          observer.disconnect();
          landFieldError(screenRoot(), landed);
          observer.observe(screenRoot(), { childList: true, subtree: true });
        }
      });
    });
    observer.observe(screenRoot(), { childList: true, subtree: true });
  }

  function dropLanded() {
    if (!landed) return;
    landed = null;
    clearFieldError(screenRoot());
  }

  // ---- body ----
  function sectionNode(sec, i, a) {
    const plan = sec.plan;
    const node = h("section", { class: "apply-section", dataset: { kind: sec.kind, status: sec.status, index: i } });
    const head = h("header", { class: "apply-sec-head" }, h("h3", { text: sec.label }), pill(SEC_STATUS[sec.status] || sec.status, SEC_PILL[sec.status] || "queued"));
    head.firstChild.id = "apply-sec-title-" + i;
    node.append(head);
    if (!plan) {
      if (sec.status === "planning") node.append(h("p", { class: "muted", text: "Planning (no hardware is touched)…" }));
      if (sec.error) node.append(h("p", { class: "apply-error", role: "alert", text: sec.error.message || sec.error.code }));
      return node;
    }
    const custom = kinds[sec.kind] && kinds[sec.kind].view ? kinds[sec.kind].view(plan) : null;
    const rows = custom ? [] : planRows(plan);
    const changed = custom ? custom.units : rows.filter((r) => r.changes > 0);
    const writes = planWrites(plan);
    const finished = ["done", "failed", "cancelled", "stale"].includes(sec.status);
    const lead = h("p", { class: "apply-lead" },
      h("strong", { text: changed.length ? plural(changed.length, "unit") + " change" + (changed.length === 1 ? "s" : "") : "Nothing changes" }),
      h("span", { class: "muted", text: changed.length ? ": " + changed.map((r) => r.unit).join(", ") : "" }));
    node.append(lead);
    // progress, result and error sit right under the lead so they are seen without scrolling; the plan details fold away once the run is over
    const prog = h("progress", { class: "apply-bar", max: 1, value: 0, hidden: sec.status !== "applying", "aria-label": "Apply progress" });
    const progText = h("p", { class: "apply-progress muted", role: "status", hidden: sec.status !== "applying" });
    const top = h("div", { class: "apply-top" }, prog, progText);
    if (sec.status === "done" && sec.summary) top.append(resultNode(sec.summary, custom !== null));
    if (sec.error && sec.status !== "applying") top.append(errorNode(sec));
    const vc = isObj(plan.vccia) && isNum(plan.vccia.predicted_v) ? plan.vccia : null;
    if (vc) {
      const by = isObj(vc.set_by) && vc.set_by.unit ? "set by " + vc.set_by.unit + (isNum(vc.set_by.ratio) ? " at " + vc.set_by.ratio + TIMES : "") + (vc.active_cores === "all" ? " with every core loaded" : "") : "";
      node.append(h("p", { class: "apply-vccia", text: "Predicted VccIA ≈ " + vc.predicted_v.toFixed(2) + " V" + (by ? ", " + by : "") + (isNum(vc.before_v) ? ", now ≈ " + vc.before_v.toFixed(2) + " V" : "") + (vc.label ? " (" + vc.label + ")" : "") + ". Lowering a unit that does not set the maximum does not lower Vcore." }));
    }
    const ops = isNum(plan.ops_needed) ? plan.ops_needed : null;
    node.append(h("p", { class: "apply-ops", text: custom ? writes + " write" + (writes === 1 ? "" : "s") + " to root sysfs files, one at a time, each read back. Runtime values: a reboot restores the firmware / kernel defaults." : "Each request is a WRMSR, sent one transaction at a time and read back. " + writes + " write" + (writes === 1 ? "" : "s") + (isNum(plan.reads) ? " + " + plan.reads + " read-only requests" : "") + (ops !== null ? " · " + ops + " operations" : "") + (isNum(plan.ops_left) ? " of the " + plan.ops_left + " left this boot" : "") + "." }));
    if (plan.words_assumed === true) node.append(h("p", { class: "apply-warn-line", text: "Some words were not read in this session, so the plan assumed their current values. Read the CPU state first for a plan based on what the CPU holds." }));
    const warns = planWarnings(plan);
    if (warns.length) {
      const ul = h("ul", { class: "apply-warnings", "aria-label": "Plan warnings" });
      for (const w of warns) ul.append(h("li", { class: w.cls, dataset: { code: w.code } }, w.label ? h("span", { class: "apply-badge", title: w.code, text: w.label }) : null, " ", w.text));
      node.append(h("details", { class: "apply-warnings-box", open: !finished }, h("summary", { text: plural(warns.length, "plan note") + (warns.some((w) => w.cls) ? " (warnings marked)" : "") }), ul));
    }
    // per-unit: before/after and status
    const customStatus = (st) => Object.fromEntries((custom ? custom.units : []).map((u) => {
      const rb = sec.summary ? sec.summary.readback.filter((x) => u.keys.includes(x.key)) : [];
      return [u.unit, st || (sec.status === "done" ? (rb.length && rb.every((x) => x.match === true) ? "ok" : rb.length ? "mismatch" : "unverified") : sec.status === "applying" ? "writing" : ["failed", "cancelled", "stale"].includes(sec.status) && sec.opId ? "unverified" : "queued")];
    }));
    const status = custom ? customStatus() : unitStatuses(plan, sec.progress, sec.status === "done" ? { ok: true, readback: sec.summary ? sec.summary.readback : [], complete: sec.summary ? sec.summary.complete : true } : ["failed", "cancelled", "stale"].includes(sec.status) && sec.opId ? { ok: false } : null);
    const table = h("table", { class: "apply-units", "aria-label": "Units: before and after" }, h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Unit" }), h("th", { scope: "col", text: "Before → after" }), h("th", { scope: "col", text: "Status" }))));
    const body = h("tbody");
    const statusCells = new Map();
    for (const u of custom ? custom.units.map((x) => x.unit) : unitOrder(plan)) {
      const r = rows.find((x) => x.unit === u);
      const cu = custom ? custom.units.find((x) => x.unit === u) : null;
      const lines = cu ? cu.lines : r ? r.rows.filter((x) => x.changed) : [];
      const cell = h("td", { class: "apply-status" });
      statusCells.set(u, cell);
      body.append(h("tr", { dataset: { unit: u } }, h("th", { scope: "row", text: u }), h("td", { class: "apply-changes" }, lines.length ? lines.map((x) => h("div", { class: "mono", text: cu ? x : describeRow(x) })) : h("span", { class: "muted", text: u.startsWith("turbo") ? "turbo ratio table (see the Turbo ratios tab)" : "no change" })), cell));
    }
    table.append(body);
    node.append(h("div", { class: "scroll apply-scroll" }, table));
    const setStatus = (st) => {
      for (const [u, cell] of statusCells) {
        const s = st[u] || "queued";
        clear(cell).append(pill(STATUS_TEXT[s] || s, s));
        cell.parentElement.dataset.status = s;
      }
    };
    setStatus(status);
    // ordered writes
    const steps = custom ? custom.steps.map((label) => ({ kind: "step", conditional: false, label })) : stepRows(plan);
    if (steps.length) {
      const ol = h("ol", { class: "apply-steps" });
      for (const s of steps) ol.append(h("li", { dataset: { kind: s.kind }, class: s.conditional ? "conditional" : "" }, h("span", { class: "mono", text: s.label })));
      node.append(h("details", { class: "apply-order", open: !finished }, h("summary", { text: "Ordered write list (" + steps.length + " step" + (steps.length === 1 ? "" : "s") + ", the order they are sent)" }), ol,
        plan.steps_truncated === true ? h("p", { class: "muted", text: "The step list is shortened to fit one message." }) : null));
    }
    live = live || { sections: new Map() };
    live.sections.set(i, { prog, progText, setStatus, plan, statusFn: custom ? () => customStatus("writing") : null });
    node.insertBefore(top, lead.nextSibling);
    if (sec.status === "review" || sec.status === "stale") node.append(h("p", { class: "muted apply-revert", text: custom ? "Revert: Revert to boot values on the tab, or reboot." : "Revert: cold boot the machine, or apply a stored profile. The CPU forgets these words at every cold boot." }));
    return node;
  }

  function errorNode(sec) {
    const e = sec.error;
    const text = e.code === "PLAN_STALE" ? "A word changed since this plan was made, or the plan is gone (plans do not survive a helper restart). Nothing was written. " + (e.message || "")
      : e.code === "CONFIRM_MISMATCH" ? "The helper did not accept the confirmation (used already, or wrong). Nothing was written. " + (e.message || "")
      : e.code === "CANCELLED" ? "Cancelled between transactions. The requests already sent are in the audit log; " + (kinds[sec.kind] && kinds[sec.kind].view ? "some runtime settings may be written, some not. " : "the CPU may hold a mix of old and new words. ") + (e.message || "")
      : (e.message || e.code || "The helper refused.");
    return h("div", { class: "apply-error-box", role: "alert", dataset: { code: e.code || "" } }, h("strong", { text: sec.status === "cancelled" ? "Cancelled" : sec.status === "stale" ? "Plan is stale" : "Not applied" }), h("p", { text }));
  }

  function resultNode(s, runtimeKind = false) {
    const box = h("div", { class: "apply-result", dataset: { ok: String(s.allMatch) } });
    box.append(h("p", { class: "apply-headline" }, pill(resultHeadline(s), s.allMatch ? "ok" : "mismatch")));
    if (!s.complete) {
      const wd = partialWording(runtimeKind, s.stop && s.stop.tripped === true);
      box.append(h("div", { class: "apply-error-box", role: "alert" }, h("strong", { text: wd.title }), h("p", { text: wd.text })));
    } else if (s.mismatches.length) box.append(h("p", { class: "apply-warn-line", text: runtimeKind ? "Do not trust this state: use Revert to boot values, or reboot." : "Do not trust this state: re-apply the accepted profile or cold boot." }));
    if (s.opsUsed !== null) box.append(h("p", { class: "muted", text: s.opsUsed + " mailbox operations used." }));
    for (const u of s.residual) box.append(h("p", { class: "muted", text: "Residual OC point rewritten on " + u + "." }));
    for (const why of s.ringRefused) box.append(h("p", { class: "apply-warn-line", text: "Ring not written: " + why }));
    if (s.cpufreq.length) {
      const t = h("table", { class: "apply-table", "aria-label": "OS frequency caps" }, h("thead", {}, h("tr", {}, ["CPU", "scaling_max_freq before", "after"].map((x) => h("th", { scope: "col", text: x })))));
      t.append(h("tbody", {}, s.cpufreq.map((x) => h("tr", {}, h("td", { class: "num", text: x.cpu }), h("td", { class: "num", text: (x.before_khz / 1e6).toFixed(2) + " GHz" }), h("td", { class: "num", text: (x.after_khz / 1e6).toFixed(2) + " GHz" })))));
      box.append(h("details", { class: "apply-cpufreq" }, h("summary", { text: "OS frequency cap of " + plural(s.cpufreq.length, "CPU") + " followed the new ratio" }), t));
    }
    if (s.readback.length) {
      const t = h("table", { class: "apply-table", "aria-label": "Read-back" }, h("thead", {}, h("tr", {}, ["Unit", "Point", "CPU", "Expected", "Actual", "Result"].map((x) => h("th", { scope: "col", text: x })))));
      t.append(h("tbody", {}, s.readback.map((x) => {
        const c = readbackCells(x);
        return h("tr", { class: x.match === true ? "" : "is-bad", dataset: { match: String(x.match === true) } },
          h("td", { text: c.unit }), h("td", { text: c.point }), h("td", { class: "num", text: c.cpu }),
          h("td", { class: "mono", text: c.expected }), h("td", { class: "mono", text: c.actual }), h("td", {}, pill(x.match === true ? "match" : "mismatch", x.match === true ? "ok" : "mismatch")));
      })));
      box.append(h("details", { class: "apply-readback", open: !s.allMatch }, h("summary", { text: "Read-back table (" + s.total + " words)" }), h("div", { class: "scroll apply-scroll" }, t)));
    }
    return box;
  }

  function footer(a) {
    const nodes = [];
    const stale = a.sections.some((s) => s.status === "stale") || (a.state === "review" && /Re-plan/.test(ctl.gate()));
    const close = h("button", { type: "button", class: "btn", id: "apply-close", text: a.state === "done" || a.state === "failed" ? "Close" : "Close (keeps the plan)", on: { click: () => closeDrawer(store) } });
    if (a.state === "applying") {
      const note = h("span", { class: "muted apply-cancel-note", text: "Cancel takes effect between transactions: the request being written finishes first." });
      const cancelling = a.sections.some((s) => s.cancelling);
      nodes.push(note, h("button", { type: "button", class: "btn", id: "apply-cancel", text: cancelling ? "Cancelling…" : "Cancel apply", props: { disabled: cancelling }, on: { click: () => void ctl.cancel() } }));
      return nodes;
    }
    nodes.push(close);
    if (stale || a.state === "failed" && a.sections.some((s) => s.status === "stale")) nodes.push(h("button", { type: "button", class: "btn primary", id: "apply-replan", text: "Re-plan", title: "Make a fresh plan for the same request", on: { click: () => void ctl.replan() } }));
    if (a.state === "review") {
      const why = ctl.gate();
      const writes = a.sections.reduce((n, s) => n + (s.plan ? planWrites(s.plan) : 0), 0);
      if (!stale) nodes.push(h("span", { class: "muted apply-why", id: "apply-why", role: "status", text: why || "Ready: this sends " + plural(writes, "write") + ". Its confirmation code is single-use." }));
      nodes.push(h("button", { type: "button", class: "btn primary", id: "apply-go", text: "Apply " + plural(writes, "write"), props: { disabled: why !== "" }, title: why || "Sends the plan(s): one WRMSR transaction at a time, each read back", on: { click: () => void ctl.apply() } }));
    }
    return nodes;
  }

  function structKey(a) {
    return [a.state, a.seq, a.error && a.error.message, a.sections.map((s) => [s.status, s.plan && s.plan.plan_id, s.cancelling, s.error && s.error.code, s.summary ? 1 : 0].join(":")).join("|")].join("#");
  }

  function titleOf(a) {
    if (a.state === "applying") return "Applying…";
    if (a.state === "done") return "Applied";
    if (a.state === "failed") return a.sections.some((s) => s.status === "stale") ? "Plan is stale" : a.sections.some((s) => s.status === "cancelled") ? "Cancelled" : a.sections.some((s) => s.summary) ? "Applied, with problems" : "Not applied";
    return "Review & Apply";
  }

  function render() {
    const a = store.get().apply;
    if (a.state === "idle") return;
    const key = structKey(a);
    if (key !== built) {
      built = key;
      live = null;
      const nodes = [];
      if (a.state === "planning" && a.sections.every((s) => !s.plan)) nodes.push(h("p", { class: "muted", id: "apply-planning", text: "Planning (no hardware is touched)…" }));
      if (a.error) {
        const err = h("div", { class: "apply-error-box", id: "apply-plan-error", role: "alert", dataset: { unit: a.error.located && a.error.located.unit || "" } }, h("strong", { text: a.error.located && a.error.located.unit ? "Not planned: " + a.error.located.unit : "Not planned" }), h("p", { text: a.error.message }));
        if (a.error.located && a.error.located.unit) err.append(h("p", { class: "muted", text: "The field is marked in the editor: change the value and use Review & Apply again." }));
        nodes.push(err);
      }
      if (!(a.state === "failed" && a.error && a.sections.every((s) => !s.plan))) a.sections.forEach((s, i) => nodes.push(sectionNode(s, i, a)));
      const had = doc.activeElement && drawer.footer.contains(doc.activeElement) ? doc.activeElement.id : "";
      drawer.setContent(nodes, footer(a));
      if (had) drawer.focus("#" + had); // a re-render must not drop the focus of the button the user is on
      // opened by the user's Review & Apply only: a drawer the user closed (Esc, X) stays closed while the apply goes on; the end of the run is a toast then
      if (wantOpen) openDrawer(store, titleOf(a));
      else store.update((st) => ({ drawer: { ...st.drawer, title: titleOf(a) } }));
      if (wantOpen && needFocus && a.state !== "planning") {
        needFocus = false;
        drawer.focus("#apply-close"); // Enter must never be one keystroke from a write
      }
    }
    refreshProgress();
  }

  function refreshProgress() {
    const a = store.get().apply;
    if (!live) return;
    const elapsed = a.startedAt !== null ? performance.now() - a.startedAt : 0;
    a.sections.forEach((s, i) => {
      const l = live.sections.get(i);
      if (!l || s.status !== "applying") return;
      const p = s.progress;
      l.prog.max = Math.max(1, p ? p.of : 1);
      l.prog.value = p ? Math.min(p.step, l.prog.max) : 0;
      l.progText.textContent = progressText(p, elapsed);
      l.setStatus(l.statusFn ? l.statusFn() : unitStatuses(l.plan, p, null));
    });
  }

  // the Apply gate also depends on the draft (outdated plan), the link and the clock
  function refreshGate() {
    const a = store.get().apply;
    if (a.state !== "review") return;
    const go = drawer.footer.querySelector("#apply-go");
    const why = ctl.gate();
    const needsReplan = /Re-plan/.test(why);
    if (!go) return;
    go.disabled = why !== "";
    const note = drawer.footer.querySelector("#apply-why");
    if (note) note.textContent = why || note.textContent;
    if (needsReplan && !drawer.footer.querySelector("#apply-replan")) {
      built = null;
      render();
    }
  }

  // ---- events ----
  const onReview = (e) => {
    // G-213: "tune:review" may carry detail.requests (e.g. [{kind: "tune", intent: {profile: "<name>"}, label}]): the Profiles screen opens the same drawer for a whole-state intent.
    // Such a plan error is not a field of the Tune editor, so it is never landed on one.
    const requests = e && e.detail && Array.isArray(e.detail.requests) && e.detail.requests.length ? e.detail.requests : null;
    // the pending bar's placeholder (app.js) sets the drawer content right after this event is dispatched: render on a later tick
    wantOpen = true;
    needFocus = true;
    queueMicrotask(async () => {
      await ensureStyle();
      dropLanded();
      const a = store.get().apply;
      if (ctl.isBusy() || a.state === "applying") {
        built = null;
        render();
        return;
      }
      patch0({ state: "planning", sections: [], error: null });
      built = null;
      render();
      const state = await ctl.review(requests || ["tune"]); // every state change renders through the store watch
      const err = store.get().apply.error;
      if (state === "failed" && err && err.located && !requests) land(err.located);
    });
  };
  const patch0 = (p) => store.update((s) => ({ apply: { ...s.apply, ...p } }));
  let drawerWasOpen = store.get().drawer.open;
  disposers.push(store.subscribe((st) => {
    if (drawerWasOpen && !st.drawer.open) wantOpen = false;
    drawerWasOpen = st.drawer.open;
  }));
  store.events.addEventListener("tune:review", onReview);
  disposers.push(() => store.events.removeEventListener("tune:review", onReview));
  disposers.push(store.watch((s) => s.apply, render));
  disposers.push(store.watch((s) => s.tune.draft, () => {
    if (landed) dropLanded(); // an edit answers the error
    refreshGate();
  }));
  disposers.push(store.watch((s) => s.link.state, refreshGate));
  ticker = setInterval(() => {
    const a = store.get().apply;
    if (a.state === "applying") refreshProgress();
    else if (a.state === "review") refreshGate();
  }, 500);
  watchLanded();

  // a finished apply with the drawer closed: say so (the window may not even be focused)
  let lastState = "idle";
  disposers.push(store.watch((s) => s.apply.state, (st) => {
    const prev = lastState;
    lastState = st;
    if (prev !== "applying" || store.get().drawer.open) return;
    const a = store.get().apply;
    const done = a.sections.filter((s) => s.summary);
    const text = st === "done" ? "Applied: " + done.map((s) => resultHeadline(s.summary)).join("; ") + "." : "Apply stopped: " + (a.sections.find((s) => s.error) || { error: { message: "see the drawer" } }).error.message;
    say(st === "done" ? "ok" : "warn", text);
  }));

  return {
    controller: ctl,
    dispose() {
      for (const d of disposers.splice(0)) d();
      if (ticker) clearInterval(ticker);
      if (observer) observer.disconnect();
    },
  };
}
