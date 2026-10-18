// components/spark.js: the dashboard's sparkline (a bigger sibling of the strip's) and the stylesheet installer the G-206 components share.
// Also home of the small pieces the three G-206 screens share: `segmented()` (a chip group) and the common screen CSS (cards, tables, key/value lists).
// Source: web/app.js sparkline() (the 120-sample tile sparklines, "n/a" leaves are gaps); scaling is fmt.sparkSegments, so gaps, flat lines and one-sample
// series behave exactly like the monitoring strip.
//
// Styles: this task owns no stylesheet, and the strict CSP (PLAN §2) forbids <style> elements and style attributes. A constructed stylesheet
// (`CSSStyleSheet.replaceSync` + `adoptedStyleSheets`) is CSSOM, not markup, so it is allowed under `style-src 'self'` and keeps the CSS next to the code
// that needs it. Rules use the tokens of styles/tokens.css only. Move them into styles/*.css whenever the orchestrator prefers a file.
import { h, svg } from "./dom.js";
import { sparkSegments } from "./fmt.js";

const installed = new WeakMap(); // Document -> Set(key)

/** Install `css` once per document under `key`. Returns false when constructable stylesheets are unavailable (rules then simply do not apply). */
export function installStyles(key, css, doc = globalThis.document) {
  if (!doc || !doc.defaultView || typeof doc.defaultView.CSSStyleSheet !== "function" || !("adoptedStyleSheets" in doc)) return false;
  let keys = installed.get(doc);
  if (!keys) installed.set(doc, (keys = new Set()));
  if (keys.has(key)) return true;
  const sheet = new doc.defaultView.CSSStyleSheet();
  sheet.replaceSync(css);
  doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, sheet];
  keys.add(key);
  return true;
}

/** SVG path data ("M x,y L x,y M ...") for `values` (null = gap) scaled into w x h; "" when there is nothing to draw. */
export function sparkPath(values, w, h, pad = 2) {
  return sparkSegments(values, w, h, pad)
    .map((seg) => (seg.length === 1 ? `M${seg[0]} L${seg[0]}` : "M" + seg.join(" L")))
    .join(" ");
}

const CSS = `
.spark-lg { width: 100%; height: 30px; display: block; }
.spark-lg .spark-line { stroke: var(--pending); stroke-width: 1.5; fill: none; vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round; }
.spark-lg .spark-base { stroke: var(--border); stroke-width: 1; vector-effect: non-scaling-stroke; }
.spark-lg.empty .spark-line { display: none; }
.spark-lg.ok .spark-line { stroke: var(--ok); }
.spark-lg.warn .spark-line { stroke: var(--warn); }
`;

/** createSpark({w, h, tone}) -> {el, draw(values), segments}; `values` are numbers or null. `tone`: "" | "ok" | "warn" picks the semantic stroke colour. */
export function createSpark({ w = 120, h = 30, tone = "" } = {}) {
  installStyles("spark", CSS);
  const base = svg("line", { class: "spark-base", x1: 0, x2: w, y1: h - 0.5, y2: h - 0.5 });
  const line = svg("path", { class: "spark-line", d: "" });
  const el = svg("svg", { class: ["spark-lg", "empty", tone].filter(Boolean).join(" "), viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" }, base, line);
  return {
    el,
    draw(values) {
      const d = sparkPath(values || [], w, h);
      line.setAttribute("d", d);
      el.classList.toggle("empty", d === "");
      return d;
    },
  };
}

/** A group of toggle chips (one pressed). options: [{value, label, title?}]. -> {el, set(value), get()}; `onChange(value)` fires on a user click only. */
export function segmented(options, current, onChange, { label = "" } = {}) {
  let value = current;
  const buttons = new Map();
  const el = h("div", { class: "seg", role: "group", "aria-label": label });
  const paint = () => buttons.forEach((b, v) => b.setAttribute("aria-pressed", String(v === String(value))));
  for (const o of options) {
    const b = h("button", { type: "button", class: "chip toggle", dataset: { value: o.value }, title: o.title || "", text: o.label, on: { click: () => { value = o.value; paint(); onChange(o.value); } } });
    buttons.set(String(o.value), b);
    el.append(b);
  }
  paint();
  return {
    el,
    set(v) {
      value = v;
      paint();
    },
    get: () => value,
  };
}

const SCREEN_CSS = `
.sc { display: flex; flex-direction: column; gap: var(--space-4); max-width: 1500px; }
.sc .screen-head { margin-bottom: 0; flex-wrap: wrap; }
.sc-meta { color: var(--text-dim); font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }
.dcard { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space-3) var(--space-4); min-width: 0; }
.dcard-head { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; margin-bottom: var(--space-3); }
.dcard-head h2 { font-size: var(--fs-md); text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-dim); }
.dcard-note { margin: 0 0 var(--space-3); color: var(--text-dim); font-size: var(--fs-sm); }
.seg { display: inline-flex; gap: 4px; flex-wrap: wrap; }
.seg .chip.toggle { padding: 1px 9px; }
.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 3px var(--space-4); margin: 0; font-size: var(--fs-sm); }
.kv dt { color: var(--text-dim); }
.kv dd { margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.sc .na { color: var(--text-faint); }
.tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }
.tbl th { text-align: left; font-weight: 500; font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); padding: 4px 8px; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
.tbl td { padding: 3px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
.tbl tbody tr:hover { background: var(--surface-2); }
.tbl .n { text-align: right; }
.tbl .mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
`;

/** Install the shared screen CSS (idempotent). Every G-206 view calls this first. */
export function installScreenStyles() {
  return installStyles("screens", SCREEN_CSS);
}
