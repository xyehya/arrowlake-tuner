"use strict";
// arrowlake-tuner dashboard (G-105). Read-only: the only mailbox op this page can start is vf.dump (explicit button), plus cancel.
// All data is written with textContent; no HTML strings are ever parsed and there is no inline style. Contract 8 and its addenda: docs/ARCHITECTURE.md.
(function () {
  const COST_LOW = 112;   // vf.dump estimate, mailbox operations (G-102 / G-105 task)
  const COST_HIGH = 124;
  const HEARTBEAT_STALE_MS = 7000;  // the helper beats every 2 s
  const TELEMETRY_STALE_MS = 5000;
  const EVENTS_URL = "/api/events";

  const $ = (id) => document.getElementById(id);
  const S = {
    sseUp: false,
    mode: null,            // "write" | "read-only" | "refuse" | null (unknown)
    writesAllowed: null,   // boolean | null
    hbState: null, hbOpId: null, opsUsed: null, opsBudget: null, hbAt: 0,
    teleAt: 0,
    dumpId: null,          // request id of the vf.dump this page started (or adopted after a reload)
    adopted: false,        // dumpId came from a heartbeat/progress event, not from this page's own POST
    doneIds: new Set(),    // ids whose result has been shown (a late heartbeat must not re-adopt them)
    submitting: false,
    cancelling: false,
    sessionLost: false,
  };

  // ---- DOM helpers (textContent only) ----------------------------------------------------------------------------------------------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function setText(id, text) { $(id).textContent = String(text); }
  function fill(node, children) { node.replaceChildren(...children); }
  function td(text, cls, title) {
    const c = el("td", cls, text);
    if (title) c.title = title;
    return c;
  }
  function pill(text, cls) { return el("span", "pill " + cls, text); }
  function setPill(id, text, cls) { const p = $(id); p.textContent = text; p.className = "pill " + cls; }
  function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
  function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }

  // A telemetry leaf is {value, unit, source, status, reason?}. A missing value is "n/a" with the reason as a tooltip; never a guessed number.
  function reading(r, digits) {
    if (isObj(r) && isNum(r.value) && r.status !== "unavailable") {
      return { ok: true, value: r.value, text: digits === undefined ? String(r.value) : r.value.toFixed(digits), title: r.source || "" };
    }
    const reason = isObj(r) && typeof r.reason === "string" ? r.reason : "no reading";
    return { ok: false, value: null, text: "n/a", title: reason };
  }
  function readingCell(r, digits) {
    const x = reading(r, digits);
    return td(x.text, x.ok ? "n" : "n na", x.title);
  }
  function setTile(id, text, ok, title) {
    const t = $(id);
    t.textContent = text;
    t.className = "tile-value" + (ok ? "" : " na");
    t.title = title || "";
  }

  // ---- network ---------------------------------------------------------------------------------------------------------------------
  async function api(op, args) {
    let resp;
    try {
      resp = await fetch("/api/call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ op: op, args: args || {} }),
      });
    } catch (e) {
      return { status: 0, body: { ok: false, server_error: "network" } };
    }
    let body = null;
    try { body = await resp.json(); } catch (e) { body = { ok: false, server_error: "bad_response" }; }
    if (resp.status === 401) markSessionLost();
    return { status: resp.status, body: isObj(body) ? body : { ok: false, server_error: "bad_response" } };
  }

  function errText(body) {
    if (!isObj(body)) return "unknown error";
    if (isObj(body.error)) {
      let t = String(body.error.code || "ERROR") + ": " + String(body.error.message || "");
      const d = body.error.detail;
      if (isObj(d)) {
        const bits = [];
        if (d.state !== undefined) bits.push("state " + d.state);
        if (d.check !== undefined) bits.push("check " + d.check);
        if (bits.length) t += " (" + bits.join(", ") + ")";
      }
      return t;
    }
    if (typeof body.server_error === "string") return "server: " + body.server_error;
    return "unknown error";
  }

  function markSessionLost() {
    S.sessionLost = true;
    showBanner("b-refuse", "Session ended. Open a fresh launcher link to continue.");
  }

  // ---- banner ----------------------------------------------------------------------------------------------------------------------
  function showBanner(cls, text) {
    const b = $("banner");
    b.className = "banner " + cls;
    b.textContent = text;
    b.hidden = false;
  }
  function hideBanner() { $("banner").hidden = true; }

  // ---- preflight -------------------------------------------------------------------------------------------------------------------
  function statusClass(s) { return "s-" + String(s).replace(/[^a-z-]/g, ""); }

  function renderPreflight(r) {
    if (!isObj(r) || typeof r.mode !== "string" || !Array.isArray(r.checks)) {
      S.mode = null; S.writesAllowed = null;
      setText("pf-summary", "The helper answered, but not with a full preflight report.");
      $("pf-table").hidden = true;
      updateGate();
      return;
    }
    S.mode = r.mode;
    S.writesAllowed = typeof r.writes_allowed === "boolean" ? r.writes_allowed : (r.mode === "write" ? null : false);
    const sum = $("pf-summary");
    fill(sum, [
      document.createTextNode("Mode: "), pill(r.mode, statusClass(r.mode)),
      document.createTextNode("  Platform: " + String(r.platform || "n/a") + "  Writes allowed: " +
        (S.writesAllowed === null ? "n/a" : S.writesAllowed ? "yes" : "no")),
    ]);
    fill($("pf-body"), r.checks.map((c) => {
      const row = el("tr");
      row.append(td(c && c.name), el("td"), td(c && c.reason));
      row.cells[1].append(pill(c && c.status, statusClass(c && c.status)));
      return row;
    }));
    $("pf-table").hidden = false;
    if (r.mode === "read-only") {
      const why = r.checks.filter((c) => c && c.status === "read-only").map((c) => c.name + ": " + c.reason);
      showBanner("b-readonly", "Read-only mode. Writes and mailbox reads are disabled. " + why.join(" | "));
    } else if (r.mode === "refuse") {
      const why = r.checks.filter((c) => c && c.status === "refuse").map((c) => c.name + ": " + c.reason);
      showBanner("b-refuse", "Refused: the app cannot operate on this machine. " + why.join(" | "));
    } else if (S.writesAllowed === false) {
      showBanner("b-readonly", "Preflight mode is write, but the helper does not allow writes (a check is unknown). Mailbox reads are disabled.");
    } else if (!S.sessionLost) {
      hideBanner();
    }
    updateGate();
  }

  async function loadPreflight() {
    const { status, body } = await api("preflight", {});
    if (status === 200 && body.ok) renderPreflight(body.result);
    else {
      S.mode = null; S.writesAllowed = null;
      setText("pf-summary", "Preflight unavailable: " + errText(body));
      $("pf-table").hidden = true;
      updateGate();
    }
  }

  // ---- heartbeat / helper panel ----------------------------------------------------------------------------------------------------
  function onHeartbeat(ev) {
    S.hbAt = Date.now();
    S.hbState = typeof ev.state === "string" ? ev.state : null;
    S.hbOpId = typeof ev.op_id === "string" ? ev.op_id : null;
    S.opsUsed = isNum(ev.ops_used) ? ev.ops_used : null;
    S.opsBudget = isNum(ev.ops_budget) && ev.ops_budget > 0 ? ev.ops_budget : null;
    if (S.hbState && S.hbState !== "idle" && S.hbOpId && !S.dumpId && !S.doneIds.has(S.hbOpId)) {  // re-attach to a running op after a reload
      S.dumpId = S.hbOpId; S.adopted = true;
    } else if (S.hbState === "idle" && S.adopted && S.dumpId) {  // the adopted op ended and its result was missed
      S.dumpId = null; S.adopted = false; S.cancelling = false;
    }
    renderHelper();
    updateGate();
  }

  function opsLeft() {
    return S.opsUsed !== null && S.opsBudget !== null ? Math.max(S.opsBudget - S.opsUsed, 0) : null;
  }

  function renderHelper() {
    const left = opsLeft();
    setText("hb-state", S.hbState === null ? "n/a" : S.hbState);
    setText("hb-used", S.opsUsed === null ? "n/a" : S.opsUsed);
    setText("hb-budget", S.opsBudget === null ? "n/a" : S.opsBudget);
    setText("hb-left", left === null ? "n/a" : left);
    setText("vf-left", left === null ? "n/a" : left + " of " + S.opsBudget);
    const bar = $("hb-bar");
    if (left === null) bar.hidden = true;
    else { bar.max = S.opsBudget; bar.value = Math.min(S.opsUsed, S.opsBudget); bar.hidden = false; }
    if (S.hbState === null) setPill("conn-helper", "helper: n/a", "s-unknown");
    else setPill("conn-helper", "helper: " + S.hbState, S.hbState === "idle" ? "s-ok" : "s-busy");
  }

  // ---- telemetry -------------------------------------------------------------------------------------------------------------------
  function sortedKeys(o) {
    return Object.keys(o).sort((a, b) => {
      const x = Number(a), y = Number(b);
      return Number.isFinite(x) && Number.isFinite(y) ? x - y : a < b ? -1 : a > b ? 1 : 0;
    });
  }

  function onTelemetry(ev) {
    S.teleAt = Date.now();
    const v = isObj(ev.values) ? ev.values : {};
    const note = $("tele-note");
    if (!isObj(v.cores)) {
      note.textContent = "The sampler sent no per-core values" + (typeof v.status === "string" ? ": " + v.status : ".");
      note.hidden = false;
    } else {
      note.hidden = true;
    }
    renderCores(isObj(v.cores) ? v.cores : {});
    renderTemps(isObj(v.temps) ? v.temps : {});
    const power = isObj(v.power) ? v.power : {};
    const dram = isObj(v.dram) ? v.dram : {};
    const pk = reading(power.package, 1), cw = reading(power.core, 1), vc = reading(v.vcore, 3);
    setTile("t-pkgw", pk.ok ? pk.text + " W" : "n/a", pk.ok, pk.title);
    setTile("t-corew", cw.ok ? cw.text + " W" : "n/a", cw.ok, cw.title);
    setTile("t-vcore", vc.ok ? vc.text + " V" : "n/a", vc.ok, vc.title);
    const dr = reading(dram.read, 1), dw = reading(dram.write, 1);
    setTile("t-dram", dr.ok && dw.ok ? dr.text + " / " + dw.text + " GB/s" : "n/a", dr.ok && dw.ok, dr.ok && dw.ok ? "" : (dr.ok ? dw.title : dr.title));
    const lim = reading(v.limit_reasons_raw);
    setTile("t-limit", lim.ok ? "0x" + lim.value.toString(16).toUpperCase() : "n/a", lim.ok, lim.title);
    // highest core clock: derived only from values that were actually read
    let best = null;
    for (const k of Object.keys(isObj(v.cores) ? v.cores : {})) {
      const m = reading(v.cores[k] && v.cores[k].mhz);
      if (m.ok && (best === null || m.value > best.value)) best = { value: m.value, cpu: k };
    }
    setTile("t-mhz", best ? best.value.toFixed(0) + " MHz" : "n/a", best !== null, best ? "cpu " + best.cpu : "no clock reading");
    // hottest sensor, same rule
    let hot = null;
    for (const k of Object.keys(isObj(v.temps) ? v.temps : {})) {
      const t = reading(v.temps[k]);
      if (t.ok && (hot === null || t.value > hot.value)) hot = { value: t.value, label: k };
    }
    setTile("t-temp", hot ? hot.value.toFixed(0) + " C" : "n/a", hot !== null, hot ? hot.label : "no temperature reading");
    renderAges();
  }

  function renderCores(cores) {
    const keys = sortedKeys(cores);
    if (!keys.length) {
      const row = el("tr"), cell = td("n/a (no per-core values)", "na");
      cell.colSpan = 6;
      row.append(cell);
      fill($("core-body"), [row]);
      return;
    }
    fill($("core-body"), keys.map((k) => {
      const c = isObj(cores[k]) ? cores[k] : {};
      const row = el("tr");
      row.append(td(k, "n"), td(typeof c.name === "string" ? c.name : "n/a", typeof c.name === "string" ? "" : "na"),
        readingCell(c.mhz, 0), readingCell(c.busy, 1), readingCell(c.ratio, 0), readingCell(c.vid, 3));
      return row;
    }));
  }

  function renderTemps(temps) {
    const keys = Object.keys(temps).sort();
    if (!keys.length) {
      const row = el("tr"), cell = td("n/a", "na");
      cell.colSpan = 2;
      row.append(cell);
      fill($("temp-body"), [row]);
      return;
    }
    fill($("temp-body"), keys.map((k) => {
      const row = el("tr");
      row.append(td(k), readingCell(temps[k], 0));
      return row;
    }));
  }

  // ---- ages / staleness (1 Hz) ------------------------------------------------------------------------------------------------------
  function renderAges() {
    const now = Date.now();
    const ta = $("tele-age");
    if (!S.teleAt) { ta.textContent = "no sample yet"; ta.className = "muted"; }
    else {
      const age = (now - S.teleAt) / 1000;
      const stale = now - S.teleAt > TELEMETRY_STALE_MS;
      ta.textContent = stale ? "stale: last sample " + age.toFixed(0) + " s ago" : "live";
      ta.className = stale ? "stale" : "muted";
    }
    const ha = $("hb-age");
    if (!S.hbAt) ha.textContent = "n/a";
    else {
      const stale = now - S.hbAt > HEARTBEAT_STALE_MS;
      ha.textContent = stale ? "lost (" + ((now - S.hbAt) / 1000).toFixed(0) + " s ago)" : ((now - S.hbAt) / 1000).toFixed(0) + " s ago";
      ha.className = stale ? "stale" : "";
    }
  }
  function heartbeatFresh() { return S.hbAt > 0 && Date.now() - S.hbAt <= HEARTBEAT_STALE_MS; }

  // ---- vf.dump gate and flow -------------------------------------------------------------------------------------------------------
  function gateReason() {
    if (S.sessionLost) return "Session ended.";
    if (!S.sseUp) return "Waiting for the event stream.";
    if (S.mode === null) return "Preflight has not been read.";
    if (S.mode !== "write") return "Preflight mode is " + S.mode + ": mailbox reads are disabled.";
    if (S.writesAllowed !== true) return "The helper's preflight does not allow writes.";
    if (!heartbeatFresh() || opsLeft() === null) return "Waiting for a helper heartbeat with the ops budget.";
    if (S.dumpId || S.submitting) return "A helper operation is running.";
    if (S.hbState !== "idle") return "The helper is " + S.hbState + ".";
    if (opsLeft() < COST_HIGH) return "Does not fit: " + opsLeft() + " ops left, up to " + COST_HIGH + " needed.";
    return "";
  }

  function updateGate() {
    const reason = gateReason();
    $("btn-dump").disabled = reason !== "";
    setText("vf-reason", reason);
    const running = Boolean(S.dumpId);
    $("btn-cancel").disabled = !running || S.cancelling || S.sessionLost;
    $("vf-run").hidden = !running;
  }

  async function startDump() {
    if (gateReason() !== "") return;
    S.submitting = true;
    setText("vf-status", "Submitting...");
    updateGate();
    const { status, body } = await api("vf.dump", {});
    S.submitting = false;
    if (status === 202 && isObj(body) && typeof body.id === "string") {
      S.dumpId = body.id; S.adopted = false;
      S.cancelling = false;
      setText("vf-status", "Running (operation " + body.id + "). Results arrive over the event stream.");
      setText("vf-progress", "waiting for the first step");
      $("vf-bar").max = 1; $("vf-bar").value = 0;
    } else {
      setText("vf-status", "Not started: " + (status === 429 ? "too many operations pending" : errText(body)));
    }
    updateGate();
  }

  async function cancelDump() {
    const id = S.dumpId || S.hbOpId;
    if (!id) return;
    S.cancelling = true;
    updateGate();
    const { status, body } = await api("cancel", { op_id: id });
    if (status === 200 && body.ok) setText("vf-status", "Cancel requested. It takes effect between mailbox transactions.");
    else { S.cancelling = false; setText("vf-status", "Cancel not accepted: " + errText(body)); }
    updateGate();
  }

  function onProgress(ev) {
    if (!S.dumpId && typeof ev.op_id === "string" && !S.doneIds.has(ev.op_id)) { S.dumpId = ev.op_id; S.adopted = true; }
    if (ev.op_id !== S.dumpId) return;
    if (isNum(ev.step) && isNum(ev.of) && ev.of > 0) {
      $("vf-bar").max = ev.of; $("vf-bar").value = Math.min(ev.step, ev.of);
      let t = "step " + ev.step + " of " + ev.of;
      if (isNum(ev.ops_used) && isNum(ev.ops_budget)) {
        t += "; ops used " + ev.ops_used + " / " + ev.ops_budget;
        S.opsUsed = ev.ops_used; S.opsBudget = ev.ops_budget;
        renderHelper();
      }
      setText("vf-progress", t);
    }
    updateGate();
  }

  function onResult(ev) {
    // A result for an id this page does not remember (reload, second tab) is shown too: the operation is addressed by id, not by tab.
    if (typeof ev.id === "string") S.doneIds.add(ev.id);
    if (S.dumpId !== null && ev.id !== S.dumpId) return;  // another tab's operation ended; this page's running op is untouched
    S.dumpId = null; S.adopted = false; S.cancelling = false;  // one long op at a time in G1
    $("vf-run").hidden = true;
    if (ev.ok === true && isObj(ev.result)) {
      const r = ev.result;
      setText("vf-status", (r.complete === false ? "Incomplete read. " : "Read complete. ") +
        (isNum(r.ops_done) ? r.ops_done + " mailbox operations used. " : "") + (r.simulator === true ? "(simulator data)" : ""));
      renderDump(r);
      renderAccepted(r);
    } else if (typeof ev.server_error === "string") {
      setText("vf-status", ev.server_error === "helper_lost" ? "The helper was lost before it answered. Check the audit log before retrying."
        : "Failed: " + ev.server_error);
    } else {
      const code = isObj(ev.error) ? ev.error.code : "";
      setText("vf-status", code === "CANCELLED" ? "Cancelled. The requests already sent are in the audit log. " + errText(ev) : "Failed: " + errText(ev));
    }
    updateGate();
    loadAudit();
  }

  function fmtPoint(p) {
    if (!isObj(p)) return "n/a";
    const ratio = isNum(p.ratio) ? p.ratio + "x" : "n/a";
    const off = isNum(p.offset_mv) ? (p.offset_mv > 0 ? "+" : "") + p.offset_mv.toFixed(1) + " mV" : "n/a";
    return ratio + " " + off;
  }

  function renderDump(r) {
    const rows = [];
    for (const u of Array.isArray(r.units) ? r.units : []) {
      for (const c of Array.isArray(u && u.cpus) ? u.cpus : []) {
        const row = el("tr");
        const pts = Array.isArray(c.points) ? c.points.map(fmtPoint).join(", ") : "n/a";
        row.append(td(c.cpu, "n"), td(typeof c.name === "string" ? c.name : u.name), td(c.oc ? fmtPoint(c.oc) : "n/a", c.oc ? "" : "na"), td(pts, "mono"));
        rows.push(row);
      }
    }
    fill($("vf-body"), rows);
    $("vf-table").hidden = rows.length === 0;
  }

  function renderAccepted(r) {
    const sum = $("acc-summary");
    const tbl = $("acc-table");
    if (!("accepted_match" in r)) {
      sum.textContent = "This helper does not report an accepted-state comparison.";
      tbl.hidden = true;
      return;
    }
    const m = r.accepted_match;
    if (m === null) {
      sum.textContent = "No accepted state installed.";
      tbl.hidden = true;
      return;
    }
    if (!Array.isArray(m)) {
      sum.textContent = "The accepted-state comparison has an unexpected shape.";
      tbl.hidden = true;
      return;
    }
    const bad = m.filter((x) => !isObj(x) || x.match !== true);
    const incomplete = r.complete === false ? " The read was incomplete, so cores that were not read are missing below." : "";
    sum.textContent = (bad.length === 0 ? "Matches accepted on all " + m.length + " compared cores." : "Differs from accepted on " + bad.length + " of " + m.length + " cores.") + incomplete;
    fill($("acc-body"), m.map((x) => {
      const o = isObj(x) ? x : {};
      const diffs = Array.isArray(o.diffs) ? o.diffs : [];
      const row = el("tr");
      row.append(td(o.cpu, "n"), el("td"), td(
        diffs.map((d) => "point " + (isObj(d) ? d.point : "?") + ": expected " + (isObj(d) ? d.expected : "?") + ", actual " + (isObj(d) ? d.actual : "?")).join("; ") || "none", "mono"));
      row.cells[1].append(pill(o.match === true ? "match" : "differs (" + diffs.length + ")", o.match === true ? "s-ok" : "s-bad"));
      return row;
    }));
    tbl.hidden = false;
  }

  // ---- audit tail ------------------------------------------------------------------------------------------------------------------
  function hex(v) { return isNum(v) ? "0x" + v.toString(16) : v === undefined || v === null ? "" : String(v); }

  async function loadAudit() {
    const { status, body } = await api("audit.tail", { n: 20 });
    const note = $("audit-note");
    if (!(status === 200 && body.ok && isObj(body.result))) {
      note.textContent = "Audit log unavailable: " + errText(body);
      $("audit-table").hidden = true;
      return;
    }
    const recs = Array.isArray(body.result.records) ? body.result.records.slice().reverse() : [];
    note.textContent = recs.length ? "Newest first" + (body.result.truncated ? " (older records not shown)." : ".") : "The audit log is empty.";
    fill($("audit-body"), recs.map((r) => {
      const o = isObj(r) ? r : { unparsable: String(r) };
      const row = el("tr");
      row.append(td(o.ts, "mono"), td(o.seq, "n"), td(o.event !== undefined ? o.event : (o.unparsable !== undefined ? "unparsable" : "")),
        td(o.cpu), td(hex(o.cmd), "mono"), td(o.rc_name !== undefined ? o.rc_name : (o.rc !== undefined ? o.rc : "")), td(o.reason !== undefined ? o.reason : ""));
      return row;
    }));
    $("audit-table").hidden = recs.length === 0;
  }

  // ---- event stream ----------------------------------------------------------------------------------------------------------------
  let es = null;
  let retryTimer = null;

  function parse(e) {
    try { const d = JSON.parse(e.data); return isObj(d) ? d : null; } catch (x) { return null; }
  }

  async function onOpen() {
    S.sseUp = true;
    setPill("conn-sse", "events: connected", "s-ok");
    updateGate();
    api("telemetry.start", { interval_s: 1 });  // read-only sampler; already running is fine
    loadPreflight();
    loadAudit();
  }

  function onError() {
    S.sseUp = false;
    setPill("conn-sse", "events: reconnecting", "s-busy");
    updateGate();
    if (es && es.readyState === 2) {  // CLOSED: the server refused the stream (expired session, too many streams); retry ourselves
      es = null;
      clearTimeout(retryTimer);
      retryTimer = setTimeout(connect, 3000);
      fetch("/api/status", { credentials: "same-origin" }).then((r) => { if (r.status === 401) markSessionLost(); }).catch(() => {});
    }
  }

  function connect() {
    if (es) es.close();
    es = new EventSource(EVENTS_URL);
    es.onopen = onOpen;
    es.onerror = onError;
    const on = (name, fn) => es.addEventListener(name, (e) => { const d = parse(e); if (d) fn(d); });
    on("heartbeat", onHeartbeat);
    on("telemetry", onTelemetry);
    on("progress", onProgress);
    on("result", onResult);
    on("state", (d) => { if (typeof d.state === "string") { S.hbState = d.state; renderHelper(); updateGate(); } });
  }

  // ---- wiring ----------------------------------------------------------------------------------------------------------------------
  $("btn-preflight").addEventListener("click", loadPreflight);
  $("btn-audit").addEventListener("click", loadAudit);
  $("btn-dump").addEventListener("click", startDump);
  $("btn-cancel").addEventListener("click", cancelDump);
  setInterval(() => { renderAges(); updateGate(); }, 1000);
  renderHelper();
  updateGate();
  connect();
})();
