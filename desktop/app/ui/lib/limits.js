// limits.js: the limits of a tune.read result with their sources (G-207, rebuilt on `cpu_limits` in G-217 / D-049).
//
// The only limits are the CPU's own, as the helper reports them in `cpu_limits` (lib/cpulimits.js): the max voltage (OC mailbox 0x28), the OC ratio limit (0x01), the 11-bit offset field.
// A limit the read did not carry is `null` and the control that needs it says "Read CPU state first": a positive offset, a raise of a ratio or a target voltage is disabled with that reason,
// a negative offset stays editable (it cannot exceed any ceiling). Nothing here is invented; there is no operator policy in the GUI.
import { isNum } from "./format.js";
import { READ_FIRST } from "./cpulimits.js";

/** Grid of the UI controls (not hardware limits): mV step of the offset field, V floor and step of the adaptive target (5 mV grid, PLAN §4.2). */
export const GRID = { offsetStep: 5, vtMin: 0.9, vtStep: 0.005 };
export const NO_LIMITS = "The helper did not report the CPU limits (update the helper), so the controls are disabled.";

const SRC = { offset: "CPU (11-bit V/F offset field)", vt: "CPU max voltage (mailbox 0x28)", ratio: "CPU OC ratio limit (mailbox 0x01)" };

/**
 * Everything the editors need to know about the limits of one kind ("P" | "E" | "R"; the ring has its own entry in `cpu_limits`).
 *   known  the read carried `cpu_limits` with the offset field range (false on a helper without them -> editors disabled with NO_LIMITS)
 *   read   the kind's voltage ceiling (0x28) was read: positive offsets and target voltages are allowed
 *   offset {min, max, source, positive: reason | null} | null    the window; `max` is 0 until the ceiling is read
 *   vt {max, source} | null, ratio {max, source} | null           null until read; `vtWhy` / `ratioWhy` say what to do (READ_FIRST)
 */
export function limitsFor(model, kind) {
  const cl = model ? model.cpuLimits : null;
  const k = cl && cl.kinds ? cl.kinds[kind === "R" ? "ring" : kind] || null : null;
  const out = { kind, known: false, read: false, offset: null, vt: null, ratio: null, cpu: k, vtWhy: null, ratioWhy: null, note: null };
  if (!cl || !cl.offsetMv) return out;
  out.known = true;
  const vRead = !!k && isNum(k.maxMv);
  out.read = vRead;
  out.offset = { min: cl.offsetMv[0], max: vRead ? cl.offsetMv[1] : Math.min(cl.offsetMv[1], 0), source: SRC.offset, positive: vRead ? null : READ_FIRST + ": positive offsets need the CPU voltage ceiling (mailbox 0x28)" };
  if (vRead) out.vt = { max: k.maxMv / 1000, source: SRC.vt };
  else out.vtWhy = READ_FIRST + ": the CPU voltage ceiling (mailbox 0x28) has not been read";
  if (k && isNum(k.maxRatio)) out.ratio = { max: k.maxRatio, source: SRC.ratio };
  else out.ratioWhy = READ_FIRST + ": the OC ratio limit (mailbox 0x01) has not been read";
  return out;
}

/** The offset window {min,max,source,positive} of a kind, or null (control disabled). */
export function offsetWindow(model, kind) {
  return limitsFor(model, kind).offset;
}

/** `mv` rounded to a whole mV and clamped into the window; null when the kind has no window (nothing may be set). A positive value is clamped to 0 until the CPU voltage ceiling is read. */
export function clampOffset(model, kind, mv) {
  const w = offsetWindow(model, kind);
  if (!w || !isNum(mv)) return null;
  return Math.min(Math.max(Math.round(mv), w.min), w.max);
}

/** The adaptive-target grid {min,max,step,source} of a kind, or null until the CPU voltage ceiling is read. */
export function vtWindow(model, kind) {
  const v = limitsFor(model, kind).vt;
  return v ? { min: GRID.vtMin, max: v.max, step: GRID.vtStep, source: v.source } : null;
}

/** Snap a voltage onto the 5 mV grid from the floor (D-035: the plan never sends a zero target). */
export function snapGrid(v) {
  return Math.round(GRID.vtMin * 1000 + Math.round((v - GRID.vtMin) / GRID.vtStep) * GRID.vtStep * 1000) / 1000;
}
