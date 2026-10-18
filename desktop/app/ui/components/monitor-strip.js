// components/monitor-strip.js: the always-visible bottom strip, PLAN §4.1: selectable live series as sparklines + numbers, limit-reason badges, collapsible.
// The sparkline here is the strip's own minimal one; the dashboard's chart/spark components belong to G-206.
import { h, svg, icon, clear } from "./dom.js";
import { fmtNum, sparkSegments } from "./fmt.js";
import { SERIES, toggleSeries, setStrip } from "../store.js";

const W = 96;
const H = 26;

function limitText(l) {
  if (l.state === "na") return "limits n/a";
  if (l.state === "none") return "no limit active";
  return `limit bits 0x${l.raw.toString(16).toUpperCase()}`;
}

export function createMonitorStrip(root, store) {
  clear(root);
  root.setAttribute("role", "region");
  root.setAttribute("aria-label", "Live monitoring");

  const collapse = h("button", { type: "button", class: "btn icon-btn", id: "strip-toggle", "aria-controls": "strip-body", title: "Collapse or expand the monitoring strip", on: { click: () => setStrip(store, { collapsed: !store.get().strip.collapsed }) } }, icon("chevron-down"));
  const chips = new Map();
  const chipBox = h("div", { class: "strip-chips", role: "group", "aria-label": "Series shown" });
  for (const s of SERIES) {
    const c = h("button", { type: "button", class: "chip toggle", dataset: { series: s.key }, "aria-pressed": "true", text: s.label, on: { click: () => toggleSeries(store, s.key) } });
    chips.set(s.key, c);
    chipBox.append(c);
  }
  const badge = h("span", { class: "chip", id: "limit-badges" });
  const head = h("div", { class: "strip-head" }, h("strong", { class: "strip-title", text: "Monitoring" }), chipBox, h("span", { class: "spacer" }), badge, collapse);

  const cells = new Map();
  const body = h("div", { class: "strip-body", id: "strip-body" });
  for (const s of SERIES) {
    const line = svg("path", { class: "spark-line", fill: "none" });
    const spark = svg("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, width: W, height: H, "aria-hidden": "true", preserveAspectRatio: "none" }, line);
    const value = h("span", { class: "strip-value num", text: "n/a" });
    const unit = h("span", { class: "strip-unit", text: s.unit });
    const cell = h("div", { class: "strip-cell", dataset: { series: s.key } }, h("span", { class: "strip-label", text: s.label }), h("span", { class: "strip-read" }, value, unit), spark);
    cells.set(s.key, { cell, value, unit, spark, line });
    body.append(cell);
  }
  root.append(head, body);

  const draw = (st) => {
    const samples = st.telemetry.samples;
    for (const s of SERIES) {
      const c = cells.get(s.key);
      const smp = samples ? samples[s.key] : null;
      const hist = st.telemetry.history[s.key] || [];
      const have = smp && smp.value !== null;
      c.value.textContent = have ? fmtNum(smp.value, s.digits) : "n/a";
      c.unit.hidden = !have;
      c.cell.classList.toggle("na", !have);
      c.cell.title = have ? `${s.label}${smp.source ? " · " + smp.source : ""}` : `${s.label}: n/a${smp && smp.reason ? " · " + smp.reason : samples ? "" : " · no telemetry yet"}`;
      const segs = sparkSegments(hist, W, H);
      c.line.setAttribute("d", segs.map((seg) => "M" + seg.join(" L")).join(" "));
      c.spark.classList.toggle("empty", segs.length === 0);
    }
  };

  const unsubs = [
    store.watch((s) => s.telemetry, (_t, st) => draw(st)),
    store.watch(
      (s) => s.telemetry.limits,
      (l) => {
        badge.textContent = limitText(l);
        badge.dataset.state = l.state === "raw" ? "warn" : l.state === "none" ? "ok" : "unknown";
        badge.title = l.state === "na" ? `Limit reasons: n/a${l.reason ? " · " + l.reason : ""}` : `Raw ${l.source || "limit-reason"} word; named reasons (PL1, PL2, thermal, current) arrive with the power controls.`;
      },
    ),
    store.watch(
      (s) => s.strip,
      (strip) => {
        root.classList.toggle("collapsed", strip.collapsed);
        body.hidden = strip.collapsed;
        collapse.setAttribute("aria-expanded", String(!strip.collapsed));
        for (const [key, c] of chips) c.setAttribute("aria-pressed", String(strip.series.includes(key)));
        for (const [key, c] of cells) c.cell.hidden = !strip.series.includes(key);
      },
    ),
  ];
  return () => unsubs.forEach((u) => u());
}
