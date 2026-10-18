// views/tune/runtime-kinds.js: the "power" and "runtime" section kinds of the apply drawer (views/tune/apply.js registerKind) and the helper reads they share (G-217).
//
// WHAT THE DRAWER NEEDS FROM apply.js (apply.js is G-210's file; `tests/apply-hooks.patch` is the ~30-line change, see its header): power.plan / runtime.plan take their fields at the top level
// (ops_power.py _validate_power_plan, ops_runtime.py _validate_runtime_plan; the Rust allowlist rejects {intent: ...}), so a kind may carry
//   planArgs(intent) -> the args of the plan call (default {intent}),   explain(error) -> the refusal text,   view(plan) -> {units: [{unit, keys, lines}], steps: [text]} (a plan that is not tune-shaped).
// Without those hooks apply.js ignores the extra fields and would send {intent: ...}, which the helper refuses (BAD_REQUEST): the Review button of the tabs stays correct, the drawer then says so.
//
// The tabs build their requests with requestReview(): one drawer, V/F section first (when there is a Tune draft), then power, then runtime; applied in that order, stop on first failure.
import { registerKind, KINDS } from "./apply.js";
import { ensureTune } from "../tune.js";
import { pushToast } from "../../store.js";
import { isEmpty as draftIsEmpty } from "../../lib/draft.js";
import { isNum } from "../../lib/format.js";
import { ensureRt, setRead, editRt, patchRt, powerRequest, runtimeRequest, emptyEdits } from "../../lib/rtstate.js";
import { explainRefusal } from "../../lib/cpulimits.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

// ---- reads (shared by the tabs and by afterApply) -------------------------------------------------------------------------------------------------
/** power.read / runtime.read into the slice. power.read is a long op (guarded MSR reads): {id} + a result event. Plain MSR / sysfs reads, never the OC mailbox. */
export async function loadRead(store, bridge, key) {
  const op = key === "power" ? "power.read" : "runtime.read";
  setRead(store, key, { state: "reading", error: null });
  let res;
  try {
    const first = await bridge.call(op);
    res = first.ok && first.pending ? await bridge.wait(first.id) : first;
  } catch (e) {
    res = { ok: false, error: { code: "BRIDGE", message: String((e && e.message) || e) } };
  }
  if (!res.ok) {
    setRead(store, key, { state: "error", error: res.error.message || res.error.code });
    return null;
  }
  setRead(store, key, { state: "ready", result: res.result, error: null, at: new Date().toISOString() });
  return res.result;
}

// ---- display of a plan that is not tune-shaped ----------------------------------------------------------------------------------------------------
export const PL_NAME = { pl1_w: "Processor Core Power Limit (PL1)", pl2_w: "Processor Core Turbo Power Limit (PL2)", tau_s: "Turbo Boost Power Time Window" };
const watts = (uw) => (isNum(uw) ? Math.round(uw / 1e4) / 100 + " W" : "?");
const secs = (us) => (isNum(us) ? Math.round(us / 1e5) / 10 + " s" : "?");
const khzRatio = (khz) => (isNum(khz) ? Math.round(khz / 1e5) + "×" : "?");

/** power.plan result -> {units: [{unit, keys, lines}], steps: [text]}: one row per key the plan writes, the steps in the order the helper sends them. */
export function powerView(plan) {
  const steps = (Array.isArray(plan && plan.steps) ? plan.steps : []).filter(isObj);
  const fmt = (key, v) => (key === "tau_s" ? secs(v) : watts(v));
  return {
    units: steps.map((s) => ({ unit: PL_NAME[s.key] || String(s.key), keys: [s.key], lines: [fmt(s.key, s.before) + " → " + fmt(s.key, s.value)] })),
    steps: steps.map((s) => "write " + (PL_NAME[s.key] || s.key) + " = " + fmt(s.key, s.value) + " (powercap " + s.file + "), then read back the file and MSR 0x610"),
  };
}

/** runtime.plan result -> the same shape: CPUs asking for the same change are one row ("cpu 8, 9, 10, 11"); the plan's own note ("capped at 22×") is shown, never predicted here. */
export function runtimeView(plan) {
  const steps = (Array.isArray(plan && plan.steps) ? plan.steps : []).filter(isObj);
  const groups = [];
  for (const s of steps) {
    if (s.domain === "caps") {
      const sig = [s.before, s.value, s.note || ""].join("|");
      const g = groups.find((x) => x.sig === sig && x.domain === "caps");
      if (g) g.keys.push(s.key);
      else groups.push({ domain: "caps", sig, keys: [s.key], step: s });
    } else groups.push({ domain: s.domain, sig: String(s.key), keys: [s.key], step: s });
  }
  const units = groups.map((g) => {
    const s = g.step;
    if (g.domain === "caps") return { unit: "cpu " + g.keys.map((k) => String(k).replace(/^cpu/, "")).join(", "), keys: g.keys, lines: ["max " + khzRatio(s.before) + " → " + s.ratio + "×" + (s.note ? " (" + s.note + ")" : "")] };
    return { unit: "Uncore " + String(s.key).replace("_ratio", "") + " ratio", keys: g.keys, lines: [khzRatio(s.before) + " → " + khzRatio(s.value)] };
  });
  return {
    units,
    steps: steps.map((s) => (s.domain === "caps" ? "write " + s.file + " = " + s.value + " kHz (" + s.ratio + "×)" : "write " + s.file + " = " + s.value + " kHz") + (s.note ? " · " + s.note : "")),
  };
}

const locateRefusal = (error) => ({ unit: null, field: null, ratio: null, point: null, key: isObj(error && error.detail) && typeof error.detail.key === "string" ? error.detail.key : null, message: explainRefusal(error), check: isObj(error && error.detail) ? error.detail.check || null : null });

function requestOf(build) {
  return (store) => {
    const r = build(ensureRt(store));
    return r.error ? { error: r.error } : { intent: r.intent };
  };
}

function afterOf(key, clear) {
  return async ({ store, bridge, summary }) => {
    if (summary.allMatch) editRt(store, (e) => ({ ...e, ...clear }));
    await loadRead(store, bridge, key);
  };
}

export const POWER_KIND = {
  label: "Power limits (runtime)",
  planOp: "power.plan",
  applyOp: "power.apply",
  planArgs: (intent) => intent,
  request: requestOf(powerRequest),
  locate: locateRefusal,
  explain: explainRefusal,
  view: powerView,
  afterApply: afterOf("power", { power: {} }),
};
export const RUNTIME_KIND = {
  label: "Ratio caps & uncore (runtime)",
  planOp: "runtime.plan",
  applyOp: "runtime.apply",
  planArgs: (intent) => intent,
  request: requestOf(runtimeRequest),
  locate: locateRefusal,
  explain: explainRefusal,
  view: runtimeView,
  afterApply: afterOf("runtime", { caps: {}, uncore: {} }),
};

/** Idempotent: put the two kinds into the drawer's registry. */
export function registerRuntimeKinds() {
  if (!KINDS.power) registerKind("power", POWER_KIND);
  if (!KINDS.runtime) registerKind("runtime", RUNTIME_KIND);
}

// ---- the tabs' Review button ------------------------------------------------------------------------------------------------------------------------
/**
 * Open the apply drawer for the pending runtime edits (+ the Tune V/F draft, when there is one). `only` = [{kind, intent, label}] replaces the edits (a revert).
 * -> the requests it dispatched ([] = nothing to review: a toast says so).
 */
export function requestReview(store, only = null) {
  registerRuntimeKinds();
  const t = ensureTune(store);
  const rt = ensureRt(store);
  let requests;
  if (only) requests = only;
  else {
    requests = [];
    if (t.model && !draftIsEmpty(t.draft)) requests.push("tune");
    const p = powerRequest(rt);
    const r = runtimeRequest(rt);
    for (const [kind, req, label] of [["power", p, "Power limits"], ["runtime", r, "Ratio caps & uncore"]]) {
      if (req.error) {
        pushToast(store, { kind: "warn", text: label + ": " + req.error });
        return [];
      }
      if (req.intent) requests.push({ kind, intent: req.intent, label });
    }
  }
  if (!requests.length) {
    pushToast(store, { kind: "info", text: "Nothing to review: no pending changes." });
    return [];
  }
  if (!store.events) store.events = new EventTarget();
  store.events.dispatchEvent(new CustomEvent("tune:review", { detail: { requests } }));
  return requests;
}

/** A revert request: the helper puts back the values it journaled before the first write of this boot. */
export const revertRequest = (kind) => ({ kind, intent: { revert: true }, label: kind === "power" ? "Power limits: revert to the values of the boot" : "Ratio caps & uncore: revert to the values of the boot" });

export { patchRt, emptyEdits };
