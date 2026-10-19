// lib/rtstate.js: the state of the runtime controls (G-217): the store slice `rt` = what power.read / runtime.read last said and the edits not yet applied, and the pure functions that turn
// slice + reads into the intents of lib/intent.js. No DOM, no helper calls (the views call the bridge). Runtime settings: a reboot restores the firmware / kernel values.
//   rt = { edits: {power: {pl1_w?, pl2_w?, tau_s?}, caps: {<unit id>: ratio}, uncore: {min_ratio?, max_ratio?}},
//          power:   {state idle|reading|ready|error, result, error, at},      // power.read
//          runtime: {state idle|reading|ready|error, result, error, at} }     // runtime.read
// A cap is edited per UNIT (P0..P7, EC0..EC3); the intent names every online CPU of an E cluster (they run one ratio; the helper expands it anyway and refuses two different asks).
import { isNum } from "./format.js";
import { powerIntent, runtimeIntent } from "./intent.js";

export const emptyEdits = () => ({ power: {}, caps: {}, uncore: {} });
const emptyRead = () => ({ state: "idle", result: null, error: null, at: null });
export const initialRt = () => ({ edits: emptyEdits(), power: emptyRead(), runtime: emptyRead() });

export function ensureRt(store) {
  if (!store.get().rt) store.set({ rt: initialRt() });
  return store.get().rt;
}
export function patchRt(store, patch) {
  ensureRt(store);
  store.update((s) => ({ rt: { ...s.rt, ...patch } }));
}
export function setRead(store, key, patch) {
  const rt = ensureRt(store);
  patchRt(store, { [key]: { ...rt[key], ...patch } });
}
/** `fn(edits) -> edits` (a new object); the slice is replaced only when it changed. */
export function editRt(store, fn) {
  const rt = ensureRt(store);
  const next = fn(rt.edits);
  if (next !== rt.edits) patchRt(store, { edits: next });
}
export const discardRt = (store) => patchRt(store, { edits: emptyEdits() });

/** The values in force as the file (powercap) says, else the MSR: {pl1_w, pl2_w, tau_s} (numbers or null). */
export function powerCurrent(result) {
  const pc = result && result.powercap ? result.powercap : {};
  const ms = result && result.msr && result.msr.limit ? result.msr.limit : {};
  const pick = (k) => (isNum(pc[k]) ? pc[k] : isNum(ms[k]) ? ms[k] : null);
  return { pl1_w: pick("pl1_w"), pl2_w: pick("pl2_w"), tau_s: pick("tau_s") };
}

/** True when the file and the MSR disagree (more than rounding): shown next to the current value. */
export function powerDisagrees(result, key) {
  const pc = result && result.powercap ? result.powercap[key] : null;
  const ms = result && result.msr && result.msr.limit ? result.msr.limit[key] : null;
  return isNum(pc) && isNum(ms) && Math.abs(pc - ms) > (key === "tau_s" ? Math.max(0.5, pc * 0.02) : 0.5);
}

/** Why the power controls cannot be used right now ("" = they can). */
export function powerDisabledReason(result) {
  if (!result) return "Read the power limits first";
  if (result.available === false) return result.note || "the power limits are not available on this machine";
  if (result.locked === true) return result.read_only_reason || "the power limits are locked by firmware (MSR 0x610 lock bit)";
  if (result.writable === false) return result.read_only_reason || result.note || "the power limits cannot be written here";
  return "";
}

/** -> {intent: {power}} | {intent: null} | {error} for the pending power edits against what power.read said. */
export function powerRequest(rt) {
  return powerIntent(rt.edits.power, powerCurrent(rt.power.result));
}

/** The cap rows of a runtime.read: [{unit, kind, cpus: [all], online: [online cpus], offline: n, capKhz, capRatio (G-233: the ratio the cores really run, whole), maxRatio (the same for cpuinfo_max_freq; null = unknown), minKhz, maxKhz}]
 *  in unit order (P0..P7, EC0..). An E-core's kernel kHz / 100000 is up to one ratio high, so the helper's `cap_ratio_effective` / `max_ratio_effective` are used; kHz / 100000 only for a helper that sends none. */
export function capRows(result) {
  const rows = result && Array.isArray(result.caps) ? result.caps : [];
  const by = new Map();
  for (const r of rows) {
    if (!r || typeof r.unit !== "string" || !isNum(r.cpu)) continue;
    const g = by.get(r.unit) || { unit: r.unit, kind: r.kind, cpus: [], online: [], capKhz: [], capEff: [], maxRatio: null, minKhz: null, maxKhz: null };
    g.cpus.push(r.cpu);
    if (r.online !== false) {
      g.online.push(r.cpu);
      const f = r.cpufreq || {};
      if (isNum(f.cap_khz)) {
        g.capKhz.push(f.cap_khz);
        g.capEff.push(isNum(f.cap_ratio_effective) ? f.cap_ratio_effective : Math.round(f.cap_khz / 100000));
      }
      if (isNum(f.max_ratio_effective)) g.maxRatio = Math.min(g.maxRatio ?? Infinity, f.max_ratio_effective);
      if (isNum(f.min_khz)) g.minKhz = Math.max(g.minKhz ?? 0, f.min_khz);
      if (isNum(f.cpuinfo_max_khz)) g.maxKhz = Math.min(g.maxKhz ?? Infinity, f.cpuinfo_max_khz);
    }
    by.set(r.unit, g);
  }
  return [...by.values()].map((g) => {
    const khz = g.capKhz.length ? Math.max(...g.capKhz) : null;
    const ratios = g.capEff;
    return { ...g, capKhz: khz, capRatio: ratios.length ? Math.max(...ratios) : null, capMin: ratios.length ? Math.min(...ratios) : null, mixedCap: new Set(ratios).size > 1, offline: g.cpus.length - g.online.length };
  });
}

/** The uncore state of a runtime.read: {min, max, initialMin, initialMax} in whole ratios, or null. */
export function uncoreState(result) {
  const u = result && result.uncore;
  if (!u || !isNum(u.min_ratio) || !isNum(u.max_ratio)) return null;
  return { min: Math.round(u.min_ratio), max: Math.round(u.max_ratio), initialMin: isNum(u.initial_min_ratio) ? Math.round(u.initial_min_ratio) : null, initialMax: isNum(u.initial_max_ratio) ? Math.round(u.initial_max_ratio) : null };
}

/** -> {intent: {caps?, uncore?}} | {intent: null} | {error}. Values equal to what runtime.read says are dropped (the helper would answer NOTHING_TO_DO). */
export function runtimeRequest(rt) {
  const result = rt.runtime.result;
  const rows = capRows(result);
  const caps = {};
  for (const [unit, ratio] of Object.entries(rt.edits.caps)) {
    const g = rows.find((r) => r.unit === unit);
    if (!g) return { error: "no CPU of " + unit + " in the last runtime read" };
    if (!g.online.length) return { error: unit + " has no online CPU" };
    if (g.capRatio === ratio && !g.mixedCap) continue;
    for (const cpu of g.online) caps[String(cpu)] = ratio;
  }
  const cur = uncoreState(result);
  const uncore = {};
  for (const [k, v] of Object.entries(rt.edits.uncore)) {
    if (!isNum(v)) continue;
    if (cur && ((k === "min_ratio" && cur.min === v) || (k === "max_ratio" && cur.max === v))) continue;
    uncore[k] = v;
  }
  const lo = uncore.min_ratio ?? (cur ? cur.min : null);
  const hi = uncore.max_ratio ?? (cur ? cur.max : null);
  if (isNum(lo) && isNum(hi) && lo > hi && Object.keys(uncore).length) return { error: "uncore min " + lo + "× is above max " + hi + "×" };
  return runtimeIntent({ caps, uncore });
}

/** How many pending edits each control has (after dropping the ones equal to the read): {power, caps, uncore, total}. */
export function pendingCounts(rt) {
  const p = powerIntent(rt.edits.power, powerCurrent(rt.power.result));
  const r = runtimeRequest(rt);
  const caps = r.intent && r.intent.caps ? new Set(capRows(rt.runtime.result).filter((g) => g.online.some((c) => String(c) in r.intent.caps)).map((g) => g.unit)).size : 0;
  const uncore = r.intent && r.intent.uncore ? Object.keys(r.intent.uncore).length : 0;
  const power = p.intent ? Object.keys(p.intent.power).length : 0;
  return { power, caps, uncore, total: power + caps + uncore };
}
