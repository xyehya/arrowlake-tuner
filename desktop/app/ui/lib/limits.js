// limits.js: the limits of a tune.read result with their sources (G-207). Ported from web/app.js normLimits / normHw / limitLine (G-133, D-038/D-039).
//
// Two layers, never invented here: `limits` = the operator's policy (/etc/arltuner/policy.toml) as the helper resolved it, and `hw_limits` = what the CPU itself reports.
// The effective ceiling is the stricter of the two; every limit says which one binds. A limit the policy does not state is `null`: the control is disabled with a
// reason, never given a guess. The ring has no policy window in the read (the helper checks it at plan time), so it only gets the CPU/encoding range.
import { isNum, MINUS, TIMES } from "./format.js";

export const POLICY_FILE = "/etc/arltuner/policy.toml";
/** Grid of the UI controls (not hardware limits): mV step of the offset field, V floor and step of the adaptive target (5 mV grid, PLAN §4.2). */
export const GRID = { offsetStep: 5, vtMin: 0.9, vtStep: 0.005 };
export const NO_LIMITS = "The helper did not report the policy limits (update the helper), so the controls are disabled.";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

/** `limits` -> {P|E: {offMin, offMax, vtMax, ratioMax, vtSrc, ratioSrc}} or null. A null number is a limit the policy does not state (or an "auto" that could not be resolved). */
export function normLimits(l) {
  if (!isObj(l) || !isObj(l.offset_mv)) return null;
  const out = {};
  const src = isObj(l.source) ? l.source : {};
  for (const k of ["P", "E"]) {
    const o = l.offset_mv[k];
    if (!isObj(o) || !isNum(o.min) || !isNum(o.max)) return null;
    const pick = (obj) => (isObj(obj) && isNum(obj[k]) ? obj[k] : null);
    const why = (obj) => (isObj(obj) && typeof obj[k] === "string" ? obj[k] : null);
    out[k] = { offMin: o.min, offMax: o.max, vtMax: pick(l.v_target_max_v), ratioMax: pick(l.ratio_max), vtSrc: why(src.v_target_max), ratioSrc: why(src.ratio_max) };
  }
  return out;
}

/** `hw_limits` -> {P|E: {ratioOc, vtMaxV, off:[lo,hi]|null}} or null (absent on a helper without it, D-039). */
export function normHw(h) {
  if (!isObj(h)) return null;
  const out = {};
  for (const k of ["P", "E"]) {
    const o = h[k];
    if (!isObj(o)) continue;
    out[k] = {
      ratioOc: isNum(o.ratio_max_oc) ? o.ratio_max_oc : null,
      vtMaxV: isNum(o.v_target_max_mv) ? o.v_target_max_mv / 1000 : null,
      off: Array.isArray(o.offset_mv) && o.offset_mv.length === 2 && o.offset_mv.every(isNum) ? o.offset_mv.slice() : null,
    };
  }
  return Object.keys(out).length ? out : null;
}

const polSource = (src) => (src === "auto" ? "policy (auto: the CPU's own limit)" : "policy");

/** The strictest of {value, source} candidates that carry a number, or null. Ties keep the first (the policy). */
function strictest(cands, pick) {
  let best = null;
  for (const c of cands) if (isNum(c.value) && (best === null || pick(c.value, best.value) !== best.value)) best = c;
  return best;
}

/**
 * Everything the editors need to know about the limits of one kind ("P" | "E" | "R"). `known` is false without policy limits (P/E) -> editors disabled.
 *   offset {min,max,source} | null, vt {max,source,policy,cpu} | null, ratio {max,source,policy,cpu} | null, plus `policy`/`cpu` raw for the hint text.
 */
export function limitsFor(model, kind) {
  const pol = model && model.limits ? model.limits[kind] || null : null;
  const hwKind = kind === "R" ? "P" : kind;
  const hw = model && model.hw ? model.hw[hwKind] || null : null;
  const out = { kind, known: false, offset: null, vt: null, ratio: null, policy: pol, cpu: hw, note: null };
  if (kind === "R") {
    // The ring: the read carries no policy window for it. Offer the V/F word's own range; the helper applies the ring's policy entry at plan time.
    if (hw && hw.off) out.offset = { min: hw.off[0], max: hw.off[1], source: "CPU / V/F word range (the ring's policy window is checked when you review)" };
    out.note = "the read carries no policy window for the ring";
    return out;
  }
  if (!pol) return out;
  out.known = true;
  const lo = strictest([{ value: pol.offMin, source: polSource() }, { value: hw && hw.off ? hw.off[0] : null, source: "CPU" }], Math.max);
  const hi = strictest([{ value: pol.offMax, source: polSource() }, { value: hw && hw.off ? hw.off[1] : null, source: "CPU" }], Math.min);
  if (lo && hi) out.offset = { min: lo.value, max: hi.value, source: lo.source === hi.source ? lo.source : lo.source + " / " + hi.source };
  const vt = strictest([{ value: pol.vtMax, source: polSource(pol.vtSrc) }, { value: hw ? hw.vtMaxV : null, source: "CPU" }], Math.min);
  if (vt) out.vt = { max: vt.value, source: vt.source, policy: pol.vtMax, cpu: hw ? hw.vtMaxV : null };
  const ra = strictest([{ value: pol.ratioMax, source: polSource(pol.ratioSrc) }, { value: hw ? hw.ratioOc : null, source: "CPU" }], Math.min);
  if (ra) out.ratio = { max: ra.value, source: ra.source, policy: pol.ratioMax, cpu: hw ? hw.ratioOc : null };
  return out;
}

/** The offset window {min,max,source} of a kind, or null (control disabled). */
export function offsetWindow(model, kind) {
  return limitsFor(model, kind).offset;
}

/** `mv` rounded to a whole mV (the intent takes whole mV) and clamped into the window; null when the kind has no window (nothing may be set). */
export function clampOffset(model, kind, mv) {
  const w = offsetWindow(model, kind);
  if (!w || !isNum(mv)) return null;
  return Math.min(Math.max(Math.round(mv), w.min), w.max);
}

/** The adaptive-target grid {min,max,step,source} of a kind, or null without a stated ceiling. */
export function vtWindow(model, kind) {
  const v = limitsFor(model, kind).vt;
  return v ? { min: GRID.vtMin, max: v.max, step: GRID.vtStep, source: v.source } : null;
}

/** Snap a voltage onto the 5 mV grid from the floor (D-035: the plan never sends a zero target). */
export function snapGrid(v) {
  return Math.round(GRID.vtMin * 1000 + Math.round((v - GRID.vtMin) / GRID.vtStep) * GRID.vtStep * 1000) / 1000;
}

const sgn = (x) => (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x);

/** The one-line provenance of a limit: your policy, editable, next to what the CPU allows. what: "offset" | "vt" | "ratio". */
export function limitLine(model, kind, what) {
  const l = limitsFor(model, kind);
  if (kind === "R") return what === "offset" && l.offset ? "Limit: " + sgn(l.offset.min) + "…" + sgn(l.offset.max) + " mV (" + l.offset.source + ")." : "";
  if (!l.known) return "";
  const p = l.policy, hw = l.cpu;
  let mine, cpu = null;
  if (what === "offset") {
    mine = sgn(p.offMin) + "…" + sgn(p.offMax) + " mV";
    if (hw && hw.off) cpu = sgn(hw.off[0]) + "…" + sgn(hw.off[1]) + " mV";
  } else if (what === "vt") {
    mine = isNum(p.vtMax) ? "up to " + p.vtMax.toFixed(3) + " V" : null;
    if (hw && isNum(hw.vtMaxV)) cpu = "up to " + hw.vtMaxV.toFixed(3) + " V";
  } else {
    mine = isNum(p.ratioMax) ? "up to " + p.ratioMax + TIMES : null;
    if (hw && isNum(hw.ratioOc)) cpu = "OC ratios up to " + hw.ratioOc + TIMES;
  }
  return "Limit: " + (mine || "none stated") + " from your policy (" + POLICY_FILE + ")" + (cpu ? " — editable; your CPU allows " + cpu + "." : " — editable in " + POLICY_FILE + ".");
}
