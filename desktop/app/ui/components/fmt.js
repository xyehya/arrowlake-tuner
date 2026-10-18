// components/fmt.js: number/time formatting for the frame. Pure (no DOM), unit-tested. Units are always shown; a missing value is "n/a", never 0.

export const NA = "n/a";

export function fmtNum(value, digits = 0) {
  if (value === null || value === undefined || !Number.isFinite(value)) return NA;
  return value.toFixed(digits);
}

export function fmtWithUnit(value, unit, digits = 0) {
  const n = fmtNum(value, digits);
  return n === NA ? NA : `${n} ${unit}`;
}

/** "12:34:56" in the local zone, from an ISO string or epoch seconds; "never" when absent. */
export function fmtClock(when) {
  if (when === null || when === undefined || when === "") return "never";
  const d = typeof when === "number" ? new Date(when * 1000) : new Date(when);
  if (Number.isNaN(d.getTime())) return "never";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** Points for an SVG polyline: `values` (null = gap) scaled into w x h. Returns an array of segments, each an array of "x,y" strings. */
export function sparkSegments(values, w, h, pad = 2) {
  const nums = values.filter((v) => v !== null && Number.isFinite(v));
  if (nums.length === 0) return [];
  let lo = Math.min(...nums);
  let hi = Math.max(...nums);
  if (hi - lo < 1e-9) {
    lo -= 0.5;
    hi += 0.5;
  }
  const n = values.length;
  const x = (i) => (n === 1 ? w / 2 : pad + (i * (w - 2 * pad)) / (n - 1));
  const y = (v) => h - pad - ((v - lo) / (hi - lo)) * (h - 2 * pad);
  const segs = [];
  let cur = [];
  values.forEach((v, i) => {
    if (v === null || !Number.isFinite(v)) {
      if (cur.length) segs.push(cur);
      cur = [];
    } else {
      cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    }
  });
  if (cur.length) segs.push(cur);
  return segs;
}

export function plural(n, one, many = one + "s") {
  return `${n} ${n === 1 ? one : many}`;
}
