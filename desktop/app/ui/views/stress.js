// views/stress.js: Stress screen (PLAN §4.4, D-019): a quick screen for an unstable setting, never an acceptance gate. Ops: stress.start / stress.stop / stress.list.
// Port of web/app.js Stress (ST/buildStressControls/renderStress/startStress/stopStress/stressProgress/onStressResult/stSummary/renderStressResult/
// loadStressList/renderStressHistory :4490-4811, markup web/index.html:334-400). Differences from the page: the units come from preflight.topology (D-036),
// the run state lives in a controller keyed by the bridge (so a result that arrives while another screen is open is kept), and the controller and every
// rule (args, gate, wording) are pure/DOM-free and unit-tested. The D-019 banner is always rendered, the result never says "pass", only "no errors detected in N min".
// Also home of what views/curves.js shares with it (this task owns no other file; hoist candidate: components/units.js): unitModel / favMark / cpuSub / unitTitle / quickSets,
// unitPicker, UNIT_CSS (palette, tiles, .pill), opGate, errText, fmtDate.
import { h, svg, clear } from "../components/dom.js";
import { fmtNum, NA } from "../components/fmt.js";
import { createSpark, installStyles, installScreenStyles, segmented } from "../components/spark.js";
import { coreReading, readingValue } from "../components/coremap.js";
import { isNum, TIMES } from "../lib/format.js";
import { unitKind, unitLabel } from "../lib/units.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isStr = (x) => typeof x === "string";

export const BANNER_HEAD = "Quick screen — NOT an acceptance gate.";
export const BANNER_TEXT =
  " It can find an unstable setting fast; it cannot show that a setting is stable. Before you trust a setting, run a long self-checking load (a Linpack-class test for an hour or more).";

export const DURATIONS = [
  { value: 60, label: "1 min" },
  { value: 300, label: "5 min" },
  { value: 900, label: "15 min" },
  { value: 3600, label: "60 min" },
  { value: "custom", label: "Custom" },
];
export const METHODS = [
  { value: "integer", label: "Integer", title: "Integer arithmetic on every selected core (stress-ng, int64)" },
  { value: "vector-math", label: "Vector math", title: "Matrix products: heavier on the vector units (stress-ng, matrixprod)" },
  { value: "cache", label: "Cache", title: "Cache thrashing on every selected core (stress-ng, cache)" },
];
export const METHOD_WORDS = { integer: "integer", "vector-math": "vector math", cache: "cache" };
export const MEM_MB = [1024, 2048, 4096, 8192, 16384, 32768];
export const DUR_MIN = 30;
export const DUR_MAX = 3600;
export const CAP_MIN = 8;
export const CAP_MAX = 60;

// ---------------------------------------------------------------- units (shared with Curves)

/** Shared by Stress and Curves: the 12-unit palette of web/style.css (warm P-cores, cool E-clusters; `--u` carries the colour, every mark reads it), the unit tiles and the `.pill` chip. */
export const UNIT_CSS = `
.u-P0 { --u: #ffb547; } .u-P1 { --u: #ff8a4c; } .u-P2 { --u: #ff6b6b; } .u-P3 { --u: #ffe27a; }
.u-P4 { --u: #ff8fc0; } .u-P5 { --u: #d97be8; } .u-P6 { --u: #d2a86b; } .u-P7 { --u: #f4cdb0; }
.u-EC0 { --u: #5ad8e6; } .u-EC1 { --u: #3ee0a8; } .u-EC2 { --u: #6fa8ff; } .u-EC3 { --u: #a9b6ff; }
.unit-tile { position: relative; display: inline-flex; flex-direction: column; gap: 1px; min-width: 74px; padding: 5px 10px 5px 14px; text-align: left;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); color: var(--text); font: inherit; cursor: pointer; }
.unit-tile::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--u, var(--pending)); border-radius: var(--radius-sm) 0 0 var(--radius-sm); }
.unit-tile[aria-pressed="true"] { border-color: color-mix(in srgb, var(--u, var(--pending)) 55%, var(--border)); background: color-mix(in srgb, var(--u, var(--pending)) 10%, var(--surface-2)); }
.unit-tile[aria-pressed="false"] { background: transparent; color: var(--text-dim); border-style: dashed; }
.unit-tile[aria-pressed="false"]::before { opacity: 0.25; }
.unit-tile:disabled { cursor: default; }
.unit-tile.static { cursor: default; }
.unit-tile .ut-name { font-weight: 600; }
.unit-tile .ut-sub { font-size: var(--fs-xs); color: var(--text-dim); }
.unit-tile .ut-mhz { font-size: var(--fs-xs); font-variant-numeric: tabular-nums; min-height: 1.2em; }
.pill { display: inline-block; padding: 0 9px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-dim); font-size: var(--fs-xs); white-space: nowrap; }
.pill[data-tone="ok"] { color: var(--ok); border-color: rgba(79, 197, 140, 0.45); background: var(--ok-bg); }
.pill[data-tone="warn"] { color: var(--warn); border-color: rgba(242, 178, 76, 0.5); background: var(--warn-bg); }
.pill[data-tone="bad"] { color: var(--danger); border-color: rgba(239, 107, 95, 0.5); background: var(--danger-bg); }
.pill[data-tone="pending"] { color: var(--pending); border-color: rgba(111, 168, 255, 0.5); background: var(--pending-bg); }
.unit-picker { display: flex; flex-direction: column; gap: var(--space-2); }
.unit-rows { display: flex; flex-direction: column; gap: 6px; }
.unit-row { display: flex; flex-wrap: wrap; gap: 6px; }
.unit-quick { display: flex; gap: var(--space-3); font-size: var(--fs-sm); }
.unit-quick button { background: none; border: 0; padding: 0; color: var(--pending); font: inherit; cursor: pointer; text-decoration: underline; }
.unit-quick button:disabled { color: var(--text-faint); cursor: default; text-decoration: none; }
`;

/** preflight.topology -> {ids (topology order), byId, favoredSource}; empty when the topology is not known (nothing is invented, D-036). */
export function unitModel(preflight) {
  const list = preflight && isObj(preflight.topology) && Array.isArray(preflight.topology.units) ? preflight.topology.units : [];
  const byId = {};
  const ids = [];
  for (const u of list) {
    if (!isObj(u) || !isStr(u.id) || byId[u.id]) continue;
    byId[u.id] = { id: u.id, kind: u.kind === "E" ? "E" : "P", cpus: Array.isArray(u.cpus) ? u.cpus.filter(isNum) : [], favored: u.favored === true, maxRatio: isNum(u.max_ratio) ? u.max_ratio : null };
    ids.push(u.id);
  }
  return { ids, byId, favoredSource: preflight && isObj(preflight.topology) && isStr(preflight.topology.favored_source) ? preflight.topology.favored_source : "" };
}
export const idsOfKind = (m, kind) => m.ids.filter((u) => unitKind(u) === kind);
export const favoredIds = (m) => m.ids.filter((u) => m.byId[u].favored);
export const favMark = (m, u) => (m.byId[u] && m.byId[u].favored ? " ★" : "");
export function cpuSub(m, u) {
  const c = m.byId[u] ? m.byId[u].cpus : [];
  return !c.length ? "" : c.length === 1 ? `CPU ${c[0]}` : `CPUs ${c[0]}–${c[c.length - 1]}`;
}
export function unitTitle(m, u) {
  const info = m.byId[u];
  return `${unitLabel(u)}${cpuSub(m, u) ? " · " + cpuSub(m, u) : ""}${info && info.favored ? ` · favored core${m.favoredSource ? " (" + m.favoredSource + ")" : ""}` : ""}${info && info.maxRatio ? ` · max ${info.maxRatio}${TIMES}` : ""}`;
}
/** The quick sets next to a unit picker: [label, ids]. */
export function quickSets(m) {
  return [["All", m.ids], ["P-cores", idsOfKind(m, "P")], ["E-clusters", idsOfKind(m, "E")], ["None", []]];
}

/**
 * A group of unit toggle tiles with the quick sets. -> {el, set(Set), setDisabled(bool), setMhz({unit: text}), tiles}; `onChange(Set)` fires on a user click only.
 * `mhz` adds a live clock line to each tile (Stress); without it the tile is a chip (Curves).
 */
export function unitPicker(m, initial, onChange, { mhz = false, label = "Units" } = {}) {
  let sel = new Set(initial);
  let disabled = false;
  const tiles = new Map();
  const mk = (u) => {
    const kids = [h("span", { class: "ut-name", text: u + favMark(m, u) }), h("span", { class: "ut-sub", text: cpuSub(m, u) })];
    if (mhz) kids.push(h("span", { class: "ut-mhz" }));
    const b = h("button", { type: "button", class: ["unit-tile", `u-${u}`], dataset: { unit: u }, title: unitTitle(m, u), "aria-pressed": "true" }, ...kids);
    b.addEventListener("click", () => {
      const next = new Set(sel);
      if (next.has(u)) next.delete(u);
      else next.add(u);
      sel = next;
      paint();
      onChange(new Set(sel));
    });
    tiles.set(u, b);
    return b;
  };
  const rows = h("div", { class: "unit-rows" }, h("div", { class: "unit-row" }, ...idsOfKind(m, "P").map(mk)), h("div", { class: "unit-row" }, ...idsOfKind(m, "E").map(mk)));
  const quick = h("div", { class: "unit-quick" });
  for (const [name, ids] of quickSets(m)) quick.append(h("button", { type: "button", text: name, on: { click: () => { sel = new Set(ids); paint(); onChange(new Set(sel)); } } }));
  const el = h("div", { class: "unit-picker", role: "group", "aria-label": label }, rows, quick);
  function paint() {
    for (const [u, b] of tiles) {
      b.setAttribute("aria-pressed", String(sel.has(u)));
      b.disabled = disabled;
    }
    for (const b of quick.children) b.disabled = disabled;
  }
  paint();
  return {
    el,
    tiles,
    set(s) {
      sel = new Set(s);
      paint();
    },
    setDisabled(d) {
      disabled = d;
      paint();
    },
    setMhz(map) {
      if (mhz) for (const [u, b] of tiles) b.querySelector(".ut-mhz").textContent = map && map[u] ? map[u] : "";
    },
  };
}

// ---------------------------------------------------------------- pure rules (unit-tested)

export function fmtLen(s) {
  if (!isNum(s)) return NA;
  const t = Math.round(s);
  if (t >= 60 && t % 60 === 0) return `${t / 60} min`;
  if (t < 60) return `${t} s`;
  return mmss(t);
}
export function mmss(s) {
  const t = Math.max(0, Math.round(isNum(s) ? s : 0));
  const m = Math.floor(t / 60);
  const r = t % 60;
  return `${m}:${r < 10 ? "0" : ""}${r}`;
}
/** "Jan 5 14:03" in the local zone; "n/a" for anything that is not a date (web fmtDate). */
export function fmtDate(x) {
  let d = null;
  if (typeof x === "string") d = new Date(x);
  else if (isNum(x)) d = new Date(x < 1e12 ? x * 1000 : x);
  if (!d || Number.isNaN(d.getTime())) return NA;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}
const clampInt = (x, lo, hi) => Math.min(hi, Math.max(lo, Math.round(Number(x) || lo)));

export function initialStress() {
  return { kind: "cpu", method: "integer", dur: 300, custom: false, customS: 300, cap: null, memMb: null, sel: null };
}
export const stressDuration = (st) => (st.custom ? clampInt(st.customS, DUR_MIN, DUR_MAX) : st.dur);
export const selectedUnits = (st, ids) => (st.sel ? ids.filter((u) => st.sel.has(u)) : ids.slice());

/** The stress.start arguments (ops.rs stress_start): kind, duration_s 30..3600, method, units (omitted when every unit is loaded), ratio_cap 8..60, memory_mb. */
export function stressArgs(st, ids) {
  const args = { kind: st.kind, duration_s: stressDuration(st) };
  if (st.kind === "cpu") {
    args.method = st.method;
    const units = selectedUnits(st, ids);
    if (units.length < ids.length) args.units = units;
  } else if (st.memMb !== null) args.memory_mb = st.memMb;
  if (st.cap !== null) args.ratio_cap = st.cap;
  return args;
}

/** Why an op cannot start now ("" = it can). Mirrors the web's idleGate without the SSE/heartbeat parts (the bridge is the link). */
export function opGate({ link, preflight, helperOp }, busy = false) {
  if (!link || link.state !== "connected") return `The helper is not connected (${String((link && link.state) || "unknown").replace(/_/g, " ")}).`;
  if (preflight && preflight.mode === "refuse") return "Preflight refused this system. See System.";
  if (busy) return "A helper operation is running.";
  if (helperOp && helperOp.state && helperOp.state !== "idle") return `The helper is ${helperOp.state}.`;
  return "";
}

export function estimateText(st, ids, { running, stopping, gate }) {
  const none = st.kind === "cpu" && selectedUnits(st, ids).length === 0;
  if (running) return stopping ? "Stopping. The tool is ended and the cpufreq limits are put back." : "Running. Stop ends it early; the result is kept either way.";
  if (gate) return gate;
  if (!ids.length && st.kind === "cpu") return "The CPU topology is not known yet (preflight).";
  if (none) return "Select at least one unit.";
  const n = selectedUnits(st, ids).length;
  const what =
    st.kind === "cpu"
      ? `${METHOD_WORDS[st.method]} load on ${n === ids.length ? `all ${ids.length} units` : `${n} ${n === 1 ? "unit" : "units"}`}`
      : `memory test of ${st.memMb === null ? "3/4 of free memory" : st.memMb / 1024 + " GB"} on all CPUs`;
  return `Ready: ${what} for ${fmtLen(stressDuration(st))}${st.cap === null ? "" : `, capped at ${st.cap}${TIMES}`}. It raises power and heat for that time.`;
}

/** Words of a result. "No errors detected in N min" at most: nothing here says a setting is proven stable. */
export function stSummary(r) {
  const n = isNum(r.errors) ? r.errors : 0;
  const ran = fmtLen(Math.round(isNum(r.elapsed_s) ? r.elapsed_s : 0));
  if (r.stopped_by === "cancel") return `Stopped after ${ran} of ${fmtLen(r.duration_s)}. ${n === 0 ? "No errors detected in that time." : `${n} ${n === 1 ? "error" : "errors"} detected.`}`;
  if (n > 0) return `${n} ${n === 1 ? "error" : "errors"} detected in ${ran}. Treat this setting as unstable.`;
  if (r.stopped_by === "error") return `The screen ended with a tool error after ${ran}. See the lines below.`;
  if (r.health === "delta") return `No errors detected in ${ran}, but the machine health check changed. See below.`;
  return `No errors detected in ${ran}.`;
}

/** {bad, pills:[{text, tone: ok|warn|bad|neutral, title?}]} of a stress.start result. */
export function resultFlags(r) {
  const n = isNum(r.errors) ? r.errors : 0;
  const mce = isNum(r.mce_new) ? r.mce_new : 0;
  const bad = n > 0 || r.stopped_by === "error" || r.health === "delta" || mce > 0;
  const pills = [
    { text: r.stopped_by === "time" ? "ran to the end" : r.stopped_by === "cancel" ? "stopped by you" : "tool error", tone: r.stopped_by === "error" ? "bad" : "neutral" },
    { text: r.health === "clean" ? "health clean" : "health changed", tone: r.health === "clean" ? "ok" : "bad" },
  ];
  if (mce > 0) pills.push({ text: `${mce} new machine-check ${mce === 1 ? "event" : "events"}`, tone: "bad" });
  const ach = isObj(r.achieved) ? Object.values(r.achieved).filter(isObj) : [];
  if (r.cap_valid === true && ach.some((a) => a.cap_held === false)) {
    pills.push({ text: "cap held within one ratio", tone: "warn", title: "Every loaded unit ran within one ratio of the cap, but not all exactly at it. See the units below." });
  } else if (r.cap_valid === true) pills.push({ text: "cap held", tone: "ok", title: "Every loaded unit ran within one ratio of the cap." });
  else if (r.cap_valid === false) pills.push({ text: "cap not held", tone: "bad", title: "A loaded unit ran more than one ratio away from the cap, so the screen did not test the capped ratio." });
  return { bad, pills };
}

/** What a unit's achieved-clock tile says (web renderStressResult). */
export function achievedText(a) {
  const sub = !isNum(a.ratio_mode)
    ? "ratio n/a"
    : a.cap_held === false && isNum(a.cap_ratio)
      ? `ran ${a.ratio_mode}${TIMES} (cap ${a.cap_ratio}${TIMES})`
      : a.cap_held === true
        ? `cap held at ${a.cap_ratio}${TIMES}`
        : `${a.ratio_mode}${TIMES} mostly`;
  return { sub, mhz: isNum(a.mhz_mean) ? `${a.mhz_mean.toFixed(0)} MHz` : NA };
}

export function screenText(r) {
  if (r.kind === "memory") {
    const mb = isNum(r.memory_mb) ? ` ${r.memory_mb >= 1024 ? (r.memory_mb / 1024).toFixed(r.memory_mb % 1024 ? 1 : 0) + " GB" : r.memory_mb + " MB"}` : "";
    return `Memory${mb}${isNum(r.ratio_cap) ? `, cap ${r.ratio_cap}${TIMES}` : ""}`;
  }
  return `CPU ${METHOD_WORDS[r.method] || "load"}${isNum(r.ratio_cap) ? `, cap ${r.ratio_cap}${TIMES}` : ""}`;
}
export function unitsText(r, ids) {
  if (r.kind === "memory") return "all CPUs";
  const u = Array.isArray(r.units) ? r.units.filter(isStr) : [];
  return !u.length || (ids.length && u.length >= ids.length) ? (ids.length ? `all ${ids.length}` : "all") : u.join(" ");
}

/** A stress.list row as display cells. */
export function historyRow(r, ids = []) {
  const n = isNum(r.errors) ? r.errors : 0;
  const mce = isNum(r.mce_new) ? r.mce_new : 0;
  return {
    id: r.id,
    started: fmtDate(r.started),
    screen: screenText(r),
    units: unitsText(r, ids),
    ran: mmss(isNum(r.elapsed_s) ? r.elapsed_s : 0),
    asked: `asked for ${fmtLen(isNum(r.duration_s) ? r.duration_s : 0)}`,
    ended: { text: r.stopped_by === "time" ? "full length" : r.stopped_by === "cancel" ? "stopped" : "tool error", tone: r.stopped_by === "error" ? "bad" : "neutral" },
    errors: n,
    health: { text: r.health === "clean" ? "clean" : "changed", tone: r.health === "clean" ? "ok" : "bad" },
    mce: mce > 0 ? `${mce} MCE` : "",
    temp: isNum(r.max_temp_c) ? `${r.max_temp_c.toFixed(0)} °C` : NA,
    power: isNum(r.max_pkg_w) ? `${r.max_pkg_w.toFixed(0)} W` : NA,
  };
}

/** Mean clock of a unit's CPUs in a telemetry `values` object: {value|null, text}. */
export function unitMhz(values, cpus) {
  const vs = cpus.map((c) => readingValue(coreReading(values, c, "mhz")).value).filter(isNum);
  return vs.length ? { value: vs.reduce((a, b) => a + b, 0) / vs.length, text: `${Math.round(vs.reduce((a, b) => a + b, 0) / vs.length)} MHz` } : { value: null, text: NA };
}

/** Ring fill (0..100) of a run or result. */
export function ringFill(elapsed, of) {
  return isNum(of) && of > 0 && isNum(elapsed) ? Math.min(100, Math.max(0, (elapsed / of) * 100)) : 0;
}

export function errText(err) {
  if (!isObj(err)) return "no answer from the helper";
  const d = isObj(err.detail) && isStr(err.detail.state) ? ` (${err.detail.state})` : "";
  return `${err.message || err.code || "failed"}${d}`;
}

// ---------------------------------------------------------------- controller (no DOM; one per bridge, so state survives a screen change)

const controllers = new WeakMap();

/** The run state and the three ops of the screen. `ctx` = {bridge, store}. paint callbacks are added with `subscribe`. */
export function stressController(ctx) {
  const cached = controllers.get(ctx.bridge);
  if (cached) return cached;
  const c = createStressController(ctx);
  controllers.set(ctx.bridge, c);
  return c;
}

export function createStressController({ bridge, store }) {
  const st = initialStress();
  const view = { run: null, result: null, list: [], loaded: false, listProblem: "", status: "", submitting: false, stopping: false, done: new Set() };
  const painters = new Set();
  const paint = () => painters.forEach((fn) => fn());
  const ids = () => unitModel(store.get().preflight).ids;

  function watchRun(id) {
    bridge.wait(id).then((fin) => onResult(fin));
  }

  // What the helper said each op is doing (`state` / `heartbeat` carry {op_id, state}): a run begun elsewhere is adopted only when it is a stress run, never a read or an apply.
  const opState = new Map();
  function noteState(p) {
    if (!isObj(p) || !isStr(p.op_id) || !isStr(p.state)) return;
    opState.set(p.op_id, p.state);
    if (opState.size > 32) opState.delete(opState.keys().next().value);
  }

  function onProgress(p) {
    if (!isObj(p) || !(isNum(p.elapsed_s) || isNum(p.of_s)) || p.step !== undefined) return; // stress progress only (a measurement carries `step`)
    if (!view.run) {
      if (!isStr(p.op_id) || view.done.has(p.op_id) || opState.get(p.op_id) !== "stressing") return;
      view.run = { id: p.op_id, kind: "cpu", of: 0, elapsed: 0, errors: 0, units: ids(), adopted: true }; // adopted after a reload: the settings are not known
      watchRun(p.op_id);
    }
    if (p.op_id && view.run.id && p.op_id !== view.run.id) return;
    if (isNum(p.elapsed_s)) view.run.elapsed = p.elapsed_s;
    if (isNum(p.of_s)) view.run.of = p.of_s;
    if (isNum(p.errors)) view.run.errors = p.errors;
    paint();
  }

  function onResult(fin) {
    if (fin && isStr(fin.id)) view.done.add(fin.id);
    view.run = null;
    view.stopping = false;
    if (fin && fin.ok === true && isObj(fin.result)) {
      view.result = fin.result;
      view.status = "";
    } else {
      view.result = null;
      view.status = `Failed: ${errText(fin && fin.error)}`;
    }
    paint();
    loadList();
  }

  async function start() {
    const s = store.get();
    const all = ids();
    if (view.run || view.submitting || opGate(s, false) || (st.kind === "cpu" && selectedUnits(st, all).length === 0)) return false;
    const args = stressArgs(st, all);
    view.submitting = true;
    view.status = "Submitting…";
    paint();
    const first = await bridge.call("stress.start", args);
    view.submitting = false;
    if (!first.ok) {
      view.status = `Not started: ${errText(first.error)}`;
      paint();
      return false;
    }
    view.run = { id: first.id, kind: st.kind, of: args.duration_s, elapsed: 0, errors: 0, units: st.kind === "cpu" ? selectedUnits(st, all) : all };
    view.result = null;
    view.status = `Started (operation ${first.id}).`;
    paint();
    if (first.pending) watchRun(first.id);
    else onResult(first);
    return true;
  }

  async function stop() {
    if (!view.run || view.stopping) return false;
    view.stopping = true;
    paint();
    const res = await bridge.call("stress.stop", {});
    if (!res.ok) {
      view.stopping = false;
      view.status = `Stop not accepted: ${errText(res.error)}`;
    } else view.status = "Stop requested. The result arrives in a moment.";
    paint();
    return res.ok;
  }

  async function loadList() {
    const res = await bridge.call("stress.list", {});
    if (res.ok && isObj(res.result) && Array.isArray(res.result.results)) {
      view.list = res.result.results.filter(isObj);
      view.loaded = true;
      view.listProblem = "";
    } else view.listProblem = `Stored screens unavailable: ${errText(res.error)}`;
    paint();
  }

  bridge.on("state", noteState);
  bridge.on("heartbeat", noteState);
  bridge.on("progress", onProgress);
  store.watch((s) => s.link.state, (state) => {
    if (state === "connected" && !view.loaded) loadList();
  });

  return {
    st,
    view,
    ids,
    start,
    stop,
    loadList,
    subscribe(fn) {
      painters.add(fn);
      return () => painters.delete(fn);
    },
    gate: () => opGate(store.get(), view.submitting || !!view.run),
  };
}

// ---------------------------------------------------------------- view

const CSS = `
.st-banner { margin: 0; padding: var(--space-3) var(--space-4); border: 1px solid var(--warn); border-left-width: 4px; border-radius: var(--radius); background: var(--warn-bg); color: var(--text); }
.st-banner strong { color: var(--warn); }
.st-grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(280px, 2fr); gap: var(--space-4); align-items: start; }
@media (max-width: 1100px) { .st-grid { grid-template-columns: minmax(0, 1fr); } }
.st-fields { display: flex; flex-direction: column; gap: var(--space-3); }
.st-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.st-field[hidden] { display: none; }
.st-label { font-size: var(--fs-sm); color: var(--text-dim); }
.st-field select, .st-field input[type="range"] { max-width: 320px; }
.st-field select { padding: 3px 8px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); font: inherit; }
.st-go { display: flex; flex-direction: column; gap: var(--space-2); margin-top: var(--space-3); }
.st-buttons { display: flex; gap: var(--space-2); }
.st-est { margin: 0; color: var(--text-dim); font-size: var(--fs-sm); }
.st-status { margin: var(--space-2) 0 0; color: var(--text-dim); font-size: var(--fs-sm); min-height: 1.2em; }
.st-phase { align-self: flex-start; padding: 1px 10px; border: 1px solid var(--border); border-radius: 999px; font-size: var(--fs-sm); color: var(--text-dim); background: var(--surface-2); }
.st-phase[data-tone="busy"] { color: var(--pending); border-color: var(--pending); background: var(--pending-bg); }
.st-ringbox { display: flex; align-items: center; gap: var(--space-4); }
.st-ring { position: relative; width: 120px; height: 120px; flex: none; }
.st-ring svg { width: 120px; height: 120px; transform: rotate(-90deg); }
.st-ring .ring-bg { fill: none; stroke: var(--border); stroke-width: 10; }
.st-ring .ring-used { fill: none; stroke: var(--pending); stroke-width: 10; stroke-linecap: round; }
.st-ring .ring-used.empty { stroke-opacity: 0; }
.st-ring .ring-used.bad { stroke: var(--danger); }
.st-ring-label { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.st-clock { font-size: var(--fs-xl); font-weight: 600; font-variant-numeric: tabular-nums; }
.st-errors { font-size: var(--fs-sm); color: var(--text-dim); }
.st-errors.bad { color: var(--danger); font-weight: 600; }
.st-ro { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-3); margin-top: var(--space-3); }
.st-ro-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.st-ro-label { font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); }
.st-ro-value { font-size: var(--fs-lg); font-weight: 600; font-variant-numeric: tabular-nums; }
.st-ro-value.na { color: var(--text-faint); font-weight: 500; font-size: var(--fs-md); }
.st-ro-why { font-size: var(--fs-xs); color: var(--text-faint); min-height: 1.2em; }
.st-sum { margin: 0 0 var(--space-3); font-size: var(--fs-lg); }
.st-sum.bad { color: var(--danger); }
.st-pills { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.st-stats { display: flex; flex-wrap: wrap; gap: var(--space-5); margin: var(--space-3) 0; }
.st-stat { display: flex; flex-direction: column; }
.st-stat-v { font-size: var(--fs-lg); font-weight: 600; font-variant-numeric: tabular-nums; }
.st-stat-v.bad { color: var(--danger); }
.st-result h3 { margin: var(--space-3) 0 var(--space-2); font-size: var(--fs-md); color: var(--text-dim); }
.st-lines { margin: 0; padding-left: var(--space-5); color: var(--text-dim); font-size: var(--fs-sm); }
.st-hist-wrap { overflow: auto; }
.tbl .bad-text { color: var(--danger); font-weight: 600; }
`;

function pillEl(p) {
  return h("span", { class: "pill", dataset: { tone: p.tone }, title: p.title || "", text: p.text });
}

export function stressView() {
  const subs = [];
  return {
    id: "stress",
    mount(root, ctx) {
      installScreenStyles();
      installStyles("units", UNIT_CSS);
      installStyles("stress", CSS);
      const { store } = ctx;
      const ctl = stressController(ctx);
      const { st, view } = ctl;
      // The topology comes from preflight, which may arrive after this screen was mounted (a start on #/stress): the picker is rebuilt when it does.
      let model = unitModel(store.get().preflight);
      let ids = model.ids;
      let topoKey = ids.join(",") + "|" + favoredIds(model).join(",");

      // ---- setup card ----
      const kindSeg = segmented([{ value: "cpu", label: "CPU", title: "Load the cores you pick with stress-ng" }, { value: "memory", label: "Memory", title: "Test the memory with stressapptest on all CPUs" }], st.kind, (k) => { st.kind = k; render(); }, { label: "Screen" });
      const methodSeg = segmented(METHODS, st.method, (m) => { st.method = m; render(); }, { label: "CPU load" });
      const durSeg = segmented(DURATIONS, st.custom ? "custom" : st.dur, (v) => { if (v === "custom") st.custom = true; else { st.custom = false; st.dur = v; } render(); }, { label: "Length" });
      const memSel = h("select", { id: "st-mem", "aria-label": "Memory to test" }, h("option", { value: "", text: "Automatic: 3/4 of free memory" }), ...MEM_MB.map((mb) => h("option", { value: String(mb), text: `${mb / 1024} GB` })));
      memSel.addEventListener("change", () => { st.memMb = memSel.value === "" ? null : Number(memSel.value); render(); });
      const durOut = h("output", { id: "st-dur-val", text: fmtLen(st.customS) });
      const durRange = h("input", { type: "range", id: "st-dur-custom", min: DUR_MIN, max: DUR_MAX, step: 30, value: st.customS, "aria-label": "Custom length" });
      durRange.addEventListener("input", () => { st.custom = true; st.customS = Number(durRange.value); render(); });
      const capSel = h("select", { id: "st-cap", "aria-label": "Ratio cap" }, h("option", { value: "", text: "No cap" }));
      for (let r = CAP_MAX; r >= CAP_MIN; r--) capSel.append(h("option", { value: String(r), text: `${r}${TIMES} (${r * 100} MHz)` }));
      capSel.addEventListener("change", () => { st.cap = capSel.value === "" ? null : Number(capSel.value); render(); });
      const buildPicker = () => unitPicker(model, st.sel || new Set(ids), (s) => { st.sel = s; render(); }, { mhz: true, label: "Units to load" });
      let picker = buildPicker();
      const field = (label, ...kids) => { const f = h("div", { class: "st-field" }, h("span", { class: "st-label", text: label }), ...kids); return f; };
      const fKind = field("Screen", kindSeg.el);
      const fMethod = field("CPU load", methodSeg.el);
      const fMem = field("Memory to test", memSel);
      const fDur = field("Length", durSeg.el);
      const fCustom = field("Custom length", h("div", { class: "unit-row" }, durRange, durOut));
      const fCap = field("Ratio cap", capSel, h("span", { class: "st-ro-why", text: "E-cores run one ratio per cluster, so a cap is applied to all four cores of every E-cluster that is loaded." }));
      const fUnits = field("Units to load", picker.el);
      const startBtn = h("button", { type: "button", class: "btn primary", id: "btn-stress-start", text: "Start", on: { click: () => ctl.start() } });
      const stopBtn = h("button", { type: "button", class: "btn", id: "btn-stress-stop", text: "Stop", on: { click: () => ctl.stop() } });
      const est = h("p", { class: "st-est", id: "st-est", role: "status" });
      const status = h("p", { class: "st-status", id: "st-status", role: "status" });
      const setup = h("section", { class: "dcard", id: "st-setup" }, h("div", { class: "dcard-head" }, h("h2", { text: "Setup" })), h("div", { class: "st-fields" }, fKind, fMethod, fMem, fDur, fCustom, fCap, fUnits), h("div", { class: "st-go" }, h("div", { class: "st-buttons" }, startBtn, stopBtn), est));

      // ---- live card ----
      const phase = h("span", { class: "st-phase", id: "st-phase", text: "not running" });
      const ring = svg("circle", { cx: 60, cy: 60, r: 50, class: "ring-used empty", id: "st-ring", pathLength: 100, "stroke-dasharray": "0 100" });
      const clock = h("span", { class: "st-clock", id: "st-clock", text: "0:00" });
      const of = h("span", { class: "muted", id: "st-of" });
      const errors = h("span", { class: "st-errors", id: "st-errors" });
      const ringBox = h("div", { class: "st-ringbox" }, h("div", { class: "st-ring" }, svg("svg", { viewBox: "0 0 120 120", "aria-hidden": "true", focusable: "false" }, svg("circle", { cx: 60, cy: 60, r: 50, class: "ring-bg" }), ring), h("div", { class: "st-ring-label" }, clock, of)), errors);
      const mk = (label, id, key, digits, unit) => {
        const value = h("span", { class: "st-ro-value na", id, text: NA });
        const why = h("span", { class: "st-ro-why", id: `${id}-why` });
        const spark = createSpark({ w: 120, h: 28 });
        return { key, digits, unit, value, why, spark, el: h("div", { class: "st-ro-item" }, h("span", { class: "st-ro-label", text: label }), value, spark.el, why) };
      };
      const tiles = [mk("Hottest sensor", "t-st-temp", "temp", 0, "°C"), mk("Package power", "t-st-pkgw", "pkg_power", 1, "W")];
      const live = h("section", { class: "dcard", id: "st-live" }, h("div", { class: "dcard-head" }, h("h2", { text: "Live" }), phase), ringBox, h("div", { class: "st-ro" }, ...tiles.map((t) => t.el)), status);

      // ---- result + history ----
      const result = h("article", { class: "dcard st-result", id: "st-result", hidden: true, "aria-live": "polite" });
      const histEmpty = h("p", { class: "dcard-note na", id: "st-hist-empty" });
      const histBody = h("tbody", { id: "st-hist-body" });
      const histTable = h("table", { class: "tbl", id: "st-hist", hidden: true }, h("caption", { class: "muted", hidden: true, text: "Earlier stress screens, newest first" }), h("thead", {}, h("tr", {}, ...["Started", "Screen", "Units", "Ran", "Ended", "Errors", "Health", "Hottest", "Peak power"].map((t, i) => h("th", { class: i === 3 || i >= 5 && i !== 6 ? "n" : "", text: t })))), histBody);
      const refresh = h("button", { type: "button", class: "btn", id: "btn-stress-refresh", text: "Refresh", on: { click: () => ctl.loadList() } });
      const history = h("section", { class: "dcard", id: "st-history-card" }, h("div", { class: "dcard-head" }, h("h2", { text: "Earlier screens" }), h("span", { class: "spacer" }), refresh), histEmpty, h("div", { class: "st-hist-wrap" }, histTable));

      root.append(
        h("div", { class: "sc stress" },
          h("header", { class: "screen-head" }, h("h1", { text: "Stress" }), h("span", { class: "sc-meta", text: "quick screens with stress-ng / stressapptest" })),
          h("p", { class: "st-banner", id: "st-banner", role: "note" }, h("strong", { text: BANNER_HEAD }), BANNER_TEXT),
          h("div", { class: "st-grid" }, setup, live),
          result,
          history,
        ),
      );

      function renderResult() {
        const r = view.result;
        result.hidden = !r;
        if (!r) return;
        const flags = resultFlags(r);
        const n = isNum(r.errors) ? r.errors : 0;
        const stat = (k, v, bad) => h("div", { class: "st-stat" }, h("span", { class: "st-ro-label", text: k }), h("span", { class: ["st-stat-v", bad && "bad"], text: v }));
        const nodes = [
          h("div", { class: "dcard-head" }, h("h2", { text: "Result" }), h("div", { class: "st-pills" }, ...flags.pills.map(pillEl))),
          h("p", { class: ["st-sum", flags.bad && "bad"], id: "st-sum", text: stSummary(r) }),
          h("p", { class: "muted", text: "A quick screen, not an acceptance gate." }),
          h("div", { class: "st-stats" }, stat("Errors", String(n), n > 0), stat("Ran", `${mmss(isNum(r.elapsed_s) ? r.elapsed_s : 0)} of ${fmtLen(r.duration_s)}`), stat("Hottest", isNum(r.max_temp_c) ? `${r.max_temp_c.toFixed(0)} °C` : NA), stat("Peak package power", isNum(r.max_pkg_w) ? `${r.max_pkg_w.toFixed(1)} W` : NA)),
        ];
        const ach = isObj(r.achieved) ? r.achieved : {};
        const done = Object.keys(ach).filter((u) => isObj(ach[u])).sort((a, b) => (ids.indexOf(a) < 0 ? 99 : ids.indexOf(a)) - (ids.indexOf(b) < 0 ? 99 : ids.indexOf(b)));
        if (done.length) {
          const tile = (u) => {
            const a = achievedText(ach[u]);
            return h("div", { class: ["unit-tile", "static", `u-${u}`], dataset: { unit: u }, title: `${unitLabel(u)}: mean clock and the ratio most often seen under load` }, h("span", { class: "ut-name", text: u + favMark(model, u) }), h("span", { class: "ut-sub", text: a.sub }), h("span", { class: "ut-mhz", text: a.mhz }));
          };
          nodes.push(h("h3", { text: "Clock each unit held" }), h("div", { class: "unit-rows", role: "group", "aria-label": "Achieved clock per unit" }, h("div", { class: "unit-row" }, ...done.map(tile))));
        } else nodes.push(h("p", { class: "muted", text: "Achieved clocks: n/a (no telemetry sampler was running)." }));
        const lines = Array.isArray(r.error_lines) ? r.error_lines.filter(isStr) : [];
        if (lines.length) nodes.push(h("h3", { text: "Notes from the tool and the helper" }), h("ul", { class: "st-lines" }, ...lines.map((x) => h("li", { text: x }))));
        nodes.push(h("p", { class: "muted", text: isStr(r.id) ? `Stored as ${r.id}.` : "This result could not be stored on the machine." }));
        clear(result).append(...nodes);
      }

      function renderHistory() {
        const rows = view.list.map((r) => historyRow(r, ids));
        histTable.hidden = rows.length === 0;
        histEmpty.hidden = rows.length > 0 && !view.listProblem;
        histEmpty.textContent = view.listProblem || (rows.length ? "" : view.loaded ? "No stress screens stored yet. Start one above; the result is kept on this computer." : "Loading the stored screens…");
        clear(histBody).append(
          ...rows.map((r) => h("tr", { dataset: { id: r.id || "" } },
            h("td", { text: r.started }), h("td", { text: r.screen }), h("td", { text: r.units }),
            h("td", { class: "n", title: r.asked, text: r.ran }),
            h("td", {}, pillEl(r.ended)),
            h("td", { class: r.errors > 0 ? "n bad-text" : "n", text: String(r.errors) }),
            h("td", {}, pillEl(r.health), r.mce ? [" ", pillEl({ text: r.mce, tone: "bad" })] : null),
            h("td", { class: "n", text: r.temp }), h("td", { class: "n", text: r.power }))),
        );
      }

      function renderLive() {
        const s = store.get();
        const run = view.run;
        const running = !!run;
        const of_ = running && isNum(run.of) && run.of > 0 ? run.of : stressDuration(st);
        const elapsed = running ? run.elapsed : view.result ? view.result.elapsed_s : 0;
        const errs = running ? run.errors : view.result ? view.result.errors : 0;
        const f = ringFill(elapsed, running ? of_ : view.result ? view.result.duration_s : of_);
        ring.setAttribute("stroke-dasharray", `${f.toFixed(1)} 100`);
        ring.setAttribute("class", `ring-used${f <= 0 ? " empty" : ""}${isNum(errs) && errs > 0 ? " bad" : ""}`);
        clock.textContent = mmss(elapsed);
        of.textContent = `of ${fmtLen(running ? of_ : view.result ? view.result.duration_s : of_)}`;
        errors.textContent = !running && !view.result ? "no errors counted" : errs === 0 ? (running ? "no errors so far" : "no errors detected") : `${errs} ${errs === 1 ? "error" : "errors"} counted`;
        errors.classList.toggle("bad", isNum(errs) && errs > 0);
        phase.textContent = running ? (view.stopping ? "stopping" : "running") : "not running";
        phase.dataset.tone = running ? "busy" : "";
        const hist = s.telemetry.history || {};
        for (const t of tiles) {
          const smp = s.telemetry.samples ? s.telemetry.samples[t.key] : null;
          const have = !!smp && isNum(smp.value);
          t.value.textContent = have ? `${fmtNum(smp.value, t.digits)} ${t.unit}` : NA;
          t.value.classList.toggle("na", !have);
          t.why.textContent = have ? "" : (smp && smp.reason) || "no telemetry yet";
          t.spark.draw(hist[t.key] || []);
        }
        const mhz = {};
        if (running && s.telemetry.values) for (const u of run.units) if (model.byId[u]) mhz[u] = unitMhz(s.telemetry.values, model.byId[u].cpus).text;
        picker.setMhz(mhz);
      }

      function syncTopology() {
        const next = unitModel(store.get().preflight);
        const key = next.ids.join(",") + "|" + favoredIds(next).join(",");
        if (key === topoKey) return;
        topoKey = key;
        model = next;
        ids = next.ids;
        const fresh = buildPicker();
        picker.el.replaceWith(fresh.el);
        picker = fresh;
      }

      function render() {
        syncTopology();
        const running = !!view.run;
        const cpu = st.kind === "cpu";
        kindSeg.set(st.kind);
        methodSeg.set(st.method);
        durSeg.set(st.custom ? "custom" : st.dur);
        fMethod.hidden = !cpu;
        fUnits.hidden = !cpu;
        fMem.hidden = cpu;
        fCustom.hidden = !st.custom;
        durOut.textContent = fmtLen(stressDuration(st));
        durRange.setAttribute("aria-valuetext", fmtLen(stressDuration(st)));
        for (const el of [memSel, capSel, durRange]) el.disabled = running;
        for (const seg of [kindSeg, methodSeg, durSeg]) for (const b of seg.el.children) b.disabled = running;
        picker.set(running ? new Set(view.run.units) : st.sel || new Set(ids));
        picker.setDisabled(running);
        const gate = ctl.gate();
        const none = cpu && selectedUnits(st, ids).length === 0;
        startBtn.disabled = running || !!gate || none || !ids.length && cpu;
        stopBtn.disabled = !running || view.stopping;
        est.textContent = estimateText(st, ids, { running, stopping: view.stopping, gate });
        status.textContent = view.status;
        renderLive();
        renderResult();
        renderHistory();
      }

      subs.push(ctl.subscribe(render));
      // heartbeats replace store.helperOp several times a second: repaint only when what the screen shows from it (the gate text, the topology) changed
      let lastGate = ctl.gate();
      subs.push(store.subscribe((s, prev) => {
        const gate = ctl.gate();
        if (s.link !== prev.link || s.preflight !== prev.preflight || gate !== lastGate) {
          lastGate = gate;
          render();
        } else if (s.telemetry !== prev.telemetry) renderLive();
      }));
      render();
      if (store.get().link.state === "connected") ctl.loadList();
    },
    unmount() {
      subs.splice(0).forEach((u) => u());
    },
  };
}
