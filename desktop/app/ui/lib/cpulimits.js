// lib/cpulimits.js: the CPU's own limits (D-049) as the helper reports them in `cpu_limits` on tune.read / tune.last / power.read / runtime.read (G-226). The GUI shows no operator
// policy: every limit line here names the CPU register or sysfs file the number came from. A limit is `null` until the read that carries it ran; the control then says "Read CPU state first".
//   cpu_limits = {P|E|ring: {max_mv, max_ratio, source}, offset_mv: [lo, hi], power: {locked, pl_max_w, source}, caps_khz: {"<cpu>": [min|null, max]} | null, uncore_khz: [min, max] | null}
// tune.read / tune.last fill P / E / ring and offset_mv; power.read adds `power`, runtime.read adds `caps_khz` and `uncore_khz` (the other reads leave those null).
// Pure; no DOM. Ratios are 100 MHz bins (cpufreq kHz / 100000).
import { isNum, TIMES, MINUS } from "./format.js";

export const READ_FIRST = "Read CPU state first";
export const BIN_KHZ = 100000;
const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const pair = (a) => (Array.isArray(a) && a.length === 2 && a.every(isNum) && a[0] <= a[1] ? [a[0], a[1]] : null);

/**
 * `cpu_limits` -> {kinds: {P|E|ring: {maxMv, maxRatio, source}}, offsetMv: [lo, hi] | null, power: {locked: bool|null, plMaxW: number|null, source} | null,
 * capsKhz: {cpu: [min|null, max]}, uncoreKhz: [min, max] | null} or null (an old helper without it).
 */
export function normCpuLimits(raw) {
  if (!isObj(raw)) return null;
  const kinds = {};
  for (const k of ["P", "E", "ring"]) {
    const o = raw[k];
    if (!isObj(o)) continue;
    kinds[k] = { maxMv: isNum(o.max_mv) ? o.max_mv : null, maxRatio: isNum(o.max_ratio) ? o.max_ratio : null, source: typeof o.source === "string" ? o.source : "" };
  }
  const caps = {};
  if (isObj(raw.caps_khz)) {
    for (const [cpu, w] of Object.entries(raw.caps_khz)) {
      if (Array.isArray(w) && w.length === 2 && isNum(w[1]) && (w[0] === null || isNum(w[0]))) caps[cpu] = [w[0], w[1]];
    }
  }
  const p = isObj(raw.power)
    ? { locked: typeof raw.power.locked === "boolean" ? raw.power.locked : null, plMaxW: isNum(raw.power.pl_max_w) ? raw.power.pl_max_w : null, source: typeof raw.power.source === "string" ? raw.power.source : "" }
    : null;
  return { kinds, offsetMv: pair(raw.offset_mv), power: p, capsKhz: caps, uncoreKhz: pair(raw.uncore_khz) };
}

/** The cap window of one CPU in whole ratios {min | null, max, minKhz | null, maxKhz} (the bins fully inside the cpufreq range), or null when that CPU's range was not read. */
export function capWindow(cl, cpu) {
  const w = cl && cl.capsKhz ? cl.capsKhz[String(cpu)] : null;
  if (!w) return null;
  return { min: w[0] === null ? null : Math.ceil(w[0] / BIN_KHZ - 1e-9), max: Math.floor(w[1] / BIN_KHZ + 1e-9), minKhz: w[0], maxKhz: w[1] };
}

/** The uncore ratio window {min, max} or null. */
export function uncoreWindow(cl) {
  const w = cl ? cl.uncoreKhz : null;
  return w ? { min: Math.ceil(w[0] / BIN_KHZ - 1e-9), max: Math.floor(w[1] / BIN_KHZ + 1e-9) } : null;
}

/** True when the PL field maximum and the lock bit of MSR 0x610 were both read (power.read). */
export const powerRead = (cl) => !!(cl && cl.power && cl.power.plMaxW !== null && cl.power.locked !== null);

const mhz = (khz) => Math.round(khz / 1000);
const unitsOf = (cl, cpus) => cpus.map((c) => capWindow(cl, c)).filter(Boolean);

/** "cpufreq 800–5700 MHz (cpu0)" for one CPU, or a range over a cluster's CPUs. null when none is reported. */
export function capsLine(cl, cpus) {
  const ws = unitsOf(cl, cpus);
  if (!ws.length) return null;
  const los = ws.map((w) => w.minKhz).filter(isNum);
  const hi = Math.min(...ws.map((w) => w.maxKhz));
  return "cpufreq " + (los.length ? mhz(Math.max(...los)) : "?") + "–" + mhz(hi) + " MHz (" + (cpus.length === 1 ? "cpu" + cpus[0] : "cpu" + cpus[0] + "…" + cpus[cpus.length - 1]) + ")";
}

/** "uncore 8×–40× (intel_uncore_frequency initial bounds)" or null. */
export function uncoreLine(cl) {
  const w = uncoreWindow(cl);
  return w ? "uncore " + w.min + TIMES + "–" + w.max + TIMES + " (intel_uncore_frequency initial_min/max_freq_khz)" : null;
}

/** The PL line: field maximum and the lock. Says "Read CPU state first" until power.read filled it. */
export function powerLine(cl) {
  const p = cl ? cl.power : null;
  if (!p || (p.locked === null && p.plMaxW === null)) return READ_FIRST + ": the bounds of the power limits (MSR 0x610 lock bit and field width) come from the power read.";
  if (p.locked) return "locked by firmware (MSR 0x610 lock bit): read-only until the next reset";
  return p.plMaxW !== null ? "PL field maximum " + p.plMaxW + " W (the 15-bit field of MSR 0x610)" : "PL field maximum not read";
}

const sgn = (x) => (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x);

/** The CPU limits of one editor kind of a model of lib/units.js ("P" | "E" | "R": the ring has its own entry), or null. */
function kindOf(model, kind) {
  const cl = model ? model.cpuLimits : null;
  return cl && cl.kinds ? cl.kinds[kind === "R" ? "ring" : kind] || null : null;
}

/** The V/F offset field range [lo, hi] (the 11-bit field, XTU's -1000..+999 mV) or null when the read carried none. */
export function modelOffsetRange(model) {
  return model && model.cpuLimits ? model.cpuLimits.offsetMv : null;
}

/** One line for the inspector's limit hint, from the CPU only: what = "offset" | "vt" | "ratio". */
export function modelCpuLine(model, kind, what) {
  const k = kindOf(model, kind);
  const off = modelOffsetRange(model);
  if (what === "offset") {
    if (!off) return "";
    const base = "CPU limit: " + sgn(off[0]) + "…" + sgn(off[1]) + " mV (V/F offset field).";
    return k && isNum(k.maxMv) ? base : base + " Positive offsets: " + READ_FIRST + " (the CPU voltage ceiling, mailbox 0x28, has not been read).";
  }
  if (what === "vt") return k && isNum(k.maxMv) ? "CPU limit: up to " + (k.maxMv / 1000).toFixed(3) + " V (CPU max voltage, mailbox 0x28)." : READ_FIRST + " (the CPU voltage ceiling, mailbox 0x28, has not been read).";
  return k && isNum(k.maxRatio) ? "CPU limit: OC ratios up to " + k.maxRatio + TIMES + " (OC ratio limit, mailbox 0x01)." : READ_FIRST + " (the OC ratio limit, mailbox 0x01, has not been read).";
}

/** The limit-column text of the offset editors, from the CPU's own V/F offset range (D-049: no policy in the GUI). null = the read has no CPU range (the caller keeps its text). */
export function cpuOffsetCell(model, kind) {
  const off = modelOffsetRange(model);
  if (!off) return null;
  const k = kindOf(model, kind);
  const hi = k && isNum(k.maxMv) ? off[1] : Math.min(off[1], 0);
  return (off[0] === -hi ? "±" + hi : sgn(off[0]) + "…" + sgn(hi)) + " · CPU";
}

/** The Refusal text for an error {code, message, detail:{check, key}}: CPU_LIMIT_UNREAD -> "Read CPU state first"; otherwise the helper's own message (it names the CPU rule it hit). */
export function explainRefusal(error) {
  const e = isObj(error) ? error : {};
  const check = isObj(e.detail) && typeof e.detail.check === "string" ? e.detail.check : "";
  if (check === "CPU_LIMIT_UNREAD" || e.code === "CPU_LIMIT_UNREAD") return READ_FIRST + " (Tune → Read CPU state): the CPU limit this value is checked against has not been read. Nothing was written.";
  return String(e.message || e.code || "The helper refused.");
}
