// format.js: number/label formatting shared by the Tune logic (G-207). Pure; no DOM. Ported from web/app.js fmtMv / MINUS / mixed text.
// Values are shown with their unit; a missing value is an en dash, never a guess.

export const MINUS = "−";
export const TIMES = "×";
export const DASH = "–";

export const isNum = (x) => typeof x === "number" && Number.isFinite(x);

/** Signed mV, whole when within 0.05 of a whole number (the hardware offset is a 1/1024 V raw, so reads are -19.531), else one decimal. */
export function fmtMvNum(x) {
  if (!isNum(x)) return DASH;
  const whole = Math.abs(x - Math.round(x)) < 0.05;
  const body = whole ? String(Math.abs(Math.round(x))) : Math.abs(x).toFixed(1);
  return (x > 0.0499 ? "+" : x < -0.0499 ? MINUS : "") + body;
}
export function fmtMv(x) {
  return isNum(x) ? fmtMvNum(x) + " mV" : DASH;
}
/** Volts, 3 decimals (4 when the value is not on the 1 mV grid, like an adaptive target read back from 1/1024 V). */
export function fmtV(v) {
  if (!isNum(v)) return DASH;
  return v.toFixed(Math.abs(v * 1000 - Math.round(v * 1000)) < 1e-6 ? 3 : 4) + " V";
}
export function fmtRatio(r) {
  return isNum(r) ? r + TIMES : DASH;
}

/** "mixed (min…max unit)" for an aggregate {state:"mixed", min, max}; the plain value otherwise. `fmt` formats one number (with its unit). */
export function fmtAgg(agg, fmt) {
  if (!agg || agg.state === "none") return DASH;
  if (agg.state === "same") return fmt(agg.value);
  return "mixed (" + fmt(agg.min) + "…" + fmt(agg.max) + ")";
}

export function plural(n, one, many = one + "s") {
  return n + " " + (n === 1 ? one : many);
}
