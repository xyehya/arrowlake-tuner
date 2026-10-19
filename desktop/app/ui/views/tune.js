// views/tune.js: the Tune screen (PLAN §4.2): the "Read CPU state" action, the sub-tab bar (Cores · Turbo ratios · Power · Ring & uncore · Ratios & parking) and the Cores tab (tile grid + inspector).
//
// State lives in the app store, slice `tune`, so the draft survives leaving the screen and the pending bar / Playwright / other sub-tabs all see the same thing:
//   { model (lib/units.js, or null before the first read), draft (lib/draft.js), selection [unit ids], anchor, tileMode "ratio"|"live", subtab,
//     read {state idle|reading|ready|error, step, of, startedAt, durationMs, ops, at, error, fromLast},
//     base {state none|loading|ready, fn (unitId, ratio) -> {v, src} | null, name, id, reason}, reviewSeq }
// Every change of `model` or `draft` re-counts the pending bar (store.pending = {changes, units} from lib/draft.js diff()), so any editor, G-209's turbo tab included, is counted by editing the draft.
//
// SUB-TAB CONTRACT (shared with G-209 / G-217): a sub-tab module exports `mount(el, ctx)` and `unmount()`. ctx =
//   { store, bridge, toast,
//     draft:  { get() -> draft, set(next), edit((model, draft) -> next draft), discard(), lib (the lib/draft.js module: setBucket, setPoint, diff, ...) },
//     units:  the lib/units.js module,   limits: the lib/limits.js module,
//     model:  () -> the units model of the current read (null before the first read; the tab is remounted when a new read replaces it),
//     tune:   the controller (state(), setSelection(), readCpu(), ...) }
// tune.js owns the tab bar and mounts the modules of SUBTAB_MODULES: turbo.js (G-209, needs a tune.read), power.js / ring.js / runtime.js (G-217: they read power.read / runtime.read themselves, so no
// tune.read is needed; the ring tab shows its V/F editor once there is one). "Ratio caps" is PLAN's "Ratios & parking" without parking (held, D-048).
//
// app.js integration: `VIEWS.tune = tuneView`; the pending bar's Discard calls `discardDraft(store)` and Review calls `requestReview(store)`, which bumps `tune.reviewSeq` and dispatches the
// DOM event "tune:review" ({detail: {changes, units}}) on `store.events` (an EventTarget; it exists from the first discardDraft / requestReview / Tune mount on). G-210 listens to that.
import { h, clear } from "../components/dom.js";
import { fmtClock, plural } from "../components/fmt.js";
import { setPending, setLastRead, pushToast } from "../store.js";
import * as unitsLib from "../lib/units.js";
import * as draftLib from "../lib/draft.js";
import * as limitsLib from "../lib/limits.js";
import { pendingCounts, discardRt } from "../lib/rtstate.js";
import { requestReview as requestRuntimeReview } from "./tune/runtime-kinds.js";
import { createGrid } from "./tune/grid.js";
import { createInspector } from "./tune/inspector.js";

export const SUBTABS = [
  { id: "cores", label: "Cores" },
  { id: "turbo", label: "Turbo ratios" },
  { id: "power", label: "Power" },
  { id: "ring", label: "Ring & uncore" },
  { id: "ratios", label: "Ratio caps" }, // "Ratios & parking" of PLAN §4.2 minus parking (held, D-048)
];
/** The sub-tabs that are separate modules (`mount(el, ctx)` / `unmount()`), and whether they need a tune.read first (the runtime tabs read power.read / runtime.read themselves, G-217). */
export const SUBTAB_MODULES = {
  turbo: { path: "./tune/turbo.js", needsRead: true },
  power: { path: "./tune/power.js", needsRead: false },
  ring: { path: "./tune/ring.js", needsRead: false },
  ratios: { path: "./tune/runtime.js", needsRead: false },
};
export const NO_BASELINE_REASON = "no baseline curve (Curves → Measure)";

// ---- the store slice ----------------------------------------------------------------------------------------------------------------------
export const initialTune = () => ({
  model: null,
  draft: draftLib.emptyDraft(),
  selection: [],
  anchor: null,
  tileMode: "ratio",
  subtab: "cores",
  read: { state: "idle", step: 0, of: 0, startedAt: null, durationMs: null, ops: null, at: null, error: null, fromLast: false },
  base: { state: "none", fn: null, name: "", id: null, reason: "baseline not loaded yet" },
  reviewSeq: 0,
});

const watched = new WeakSet();

export function ensureTune(store) {
  if (!store.events) store.events = new EventTarget(); // the "tune:review" channel (see requestReview)
  if (!store.get().tune) store.set({ tune: initialTune() });
  if (!watched.has(store) && typeof store.watch === "function") {
    watched.add(store);
    store.watch((s) => s.rt, () => syncPending(store)); // the runtime edits (power, ratio caps, uncore) are part of the same pending bar
  }
  return store.get().tune;
}

/**
 * Write the pending bar: the V/F + turbo + ring draft counts {changes, units} plus the pending runtime edits (power / ratio caps / uncore, lib/rtstate.js; the runtime tabs show
 * "N runtime, M V/F" with M = pending.changes - pendingCounts(rt).total).
 */
export function setPendingAll(store, changes, units) {
  const rt = store.get().rt;
  const c = rt ? pendingCounts(rt) : { power: 0, caps: 0, uncore: 0, total: 0 };
  setPending(store, changes + c.total, units + (c.power ? 1 : 0) + c.caps + (c.uncore ? 1 : 0));
}

/** Re-count the pending bar from the slice: {changes, units} of diff(model, draft), plus the runtime edits. */
export function syncPending(store) {
  const t = store.get().tune;
  if (!t || !t.model) return setPendingAll(store, 0, 0);
  const d = draftLib.diff(t.model, t.draft);
  return setPendingAll(store, d.total, d.unitCount);
}

const hasRuntimeEdits = (rt) => !!rt && ["power", "caps", "uncore"].some((k) => rt.edits && Object.keys(rt.edits[k] || {}).length > 0);

export function patchTune(store, patch) {
  ensureTune(store);
  store.update((s) => ({ tune: { ...s.tune, ...patch } }));
  if ("draft" in patch || "model" in patch) syncPending(store);
}

export function discardDraft(store) {
  ensureTune(store);
  if (hasRuntimeEdits(store.get().rt)) discardRt(store);
  patchTune(store, { draft: draftLib.emptyDraft() });
}

/**
 * The pending bar's "Review & Apply": counts a request in the slice and fires "tune:review" on store.events. With pending runtime edits (power, ratio caps, uncore) one drawer covers
 * everything: the V/F + turbo + ring draft first, then power, then the caps / uncore (views/tune/runtime-kinds.js builds those requests).
 */
export function requestReview(store) {
  const t = ensureTune(store);
  if (hasRuntimeEdits(store.get().rt)) {
    patchTune(store, { reviewSeq: t.reviewSeq + 1 });
    return requestRuntimeReview(store);
  }
  const d = t.model ? draftLib.diff(t.model, t.draft) : { total: 0, unitCount: 0 };
  patchTune(store, { reviewSeq: t.reviewSeq + 1 });
  store.events.dispatchEvent(new CustomEvent("tune:review", { detail: { changes: d.total, units: d.unitCount } }));
}

// ---- the baseline curve -> predict.js `base` ---------------------------------------------------------------------------------------------------
/**
 * A stored curve record (curves.get) -> `(unitId, ratio) => {v, src} | null` for lib/predict.js. A measured point (status ok) gives its VID; an interpolated one is marked so;
 * a ratio between two measured points is the straight line between them (web/app.js:2783, G-133); nothing is extrapolated. null = no voltage known (the predictor falls back to
 * the fused top point only at the top ratio).
 */
export function baselineFromRecord(rec) {
  const pointsOf = (unitId) => {
    const u = rec && rec.units && typeof rec.units === "object" ? rec.units[unitId] : null;
    return u && Array.isArray(u.points) ? u.points.filter((p) => p && Number.isFinite(p.ratio) && Number.isFinite(p.vid_v) && (p.status === "ok" || p.status === "interpolated")) : [];
  };
  return (unitId, ratio) => {
    const ps = pointsOf(unitId);
    const exact = ps.find((p) => p.ratio === ratio);
    if (exact) return { v: exact.vid_v, src: exact.status === "interpolated" ? "interpolated" : "baseline" };
    const ok = ps.filter((p) => p.status === "ok");
    const lo = ok.filter((p) => p.ratio < ratio).sort((a, b) => b.ratio - a.ratio)[0];
    const hi = ok.filter((p) => p.ratio > ratio).sort((a, b) => a.ratio - b.ratio)[0];
    if (!lo || !hi) return null;
    const v = lo.vid_v + ((hi.vid_v - lo.vid_v) * (ratio - lo.ratio)) / (hi.ratio - lo.ratio);
    return { v: Math.min(Math.max(v, Math.min(lo.vid_v, hi.vid_v)), Math.max(lo.vid_v, hi.vid_v)), src: "interpolated" };
  };
}

async function fetchCurve(bridge, id) {
  const first = await bridge.call("curves.get", { id });
  if (!first.ok) return { error: first.error.message };
  let rec = first.result;
  // a record too big for one line comes back with omitted_units; fetch the rest a few units at a time
  for (let tries = 0; tries < 6 && Array.isArray(rec.omitted_units) && rec.omitted_units.length; tries++) {
    const want = rec.omitted_units.slice(0, 4);
    const more = await bridge.call("curves.get", { id, units: want });
    if (!more.ok) break;
    rec = { ...rec, units: { ...rec.units, ...more.result.units }, omitted_units: rec.omitted_units.filter((u) => !want.includes(u) || !more.result.units[u]) };
  }
  return { rec };
}

export const fmtSeconds = (ms) => (ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(1) + " s");

// ---- the controller ----------------------------------------------------------------------------------------------------------------------
export function createController({ store, bridge, toast }) {
  ensureTune(store);
  const state = () => store.get().tune;
  const say = (kind, text) => (toast ? toast(kind, text) : pushToast(store, { kind, text }));

  function adopt(result, extra) {
    const model = unitsLib.modelFromRead(result);
    if (!model) return null;
    const kept = state().selection.filter((id) => model.units[id]); // a re-read keeps the selection (the inspector does not jump)
    const first = model.order.find((id) => model.units[id].kind === "P") || model.order[0];
    const selection = kept.length ? kept : first ? [first] : [];
    patchTune(store, { model, draft: draftLib.emptyDraft(), selection, anchor: selection.includes(state().anchor) ? state().anchor : selection[0] || null, ...extra });
    void tc.loadBaseline();
    return model;
  }

  const tc = {
    store,
    bridge,
    state,
    model: () => state().model,
    draft: () => state().draft,
    baseFn: () => (state().base.state === "ready" ? state().base.fn : null),
    baseReason: () => state().base.reason,
    edit(fn) {
      const s = state();
      if (!s.model) return;
      const next = fn(s.model, s.draft);
      if (next !== s.draft) patchTune(store, { draft: next });
    },
    setSelection: (ids, anchor) => patchTune(store, { selection: ids.slice(), anchor: anchor ?? null }),
    setTileMode: (mode) => patchTune(store, { tileMode: mode === "live" ? "live" : "ratio" }),
    setSubtab: (id) => SUBTABS.some((t) => t.id === id) && patchTune(store, { subtab: id }),

    /** The read of this boot, if the helper has one (tune.last: read-only, no mailbox traffic). */
    async loadLast() {
      const r = await bridge.call("tune.last");
      if (!r.ok || !r.result || !r.result.available || !r.result.result || state().model) return false;
      const at = new Date(r.result.at * 1000).toISOString();
      const model = adopt(r.result.result, { read: { ...state().read, state: "ready", fromLast: true, at, durationMs: null, ops: r.result.result.ops_done ?? null, error: null } });
      return !!model;
    },

    /** tune.read with live progress; the measured duration is shown next to the button. Never automatic. */
    async readCpu() {
      if (state().read.state === "reading") return;
      const startedAt = performance.now();
      patchTune(store, { read: { ...state().read, state: "reading", step: 0, of: 0, startedAt, error: null, fromLast: false } });
      const off = bridge.on("progress", (p) => {
        if (state().read.state !== "reading" || !p) return;
        patchTune(store, { read: { ...state().read, step: p.step ?? state().read.step, of: p.of ?? state().read.of } });
      });
      let res;
      try {
        const first = await bridge.call("tune.read");
        res = first.ok && first.pending ? await bridge.wait(first.id) : first;
      } finally {
        off();
      }
      const durationMs = performance.now() - startedAt;
      if (!res.ok) {
        patchTune(store, { read: { ...state().read, state: "error", durationMs, error: res.error.message || res.error.code } });
        say("error", "Read CPU state failed: " + (res.error.message || res.error.code));
        return;
      }
      const hadDraft = !draftLib.isEmpty(state().draft);
      const at = res.result.read_at || new Date().toISOString();
      const model = adopt(res.result, { read: { state: "ready", step: state().read.step, of: state().read.of, startedAt: null, durationMs, ops: res.result.ops_done ?? null, at, error: null, fromLast: false } });
      if (!model) {
        patchTune(store, { read: { ...state().read, state: "error", durationMs, error: "the read held no units" } });
        say("error", "Read CPU state: the helper's answer held no units.");
        return;
      }
      setLastRead(store, at, res.result.accepted_match ?? null, res.result.accepted_note || "", res.result.uvp);
      say("ok", "Read CPU state in " + fmtSeconds(durationMs) + (hadDraft ? "; the pending edits were dropped (they were made against the previous read)." : "."));
    },

    async loadBaseline() {
      patchTune(store, { base: { state: "loading", fn: null, name: "", id: null, reason: "the baseline curve is loading" } });
      const list = await bridge.call("curves.list");
      if (!list.ok) return patchTune(store, { base: { state: "none", fn: null, name: "", id: null, reason: "stored curves unavailable: " + list.error.message } });
      const row = (list.result.curves || []).find((c) => c && c.baseline === true);
      if (!row) return patchTune(store, { base: { state: "none", fn: null, name: "", id: null, reason: NO_BASELINE_REASON } });
      const got = await fetchCurve(bridge, row.id);
      if (!got.rec) return patchTune(store, { base: { state: "none", fn: null, name: "", id: row.id, reason: "the baseline curve could not be loaded: " + got.error } });
      patchTune(store, { base: { state: "ready", fn: baselineFromRecord(got.rec), name: got.rec.name || row.id, id: row.id, reason: "" } });
    },
  };
  return tc;
}

/** The context handed to a sub-tab module (see the contract in the header). */
export function subTabContext(tc, ctx) {
  const { store } = tc;
  return {
    store,
    bridge: tc.bridge,
    toast: ctx.toast,
    draft: {
      get: () => tc.state().draft,
      set: (next) => patchTune(store, { draft: next }),
      edit: (fn) => tc.edit(fn),
      discard: () => discardDraft(store),
      lib: draftLib,
    },
    getDraft: () => tc.state().draft, // the turbo tab (G-209) reads/writes the shared draft through these accessors
    setDraft: (next) => patchTune(store, { draft: next }),
    units: unitsLib,
    limits: limitsLib,
    model: tc.state().model, // the units model at mount time (a new read remounts the sub-tab); getModel() for the live value
    getModel: () => tc.state().model,
    tune: tc,
  };
}

// ---- the view -----------------------------------------------------------------------------------------------------------------------------
let stylePromise = null;
/** The stylesheet of the Tune screen is loaded by the screen itself (no change to index.html); the content stays hidden until it is in. */
function ensureStyle() {
  if (stylePromise) return stylePromise;
  stylePromise = new Promise((resolve) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = new URL("../styles/tune.css", import.meta.url).href;
    link.addEventListener("load", () => resolve());
    link.addEventListener("error", () => resolve());
    document.head.append(link);
  });
  return stylePromise;
}

function coresTab(tc) {
  let grid = null;
  let inspector = null;
  return {
    mount(el) {
      grid = createGrid(tc);
      inspector = createInspector(tc);
      el.append(h("div", { class: "cores-layout" }, grid.el, inspector.el));
      grid.update();
      inspector.update();
    },
    unmount() {
      if (grid) grid.dispose();
      if (inspector) inspector.dispose();
      grid = inspector = null;
    },
  };
}

export function tuneView() {
  const disposers = [];
  let sub = null; // {id, model, unmount()}
  let ticker = null;
  return {
    id: "tune",
    mount(root, ctx) {
      const tc = createController(ctx);
      const { store } = tc;
      const wrap = h("div", { class: "tune", hidden: true });
      const tabs = h("div", { class: "subtabs", role: "tablist", "aria-label": "Tune sections" });
      const readBtn = h("button", { type: "button", class: "btn primary", id: "tune-read", on: { click: () => tc.readCpu() } });
      const readText = h("span", { class: "read-text", id: "tune-read-status", role: "status" });
      const progress = h("progress", { class: "read-progress", max: 1, value: 0, hidden: true, "aria-label": "Read progress" });
      const bar = h("div", { class: "read-bar" }, readBtn, progress, readText);
      const panel = h("div", { class: "subpanel", role: "tabpanel", id: "tune-panel" });
      wrap.append(h("header", { class: "screen-head" }, h("h1", { text: "Tune" }), tabs, h("span", { class: "spacer" })), bar, panel);
      root.append(wrap);
      ensureStyle().then(() => (wrap.hidden = false));

      const tabEls = new Map();
      for (const t of SUBTABS) {
        const b = h("button", { type: "button", role: "tab", class: "subtab", id: "subtab-" + t.id, "aria-controls": "tune-panel", dataset: { tab: t.id }, text: t.label, on: { click: () => tc.setSubtab(t.id) } });
        b.addEventListener("keydown", (e) => {
          const i = SUBTABS.findIndex((x) => x.id === t.id);
          const to = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? SUBTABS.length - 1 : null;
          if (to === null) return;
          e.preventDefault();
          const next = SUBTABS[(to + SUBTABS.length) % SUBTABS.length];
          tc.setSubtab(next.id);
          tabEls.get(next.id).focus();
        });
        tabEls.set(t.id, b);
        tabs.append(b);
      }

      function renderReadBar() {
        const s = store.get();
        const r = s.tune.read;
        const connected = s.link.state === "connected";
        const reading = r.state === "reading";
        const last = r.durationMs;
        readBtn.textContent = (s.tune.model ? "Re-read CPU state" : "Read CPU state") + (last !== null && Number.isFinite(last) && !reading ? " (~" + fmtSeconds(last) + ")" : "");
        readBtn.disabled = reading || !connected;
        readBtn.title = !connected ? "The helper is not connected" : "tune.read: reads every V/F word through the OC mailbox (read-only requests, each one a mailbox transaction). Never runs by itself.";
        progress.hidden = !reading;
        if (reading) {
          progress.max = Math.max(1, r.of || 1);
          progress.value = r.step || 0;
        }
        let text;
        if (reading) text = "Reading… " + (r.of ? "step " + r.step + " of " + r.of + " · " : "") + fmtSeconds(performance.now() - r.startedAt);
        else if (r.state === "error") text = "Read failed: " + r.error;
        else if (r.state === "ready" && r.fromLast) text = "Showing the read of this boot (" + fmtClock(r.at) + "). Re-read to refresh.";
        else if (r.state === "ready") text = "Read at " + fmtClock(r.at) + (r.ops ? " · " + plural(r.ops, "op") : "") + " in " + fmtSeconds(r.durationMs ?? 0);
        else text = "No CPU state read yet this boot. A read is mailbox traffic, so it only runs when you ask.";
        readText.textContent = text;
        readText.dataset.state = r.state;
        if (reading && !ticker) ticker = setInterval(renderReadBar, 200);
        if (!reading && ticker) {
          clearInterval(ticker);
          ticker = null;
        }
      }

      function unmountSub() {
        if (sub) sub.unmount();
        sub = null;
        clear(panel);
      }

      function renderSub() {
        const t = store.get().tune;
        for (const [id, b] of tabEls) {
          b.setAttribute("aria-selected", String(id === t.subtab));
          b.tabIndex = id === t.subtab ? 0 : -1;
        }
        panel.setAttribute("aria-labelledby", "subtab-" + t.subtab);
        if (sub && sub.id === t.subtab && sub.model === t.model) return;
        unmountSub();
        const def = SUBTABS.find((x) => x.id === t.subtab);
        panel.dataset.tab = t.subtab;
        const spec = SUBTAB_MODULES[t.subtab];
        if (!t.model && (!spec || spec.needsRead)) {
          panel.append(h("div", { class: "card empty-read" }, h("p", { text: "Nothing to edit yet." }), h("p", { class: "muted", text: 'Use "Read CPU state" above: the app reads the V/F points, the turbo tables and the limits once per boot. Nothing is written.' })));
          sub = { id: t.subtab, model: t.model, unmount() {} };
          return;
        }
        if (t.subtab === "cores") {
          const tab = coresTab(tc);
          tab.mount(panel);
          sub = { id: "cores", model: t.model, unmount: () => tab.unmount() };
          return;
        }
        const mine = { id: t.subtab, model: t.model, unmount() {} };
        sub = mine;
        const note = h("p", { class: "muted", text: "Loading…" });
        panel.append(note);
        import(spec.path).then(
          (mod) => {
            if (sub !== mine) return;
            clear(panel);
            mod.mount(panel, subTabContext(tc, ctx));
            mine.unmount = () => mod.unmount();
          },
          () => {
            if (sub !== mine) return;
            note.textContent = def.label + " could not be loaded.";
          },
        );
      }

      disposers.push(store.watch((s) => s.tune.read, renderReadBar));
      disposers.push(
        store.watch(
          (s) => s.link.state,
          (state) => {
            renderReadBar();
            if (state === "connected" && !tc.model() && tc.state().read.state === "idle") void tc.loadLast(); // the read of this boot, if there is one (read-only, no mailbox traffic)
          },
        ),
      );
      disposers.push(store.watch((s) => s.tune.subtab, renderSub));
      disposers.push(store.watch((s) => s.tune.model, renderSub));
    },
    unmount() {
      for (const d of disposers.splice(0)) d();
      if (ticker) clearInterval(ticker);
      ticker = null;
      if (sub) sub.unmount();
      sub = null;
    },
  };
}
