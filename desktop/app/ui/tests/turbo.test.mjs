// node --test "desktop/app/ui/tests/*.test.mjs"   (G-209: turbo ratio tables — validation, the draft round-trip, and the rendered sub-tab under a
// minimal fake DOM. No browser, no helper, no hardware: the recorded simulator fixture is the only input.)
import test from "node:test";
import assert from "node:assert/strict";
import FIX from "../fixtures/fixtures.js";
import { modelFromRead } from "../lib/units.js";
import { emptyDraft, diff, turboCur } from "../lib/draft.js";
import { buildIntent } from "../lib/intent.js";
import { createAppStore } from "../store.js";

// ---------------------------------------------------------------- a minimal DOM (only what components/dom.js and the view touch)
class FNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this._text = "";
    this._attrs = new Map();
    this._listeners = new Map();
    const classes = new Set();
    this.classList = {
      add: (...cs) => cs.forEach((c) => String(c).split(/\s+/).filter(Boolean).forEach((x) => classes.add(x))),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      contains: (c) => classes.has(c),
      toggle: (c, on) => {
        const v = on === undefined ? !classes.has(c) : !!on;
        if (v) classes.add(c);
        else classes.delete(c);
        return v;
      },
    };
  }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text;
  }
  set textContent(v) {
    this._text = String(v);
    this.children = [];
  }
  append(...nodes) {
    for (const n of nodes.flat()) {
      if (n === null || n === undefined) continue;
      n.parentNode = this;
      this.children.push(n);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  setAttribute(k, v) {
    this._attrs.set(k, String(v));
  }
  getAttribute(k) {
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  removeAttribute(k) {
    this._attrs.delete(k);
  }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  dispatch(type, extra = {}) {
    const ev = { type, target: this, preventDefault() {}, ...extra };
    for (const fn of [...(this._listeners.get(type) || [])]) fn(ev);
    return ev;
  }
  all(pred, out = []) {
    for (const c of this.children) {
      if (pred(c)) out.push(c);
      c.all(pred, out);
    }
    return out;
  }
  byClass(cls) {
    return this.all((n) => n.classList.contains(cls));
  }
  byTag(tag) {
    return this.all((n) => n.tagName === String(tag).toUpperCase());
  }
  first(cls) {
    return this.byClass(cls)[0] || null;
  }
}

globalThis.Node = FNode;
globalThis.document = {
  activeElement: null,
  createElement: (t) => new FNode(t),
  createElementNS: (_ns, t) => new FNode(t),
  createTextNode: (t) => {
    const n = new FNode("#text");
    n.textContent = t;
    return n;
  },
};

const turbo = await import("../views/tune/turbo.js");
const {
  TURBO_CSS, parseRatio, bucketLabel, bucketFloor, bucketCeiling, ratioLimit, checkBucket, editBucket, limitHint, chartLayout, stepPath,
  CHART, mount, unmount,
} = turbo;

const READ = FIX["tune-read"].result;
const model = (patch = {}) => modelFromRead({ ...structuredClone(READ), ...patch });

/** Mount on the fixture model with a host-owned draft (the way views/tune.js will). */
function host(patch = {}, ctxExtra = {}) {
  const m = patch === null ? null : model(patch);
  const store = createAppStore();
  let draft = emptyDraft();
  const el = new FNode("div");
  mount(el, { store, model: m, getDraft: () => draft, setDraft: (d) => (draft = d), ...ctxExtra });
  return { el, store, model: m, draft: () => draft };
}

/** The eight body rows of the P table (the first of the two tables). */
const pRows = (el) => el.byClass("turbo-table")[0].byClass("turbo-row");
const inputOf = (row) => row.byTag("input")[0];
const btnOf = (row, label) => row.byTag("button").find((b) => b.textContent === label);

// ---------------------------------------------------------------- validation (pure)
test("turbo: the stylesheet path constant points at the repo stylesheet", () => {
  assert.equal(TURBO_CSS, "styles/turbo.css");
  assert.match(TURBO_CSS, /^styles\/[a-z]+\.css$/);
});

test("turbo: a typed ratio is a whole number, never a rounded one", () => {
  assert.equal(parseRatio("57"), 57);
  assert.equal(parseRatio(" 57 "), 57);
  assert.equal(parseRatio("57x"), 57);
  assert.equal(parseRatio("57×"), 57);
  assert.equal(parseRatio(58), 58);
  assert.equal(parseRatio("57.5"), null);
  assert.equal(parseRatio(""), null);
  assert.equal(parseRatio("abc"), null);
  assert.equal(parseRatio("-3"), null);
  assert.equal(parseRatio("0"), 0, "0 parses; the floor check refuses it");
});

test("turbo: row labels use the bucket counts the read carries, else the ordinal", () => {
  assert.deepEqual(bucketLabel(0, [1, 2, 3, 4, 5, 6, 7, 8]), { text: "1 active core", count: 1, fromRead: true });
  assert.deepEqual(bucketLabel(7, [1, 2, 3, 4, 6, 8, 12, 16]), { text: "16 active cores", count: 16, fromRead: true });
  assert.deepEqual(bucketLabel(2, null), { text: "3 active cores", count: 3, fromRead: false });
});

test("turbo: floor is the lowest live V/F point, the ceiling of bucket 1 is the read ratio limit", () => {
  const m = model();
  assert.equal(bucketFloor(m, "P"), 8);
  assert.equal(bucketFloor(m, "E"), 8);
  const eff = turboCur(m, "P").slice();
  assert.equal(bucketCeiling(m, "P", 0, eff), 60, "policy ∩ CPU ratio_max");
  assert.equal(bucketCeiling(m, "P", 3, eff), eff[2]);
  assert.deepEqual(ratioLimit(m, "P"), { max: 60, source: "policy (auto: the CPU's own limit)", policy: 60, cpu: 120 });
  assert.equal(bucketCeiling(model({ limits: null }), "P", 0, eff), eff[0], "no stated ceiling = no raise above the read (G-133)");
});

test("turbo: checkBucket refuses the order, the ceiling and the floor with a reason", () => {
  const m = model();
  const eff = turboCur(m, "P").slice();

  assert.deepEqual(checkBucket(m, "P", 0, 58, eff), { ok: true, value: 58 }, "raising bucket 1 is allowed up to the limit");

  const order = checkBucket(m, "P", 2, 58, eff);
  assert.equal(order.ok, false);
  assert.equal(order.code, "turbo_order");
  assert.match(order.reason, /58× is above 57×/);
  assert.deepEqual(checkBucket(m, "P", 2, 56, eff), { ok: true, value: 56 }, "still at or below the bucket above it");
  assert.match(order.reason, /may only fall/);

  const over = checkBucket(m, "P", 0, 61, eff);
  assert.equal(over.code, "above_limit");
  assert.match(over.reason, /60×/);
  assert.match(over.reason, /policy/);

  assert.equal(checkBucket(m, "P", 4, "5", eff).code, "below_floor");
  assert.equal(checkBucket(m, "P", 4, "", eff).code, "not_a_ratio");
  assert.equal(checkBucket(m, "P", 4, "5.5", eff).code, "not_a_ratio");
  assert.deepEqual(checkBucket(m, "P", 4, 55, eff), { ok: true, value: 55 }, "no-op always accepted");

  const nope = checkBucket(model({ limits: null }), "P", 0, 58, eff);
  assert.equal(nope.code, "no_ceiling");
  assert.match(nope.reason, /may only be lowered/);

  assert.equal(checkBucket(model({ turbo: null, tables: null }), "P", 0, 50, eff).code, "no_table");
  assert.equal(checkBucket(m, "P", 9, 50, eff).code, "no_bucket");
});

// ---------------------------------------------------------------- the draft round-trip + golden intents
test("turbo: an invalid value never reaches the draft; a valid one does, through the draft's turbo API", () => {
  const m = model();
  const d0 = emptyDraft();
  const refused = editBucket(m, d0, "P", 2, 58);
  assert.equal(refused.draft, d0, "the draft is untouched");
  assert.equal(refused.code, "turbo_order");
  assert.equal(diff(m, refused.draft).total, 0);

  const ok = editBucket(m, d0, "P", 0, 58);
  assert.deepEqual(ok.draft.turbo.P, [58, 57, 55, 55, 55, 55, 55, 55]);
  assert.equal(diff(m, ok.draft).turbo.P, 1);
});

test("turbo: lowering a bucket brings the buckets on its right down with it (golden P intent)", () => {
  const m = model();
  const d = editBucket(m, emptyDraft(), "P", 5, 50).draft;
  assert.deepEqual(d.turbo.P, [57, 57, 55, 55, 55, 50, 50, 50]);
  assert.equal(diff(m, d).turbo.P, 3);
  assert.deepEqual(buildIntent(m, d), { intent: { turbo: { P: [57, 57, 55, 55, 55, 50, 50, 50] } } });
});

test("turbo: both tables in one intent are exactly {turbo:{P:[...8],E:[...8]}}", () => {
  const m = model();
  let d = editBucket(m, emptyDraft(), "P", 0, 58).draft;
  d = editBucket(m, d, "E", 3, 47).draft;
  assert.deepEqual(d.turbo.E, [48, 48, 48, 47, 47, 47, 47, 47]);
  const built = buildIntent(m, d);
  assert.deepEqual(Object.keys(built.intent), ["turbo"]);
  assert.deepEqual(Object.keys(built.intent.turbo), ["P", "E"]);
  assert.equal(built.intent.turbo.P.length, 8);
  assert.equal(built.intent.turbo.E.length, 8);
  assert.deepEqual(built.intent, { turbo: { P: [58, 57, 55, 55, 55, 55, 55, 55], E: [48, 48, 48, 47, 47, 47, 47, 47] } });
  assert.equal(diff(m, d).turbo.E, 5, "one bucket lowered, five differ from the read");
});

test("turbo: the limit hint names the policy file and the CPU's own ceiling", () => {
  const line = limitHint(model(), "P");
  assert.match(line, /60×/);
  assert.match(line, /policy/i);
  assert.match(line, /120×/);
  assert.match(limitHint(model({ limits: null }), "P"), /no bucket may be raised/);
});

test("turbo: the step chart keeps eight buckets and a flat table off the axis", () => {
  const lay = chartLayout([57, 57, 55, 55, 55, 55, 55, 55], [58, 57, 55, 55, 55, 55, 55, 55]);
  assert.equal(lay.W, CHART.W);
  assert.equal(lay.n, 8);
  assert.ok(lay.y(57) < lay.y(55), "a higher ratio sits higher");
  assert.ok(lay.lo < 55 && lay.hi > 58);
  const path = stepPath([57, 57, 55], lay);
  assert.match(path, /^M[\d.]+ [\d.]+L/);
  assert.equal((path.match(/L/g) || []).length, 4, "a step holds, then jumps: 2 segments of 2 commands");
});

// ---------------------------------------------------------------- rendering
test("turbo: mount renders an active-cores table per kind, the read-only counts and the values as read", () => {
  const v = host();
  const tables = v.el.byClass("turbo-table");
  assert.equal(tables.length, 2);
  for (const t of tables) assert.equal(t.byClass("turbo-row").length, 8);

  const p = pRows(v.el);
  assert.equal(p[0].first("turbo-cores-text").textContent, "1 active core");
  assert.equal(p[7].first("turbo-cores-text").textContent, "8 active cores");
  assert.deepEqual(p.map((r) => r.first("turbo-read").textContent), ["57×", "57×", "55×", "55×", "55×", "55×", "55×", "55×"]);
  assert.deepEqual(p.map((r) => inputOf(r).value), ["57", "57", "55", "55", "55", "55", "55", "55"]);

  const e = tables[1].byClass("turbo-row");
  assert.deepEqual(
    e.map((r) => r.first("turbo-cores-text").textContent),
    ["1 active core", "2 active cores", "3 active cores", "4 active cores", "6 active cores", "8 active cores", "12 active cores", "16 active cores"],
  );

  assert.equal(v.el.byClass("turbo-limit").length, 2);
  assert.equal(v.el.byClass("turbo-chart").length, 2);
  assert.equal(v.el.byClass("tb-eff").length, 2);
  assert.equal(v.el.byClass("turbo-dirty").filter((c) => !c.hidden).length, 0, "nothing pending at mount");
  unmount();
});

test("turbo: typing an out-of-order value shows a reason in the row and leaves the draft alone", () => {
  const v = host();
  const row = pRows(v.el)[4]; // 5 active cores, 55x, left bucket 55x
  const input = inputOf(row);
  document.activeElement = input;
  input.value = "58";
  input.dispatch("input");

  assert.equal(v.draft().turbo.P, undefined, "refused: the draft still equals the read");
  assert.equal(row.first("turbo-reason").hidden, false);
  assert.match(row.first("turbo-reason").textContent, /58× is above 55×/);
  assert.equal(input.getAttribute("aria-invalid"), "true");
  assert.equal(row.classList.contains("refused"), true);
  assert.equal(v.store.get().pending.changes, 0);

  input.value = "54";
  input.dispatch("input");
  assert.deepEqual(v.draft().turbo.P, [57, 57, 55, 55, 54, 54, 54, 54], "54 landed, and the buckets to its right came down");
  assert.equal(row.first("turbo-reason").hidden, true);
  assert.equal(input.getAttribute("aria-invalid"), null);
  document.activeElement = null;
  unmount();
});

test("turbo: a valid typed edit marks the row, the card and the pending bar", () => {
  const v = host();
  const row = pRows(v.el)[5];
  const input = inputOf(row);
  document.activeElement = input;
  input.value = "50";
  input.dispatch("input");

  assert.deepEqual(v.draft().turbo.P, [57, 57, 55, 55, 55, 50, 50, 50]);
  assert.equal(row.classList.contains("changed"), true);
  assert.equal(row.first("turbo-dot").classList.contains("on"), true);
  assert.equal(pRows(v.el)[6].classList.contains("changed"), true, "the bucket to its right came down with it");
  assert.equal(inputOf(pRows(v.el)[6]).value, "50");
  const chip = v.el.byClass("turbo-dirty")[0];
  assert.equal(chip.hidden, false);
  assert.match(chip.textContent, /3 buckets changed/);
  assert.deepEqual(v.store.get().pending, { changes: 3, units: 1 });
  document.activeElement = null;
  unmount();
});

test("turbo: stepper buttons and arrow keys step by one and stop at the ceiling and the floor", () => {
  const v = host();
  const row0 = pRows(v.el)[0];
  const plus = btnOf(row0, "+");
  const minus = btnOf(row0, "−");
  const input = inputOf(row0);

  plus.dispatch("click");
  assert.equal(v.draft().turbo.P[0], 58);
  assert.equal(input.value, "58", "the field follows the button");
  plus.dispatch("click");
  plus.dispatch("click");
  assert.equal(v.draft().turbo.P[0], 60);
  assert.equal(plus.disabled, true, "60× is the read limit");

  input.dispatch("keydown", { key: "ArrowDown" });
  assert.equal(v.draft().turbo.P[0], 59);
  input.dispatch("keydown", { key: "ArrowUp" });
  assert.equal(v.draft().turbo.P[0], 60);
  input.dispatch("keydown", { key: "ArrowUp" });
  assert.equal(v.draft().turbo.P[0], 60, "refused at the ceiling, never 61");
  assert.equal(row0.first("turbo-reason").hidden, false);
  assert.match(row0.first("turbo-reason").textContent, /60×/);

  minus.dispatch("click");
  assert.equal(v.draft().turbo.P[0], 59);

  // the floor: the last bucket lowered to the lowest V/F point, then minus is off
  const row7 = pRows(v.el)[7];
  const in7 = inputOf(row7);
  document.activeElement = in7;
  in7.value = "8";
  in7.dispatch("input");
  assert.equal(v.draft().turbo.P[7], 8);
  assert.equal(btnOf(row7, "−").disabled, true);
  document.activeElement = null;
  unmount();
});

test("turbo: Escape drops an unaccepted text, the undo button clears the kind's draft", () => {
  const v = host();
  const row = pRows(v.el)[3];
  const input = inputOf(row);
  document.activeElement = input;
  input.value = "59"; // above the left bucket (55x)
  input.dispatch("input");
  assert.equal(row.first("turbo-reason").hidden, false);
  input.dispatch("keydown", { key: "Escape" });
  assert.equal(input.value, "55");
  assert.equal(row.first("turbo-reason").hidden, true);

  input.value = "54";
  input.dispatch("input");
  assert.ok(v.draft().turbo.P);
  const undo = v.el.byClass("turbo-undo")[0];
  assert.equal(undo.disabled, false);
  undo.dispatch("click");
  assert.equal(v.draft().turbo.P, undefined);
  assert.equal(undo.disabled, true);
  assert.equal(v.el.byClass("turbo-dirty")[0].hidden, true);
  assert.deepEqual(v.store.get().pending, { changes: 0, units: 0 });
  document.activeElement = null;
  unmount();
});

test("turbo: without a read (or without tables) the view says so instead of drawing empty controls", () => {
  const el = new FNode("div");
  mount(el, { getDraft: () => emptyDraft(), setDraft: () => {} });
  assert.equal(el.byClass("turbo-table").length, 0);
  assert.match(el.first("turbo-note").textContent, /No CPU state read yet/);
  unmount();

  const el2 = new FNode("div");
  mount(el2, { model: model({ turbo: null, tables: null }), getDraft: () => emptyDraft(), setDraft: () => {} });
  assert.equal(el2.byClass("turbo-table").length, 0);
  assert.match(el2.first("turbo-note").textContent, /were not read/);
  unmount();
});

test("turbo: unmount clears the host, and a detached control can no longer edit the draft", () => {
  const v = host();
  const plus = btnOf(pRows(v.el)[0], "+");
  unmount();
  assert.equal(v.el.children.length, 0);
  assert.equal(v.el.classList.contains("turbo-view"), false);

  plus.dispatch("click");
  assert.equal(v.draft().turbo.P, undefined);
  assert.equal(v.store.get().pending.changes, 0);
});

test("turbo: the draft may also be handed in through ctx.draft (a value), and a getDraft without setDraft throws", () => {
  const m = model();
  const el = new FNode("div");
  const ctx = { model: m, draft: emptyDraft() };
  mount(el, ctx);
  const input = inputOf(pRows(el)[0]);
  input.value = "58";
  input.dispatch("input");
  assert.equal(ctx.draft.turbo.P[0], 58, "the host's draft was replaced with the edited one");
  unmount();

  assert.throws(() => mount(new FNode("div"), { model: m, getDraft: () => emptyDraft() }), /needs ctx.setDraft/);
});
