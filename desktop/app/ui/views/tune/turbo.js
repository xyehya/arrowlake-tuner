// turbo.js: the Tune screen's "Turbo ratios" sub-tab (G-209). PLAN §4.2 — an XTU-style Active Cores table for P and for E (1..8 buckets), side by side:
// one ratio stepper per bucket (minus / typed / plus, arrow keys ±1), the value as read, a pending marker when the draft differs, the read-only bucket core
// counts and the CPU's ceiling with its source; below each table an inline SVG step chart (ratio vs active cores).
// Ported from web/app.js:2846-2860 (turboCur / turboEff / turboKinds / setBucket), 3514-3600 (turboLabel / turboStepChart / renderTurbo / buildTurbo).
//
//   mount(el, ctx) / unmount()
//   ctx = {store, bridge, draft, units, limits, model?, read?, getDraft?, setDraft?, onChange?}
//     store   the app store (store.js). After every accepted edit the pending counts are written with setPendingAll(store, ...) (views/tune.js: the draft plus the runtime edits); nothing else is read from it.
//     bridge  accepted for parity with the other views; this view never talks to the helper. An edit is local until Review & Apply (PLAN §4.3).
//     draft   lib/draft.js — the turbo API used here is turboCur(model, kind), turboEff(model, draft, kind), setBucket(model, draft, kind, i, value),
//             discardTurbo(draft, kind) and diff(model, draft). A wrapper exposing the same functions works; the current draft VALUE may also be passed.
//     units   lib/units.js (modelFromRead; used only when no model is handed in), limits lib/limits.js (limitsFor).
//     model   the units model (units.modelFromRead of a tune.read result); `read` (the raw result) is the fallback, then store state.
//     getDraft / setDraft  the host's accessors for the shared draft (required together). Without them the draft in `ctx.draft`, in store state
//             (`tune.draft`), or a view-local empty draft is used, in that order.
//   The view repaints itself after each edit; a new `tune.read` remounts it.
//
//   Stylesheet: TURBO_CSS below. index.html is owned by another task (G-208) — it must link this file:
//     <link rel="stylesheet" href="styles/turbo.css">
//
// Hardware rules carried here, never invented: the turbo table may only be non-increasing (a bucket may not exceed the bucket to its left, fewer active
// cores = higher ratio), the floor is the lowest V/F point the kind's cores have (units.turboFloor, read live) and the ceiling of bucket 1 is the
// CPU's OC ratio limit from the read (cpu_limits, limitsFor). With that limit unread a bucket is never raised above what was read (G-133).
import { h, svg, clear } from "../../components/dom.js";
import * as draftLib from "../../lib/draft.js";
import * as unitsLib from "../../lib/units.js";
import * as limitsLib from "../../lib/limits.js";
import { setPendingAll } from "../tune.js";
import { ocAboveTable } from "./oc.js";
import { isNum, fmtRatio, MINUS, TIMES } from "../../lib/format.js";

export const TURBO_CSS = "styles/turbo.css";
export const TURBO_KINDS = ["P", "E"];
export const BUCKETS = 8;
const KIND_TITLE = { P: "P-cores", E: "E-clusters" };
const KIND_REG = { P: "0x1AD / 0x1AE", E: "0x650 / 0x651" }; // ratios / counts

// ---------------------------------------------------------------- pure helpers (unit-tested without a DOM)

const RATIO_TEXT = /^\s*(\d{1,3})\s*[x×]?\s*$/i;

/** A typed ratio (number or "57" / "57x" / " 57× ") as an integer, or null when it is not one. Fractional, empty and negative text is refused, never rounded. */
export function parseRatio(text) {
  if (isNum(text)) return Number.isInteger(text) ? text : null;
  const m = RATIO_TEXT.exec(String(text === null || text === undefined ? "" : text));
  return m ? Number(m[1]) : null;
}

/** Row label of bucket i: the core count the read carries (0x1AE / 0x651), else the bucket ordinal. `fromRead` says which one it was. */
export function bucketLabel(i, counts) {
  const n = counts && isNum(counts[i]) && counts[i] > 0 ? counts[i] : null;
  const count = n === null ? (Number.isInteger(i) ? i + 1 : 0) : n;
  return { text: count === 1 ? "1 active core" : count + " active cores", count, fromRead: n !== null };
}

/** Lowest ratio a bucket of this kind may hold: the lowest V/F point of its units (read live), never a literal. */
export const bucketFloor = (model, kind) => unitsLib.turboFloor(model, kind) ?? 1;

/** The ratio limit of a kind from the read (the CPU's own OC ratio limit, mailbox 0x01): {max, source} or null when it was not read. */
export function ratioLimit(model, kind) {
  const l = limitsLib.limitsFor(model, kind);
  return l && l.known && l.ratio ? { max: l.ratio.max, source: l.ratio.source } : null;
}

/** Ceiling of bucket i: the bucket to its left; for bucket 0 the effective ratio limit, else the read value (no stated ceiling = no raise, G-133). */
export function bucketCeiling(model, kind, i, eff) {
  if (!Array.isArray(eff) || !Number.isInteger(i) || i < 0) return null;
  if (i > 0) return eff[i - 1];
  const r = ratioLimit(model, kind);
  if (r) return r.max;
  const cur = draftLib.turboCur(model, kind);
  return cur ? cur[0] : null;
}

/** Why a typed value cannot be taken, in one sentence the row can show in place. */
function orderReason(model, kind, i, n, eff) {
  const left = bucketLabel(i - 1, model.turbo && model.turbo[kind] ? model.turbo[kind].counts : null).text;
  return n + TIMES + " is above " + eff[i - 1] + TIMES + " at " + left + ": ratios may only fall as more cores are active. Raise " + left + " first, or pick " + eff[i - 1] + TIMES + " or less.";
}

/**
 * Validate one bucket edit -> {ok: true, value} | {ok: false, code, reason}. `eff` is the effective table (draft over read).
 * Codes: no_table, no_bucket, not_a_ratio, below_floor, turbo_order, above_limit, no_ceiling. A value equal to the effective one is always accepted.
 */
export function checkBucket(model, kind, i, value, eff) {
  const cur = draftLib.turboCur(model, kind);
  if (!cur) return { ok: false, code: "no_table", reason: "the " + kind + " turbo ratio table was not read" };
  if (!Number.isInteger(i) || i < 0 || i >= cur.length) return { ok: false, code: "no_bucket", reason: "no such bucket" };
  const table = Array.isArray(eff) && eff.length === cur.length ? eff : cur;
  const n = parseRatio(value);
  if (n === null) return { ok: false, code: "not_a_ratio", reason: "A turbo ratio is a whole number (for example 57), not \u201C" + String(value) + "\u201D." };
  if (n === table[i]) return { ok: true, value: n };
  const floor = bucketFloor(model, kind);
  if (n < floor) return { ok: false, code: "below_floor", reason: n + TIMES + " is below " + floor + TIMES + ", the lowest V/F point these cores have: a lower ratio is not offered." };
  const ceil = bucketCeiling(model, kind, i, table);
  if (isNum(ceil) && n > ceil) {
    if (i > 0) return { ok: false, code: "turbo_order", reason: orderReason(model, kind, i, n, table) };
    const r = ratioLimit(model, kind);
    return r
      ? { ok: false, code: "above_limit", reason: n + TIMES + " is above the " + r.max + TIMES + " ceiling (" + r.source + ")." }
      : { ok: false, code: "no_ceiling", reason: n + TIMES + " is above the " + cur[0] + TIMES + " value that was read, and the CPU's OC ratio limit (mailbox 0x01) has not been read (Read CPU state first), so this table may only be lowered." };
  }
  return { ok: true, value: n };
}

/**
 * One bucket edit through the draft's turbo API. `value` is typed text or a number. -> {draft, value?, changed?} or {draft (unchanged), code, reason}.
 * The validation above runs first, so a value that would break the order (or the ceiling) never reaches the draft and is never silently clamped.
 */
export function editBucket(model, draft, kind, i, value, api = draftLib) {
  const cur = api.turboCur(model, kind);
  const eff = api.turboEff(model, draft, kind);
  if (!cur || !eff) return { draft, code: "no_table", reason: "the " + kind + " turbo ratio table was not read" };
  const chk = checkBucket(model, kind, i, value, eff);
  if (!chk.ok) return { draft, code: chk.code, reason: chk.reason };
  const next = api.setBucket(model, draft, kind, i, chk.value);
  const after = api.turboEff(model, next, kind);
  if (!after || after[i] !== chk.value) return { draft, code: "not_applied", reason: "the draft would not take " + chk.value + TIMES + " for this bucket" };
  return { draft: next, value: chk.value, changed: next !== draft };
}

/** The ceiling hint of a table with its source (the CPU's OC ratio limit), in one line. */
export function limitHint(model, kind) {
  const l = limitsLib.limitsFor(model, kind);
  if (!l.known) return "The helper did not report the CPU limits, so no bucket may be raised above what was read (lowering stays available).";
  if (!l.ratio) return l.ratioWhy + ", so no bucket may be raised above what was read (lowering stays available).";
  return "Bucket 1 (fewest active cores) may be raised up to " + l.ratio.max + TIMES + " (" + l.ratio.source + "). Lowering a bucket brings the buckets to its right down with it.";
}

// ---- the step chart -------------------------------------------------------------------------------------------------------------------------
export const CHART = { W: 340, H: 120, L: 36, R: 10, T: 10, B: 22 };

/** Geometry of a step chart over the read and the effective table: y spans min-1 .. max+1 so a flat table is not a line on the axis. */
export function chartLayout(read, eff, box = CHART) {
  const all = (Array.isArray(read) ? read : []).concat(Array.isArray(eff) ? eff : []).filter(isNum);
  const lo = (all.length ? Math.min(...all) : 0) - 1;
  const hi = (all.length ? Math.max(...all) : 1) + 1;
  const n = Math.max(Array.isArray(eff) ? eff.length : 0, 1);
  const span = hi - lo || 1;
  return {
    ...box,
    lo,
    hi,
    n,
    x: (i) => box.L + (n === 1 ? (box.W - box.L - box.R) / 2 : (i * (box.W - box.L - box.R)) / (n - 1)),
    y: (v) => box.T + ((hi - v) * (box.H - box.T - box.B)) / span,
  };
}

const px = (v) => Number(v).toFixed(1);

/** "M x0 y0 L x1 y0 L x1 y1 …": a step (hold, then jump) path for one table. */
export function stepPath(arr, lay) {
  return arr
    .map((v, i) => (i ? "L" + px(lay.x(i)) + " " + px(lay.y(arr[i - 1])) + "L" : "M") + px(lay.x(i)) + " " + px(lay.y(v)))
    .join("");
}

// ---------------------------------------------------------------- the view

let V = null; // the one mounted instance

const isDraftValue = (x) => !!x && typeof x === "object" && !Array.isArray(x) && typeof x.setBucket !== "function" && (x.units !== undefined || x.turbo !== undefined);

function pickApi(ctx) {
  const mod = (x, key, fallback) => (x && typeof x === "object" && typeof x[key] === "function" ? x : fallback);
  return { draft: mod(ctx.draft, "setBucket", draftLib), units: mod(ctx.units, "modelFromRead", unitsLib), limits: mod(ctx.limits, "limitsFor", limitsLib) };
}

function stateOf(inst) {
  const s = inst.store && typeof inst.store.get === "function" ? inst.store.get() : null;
  return s || null;
}

function getDraft(inst) {
  const c = inst.ctx;
  if (typeof c.getDraft === "function") {
    const d = c.getDraft();
    if (d) return d;
  }
  if (isDraftValue(c.draft)) return c.draft;
  const s = stateOf(inst);
  if (s && s.tune && isDraftValue(s.tune.draft)) return s.tune.draft;
  return inst.local;
}

function setDraft(inst, next) {
  const c = inst.ctx;
  if (typeof c.setDraft === "function") c.setDraft(next);
  else if (isDraftValue(c.draft)) c.draft = next;
  else if (inst.store && stateOf(inst) && stateOf(inst).tune) inst.store.update((st) => ({ tune: { ...(st.tune || {}), draft: next } }));
  else inst.local = next;
}

function resolveModel(inst) {
  const c = inst.ctx;
  if (c.model) return c.model;
  if (c.read) return inst.api.units.modelFromRead(c.read);
  const s = stateOf(inst);
  if (s) {
    if (s.model) return s.model;
    if (s.tune && s.tune.model) return s.tune.model;
    if (s.read) return inst.api.units.modelFromRead(s.read);
  }
  return null;
}

const effOf = (inst, kind) => inst.api.draft.turboEff(inst.model, getDraft(inst), kind);

function syncPending(inst) {
  if (!inst.store) return;
  const d = inst.api.draft.diff(inst.model, getDraft(inst));
  setPendingAll(inst.store, d.total, d.unitCount);
}

const note = (text) => h("p", { class: "turbo-note", text });

export function mount(el, ctx = {}) {
  unmount();
  if (typeof ctx.getDraft === "function" && typeof ctx.setDraft !== "function") {
    throw new TypeError("turbo.mount: ctx.getDraft needs ctx.setDraft (the shared draft belongs to the host)");
  }
  const inst = {
    el,
    ctx,
    api: pickApi(ctx),
    store: ctx.store || null,
    local: draftLib.emptyDraft(),
    rows: { P: [], E: [] },
  };
  inst.model = resolveModel(inst);
  V = inst;
  el.classList.add("turbo-view");
  build(inst);
  paint(inst);
  syncPending(inst);
}

export function unmount() {
  const inst = V;
  if (!inst) return;
  V = null;
  clear(inst.el);
  inst.el.classList.remove("turbo-view");
  inst.rows = { P: [], E: [] };
}

function build(inst) {
  const el = inst.el;
  clear(el);
  el.append(
    h(
      "div",
      { class: "turbo-head" },
      h("h2", { class: "turbo-title", text: "Turbo ratios" }),
      h("p", { class: "turbo-sub", text: "Ratios by active cores. Fewer active cores may run higher; a bucket can never be above the one to its left." }),
    ),
  );
  if (!inst.model) {
    el.append(note("No CPU state read yet. Use \u201CRead CPU state\u201D on the Tune screen: the tables are read from 0x1AD/0x1AE and 0x650/0x651, and cannot be edited without a read."));
    return;
  }
  if (!inst.model.turbo) {
    el.append(note("The turbo ratio tables were not read, so they cannot be edited. Read the CPU state again (a helper without the table read reports `tables_note`)."));
    return;
  }
  const wrap = h("div", { class: "turbo-tables" });
  for (const kind of TURBO_KINDS) wrap.append(buildKind(inst, kind));
  el.append(wrap, h("p", { class: "turbo-foot", text: "Counts are read-only (from the read). A pending ratio is not written until you review and apply it." }));
}

function buildKind(inst, kind) {
  const t = inst.model.turbo[kind];
  const card = h("section", { class: ["turbo-card", "k-" + kind], dataset: { kind } });
  const chip = h("span", { class: "turbo-dirty", props: { hidden: true } });
  const undo = h("button", { class: ["btn", "turbo-undo"], type: "button", text: "Undo " + KIND_TITLE[kind] + " changes", props: { disabled: true } });
  const chartHost = h("div", { class: "turbo-chart-host" });
  const ocBanner = h("p", { class: "turbo-oc-note", role: "note", props: { hidden: true } });

  const table = h("table", { class: "turbo-table", "aria-label": KIND_TITLE[kind] + " turbo ratio by active cores" });
  const thead = h(
    "thead",
    {},
    h("tr", {}, h("th", { scope: "col", class: "turbo-th-cores", text: "Active cores" }), h("th", { scope: "col", text: "Ratio" }), h("th", { scope: "col", class: "turbo-th-read", text: "Read now" })),
  );
  const tbody = h("tbody", {});
  for (let i = 0; i < BUCKETS; i++) tbody.append(buildRow(inst, kind, i, t));
  table.append(thead, tbody);

  card.append(
    h(
      "div",
      { class: "turbo-card-head" },
      h("h3", { class: "turbo-kind", text: KIND_TITLE[kind] }),
      h("span", { class: "turbo-reg", text: KIND_REG[kind], title: "ratio table / active-core counts (registers.md)" }),
      h("span", { class: "spacer" }),
      chip,
      undo,
    ),
    ocBanner,
    table,
    chartHost,
    h("p", { class: "turbo-limit", text: limitHint(inst.model, kind) }),
    h("p", { class: "turbo-help", text: "Typed values and arrow keys step by 1. " + (t.counts ? "" : "The read carried no bucket counts, so the labels show the bucket ordinal. ") + "Ratios may only fall as more cores are active." }),
  );

  undo.addEventListener("click", () => {
    if (V !== inst) return;
    const next = inst.api.draft.discardTurbo(getDraft(inst), kind);
    setDraft(inst, next);
    syncPending(inst);
    paint(inst);
    if (typeof inst.ctx.onChange === "function") inst.ctx.onChange(next, inst.model);
  });

  inst.card = inst.card || {};
  inst.card[kind] = { card, chip, undo, chartHost, tbody, ocBanner };
  return card;
}

function buildRow(inst, kind, i, t) {
  const cur = t.ratios[i];
  const label = bucketLabel(i, t.counts);
  const dot = h("span", { class: "pending-dot turbo-dot", "aria-hidden": "true" });
  const minus = h("button", { class: "turbo-step", type: "button", text: MINUS, "aria-label": "Lower the ratio for " + label.text, title: "1\u00d7 less" });
  const plus = h("button", { class: "turbo-step", type: "button", text: "+", "aria-label": "Raise the ratio for " + label.text, title: "1\u00d7 more" });
  const input = h("input", {
    class: "turbo-num",
    type: "text",
    inputmode: "numeric",
    autocomplete: "off",
    spellcheck: "false",
    role: "spinbutton",
    "aria-label": label.text + " ratio, " + KIND_TITLE[kind],
    props: { value: String(cur) },
  });
  const reason = h("div", { class: "turbo-reason", "aria-live": "polite", props: { hidden: true } });
  const read = h("span", { class: "turbo-read num", text: fmtRatio(cur) });
  const labelEl = h("span", { class: "turbo-cores-text", text: label.text });
  const th = h("th", { scope: "row", class: "turbo-cores", title: "bucket " + (i + 1) + " of " + BUCKETS }, labelEl);

  const row = h(
    "tr",
    { class: "turbo-row", dataset: { bucket: i } },
    th,
    h("td", { class: "turbo-cell" }, h("div", { class: "turbo-stepper" }, dot, minus, input, h("span", { class: "turbo-x", "aria-hidden": "true", text: TIMES }), plus), reason),
    h("td", { class: "turbo-read-cell" }, read),
  );

  minus.addEventListener("click", () => step(inst, kind, i, -1));
  plus.addEventListener("click", () => step(inst, kind, i, 1));
  input.addEventListener("input", () => typed(inst, kind, i));
  input.addEventListener("keydown", (ev) => key(inst, kind, i, ev));
  input.addEventListener("blur", () => resync(inst, kind, i));
  input.addEventListener("focus", () => input.removeAttribute("aria-invalid"));

  const ref = { row, input, minus, plus, reason, read, dot, labelEl };
  inst.rows[kind][i] = ref;
  return row;
}

function showReason(ref, text) {
  ref.reason.textContent = text;
  ref.reason.hidden = false;
  ref.row.classList.add("refused");
}

function clearReason(ref) {
  ref.reason.hidden = true;
  ref.reason.textContent = "";
  ref.row.classList.remove("refused");
  ref.input.removeAttribute("aria-invalid");
}

function setInput(ref, value) {
  ref.input.value = String(value);
  ref.input.removeAttribute("aria-invalid");
}

/** One accepted edit: validate, apply through the draft, refresh the pending bar and repaint. */
function commit(inst, kind, i, value) {
  if (V !== inst) return { ok: false, reason: "the view is not mounted" };
  const before = getDraft(inst);
  const out = editBucket(inst.model, before, kind, i, value, inst.api.draft);
  if (out.reason) return { ok: false, reason: out.reason };
  if (out.changed) {
    setDraft(inst, out.draft);
    syncPending(inst);
    paint(inst);
    if (typeof inst.ctx.onChange === "function") inst.ctx.onChange(out.draft, inst.model);
  }
  return { ok: true, value: out.value };
}

function step(inst, kind, i, d) {
  const ref = inst.rows[kind][i];
  if (!ref) return;
  const eff = effOf(inst, kind);
  const out = commit(inst, kind, i, eff[i] + d);
  if (!out.ok) {
    showReason(ref, out.reason);
    return;
  }
  clearReason(ref);
  setInput(ref, effOf(inst, kind)[i]);
}

function typed(inst, kind, i) {
  const ref = inst.rows[kind][i];
  if (!ref) return;
  const out = commit(inst, kind, i, ref.input.value);
  if (!out.ok) {
    ref.input.setAttribute("aria-invalid", "true");
    showReason(ref, out.reason);
    return;
  }
  clearReason(ref);
}

function key(inst, kind, i, ev) {
  const ref = inst.rows[kind][i];
  if (!ref) return;
  if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
    ev.preventDefault();
    step(inst, kind, i, ev.key === "ArrowUp" ? 1 : -1);
    return;
  }
  if (ev.key === "Enter") {
    ev.preventDefault();
    const out = commit(inst, kind, i, ref.input.value);
    if (!out.ok) showReason(ref, out.reason);
    else clearReason(ref);
    return;
  }
  if (ev.key === "Escape") {
    ev.preventDefault();
    resync(inst, kind, i);
  }
}

/** The field shows the draft value again (blur, Escape): an unaccepted text is dropped, never silently kept. */
function resync(inst, kind, i) {
  const ref = inst.rows[kind][i];
  if (!ref) return;
  clearReason(ref);
  setInput(ref, effOf(inst, kind)[i]);
}

function paint(inst) {
  if (!inst.model || !inst.model.turbo) return;
  for (const kind of TURBO_KINDS) {
    const t = inst.model.turbo[kind];
    const cur = t.ratios;
    const eff = effOf(inst, kind);
    const card = inst.card ? inst.card[kind] : null;
    let dirty = 0;
    for (let i = 0; i < BUCKETS; i++) {
      const ref = inst.rows[kind][i];
      if (!ref) continue;
      const changed = eff[i] !== cur[i];
      if (changed) dirty++;
      ref.row.classList.toggle("changed", changed);
      ref.dot.classList.toggle("on", changed);
      ref.read.textContent = fmtRatio(cur[i]);
      ref.labelEl.textContent = bucketLabel(i, t.counts).text;
      ref.minus.disabled = !checkBucket(inst.model, kind, i, eff[i] - 1, eff).ok;
      ref.plus.disabled = !checkBucket(inst.model, kind, i, eff[i] + 1, eff).ok;
      ref.input.setAttribute("aria-valuenow", String(eff[i]));
      ref.input.setAttribute("aria-valuemin", String(bucketFloor(inst.model, kind)));
      const ceil = bucketCeiling(inst.model, kind, i, eff);
      if (isNum(ceil)) ref.input.setAttribute("aria-valuemax", String(ceil));
      else ref.input.removeAttribute("aria-valuemax");
      if (typeof document === "undefined" || document.activeElement !== ref.input) {
        setInput(ref, eff[i]);
        clearReason(ref);
      }
    }
    if (card) {
      card.card.classList.toggle("pending", dirty > 0);
      card.chip.hidden = dirty === 0;
      card.chip.textContent = dirty + (dirty === 1 ? " bucket changed" : " buckets changed");
      card.undo.disabled = dirty === 0;
      paintOcNote(card.ocBanner, ocAboveTable(inst.model, getDraft(inst), kind));
      clear(card.chartHost);
      card.chartHost.append(chartFor(kind, cur, eff, t.counts));
    }
  }
}

/** D-050: the (i) banner of a table that is below the OC ratio of some of its units (drafted or read): the units and their ratios; the table is not touched for you. */
function paintOcNote(el, over) {
  el.hidden = !over;
  if (!over) {
    el.textContent = "";
    return;
  }
  const list = over.units.map((x) => x.id + " " + x.ratio + TIMES).join(", ");
  el.textContent = "\u24D8 OC ratio above this table: " + list + ". The table is separate from the OC ratio (D-050), so " + (over.units.length === 1 ? "that core runs" : "those cores run") + " at most " + over.top + TIMES + " until you raise it here.";
}

function chartFor(kind, read, eff, counts) {
  const lay = chartLayout(read, eff);
  const label =
    KIND_TITLE[kind] + " turbo ratio by active cores: " + eff.map((v, i) => bucketLabel(i, counts).text + " " + v + TIMES).join(", ") + ". Read: " + read.map((v) => v + TIMES).join(", ") + ".";
  const s = svg("svg", { class: "turbo-chart", viewBox: "0 0 " + lay.W + " " + lay.H, role: "img", "aria-label": label });
  for (const v of [Math.ceil(lay.lo), Math.floor(lay.hi)]) {
    s.append(svg("line", { class: "tb-grid", x1: px(lay.L), x2: px(lay.W - lay.R), y1: px(lay.y(v)), y2: px(lay.y(v)) }));
    const tick = svg("text", { class: "tb-tick", x: px(lay.L - 5), y: px(lay.y(v) + 4), "text-anchor": "end" }, String(v) + TIMES);
    s.append(tick);
  }
  for (let i = 0; i < lay.n; i++) {
    const c = bucketLabel(i, counts).count;
    s.append(svg("text", { class: "tb-tick", x: px(lay.x(i)), y: px(lay.H - 6), "text-anchor": "middle" }, String(c)));
  }
  if (read.some((v, i) => v !== eff[i])) s.append(svg("path", { class: "tb-line tb-cur", d: stepPath(read, lay) }));
  s.append(svg("path", { class: "tb-line tb-eff", d: stepPath(eff, lay) }));
  eff.forEach((v, i) => s.append(svg("circle", { class: "tb-dot k-" + kind + (v !== read[i] ? " changed" : ""), cx: px(lay.x(i)), cy: px(lay.y(v)), r: "3.5" })));
  return s;
}
