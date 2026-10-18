// components/throttle.js: the XTU-style throttling indicators (G-229; docs/knowledge/throttling.md): chips Power · Current/EDP · Thermal · VR thermal (+ a grey
// Max-turbo chip) that flip Yes/No with the 10 Hz duty, a popover with every active status bit and the word it came from, and the "Throttle events" list.
// Pure models first (unit-tested under node), DOM builders after. Reads only; "Reset indicators" clears this app's own counters, never a register.
import { h, clear } from "./dom.js";
import { fmtClock, fmtNum, NA } from "./fmt.js";
import { installStyles } from "./spark.js";
import { THROTTLE_GROUPS, resetThrottle, setThrottleEvents } from "../store.js";

export const EVENTS_POLL_MS = 15000;
export const EVENTS_N = 50;
const XTU_NOTE = "since boot (sticky); may have been cleared by XTU on Windows";
const KERNEL_NOTE = "since boot (sticky); the Linux kernel clears these itself";

const pct = (duty) => `${Math.round(duty * 100)} %`;

/** One chip: {text, state: 'ok'|'warn'|'error'|'info'|'na', title}. Yes = amber (power, current) or red (thermal); Max turbo is grey whatever it says. */
export function chipModel(g) {
  if (g.state === "na") return { text: `${g.chip}: ${NA}`, state: "na", title: `${g.label}: n/a` };
  if (g.state === "no") return { text: `${g.chip}: No`, state: g.info ? "info" : "ok", title: `${g.label}: not limiting now` };
  const duty = typeof g.duty === "number" ? ` · ${pct(g.duty)}` : "";
  return {
    text: `${g.chip}: Yes${duty}`,
    state: g.info ? "info" : g.severity === "error" ? "error" : "warn",
    title: `${g.label}: limiting${typeof g.duty === "number" ? ` for ${pct(g.duty)} of the last samples` : " now"}${g.bits.length ? " (" + g.bits.map((b) => b.label).join(", ") + ")" : ""}`,
  };
}

/** "1 h 02 min" / "3 min 04 s" / "12.5 s" */
export function fmtSeconds(s) {
  if (!Number.isFinite(s) || s < 0) return NA;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${String(Math.round(s - m * 60)).padStart(2, "0")} s` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/** The popover as data: {state, reason, groups:[{label, text, seconds}], active:[line], others:[line], unknown:[line], sticky:[{head, note, names}], kernel, footer}. */
export function popoverModel(model, session) {
  if (!model || model.state === "na") {
    return { state: "na", reason: (model && model.reason) || "no telemetry yet", source: (model && model.source) || "", groups: [], active: [], others: [], unknown: [], sticky: [], kernel: null, words: model ? model.words : [], footer: "" };
  }
  const groups = model.groups.map((g) => ({ label: g.label, text: chipModel(g).text.replace(`${g.chip}: `, ""), state: chipModel(g).state, seconds: session ? session.seconds[g.key] || 0 : 0 }));
  const active = model.active.map((a) => {
    const g = THROTTLE_GROUPS.find((x) => x.key === a.group);
    return { bit: a.bit, label: a.label, group: g ? g.label : "informational", info: !g || !!g.info, duty: a.duty, msr: a.msr, line: `bit ${a.bit} · ${a.label} · ${g ? g.label : "informational"}${a.duty !== null ? ` · ${pct(a.duty)} of samples` : ""} · ${a.msr}` };
  });
  const others = [];
  for (const w of model.words) {
    if (w.msr === "0x64F" || !w.status.length) continue;
    others.push(`${w.msr} ${w.name}: ${w.status.map((s) => s.label).join(", ")}`);
  }
  const sticky = model.words.filter((w) => w.log.length).map((w) => ({ head: `${w.msr} ${w.name}`, note: w.xtuClears ? XTU_NOTE : KERNEL_NOTE, names: w.log.map((l) => l.label) }));
  const k = model.kernel;
  const kernel = k ? `Kernel thermal events since boot: core ${k.core_count}, package ${k.package_count}${k.core_delta + k.package_delta ? ` (+${k.core_delta + k.package_delta} this tick)` : ""}` : null;
  const footer = model.samples > 1 ? `${model.samples} samples over the last ${model.windowS === null ? "?" : fmtNum(model.windowS, 1)} s (~10 Hz on CPU 0)` : "one point-in-time read (no duty: this helper does not oversample)";
  return { state: "ok", reason: null, source: model.source, groups, active, others, unknown: model.unknown.map((b) => `bit ${b} · not named by the SDM`), sticky, kernel, words: model.words, footer, simulated: !!model.simulated, logFile: model.logFile };
}

/** One throttle.jsonl record -> table cells. */
export function eventModel(r) {
  if (!r || typeof r !== "object" || r.unparsable !== undefined) return { bad: true, text: r && r.unparsable !== undefined ? `unparsable line: ${r.unparsable}` : "unreadable record" };
  const num = (v, u, d = 0) => (typeof v === "number" ? `${fmtNum(v, d)} ${u}` : NA);
  return {
    bad: false,
    key: `${r.t_on}-${r.group}`,
    when: `${r.t_on ? new Date(r.t_on * 1000).toLocaleDateString() : ""} ${fmtClock(r.t_on)}`.trim(),
    tOff: typeof r.t_off === "number" ? r.t_off : null,
    group: r.label || r.group || NA,
    bits: Array.isArray(r.bits) ? r.bits.join(", ") : NA,
    duration: typeof r.dur_ms === "number" ? fmtSeconds(r.dur_ms / 1000) : NA,
    duty: typeof r.duty === "number" ? pct(r.duty) : NA,
    peaks: [num(r.peak_temp_c, "°C"), num(r.peak_pkg_w, "W", 1), num(r.peak_mhz, "MHz")].join(" · "),
    kernel: typeof r.kernel_thermal_delta === "number" && r.kernel_thermal_delta > 0 ? `+${r.kernel_thermal_delta} kernel thermal` : "",
    tuning: r.tuning_id || "none recorded",
  };
}

/** Events newer than the last "Reset indicators" (all of them when `all` or never reset), newest first. */
export function visibleEvents(records, resetAt, all) {
  const list = Array.isArray(records) ? records : [];
  const kept = !all && resetAt ? list.filter((r) => !r || typeof r.t_off !== "number" || r.t_off >= resetAt) : list;
  return [...kept].reverse();
}

const CSS = `
.thr { position: relative; display: inline-flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.thr .chip { cursor: pointer; font-family: inherit; }
.thr .chip[data-state="na"] { color: var(--text-faint); }
.thr .chip[aria-expanded="true"] { border-color: var(--border-strong); }
.thr-pop { position: absolute; z-index: 20; top: calc(100% + 6px); left: 0; width: min(560px, 92vw); max-height: 70vh; overflow: auto; padding: var(--space-3) var(--space-4); background: var(--surface-2); border: 1px solid var(--border-strong); border-radius: var(--radius); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4); font-size: var(--fs-sm); text-align: left; white-space: normal; }
.thr.up .thr-pop { top: auto; bottom: calc(100% + 6px); right: 0; left: auto; }
.thr-pop[hidden] { display: none; }
.thr-pop h3 { font-size: var(--fs-md); margin: 0 0 var(--space-2); display: flex; align-items: center; gap: var(--space-2); }
.thr-pop h4 { margin: var(--space-3) 0 var(--space-1); font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); }
.thr-pop ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.thr-pop .spacer { flex: 1; }
.thr-pop .raw { font-family: var(--font-mono); color: var(--text-dim); }
.thr-pop .note { color: var(--text-faint); }
.thr-pop .g-row { display: grid; grid-template-columns: 120px 110px minmax(0, 1fr); gap: var(--space-2); }
.thr-pop .g-row .yes-warn { color: var(--warn); } .thr-pop .g-row .yes-error { color: var(--danger); } .thr-pop .g-row .no { color: var(--ok); }
.thr-events { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
.thr-events th { text-align: left; font-weight: 500; color: var(--text-dim); padding: 2px var(--space-2); border-bottom: 1px solid var(--border); }
.thr-events td { padding: 3px var(--space-2); border-bottom: 1px solid var(--surface-3); vertical-align: top; }
.thr-events td.mono { font-family: var(--font-mono); }
`;

function line(text, cls) {
  return h("li", { class: cls, text });
}

function renderPopover(box, model, session, actions) {
  const pm = popoverModel(model, session);
  clear(box);
  const close = h("button", { type: "button", class: "btn", text: "Close", on: { click: actions.close } });
  const reset = h("button", { type: "button", class: "btn", text: "Reset indicators", title: "Clears this app's session counters only. No register is written.", on: { click: actions.reset } });
  box.append(h("h3", {}, h("span", { text: "What is limiting the CPU" }), h("span", { class: "spacer" }), reset, close));
  if (pm.state === "na") {
    box.append(h("p", { class: "na", text: `n/a · ${pm.reason}${pm.source ? " (" + pm.source + ")" : ""}` }));
    return;
  }
  if (pm.simulated) box.append(h("p", { class: "note", text: "Simulated 0x64F words (the simulator's throttle script), not hardware." }));
  box.append(h("h4", { text: "Indicators · time limited this session" }));
  const gl = h("ul");
  for (const g of pm.groups) gl.append(h("li", { class: "g-row" }, h("span", { text: g.label }), h("span", { class: g.text.startsWith("Yes") ? `yes-${g.state}` : g.state === "ok" ? "no" : "", text: g.text }), h("span", { class: "note", text: fmtSeconds(g.seconds) })));
  box.append(gl);
  box.append(h("h4", { text: "Active now (status bits)" }));
  const al = h("ul");
  if (!pm.active.length && !pm.unknown.length) al.append(line("No status bit is set.", "note"));
  for (const a of pm.active) al.append(line(a.line, a.info ? "note" : ""));
  for (const u of pm.unknown) al.append(line(u, "note"));
  box.append(al);
  if (pm.others.length) {
    box.append(h("h4", { text: "Other limit words (not in the headline chips)" }));
    const ol = h("ul");
    for (const o of pm.others) ol.append(line(o));
    box.append(ol);
  }
  if (pm.sticky.length) {
    box.append(h("h4", { text: "Log bits" }));
    const sl = h("ul");
    for (const s of pm.sticky) sl.append(h("li", {}, h("strong", { text: s.head }), h("span", { text: `: ${s.names.join(", ")}` }), h("div", { class: "note", text: s.note })));
    box.append(sl);
  }
  if (pm.kernel) box.append(h("p", { class: "note", text: pm.kernel }));
  box.append(h("h4", { text: "Raw words" }));
  const wl = h("ul");
  for (const w of pm.words) wl.append(h("li", { class: "raw", text: `${w.msr} = ${w.rawText}  (${w.name})` }));
  if (!pm.words.length) wl.append(line("none readable", "note"));
  box.append(wl);
  box.append(h("p", { class: "note", text: `${pm.footer}. Source: ${pm.source}.${pm.logFile ? (pm.logFile.error ? " Event log not writable: " + pm.logFile.error : " Event log: " + pm.logFile.path) : ""}` }));
}

/** The chip group. `opts.up`: the popover opens upward (the bottom strip). Returns {el, dispose}. */
export function createThrottleChips(store, opts = {}) {
  installStyles("throttle", CSS);
  const root = h("div", { class: ["thr", opts.up ? "up" : ""], role: "group", "aria-label": "Throttling indicators" });
  const chips = new Map();
  const pop = h("div", { class: "thr-pop", role: "dialog", "aria-label": "Throttling details", hidden: true });
  for (const g of THROTTLE_GROUPS) {
    const c = h("button", { type: "button", class: "chip", dataset: { group: g.key }, "aria-haspopup": "dialog", "aria-expanded": "false", on: { click: () => toggle() } });
    chips.set(g.key, c);
    root.append(c);
  }
  root.append(pop);
  let open = false;
  const draw = () => {
    const st = store.get();
    const model = st.telemetry.throttle;
    for (const g of model.groups) {
      const m = chipModel(g);
      const c = chips.get(g.key);
      c.textContent = m.text;
      c.dataset.state = m.state;
      c.title = m.title;
    }
    if (open) renderPopover(pop, model, st.throttleSession, actions);
  };
  const onDoc = (e) => {
    if (open && !e.composedPath().includes(root)) setOpen(false); // composedPath: a button the popover re-rendered away is still inside
  };
  const onKey = (e) => {
    if (open && e.key === "Escape") setOpen(false);
  };
  const setOpen = (v) => {
    open = v;
    pop.hidden = !v;
    for (const c of chips.values()) c.setAttribute("aria-expanded", String(v));
    if (v) draw();
  };
  const toggle = () => setOpen(!open);
  const actions = { close: () => setOpen(false), reset: () => { resetThrottle(store); draw(); } };
  document.addEventListener("click", onDoc);
  document.addEventListener("keydown", onKey);
  const unsub = store.watch((s) => s.telemetry.throttle, () => draw());
  const unsub2 = store.watch((s) => s.throttleSession, () => open && draw());
  return {
    el: root,
    dispose() {
      unsub();
      unsub2();
      document.removeEventListener("click", onDoc);
      document.removeEventListener("keydown", onKey);
    },
  };
}

/** The "Throttle events" body (table + refresh). `bridge.call("throttle.tail", {n})`; a helper without that op shows n/a and the reason. Returns {el, refresh, dispose}. */
export function createThrottleEvents(store, bridge) {
  installStyles("throttle", CSS);
  const body = h("div", { id: "thr-events-body" });
  const showAll = h("button", { type: "button", class: "btn", id: "thr-events-all", text: "Show all", on: { click: () => { all = !all; draw(); } } });
  const refreshBtn = h("button", { type: "button", class: "btn", id: "thr-events-refresh", text: "Refresh", on: { click: () => refresh() } });
  const el = h("div", {}, h("div", { class: "dcard-head" }, h("h2", { text: "Throttle events" }), h("span", { class: "spacer" }), showAll, refreshBtn), body);
  let all = false;
  let timer = null;
  let stopped = false;
  const draw = () => {
    const st = store.get();
    const ev = st.throttleEvents;
    const resetAt = st.throttleSession.resetAt;
    showAll.hidden = !resetAt;
    showAll.textContent = all ? "Since reset" : "Show all";
    clear(body);
    if (ev.state === "loading" && !ev.records.length) return void body.append(h("p", { class: "muted", text: "Reading…" }));
    if (ev.state === "error") return void body.append(h("p", { class: "na", text: `n/a · ${ev.error}` }));
    const list = visibleEvents(ev.records, resetAt, all);
    if (!list.length) return void body.append(h("p", { class: "muted", text: ev.state === "idle" ? "Not read yet." : resetAt && !all ? "No throttle events since the reset." : "No throttle events logged." }));
    const t = h("table", { class: "thr-events" }, h("thead", {}, h("tr", {}, ...["When", "Limit", "Bits", "Lasted", "Duty", "Peaks", "Tuning"].map((x) => h("th", { text: x })))));
    const tb = h("tbody");
    for (const r of list) {
      const m = eventModel(r);
      tb.append(m.bad ? h("tr", {}, h("td", { colspan: 7, class: "na", text: m.text })) : h("tr", {}, h("td", { text: m.when }), h("td", { text: m.group }), h("td", { text: m.bits }), h("td", { text: m.duration }), h("td", { text: m.duty }), h("td", { text: m.peaks + (m.kernel ? " · " + m.kernel : "") }), h("td", { class: "mono", text: m.tuning })));
    }
    t.append(tb);
    body.append(t);
    if (ev.truncated) body.append(h("p", { class: "note", text: "Older events are in the log file." }));
  };
  async function refresh() {
    setThrottleEvents(store, { state: "loading", error: null });
    let res;
    try {
      res = await bridge.call("throttle.tail", { n: EVENTS_N });
    } catch (e) {
      res = { ok: false, error: { message: e && e.message ? e.message : String(e) } };
    }
    if (stopped) return;
    if (res && res.ok && res.result && Array.isArray(res.result.records)) setThrottleEvents(store, { state: "ok", records: res.result.records, path: res.result.path || null, truncated: !!res.result.truncated, error: null, at: Date.now() / 1000 });
    else {
      setThrottleEvents(store, { state: "error", error: (res && res.error && res.error.message) || "the helper gave no event list", records: [] });
      if (res && res.error && res.error.code === "UNKNOWN_OP" && timer) {
        clearInterval(timer); // a helper without throttle.tail will not grow one: the Refresh button still asks again
        timer = null;
      }
    }
  }
  const unsub = [store.watch((s) => s.throttleEvents, () => draw()), store.watch((s) => s.throttleSession.resetAt, () => draw())];
  refresh();
  timer = setInterval(refresh, EVENTS_POLL_MS);
  return {
    el,
    refresh,
    dispose() {
      stopped = true;
      unsub.forEach((u) => u());
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}
