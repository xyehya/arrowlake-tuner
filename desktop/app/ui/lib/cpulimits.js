// lib/cpulimits.js: the CPU's own limits (D-049) as the helper reports them in `cpu_limits` on tune.read / tune.last / power.read / runtime.read (G-226). The GUI shows no operator
// policy: every limit line here names the CPU register or sysfs file the number came from. `null` = the helper did not report it (not read yet): the control says "Read CPU state first".
//   cpu_limits = {P|E|ring: {max_mv, max_ratio, source}, offset_mv: [lo, hi], power: {locked, pl_max_w}, caps_khz: {"<cpu>": [min, max]}, uncore_khz: [min, max]}
// Pure; no DOM. Ratios are 100 MHz bins (cpufreq kHz / 100000).
import { isNum, TIMES, MINUS } from "./format.js";

export const READ_FIRST = "Read CPU state first";
export const BIN_KHZ = 100000;
const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const pair = (a) => (Array.isArray(a) && a.length === 2 && a.every(isNum) && a[0] <= a[1] ? [a[0], a[1]] : null);

/** `cpu_limits` -> {kinds: {P|E|ring: {maxMv, maxRatio, source}}, offsetMv: [lo, hi] | null, power: {locked, plMaxW} | null, capsKhz: {cpu: [min, max]}, uncoreKhz: [min, max] | null} or null. */
export function normCpuLimits(raw) {
  if (!isObj(raw)) return null;
  const kinds = {};
  for (const k of ["P", "E", "ring"]) {
    const o = raw[k];
    if (!isObj(o)) continue;
    kinds[k] = { maxMv: isNum(o.max_mv) ? o.max_mv : null, maxRatio: isNum(o.max_ratio) ? o.max_ratio : null, source: typeof o.source === "string" ? o.source : "" };
  }
  const caps = {};
  if (isObj(raw.caps_khz)) for (const [cpu, w] of Object.entries(raw.caps_khz)) if (pair(w)) caps[cpu] = pair(w);
  const p = isObj(raw.power) ? { locked: raw.power.locked === true, plMaxW: isNum(raw.power.pl_max_w) ? raw.power.pl_max_w : null } : null;
  return { kinds, offsetMv: pair(raw.offset_mv), power: p, capsKhz: caps, uncoreKhz: pair(raw.uncore_khz) };
}

/** The cap window of one CPU in whole ratios {min, max, minKhz, maxKhz} (the bins fully inside the cpufreq range), or null. */
export function capWindow(cl, cpu) {
  const w = cl && cl.capsKhz ? cl.capsKhz[String(cpu)] : null;
  if (!w) return null;
  return { min: Math.ceil(w[0] / BIN_KHZ - 1e-9), max: Math.floor(w[1] / BIN_KHZ + 1e-9), minKhz: w[0], maxKhz: w[1] };
}

/** The uncore ratio window {min, max} or null. */
export function uncoreWindow(cl) {
  const w = cl ? cl.uncoreKhz : null;
  return w ? { min: Math.ceil(w[0] / BIN_KHZ - 1e-9), max: Math.floor(w[1] / BIN_KHZ + 1e-9) } : null;
}

const mhz = (khz) => Math.round(khz / 1000);
const unitsOf = (cl, cpus) => cpus.map((c) => capWindow(cl, c)).filter(Boolean);

/** "cpufreq 800–5700 MHz (cpu0)" for one CPU, or a range over a cluster's CPUs. null when none is reported. */
export function capsLine(cl, cpus) {
  const ws = unitsOf(cl, cpus);
  if (!ws.length) return null;
  const lo = Math.max(...ws.map((w) => w.minKhz));
  const hi = Math.min(...ws.map((w) => w.maxKhz));
  return "cpufreq " + mhz(lo) + "–" + mhz(hi) + " MHz (" + (cpus.length === 1 ? "cpu" + cpus[0] : "cpu" + cpus[0] + "…" + cpus[cpus.length - 1]) + ")";
}

/** "uncore 8×–40× (intel_uncore_frequency initial bounds)" or null. */
export function uncoreLine(cl) {
  const w = uncoreWindow(cl);
  return w ? "uncore " + w.min + TIMES + "–" + w.max + TIMES + " (intel_uncore_frequency initial_min/max_freq_khz)" : null;
}

/** The PL line: field maximum and the lock. null when the limits are not read. */
export function powerLine(cl) {
  const p = cl ? cl.power : null;
  if (!p) return null;
  if (p.locked) return "locked by firmware (MSR 0x610 lock bit): read-only until the next reset";
  return p.plMaxW !== null ? "PL field maximum " + p.plMaxW + " W (the 15-bit field of MSR 0x610)" : "PL field maximum not reported";
}

const sgn = (x) => (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x);

/** One line for the inspector's limit hint, from the CPU only: what = "offset" | "vt" | "ratio". `hw` = the normHw() entry of the kind ({ratioOc, vtMaxV, off}), or null. */
export function cpuLimitLine(hw, what) {
  if (!hw) return "";
  if (what === "offset") return hw.off ? "CPU limit: " + sgn(hw.off[0]) + "…" + sgn(hw.off[1]) + " mV (V/F offset field)." : "";
  if (what === "vt") return isNum(hw.vtMaxV) ? "CPU limit: up to " + hw.vtMaxV.toFixed(3) + " V (CPU voltage ceiling, mailbox 0x28)." : "";
  return isNum(hw.ratioOc) ? "CPU limit: OC ratios up to " + hw.ratioOc + TIMES + "." : "";
}

/** `cpuLimitLine` for a model of lib/units.js: its `hw` (the CPU's limits of the read) of the kind; the ring uses the P V/F word range. */
export function modelCpuLine(model, kind, what) {
  return cpuLimitLine(model && model.hw ? model.hw[kind === "R" ? "P" : kind] || null : null, what);
}

/** The limit-column text of the offset editors, from the CPU's own V/F offset range (D-049: no policy in the GUI). null = the read has no CPU range (the caller keeps its text). */
export function cpuOffsetCell(model, kind) {
  const hw = model && model.hw ? model.hw[kind === "R" ? "P" : kind] || null : null;
  if (!hw || !hw.off) return null;
  const [lo, hi] = hw.off;
  return (lo === -hi ? "±" + hi : sgn(lo) + "…" + sgn(hi)) + " · CPU";
}

/** The Refusal text for an error {code, message, detail:{check, key}}: CPU_LIMIT_UNREAD -> "Read CPU state first"; otherwise the helper's own message (it names the CPU rule it hit). */
export function explainRefusal(error) {
  const e = isObj(error) ? error : {};
  const check = isObj(e.detail) && typeof e.detail.check === "string" ? e.detail.check : "";
  if (check === "CPU_LIMIT_UNREAD" || e.code === "CPU_LIMIT_UNREAD") return READ_FIRST + " (Tune → Read CPU state): the CPU limit this value is checked against has not been read. Nothing was written.";
  return String(e.message || e.code || "The helper refused.");
}
