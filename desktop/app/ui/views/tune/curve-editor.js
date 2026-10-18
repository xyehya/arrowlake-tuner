// views/tune/curve-editor.js: the V/F curve editor of one selection section (PLAN §4.2): a whole-curve offset field, one row per point
// (Point n · ratio | base V | offset field | predicted V | read-back V | limit hint) and a chart whose markers drag vertically in 5 mV steps.
// Typing, the arrow keys (5 mV, Shift 25), the wheel and the chart drag all end in the same commit() -> setPoint / setRingPoint / setCurve of lib/draft.js, so they produce the same draft.
// `curveRows()` is pure (node-tested): it is the table, the DOM only shows it. Predictions come from lib/predict.js on the injected baseline (tc.baseFn()); without one only
// the fused top point has a voltage and the other cells say why. The mailbox/register facts in the tooltips are from docs/knowledge/registers.md.
import { h, svg, clear } from "../../components/dom.js";
import { createStepper } from "../../components/stepper.js";
import { isNum, fmtV, fmtMv, fmtAgg, fmtRatio, MINUS, DASH } from "../../lib/format.js";
import { aggregate, sameMv, sameV, pointAt, ringPoint } from "../../lib/units.js";
import { emptyDraft, effPointMv, pointValue, ringValue, curveValue, setPoint, setRingPoint, setCurve } from "../../lib/draft.js";
import { predictUnit, predictedAt } from "../../lib/predict.js";
import { limitsFor, NO_LIMITS } from "../../lib/limits.js";
import { modelCpuLine, cpuOffsetCell } from "../../lib/cpulimits.js";

export const NO_BASELINE = "no baseline curve (Curves → Measure)";
export const STEP_MV = 5;
export const BIG_STEP_MV = 25;
const sgn = (x) => (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x);

/** Why the offset fields of a kind are disabled, or "" when they are not. */
export function disabledReason(model, kind) {
  const l = limitsFor(model, kind);
  if (kind === "R") {
    if (!model.features.ring) return 'this helper does not understand ring edits (tune.read reported no "ring" intent feature)';
    return l.offset ? "" : "no V/F word range known for the ring";
  }
  return l.known && l.offset ? "" : NO_LIMITS;
}

/** The one-line limit hint of a row/field: window and who sets it. */
export function limitCell(model, kind) {
  const w = limitsFor(model, kind).offset;
  if (!w) return "no window";
  return (w.min === -w.max ? "±" + w.max : sgn(w.min) + "…" + sgn(w.max)) + " · " + w.source; // mV: the column header says it
}

const eqAgg = (a, b) => a.state === b.state && (a.state === "none" || (a.state === "same" ? sameMv(a.value, b.value) : sameMv(a.min, b.min) && sameMv(a.max, b.max)));

/** The whole-curve field: aggregate of every point offset of the section, as edited (eff) or as read. */
export function sectionCurve(model, draft, section, eff = true) {
  if (section.kind === "R") {
    const u = model.units[section.ids[0]];
    return aggregate(u.points.map((p) => (eff ? effPointMv(draft, u, p) : p.offset_mv)), sameMv);
  }
  return curveValue(model, draft, section.ids, eff);
}

/**
 * Rows of a section's table. Each: {key (ratio | ring point no), ratio, point, label, ids, shared, agg, readAgg, pending, baseV, baseSrc, pred, readback, clampedBy, ocSlot, why}
 * `agg`/`readAgg` aggregate the offsets (mixed -> {state:"mixed", min, max}); `pred`/`readback`/`baseV` aggregate volts ({state:"none"} = no value, `why` says why).
 */
export function curveRows(model, draft, section, base, baseReason = NO_BASELINE) {
  const rows = [];
  if (section.kind === "R") {
    const u = model.units[section.ids[0]];
    for (const no of section.points) {
      const p = ringPoint(u, no);
      const v = ringValue(model, draft, no, true);
      rows.push({
        key: no, ratio: p.ratio, point: no, label: "Ring P2 = " + no + (isNum(p.ratio) ? " · " + fmtRatio(p.ratio) : ""), ids: section.ids, shared: true, agg: v.agg, readAgg: ringValue(model, draft, no, false).agg,
        pending: v.pending, baseV: { state: "none" }, baseSrc: "", pred: { state: "none" }, readback: { state: "none" }, clampedBy: null, ocSlot: p.ocSlot === true, why: "the ring has no stock V/F baseline",
      });
    }
    return rows;
  }
  const preds = Object.fromEntries(section.ids.map((id) => [id, predictUnit(model, draft, id, base)]));
  const reads = Object.fromEntries(section.ids.map((id) => [id, predictUnit(model, emptyDraft(), id, base)]));
  for (const ratio of section.ratios) {
    const v = pointValue(model, draft, section.ids, ratio, true);
    const ids = v.ids;
    const ps = ids.map((id) => predictedAt(preds[id], ratio)).filter(Boolean);
    const rs = ids.map((id) => predictedAt(reads[id], ratio)).filter(Boolean);
    const clamp = ps.find((p) => p.clampedBy);
    rows.push({
      key: ratio,
      ratio,
      point: pointAt(model.units[ids[0]], ratio).point,
      label: "Point " + pointAt(model.units[ids[0]], ratio).point + " · " + fmtRatio(ratio),
      ids,
      shared: ids.length === section.ids.length,
      agg: v.agg,
      readAgg: pointValue(model, draft, section.ids, ratio, false).agg,
      pending: v.pending,
      baseV: aggregate(ps.map((p) => p.base), sameV),
      baseSrc: (ps.find((p) => p.src) || {}).src || "",
      pred: aggregate(ps.map((p) => p.v), sameV),
      readback: aggregate(rs.map((p) => p.v), sameV),
      clampedBy: clamp ? clamp.clampedBy : null,
      ocSlot: false,
      why: base ? "the baseline curve has no usable voltage at this ratio" : baseReason,
    });
  }
  return rows;
}

const aggMid = (a) => (a.state === "same" ? a.value : a.state === "mixed" ? Math.round((a.min + a.max) / 2) : null);
const aggVolt = (a) => (a.state === "same" ? a.value : a.state === "mixed" ? (a.min + a.max) / 2 : null);
const fmtVText = (a) => (a.state === "none" ? DASH : a.state === "mixed" ? a.min.toFixed(3) + "…" + a.max.toFixed(3) + " V" : a.value.toFixed(3) + " V"); // voltages differ per unit: a range, not "mixed"

function niceStep(span, ticks = 4) {
  const raw = span / ticks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
}

const W = 640;
const H = 210;
const M = { l: 52, r: 14, t: 12, b: 38 };

/** The editor of a section. `tc` = Tune controller. Returns {el, update(), dispose()}. */
export function createCurveEditor(tc, section) {
  const kind = section.kind;
  const ringSec = kind === "R";
  const rowEls = new Map(); // key -> {tr, cells, stepper}
  let drag = null;
  const dom = { lo: null, hi: null, mode: null }; // chart value domain: only grows, until the editor is rebuilt (the chart does not jump while editing)

  const commitPoint = (key, mv) => tc.edit((m, d) => (ringSec ? setRingPoint(m, d, key, mv) : setPoint(m, d, section.ids, key, mv)));
  const commitCurve = (mv) => tc.edit((m, d) => setCurve(m, d, section.ids, mv));

  const title = ringSec ? "mailbox 0x11 (P1 = 2 ring domain, P2 = point) · docs/knowledge/registers.md" : "mailbox 0x11 (P1 = 0, P2 = point) · docs/knowledge/registers.md (V/F data word)";
  const curveField = createStepper({
    label: "Whole-curve offset", unit: "mV", step: STEP_MV, big: BIG_STEP_MV, onCommit: commitCurve,
    title: "Offset applied to every point of the curve; typed mV, Up/Down " + STEP_MV + " mV, Shift " + BIG_STEP_MV + " mV, wheel while focused · " + title,
  });
  const curveRow = h("div", { class: "curve-top" }, h("label", { class: "field-label", text: "Whole curve" }), curveField.el, h("span", { class: "muted curve-note", text: "sets every point of " + (section.ids.length === 1 ? "this unit" : section.ids.length + " units") }));

  const thead = h("thead", {}, h("tr", {},
    h("th", { class: "c-pt", text: "Point · ratio" }),
    h("th", { class: "c-num", text: "Base V", title: "Stock voltage of the point: the baseline curve, else the fused top point (cmd 0x09)" }),
    h("th", { class: "c-off", text: "Offset (mV)", title: "V/F point offset (mV); " + title }),
    h("th", { class: "c-num", text: "Predicted V", title: "Base V + offset, raised to the previous point's voltage when lower (V(n) ≥ V(n−1)). Not a CPU read." }),
    h("th", { class: "c-num", text: "Read-back V", title: "The same on the offsets the CPU reported at the last read" }),
    h("th", { class: "c-lim", text: "Limit (mV)", title: "The CPU's own offset range (its V/F word); the helper refuses a value outside it" })));
  const tbody = h("tbody", {});
  const table = h("table", { class: "vf-table", "aria-label": (ringSec ? "Ring" : kind === "P" ? "P-core" : "E-cluster") + " V/F points" }, thead, tbody);

  const reason = () => disabledReason(tc.model(), kind);

  function rowOf(r) {
    const stepper = createStepper({
      label: r.label + " offset", unit: "mV", hideUnit: true, step: STEP_MV, big: BIG_STEP_MV, onCommit: (mv) => commitPoint(r.key, mv),
      title: r.label + " offset · typed mV, Up/Down " + STEP_MV + " mV, Shift " + BIG_STEP_MV + " mV, wheel while focused · " + title,
    });
    const cells = {
      pt: h("td", { class: "c-pt" }, h("span", { class: "pt-label", text: r.label }), h("span", { class: "pt-tag muted", hidden: true })),
      base: h("td", { class: "c-num" }),
      off: h("td", { class: "c-off" }, stepper.el),
      pred: h("td", { class: "c-num" }),
      read: h("td", { class: "c-num" }),
      lim: h("td", { class: "c-lim muted" }),
    };
    const tr = h("tr", { dataset: { ratio: r.ratio ?? "", key: r.key, ring: ringSec ? "1" : "" } }, cells.pt, cells.base, cells.off, cells.pred, cells.read, cells.lim);
    return { tr, cells, stepper };
  }

  // ---- chart ----
  const plotW = W - M.l - M.r;
  const plotH = H - M.t - M.b;
  const gGrid = svg("g", { class: "chart-grid" });
  const gLines = svg("g", { class: "chart-lines" });
  const gMarks = svg("g", { class: "chart-marks" });
  const gAxis = svg("g", { class: "chart-axis" });
  const lnBase = svg("polyline", { class: "chart-base", fill: "none" });
  const lnRead = svg("polyline", { class: "chart-read", fill: "none" });
  const lnDraft = svg("polyline", { class: "chart-draft", fill: "none" });
  gLines.append(lnBase, lnRead, lnDraft);
  const chart = svg("svg", { class: "vf-chart", viewBox: "0 0 " + W + " " + H, role: "group", "aria-label": "V/F curve chart. Drag a marker up or down, or focus it and use the arrow keys.", preserveAspectRatio: "xMidYMid meet" }, gGrid, gLines, gMarks, gAxis);
  const legend = h("p", { class: "chart-legend muted" });
  const markers = new Map(); // key -> circle
  let rowsNow = [];

  function mvPerUnit() {
    return dom.mode === "volt" ? (1000 * (dom.hi - dom.lo)) / plotH : (dom.hi - dom.lo) / plotH;
  }
  const yOf = (val) => M.t + plotH - ((Math.min(Math.max(val, dom.lo), dom.hi) - dom.lo) / (dom.hi - dom.lo)) * plotH;
  const xOf = (i, n) => M.l + ((i + 0.5) * plotW) / n;

  function chartUpdate() {
    const rows = rowsNow;
    const n = rows.length;
    if (!n) return;
    const volt = rows.every((r) => r.pred.state !== "none" && r.baseV.state !== "none") && n >= 2;
    const val = (r, which) => (volt ? aggVolt(which === "base" ? r.baseV : which === "read" ? r.readback : r.pred) : which === "base" ? 0 : aggMid(which === "read" ? r.readAgg : r.agg));
    const all = rows.flatMap((r) => ["base", "read", "draft"].map((w) => val(r, w))).filter(isNum);
    if (!drag) {
      let lo, hi;
      if (volt) {
        lo = Math.min(...all) - 0.02;
        hi = Math.max(...all) + 0.02;
      } else {
        lo = Math.min(-50, Math.floor((Math.min(...all) - 10) / 25) * 25);
        hi = Math.max(25, Math.ceil((Math.max(...all) + 10) / 25) * 25);
      }
      if (dom.mode !== (volt ? "volt" : "mv") || dom.lo === null) {
        dom.mode = volt ? "volt" : "mv";
        dom.lo = lo;
        dom.hi = hi;
      } else {
        dom.lo = Math.min(dom.lo, lo);
        dom.hi = Math.max(dom.hi, hi);
      }
    }
    chart.dataset.mode = dom.mode;
    chart.dataset.mvPerUnit = String(Math.round(mvPerUnit() * 1000) / 1000);
    chart.dataset.viewH = String(H);
    // grid + y labels
    clear(gGrid);
    clear(gAxis);
    const unit = dom.mode === "volt" ? 1 : 1;
    const step = niceStep(dom.hi - dom.lo) * unit;
    for (let t = Math.ceil(dom.lo / step) * step; t <= dom.hi + 1e-9; t += step) {
      const y = yOf(t);
      gGrid.append(svg("line", { class: "chart-gridline" + (Math.abs(t) < 1e-9 && dom.mode === "mv" ? " zero" : ""), x1: M.l, x2: W - M.r, y1: y, y2: y }));
      gAxis.append(svg("text", { class: "chart-ytick", x: M.l - 6, y: y + 3, "text-anchor": "end" }, dom.mode === "volt" ? t.toFixed(2) : sgn(Math.round(t))));
    }
    gAxis.append(svg("text", { class: "chart-ytitle", x: 4, y: M.t + 8 }, dom.mode === "volt" ? "V" : "mV"));
    const pts = (which) =>
      rows
        .map((r, i) => {
          const v = val(r, which);
          return isNum(v) ? xOf(i, n).toFixed(1) + "," + yOf(v).toFixed(1) : null;
        })
        .filter(Boolean)
        .join(" ");
    lnBase.setAttribute("points", pts("base"));
    lnRead.setAttribute("points", pts("read"));
    lnDraft.setAttribute("points", pts("draft"));
    const anyPending = rows.some((r) => r.pending);
    lnRead.classList.toggle("chart-hidden", !anyPending);
    lnDraft.classList.toggle("pending", anyPending);
    rows.forEach((r, i) => {
      const x = xOf(i, n);
      const cur = val(r, "draft");
      let m = markers.get(r.key);
      if (!m) return;
      m.setAttribute("cx", x.toFixed(1));
      m.setAttribute("cy", (isNum(cur) ? yOf(cur) : M.t + plotH).toFixed(1));
      m.classList.toggle("pending", r.pending);
      m.classList.toggle("mixed", r.agg.state === "mixed");
      m.setAttribute("aria-valuenow", isNum(aggMid(r.agg)) ? String(aggMid(r.agg)) : "");
      m.setAttribute("aria-valuetext", r.label + ": " + fmtAgg(r.agg, fmtMv));
      gAxis.append(svg("text", { class: "chart-xtick", x: x, y: H - M.b + 14, "text-anchor": "middle" }, isNum(r.ratio) ? r.ratio + "×" : String(r.key)));
      gAxis.append(svg("text", { class: "chart-xsub", x: x, y: H - M.b + 27, "text-anchor": "middle" }, "P" + r.point));
    });
    legend.textContent = dom.mode === "volt"
      ? "Dashed: baseline V. Solid: predicted V on your edits" + (anyPending ? " (pending); faint: the CPU's current offsets" : "") + ". Points are spaced evenly."
      : "Offsets in mV; the zero line is the fused curve. No baseline curve (Curves → Measure), so voltages are not drawn. Points are spaced evenly.";
  }

  function ensureMarkers(rows) {
    if (markers.size === rows.length && rows.every((r) => markers.has(r.key))) return;
    clear(gMarks);
    markers.clear();
    rows.forEach((r, i) => {
      const m = svg("circle", {
        class: "chart-marker", r: 6, cx: 0, cy: 0, tabindex: i === 0 ? "0" : "-1", role: "slider", "aria-label": r.label + " offset (mV)", "aria-orientation": "vertical",
        "data-key": r.key, "data-ratio": r.ratio ?? "",
      });
      m.addEventListener("pointerdown", (e) => onDown(e, r.key, m));
      m.addEventListener("pointermove", (e) => onMove(e));
      m.addEventListener("pointerup", (e) => onUp(e, m));
      m.addEventListener("pointercancel", (e) => onUp(e, m));
      m.addEventListener("keydown", (e) => onKey(e, r.key));
      gMarks.append(m);
      markers.set(r.key, m);
    });
  }

  const currentMv = (key) => aggMid((rowsNow.find((r) => r.key === key) || { agg: { state: "none" } }).agg) ?? 0;
  const snap = (mv) => Math.round(mv / STEP_MV) * STEP_MV;

  function onDown(e, key, m) {
    if (e.button !== 0 || reason()) return;
    e.preventDefault();
    m.focus();
    m.setPointerCapture(e.pointerId);
    const rect = chart.getBoundingClientRect();
    const mvPerPx = mvPerUnit() * (H / rect.height);
    drag = { key, startY: e.clientY, startMv: currentMv(key), mvPerPx, last: currentMv(key) };
    chart.classList.add("dragging");
  }
  function onMove(e) {
    if (!drag) return;
    const target = snap(drag.startMv - (e.clientY - drag.startY) * drag.mvPerPx);
    if (target !== drag.last) {
      drag.last = target;
      commitPoint(drag.key, target);
    }
  }
  function onUp(e, m) {
    if (!drag) return;
    drag = null;
    chart.classList.remove("dragging");
    if (m.hasPointerCapture(e.pointerId)) m.releasePointerCapture(e.pointerId);
    update();
  }
  function onKey(e, key) {
    const keys = rowsNow.map((r) => r.key);
    const i = keys.indexOf(key);
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const next = markers.get(keys[Math.min(keys.length - 1, Math.max(0, i + (e.key === "ArrowLeft" ? -1 : 1)))]);
      for (const m of markers.values()) m.tabIndex = -1;
      next.tabIndex = 0;
      next.focus();
    } else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !reason()) {
      e.preventDefault();
      const size = e.shiftKey ? BIG_STEP_MV : STEP_MV;
      const cur = currentMv(key);
      const q = cur / size;
      commitPoint(key, (e.key === "ArrowUp" ? Math.floor(q + 1e-9) + 1 : Math.ceil(q - 1e-9) - 1) * size);
    }
  }

  const hint = h("p", { class: "limit-line muted" });
  const noteBase = h("p", { class: "base-note muted" });
  const el = h("div", { class: "curve-editor", dataset: { kind } }, curveRow, noteBase, h("div", { class: "table-wrap" }, table), h("figure", { class: "chart-wrap" }, chart, legend), hint);

  function update() {
    const s = tc.state();
    const model = s.model;
    const draft = s.draft;
    const base = tc.baseFn();
    const why = reason();
    const w = limitsFor(model, kind).offset;
    rowsNow = curveRows(model, draft, section, base, tc.baseReason());
    // rows
    if (rowEls.size !== rowsNow.length || !rowsNow.every((r) => rowEls.has(r.key))) {
      clear(tbody);
      rowEls.clear();
      for (const r of rowsNow) {
        const row = rowOf(r);
        rowEls.set(r.key, row);
        tbody.append(row.tr);
      }
    }
    const fieldBase = { disabled: !!why, reason: why, min: w ? w.min : undefined, max: w ? w.max : undefined };
    const cur = sectionCurve(model, draft, section, true);
    const rd = sectionCurve(model, draft, section, false);
    curveField.set({
      ...fieldBase,
      value: cur.state === "same" ? cur.value : null,
      mixed: cur.state === "mixed" ? cur : null,
      placeholder: cur.state === "none" ? DASH : "",
      pending: !eqAgg(cur, rd),
      was: rd.state === "same" ? fmtMv(rd.value) : rd.state === "mixed" ? "mixed" : "",
    });
    for (const r of rowsNow) {
      const row = rowEls.get(r.key);
      row.stepper.set({
        ...fieldBase,
        value: r.agg.state === "same" ? r.agg.value : null,
        mixed: r.agg.state === "mixed" ? r.agg : null,
        pending: r.pending,
        was: r.readAgg.state === "same" ? fmtMv(r.readAgg.value) : r.readAgg.state === "mixed" ? "mixed" : "",
      });
      const c = row.cells;
      c.pt.querySelector(".pt-label").textContent = r.label;
      const tag = c.pt.querySelector(".pt-tag");
      tag.hidden = !(r.ocSlot || !r.shared);
      tag.textContent = r.ocSlot ? "OC slot" : !r.shared ? r.ids.length + " of " + section.ids.length + " units" : "";
      tag.title = r.ocSlot ? "P2 = 9 is the OC ratio's slot: an offset written there moves the OC ratio [MEASURED lnx 2026-10-15]" : "Only these units have a point at this ratio; the edit applies to them";
      c.base.textContent = fmtVText(r.baseV);
      c.base.title = r.baseV.state === "none" ? r.why : "from the " + (r.baseSrc === "fused" ? "fused top point (cmd 0x09)" : r.baseSrc === "interpolated" ? "baseline curve (interpolated between measured points)" : "baseline curve");
      c.pred.textContent = fmtVText(r.pred) + (r.clampedBy ? " ↑" : "");
      c.pred.title = r.pred.state === "none" ? r.why : r.clampedBy ? "raised to point " + r.clampedBy.point + "'s voltage: the CPU keeps V(n) ≥ V(n−1)" : "base V + offset; not a CPU read";
      c.pred.classList.toggle("pending", r.pending && r.pred.state !== "none");
      c.pred.classList.toggle("na", r.pred.state === "none");
      c.base.classList.toggle("na", r.baseV.state === "none");
      c.read.textContent = fmtVText(r.readback);
      c.read.title = r.readback.state === "none" ? r.why : "base V + the offsets read from the CPU";
      c.read.classList.toggle("na", r.readback.state === "none");
      c.lim.textContent = cpuOffsetCell(model, kind) || limitCell(model, kind);
      c.lim.title = (w ? "Offsets outside " + sgn(w.min) + "…" + sgn(w.max) + " mV are not accepted. " : "") + modelCpuLine(model, kind, "offset");
      row.tr.classList.toggle("pending", r.pending);
    }
    ensureMarkers(rowsNow);
    chartUpdate();
    hint.textContent = why || modelCpuLine(model, kind, "offset");
    hint.classList.toggle("warn", !!why);
    noteBase.hidden = !!base || ringSec;
    noteBase.textContent = base ? "" : "Predicted voltages need a baseline curve (Curves → Measure); until then only the fused top point has a voltage.";
  }

  return { el, update, dispose() {} };
}
