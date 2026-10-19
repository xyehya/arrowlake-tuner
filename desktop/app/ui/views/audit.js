// views/audit.js: Audit screen (PLAN §4.4): the helper's audit log tail (audit.tail, newest first), filterable. Read-only; no mailbox traffic.
// Port of web/app.js "audit tail" (MB_NAMES/MSR_NAMES/auditRequest/auditTime/auditKeep/loadAudit :5218-5266). Request names follow docs/knowledge/registers.md
// "Mailbox commands" and the plain MSRs the guard allows; anything else shows as hex. Differences: a free-text filter next to the web page's two-way
// filter, a visible verdict for read-back records (verified / MISMATCH), and the row model is pure so it is unit-tested (auditRows).
import { h, clear } from "../components/dom.js";
import { NA } from "../components/fmt.js";
import { installScreenStyles, installStyles, segmented } from "../components/spark.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isNum = (x) => typeof x === "number" && Number.isFinite(x);

export const AUDIT_N = 200;

/** Mailbox command names (registers.md "Mailbox commands"). */
export const MB_NAMES = {
  0x01: "OC capability read",
  0x02: "per-core capability read",
  0x09: "fused top point read",
  0x10: "V/F word read",
  0x11: "V/F word write",
  0x14: "misc config read",
  0x15: "misc config write",
  0x1c: "favored ratio read",
  0x1d: "favored ratio write",
  0x26: "granular ratio read",
  0x27: "granular ratio write",
  0x28: "max voltage read",
};
/** Plain MSRs the guard allows reading (registers.md). */
export const MSR_NAMES = { 0xce: "platform info", 0x198: "perf status", 0x1ad: "P turbo ratios", 0x1ae: "P turbo core counts", 0x650: "E turbo ratios", 0x651: "E turbo core counts" };
/** Mailbox commands that change the CPU. */
export const WRITE_CMDS = [0x11, 0x15, 0x1d, 0x27];
const READ_VF_CMD = 0x10;

export const hex = (v) => (isNum(v) ? "0x" + v.toString(16) : v === undefined || v === null ? "" : String(v));

function toNum(v) {
  if (isNum(v)) return v;
  return typeof v === "string" && /^0x[0-9a-f]+$/i.test(v) ? parseInt(v, 16) : null;
}

/** "0x11 V/F word write (0x00, 0x05)" / "MSR 0x198 perf status" / "" for a record with neither. */
export function auditRequest(o) {
  const cmd = toNum(o.cmd);
  const msr = toNum(o.msr);
  if (cmd !== null) {
    const p = [o.p1, o.p2];
    const hasP = toNum(o.p1) !== null || toNum(o.p2) !== null;
    return hex(cmd) + (MB_NAMES[cmd] ? " " + MB_NAMES[cmd] : "") + (hasP ? " (" + p.map((x) => (toNum(x) !== null ? hex(toNum(x)) : "-")).join(", ") + ")" : "");
  }
  if (msr !== null) return "MSR " + hex(msr) + (MSR_NAMES[msr] ? " " + MSR_NAMES[msr] : "") + (o.msr_kind === "write" ? " write" : "");
  return "";
}

/** Local "Oct 18, 08:58:27" from an ISO timestamp; the raw value when it does not parse. */
export function auditTime(ts) {
  const d = typeof ts === "string" ? new Date(ts) : null;
  return d && !Number.isNaN(d.getTime())
    ? d.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : String(ts === undefined || ts === null ? "" : ts);
}

/** "writes" keeps what changed or could have changed the CPU: write attempts and their results, read-backs, refusals, errors and trips; G-235: also the runtime / cpufreq / powercap
 *  attempts and the op-level `result` summaries (a measure or stress run, a recovered journal, a saved profile). */
export function auditKeep(o, filter) {
  if (filter === "all") return true;
  if (["refused", "error", "trip", "readback", "runtime_attempt", "powercap_attempt"].includes(o.event)) return true;
  if (o.event === "result" && typeof o.op === "string" && o.cmd === undefined && o.msr === undefined) return true; // G-235: the summary record of an op that changed cpufreq or a file (vf.measure, stress.start, profiles.save, journal.recover ...)
  const cmd = toNum(o.cmd);
  return o.msr_kind === "write" || (cmd !== null && WRITE_CMDS.includes(cmd));
}

/** One display row from a record (or from a line the helper could not parse). */
export function auditRow(r) {
  const o = isObj(r) ? r : { unparsable: String(r) };
  const rc = o.rc_name !== undefined ? o.rc_name : o.rc !== undefined ? o.rc : "";
  const rcOk = rc === 0 || rc === "Pass" || rc === "OK";
  let kind = rc === "" ? "" : rcOk ? "ok" : "warn";
  let result = String(rc === "" && o.value !== undefined ? o.value : rc);
  // G-230: a V/F read (0x10) that walks past the last point answers rc 0x11: the expected end of the table, not an error (a write or anything else with 0x11 stays amber)
  if ((rc === 17 || rc === "UNKNOWN_0x11") && toNum(o.cmd) === READ_VF_CMD) {
    result = "end of table (0x11)";
    kind = "";
  }
  let verdict = "";
  if (o.event === "readback") {
    verdict = o.verified === true ? "verified" : o.verified === false ? "MISMATCH" : o.pending === true ? "pending (re-read " + (isNum(o.poll) ? o.poll + 1 : "?") + ")" : "";
    if (o.verified === false) kind = "error";
  }
  const event = o.event !== undefined ? String(o.event) : o.unparsable !== undefined ? "unparsable" : "";
  // G-235: `detail` first (the summary records carry "vf.measure: 48 files, restored ok"); a run summary without one is composed from its counts
  const summary = event === "result" && typeof o.op === "string" && isNum(o.file_count) ? `${o.op}: ${o.file_count} files, ${o.restored === false ? "NOT restored" : "restored ok"}` : "";
  const detail = o.detail !== undefined && o.detail !== null ? String(o.detail) : summary || (o.reason !== undefined ? String(o.reason) : o.purpose !== undefined ? String(o.purpose) : o.unparsable !== undefined ? o.unparsable : "");
  if (rc === "" && o.value === undefined && typeof o.outcome === "string") {
    result = o.outcome;
    kind = o.restored === false || o.outcome === "error" ? "error" : o.outcome === "cancelled" || o.outcome === "cancel" ? "warn" : "ok";
  }
  const eventKind = ["refused", "error", "trip"].includes(event) || (event === "result" && o.restored === false) ? "error" : event === "readback" ? (o.verified === false ? "error" : "ok") : "";
  return {
    time: auditTime(o.ts),
    timeTitle: String(o.ts ?? ""),
    seq: o.seq === undefined ? "" : String(o.seq),
    event,
    eventKind,
    cpu: o.cpu === undefined || o.cpu === null ? "" : String(o.cpu),
    request: auditRequest(o) || (typeof o.op === "string" ? o.op : ""),
    result,
    resultKind: kind,
    verdict,
    detail,
  };
}

/** Rows newest first, after the filter ("all" | "writes") and the free-text query (case-insensitive over every shown column). */
export function auditRows(records, { filter = "all", text = "" } = {}) {
  const q = String(text).trim().toLowerCase();
  const kept = (Array.isArray(records) ? records.slice().reverse() : []).filter((r) => !isObj(r) || auditKeep(r, filter)).map(auditRow);
  if (!q) return kept;
  return kept.filter((r) => [r.time, r.seq, r.event, r.cpu, r.request, r.result, r.verdict, r.detail].join(" ").toLowerCase().includes(q));
}

/** The line above the table: how many records, which filter, newest first, truncation. */
export function auditNote(total, shown, { filter, text }, truncated) {
  if (!total) return "The audit log is empty.";
  const scope = filter === "writes" ? `${shown} write-related of the last ${total} records` : text.trim() ? `${shown} of the last ${total} records match` : `Last ${total} records`;
  return `${scope}, newest first, local time${truncated ? " (older records not shown)." : "."}`;
}

const CSS = `
.audit-bar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.audit-search { min-width: 220px; padding: 3px 8px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); font: inherit; }
.audit-search::placeholder { color: var(--text-faint); }
.audit-wrap { overflow: auto; max-height: calc(100vh - 280px); min-height: 120px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.audit-wrap .tbl th { position: sticky; top: 0; background: var(--surface-2); z-index: 1; }
.audit-wrap .tbl td.when { white-space: nowrap; }
.audit-wrap .tbl td.detail { color: var(--text-dim); max-width: 520px; overflow-wrap: anywhere; }
.audit-wrap .tbl tr.bad td { background: var(--danger-bg); }
.audit-wrap .chip { padding: 0 8px; font-size: var(--fs-xs); }
.audit-wrap .chip::before { width: 6px; height: 6px; }
.audit-empty { padding: var(--space-4); }
`;

export function auditView() {
  const subs = [];
  return {
    id: "audit",
    mount(root, ctx) {
      const { bridge, store } = ctx;
      installScreenStyles();
      installStyles("audit", CSS);
      const state = { filter: "all", text: "", records: [], truncated: false, path: "", phase: "loading", error: "", seq: 0 };

      const note = h("p", { class: "dcard-note", id: "audit-note", role: "status" });
      const body = h("tbody", { id: "audit-body" });
      const table = h(
        "table",
        { class: "tbl", id: "audit-table" },
        h("caption", { class: "muted", hidden: true, text: "Audit log, newest first" }),
        h("thead", {}, h("tr", {}, ...["Time", "Seq", "Event", "CPU", "Request", "Result", "Detail"].map((t, i) => h("th", { class: i === 1 || i === 3 ? "n" : "", text: t })))),
        body,
      );
      const wrap = h("div", { class: "audit-wrap", id: "audit-wrap", tabindex: "0", "aria-label": "Audit records" }, table);
      const empty = h("p", { class: "audit-empty na", id: "audit-empty", hidden: true });
      const refresh = h("button", { type: "button", class: "btn", id: "audit-refresh", text: "Refresh", on: { click: () => load() } });
      const search = h("input", { type: "search", class: "audit-search", id: "audit-search", placeholder: "Filter text…", "aria-label": "Filter text", on: { input: () => { state.text = search.value; draw(); } } });
      const seg = segmented([{ value: "all", label: "All" }, { value: "writes", label: "Writes & refusals", title: "Write attempts and results, read-backs, refusals, errors and trips" }], "all", (v) => { state.filter = v; draw(); }, { label: "Audit filter" });
      const path = h("span", { class: "meta", id: "audit-path" });

      root.append(
        h(
          "div",
          { class: "sc audit" },
          h("header", { class: "screen-head" }, h("h1", { text: "Audit" }), h("span", { class: "sc-meta", text: "what the helper did, from its own log" })),
          h("section", { class: "dcard" }, h("div", { class: "dcard-head audit-bar" }, seg.el, search, h("span", { class: "spacer" }), path, refresh), note, wrap, empty),
        ),
      );

      function draw() {
        clear(body);
        refresh.disabled = state.phase === "loading";
        path.textContent = state.path;
        if (state.phase === "loading") {
          note.textContent = "Reading the audit log…";
          wrap.hidden = true;
          empty.hidden = true;
          return;
        }
        if (state.phase === "error") {
          note.textContent = `Audit log unavailable: ${state.error}`;
          wrap.hidden = true;
          empty.hidden = false;
          empty.textContent = `${NA} · ${state.error}`;
          return;
        }
        const rows = auditRows(state.records, state);
        note.textContent = auditNote(state.records.length, rows.length, state, state.truncated);
        wrap.hidden = rows.length === 0;
        empty.hidden = rows.length !== 0;
        empty.textContent = state.records.length ? "No record matches this filter." : "The audit log is empty.";
        for (const r of rows) {
          const tr = h("tr", { class: r.eventKind === "error" || r.resultKind === "error" ? "bad" : "" });
          const chip = (text, kind) => (text === "" ? "" : h("span", { class: "chip", dataset: kind ? { state: kind } : {}, text }));
          tr.append(
            h("td", { class: "mono when", title: r.timeTitle, text: r.time }),
            h("td", { class: "n", text: r.seq }),
            h("td", {}, chip(r.event, r.eventKind)),
            h("td", { class: "n", text: r.cpu }),
            h("td", { class: "mono", text: r.request }),
            h("td", {}, chip(r.result, r.resultKind), r.verdict ? h("span", { class: "meta", text: " " + r.verdict }) : null),
            h("td", { class: "detail", text: r.detail }),
          );
          body.append(tr);
        }
      }

      async function load() {
        const mine = ++state.seq;
        state.phase = "loading";
        draw();
        const res = await bridge.call("audit.tail", { n: AUDIT_N });
        if (mine !== state.seq) return; // unmounted or a newer request won
        if (res.ok && isObj(res.result)) {
          state.records = Array.isArray(res.result.records) ? res.result.records : [];
          state.truncated = res.result.truncated === true;
          state.path = typeof res.result.path === "string" ? res.result.path : "";
          state.phase = "done";
        } else {
          state.phase = "error";
          state.error = res.error && res.error.message ? res.error.message : "the helper did not answer";
        }
        draw();
      }

      // The helper link may come up after this screen opened: load when it becomes connected.
      subs.push(store.watch((s) => s.link.state, (st) => { if (st === "connected" && state.phase !== "done") load(); }));
      subs.push(() => { state.seq = -1; });
      if (store.get().link.state !== "connected") {
        state.phase = "error";
        state.error = `the helper is not connected (${store.get().link.state.replace("_", " ")})`;
        draw();
      }
    },
    unmount() {
      subs.splice(0).forEach((u) => u());
    },
  };
}
