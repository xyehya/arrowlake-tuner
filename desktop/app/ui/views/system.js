// views/system.js: System screen (PLAN §4.4): preflight, tuning limits with their sources, helper/link and the refresh-interval setting, live topology.
// Port of web/app.js: renderPreflight (:375-420, CHECK_ORDER :353), renderHelper (:476-494).
// The limits are never fixed in the page: they are the CPU's own (D-049), from the helper's kept tune read (`tune.last`: `cpu_limits`; no mailbox traffic) plus the Power /
// Ratio caps / Ring & uncore reads when those ran. Without a read there is no table, only the reason; a limit not read yet says "Read CPU state first".
// Not here yet: persisted settings (settings_get/set arrive with the Tauri core, G-204), theme density, notifications.
import { h, clear } from "../components/dom.js";
import { fmtClock, NA } from "../components/fmt.js";
import { setPreflight } from "../store.js";
import { normCpuLimits, capWindow, uncoreWindow, READ_FIRST } from "../lib/cpulimits.js";
import { installScreenStyles, installStyles, segmented } from "../components/spark.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isNum = (x) => typeof x === "number" && Number.isFinite(x);

export const MINUS = "−";
export const INTERVALS = [0.25, 0.5, 1, 2, 5];
export const DEFAULT_INTERVAL_S = 1;

/** Worst first (web CHECK_ORDER). */
export const CHECK_ORDER = { refuse: 0, fail: 0, "read-only": 1, unknown: 2, warn: 3, missing: 4, pass: 5 };

export function sortChecks(checks) {
  const rank = (c) => (c && CHECK_ORDER[c.status] !== undefined ? CHECK_ORDER[c.status] : 6);
  return (Array.isArray(checks) ? checks : []).map((c, i) => [c, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map((x) => x[0]);
}

/** Chip colour for a preflight status: ok / warn / error (the three semantic accents only). */
export function checkState(status) {
  if (status === "pass") return "ok";
  if (status === "fail" || status === "refuse") return "error";
  return "warn";
}

export function fmtSigned(x) {
  return (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x);
}

/**
 * The CPU's own limits (D-049, lib/cpulimits.js): `tune.last`'s `cpu_limits` carries P / E / ring and the offset field; the power, cpufreq and uncore parts come from the
 * Power / Ratio caps / Ring & uncore reads (`rt` = {power, runtime}: their `cpu_limits`), merged in when those were read.
 */
export function mergeCpuLimits(base, rt = {}) {
  const cl = normCpuLimits(base);
  const pw = normCpuLimits(rt.power);
  const run = normCpuLimits(rt.runtime);
  if (!cl && !pw && !run) return null;
  const out = cl || { kinds: {}, offsetMv: null, power: null, capsKhz: {}, uncoreKhz: null };
  return {
    ...out,
    power: pw && pw.power && (pw.power.plMaxW !== null || pw.power.locked !== null) ? pw.power : out.power,
    capsKhz: run && Object.keys(run.capsKhz).length ? run.capsKhz : out.capsKhz,
    uncoreKhz: run && run.uncoreKhz ? run.uncoreKhz : out.uncoreKhz,
  };
}

/** Rows of the limits table. Each: {what, kind, cpu, source}; a limit that was not read says "Read CPU state first" in its cell (starts with NA). */
export function limitRows(cl) {
  if (!cl) return [];
  const rows = [];
  const unread = () => `${NA} · ${READ_FIRST}`;
  const mhz = (khz) => Math.round(khz / 1000);
  for (const [k, kind] of [["P", "P-cores"], ["E", "E-clusters"], ["ring", "Ring"]]) {
    const o = cl.kinds[k];
    if (!o) continue;
    rows.push({ what: "CPU max voltage", kind, cpu: isNum(o.maxMv) ? `${(o.maxMv / 1000).toFixed(3)} V` : unread(), source: "OC mailbox 0x28 [11:0] − 1 LSB" });
    rows.push({ what: "OC ratio limit", kind, cpu: isNum(o.maxRatio) ? `${o.maxRatio}×` : unread(), source: "OC mailbox 0x01 [7:0]" });
  }
  rows.push({ what: "V/F offset field", kind: "P, E, ring", cpu: cl.offsetMv ? `${fmtSigned(cl.offsetMv[0])}…${fmtSigned(cl.offsetMv[1])} mV` : `${NA} · not reported`, source: "11-bit offset field" });
  const p = cl.power;
  rows.push({
    what: "Package power limits",
    kind: "Package",
    cpu: !p || (p.locked === null && p.plMaxW === null) ? `${NA} · ${READ_FIRST} (Tune → Power)` : `${p.locked ? "locked by firmware" : "writable"}, field maximum ${isNum(p.plMaxW) ? p.plMaxW + " W" : NA}`,
    source: "MSR 0x610 (lock bit 63, 15-bit fields) in the units of MSR 0x606",
  });
  const cpus = Object.keys(cl.capsKhz);
  const ws = cpus.map((c) => capWindow(cl, c)).filter(Boolean);
  rows.push({
    what: "Ratio cap range",
    kind: "All CPUs",
    cpu: ws.length ? `${mhz(Math.min(...ws.map((w) => w.minKhz).filter(isNum)))}–${mhz(Math.max(...ws.map((w) => w.maxKhz)))} MHz (${cpus.length} CPUs)` : `${NA} · ${READ_FIRST} (Tune → Ratio caps)`,
    source: "cpufreq cpuinfo_min/max_freq",
  });
  const u = uncoreWindow(cl);
  rows.push({ what: "Uncore ratio range", kind: "Ring", cpu: u ? `${u.min}×–${u.max}×` : `${NA} · ${READ_FIRST} (Tune → Ring & uncore)`, source: "intel_uncore_frequency initial_min/max_freq_khz" });
  return rows;
}

/** The note above the limits table. `last` = the tune.last result ({available, result}) or null; `cl` = mergeCpuLimits(). */
export function limitsNote(last, cl) {
  if (!last || last.available !== true) return "Not read yet this boot: read the ratios and voltages in Tune to see the limits.";
  if (!cl) return "The helper did not report the CPU limits (update the helper), so the Tune controls stay disabled.";
  return "These are the limits of your CPU, read from it. They are the only bounds on a value (besides the field encodings); a limit that was not read is shown as \"Read CPU state first\" and a write that needs it is refused until it is.";
}

/** Preflight summary + banner text (why the app is read-only / refused). */
export function preflightSummary(pre) {
  if (!isObj(pre) || typeof pre.mode !== "string" || !Array.isArray(pre.checks)) return null;
  const why = (status) => pre.checks.filter((c) => c && c.status === status).map((c) => `${c.name}: ${c.reason}`);
  const writes = typeof pre.writes_allowed === "boolean" ? pre.writes_allowed : null;
  let banner = "";
  if (pre.mode === "read-only") banner = "Read-only mode: mailbox requests are off, so Tune cannot read or apply. " + why("read-only").join(" | ");
  else if (pre.mode === "refuse") banner = "Refused: the app cannot operate on this system. " + why("refuse").join(" | ");
  else if (writes === false) banner = "The helper does not permit mailbox requests (a check is unknown or failed): reading and applying are off. See the checks below.";
  return { mode: pre.mode, platform: String(pre.platform || NA), board: String(pre.board || NA), writes, banner, counts: countStatuses(pre.checks) };
}

export function countStatuses(checks) {
  const c = {};
  for (const x of Array.isArray(checks) ? checks : []) if (x && x.status) c[x.status] = (c[x.status] || 0) + 1;
  return c;
}

/** Rows of the live topology table from preflight.topology (D-036: never hard-coded). */
export function topologyRows(topology) {
  if (!isObj(topology) || !Array.isArray(topology.units)) return [];
  return topology.units.filter(isObj).map((u) => ({
    id: String(u.id),
    kind: u.kind === "P" ? "P-core" : u.kind === "E" ? "E-cluster" : String(u.kind ?? NA),
    cpus: Array.isArray(u.cpus) ? (u.cpus.length > 1 ? `${u.cpus[0]}–${u.cpus[u.cpus.length - 1]}` : String(u.cpus[0])) : NA,
    maxRatio: isNum(u.max_ratio) ? `${u.max_ratio}×` : NA,
    perf: isNum(u.perf) ? String(u.perf) : NA,
    favored: u.favored === true,
  }));
}

const PREFS = new WeakMap(); // store -> {intervalS}

const CSS = `
.sys-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: var(--space-4); align-items: start; }
.sys-col { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.checks { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; }
.checks li { display: grid; grid-template-columns: 78px 150px minmax(0, 1fr); gap: var(--space-3); padding: 3px 0; border-bottom: 1px solid var(--border); font-size: var(--fs-sm); align-items: baseline; }
.checks .name { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text); overflow-wrap: anywhere; }
.checks .why { color: var(--text-dim); overflow-wrap: anywhere; }
.checks .chip { justify-self: start; padding: 0 8px; font-size: var(--fs-xs); }
.checks .chip::before { width: 6px; height: 6px; }
.sys-banner { margin: 0 0 var(--space-3); padding: var(--space-2) var(--space-3); border: 1px solid rgba(242, 178, 76, 0.5); background: var(--warn-bg); color: var(--warn); border-radius: var(--radius-sm); font-size: var(--fs-sm); }
.sys-banner[data-kind="error"] { border-color: rgba(239, 107, 95, 0.5); background: var(--danger-bg); color: var(--danger); }
.sys-summary { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); align-items: center; font-size: var(--fs-sm); color: var(--text-dim); }
.sys-summary b { color: var(--text); font-weight: 600; }
.star { color: var(--warn); }
.sys-sub { margin-top: var(--space-4); }
@media (max-width: 1100px) { .sys-grid { grid-template-columns: minmax(0, 1fr); } .checks li { grid-template-columns: 78px minmax(0, 1fr); } .checks .why { grid-column: 2; } }
`;

export function systemView() {
  const subs = [];
  let timer = null;
  let closed = false;
  return {
    id: "system",
    mount(root, ctx) {
      const { store, bridge, toast } = ctx;
      closed = false;
      installScreenStyles();
      installStyles("system", CSS);
      const prefs = PREFS.get(store) || { intervalS: DEFAULT_INTERVAL_S };
      PREFS.set(store, prefs);

      // ---- preflight
      const banner = h("p", { class: "sys-banner", id: "sys-banner", hidden: true });
      const summary = h("div", { class: "sys-summary", id: "sys-summary" });
      const checks = h("ul", { class: "checks", id: "sys-checks" });
      const rerun = h("button", { type: "button", class: "btn", id: "sys-rerun", text: "Re-run", on: { click: () => rerunPreflight() } });
      const preCard = h("section", { class: "dcard", id: "sys-preflight" }, h("div", { class: "dcard-head" }, h("h2", { text: "Preflight" }), h("span", { class: "spacer" }), rerun), banner, summary, checks);

      // ---- limits
      const limNote = h("p", { class: "dcard-note", id: "sys-limits-note" });
      const limBody = h("tbody", { id: "sys-limits-body" });
      const limTable = h("table", { class: "tbl", id: "sys-limits" }, h("thead", {}, h("tr", {}, ...["Limit", "Applies to", "CPU limit", "Source"].map((t) => h("th", { text: t })))), limBody);
      const limReload = h("button", { type: "button", class: "btn", id: "sys-limits-reload", text: "Reload", title: "Ask the helper for its kept tune read again (no mailbox traffic)", on: { click: () => loadLimits() } });
      const limCard = h("section", { class: "dcard", id: "sys-limits-card" }, h("div", { class: "dcard-head" }, h("h2", { text: "Tuning limits" }), h("span", { class: "spacer" }), limReload), limNote, limTable);

      // ---- helper + settings
      const helperKv = h("dl", { class: "kv", id: "sys-helper" });
      const intervalSeg = segmented(INTERVALS.map((s) => ({ value: String(s), label: `${s} s` })), String(prefs.intervalS), (v) => changeInterval(Number(v)), { label: "Refresh interval" });
      const settingNote = h("p", { class: "dcard-note", id: "sys-setting-note", text: "" });
      const helperCard = h(
        "section",
        { class: "dcard", id: "sys-helper-card" },
        h("div", { class: "dcard-head" }, h("h2", { text: "Helper & link" })),
        helperKv,
        h("div", { class: "dcard-head sys-sub" }, h("h2", { text: "Settings" })),
        h("dl", { class: "kv" }, h("dt", { text: "Refresh interval" }), h("dd", {}, intervalSeg.el)),
        settingNote,
      );

      // ---- topology
      const topoBody = h("tbody", { id: "sys-topo-body" });
      const topoNote = h("p", { class: "dcard-note", id: "sys-topo-note" });
      const topoCard = h(
        "section",
        { class: "dcard", id: "sys-topology" },
        h("div", { class: "dcard-head" }, h("h2", { text: "Topology" })),
        topoNote,
        h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ...["Unit", "Kind", "CPUs", "Fused top", "Perf", "Favored"].map((t, i) => h("th", { class: i === 3 || i === 4 ? "n" : "", text: t })))), topoBody),
      );

      root.append(
        h(
          "div",
          { class: "sc system" },
          h("header", { class: "screen-head" }, h("h1", { text: "System" }), h("span", { class: "sc-meta", id: "sys-meta", text: "" })),
          h("div", { class: "sys-grid" }, h("div", { class: "sys-col" }, preCard, limCard), h("div", { class: "sys-col" }, helperCard, topoCard)),
        ),
      );

      // ---- drawing
      const drawPreflight = (pre) => {
        clear(summary);
        clear(checks);
        const sum = preflightSummary(pre);
        if (!sum) {
          banner.hidden = true;
          summary.append(h("span", { class: "na", text: `${NA} · ${pre ? "the helper answered, but not with a full preflight report" : "no preflight yet (helper not connected?)"}` }));
          return;
        }
        banner.hidden = !sum.banner;
        banner.textContent = sum.banner;
        banner.dataset.kind = sum.mode === "refuse" ? "error" : "warn";
        const chip = (text, state) => h("span", { class: "chip", dataset: { state }, text });
        summary.append(
          h("span", {}, "Mode ", chip(sum.mode, sum.mode === "write" ? "ok" : sum.mode === "refuse" ? "error" : "warn")),
          h("span", {}, "Writes ", chip(sum.writes === null ? NA : sum.writes ? "allowed" : "not allowed", sum.writes ? "ok" : "warn")),
          h("span", {}, "Platform ", h("b", { text: sum.platform })),
          h("span", {}, "Board ", h("b", { text: sum.board })),
          h("span", {}, Object.entries(sum.counts).map(([k, n]) => `${n} ${k}`).join(" · ")),
        );
        for (const c of sortChecks(pre.checks)) {
          checks.append(h("li", { dataset: { status: c.status } }, h("span", { class: "chip", dataset: { state: checkState(c.status) }, text: String(c.status) }), h("span", { class: "name", text: String(c.name) }), h("span", { class: "why", text: String(c.reason ?? "") })));
        }
      };
      const drawTopology = (pre) => {
        clear(topoBody);
        const rows = topologyRows(pre && pre.topology);
        const src = pre && pre.topology && pre.topology.favored_source;
        topoNote.textContent = rows.length ? `Read from this CPU by the helper${src ? `; favored cores from ${src}` : ""}.` : `${NA} · no topology from the helper yet`;
        for (const r of rows) {
          topoBody.append(h("tr", {}, h("td", { text: r.id }), h("td", { text: r.kind }), h("td", { text: r.cpus }), h("td", { class: "n", text: r.maxRatio }), h("td", { class: "n", text: r.perf }), h("td", {}, r.favored ? h("span", { class: "star", title: "Favored core", text: "★" }) : "")));
        }
      };
      const drawHelper = (st) => {
        clear(helperKv);
        const l = st.link;
        const op = st.helperOp;
        const add = (k, ...v) => helperKv.append(h("dt", { text: k }), h("dd", {}, ...v));
        add("Link", h("span", { class: "chip", dataset: { state: l.state }, text: l.state.replace("_", " ") }), " ", l.message ? h("span", { class: "meta", text: l.message }) : "");
        if (l.fix) add("Fix", h("code", { class: "fix", title: "Run this in a terminal; the app never runs sudo itself.", text: l.fix }));
        add("Bridge", bridge.name);
        add("Helper state", op.state || NA);
        add("Mailbox ops this boot", isNum(op.opsUsed) ? String(op.opsUsed) : NA);
        add("Per-boot cap", isNum(op.opsBudget) && op.opsBudget > 0 ? `${op.opsBudget} (a limit, not a target)` : NA);
        add("Last telemetry", isNum(st.telemetry.t) ? fmtClock(st.telemetry.t) : "none yet");
        add("Last tune read", st.lastReadAt ? fmtClock(st.lastReadAt) : "none this boot");
      };
      let lastTune = null;
      const drawLimits = (last = lastTune) => {
        lastTune = last;
        clear(limBody);
        const rt = store.get().rt || {};
        const cl = last && last.available === true && isObj(last.result) ? mergeCpuLimits(last.result.cpu_limits, { power: rt.power && rt.power.result ? rt.power.result.cpu_limits : null, runtime: rt.runtime && rt.runtime.result ? rt.runtime.result.cpu_limits : null }) : null;
        limNote.textContent = limitsNote(last, cl);
        limTable.hidden = !cl;
        for (const r of limitRows(cl)) {
          const cell = (t) => h("td", { class: t.startsWith(NA) ? "na" : "", text: t });
          limBody.append(h("tr", {}, h("td", { text: r.what }), h("td", { text: r.kind }), cell(r.cpu), h("td", { class: "muted", text: r.source })));
        }
      };

      // ---- actions
      async function rerunPreflight() {
        rerun.disabled = true;
        const res = await bridge.call("preflight");
        rerun.disabled = false;
        if (closed) return;
        if (res.ok) setPreflight(store, res.result);
        else toast("error", `preflight failed: ${res.error.message}`);
      }
      async function loadLimits() {
        limNote.textContent = "Asking the helper for its kept tune read…";
        const res = await bridge.call("tune.last");
        if (closed) return;
        if (res.ok) drawLimits(res.result);
        else {
          clear(limBody);
          limTable.hidden = true;
          limNote.textContent = `${NA} · limits unavailable: ${res.error.message}`;
        }
      }
      async function changeInterval(s) {
        const res = await bridge.call("telemetry.start", { interval_s: s });
        if (closed) return;
        if (res.ok) {
          prefs.intervalS = s;
          settingNote.textContent = `Telemetry now every ${s} s (until the app restarts: persisted settings arrive with the desktop core).`;
        } else {
          intervalSeg.set(String(prefs.intervalS));
          settingNote.textContent = `The interval was not changed: ${res.error.message}`;
          toast("warn", `refresh interval not changed: ${res.error.message}`);
        }
      }

      subs.push(
        store.watch((s) => s.preflight, (pre) => { drawPreflight(pre); drawTopology(pre); }),
        store.watch((s) => s.link, (_v, st) => drawHelper(st)),
        store.watch((s) => s.helperOp, (_v, st) => drawHelper(st)),
        store.watch((s) => s.lastReadAt, (_v, st) => drawHelper(st)),
        store.watch((s) => s.rt, () => drawLimits()),
      );
      // telemetry.t changes every sample: redraw only the helper card line, cheaply, once a second
      timer = globalThis.setInterval(() => drawHelper(store.get()), 1000);
      drawLimits(null);
      if (store.get().link.state === "connected") loadLimits();
      else subs.push(store.watch((s) => s.link.state, (st) => { if (st === "connected") loadLimits(); }));
    },
    unmount() {
      closed = true;
      subs.splice(0).forEach((u) => u());
      if (timer) globalThis.clearInterval(timer);
      timer = null;
    },
  };
}
