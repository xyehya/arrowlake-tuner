"use strict";
// arrowlake-tuner dashboard (G-105, redesigned in G-110). Read-only: the only mailbox op this page can start is vf.dump (explicit button), plus cancel.
// All data is written with textContent; no HTML strings are ever parsed and there is no inline style: geometry and colour go through SVG attributes
// and classes only (CSP style-src 'self'). Contract 8 and its addenda: docs/ARCHITECTURE.md.
(function () {
  const COST_LOW = 112;   // vf.dump estimate, mailbox operations (G-102 / G-105 task)
  const COST_HIGH = 124;
  const HEARTBEAT_STALE_MS = 7000;  // the helper beats every 2 s
  const TELEMETRY_STALE_MS = 5000;
  const EVENTS_URL = "/api/events";
  const HISTORY_MS = 300000;        // client-side history window: 5 min
  const HISTORY_MAX = 330;          // ring buffer length (1 Hz sampling, with slack)
  const SPARK_MS = 60000;           // per-core and readout sparklines: last 60 s

  const $ = (id) => document.getElementById(id);
  const SVGNS = $("die").namespaceURI;  // taken from the page so no namespace URL literal is needed
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
  // View state (client only; nothing here is sent anywhere).
  const V = {
    tab: "overview",
    hist: [],              // ring buffer of samples {t, pkg, corew, vcore, hot, top, dr, dw, cores: {cpu: {mhz, busy, vid, temp}}, temps: {key: C}}
    last: {},              // latest telemetry values (for n/a reasons)
    coreKeys: "",          // signature of the current core set (rebuild map/table when it changes)
    coreInfo: [],          // [{cpu, name, short, kind}]
    selected: null,        // selected cpu key (Cores tab)
    metric: "mhz",         // core-map colouring
    filter: "all",         // Cores tab filter
    probing: false,        // a core-map tile is hovered or focused (its readings are in the probe line)
  };

  // ---- DOM helpers (textContent only) ----------------------------------------------------------------------------------------------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function sv(tag, attrs, cls) {
    const e = document.createElementNS(SVGNS, tag);
    if (attrs) for (const k of Object.keys(attrs)) e.setAttribute(k, String(attrs[k]));
    if (cls) e.setAttribute("class", cls);
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
  function clamp(x, a, b) { return Math.min(Math.max(x, a), b); }

  // A telemetry leaf is {value, unit, source, status, reason?}. A missing value is "n/a" with the reason as a tooltip; never a guessed number.
  function reading(r, digits) {
    if (isObj(r) && isNum(r.value) && r.status !== "unavailable") {
      return { ok: true, value: r.value, text: digits === undefined ? String(r.value) : r.value.toFixed(digits), title: r.source || "" };
    }
    const reason = isObj(r) && typeof r.reason === "string" ? r.reason : "no reading";
    return { ok: false, value: null, text: "n/a", title: reason };
  }
  function num(r) { const x = reading(r); return x.ok ? x.value : null; }
  function readingCell(r, digits) {
    const x = reading(r, digits);
    return td(x.text, x.ok ? "n" : "n na", x.title);
  }
  // Overview readout: big number + small unit, or "n/a" with the reason on hover.
  function setTile(id, value, unit, ok, title) {
    const t = $(id);
    if (ok) fill(t, [document.createTextNode(value), el("span", "unit", unit)]);
    else t.textContent = "n/a";
    t.className = "ro-value" + (ok ? "" : " na") + (t.classList.contains("mono") ? " mono" : "");
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

  // ---- tabs (ARIA tablist: roving tabindex, arrow keys, Home/End; the active tab lives in the URL hash) -----------------------------
  const TABS = ["overview", "cores", "sensors", "vf", "system", "audit"];

  function selectTab(name, focus) {
    if (TABS.indexOf(name) < 0) name = "overview";
    V.tab = name;
    for (const t of TABS) {
      const btn = $("tab-" + t), panel = $("panel-" + t);
      const on = t === name;
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
      panel.hidden = !on;
    }
    if (focus) $("tab-" + name).focus();
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    renderVisible();
  }

  function onTabKey(e) {
    const i = TABS.indexOf(V.tab);
    let j = null;
    if (e.key === "ArrowRight") j = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") j = (i + TABS.length - 1) % TABS.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = TABS.length - 1;
    if (j === null) return;
    e.preventDefault();
    selectTab(TABS[j], true);
  }

  // ---- segmented controls (view-only toggles, built here so the HTML has no extra buttons) -----------------------------------------
  function segmented(host, options, current, onPick) {
    const btns = options.map((o) => {
      const b = el("button", "seg-btn", o.label);
      b.type = "button";
      b.setAttribute("aria-pressed", o.value === current ? "true" : "false");
      b.addEventListener("click", () => {
        for (const x of btns) x.setAttribute("aria-pressed", x === b ? "true" : "false");
        onPick(o.value);
      });
      return b;
    });
    fill(host, btns);
  }

  // ---- colour ramps (sequential, one hue each, dark to bright; the core map always sits on a dark die) ------------------------------
  const METRICS = {
    mhz: { label: "Clock", unit: "MHz", lo: 400, hi: 6000, digits: 0, ramp: ["#132a4a", "#1d4f7c", "#2a7fb0", "#45b4dc", "#9fe6ff"] },
    busy: { label: "Busy", unit: "%", lo: 0, hi: 100, digits: 0, ramp: ["#221a46", "#3f3285", "#6852c4", "#9a80f0", "#d9ccff"] },
    temp: { label: "Temp", unit: "C", lo: 25, hi: 105, digits: 0, ramp: ["#33200f", "#6e3812", "#b5591a", "#ec8a2c", "#ffd391"] },
    vid: { label: "VID", unit: "V", lo: 0.6, hi: 1.5, digits: 3, ramp: ["#0f302a", "#185a4a", "#238a70", "#40bf98", "#a3efd2"] },
  };
  function hexRgb(h) { return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }
  function rampAt(ramp, f) {
    const x = clamp(f, 0, 1) * (ramp.length - 1);
    const i = Math.min(Math.floor(x), ramp.length - 2), u = x - i;
    const a = hexRgb(ramp[i]), b = hexRgb(ramp[i + 1]);
    const c = a.map((v, k) => Math.round(v + (b[k] - v) * u));
    return { hex: "#" + c.map((v) => v.toString(16).padStart(2, "0")).join(""), light: (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255 > 0.55 };
  }

  // ---- core identity ---------------------------------------------------------------------------------------------------------------
  function sortedKeys(o) {
    return Object.keys(o).sort((a, b) => {
      const x = Number(a), y = Number(b);
      return Number.isFinite(x) && Number.isFinite(y) ? x - y : a < b ? -1 : a > b ? 1 : 0;
    });
  }
  // P or E: from the topology name ("P-Core 3", "E-Core 12"); without a name, Linux CPUs 0..7 are the P-cores (ARCHITECTURE.md contract 1).
  function coreKind(cpu, name) {
    if (typeof name === "string") {
      if (/^\s*p/i.test(name)) return "P";
      if (/^\s*e/i.test(name)) return "E";
    }
    const n = Number(cpu);
    return Number.isFinite(n) && n < 8 ? "P" : "E";
  }
  function shortName(name, kind, cpu) {
    if (typeof name === "string") {
      const m = name.match(/(\d+)\s*$/);
      if (m) return kind + m[1];
      return name.slice(0, 4);
    }
    return "c" + cpu;
  }

  // Temperatures by CPU (G-109 adds {name, cpu} to temps entries). Without a cpu field there is no per-core temperature.
  function cpuOf(t) {
    if (!isObj(t)) return null;
    return isNum(t.cpu) ? String(t.cpu) : typeof t.cpu === "string" && /^\d+$/.test(t.cpu) ? t.cpu : null;
  }
  function tempsByCpu(temps) {
    const out = {};
    for (const k of Object.keys(temps)) {
      const cpu = cpuOf(temps[k]);
      if (cpu !== null) out[cpu] = temps[k];
    }
    return out;
  }
  const NO_CORE_TEMP = { value: null, status: "unavailable", reason: "this sample has no per-core temperature mapping" };

  function coreReading(cpu, metric) {
    const cores = isObj(V.last.cores) ? V.last.cores : {};
    const c = isObj(cores[cpu]) ? cores[cpu] : {};
    if (metric === "temp") {
      const t = tempsByCpu(isObj(V.last.temps) ? V.last.temps : {})[cpu];
      return t === undefined ? NO_CORE_TEMP : t;
    }
    return c[metric];
  }

  // ---- preflight -------------------------------------------------------------------------------------------------------------------
  function statusClass(s) { return "s-" + String(s).replace(/[^a-z-]/g, ""); }
  const CHECK_ORDER = { refuse: 0, "read-only": 1, unknown: 2, missing: 3, pass: 4 };

  function renderPreflight(r) {
    if (!isObj(r) || typeof r.mode !== "string" || !Array.isArray(r.checks)) {
      S.mode = null; S.writesAllowed = null;
      setText("pf-summary", "The helper answered, but not with a full preflight report.");
      fill($("pf-body"), []);
      updateGate();
      return;
    }
    S.mode = r.mode;
    S.writesAllowed = typeof r.writes_allowed === "boolean" ? r.writes_allowed : (r.mode === "write" ? null : false);
    const sum = $("pf-summary");
    sum.className = "pf-summary";
    fill(sum, [
      el("span", "pf-k", "Preflight mode"), pill(r.mode, statusClass(r.mode)),
      el("span", "muted", "no write controls in this build"),
      el("span", "pf-k", "Platform"), el("span", "", String(r.platform || "n/a")),
      el("span", "pf-k", "Helper permits mailbox requests"),
      pill(S.writesAllowed === null ? "n/a" : S.writesAllowed ? "yes" : "no", S.writesAllowed ? "s-ok" : "s-unknown"),
    ]);
    const checks = r.checks.slice().sort((a, b) => {
      const x = CHECK_ORDER[a && a.status], y = CHECK_ORDER[b && b.status];
      return (x === undefined ? 5 : x) - (y === undefined ? 5 : y);
    });
    fill($("pf-body"), checks.map((c) => {
      const li = el("li", "check");
      li.append(pill(c && c.status, statusClass(c && c.status)), el("span", "check-name", c && c.name), el("span", "check-reason", c && c.reason));
      return li;
    }));
    if (r.mode === "read-only") {
      const why = r.checks.filter((c) => c && c.status === "read-only").map((c) => c.name + ": " + c.reason);
      showBanner("b-readonly", "Read-only mode. Mailbox reads are disabled. " + why.join(" | "));
    } else if (r.mode === "refuse") {
      const why = r.checks.filter((c) => c && c.status === "refuse").map((c) => c.name + ": " + c.reason);
      showBanner("b-refuse", "Refused: the app cannot operate on this machine. " + why.join(" | "));
    } else if (S.writesAllowed === false) {
      showBanner("b-readonly", "The helper does not permit mailbox requests (a preflight check is unknown), so the V/F read is disabled. See System.");
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
      fill($("pf-body"), []);
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

  function setRing(id, left, budget) {
    const r = $(id);
    if (left === null) { r.setAttribute("stroke-dasharray", "0 100"); r.setAttribute("class", "ring-used"); return; }
    const f = clamp(left / budget, 0, 1) * 100;
    r.setAttribute("stroke-dasharray", f.toFixed(2) + " 100");
    r.setAttribute("class", "ring-used" + (left < COST_HIGH ? " low" : ""));
  }

  function renderHelper() {
    const left = opsLeft();
    setText("hb-state", S.hbState === null ? "n/a" : S.hbState);
    setText("hb-used", S.opsUsed === null ? "n/a" : S.opsUsed);
    setText("hb-budget", S.opsBudget === null ? "n/a" : S.opsBudget);
    setText("hb-left", left === null ? "n/a" : left);
    setText("vf-left", left === null ? "n/a" : left + " of " + S.opsBudget);
    setText("ov-left", left === null ? "n/a" : left);
    setText("ov-state", S.hbState === null ? "helper: n/a" : "of " + S.opsBudget + ", helper " + S.hbState);
    setRing("ring-ov", left, S.opsBudget);
    setRing("ring-sys", left, S.opsBudget);
    if (S.hbState === null) setPill("conn-helper", "helper: n/a", "s-unknown");
    else setPill("conn-helper", "helper: " + S.hbState, S.hbState === "idle" ? "s-ok" : "s-busy");
  }

  // ---- telemetry -------------------------------------------------------------------------------------------------------------------
  function onTelemetry(ev) {
    S.teleAt = Date.now();
    const v = isObj(ev.values) ? ev.values : {};
    V.last = v;
    const note = $("tele-note");
    if (!isObj(v.cores)) {
      note.textContent = "The sampler sent no per-core values" + (typeof v.status === "string" ? ": " + v.status : ".");
      note.hidden = false;
    } else {
      note.hidden = true;
    }
    const cores = isObj(v.cores) ? v.cores : {};
    const temps = isObj(v.temps) ? v.temps : {};
    const power = isObj(v.power) ? v.power : {};
    const dram = isObj(v.dram) ? v.dram : {};
    syncCoreSet(cores);

    // readouts
    const pk = reading(power.package, 1), cw = reading(power.core, 1), vc = reading(v.vcore, 3);
    setTile("t-pkgw", pk.text, "W", pk.ok, pk.title);
    setTile("t-corew", cw.text, "W", cw.ok, cw.title);
    setTile("t-vcore", vc.text, "V", vc.ok, vc.title);
    const dr = reading(dram.read, 1), dw = reading(dram.write, 1);
    setTile("t-dram", dr.text + " / " + dw.text, "GB/s", dr.ok && dw.ok, dr.ok && dw.ok ? "" : (dr.ok ? dw.title : dr.title));
    const lim = reading(v.limit_reasons_raw);
    setTile("t-limit", lim.ok ? "0x" + lim.value.toString(16).toUpperCase() : "n/a", "", lim.ok, lim.title);
    // highest core clock: derived only from values that were actually read
    let best = null;
    for (const k of Object.keys(cores)) {
      const m = reading(cores[k] && cores[k].mhz);
      if (m.ok && (best === null || m.value > best.value)) best = { value: m.value, cpu: k };
    }
    const anyMhz = Object.keys(cores).map((k) => reading(cores[k] && cores[k].mhz)).find((x) => !x.ok);
    setTile("t-mhz", best ? best.value.toFixed(0) : "n/a", "MHz", best !== null,
      best ? coreLabel(best.cpu) : (anyMhz ? anyMhz.title : "no clock reading"));
    // hottest sensor, same rule
    let hot = null;
    for (const k of Object.keys(temps)) {
      const t = reading(temps[k]);
      if (t.ok && (hot === null || t.value > hot.value)) hot = { value: t.value, label: tempLabel(k, temps[k]) };
    }
    const tFirst = Object.keys(temps).map((k) => reading(temps[k]))[0];
    setTile("t-temp", hot ? hot.value.toFixed(0) : "n/a", "°C", hot !== null, hot ? hot.label : (tFirst ? tFirst.title : "no temperature reading"));

    // history sample
    const tbc = tempsByCpu(temps);
    const smp = { t: S.teleAt, pkg: pk.ok ? pk.value : null, corew: cw.ok ? cw.value : null, vcore: vc.ok ? vc.value : null,
      hot: hot ? hot.value : null, top: best ? best.value : null, dr: dr.ok ? dr.value : null, dw: dw.ok ? dw.value : null, cores: {}, temps: {} };
    for (const k of Object.keys(cores)) {
      const c = isObj(cores[k]) ? cores[k] : {};
      smp.cores[k] = { mhz: num(c.mhz), busy: num(c.busy), vid: num(c.vid), temp: tbc[k] ? num(tbc[k]) : null };
    }
    for (const k of Object.keys(temps)) smp.temps[k] = num(temps[k]);
    V.hist.push(smp);
    while (V.hist.length > HISTORY_MAX || (V.hist.length && V.hist[0].t < S.teleAt - HISTORY_MS - 5000)) V.hist.shift();

    renderAges();
    renderVisible();
  }

  function coreLabel(cpu) {
    const info = V.coreInfo.find((c) => c.cpu === cpu);
    return info ? info.name + " (cpu " + cpu + ")" : "cpu " + cpu;
  }
  function tempLabel(key, t) { return isObj(t) && typeof t.name === "string" && t.name ? t.name : key; }

  // ---- core set: (re)build the map and the table only when the set of cores changes, so focus and hover survive updates -------------
  function syncCoreSet(cores) {
    const keys = sortedKeys(cores);
    const sig = keys.map((k) => k + ":" + (isObj(cores[k]) && typeof cores[k].name === "string" ? cores[k].name : "")).join("|");
    if (sig === V.coreKeys) return;
    V.coreKeys = sig;
    V.coreInfo = keys.map((k) => {
      const name = isObj(cores[k]) && typeof cores[k].name === "string" ? cores[k].name : null;
      const kind = coreKind(k, name);
      return { cpu: k, name: name === null ? "cpu " + k : name, short: shortName(name, kind, k), kind: kind };
    });
    if (V.selected !== null && !V.coreInfo.some((c) => c.cpu === V.selected)) V.selected = null;
    buildDie();
    buildCoreTable();
    renderFilterCounts();
  }

  // ---- core map (Overview hero) ----------------------------------------------------------------------------------------------------
  // Schematic, not a die photo: P-cores in pairs and E-cores in clusters of four, interleaved over two rows.
  const DIE = { P_W: 78, P_H: 128, E: 34, GAP: 10, GROUP_GAP: 18, PAD: 22, ROW_GAP: 22 };
  const tileRefs = {};  // cpu -> {g, rect, val, title}

  function buildDie() {
    const host = $("die-cores");
    for (const k of Object.keys(tileRefs)) delete tileRefs[k];
    const P = V.coreInfo.filter((c) => c.kind === "P"), E = V.coreInfo.filter((c) => c.kind === "E");
    const groups = [];  // alternate a P pair and an E quad
    let pi = 0, ei = 0;
    while (pi < P.length || ei < E.length) {
      if (pi < P.length) { groups.push({ kind: "P", cores: P.slice(pi, pi + 2) }); pi += 2; }
      if (ei < E.length) { groups.push({ kind: "E", cores: E.slice(ei, ei + 4) }); ei += 4; }
    }
    const rows = groups.length > 3 ? 2 : 1;
    const perRow = Math.ceil(groups.length / rows);
    const width = (g) => g.kind === "P" ? g.cores.length * DIE.P_W + (g.cores.length - 1) * DIE.GAP : 2 * DIE.E * 2 + DIE.GAP;
    const nodes = [];
    let maxX = 0;
    for (let r = 0; r < rows; r++) {
      let x = DIE.PAD;
      const y0 = DIE.PAD + r * (DIE.P_H + DIE.ROW_GAP);
      for (const g of groups.slice(r * perRow, (r + 1) * perRow)) {
        if (g.kind === "E") {
          nodes.push(sv("rect", { x: x - 6, y: y0 - 6, width: width(g) + 12, height: DIE.P_H + 12, rx: 10 }, "cluster"));
        }
        g.cores.forEach((c, i) => {
          let tx, ty, tw, th;
          if (g.kind === "P") { tx = x + i * (DIE.P_W + DIE.GAP); ty = y0; tw = DIE.P_W; th = DIE.P_H; }
          else { tx = x + (i % 2) * (2 * DIE.E + DIE.GAP); ty = y0 + Math.floor(i / 2) * ((DIE.P_H + DIE.GAP) / 2); tw = 2 * DIE.E; th = (DIE.P_H - DIE.GAP) / 2; }
          nodes.push(buildTile(c, tx, ty, tw, th));
        });
        x += width(g) + DIE.GROUP_GAP;
        maxX = Math.max(maxX, x);
      }
    }
    const h = DIE.PAD * 2 + rows * DIE.P_H + (rows - 1) * DIE.ROW_GAP;
    $("die").setAttribute("viewBox", "0 0 " + Math.max(maxX - DIE.GROUP_GAP + DIE.PAD, 200) + " " + h);
    if (!nodes.length) nodes.push(sv("text", { x: 24, y: 48 }, "die-empty"));
    fill(host, nodes);
    if (!V.coreInfo.length) host.lastChild.textContent = "No per-core values yet.";
  }

  function buildTile(c, x, y, w, h) {
    const g = sv("g", { tabindex: 0, role: "button" }, "tile tile-" + c.kind);
    const title = sv("title");
    const rect = sv("rect", { x: x, y: y, width: w, height: h, rx: c.kind === "P" ? 8 : 5, fill: "url(#na-hatch)" }, "tile-face");
    const name = sv("text", { x: x + 8, y: y + 17 }, "tile-name");
    name.textContent = c.short;
    const val = sv("text", { x: x + w / 2, y: y + h / 2 + (c.kind === "P" ? 14 : 10), "text-anchor": "middle" }, "tile-val tile-val-" + c.kind);
    val.textContent = "n/a";
    g.append(title, rect, name, val);
    const open = () => { V.selected = c.cpu; selectTab("cores", false); renderCoreSelection(); };
    g.addEventListener("click", open);
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    g.addEventListener("pointerenter", () => { V.probing = true; probe(c); });
    g.addEventListener("focus", () => { V.probing = true; probe(c); });
    g.addEventListener("pointerleave", () => { V.probing = false; });
    g.addEventListener("blur", () => { V.probing = false; });
    tileRefs[c.cpu] = { g: g, rect: rect, val: val, name: name, title: title };
    return g;
  }

  function fmtReading(r, digits, unit) { const x = reading(r, digits); return x.ok ? x.text + " " + unit : "n/a (" + x.title + ")"; }

  function probe(c) {
    const cpu = c.cpu;
    setText("probe", c.name + ", cpu " + cpu + ": clock " + fmtReading(coreReading(cpu, "mhz"), 0, "MHz") + "; busy " +
      fmtReading(coreReading(cpu, "busy"), 0, "%") + "; VID " + fmtReading(coreReading(cpu, "vid"), 3, "V") + "; temp " +
      fmtReading(coreReading(cpu, "temp"), 0, "°C") + ".");
  }

  function renderDie() {
    const m = METRICS[V.metric];
    for (const c of V.coreInfo) {
      const ref = tileRefs[c.cpu];
      if (!ref) continue;
      const x = reading(coreReading(c.cpu, V.metric), m.digits);
      if (x.ok) {
        const col = rampAt(m.ramp, (x.value - m.lo) / (m.hi - m.lo));
        ref.rect.setAttribute("fill", col.hex);
        ref.g.setAttribute("class", "tile tile-" + c.kind + (col.light ? " ink-dark" : ""));
        ref.val.textContent = c.kind === "P" ? x.text : (m.digits === 3 ? x.value.toFixed(2) : x.text);
        ref.title.textContent = c.name + " (cpu " + c.cpu + "): " + x.text + " " + m.unit;
        ref.g.setAttribute("aria-label", c.name + ", " + m.label + " " + x.text + " " + m.unit + ". Open in Cores.");
      } else {
        ref.rect.setAttribute("fill", "url(#na-hatch)");
        ref.g.setAttribute("class", "tile tile-" + c.kind + " tile-na");
        ref.val.textContent = "n/a";
        ref.title.textContent = c.name + " (cpu " + c.cpu + "): n/a, " + x.title;
        ref.g.setAttribute("aria-label", c.name + ", " + m.label + " not available: " + x.title + ". Open in Cores.");
      }
    }
    if (!V.probing) {
      const reasons = V.coreInfo.map((c) => reading(coreReading(c.cpu, V.metric))).filter((x) => !x.ok).map((x) => x.title);
      setText("probe", V.coreInfo.length && reasons.length === V.coreInfo.length
        ? m.label + " is n/a on every core: " + reasons[0] + "."
        : "Point at a core for its readings. Click to open it in Cores.");
    }
    setText("ramp-lo", m.digits === 3 ? m.lo.toFixed(1) : m.lo);
    setText("ramp-hi", m.digits === 3 ? m.hi.toFixed(1) : m.hi);
    setText("ramp-unit", m.unit + " (display scale)");
  }

  function buildRamp() {
    const m = METRICS[V.metric];
    const stops = [];
    for (let i = 0; i < 40; i++) stops.push(sv("rect", { x: i * 5, y: 0, width: 5.4, height: 10, fill: rampAt(m.ramp, i / 39).hex }));
    fill($("ramp-stops"), stops);
  }

  // ---- charts (SVG, fixed viewBox, non-scaling strokes; hover moves a crosshair and rewrites the readout) ---------------------------
  const CW = 600, CH = 150;
  const charts = {};

  // cfg: {title, unit, digits, series: [{label, get(sample) -> number|null, cls}], reason() -> string, zero?: bool}
  function makeChart(id, cfg) {
    const fig = $(id);
    fig.replaceChildren();
    const head = el("div", "ch-head");
    const title = el("h3", "ch-title", cfg.title);
    const read = el("span", "ch-read", "n/a");
    head.append(title, read);
    fig.append(head);
    if (cfg.series.length > 1) {
      const lg = el("div", "legend");
      for (const s of cfg.series) { const k = el("span", "lg-item"); k.append(el("span", "lg-swatch " + s.cls), document.createTextNode(s.label)); lg.append(k); }
      fig.append(lg);
    }
    const body = el("div", "ch-body");
    const yax = el("div", "ch-y");
    const yHi = el("span", "", ""), yLo = el("span", "", "");
    yax.append(yHi, yLo);
    const svg = sv("svg", { viewBox: "0 0 " + CW + " " + CH, preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" }, "ch-svg");
    const grid = sv("g", null, "ch-grid");
    for (let i = 0; i <= 4; i++) grid.append(sv("line", { x1: 0, x2: CW, y1: (i * CH) / 4, y2: (i * CH) / 4, "vector-effect": "non-scaling-stroke" }));
    const paths = cfg.series.map((s) => sv("path", { d: "", "vector-effect": "non-scaling-stroke" }, "ch-line " + s.cls));
    const cross = sv("line", { x1: -10, x2: -10, y1: 0, y2: CH, "vector-effect": "non-scaling-stroke" }, "ch-cross");
    svg.append(grid, ...paths, cross);
    const empty = el("p", "ch-empty", "");
    body.append(yax, svg, empty);
    const xax = el("div", "ch-x");
    const xLabel = el("span", "", "1 min ago");
    xax.append(xLabel, el("span", "", "now"));
    fig.append(body, xax);
    const ch = { cfg: cfg, read: read, yHi: yHi, yLo: yLo, svg: svg, paths: paths, cross: cross, empty: empty, hover: null, lo: 0, hi: 1, t0: 0, span: 60000, xLabel: xLabel };
    svg.addEventListener("pointermove", (e) => {
      const b = svg.getBoundingClientRect();
      if (b.width > 0) { ch.hover = clamp((e.clientX - b.left) / b.width, 0, 1); paintReadout(ch); }
    });
    svg.addEventListener("pointerleave", () => { ch.hover = null; paintReadout(ch); });
    charts[id] = ch;
    return ch;
  }

  function niceRange(lo, hi, zero) {
    if (zero) lo = Math.min(lo, 0);
    if (hi - lo < 1e-9) { const d = Math.abs(hi) > 1 ? Math.abs(hi) * 0.05 : 0.05; lo -= d; hi += d; }
    const pad = (hi - lo) * 0.08;
    return [zero && lo >= 0 ? lo : lo - pad, hi + pad];
  }

  function fmtVal(v, digits) { return v.toFixed(digits); }

  // The window grows with the history: 1 min at first, up to 5 min, so a fresh page is not a sliver at the right edge.
  function windowMs() {
    if (!V.hist.length) return 60000;
    return clamp(Math.ceil((Date.now() - V.hist[0].t) / 60000) * 60000, 60000, HISTORY_MS);
  }

  function renderChart(ch) {
    const cfg = ch.cfg, now = Date.now(), span = windowMs(), t0 = now - span;
    ch.t0 = t0; ch.span = span;
    ch.xLabel.textContent = Math.round(span / 60000) + " min ago";
    const pts = V.hist.filter((s) => s.t >= t0);
    let lo = Infinity, hi = -Infinity;
    for (const s of pts) for (const se of cfg.series) { const v = se.get(s); if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    const has = lo <= hi;
    ch.empty.hidden = has;
    ch.svg.setAttribute("class", "ch-svg" + (has ? "" : " is-empty"));
    if (!has) {
      ch.empty.textContent = "n/a: " + cfg.reason();
      ch.yHi.textContent = ""; ch.yLo.textContent = "";
      for (const p of ch.paths) p.setAttribute("d", "");
      ch.read.textContent = "n/a";
      ch.read.title = cfg.reason();
      return;
    }
    const r = cfg.range ? [Math.min(cfg.range[0], lo), Math.max(cfg.range[1], hi)] : niceRange(lo, hi, cfg.zero === true);
    ch.lo = r[0]; ch.hi = r[1];
    ch.yHi.textContent = fmtVal(ch.hi, cfg.digits);
    ch.yLo.textContent = fmtVal(ch.lo, cfg.digits);
    cfg.series.forEach((se, i) => {
      let d = "", prevT = null;
      for (const s of pts) {
        const v = se.get(s);
        if (!isNum(v)) { prevT = null; continue; }
        const x = ((s.t - t0) / span) * CW, y = CH - ((v - ch.lo) / (ch.hi - ch.lo)) * CH;
        d += (prevT === null || s.t - prevT > 3500 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
        prevT = s.t;
      }
      ch.paths[i].setAttribute("d", d);
    });
    paintReadout(ch);
  }

  function paintReadout(ch) {
    const cfg = ch.cfg;
    if (!V.hist.length) return;
    let s = V.hist[V.hist.length - 1], ago = null;
    if (ch.hover !== null) {
      const t = ch.t0 + ch.hover * ch.span;
      let best = null;
      for (const x of V.hist) if (best === null || Math.abs(x.t - t) < Math.abs(best.t - t)) best = x;
      if (best && Math.abs(best.t - t) < 4000) { s = best; ago = Math.round((Date.now() - best.t) / 1000); }
      const cx = ((s.t - ch.t0) / ch.span) * CW;
      ch.cross.setAttribute("x1", cx.toFixed(1)); ch.cross.setAttribute("x2", cx.toFixed(1));
    } else {
      ch.cross.setAttribute("x1", -10); ch.cross.setAttribute("x2", -10);
    }
    const parts = cfg.series.map((se) => {
      const v = se.get(s);
      return (cfg.series.length > 1 ? se.label + " " : "") + (isNum(v) ? fmtVal(v, cfg.digits) + " " + cfg.unit : "n/a");
    });
    ch.read.textContent = (ago !== null ? ago + " s ago: " : "") + parts.join(", ");
    ch.read.title = "";
  }

  function sparkline(svgId, getter, digits) {
    const svg = $(svgId);
    const now = Date.now(), t0 = now - SPARK_MS;
    const pts = V.hist.filter((s) => s.t >= t0);
    let lo = Infinity, hi = -Infinity;
    for (const s of pts) { const v = getter(s); if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    if (!(lo <= hi)) { svg.replaceChildren(); return; }
    const r = niceRange(lo, hi, false);
    let d = "", prevT = null;
    for (const s of pts) {
      const v = getter(s);
      if (!isNum(v)) { prevT = null; continue; }
      const x = ((s.t - t0) / SPARK_MS) * 120, y = 27 - ((v - r[0]) / (r[1] - r[0])) * 26;
      d += (prevT === null || s.t - prevT > 3500 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
      prevT = s.t;
    }
    fill(svg, [sv("path", { d: d, "vector-effect": "non-scaling-stroke" }, "spark-line")]);
    svg.setAttribute("aria-label", "last 60 s, " + lo.toFixed(digits) + " to " + hi.toFixed(digits));
  }

  // chart definitions; reasons always come from the latest reading so an empty chart says why
  function rsn(get) { return () => { const x = reading(get()); return x.ok ? "no samples in the window yet" : x.title; }; }
  const lastPower = (k) => () => (isObj(V.last.power) ? V.last.power[k] : undefined);
  const lastDram = (k) => () => (isObj(V.last.dram) ? V.last.dram[k] : undefined);
  function hotReason() {
    const temps = isObj(V.last.temps) ? V.last.temps : {};
    const k = Object.keys(temps)[0];
    return k === undefined ? "no temperature sensors in the sample" : reading(temps[k]).title;
  }

  function initCharts() {
    makeChart("ch-ov-power", { title: "Package power", unit: "W", digits: 1,
      series: [{ label: "Package", get: (s) => s.pkg, cls: "s1" }], reason: rsn(lastPower("package")) });
    makeChart("ch-ov-temp", { title: "Hottest temperature", unit: "°C", digits: 0,
      series: [{ label: "Hottest", get: (s) => s.hot, cls: "s2" }], reason: hotReason });
    makeChart("ch-power", { title: "Power", unit: "W", digits: 1,
      series: [{ label: "Package", get: (s) => s.pkg, cls: "s1" }, { label: "Core", get: (s) => s.corew, cls: "s2" }], reason: rsn(lastPower("package")) });
    makeChart("ch-hot", { title: "Hottest temperature", unit: "°C", digits: 0,
      series: [{ label: "Hottest", get: (s) => s.hot, cls: "s2" }], reason: hotReason });
    makeChart("ch-vcore", { title: "Vcore (board sensor)", unit: "V", digits: 3,
      series: [{ label: "Vcore", get: (s) => s.vcore, cls: "s1" }], reason: rsn(() => V.last.vcore) });
    makeChart("ch-dram", { title: "DRAM bandwidth", unit: "GB/s", digits: 1,
      series: [{ label: "Read", get: (s) => s.dr, cls: "s1" }, { label: "Write", get: (s) => s.dw, cls: "s2" }], reason: rsn(lastDram("read")) });
    const cd = (k) => (s) => (V.selected !== null && isObj(s.cores[V.selected]) ? s.cores[V.selected][k] : null);
    const cdr = (k) => () => coreReading(V.selected, k);
    makeChart("ch-cd-mhz", { title: "Clock", unit: "MHz", digits: 0, series: [{ label: "Clock", get: cd("mhz"), cls: "s1" }], reason: rsn(cdr("mhz")) });
    makeChart("ch-cd-busy", { title: "Busy", unit: "%", digits: 1, range: [0, 100], series: [{ label: "Busy", get: cd("busy"), cls: "s1" }], reason: rsn(cdr("busy")) });
    makeChart("ch-cd-vid", { title: "VID", unit: "V", digits: 3, series: [{ label: "VID", get: cd("vid"), cls: "s1" }], reason: rsn(cdr("vid")) });
    makeChart("ch-cd-temp", { title: "Temperature", unit: "°C", digits: 0, series: [{ label: "Temp", get: cd("temp"), cls: "s2" }], reason: rsn(cdr("temp")) });
  }

  // ---- Cores tab -------------------------------------------------------------------------------------------------------------------
  const rowRefs = {};  // cpu -> {tr, cells..., spark, bar}

  function buildCoreTable() {
    for (const k of Object.keys(rowRefs)) delete rowRefs[k];
    if (!V.coreInfo.length) {
      const row = el("tr"), cell = td("n/a (no per-core values)", "na");
      cell.colSpan = 8;
      row.append(cell);
      fill($("core-body"), [row]);
      return;
    }
    fill($("core-body"), V.coreInfo.map((c) => {
      const tr = el("tr", "core-row k-" + c.kind);
      tr.tabIndex = 0;
      const nameCell = el("td", "core-name");
      nameCell.append(el("span", "kind-dot k-" + c.kind), document.createTextNode(c.name));
      const cpuCell = td(c.cpu, "n");
      const mhzCell = el("td", "bar-cell");
      const bar = sv("svg", { viewBox: "0 0 100 8", preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" }, "hbar");
      const barTrack = sv("rect", { x: 0, y: 0, width: 100, height: 8, rx: 2 }, "bar-track");
      const barFill = sv("rect", { x: 0, y: 0, width: 0, height: 8, rx: 2 }, "bar-fill");
      bar.append(barTrack, barFill);
      const mhzNum = el("span", "bar-num", "n/a");
      mhzCell.append(mhzNum, bar);
      const busy = td("n/a", "n na"), ratio = td("n/a", "n na"), vid = td("n/a", "n na"), temp = td("n/a", "n na");
      const sparkCell = el("td", "spark-cell");
      const spark = sv("svg", { viewBox: "0 0 120 28", preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" }, "spark");
      sparkCell.append(spark);
      tr.append(nameCell, cpuCell, mhzCell, busy, ratio, vid, temp, sparkCell);
      const pick = () => { V.selected = c.cpu; renderCoreSelection(); };
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
      rowRefs[c.cpu] = { tr: tr, mhzNum: mhzNum, mhzCell: mhzCell, barFill: barFill, busy: busy, ratio: ratio, vid: vid, temp: temp, spark: spark };
      return tr;
    }));
    applyFilter();
  }

  function setCell(cell, r, digits) {
    const x = reading(r, digits);
    cell.textContent = x.text;
    cell.className = x.ok ? "n" : "n na";
    cell.title = x.title;
  }

  function renderCoreRows() {
    const m = METRICS.mhz;
    for (const c of V.coreInfo) {
      const ref = rowRefs[c.cpu];
      if (!ref) continue;
      const x = reading(coreReading(c.cpu, "mhz"), 0);
      ref.mhzNum.textContent = x.text;
      ref.mhzNum.className = "bar-num" + (x.ok ? "" : " na");
      ref.mhzCell.title = x.title;
      ref.barFill.setAttribute("width", x.ok ? (clamp((x.value - 0) / m.hi, 0, 1) * 100).toFixed(1) : 0);
      setCell(ref.busy, coreReading(c.cpu, "busy"), 1);
      setCell(ref.ratio, coreReading(c.cpu, "ratio"), 0);
      setCell(ref.vid, coreReading(c.cpu, "vid"), 3);
      setCell(ref.temp, coreReading(c.cpu, "temp"), 0);
      if (!ref.spark.id) ref.spark.id = "sp-core-" + c.cpu;
      sparkline(ref.spark.id, (s) => (isObj(s.cores[c.cpu]) ? s.cores[c.cpu].mhz : null), 0);
      ref.tr.setAttribute("aria-selected", V.selected === c.cpu ? "true" : "false");
    }
  }

  function renderFilterCounts() {
    const nP = V.coreInfo.filter((c) => c.kind === "P").length, nE = V.coreInfo.length - nP;
    segmented($("core-filter"), [
      { value: "all", label: "All " + V.coreInfo.length }, { value: "P", label: "P-cores " + nP }, { value: "E", label: "E-cores " + nE },
    ], V.filter, (f) => { V.filter = f; applyFilter(); });
  }

  function applyFilter() {
    for (const c of V.coreInfo) {
      const ref = rowRefs[c.cpu];
      if (ref) ref.tr.hidden = V.filter !== "all" && c.kind !== V.filter;
    }
  }

  function renderCoreSelection() {
    const c = V.coreInfo.find((x) => x.cpu === V.selected);
    $("cd-charts").hidden = !c;
    $("cd-empty").hidden = Boolean(c);
    setText("cd-title", c ? c.name : "No core selected");
    setText("cd-sub", c ? "cpu " + c.cpu + ", " + (c.kind === "P" ? "performance core" : "efficiency core") + ", last 5 min" : "");
    for (const k of Object.keys(rowRefs)) rowRefs[k].tr.setAttribute("aria-selected", k === V.selected ? "true" : "false");
    if (c && V.tab === "cores") {
      for (const id of ["ch-cd-mhz", "ch-cd-busy", "ch-cd-vid", "ch-cd-temp"]) renderChart(charts[id]);
      if (rowRefs[c.cpu]) rowRefs[c.cpu].tr.scrollIntoView({ block: "nearest" });
    }
  }

  // ---- Sensors tab: temperature list -------------------------------------------------------------------------------------------------
  const TEMP_LO = 20, TEMP_HI = 110;  // bar display scale only

  function renderTemps() {
    const temps = isObj(V.last.temps) ? V.last.temps : {};
    const keys = Object.keys(temps).sort((a, b) => {
      const ca = cpuOf(temps[a]), cb = cpuOf(temps[b]);  // package / unmapped sensors first, then by CPU
      const na = ca === null ? -1 : Number(ca), nb = cb === null ? -1 : Number(cb);
      if (na !== nb) return na - nb;
      return tempLabel(a, temps[a]).localeCompare(tempLabel(b, temps[b]), undefined, { numeric: true });
    });
    setText("temp-count", keys.length ? keys.length + (keys.length === 1 ? " sensor" : " sensors") : "");
    if (!keys.length) { fill($("temp-body"), [el("li", "na", "n/a: no temperature sensors in the sample")]); return; }
    fill($("temp-body"), keys.map((k, i) => {
      const t = temps[k];
      const x = reading(t, 0);
      const li = el("li", "temp" + (x.ok ? "" : " is-na"));
      const name = el("span", "temp-name", tempLabel(k, t));
      name.title = (cpuOf(t) !== null ? "cpu " + cpuOf(t) + ", " : "") + "sensor " + k;
      const val = el("span", "temp-val" + (x.ok ? "" : " na"), x.ok ? x.text + " °C" : "n/a");
      val.title = x.title;
      const bar = sv("svg", { viewBox: "0 0 100 6", preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" }, "hbar temp-bar");
      bar.append(sv("rect", { x: 0, y: 0, width: 100, height: 6, rx: 2 }, "bar-track"));
      if (x.ok) {
        const f = clamp((x.value - TEMP_LO) / (TEMP_HI - TEMP_LO), 0, 1);
        bar.append(sv("rect", { x: 0, y: 0, width: (f * 100).toFixed(1), height: 6, rx: 2, fill: rampAt(METRICS.temp.ramp, 0.35 + f * 0.65).hex }));
      }
      const spark = sv("svg", { viewBox: "0 0 120 28", preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false", id: "sp-temp-" + i }, "spark");
      li.append(name, val, bar, spark);
      if (!x.ok) li.append(el("span", "temp-why", x.title));
      return li;
    }));
    keys.forEach((k, i) => sparkline("sp-temp-" + i, (s) => (s.temps[k] === undefined ? null : s.temps[k]), 0));
  }

  // ---- render scheduling: only the visible tab draws ---------------------------------------------------------------------------------
  function renderVisible() {
    if (V.tab === "overview") {
      renderDie();
      renderChart(charts["ch-ov-power"]);
      renderChart(charts["ch-ov-temp"]);
      sparkline("sp-pkgw", (s) => s.pkg, 1);
      sparkline("sp-corew", (s) => s.corew, 1);
      sparkline("sp-vcore", (s) => s.vcore, 3);
      sparkline("sp-temp", (s) => s.hot, 0);
      sparkline("sp-mhz", (s) => s.top, 0);
      sparkline("sp-dram", (s) => (isNum(s.dr) && isNum(s.dw) ? s.dr + s.dw : null), 1);
    } else if (V.tab === "cores") {
      renderCoreRows();
      renderCoreSelection();
    } else if (V.tab === "sensors") {
      renderTemps();
      for (const id of ["ch-power", "ch-hot", "ch-vcore", "ch-dram"]) renderChart(charts[id]);
    }
  }

  // ---- ages / staleness (1 Hz) ------------------------------------------------------------------------------------------------------
  function renderAges() {
    const now = Date.now();
    const ta = $("tele-age");
    if (!S.teleAt) { ta.textContent = "no sample yet"; ta.className = "age muted"; }
    else {
      const age = (now - S.teleAt) / 1000;
      const stale = now - S.teleAt > TELEMETRY_STALE_MS;
      ta.textContent = stale ? "stale: last sample " + age.toFixed(0) + " s ago" : "live, 1 sample per second";
      ta.className = stale ? "age stale" : "age live";
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
    setText("vf-reason", reason === "" ? "Ready. Uses " + COST_LOW + " to " + COST_HIGH + " of the " + opsLeft() + " ops left." : reason);
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
    const curves = [];
    for (const u of Array.isArray(r.units) ? r.units : []) {
      for (const c of Array.isArray(u && u.cpus) ? u.cpus : []) {
        const row = el("tr");
        const pts = Array.isArray(c.points) ? c.points.map(fmtPoint).join(", ") : "n/a";
        const name = typeof c.name === "string" ? c.name : u.name;
        row.append(td(c.cpu, "n"), td(name), td(c.oc ? fmtPoint(c.oc) : "n/a", c.oc ? "" : "na"), td(pts, "mono"));
        rows.push(row);
        curves.push({ cpu: c.cpu, name: String(name), kind: coreKind(c.cpu, typeof name === "string" ? name : null), points: Array.isArray(c.points) ? c.points : [] });
      }
    }
    fill($("vf-body"), rows);
    $("vf-table").hidden = rows.length === 0;
    $("vf-table-empty").hidden = rows.length !== 0;
    renderVfPlot(curves, r.simulator === true);
  }

  function vfPlotPlaceholder() {
    const t = sv("text", { x: 320, y: 150, "text-anchor": "middle" }, "plot-empty");
    t.textContent = "After a read, each core's offset per V/F point is drawn here.";
    fill($("vf-plot"), [t]);
  }

  // V/F diagram: offset (mV) per V/F point index, one line per core; P-cores and E-cores in the two series colours.
  function renderVfPlot(curves, simulated) {
    const svg = $("vf-plot");
    const W = 640, H = 300, L = 56, R = 16, T = 16, B = 40;
    const all = [];
    let nMax = 0;
    for (const c of curves) {
      c.xy = c.points.filter((p) => isObj(p) && isNum(p.offset_mv)).map((p, i) => ({ i: isNum(p.point) ? p.point : i + 1, mv: p.offset_mv, ratio: p.ratio }));
      for (const p of c.xy) { all.push(p.mv); nMax = Math.max(nMax, p.i); }
    }
    if (!all.length) {
      fill(svg, [sv("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, "plot-empty")]);
      svg.lastChild.textContent = "The read returned no V/F points with offsets.";
      setText("vf-plot-note", "no points");
      fill($("vf-legend"), []);
      return;
    }
    let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
    if (hi - lo < 10) { hi += 5; lo -= 5; }
    const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / 2)));
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    const x = (i) => L + ((i - 1) / Math.max(nMax - 1, 1)) * (W - L - R);
    const y = (mv) => T + ((hi - mv) / (hi - lo)) * (H - T - B);
    const nodes = [];
    for (let v = lo; v <= hi + 1e-9; v += step) {
      nodes.push(sv("line", { x1: L, x2: W - R, y1: y(v), y2: y(v) }, v === 0 ? "plot-zero" : "plot-grid"));
      const t = sv("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end" }, "plot-tick");
      t.textContent = (v > 0 ? "+" : "") + v.toFixed(0);
      nodes.push(t);
    }
    for (let i = 1; i <= nMax; i++) {
      const t = sv("text", { x: x(i), y: H - B + 18, "text-anchor": "middle" }, "plot-tick");
      t.textContent = String(i);
      nodes.push(t);
    }
    const xl = sv("text", { x: (L + W - R) / 2, y: H - 4, "text-anchor": "middle" }, "plot-axis");
    xl.textContent = "V/F point";
    const yl = sv("text", { x: 14, y: T + (H - T - B) / 2, "text-anchor": "middle", transform: "rotate(-90 14 " + (T + (H - T - B) / 2) + ")" }, "plot-axis");
    yl.textContent = "offset (mV)";
    nodes.push(xl, yl);
    for (const c of curves) {
      if (!c.xy.length) continue;
      const g = sv("g", { tabindex: 0 }, "vf-curve " + (c.kind === "P" ? "s1" : "s2"));
      const title = sv("title");
      title.textContent = c.name + " (cpu " + c.cpu + "): " + c.xy.map((p) => "pt " + p.i + " " + (p.mv > 0 ? "+" : "") + p.mv.toFixed(1) + " mV").join(", ");
      const d = c.xy.map((p, k) => (k ? "L" : "M") + x(p.i).toFixed(1) + " " + y(p.mv).toFixed(1)).join("");
      g.append(title, sv("path", { d: d }, "vf-line"));
      for (const p of c.xy) g.append(sv("circle", { cx: x(p.i), cy: y(p.mv), r: 4 }, "vf-dot"));
      const show = () => setText("vf-plot-note", title.textContent);
      g.addEventListener("pointerenter", show);
      g.addEventListener("focus", show);
      nodes.push(g);
    }
    fill(svg, nodes);
    setText("vf-plot-note", curves.length + (curves.length === 1 ? " core" : " cores") + (simulated ? " (simulator data)" : "") + ". Point at a line for its values.");
    const lg = [];
    for (const k of ["P", "E"]) {
      if (!curves.some((c) => c.kind === k)) continue;
      const it = el("span", "lg-item");
      it.append(el("span", "lg-swatch " + (k === "P" ? "s1" : "s2")), document.createTextNode(k === "P" ? "P-cores" : "E-cores"));
      lg.push(it);
    }
    fill($("vf-legend"), lg);
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
    note.textContent = recs.length ? "Last " + recs.length + " records, newest first" + (body.result.truncated ? " (older records not shown)." : ".") : "The audit log is empty.";
    fill($("audit-body"), recs.map((r) => {
      const o = isObj(r) ? r : { unparsable: String(r) };
      const row = el("tr");
      const rc = o.rc_name !== undefined ? o.rc_name : (o.rc !== undefined ? o.rc : "");
      const rcCell = el("td");
      if (rc !== "") rcCell.append(pill(rc, rc === "OK" || rc === 0 ? "s-ok" : "s-neutral"));
      row.append(td(o.ts, "mono"), td(o.seq, "n"), td(o.event !== undefined ? o.event : (o.unparsable !== undefined ? "unparsable" : "")),
        td(o.cpu), td(hex(o.cmd), "mono"), rcCell, td(o.reason !== undefined ? o.reason : ""));
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
  for (const t of TABS) $("tab-" + t).addEventListener("click", () => selectTab(t, false));
  document.querySelector("[role=tablist]").addEventListener("keydown", onTabKey);
  for (const b of document.querySelectorAll("[data-goto]")) b.addEventListener("click", () => selectTab(b.getAttribute("data-goto"), true));
  segmented($("map-metric"), Object.keys(METRICS).map((k) => ({ value: k, label: METRICS[k].label })), V.metric,
    (k) => { V.metric = k; buildRamp(); renderDie(); });
  buildRamp();
  initCharts();
  vfPlotPlaceholder();
  syncCoreSet({});
  $("btn-preflight").addEventListener("click", loadPreflight);
  $("btn-audit").addEventListener("click", loadAudit);
  $("btn-dump").addEventListener("click", startDump);
  $("btn-cancel").addEventListener("click", cancelDump);
  setInterval(() => { renderAges(); updateGate(); }, 1000);
  selectTab(location.hash.slice(1), false);
  renderHelper();
  updateGate();
  connect();
})();
