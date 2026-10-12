"use strict";
// arrowlake-tuner web UI (G-105, redesigned in G-110, SPEC-v3 pages in G-113): Dashboard, Curves, Tune, Stress, Profiles, System, Audit.
// The only writes this page can start are in Tune: tune.plan opens a review dialog, and tune.apply is sent from that dialog's Apply button (D-033 d: one
// click, no typing), carrying the plan id and "CONFIRM <nonce>" of that plan. Everything else is a read, a cpufreq-only measurement, or curve/profile bookkeeping.
// All data is written with textContent; no HTML strings are ever parsed and there is no inline style: geometry and colour go through SVG attributes
// and classes only (CSP style-src 'self'). Contracts 8 and 9: docs/ARCHITECTURE.md.
(function () {
  const HEARTBEAT_STALE_MS = 7000;  // the helper beats every 2 s
  const EVENTS_URL = "/api/events";
  const HISTORY_MS = 900000;        // client-side history window: 15 min (wall-clock time, not sample counts)
  const HISTORY_MAX = 7000;         // hard cap on the ring buffer (0.25 s sampling x 15 min = 3600, with slack)
  const SPARK_MS = 60000;           // per-core and readout sparklines: last 60 s
  const REFRESH_STEPS = [0.25, 0.5, 1, 2, 5];   // seconds (SPEC-v3 section 2)
  const PREFS_KEY = "arltuner.prefs";

  const $ = (id) => document.getElementById(id);
  const SVGNS = $("die").namespaceURI;  // taken from the page so no namespace URL literal is needed
  const S = {
    sseUp: false,
    mode: null,            // "write" | "read-only" | "refuse" | null (unknown)
    writesAllowed: null,   // boolean | null
    hbState: null, hbOpId: null, opsUsed: null, opsBudget: null, hbAt: 0,
    opsHist: [],           // [{t, v}]: mailbox operations used this boot, noted whenever the heartbeat's count changes (the session sparkline)
    lastTried: false,      // tune.last answered once (a reconnect must not wipe the editor)
    teleAt: 0,
    intervalS: 1,          // the sampler interval this page asked for
    opId: null,            // request id of the long op (tune.read, vf.measure, tune.apply) this page started, or adopted after a reload
    opKind: null,          // "read" | "measure" | "commit" (what opId is)
    adopted: false,        // opId came from a heartbeat/progress event, not from this page's own POST
    doneIds: new Set(),    // ids whose result has been shown (a late heartbeat must not re-adopt them)
    submitting: false,
    cancelling: false,
    sessionLost: false,
  };
  // View state (client only; nothing here is sent anywhere).
  const V = {
    tab: "dashboard",
    sub: "overview",       // Dashboard sub-view: overview | cores | sensors
    hist: [],              // ring buffer of samples {t, pkg, corew, vcore, hot, top, dr, dw, cores: {cpu: {mhz, busy, vid, temp}}, temps: {key: C}}
    last: {},              // latest telemetry values (for n/a reasons)
    coreKeys: "",          // signature of the current core set (rebuild map/table when it changes)
    coreInfo: [],          // [{cpu, name, short, kind}]
    selected: null,        // selected cpu key (Cores view)
    metric: "mhz",         // core-map colouring
    scaleMode: "fixed",    // core-map colour scale: fixed | auto
    scaleLock: null,       // {lo, hi} while the automatic range is frozen
    filter: "all",         // Cores view filter
    sortKey: null, sortDir: 1,
    cols: { busy: true, ratio: true, vid: true, temp: true, spark: true },
    winMin: 1,             // chart window in minutes: 1 | 5 | 15
    paused: false,         // live drawing paused (samples still collected)
    probing: false,        // a core-map tile is hovered or focused (its readings are in the probe line)
  };

  // Remembered per browser: only the refresh interval and the chart window (nothing sensitive). Storage may be blocked; the page works without it.
  function loadPrefs() {
    try { const o = JSON.parse(window.localStorage.getItem(PREFS_KEY)); return isObj(o) ? o : {}; } catch (e) { return {}; }
  }
  function savePrefs() {
    try { window.localStorage.setItem(PREFS_KEY, JSON.stringify({ refresh_s: S.intervalS, win_min: V.winMin })); } catch (e) { /* blocked: fine */ }
  }

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
  function isStr(v) { return typeof v === "string" && v !== ""; }
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

  // detail.check values of the measure / curves / profiles ops (G-111), in words
  const CHECK_TEXT = {
    NO_CPUFREQ: "the system exposes no cpufreq controls, so the cores cannot be capped",
    LOAD_GENERATOR: "the load generator could not be started (stress-ng missing or refused)",
    MEASURE_UNAVAILABLE: "measuring is not available right now (see Preflight in System)",
    JOURNAL_UNAVAILABLE: "the cpufreq restore journal cannot be written, so nothing was changed",
    CURVE_LIMIT: "the curve store is full: delete an old curve in the Library first",
    EXISTS: "that name is already taken",
    NO_SUCH_CURVE: "there is no stored curve with that id",
    POLICY_RANGE: "a value is outside the policy range",
    NO_READ: "this helper session has not read the ratios and voltages yet: use Read ratios & voltages first",
    NOTHING_TO_DO: "nothing would change",
    NO_ACCEPTED_STATE: "no accepted state is installed",
    STEP_LIMIT: "the plan needs more steps in one go than the policy allows: change fewer units at once",
    SESSION_LIMIT: "the plan needs more mailbox operations than one session may use",
    BOOT_LIMIT: "the per-boot mailbox budget does not fit this plan",
    NO_INVOKER: "the helper does not know which user to run the tool as (it must be started from the launcher)",
    TOOL_MISSING: "the load tool is not installed (stress-ng for the CPU, stressapptest for the memory)",
    TRUST_PATH: "the load tool is not in a root-owned place, so the helper refused to run it",
    MEM_RANGE: "that much memory is not free: pick less, or leave it on automatic",
    STALE_JOURNAL: "an earlier run left cpufreq limits that could not be put back; nothing was started",
    CAP_WRITE: "the ratio cap could not be written to cpufreq, so nothing was started",
    SPAWN: "the load tool could not be started",
    RESTORE_FAILED: "the cpufreq limits could not be put back after the run: check System before doing anything else",
    TURBO_ORDER: "turbo ratios may only fall as more cores are active: a bucket cannot be higher than the one to its left (fewer active cores)",
  };

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
        if (typeof d.check === "string" && CHECK_TEXT[d.check]) t += ". In words: " + CHECK_TEXT[d.check] + ".";
        else if (typeof d.check === "string" && d.check.indexOf("PROFILE_") === 0) t += ". In words: the profile store refused this (" + d.check.slice(8).toLowerCase().replace(/_/g, " ") + ").";
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
  const TABS = ["dashboard", "curves", "tune", "stress", "profiles", "system", "audit"];
  const SUBS = ["overview", "cores", "sensors"];
  const HASH_ALIAS = { overview: "dashboard/overview", cores: "dashboard/cores", sensors: "dashboard/sensors", vf: "tune" };

  function parseHash(h) {
    let x = h.replace(/^#/, "");
    if (HASH_ALIAS[x]) x = HASH_ALIAS[x];
    const parts = x.split("/");
    return { tab: TABS.indexOf(parts[0]) >= 0 ? parts[0] : "dashboard", sub: SUBS.indexOf(parts[1]) >= 0 ? parts[1] : null };
  }

  function writeHash() {
    const want = "#" + V.tab + (V.tab === "dashboard" && V.sub !== "overview" ? "/" + V.sub : "");
    if (location.hash !== want) history.replaceState(null, "", want);
  }

  function selectTab(name, focus, sub) {
    if (TABS.indexOf(name) < 0) name = "dashboard";
    V.tab = name;
    if (sub && SUBS.indexOf(sub) >= 0) V.sub = sub;
    for (const t of TABS) {
      const btn = $("tab-" + t), panel = $("panel-" + t);
      const on = t === name;
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
      panel.hidden = !on;
    }
    for (const s of SUBS) $("dash-" + s).hidden = s !== V.sub;
    syncSegmented($("dash-sub"), V.sub);
    if (focus) $("tab-" + name).focus();
    writeHash();
    onTabShown(name);
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
  // options: [{value, label, title?}]; onPick(value). syncSegmented() moves the pressed state without calling onPick.
  function segmented(host, options, current, onPick) {
    const btns = options.map((o) => {
      const b = el("button", "seg-btn", o.label);
      b.type = "button";
      b.dataset.value = String(o.value);
      if (o.title) b.title = o.title;
      b.setAttribute("aria-pressed", o.value === current ? "true" : "false");
      b.addEventListener("click", () => {
        for (const x of btns) x.setAttribute("aria-pressed", x === b ? "true" : "false");
        onPick(o.value);
      });
      return b;
    });
    fill(host, btns);
  }
  function syncSegmented(host, current) {
    for (const b of host.children) b.setAttribute("aria-pressed", b.dataset.value === String(current) ? "true" : "false");
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
    return { hex: "#" + c.map((v) => v.toString(16).padStart(2, "0")).join(""), light: (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255 > 0.5 };
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
    FAV.pre = isObj(r) ? favFromTopology(r.topology) : null;   // absent topology: no favored cores from here (the read may still name some)
    recomputeFav();
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
      el("span", "muted", r.mode === "write" ? "writes only from the Tune review dialog" : "mailbox requests are off"),
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
      showBanner("b-readonly", "Read-only mode. Mailbox requests are disabled, so Tune cannot read or apply. Measuring and the dashboard still work. " + why.join(" | "));
    } else if (r.mode === "refuse") {
      const why = r.checks.filter((c) => c && c.status === "refuse").map((c) => c.name + ": " + c.reason);
      showBanner("b-refuse", "Refused: the app cannot operate on this system. " + why.join(" | "));
    } else if (S.writesAllowed === false) {
      showBanner("b-readonly", "The helper does not permit mailbox requests (a preflight check is unknown), so reading and applying ratios and voltages are disabled. Measuring still works. See System.");
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
    noteOps();
    if (S.hbState && S.hbState !== "idle" && S.hbOpId && !S.opId && !S.doneIds.has(S.hbOpId)) {  // re-attach to a running op after a reload
      adoptOp(S.hbOpId, S.hbState);
    } else if (S.hbState === "idle" && S.adopted && S.opId) {  // the adopted op ended and its result was missed
      const kind = S.opKind;
      S.opId = null; S.opKind = null; S.adopted = false; S.cancelling = false;
      afterMissedResult(kind);
    }
    renderHelper();
    updateGate();
  }

  function opsLeft() {
    return S.opsUsed !== null && S.opsBudget !== null ? Math.max(S.opsBudget - S.opsUsed, 0) : null;
  }

  // The budget is an indicator (D-032): how many mailbox operations this boot has used, and the shape of that over the session. The cap is a small note.
  const OPS_HIST_MAX = 720;
  function noteOps() {
    if (S.opsUsed === null) return;
    const h = S.opsHist, last = h.length ? h[h.length - 1] : null;
    if (!last || last.v !== S.opsUsed) {
      h.push({ t: Date.now(), v: S.opsUsed });
      if (h.length > OPS_HIST_MAX) h.shift();
    }
  }

  // A step line, newest at the right edge: the count only changes when something is sent. Left of the first heartbeat this page saw there is no history (dashed). At least a minute wide, so the first seconds of a session are not stretched.
  function drawOpsSpark(id) {
    const svg = $(id), h = S.opsHist;
    if (!h.length) { svg.replaceChildren(); return; }
    const now = Date.now(), t0 = h[0].t, span = Math.max(now - t0, 60000);
    let lo = Infinity, hi = -Infinity;
    for (const q of h) { lo = Math.min(lo, q.v); hi = Math.max(hi, q.v); }
    const top = Math.max(hi, lo + 10);   // a quiet session draws a flat line, not noise
    const X = (t) => (159 - ((now - t) / span) * 158).toFixed(1), Y = (v) => (40 - ((v - lo) / (top - lo)) * 34).toFixed(1);
    let d = "M" + X(h[0].t) + " " + Y(h[0].v);
    for (let i = 1; i < h.length; i++) d += " H" + X(h[i].t) + " V" + Y(h[i].v);
    d += " H" + X(now);
    const area = d + " V43 H" + X(h[0].t) + " Z";
    const pre = "M1 " + Y(h[0].v) + " H" + X(h[0].t);   // before this page opened: only the count at that moment is known, drawn dashed
    fill(svg, [sv("path", { d: "M1 41.5 H159", "vector-effect": "non-scaling-stroke" }, "ops-base"), sv("path", { d: pre, "vector-effect": "non-scaling-stroke" }, "ops-pre"),
      sv("path", { d: area }, "ops-area"), sv("path", { d: d, "vector-effect": "non-scaling-stroke" }, "ops-line")]);
    svg.setAttribute("aria-label", "Mailbox operations used this boot: " + lo + " at the start of this session, " + h[h.length - 1].v + " now");
  }

  function renderHelper() {
    if (T.words) renderProvenance();
    const used = S.opsUsed === null ? "n/a" : S.opsUsed;
    const cap = S.opsBudget === null ? "" : "Per-boot cap " + S.opsBudget + " (policy). The guard refuses past it; it is a limit, not a target.";
    setText("hb-state", S.hbState === null ? "n/a" : S.hbState);
    setText("hb-used", used);
    setText("ov-used", used);
    setText("hb-cap", cap);
    setText("ov-cap", cap);
    setText("ov-state", S.hbState === null ? "helper: n/a" : "helper " + S.hbState);
    drawOpsSpark("sp-ops-ov");
    drawOpsSpark("sp-ops-sys");
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
    if (T.words) renderTuneVcore();
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
    if (!V.paused) renderVisible();
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
    const open = () => { V.selected = c.cpu; selectTab("dashboard", false, "cores"); renderCoreSelection(); };
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
    setText("probe", c.name + coreMark(c) + ", cpu " + cpu + coreFavTitle(c) + ": clock " + fmtReading(coreReading(cpu, "mhz"), 0, "MHz") + "; busy " +
      fmtReading(coreReading(cpu, "busy"), 0, "%") + "; VID " + fmtReading(coreReading(cpu, "vid"), 3, "V") + "; temp " +
      fmtReading(coreReading(cpu, "temp"), 0, "°C") + ".");
  }

  // Colour range of the core map: the fixed display scale, or (auto) the min..max of the cores now, which "Lock" freezes.
  function mapRange() {
    const m = METRICS[V.metric];
    if (V.scaleMode === "fixed") return { lo: m.lo, hi: m.hi };
    if (V.scaleLock) return V.scaleLock;
    const vals = [];
    for (const c of V.coreInfo) { const x = reading(coreReading(c.cpu, V.metric)); if (x.ok) vals.push(x.value); }
    if (!vals.length) return { lo: m.lo, hi: m.hi };
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < (m.digits === 3 ? 0.02 : 4)) { const mid = (lo + hi) / 2, d = m.digits === 3 ? 0.01 : 2; lo = mid - d; hi = mid + d; }
    return { lo: lo, hi: hi };
  }

  function renderDie() {
    const m = METRICS[V.metric];
    const rg = mapRange();
    for (const c of V.coreInfo) {
      const ref = tileRefs[c.cpu];
      if (!ref) continue;
      const x = reading(coreReading(c.cpu, V.metric), m.digits);
      ref.name.textContent = c.short + coreMark(c);
      if (x.ok) {
        const col = rampAt(m.ramp, (x.value - rg.lo) / (rg.hi - rg.lo));
        ref.rect.setAttribute("fill", col.hex);
        ref.g.setAttribute("class", "tile tile-" + c.kind + (col.light ? " ink-dark" : ""));
        ref.val.textContent = c.kind === "P" ? x.text : (m.digits === 3 ? x.value.toFixed(2) : x.text);
        ref.title.textContent = c.name + coreMark(c) + " (cpu " + c.cpu + ")" + coreFavTitle(c) + ": " + x.text + " " + m.unit;
        ref.g.setAttribute("aria-label", c.name + coreFavTitle(c) + ", " + m.label + " " + x.text + " " + m.unit + ". Open in Cores.");
      } else {
        ref.rect.setAttribute("fill", "url(#na-hatch)");
        ref.g.setAttribute("class", "tile tile-" + c.kind + " tile-na");
        ref.val.textContent = "n/a";
        ref.title.textContent = c.name + coreMark(c) + " (cpu " + c.cpu + ")" + coreFavTitle(c) + ": n/a, " + x.title;
        ref.g.setAttribute("aria-label", c.name + ", " + m.label + " not available: " + x.title + ". Open in Cores.");
      }
    }
    if (!V.probing) {
      const reasons = V.coreInfo.map((c) => reading(coreReading(c.cpu, V.metric))).filter((x) => !x.ok).map((x) => x.title);
      setText("probe", V.coreInfo.length && reasons.length === V.coreInfo.length
        ? m.label + " is n/a on every core: " + reasons[0] + "."
        : "Point at a core for its readings. Click to open it in Cores.");
    }
    const fd = m.digits === 3 ? 2 : 0;
    setText("ramp-lo", rg.lo.toFixed(fd));
    setText("ramp-hi", rg.hi.toFixed(fd));
    setText("ramp-unit", m.unit + (V.scaleMode === "fixed" ? " (fixed scale)" : V.scaleLock ? " (locked range)" : " (auto range)"));
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

  // Minimum y-spans (G-119): 1 degree or 1 W of noise must not draw as a cliff. A narrower range is widened around its middle to the span.
  const MIN_SPAN = { temp: 10, power: 20, mhz: 500, volt: 0.05, gbs: 2 };
  function niceRange(lo, hi, zero, minSpan) {
    if (zero) lo = Math.min(lo, 0);
    if (hi - lo < 1e-9) { const d = Math.abs(hi) > 1 ? Math.abs(hi) * 0.05 : 0.05; lo -= d; hi += d; }
    const pad = (hi - lo) * 0.08;
    let a = zero && lo >= 0 ? lo : lo - pad, b = hi + pad;
    if (isNum(minSpan) && b - a < minSpan) {
      if (zero && a >= 0) b = a + minSpan;
      else { const m = (a + b) / 2; a = m - minSpan / 2; b = m + minSpan / 2; }
    }
    return [a, b];
  }

  function fmtVal(v, digits) { return v.toFixed(digits); }

  // The chart window is the operator's choice (1 / 5 / 15 min); history is kept in wall-clock time, so a slower refresh does not shorten it.
  function windowMs() { return V.winMin * 60000; }
  // A gap in the line = no sample for more than 3.5 refresh intervals (and never under 3.5 s).
  function gapMs() { return Math.max(3500, S.intervalS * 3500); }
  // At most ~700 points per line: a 15 min window at 0.25 s refresh holds 3600 samples.
  function decimate(pts) {
    const k = Math.ceil(pts.length / 700);
    return k <= 1 ? pts : pts.filter((x, i) => i % k === 0 || i === pts.length - 1);
  }

  function renderChart(ch) {
    const cfg = ch.cfg, now = Date.now(), span = windowMs(), t0 = now - span;
    ch.t0 = t0; ch.span = span;
    ch.xLabel.textContent = Math.round(span / 60000) + " min ago";
    const pts = decimate(V.hist.filter((s) => s.t >= t0));
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
    const r = cfg.range ? [Math.min(cfg.range[0], lo), Math.max(cfg.range[1], hi)] : niceRange(lo, hi, cfg.zero === true, cfg.minSpan);
    ch.lo = r[0]; ch.hi = r[1];
    ch.yHi.textContent = fmtVal(ch.hi, cfg.digits);
    ch.yLo.textContent = fmtVal(ch.lo, cfg.digits);
    cfg.series.forEach((se, i) => {
      let d = "", prevT = null;
      for (const s of pts) {
        const v = se.get(s);
        if (!isNum(v)) { prevT = null; continue; }
        const x = ((s.t - t0) / span) * CW, y = CH - ((v - ch.lo) / (ch.hi - ch.lo)) * CH;
        d += (prevT === null || s.t - prevT > gapMs() ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
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
      if (best && Math.abs(best.t - t) < gapMs()) { s = best; ago = Math.round((Date.now() - best.t) / 1000); }
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

  function sparkline(svgId, getter, digits, minSpan) {
    const svg = $(svgId);
    const now = Date.now(), t0 = now - SPARK_MS;
    const pts = V.hist.filter((s) => s.t >= t0);
    let lo = Infinity, hi = -Infinity;
    for (const s of pts) { const v = getter(s); if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    if (!(lo <= hi)) { svg.replaceChildren(); return; }
    const r = niceRange(lo, hi, false, minSpan);
    let d = "", prevT = null;
    for (const s of pts) {
      const v = getter(s);
      if (!isNum(v)) { prevT = null; continue; }
      const x = ((s.t - t0) / SPARK_MS) * 120, y = 27 - ((v - r[0]) / (r[1] - r[0])) * 26;
      d += (prevT === null || s.t - prevT > gapMs() ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
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
    makeChart("ch-ov-power", { title: "Package power", unit: "W", digits: 1, minSpan: MIN_SPAN.power,
      series: [{ label: "Package", get: (s) => s.pkg, cls: "s1" }], reason: rsn(lastPower("package")) });
    makeChart("ch-ov-temp", { title: "Hottest temperature", unit: "°C", digits: 0, minSpan: MIN_SPAN.temp,
      series: [{ label: "Hottest", get: (s) => s.hot, cls: "s2" }], reason: hotReason });
    makeChart("ch-power", { title: "Power", unit: "W", digits: 1, minSpan: MIN_SPAN.power,
      series: [{ label: "Package", get: (s) => s.pkg, cls: "s1" }, { label: "Core", get: (s) => s.corew, cls: "s2" }], reason: rsn(lastPower("package")) });
    makeChart("ch-hot", { title: "Hottest temperature", unit: "°C", digits: 0, minSpan: MIN_SPAN.temp,
      series: [{ label: "Hottest", get: (s) => s.hot, cls: "s2" }], reason: hotReason });
    makeChart("ch-vcore", { title: "Vcore (board sensor)", unit: "V", digits: 3, minSpan: MIN_SPAN.volt,
      series: [{ label: "Vcore", get: (s) => s.vcore, cls: "s1" }], reason: rsn(() => V.last.vcore) });
    makeChart("ch-dram", { title: "DRAM bandwidth", unit: "GB/s", digits: 1, minSpan: MIN_SPAN.gbs,
      series: [{ label: "Read", get: (s) => s.dr, cls: "s1" }, { label: "Write", get: (s) => s.dw, cls: "s2" }], reason: rsn(lastDram("read")) });
    const cd = (k) => (s) => (V.selected !== null && isObj(s.cores[V.selected]) ? s.cores[V.selected][k] : null);
    const cdr = (k) => () => coreReading(V.selected, k);
    makeChart("ch-cd-mhz", { title: "Clock", unit: "MHz", digits: 0, minSpan: MIN_SPAN.mhz, series: [{ label: "Clock", get: cd("mhz"), cls: "s1" }], reason: rsn(cdr("mhz")) });
    makeChart("ch-cd-busy", { title: "Busy", unit: "%", digits: 1, range: [0, 100], series: [{ label: "Busy", get: cd("busy"), cls: "s1" }], reason: rsn(cdr("busy")) });
    makeChart("ch-cd-vid", { title: "VID", unit: "V", digits: 3, minSpan: MIN_SPAN.volt, series: [{ label: "VID", get: cd("vid"), cls: "s1" }], reason: rsn(cdr("vid")) });
    makeChart("ch-cd-temp", { title: "Temperature", unit: "°C", digits: 0, minSpan: MIN_SPAN.temp, series: [{ label: "Temp", get: cd("temp"), cls: "s2" }], reason: rsn(cdr("temp")) });
  }

  // ---- Cores tab -------------------------------------------------------------------------------------------------------------------
  const rowRefs = {};  // cpu -> {tr, cells..., spark, bar}

  const CORE_COLS = [  // table columns; toggle=true ones can be switched off, sort=true ones sort by their reading
    { key: "name", label: "Core", sort: true },
    { key: "cpu", label: "CPU", cls: "n" },
    { key: "mhz", label: "Clock (MHz)", sort: true },
    { key: "busy", label: "Busy %", cls: "n", sort: true, toggle: true },
    { key: "ratio", label: "Ratio", cls: "n", sort: true, toggle: true },
    { key: "vid", label: "VID (V)", cls: "n", sort: true, toggle: true },
    { key: "temp", label: "Temp (C)", cls: "n", sort: true, toggle: true },
    { key: "spark", label: "Clock, 60 s", toggle: true },
  ];

  function buildCoreHead() {
    const tr = el("tr");
    for (const c of CORE_COLS) {
      const th = el("th", c.cls || "");
      th.scope = "col";
      th.dataset.col = c.key;
      if (c.sort) {
        const b = el("button", "th-sort", c.label);
        b.type = "button";
        b.title = "Sort by " + c.label;
        b.addEventListener("click", () => {
          if (V.sortKey === c.key) { if (V.sortDir === 1) V.sortDir = -1; else { V.sortKey = null; V.sortDir = 1; } } else { V.sortKey = c.key; V.sortDir = c.key === "name" ? 1 : -1; }
          renderCoreRows();
        });
        th.append(b);
      } else th.textContent = c.label;
      tr.append(th);
    }
    fill($("core-head"), [tr]);
  }

  function applyCols() {
    for (const c of CORE_COLS) {
      if (!c.toggle) continue;
      const on = V.cols[c.key];
      const th = $("core-head").querySelector('[data-col="' + c.key + '"]');
      if (th) th.hidden = !on;
      for (const k of Object.keys(rowRefs)) rowRefs[k].cells[c.key].hidden = !on;
    }
  }

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
      const nameTxt = document.createTextNode(c.name);
      nameCell.append(el("span", "kind-dot k-" + c.kind), nameTxt);
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
      rowRefs[c.cpu] = { tr: tr, nameCell: nameCell, nameTxt: nameTxt, mhzNum: mhzNum, mhzCell: mhzCell, barFill: barFill, busy: busy, ratio: ratio, vid: vid, temp: temp, spark: spark,
        cells: { busy: busy, ratio: ratio, vid: vid, temp: temp, spark: sparkCell } };
      return tr;
    }));
    applyFilter();
    applyCols();
  }

  function setCell(cell, r, digits) {
    const x = reading(r, digits);
    cell.textContent = x.text;
    cell.className = x.ok ? "n" : "n na";
    cell.title = x.title;
  }

  function sortValue(c, key) {
    if (key === "name") return Number(c.cpu);
    const x = reading(coreReading(c.cpu, key));
    return x.ok ? x.value : null;
  }

  function renderCoreRows() {
    const m = METRICS.mhz;
    for (const c of V.coreInfo) {
      const ref = rowRefs[c.cpu];
      if (!ref) continue;
      const x = reading(coreReading(c.cpu, "mhz"), 0);
      ref.nameTxt.nodeValue = c.name + coreMark(c);
      ref.nameCell.title = coreFavTitle(c).replace(/^, /, "");
      ref.mhzNum.textContent = x.text;
      ref.mhzNum.className = "bar-num" + (x.ok ? "" : " na");
      ref.mhzCell.title = x.title;
      ref.barFill.setAttribute("width", x.ok ? (clamp((x.value - 0) / m.hi, 0, 1) * 100).toFixed(1) : 0);
      setCell(ref.busy, coreReading(c.cpu, "busy"), 1);
      setCell(ref.ratio, coreReading(c.cpu, "ratio"), 0);
      setCell(ref.vid, coreReading(c.cpu, "vid"), 3);
      setCell(ref.temp, coreReading(c.cpu, "temp"), 0);
      if (!ref.spark.id) ref.spark.id = "sp-core-" + c.cpu;
      if (!ref.cells.spark.hidden) sparkline(ref.spark.id, (s) => (isObj(s.cores[c.cpu]) ? s.cores[c.cpu].mhz : null), 0, MIN_SPAN.mhz);
      ref.tr.setAttribute("aria-selected", V.selected === c.cpu ? "true" : "false");
    }
    // sorting: missing readings always go last; the header carries aria-sort
    const order = V.coreInfo.slice();
    if (V.sortKey) {
      order.sort((a, b) => {
        const x = sortValue(a, V.sortKey), y = sortValue(b, V.sortKey);
        if (x === null && y === null) return 0;
        if (x === null) return 1;
        if (y === null) return -1;
        return (x - y) * V.sortDir;
      });
    }
    const rows = order.map((c) => rowRefs[c.cpu] && rowRefs[c.cpu].tr).filter(Boolean);
    if (rows.length && rows.some((r, i) => $("core-body").children[i] !== r)) $("core-body").replaceChildren(...rows);
    for (const th of $("core-head").querySelectorAll("th")) {
      th.setAttribute("aria-sort", th.dataset.col === V.sortKey ? (V.sortDir === 1 ? "ascending" : "descending") : "none");
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
    if (c && V.tab === "dashboard" && V.sub === "cores") {
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
    keys.forEach((k, i) => sparkline("sp-temp-" + i, (s) => (s.temps[k] === undefined ? null : s.temps[k]), 0, MIN_SPAN.temp));
  }

  // ---- render scheduling: only the visible view draws ------------------------------------------------------------------------------
  function renderVisible() {
    if (V.tab === "dashboard") {
      if (V.sub === "overview") {
        renderDie();
        renderChart(charts["ch-ov-power"]);
        renderChart(charts["ch-ov-temp"]);
        sparkline("sp-pkgw", (s) => s.pkg, 1, MIN_SPAN.power);
        sparkline("sp-corew", (s) => s.corew, 1, MIN_SPAN.power);
        sparkline("sp-vcore", (s) => s.vcore, 3, MIN_SPAN.volt);
        sparkline("sp-temp", (s) => s.hot, 0, MIN_SPAN.temp);
        sparkline("sp-mhz", (s) => s.top, 0, MIN_SPAN.mhz);
        sparkline("sp-dram", (s) => (isNum(s.dr) && isNum(s.dw) ? s.dr + s.dw : null), 1, MIN_SPAN.gbs);
      } else if (V.sub === "cores") {
        renderCoreRows();
        renderCoreSelection();
      } else if (V.sub === "sensors") {
        renderTemps();
        for (const id of ["ch-power", "ch-hot", "ch-vcore", "ch-dram"]) renderChart(charts[id]);
      }
    } else if (V.tab === "stress") {
      renderStress();
    }
  }

  // ---- ages / staleness (1 Hz) ------------------------------------------------------------------------------------------------------
  function renderAges() {
    const now = Date.now();
    const ta = $("tele-age");
    if (!S.teleAt) { ta.textContent = "no sample yet"; ta.className = "age muted"; }
    else {
      const age = (now - S.teleAt) / 1000;
      const stale = now - S.teleAt > Math.max(5000, S.intervalS * 3000);
      ta.textContent = stale ? "stale: last sample " + age.toFixed(0) + " s ago"
        : V.paused ? "paused: still sampling every " + fmtInterval(S.intervalS) : "live, one sample every " + fmtInterval(S.intervalS);
      ta.className = stale ? "age stale" : V.paused ? "age muted" : "age live";
    }
    const ha = $("hb-age");
    if (!S.hbAt) ha.textContent = "n/a";
    else {
      const stale = now - S.hbAt > HEARTBEAT_STALE_MS;
      ha.textContent = stale ? "lost (" + ((now - S.hbAt) / 1000).toFixed(0) + " s ago)" : ((now - S.hbAt) / 1000).toFixed(0) + " s ago";
      ha.className = stale ? "stale" : "";
    }
  }
  function fmtInterval(s) { return (s < 1 ? s.toFixed(2).replace(/0$/, "") : String(s)) + " s"; }
  function heartbeatFresh() { return S.hbAt > 0 && Date.now() - S.hbAt <= HEARTBEAT_STALE_MS; }

  // ---- tuning units (Contract 9: 12 curves = 8 P-cores + 4 E-clusters) and policy ranges (D-032) ---------------------------------------
  const UNIT_IDS = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3"];
  // Favored cores come from live data only (D-036): the preflight topology (CPPC performance or cpuinfo max frequency), else the 0x1C favored ratios of the
  // last read (the P-cores whose ratio equals the highest, when they differ), else none. No core is favored by default.
  const FAV_TEXT = { acpi_cppc: "favored core (highest CPPC performance)", cpuinfo_max_freq: "favored core (highest max frequency)", "0x1C": "favored core (0x1C favored ratio)" };
  const FAV = { pre: null, read: null, ids: [], source: null, key: "|null" };   // pre: {ids, source} | null; read: [unit id] | null
  function isFav(u) { return FAV.ids.indexOf(u) >= 0; }
  function favMark(u) { return isFav(u) ? " \u2605" : ""; }
  function favTitle(u) { return isFav(u) ? FAV_TEXT[FAV.source] || "favored core" : ""; }
  function favFromTopology(topo) {
    if (!isObj(topo) || !Array.isArray(topo.units)) return null;
    const ids = topo.units.filter((x) => isObj(x) && x.favored === true && typeof x.id === "string" && x.id.charAt(0) === "P" && UNIT_IDS.indexOf(x.id) >= 0).map((x) => x.id);
    // determined: the system had a source to judge by (an empty set then means every P-core is equal: none favored), so the read must not override it
    return { ids: ids, source: topo.favored_source === "cpuinfo_max_freq" ? "cpuinfo_max_freq" : "acpi_cppc", determined: ids.length > 0 || typeof topo.favored_source === "string" };
  }
  function favFromRead(turbo) {   // {unit: ratio}: the P-cores at the highest favored ratio, only when the cores differ
    if (!isObj(turbo) || !isObj(turbo.favored)) return null;
    const ids = Object.keys(turbo.favored).filter((u) => isNum(turbo.favored[u]));
    if (ids.length < 2) return [];
    const hi = Math.max(...ids.map((u) => turbo.favored[u]));
    return ids.every((u) => turbo.favored[u] === hi) ? [] : UNIT_IDS.filter((u) => ids.indexOf(u) >= 0 && turbo.favored[u] === hi);
  }
  function recomputeFav() {
    let ids = [], source = null;
    if (FAV.pre && FAV.pre.determined) { ids = FAV.pre.ids; source = ids.length ? FAV.pre.source : null; }
    else if (FAV.read && FAV.read.length) { ids = FAV.read; source = "0x1C"; }
    const key = ids.join(",") + "|" + source;
    if (key === FAV.key) return;
    FAV.key = key; FAV.ids = ids; FAV.source = source;
    onFavoredChanged();
  }
  function onFavoredChanged() {   // the marks that were built once; paints that run on every tick or every render read FAV themselves
    for (const b of document.querySelectorAll(".unit-chip[data-unit], .mm-tile[data-unit]")) {
      const u = b.dataset.unit, n = b.querySelector(".uc-name, .mm-name");
      if (n) n.textContent = u + favMark(u);
      b.title = unitTileTitle(u);
    }
    if (CV.chart && V.tab === "curves") renderCurves(); else if (CV.chart) renderPresets();
    if (T.words) buildRows();
  }
  function coreUnit(c) { return c.kind === "P" && /^\d+$/.test(String(c.cpu)) ? "P" + c.cpu : null; }   // a dashboard core -> its tuning unit (P-core n is unit Pn)
  function coreMark(c) { const u = coreUnit(c); return u && isFav(u) ? " \u2605" : ""; }
  function coreFavTitle(c) { const u = coreUnit(c); return u && isFav(u) ? ", " + favTitle(u) : ""; }
  // The system's answer and the 0x1C favored ratios of the read disagree (an empty set counts: "none favored" against a core with a higher ratio).
  function favMismatch() { return !!(FAV.pre && FAV.pre.determined && FAV.read && FAV.pre.ids.join() !== FAV.read.join()); }
  const POLICY = {
    offsetMin: -200, offsetMax: 200, offsetStep: 5,                 // mV
    vtMin: 0.900, vtMax: { P: 1.625, E: 1.355 }, vtStep: 0.005,     // V, adaptive target at the OC point
    ratioMax: { P: 57, E: 49 },
  };
  function unitKind(id) { return id.charAt(0) === "P" ? "P" : "E"; }
  function unitCpus(id) {
    if (unitKind(id) === "P") return [Number(id.slice(1))];
    const n = Number(id.slice(2));
    return [8 + 4 * n, 9 + 4 * n, 10 + 4 * n, 11 + 4 * n];
  }
  function unitTileTitle(u) { return unitLabel(u) + " (cpu " + unitCpus(u).join(", ") + ")" + (isFav(u) ? ", " + favTitle(u) : ""); }
  function unitLabel(id) { return unitKind(id) === "P" ? "P-core " + id.slice(1) : "E-cluster " + id.slice(2); }
  const MINUS = "−";
  function fmtMv(x, digits) {
    if (!isNum(x)) return "n/a";
    const d = digits === undefined ? (Math.abs(x - Math.round(x)) < 0.05 ? 0 : 1) : digits;
    return (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x).toFixed(d) + " mV";
  }
  function fmtV(x, digits) { return isNum(x) ? x.toFixed(digits === undefined ? 3 : digits) + " V" : "n/a"; }
  function fmtClock(s) { const m = Math.floor(s / 60), r = Math.round(s % 60); return m ? m + " min " + (r < 10 ? "0" : "") + r + " s" : r + " s"; }
  // Dump unit -> tuning unit. P: the cpu is the core; E: the first cluster cpu (8, 12, 16, 20) is the cluster.
  function unitIdOf(u) {
    if (!isObj(u)) return null;
    if (typeof u.id === "string" && UNIT_IDS.indexOf(u.id) >= 0) return u.id;   // tune.read (G-112) names the unit
    if (typeof u.unit === "string" && UNIT_IDS.indexOf(u.unit) >= 0) return u.unit;
    const first = Array.isArray(u.cluster_cpus) && isNum(u.cluster_cpus[0]) ? u.cluster_cpus[0] : (Array.isArray(u.cpus) && isObj(u.cpus[0]) ? u.cpus[0].cpu : null);
    if (u.kind === "P" && isNum(first) && first >= 0 && first < 8) return "P" + first;
    if (u.kind === "E" && isNum(u.cluster) && u.cluster >= 0 && u.cluster < 4) return "EC" + u.cluster;
    if (u.kind === "E" && isNum(first) && first >= 8 && first < 24) return "EC" + Math.floor((first - 8) / 4);
    return null;
  }

  // ---- long operations: tune.read, vf.measure, tune.apply (one at a time; the helper state machine decides) ------------------------------
  function opKindFromState(st) { return st === "measuring" ? "measure" : st === "applying" ? "commit" : st === "stressing" ? "stress" : "read"; }

  function adoptOp(id, st) {
    S.opId = id; S.opKind = opKindFromState(st); S.adopted = true; S.cancelling = false;
    if (S.opKind === "stress" && !ST.run) ST.run = { kind: "cpu", of: 0, elapsed: 0, errors: 0, units: UNIT_IDS.slice() };   // started elsewhere or before a reload: settings unknown
    onOpStarted();
  }

  // A POST answers 202 {id} for a long op (the result arrives over SSE), or 200 with the result at once (a helper that finished quickly).
  function launched(kind, status, body) {
    if (status === 202 && isObj(body) && typeof body.id === "string") {
      if (S.doneIds.has(body.id)) return true;  // the SSE result beat the POST response (a fast helper): already shown
      S.opId = body.id; S.opKind = kind; S.adopted = false; S.cancelling = false;
      onOpStarted();
      return true;
    }
    if (status === 200 && isObj(body) && body.ok === true) {
      const id = typeof body.id === "string" ? body.id : null;
      if (id) S.doneIds.add(id);
      dispatchResult(kind, { id: id, ok: true, result: body.result });
      return true;
    }
    return false;
  }

  function onOpStarted() { updateGate(); }

  async function cancelOp() {
    const id = S.opId || S.hbOpId;
    if (!id) return;
    S.cancelling = true;
    updateGate();
    const { status, body } = await api("cancel", { op_id: id });
    const msg = status === 200 && body.ok ? "Cancel requested. It takes effect between steps." : "Cancel not accepted: " + errText(body);
    if (!(status === 200 && body.ok)) S.cancelling = false;
    setText(S.opKind === "measure" ? "cv-status" : "tn-status", msg);
    updateGate();
  }

  function onProgress(ev) {
    const id = typeof ev.op_id === "string" ? ev.op_id : null;
    if (!S.opId && id && !S.doneIds.has(id)) adoptOp(id, typeof ev.unit === "string" ? "measuring" : isNum(ev.elapsed_s) ? "stressing" : S.hbState);
    if (id !== S.opId) return;
    if (isNum(ev.ops_used) && isNum(ev.ops_budget)) { S.opsUsed = ev.ops_used; S.opsBudget = ev.ops_budget; renderHelper(); }
    if (S.opKind === "measure") measureProgress(ev); else if (S.opKind === "stress") stressProgress(ev); else mailboxProgress(ev);
    updateGate();
  }

  function inferKind(ev) {
    const r = isObj(ev.result) ? ev.result : null;
    if (!r) return null;
    if ("curve_id" in r || "record" in r) return "measure";
    if ("stopped_by" in r) return "stress";
    if ("readback" in r) return "commit";
    if (Array.isArray(r.units)) return "read";
    return null;
  }

  function onResult(ev) {
    // A result for an id this page does not remember (reload, second tab) is shown too: the operation is addressed by id, not by tab.
    if (typeof ev.id === "string") S.doneIds.add(ev.id);
    if (S.opId !== null && ev.id !== S.opId) return;  // another tab's operation ended; this page's running op is untouched
    const kind = S.opKind || inferKind(ev);
    S.opId = null; S.opKind = null; S.adopted = false; S.cancelling = false;
    if (kind) dispatchResult(kind, ev);
    updateGate();
    loadAudit();
  }

  function dispatchResult(kind, ev) {
    if (kind === "measure") onMeasureResult(ev);
    else if (kind === "stress") onStressResult(ev);
    else if (kind === "commit") onCommitResult(ev);
    else onReadResult(ev);
  }

  function afterMissedResult(kind) {
    if (kind === "measure") { setText("cv-status", "The measurement ended while this page was away. Stored curves were reloaded."); loadCurves(true); }
    else if (kind === "stress") { ST.run = null; setText("st-status", "A stress screen ended while this page was away. It is in the list below."); renderStress(); loadStressList(); }
    else if (kind === "commit") setText("tn-status", "An apply ended while this page was away. Read the ratios and voltages to see where things stand.");
    else { setText("tn-status", "A read ended while this page was away."); S.lastTried = false; loadLastRead(); }
  }

  // ---- gates ----------------------------------------------------------------------------------------------------------------------------
  // What a read costs, from the last read: the helper's own count of the operations it used, else one per V/F point of the units read (+ the OC word of each unit); null before any read.
  function readCost() {
    const w = T.words;
    if (!w) return null;
    if (isObj(w.raw) && isNum(w.raw.ops_done) && w.raw.ops_done > 0) return w.raw.ops_done;
    const n = UNIT_IDS.reduce((t, u) => t + (w.units[u] ? w.units[u].points.length + (w.units[u].oc ? 1 : 0) : 0), 0);
    return n > 0 ? n : null;
  }
  function readCostText() { return readCost() === null ? "about one operation per V/F point of each unit" : "about " + readCost() + " operations (what the last read used)"; }
  function renderReadCost() { setText("tn-cost-n", readCostText()); }
  // Mailbox reads (tune.read): preflight mode write, writes allowed, an idle helper with a fresh heartbeat, and the upper cost bound must fit.
  function gateReason() {
    if (S.sessionLost) return "Session ended.";
    if (!S.sseUp) return "Waiting for the event stream.";
    if (S.mode === null) return "Preflight has not been read.";
    if (S.mode !== "write") return "Preflight mode is " + S.mode + ": mailbox requests are disabled.";
    if (S.writesAllowed !== true) return "The helper's preflight does not allow writes.";
    if (!heartbeatFresh() || opsLeft() === null) return "Waiting for a helper heartbeat with the ops budget.";
    if (S.opId || S.submitting) return "A helper operation is running.";
    if (S.hbState !== "idle") return "The helper is " + S.hbState + ".";
    if (opsLeft() <= 0) return "No operations left in this boot's budget.";
    if (readCost() !== null && opsLeft() < readCost()) return "Does not fit: " + opsLeft() + " ops left, about " + readCost() + " needed.";
    return "";
  }

  // Anything that is not a mailbox request (measure, plan review): a live session, a fresh heartbeat, an idle helper.
  function idleGate() {
    if (S.sessionLost) return "Session ended.";
    if (!S.sseUp) return "Waiting for the event stream.";
    if (S.mode === "refuse") return "Preflight refused this system. See System.";
    if (!heartbeatFresh()) return "Waiting for a helper heartbeat.";
    if (S.opId || S.submitting) return "A helper operation is running.";
    if (S.hbState !== "idle") return "The helper is " + S.hbState + ".";
    return "";
  }

  // The same checks as the read gate, for a plan that needs `ops` operations.
  function applyGate(ops) {
    if (S.sessionLost) return "Session ended.";
    if (!S.sseUp) return "Waiting for the event stream.";
    if (S.mode !== "write") return "Preflight mode is " + (S.mode === null ? "unknown" : S.mode) + ": mailbox requests are disabled.";
    if (S.writesAllowed !== true) return "The helper's preflight does not allow writes.";
    if (!heartbeatFresh() || opsLeft() === null) return "Waiting for a helper heartbeat with the ops budget.";
    if (S.opId || S.submitting) return "A helper operation is running.";
    if (S.hbState !== "idle") return "The helper is " + S.hbState + ".";
    if (isNum(ops) && opsLeft() < ops) return "Does not fit: " + opsLeft() + " ops left, " + ops + " needed.";
    return "";
  }

  function updateGate() {
    updateTuneGate();
    updateCurvesGate();
    updateModalGate();
    updateStressGate();
    const tag = $("mode-tag");
    if (S.mode === "write") { tag.textContent = S.writesAllowed === true ? "guarded writes" : "writes off"; }
    else if (S.mode === null) tag.textContent = "reading preflight";
    else tag.textContent = S.mode;
    tag.className = "ro-tag" + (S.mode === "write" && S.writesAllowed === true ? " armed" : "");
  }

  // ---- Dashboard: applied-state chip, pause, refresh slider --------------------------------------------------------------------------
  function setAppliedChip(kind, text, title) {
    for (const id of ["dash-state", "tn-state"]) {
      setPill(id, "applied: " + text, kind === "stock" ? "s-ok" : kind === "words" ? "s-pass" : "s-neutral");
      $(id).title = title;
    }
  }

  function refreshLabel(i) { return fmtInterval(REFRESH_STEPS[i]); }

  function onRefreshInput() {
    const i = Number($("refresh-range").value);
    const iv = REFRESH_STEPS[clamp(i, 0, REFRESH_STEPS.length - 1)];
    $("refresh-val").textContent = refreshLabel(i);
    $("refresh-range").setAttribute("aria-valuetext", refreshLabel(i));
    S.intervalS = iv;
    savePrefs();
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(restartSampler, 250);  // let the slider settle: each change restarts the sampler
  }
  let refreshTimer = null;

  async function restartSampler() {
    if (!S.sseUp) return;
    const { status, body } = await api("telemetry.start", { interval_s: S.intervalS });
    if (!(status === 200 && body.ok)) showBanner("b-readonly", "The sampler did not accept the new refresh rate: " + errText(body));
    else if (S.mode !== "refuse" && !S.sessionLost) hideBannerIfInfo();
  }
  function hideBannerIfInfo() { if ($("banner").classList.contains("b-readonly") && $("banner").textContent.indexOf("refresh rate") >= 0) hideBanner(); }

  function setPaused(on) {
    V.paused = on;
    const b = $("btn-pause");
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.textContent = on ? "Resume" : "Pause";
    if (!on) renderVisible();
    renderAges();
  }

  // ---- Memory (mem.read: static between boots, read once on load and on demand) -----------------------------------------------------
  const TIMING_ORDER = ["tCL", "tRCD", "tRP", "tRAS", "tRC", "tRFC", "tRFCpb", "tREFI", "tFAW", "tRRD_L", "tRRD_S", "tCWL", "tRTP", "tWR", "tWTP", "tRCDW", "tRPab", "tREFSBRD"];
  let memData = null;

  async function loadMem() {
    const btn = $("btn-mem-refresh");
    btn.disabled = true;
    const { status, body } = await api("mem.read", {});
    btn.disabled = false;
    if (status === 200 && body.ok && isObj(body.result)) { memData = body.result; renderMem(memData); }
    else { memData = null; renderMemUnavailable("The helper could not read memory: " + errText(body)); }
  }

  function renderMemUnavailable(reason) {
    $("btn-mem-all").disabled = true;
    fill($("mem-body"), [el("p", "na", "n/a: " + reason)]);
  }

  function timing(t, k) { return isObj(t) && isNum(t[k]) ? String(t[k]) : "n/a"; }

  function renderMem(r) {
    if (r.available !== true) { renderMemUnavailable(typeof r.reason === "string" ? r.reason : "the helper gave no reason"); return; }
    $("btn-mem-all").disabled = false;
    const t = isObj(r.timings) ? r.timings : {};
    const head = el("div", "mem-head");
    head.append(el("span", "mem-big", (isNum(r.mts) ? "DDR5-" + r.mts : "DDR5") + " · Gear " + (isNum(r.gear) ? r.gear : "n/a") + " · " + (typeof r.cmd_rate === "string" ? r.cmd_rate : "n/a")));
    head.append(el("span", "mem-prim mono", [timing(t, "tCL"), timing(t, "tRCD"), timing(t, "tRP"), timing(t, "tRAS")].join("-")));
    const kv = el("dl", "kv mem-kv");
    const add = (k, v) => { kv.append(el("dt", "", k), el("dd", "", v)); };
    add("tRFC", timing(t, "tRFC"));
    add("tREFI", timing(t, "tREFI"));
    add("Memory clock", isNum(r.qclk_mhz) ? r.qclk_mhz.toFixed(1) + " MHz" : "n/a");
    add("BCLK", isNum(r.bclk_mhz) ? r.bclk_mhz.toFixed(1) + " MHz" : "n/a");
    add("Peak bandwidth", isNum(r.max_bandwidth_mbs) ? (r.max_bandwidth_mbs / 1000).toFixed(1) + " GB/s" : "n/a");
    const cc = isObj(r.cross_check) ? r.cross_check : {};
    add("Cross-check", cc.agree === true ? "agrees" : cc.agree === false ? "disagrees" : "n/a");
    const dimms = Array.isArray(r.dimms) ? r.dimms : [];
    const list = el("ul", "dimm-list");
    for (const d of dimms) {
      const li = el("li", "dimm");
      const tp = reading(isObj(d) ? d.temp_c : undefined, 1);
      const temp = el("span", "dimm-temp" + (tp.ok ? "" : " na"), tp.ok ? tp.text + " °C" : "n/a");
      temp.title = tp.title;
      li.append(el("span", "dimm-loc", d.locator), el("span", "dimm-desc", [d.size, d.part, d.manufacturer].filter(Boolean).join(" · ")), temp);
      list.append(li);
    }
    if (!dimms.length) list.append(el("li", "na", "n/a: no DIMM data from SMBIOS"));
    const left = el("div", "mem-left");
    left.append(head, el("p", "muted", dimms.length + (dimms.length === 1 ? " module" : " modules") + (isNum(r.instances_ok) ? ", read from " + r.instances_ok + " controller instances" : "")), list);
    fill($("mem-body"), [left, kv]);
  }

  function openTimings() {
    if (!memData || memData.available !== true) return;
    const r = memData, t = isObj(r.timings) ? r.timings : {};
    const body = [];
    const tbl = el("table", "grid num timing-grid");
    const tb = el("tbody");
    for (let i = 0; i < TIMING_ORDER.length; i += 3) {
      const tr = el("tr");
      for (const k of TIMING_ORDER.slice(i, i + 3)) { tr.append(el("th", "", k), td(timing(t, k), "n mono")); }
      tb.append(tr);
    }
    tbl.append(el("caption", "sr", "Memory timings"), tb);
    body.push(el("h3", "dlg-sub", "Timings (clocks)"), tbl);
    const ta = isObj(r.turnaround) ? r.turnaround : {};
    const mt = el("table", "grid num");
    const mh = el("thead"), mhr = el("tr");
    mhr.append(el("th", "", "Turnaround"));
    for (const c of ["SG", "DG", "SD", "DD"]) mhr.append(el("th", "n", c));
    mh.append(mhr);
    const mb = el("tbody");
    for (const k of ["RDRD", "RDWR", "WRRD", "WRWR"]) {
      const tr = el("tr");
      tr.append(el("th", "", k));
      for (const c of ["SG", "DG", "SD", "DD"]) tr.append(td(isObj(ta[k]) && isNum(ta[k][c]) ? ta[k][c] : "n/a", "n mono"));
      mb.append(tr);
    }
    mt.append(el("caption", "sr", "Turnaround timings"), mh, mb);
    body.push(el("h3", "dlg-sub", "Turnarounds (SG same group, DG different group, SD same DIMM, DD different DIMM)"), mt);
    const cc = isObj(r.cross_check) ? r.cross_check : {};
    const kv = el("dl", "kv");
    const add = (k, v) => { kv.append(el("dt", "", k), el("dd", "", v)); };
    add("Data rate", isNum(r.mts) ? r.mts + " MT/s" : "n/a");
    add("Memory clock", isNum(r.qclk_mhz) ? r.qclk_mhz.toFixed(1) + " MHz" : "n/a");
    add("BCLK", isNum(r.bclk_mhz) ? r.bclk_mhz.toFixed(3) + " MHz" : "n/a");
    add("Cross-check", cc.agree === true ? "agrees (" + cc.mhz_a + " / " + cc.mhz_b + " MHz)" : cc.agree === false ? "disagrees (" + cc.mhz_a + " / " + cc.mhz_b + " MHz)" : "n/a");
    add("Controller instances", isNum(r.instances_ok) ? String(r.instances_ok) : "n/a");
    body.push(el("h3", "dlg-sub", "Frequency"), kv);
    fill($("dlg-timings-body"), body);
    openDialog($("dlg-timings"));
  }

  function openDialog(d) { if (!d.open) d.showModal(); }
  function closeDialog(d) { if (d.open) d.close(); }

  // ---- Tune: reading the ratios and voltages (the old vf.dump) ---------------------------------------------------------------------------------
  async function startRead() {
    if (gateReason() !== "") return;
    S.submitting = true;
    setText("tn-status", "Submitting...");
    updateGate();
    const { status, body } = await api("tune.read", {});
    S.submitting = false;
    if (launched("read", status, body)) {
      if (S.opId) {
        setText("tn-status", "Reading (operation " + S.opId + "). Results arrive over the event stream.");
        setText("tn-progress", "waiting for the first step");
        $("tn-bar").max = 1; $("tn-bar").value = 0;
      }
    } else {
      setText("tn-status", "Not started: " + (status === 429 ? "too many operations pending" : errText(body)));
    }
    updateGate();
  }

  function mailboxProgress(ev) {
    if (!(isNum(ev.step) && isNum(ev.of) && ev.of > 0)) return;
    let t = "step " + ev.step + " of " + ev.of;
    if (isNum(ev.ops_used) && isNum(ev.ops_budget)) t += "; ops used " + ev.ops_used + " / " + ev.ops_budget;
    if (S.opKind === "commit") { setCommitProgress(ev.step, ev.of, t); return; }
    $("tn-bar").max = ev.of; $("tn-bar").value = Math.min(ev.step, ev.of);
    setText("tn-progress", t);
  }

  // On load: this boot's stored read (tune.last). No mailbox operation: the helper kept the last complete read in a root-owned file.
  async function loadLastRead() {
    if (T.words || S.lastTried) return;
    const { status, body } = await api("tune.last", {});
    if (status !== 200 || !body.ok || !isObj(body.result)) return;
    S.lastTried = true;
    if (T.words || body.result.available !== true || !isObj(body.result.result)) return;   // a read finished meanwhile, or none exists for this boot
    setWords(body.result.result, { at: body.result.at, applied_at: body.result.applied_at });
    setText("tn-status", "Showing the read kept by the helper (no mailbox operations used). " + (T.words && T.words.appliedSince ? "It was updated from the last apply's read-back. " : "") + "Press Re-read ratios & voltages for a fresh read.");
    renderReadCost();
  }

  function onReadResult(ev) {
    if (ev.ok === true && isObj(ev.result)) {
      const r = ev.result;
      setText("tn-status", (r.complete === false ? "Incomplete read. " : "Read complete. ") +
        (isNum(r.ops_done) ? r.ops_done + " mailbox operations used. " : "") + (r.simulator === true ? "(simulator data)" : ""));
      setWords(r);
    } else if (typeof ev.server_error === "string") {
      setText("tn-status", ev.server_error === "helper_lost" ? "The helper was lost before it answered. Check the audit log before retrying." : "Failed: " + ev.server_error);
    } else {
      const code = isObj(ev.error) ? ev.error.code : "";
      setText("tn-status", code === "CANCELLED" ? "Cancelled. The requests already sent are in the audit log. " + errText(ev) : "Failed: " + errText(ev));
    }
  }

  // ---- V/F chart engine (Curves and the Tune preview): x = frequency, y = volts (or mV for a difference) -----------------------------------------
  // model: {series: [{key, unit, label, pts: [{mhz, v}], dash, live}], markers: [{unit, mhz, v, hollow, label}], band: [{mhz, lo, hi}] | null,
  //         ymode: "v" | "mv", empty: string | null}
  // Geometry goes through SVG attributes only. The tooltip is SVG drawn inside the chart (no HTML positioning, so no inline style).
  const VC = { L: 66, R: 26, T: 18, B: 56 };   // plot margins; the width follows the container, the height follows the width

  function niceStep(span, target) {
    const raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
  }

  function interpAt(pts, mhz) {  // {v, exact} | null; pts sorted by mhz
    if (!pts.length || mhz < pts[0].mhz - 1e-6 || mhz > pts[pts.length - 1].mhz + 1e-6) return null;
    for (let i = 0; i < pts.length; i++) {
      if (Math.abs(pts[i].mhz - mhz) < 50) return { v: pts[i].v, exact: true };
      if (pts[i].mhz > mhz && i > 0) {
        const a = pts[i - 1], b = pts[i];
        return { v: a.v + ((b.v - a.v) * (mhz - a.mhz)) / (b.mhz - a.mhz), exact: false };
      }
    }
    return null;
  }

  function makeVfChart(host, opts) {
    const ch = { W: 960, H: 400, host: host, model: null, hoverMhz: null, hot: null, svg: null, tipLabel: opts.label || "V/F curves", xDom: [800, 5800], yDom: [0.6, 1.5], plot: null };

    ch.sx = (mhz) => VC.L + ((mhz - ch.xDom[0]) / (ch.xDom[1] - ch.xDom[0])) * (ch.W - VC.L - VC.R);
    ch.sy = (v) => VC.T + ((ch.yDom[1] - v) / (ch.yDom[1] - ch.yDom[0])) * (ch.H - VC.T - VC.B);

    ch.render = function (model) {
      ch.model = model;
      ch.W = clamp(Math.round(host.clientWidth || 960), 560, 1800);
      ch.H = clamp(Math.round(ch.W * 0.38), 300, 520);
      const all = [];
      for (const s of model.series) for (const p of s.pts) all.push(p);
      for (const m of model.markers || []) all.push(m);
      for (const m of model.pins || []) all.push(m);
      const svg = sv("svg", { viewBox: "0 0 " + ch.W + " " + ch.H, role: "img", tabindex: 0, focusable: "true",
        "aria-label": ch.tipLabel + ". Use the left and right arrow keys to read values." }, "vfsvg");
      ch.svg = svg;
      if (!all.length) {
        const t = sv("text", { x: ch.W / 2, y: ch.H / 2, "text-anchor": "middle" }, "plot-empty");
        t.textContent = model.empty || "Nothing to draw yet.";
        svg.append(t);
        fill(host, [svg]);
        ch.plot = null;
        return;
      }
      const ys = all.map((p) => p.v);
      let xlo = Math.min(...all.map((p) => p.mhz)), xhi = Math.max(...all.map((p) => p.mhz));
      xlo = Math.floor((xlo - 50) / 500) * 500; xhi = Math.ceil((xhi + 50) / 500) * 500;
      ch.xDom = [xlo, Math.max(xhi, xlo + 1000)];
      let ylo = Math.min(...ys), yhi = Math.max(...ys);
      if (model.band) for (const b of model.band) { ylo = Math.min(ylo, b.lo); yhi = Math.max(yhi, b.hi); }
      if (model.ymode === "mv") { ylo = Math.min(ylo, 0); yhi = Math.max(yhi, 0); }
      if (yhi - ylo < (model.ymode === "mv" ? 4 : MIN_SPAN.volt)) { const m = (yhi + ylo) / 2, d = model.ymode === "mv" ? 2 : MIN_SPAN.volt / 2; ylo = m - d; yhi = m + d; }
      const step = niceStep(yhi - ylo, 6);
      ch.yDom = [Math.floor(ylo / step) * step, Math.ceil(yhi / step) * step];
      if (ch.yDom[1] - ch.yDom[0] < step) ch.yDom[1] += step;
      const nodes = [];
      const grid = sv("g", null, "vc-grid");
      for (let v = ch.yDom[0]; v <= ch.yDom[1] + step * 1e-6; v += step) {
        const y = ch.sy(v);
        grid.append(sv("line", { x1: VC.L, x2: ch.W - VC.R, y1: y, y2: y }, model.ymode === "mv" && Math.abs(v) < step * 1e-6 ? "vc-zero" : ""));
        const t = sv("text", { x: VC.L - 10, y: y + 4, "text-anchor": "end" }, "vc-tick");
        t.textContent = model.ymode === "mv" ? (v > 0 ? "+" : v < 0 ? MINUS : "") + Math.abs(v).toFixed(step < 1 ? 1 : 0) : v.toFixed(step < 0.1 ? 2 : 1);
        grid.append(t);
      }
      for (let m = ch.xDom[0]; m <= ch.xDom[1]; m += 500) {
        const x = ch.sx(m);
        grid.append(sv("line", { x1: x, x2: x, y1: VC.T, y2: ch.H - VC.B, class: "vc-vgrid" }));
        const a = sv("text", { x: x, y: ch.H - VC.B + 18, "text-anchor": "middle" }, "vc-tick");
        a.textContent = String(m);
        const b = sv("text", { x: x, y: ch.H - VC.B + 33, "text-anchor": "middle" }, "vc-tick vc-ratio");
        b.textContent = m / 100 + "×";
        grid.append(a, b);
      }
      const xl = sv("text", { x: (VC.L + ch.W - VC.R) / 2, y: ch.H - 6, "text-anchor": "middle" }, "plot-axis");
      xl.textContent = "frequency (MHz) and ratio";
      const yl = sv("text", { x: 14, y: (VC.T + ch.H - VC.B) / 2, "text-anchor": "middle", transform: "rotate(-90 14 " + (VC.T + ch.H - VC.B) / 2 + ")" }, "plot-axis");
      yl.textContent = model.ymode === "mv" ? "difference (mV)" : "VID (V)";
      nodes.push(grid, xl, yl);
      if (model.band && model.band.length > 1) {
        const up = model.band.map((b, i) => (i ? "L" : "M") + ch.sx(b.mhz).toFixed(1) + " " + ch.sy(b.hi).toFixed(1)).join("");
        const dn = model.band.slice().reverse().map((b) => "L" + ch.sx(b.mhz).toFixed(1) + " " + ch.sy(b.lo).toFixed(1)).join("");
        nodes.push(sv("path", { d: up + dn + "Z" }, "vc-band"));
      }
      const lines = sv("g", null, "vc-lines");
      ch.lineEls = {};
      model.series.forEach((s) => {
        if (!s.pts.length) return;
        const cls = "vc-line u-" + s.unit + (s.dash ? " dash" : "") + (isFav(s.unit) ? " fav" : "");
        const p = sv("path", { d: s.pts.map((q, i) => (i ? "L" : "M") + ch.sx(q.mhz).toFixed(1) + " " + ch.sy(q.v).toFixed(1)).join("") }, cls);
        lines.append(p);
        ch.lineEls[s.key] = p;
        const marked = s.pts.some((q) => isFusedPoint(q.fused));   // fused points filled and larger, in-between ratios small and quiet (even on a dense run)
        if ((marked || s.pts.length <= 24) && !s.dash) {
          for (const q of s.pts) {
            const fused = isFusedPoint(q.fused);
            const dot = sv("circle", { cx: ch.sx(q.mhz).toFixed(1), cy: ch.sy(q.v).toFixed(1), r: !marked ? 3 : fused ? 4.5 : 2 }, "vc-dot u-" + s.unit + (marked ? (fused ? " fused" : " between") : ""));
            if (marked && fused) { const t = sv("title"); t.textContent = s.unit + " " + q.mhz / 100 + "x: " + fusedLabel(q.fused); dot.append(t); }
            lines.append(dot);
          }
        }
        if (s.live) {
          const last = s.pts[s.pts.length - 1];
          lines.append(sv("circle", { cx: ch.sx(last.mhz).toFixed(1), cy: ch.sy(last.v).toFixed(1), r: 7 }, "vc-head u-" + s.unit));
        }
      });
      nodes.push(lines);
      for (const m of model.markers || []) {
        const g = sv("g", null, "vc-marker u-" + m.unit + (m.hollow ? " hollow" : ""));
        g.append(sv("circle", { cx: ch.sx(m.mhz).toFixed(1), cy: ch.sy(m.v).toFixed(1), r: 6 }));
        if (m.label) {
          const t = sv("text", { x: ch.sx(m.mhz) + 9, y: ch.sy(m.v) - 8 }, "vc-mlabel");
          t.textContent = m.label;
          g.append(t);
        }
        nodes.push(g);
      }
      ch.cross = sv("line", { x1: -10, x2: -10, y1: VC.T, y2: ch.H - VC.B }, "vc-cross");
      ch.tip = sv("g", { transform: "translate(-999 -999)" }, "vc-tip");
      const hit = sv("rect", { x: VC.L, y: VC.T, width: ch.W - VC.L - VC.R, height: ch.H - VC.T - VC.B }, "vc-hit");
      nodes.push(ch.cross, hit, ch.tip);
      // pins sit above the hit area so the browser shows their <title> on hover: hollow ring = a point that exists but was not reached (F3/F9),
      // diamond = the fused top-point value read from the CPU (F7)
      for (const m of model.pins || []) {
        const g = sv("g", null, "vc-pin u-" + m.unit + " " + m.kind);
        const cx = ch.sx(m.mhz), cy = ch.sy(m.v);
        if (m.kind === "fused") g.append(sv("path", { d: "M" + cx.toFixed(1) + " " + (cy - 7).toFixed(1) + "L" + (cx + 7).toFixed(1) + " " + cy.toFixed(1) + "L" + cx.toFixed(1) + " " + (cy + 7).toFixed(1) + "L" + (cx - 7).toFixed(1) + " " + cy.toFixed(1) + "Z" }));
        else g.append(sv("circle", { cx: cx.toFixed(1), cy: cy.toFixed(1), r: 5.5 }));
        const t = sv("title");
        t.textContent = m.title;
        g.append(t);
        nodes.push(g);
      }
      fill(svg, nodes);
      fill(host, [svg]);
      ch.plot = { hit: hit };
      hit.addEventListener("pointermove", (e) => {
        const b = hit.getBoundingClientRect();
        if (b.width <= 0) return;
        const fx = (e.clientX - b.left) / b.width, fy = (e.clientY - b.top) / b.height;
        const mhz = ch.xDom[0] + fx * (ch.xDom[1] - ch.xDom[0]);
        ch.hover(Math.round(mhz / 100) * 100, ch.yDom[1] - fy * (ch.yDom[1] - ch.yDom[0]));
      });
      hit.addEventListener("pointerleave", () => ch.hover(null, null));
      svg.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Escape") return;
        e.preventDefault();
        if (e.key === "Escape") { ch.hover(null, null); return; }
        const cur = ch.hoverMhz === null ? Math.round((ch.xDom[0] + ch.xDom[1]) / 200) * 100 : ch.hoverMhz;
        ch.hover(clamp(cur + (e.key === "ArrowRight" ? 100 : -100), ch.xDom[0], ch.xDom[1]), null);
      });
      svg.addEventListener("blur", () => ch.hover(null, null));
      if (ch.hoverMhz !== null) ch.hover(ch.hoverMhz, null);
    };

    // Move the crosshair; the tooltip lists every drawn unit at that frequency (an exact measurement, or "~" for a straight-line estimate).
    ch.hover = function (mhz, yv) {
      ch.hoverMhz = mhz;
      if (!ch.plot || !ch.model) return;
      for (const k of Object.keys(ch.lineEls)) ch.lineEls[k].classList.remove("hot");
      ch.svg.classList.remove("has-hot");
      if (mhz === null) {
        ch.cross.setAttribute("x1", -10); ch.cross.setAttribute("x2", -10);
        ch.tip.setAttribute("transform", "translate(-999 -999)");
        ch.tip.replaceChildren();
        return;
      }
      const x = ch.sx(mhz);
      ch.cross.setAttribute("x1", x.toFixed(1)); ch.cross.setAttribute("x2", x.toFixed(1));
      const rows = [];
      for (const s of ch.model.series) {
        const at = interpAt(s.pts, mhz);
        if (at) rows.push({ s: s, v: at.v, exact: at.exact });
      }
      rows.sort((a, b) => b.v - a.v);
      let best = null;
      if (yv !== null) for (const r of rows) { const d = Math.abs(r.v - yv); if (best === null || d < best.d) best = { d: d, r: r }; }
      if (best && ch.lineEls[best.r.s.key]) { ch.lineEls[best.r.s.key].classList.add("hot"); ch.svg.classList.add("has-hot"); }
      const fmt = (v) => (ch.model.ymode === "mv" ? fmtMv(v, 1) : v.toFixed(3) + " V");
      const nodes = [];
      const w = 196, lh = 17, h = 30 + Math.max(rows.length, 1) * lh;
      nodes.push(sv("rect", { x: 0, y: 0, width: w, height: h, rx: 8 }, "vc-tipbg"));
      const head = sv("text", { x: 12, y: 19 }, "vc-tiphead");
      head.textContent = mhz / 100 + "× · " + mhz + " MHz";
      nodes.push(head);
      if (!rows.length) { const t = sv("text", { x: 12, y: 40 }, "vc-tiprow muted"); t.textContent = "no data at this frequency"; nodes.push(t); }
      rows.forEach((r, i) => {
        const y = 38 + i * lh;
        nodes.push(sv("rect", { x: 12, y: y - 9, width: 10, height: 4, rx: 2 }, "vc-tipsw u-" + r.s.unit + (r.s.dash ? " dash" : "")));
        const name = sv("text", { x: 28, y: y }, "vc-tiprow" + (best && best.r === r ? " strong" : ""));
        name.textContent = (r.s.label || r.s.unit);
        const val = sv("text", { x: w - 12, y: y, "text-anchor": "end" }, "vc-tiprow mono" + (best && best.r === r ? " strong" : ""));
        val.textContent = (r.exact ? "" : "~") + fmt(r.v);
        nodes.push(name, val);
      });
      ch.tip.replaceChildren(...nodes);
      const tx = x + 14 + w > ch.W - VC.R ? x - 14 - w : x + 14;
      ch.tip.setAttribute("transform", "translate(" + tx.toFixed(1) + " " + (VC.T + 6) + ")");
    };

    if (typeof ResizeObserver === "function") {
      let lastW = 0, timer = null;
      new ResizeObserver(() => {
        const w = Math.round(host.clientWidth);
        if (!ch.model || Math.abs(w - lastW) < 24) return;
        lastW = w;
        clearTimeout(timer);
        timer = setTimeout(() => ch.render(ch.model), 120);
      }).observe(host);
    }
    return ch;
  }

  // Why a V/F point could not be measured (contract 9 addendum: reason = max_turbo | cpufreq_granularity | active_cores; F3/F9).
  function reasonText(reason) {
    if (reason === "max_turbo") return "the point exists but this core's max turbo ratio is lower, so it never runs here";
    if (reason === "cpufreq_granularity") return "cpufreq caps cannot pin this ratio (the cap lands on a neighbour)";
    if (reason === "active_cores") return "reachable only with few active cores, and the measurement loads the whole unit";
    return "not reachable on this core";
  }

  // ---- Curves: measure, keep and compare V/F curves ----------------------------------------------------------------------------------
  const CV = {
    list: [],            // curves.list rows, newest first
    cache: {},           // id -> full record (curves.get)
    loaded: false,
    primary: null,       // id shown in the chart / table
    compare: null,       // id drawn dashed (or subtracted in difference mode)
    view: "chart",       // chart | table | library
    drawn: new Set(UNIT_IDS),
    delta: false, band: false,
    mode: "quick",       // quick | dense
    sel: new Set(UNIT_IDS),
    live: null,          // the record being filled by a running vf.measure
    tableDelta: false,   // table: difference to the baseline curve (mV)
    renaming: null,
    chart: null,
    paintQueued: false,
  };
  V.cv = CV;
  function presets() {   // the Favored preset exists only when favored cores were detected
    const list = [
      { value: "all", label: "All", units: UNIT_IDS },
      { value: "P", label: "P-cores", units: UNIT_IDS.slice(0, 8) },
      { value: "E", label: "E-clusters", units: UNIT_IDS.slice(8) },
    ];
    if (FAV.ids.length) list.push({ value: "fav", label: "Favored \u2605", units: FAV.ids.slice(), title: "The detected favored P-cores: " + FAV.ids.join(", ") + " (" + FAV_TEXT[FAV.source].replace(/^favored core /, "") + ")" });
    return list;
  }
  function renderPresets() {
    const ps = presets(), key = ps.map((x) => x.value + ":" + x.units.join(".")).join("|"), host = $("cv-presets");
    if (host.dataset.key !== key) {
      host.dataset.key = key;
      segmented(host, ps.map((x) => ({ value: x.value, label: x.label, title: x.title })), presetOf(CV.drawn), (v) => { CV.drawn = new Set(presets().find((x) => x.value === v).units); renderCurves(); });
    }
    syncSegmented(host, presetOf(CV.drawn));
  }
  const MEASURE_SECONDS = { quick: 120, dense: 600 };   // all 12 units (SPEC-v3 section 3)

  function tuningKind(c) {
    const t = isObj(c) ? c.tuning_state : null;
    if (isObj(t) && typeof t.kind === "string") return t.kind;
    if (typeof t === "string") return t;
    return isObj(c) && typeof c.tuning_state_kind === "string" ? c.tuning_state_kind : "unknown";
  }
  function fmtDate(x) {
    let d = null;
    if (typeof x === "string") d = new Date(x);
    else if (isNum(x)) d = new Date(x < 1e12 ? x * 1000 : x);
    if (!d || Number.isNaN(d.getTime())) return "n/a";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  function unitCount(c) {
    return Array.isArray(c.units) ? c.units.length : isObj(c.units) ? Object.keys(c.units).length : 0;
  }
  function curveTitle(c) { return (c.name || c.id) + " · " + fmtDate(c.created) + " · " + (c.mode || "?"); }

  // G-118: a measured point carries fused_point (1-based int, or "oc") when its ratio is one of the unit's fused V/F points; the key is absent for the
  // ratios in between. A record without the key anywhere (an older curve) draws every dot the same way, as before.
  function isFusedPoint(x) { return x === "oc" || (isNum(x) && x >= 1); }
  function fusedLabel(x) { return x === "oc" ? "OC point" : "fused point " + x; }

  // measured points of one unit: [{ratio, mhz, v, vmin, vmax, kept, total, status}] sorted by frequency; unmeasured / unreachable ones are left out
  function recPoints(rec, unit) {
    const u = isObj(rec) && isObj(rec.units) ? rec.units[unit] : null;
    const pts = isObj(u) && Array.isArray(u.points) ? u.points : [];
    return pts.filter((p) => isObj(p) && p.status !== "mismatch" && p.status !== "unreachable" && isNum(p.vid_v) && isNum(p.ratio))
      .map((p) => ({ ratio: p.ratio, mhz: isNum(p.mhz) ? p.mhz : p.ratio * 100, v: p.vid_v, vmin: p.vid_min, vmax: p.vid_max, kept: p.kept, total: p.total, status: p.status,
        fused: isFusedPoint(p.fused_point) ? p.fused_point : null }))
      .sort((a, b) => a.mhz - b.mhz);
  }
  function recAllPoints(rec, unit) {
    const u = isObj(rec) && isObj(rec.units) ? rec.units[unit] : null;
    return isObj(u) && Array.isArray(u.points) ? u.points.filter(isObj) : [];
  }

  async function loadCurves(selectNewest) {
    const { status, body } = await api("curves.list", {});
    if (!(status === 200 && body.ok && isObj(body.result) && Array.isArray(body.result.curves))) {
      CV.list = [];
      CV.loaded = true;
      setText("cv-status", "Stored curves unavailable: " + errText(body));
      renderCurves();
      return;
    }
    CV.list = body.result.curves.filter(isObj);
    CV.loaded = true;
    const ids = new Set(CV.list.map((c) => c.id));
    for (const k of Object.keys(CV.cache)) if (!ids.has(k)) delete CV.cache[k];
    if (CV.compare && !ids.has(CV.compare)) CV.compare = null;
    if (selectNewest) CV.primary = CV.list.length ? CV.list[0].id : null;   // a measurement just ended: show it
    else if (!CV.primary || !ids.has(CV.primary)) CV.primary = defaultCurveId();
    renderCurves();
    await Promise.all([CV.primary, CV.compare, baselineId(), TN_CURVE_WANT()].filter(Boolean).map(ensureRecord));
    renderCurves();
    if (T.words) refreshRows(); else renderTunePreview();
  }

  function baselineId() { const b = CV.list.find((c) => c.baseline === true); return b ? b.id : null; }

  // The curve shown first (G-123): the newest baseline, else the newest complete curve of all 12 units, else the newest (the list is newest first).
  function defaultCurveId() {
    const b = baselineId();
    if (b) return b;
    const all = CV.list.find((c) => c.complete !== false && Array.isArray(c.units) && UNIT_IDS.every((u) => c.units.indexOf(u) >= 0));
    if (all) return all.id;
    return CV.list.length ? CV.list[0].id : null;
  }

  // A large record comes back within the helper's 64 KiB line with omitted_units: [unit, ...]; the rest is fetched with curves.get {id, units} and merged.
  async function mergeOmitted(rec) {
    for (let round = 0; round < 12 && isObj(rec) && Array.isArray(rec.omitted_units) && rec.omitted_units.length; round++) {
      const want = rec.omitted_units.filter((u) => typeof u === "string");
      const { status, body } = await api("curves.get", { id: rec.id, units: want });
      if (!(status === 200 && body.ok && isObj(body.result) && isObj(body.result.units))) break;
      if (!isObj(rec.units)) rec.units = {};
      Object.assign(rec.units, body.result.units);
      const still = Array.isArray(body.result.omitted_units) ? body.result.omitted_units.filter((u) => want.indexOf(u) >= 0 && !(u in body.result.units)) : [];
      rec.omitted_units = still;
      if (still.length === want.length) break;  // no progress: stop asking
    }
    return rec;
  }

  async function ensureRecord(id) {
    if (!id || CV.cache[id]) return CV.cache[id] || null;
    const { status, body } = await api("curves.get", { id: id });
    if (status === 200 && body.ok && isObj(body.result)) {
      const rec = await mergeOmitted(body.result);
      CV.cache[id] = rec;
      return rec;
    }
    return null;
  }

  function primaryRec() { return CV.live || (CV.primary ? CV.cache[CV.primary] : null) || null; }
  function compareRec() { return CV.compare ? CV.cache[CV.compare] || null : null; }

  function unitLabelShort(u) { return u; }

  // ---- chart model ------------------------------------------------------------------------------------------------------------------
  function curvesModel() {
    const a = primaryRec(), b = compareRec();
    const model = { series: [], markers: [], band: null, ymode: CV.delta ? "mv" : "v", empty: null };
    if (!a) {
      model.empty = !CV.loaded ? "Loading stored curves." : CV.list.length ? "Loading the selected curve." : "No curve yet. Run a Quick measurement above; the chart fills as each point arrives.";
      return model;
    }
    if (CV.delta && !b) { model.empty = "Choose a second curve under Compare with to see the difference."; return model; }
    for (const u of UNIT_IDS) {
      if (!CV.drawn.has(u)) continue;
      const pa = recPoints(a, u);
      const label = u + favMark(u);
      if (CV.delta) {
        const pb = recPoints(b, u);
        const pts = pa.map((p) => { const at = interpAt(pb, p.mhz); return at ? { mhz: p.mhz, v: (p.v - at.v) * 1000 } : null; }).filter(Boolean);
        if (pts.length) model.series.push({ key: u, unit: u, label: label, pts: pts });
      } else {
        if (pa.length) model.series.push({ key: u, unit: u, label: label, pts: pa.map((p) => ({ mhz: p.mhz, v: p.v, fused: p.fused })), live: CV.live !== null && CV.liveHead === u });
        if (b) {
          const pb = recPoints(b, u);
          if (pb.length) model.series.push({ key: u + ":cmp", unit: u, label: u + " (compare)", pts: pb.map((p) => ({ mhz: p.mhz, v: p.v })), dash: true });
        }
      }
    }
    if (CV.band) {
      const solid = model.series.filter((s) => !s.dash);
      const xs = Array.from(new Set(solid.flatMap((s) => s.pts.map((p) => p.mhz)))).sort((x, y) => x - y);
      const band = [];
      for (const x of xs) {
        const vs = solid.map((s) => interpAt(s.pts, x)).filter(Boolean).map((r) => r.v);
        if (vs.length > 1) band.push({ mhz: x, lo: Math.min(...vs), hi: Math.max(...vs) });
      }
      model.band = band.length > 1 ? band : null;
    }
    if (!model.series.length) model.empty = CV.drawn.size ? "No measured points for the units drawn." : "No units drawn. Press a unit below, or pick a preset.";
    else if (!CV.delta) model.pins = curvePins(a);
    return model;
  }

  // Unreachable points as hollow rings (on the unit's last measured voltage: nothing was measured there), and on the baseline the fused top point (0x09).
  function curvePins(rec) {
    const pins = [];
    for (const u of UNIT_IDS) {
      if (!CV.drawn.has(u)) continue;
      const meas = recPoints(rec, u);
      for (const p of recAllPoints(rec, u)) {
        if (p.status !== "unreachable" || !isNum(p.ratio)) continue;
        const left = meas.filter((q) => q.ratio < p.ratio);
        if (!left.length) continue;
        pins.push({ unit: u, kind: "reach", mhz: p.ratio * 100, v: left[left.length - 1].v, title: u + " " + p.ratio + "×: " + reasonText(p.reason) + (isStr(p.reason) ? " (" + p.reason + ")" : "") + ". Drawn at the last measured voltage." });
      }
      const f = fusedOf(u);
      if (f && rec.id === baselineId() && meas.length) {
        const top = meas.find((q) => q.ratio === f.ratio);
        pins.push({ unit: u, kind: "fused", mhz: f.ratio * 100, v: f.v, title: u + " fused top point (cmd 0x09): " + f.v.toFixed(4) + " V at " + f.ratio + "×" +
          (top ? "; measured " + top.v.toFixed(4) + " V (" + fmtMv((top.v - f.v) * 1000, 1) + ")." : "; not measured on this curve.") });
      }
    }
    return pins;
  }

  function curveNotes() {
    const a = primaryRec();
    const isBase = a && a.id === baselineId() && CV.live === null;
    let base = "";
    if (isBase) {
      base = "Stock baseline: this curve is the fused V/F table as applied (offsets 0). " + (T.words && T.words.fused
        ? "Diamonds mark the top-point voltage read from the CPU (cmd 0x09); they should sit on the curve where it reaches that ratio."
        : "Read ratios & voltages (Tune) to draw the top-point voltage from the CPU (cmd 0x09) and cross-check it.");
    } else if (baselineId() && a) base = "Stock baseline: " + (CV.list.find((c) => c.id === baselineId()) || {}).name + ". Predictions in Tune are made on it.";
    else base = "No curve is marked as the stock baseline yet. Tune predicts the effective voltage on it; set one in the Library.";
    setText("cv-base-note", base);
    const stockPill = $("cv-stock");
    const isStock = !!a && CV.live === null && tuningKind(a) === "stock";
    stockPill.hidden = !isStock;
    if (isStock) { stockPill.textContent = "stock = fused table as applied"; stockPill.title = "Measured with every offset at 0: the voltages are the CPU's fused V/F table as the CPU applies it."; }
    const un = [];
    if (a) for (const u of UNIT_IDS) {
      if (!CV.drawn.has(u)) continue;
      const bad = recAllPoints(a, u).filter((p) => p.status === "unreachable" && isNum(p.ratio));
      if (bad.length) un.push(u + " " + bad.map((p) => p.ratio + "× (" + (isStr(p.reason) ? p.reason : "no reason given") + ")").join(", "));
    }
    setText("cv-unreach", un.length ? "Hollow rings are points that exist but were not reached: " + un.join("; ") + ". Hover a ring for the reason." : "");
  }

  function paintCurves() {
    if (CV.paintQueued) return;
    CV.paintQueued = true;
    requestAnimationFrame(() => { CV.paintQueued = false; if (V.tab === "curves") renderCurves(); });
  }

  function renderCurves() {
    renderCurveSelects();
    renderLegend($("cv-legend"), CV.drawn, (set) => { CV.drawn = set; renderCurves(); }, true);
    renderPresets();
    syncSegmented($("cv-view"), CV.view);
    $("cv-chart-wrap").hidden = CV.view !== "chart";
    $("cv-table-wrap").hidden = CV.view !== "table";
    $("cv-lib-wrap").hidden = CV.view !== "library";
    updateOptButtons();
    if (CV.view === "chart") { CV.chart.render(curvesModel()); curveNotes(); }
    else if (CV.view === "table") renderCurveTable();
    else renderLibrary();
    updateCurvesGate();
  }

  function presetOf(set) {
    for (const p of presets()) if (p.units.length === set.size && p.units.every((u) => set.has(u))) return p.value;
    return "";
  }

  // The unit toggles shared by Curves and the Tune preview. Press = toggle; Shift+press = show only that unit.
  function renderLegend(host, set, onChange, allowSolo) {
    fill(host, UNIT_IDS.map((u) => {
      const b = el("button", "lg-chip u-" + u);
      b.type = "button";
      b.setAttribute("aria-pressed", set.has(u) ? "true" : "false");
      b.title = unitLabel(u) + (isFav(u) ? ", " + favTitle(u) : "") + ". Press to toggle; Shift+press shows only this one.";
      b.append(el("span", "sw"), document.createTextNode(u + favMark(u)));
      b.addEventListener("click", (e) => {
        const next = new Set(set);
        if (e.shiftKey && allowSolo) { next.clear(); next.add(u); } else if (next.has(u)) next.delete(u); else next.add(u);
        onChange(next);
      });
      return b;
    }));
  }

  function renderCurveSelects() {
    const sel = $("cv-primary"), cmp = $("cv-compare");
    const opts = [];
    if (CV.live) opts.push({ v: "live", t: "Live measurement" });
    for (const c of CV.list) opts.push({ v: c.id, t: curveTitle(c) + (c.baseline ? " · baseline" : "") });
    if (!opts.length) opts.push({ v: "", t: "No stored curves" });
    fill(sel, opts.map((o) => { const op = el("option", "", o.t); op.value = o.v; return op; }));
    sel.value = CV.live ? "live" : (CV.primary || "");
    sel.disabled = !CV.list.length && !CV.live;
    const others = CV.list.filter((c) => c.id !== CV.primary);
    fill(cmp, [Object.assign(el("option", "", "None"), { value: "" })].concat(others.map((c) => Object.assign(el("option", "", curveTitle(c)), { value: c.id }))));
    cmp.value = CV.compare && others.some((c) => c.id === CV.compare) ? CV.compare : "";
    cmp.disabled = others.length === 0 || CV.live !== null;
  }

  function updateOptButtons() {
    const d = $("cv-opts").children;
    if (d.length < 2) return;
    d[0].setAttribute("aria-pressed", CV.band ? "true" : "false");
    d[1].setAttribute("aria-pressed", CV.delta ? "true" : "false");
    d[1].disabled = !CV.compare;
    d[1].title = CV.compare ? "Plot the difference between the two curves, in mV" : "Choose a second curve to compare with first";
  }

  // ---- table view -----------------------------------------------------------------------------------------------------------------------
  function bucket(f, n) { return clamp(Math.floor(f * n), 0, n - 1); }

  function renderCurveTable() {
    const a = primaryRec();
    const thead = $("cv-table").tHead, tbody = $("cv-table").tBodies[0];
    const base = baselineId() ? CV.cache[baselineId()] || null : null;
    const bd = $("cv-table-mode").children[1];
    const canDelta = Boolean(base) && a !== null && (CV.live !== null || CV.primary !== baselineId());
    if (bd) { bd.disabled = !canDelta; bd.title = canDelta ? "Show mV difference to the stock baseline" : (base ? "This is the baseline curve itself" : "Mark one stored curve as the stock baseline in the Library first"); }
    if (CV.tableDelta && !canDelta) CV.tableDelta = false;
    syncSegmented($("cv-table-mode"), CV.tableDelta ? "delta" : "v");
    if (!a) {
      fill(thead, []); fill(tbody, [row1("n/a: no curve selected yet.")]);
      setText("cv-table-note", "");
      return;
    }
    const ratios = Array.from(new Set(UNIT_IDS.flatMap((u) => recAllPoints(a, u).map((p) => p.ratio)).filter(isNum))).sort((x, y) => x - y);
    const hr = el("tr");
    const h0 = el("th", "", "Unit");
    h0.scope = "col";
    hr.append(h0);
    for (const r of ratios) { const th = el("th", "n", r + "×"); th.scope = "col"; hr.append(th); }
    fill(thead, [hr]);
    const cells = [];
    let lo = Infinity, hi = -Infinity, big = 0;
    const val = (u, p) => {
      if (p.status !== "ok" || !isNum(p.vid_v)) return null;
      if (!CV.tableDelta) return { v: p.vid_v };
      const at = interpAt(recPoints(base, u), isNum(p.mhz) ? p.mhz : p.ratio * 100);
      return at ? { v: (p.vid_v - at.v) * 1000, est: !at.exact } : null;
    };
    for (const u of UNIT_IDS) for (const p of recAllPoints(a, u)) { const x = val(u, p); if (x) { lo = Math.min(lo, x.v); hi = Math.max(hi, x.v); big = Math.max(big, Math.abs(x.v)); } }
    const rows = UNIT_IDS.filter((u) => isObj(a.units) && isObj(a.units[u])).map((u) => {
      const tr = el("tr");
      const th = el("th", "", u);
      th.scope = "row";
      tr.append(th);
      const byRatio = {};
      for (const p of recAllPoints(a, u)) byRatio[p.ratio] = p;
      for (const r of ratios) {
        const p = byRatio[r];
        if (!p) { tr.append(td("", "n vt-none")); continue; }
        const x = val(u, p);
        if (!x) {
          const why = p.status === "unreachable" ? "unreachable" : p.status === "mismatch" ? "mismatch" : CV.tableDelta ? "no baseline value" : "n/a";
          tr.append(td(p.status === "ok" ? "n/a" : "—", "n vt-na", u + " at " + r + "×: " + why + (p.status === "unreachable" ? ": " + reasonText(p.reason) + (isStr(p.reason) ? " (" + p.reason + ")" : "") : "")));
          continue;
        }
        let cls, text;
        if (CV.tableDelta) {
          text = (x.v > 0 ? "+" : x.v < 0 ? MINUS : "") + Math.abs(x.v).toFixed(0);
          const f = big > 0 ? x.v / big : 0;
          cls = "vt dv-" + (f < 0 ? "m" : "p") + bucket(Math.abs(f), 4);
        } else {
          text = x.v.toFixed(3);
          cls = "vt hv-" + bucket(hi > lo ? (x.v - lo) / (hi - lo) : 0.5, 10);
        }
        const kept = isNum(p.kept) && isNum(p.total) ? ", " + p.kept + " of " + p.total + " samples at target" : "";
        const range = isNum(p.vid_min) && isNum(p.vid_max) ? ", range " + p.vid_min.toFixed(3) + " to " + p.vid_max.toFixed(3) + " V" : "";
        tr.append(td(text + (x.est ? "~" : ""), "n " + cls, u + " at " + r + "× (" + (isNum(p.mhz) ? p.mhz.toFixed(0) : r * 100) + " MHz): " + p.vid_v.toFixed(3) + " V" + range + kept));
      }
      return tr;
    });
    fill(tbody, rows);
    setText("cv-table-note", CV.tableDelta ? "mV difference to the baseline curve (" + (base ? base.name : "") + "). Negative means lower voltage." : "Volts requested by the core at each ratio. Darker is lower.");
  }
  function row1(text) { const tr = el("tr"), c = td(text, "na"); c.colSpan = 8; tr.append(c); return tr; }

  // ---- library view -----------------------------------------------------------------------------------------------------------------------
  function renderLibrary() {
    $("cv-lib-empty").hidden = CV.list.length > 0;
    $("cv-lib").hidden = CV.list.length === 0;
    fill($("cv-lib-body"), CV.list.map((c) => {
      const tr = el("tr", c.id === CV.primary ? "is-current" : "");
      const nameCell = el("td", "lib-name");
      if (CV.renaming === c.id) {
        const inp = el("input", "inline-edit");
        inp.type = "text"; inp.maxLength = 64; inp.value = c.name || ""; inp.setAttribute("aria-label", "New name for " + (c.name || c.id));
        const save = el("button", "primary small", "Save"), no = el("button", "small", "Cancel");
        save.type = "button"; no.type = "button";
        const commit = async () => { await renameCurve(c.id, inp.value.trim()); };
        save.addEventListener("click", commit);
        no.addEventListener("click", () => { CV.renaming = null; renderLibrary(); });
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); else if (e.key === "Escape") { CV.renaming = null; renderLibrary(); } });
        nameCell.append(inp, save, no);
        setTimeout(() => inp.focus(), 0);
      } else {
        nameCell.append(el("span", "lib-title", c.name || c.id));
        if (c.baseline === true) nameCell.append(pill("stock baseline", "s-ok"));
        if (c.complete === false) nameCell.append(pill("partial", "s-unknown"));
        nameCell.title = c.id;
      }
      tr.append(nameCell, td(fmtDate(c.created)), td(c.mode || "n/a"));
      const kind = tuningKind(c);
      const tcell = el("td");
      const tp = pill(kind === "words" ? "tuned" : kind, kind === "stock" ? "s-ok" : kind === "words" ? "s-pass" : "s-neutral");
      tp.title = tuningWords(kind);
      tcell.append(tp);
      tr.append(tcell, td(unitCount(c), "n"));
      const act = el("td", "lib-actions");
      const mk = (label, title, fn, cls) => { const b = el("button", "small " + (cls || ""), label); b.type = "button"; b.title = title; b.addEventListener("click", fn); act.append(b); return b; };
      mk("View", "Show this curve in the chart", () => { CV.primary = c.id; CV.view = "chart"; ensureRecord(c.id).then(renderCurves); renderCurves(); });
      mk("Compare", "Overlay this curve on the one in the chart", () => { if (c.id !== CV.primary) { CV.compare = c.id; CV.view = "chart"; ensureRecord(c.id).then(renderCurves); renderCurves(); } });
      mk(c.baseline === true ? "Clear baseline" : "Set baseline", "A stock baseline is what the table can subtract (one at a time)", () => setBaseline(c));
      mk("Rename", "Rename this curve", () => { CV.renaming = c.id; renderLibrary(); });
      mk("Export", "Download this curve as JSON", () => exportCurve(c));
      mk("Delete", "Delete this stored curve", () => deleteCurve(c), "danger");
      tr.append(act);
      return tr;
    }));
  }

  async function renameCurve(id, name) {
    if (!name) { setText("cv-status", "A name cannot be empty."); return; }
    const { status, body } = await api("curves.update", { id: id, name: name });
    CV.renaming = null;
    setText("cv-status", status === 200 && body.ok ? "Renamed." : "Rename failed: " + errText(body));
    await loadCurves(false);
  }

  async function setBaseline(c) {
    const { status, body } = await api("curves.update", { id: c.id, baseline: c.baseline !== true });
    setText("cv-status", status === 200 && body.ok ? (c.baseline === true ? "Baseline cleared." : "\"" + (c.name || c.id) + "\" is now the stock baseline.") : "Failed: " + errText(body));
    await loadCurves(false);
  }

  async function deleteCurve(c) {
    const yes = await confirmDialog("Delete this curve?", "\"" + (c.name || c.id) + "\" (" + fmtDate(c.created) + ") is removed from the helper's store. This cannot be undone.", "Delete curve");
    if (!yes) return;
    const { status, body } = await api("curves.delete", { id: c.id });
    setText("cv-status", status === 200 && body.ok ? "Deleted." : "Delete failed: " + errText(body));
    if (CV.primary === c.id) CV.primary = null;
    await loadCurves(false);
  }

  async function exportCurve(c) {
    const rec = await ensureRecord(c.id);
    if (!rec) { setText("cv-status", "Could not load the curve to export it."); return; }
    const blob = new Blob([JSON.stringify(rec, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a");
    a.href = url;
    a.download = "arltuner-curve-" + String(c.id).replace(/[^A-Za-z0-9._-]/g, "_") + ".json";
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    setText("cv-status", "Exported " + a.download + ".");
  }

  // A small yes/no dialog; resolves false when dismissed with Esc.
  function confirmDialog(title, text, yesLabel) {
    return new Promise((resolve) => {
      const d = $("dlg-confirm");
      setText("dlg-confirm-title", title);
      setText("dlg-confirm-text", text);
      $("btn-confirm-yes").textContent = yesLabel;
      let done = false;
      const finish = (v) => { if (done) return; done = true; cleanup(); closeDialog(d); resolve(v); };
      const yes = () => finish(true), no = () => finish(false);
      const cleanup = () => { $("btn-confirm-yes").removeEventListener("click", yes); $("btn-confirm-no").removeEventListener("click", no); d.removeEventListener("close", no); };
      $("btn-confirm-yes").addEventListener("click", yes);
      $("btn-confirm-no").addEventListener("click", no);
      d.addEventListener("close", no);
      openDialog(d);
      $("btn-confirm-no").focus();
    });
  }

  // ---- measuring --------------------------------------------------------------------------------------------------------------------------
  function estimateText() {
    const n = CV.sel.size;
    const secs = MEASURE_SECONDS[CV.mode] * n / UNIT_IDS.length;
    return (CV.mode === "quick" ? "Quick" : "Dense") + ", " + n + (n === 1 ? " unit" : " units") + ", about " + (secs < 90 ? Math.round(secs) + " s" : Math.round(secs / 60) + " min");
  }

  function updateCurvesGate() {
    const measuring = S.opId !== null && S.opKind === "measure";
    const why = measuring ? "" : idleGate();
    const none = CV.sel.size === 0;
    $("btn-measure").disabled = measuring || why !== "" || none;
    $("btn-measure-cancel").disabled = !measuring || S.cancelling || S.sessionLost;
    $("cv-est").textContent = measuring ? "Measuring. Each unit is capped one ratio at a time and loaded; limits are restored when it ends or is cancelled."
      : why !== "" ? why : none ? "Select at least one unit." : "Ready: " + estimateText() + ". Raises the load on the selected cores for that time.";
    $("cv-run").hidden = !measuring;
  }

  function buildMeasureControls() {
    segmented($("cv-mode"), [{ value: "quick", label: "Quick", title: "The V/F point ratios of each unit, about 2 minutes for all 12" },
      { value: "dense", label: "Dense", title: "Every ratio from 8 to the unit's top, about 10 minutes for all 12" }], CV.mode, (m) => { CV.mode = m; updateCurvesGate(); });
    const host = $("cv-units");
    const chips = UNIT_IDS.map((u) => {
      const b = el("button", "unit-chip u-" + u);
      b.type = "button";
      b.dataset.unit = u;
      b.setAttribute("aria-pressed", "true");
      b.title = unitTileTitle(u);
      b.append(el("span", "uc-name", u + favMark(u)), el("span", "uc-cpus", unitKind(u) === "P" ? "cpu " + unitCpus(u)[0] : "cpu " + unitCpus(u)[0] + "–" + unitCpus(u)[3]));
      b.addEventListener("click", () => { if (CV.sel.has(u)) CV.sel.delete(u); else CV.sel.add(u); syncUnitChips(); updateCurvesGate(); });
      return b;
    });
    const quick = el("div", "chip-quick");
    for (const [label, units] of [["All", UNIT_IDS], ["P-cores", UNIT_IDS.slice(0, 8)], ["E-clusters", UNIT_IDS.slice(8)], ["None", []]]) {
      const b = el("button", "link", label);
      b.type = "button";
      b.addEventListener("click", () => { CV.sel = new Set(units); syncUnitChips(); updateCurvesGate(); });
      quick.append(b);
    }
    const p = el("div", "chips-p"), e = el("div", "chips-e");
    p.append(...chips.slice(0, 8)); e.append(...chips.slice(8));
    fill(host, [p, e, quick]);
  }
  function syncUnitChips() {
    for (const b of $("cv-units").querySelectorAll(".unit-chip")) b.setAttribute("aria-pressed", CV.sel.has(b.dataset.unit) ? "true" : "false");
  }

  async function startMeasure() {
    if (idleGate() !== "" || CV.sel.size === 0) return;
    const args = { mode: CV.mode };
    if (CV.sel.size < UNIT_IDS.length) args.units = UNIT_IDS.filter((u) => CV.sel.has(u));
    const nm = $("cv-name").value.trim();
    if (nm) args.name = nm;
    S.submitting = true;
    setText("cv-status", "Submitting...");
    updateGate();
    const { status, body } = await api("vf.measure", args);
    S.submitting = false;
    if (launched("measure", status, body)) {
      if (S.opId) {
        CV.live = { id: "live", name: nm || "Measuring", mode: CV.mode, units: {}, live: true };
        CV.liveHead = null;
        CV.view = "chart";
        $("cv-bar").max = 1; $("cv-bar").value = 0;
        setText("cv-progress", "starting");
        setText("cv-status", "Measuring (operation " + S.opId + "). The chart fills as each point arrives.");
        renderCurves();
      }
    } else {
      setText("cv-status", "Not started: " + (status === 429 ? "too many operations pending" : errText(body)));
    }
    updateGate();
  }

  function measureProgress(ev) {
    if (!(isNum(ev.step) && isNum(ev.of) && ev.of > 0)) return;
    $("cv-bar").max = ev.of; $("cv-bar").value = Math.min(ev.step, ev.of);
    const bits = [];
    if (typeof ev.unit === "string") bits.push(ev.unit);
    if (isNum(ev.ratio)) bits.push(ev.ratio + "×");
    bits.push("point " + ev.step + " of " + ev.of);
    if (isNum(ev.eta_s)) bits.push("about " + fmtClock(ev.eta_s) + " left");
    setText("cv-progress", bits.join(" · "));
  }

  function onVfPoint(ev) {
    const id = typeof ev.op_id === "string" ? ev.op_id : null;
    if (!S.opId && id && !S.doneIds.has(id)) adoptOp(id, "measuring");
    if (id !== S.opId || S.opKind !== "measure") return;
    if (!CV.live) CV.live = { id: "live", name: "Measuring", mode: "?", units: {}, live: true };
    if (typeof ev.unit !== "string" || UNIT_IDS.indexOf(ev.unit) < 0 || !isNum(ev.ratio)) return;
    const u = CV.live.units[ev.unit] || (CV.live.units[ev.unit] = { cpus: unitCpus(ev.unit), points: [] });
    if (ev.retry === true) { const i = u.points.findIndex((q) => q.ratio === ev.ratio); if (i >= 0) u.points.splice(i, 1); }   // G-118: a retry replaces the first attempt
    u.points.push({ ratio: ev.ratio, mhz: isNum(ev.mhz) ? ev.mhz : ev.ratio * 100, vid_v: isNum(ev.vid_v) ? ev.vid_v : null, status: typeof ev.status === "string" ? ev.status : "ok" });
    CV.liveHead = ev.status === "ok" ? ev.unit : CV.liveHead;
    paintCurves();
  }

  function sumPoints(rec) {
    let n = 0;
    if (isObj(rec) && isObj(rec.units)) for (const k of Object.keys(rec.units)) n += Array.isArray(rec.units[k].points) ? rec.units[k].points.length : 0;
    return n;
  }

  async function onMeasureResult(ev) {
    CV.live = null; CV.liveHead = null;
    if (ev.ok === true && isObj(ev.result)) {
      const rec = isObj(ev.result.record) ? ev.result.record : null;
      const id = typeof ev.result.curve_id === "string" ? ev.result.curve_id : (rec && typeof rec.id === "string" ? rec.id : null);
      if (rec && id) { if (!rec.id) rec.id = id; CV.cache[id] = await mergeOmitted(rec); }
      CV.primary = id;
      const problems = (Array.isArray(ev.result.problems) ? ev.result.problems : []).filter((x) => typeof x === "string");
      setText("cv-status", "Measured " + (rec ? sumPoints(CV.cache[id] || rec) + " points" : "the curves") + ". Stored as \"" + (rec && rec.name ? rec.name : id) + "\". " +
        (rec && isObj(rec.tuning_state) ? tuningWords(rec.tuning_state.kind) + ". " : "") + (problems.length ? "Problems: " + problems.join("; ") : ""));
      showProblems(problems);
      await loadCurves(false);
    } else if (typeof ev.server_error === "string") {
      setText("cv-status", ev.server_error === "helper_lost" ? "The helper was lost during the measurement. CPU limits are restored when it restarts." : "Failed: " + ev.server_error);
      renderCurves();
    } else {
      const code = isObj(ev.error) ? ev.error.code : "";
      if (code === "CANCELLED") {
        const cid = isObj(ev.error.detail) && typeof ev.error.detail.curve_id === "string" ? ev.error.detail.curve_id : null;
        setText("cv-status", "Cancelled. The points measured so far were kept as a partial curve; CPU limits are restored.");
        if (cid) { CV.primary = cid; await loadCurves(false); } else await loadCurves(true);
      } else {
        const cid = isObj(ev.error) && isObj(ev.error.detail) && typeof ev.error.detail.curve_id === "string" ? ev.error.detail.curve_id : null;
        setText("cv-status", "Failed: " + errText(ev) + (cid ? " The partial curve was kept." : ""));
        if (cid) { CV.primary = cid; await loadCurves(false); } else renderCurves();
      }
    }
  }

  function showProblems(list) {
    const host = $("cv-problems");
    host.hidden = list.length === 0;
    fill(host, list.map((x) => el("li", "", x)));
  }

  // The tuning state recorded with a curve: "words" (read this boot) or "unknown" (no read this boot, so offsets are not known).
  function tuningWords(kind) {
    return kind === "words" ? "Tuning at the time: read from the CPU" : kind === "stock" ? "Tuning at the time: stock = fused table as applied (every offset 0)" : "Tuning state unknown (no 'Read ratios & voltages' this boot)";
  }

  function initCurves() {
    CV.chart = makeVfChart($("cv-chart"), { label: "Measured V/F curves, volts against frequency" });
    buildMeasureControls();
    segmented($("cv-view"), [{ value: "chart", label: "Chart" }, { value: "table", label: "Table" }, { value: "library", label: "Library" }], CV.view, (v) => { CV.view = v; renderCurves(); });
    renderPresets();
    segmented($("cv-table-mode"), [{ value: "v", label: "Volts" }, { value: "delta", label: "Δ vs baseline (mV)" }], "v", (v) => { CV.tableDelta = v === "delta"; renderCurves(); });
    const opts = $("cv-opts");
    const band = el("button", "seg-btn", "Spread band"), delta = el("button", "seg-btn", "Δ mV");
    band.type = "button"; delta.type = "button";
    band.title = "Shade the lowest to highest voltage across the units drawn";
    band.addEventListener("click", () => { CV.band = !CV.band; renderCurves(); });
    delta.addEventListener("click", () => { if (CV.compare) { CV.delta = !CV.delta; renderCurves(); } });
    opts.append(band, delta);
    $("cv-primary").addEventListener("change", async (e) => {
      const v = e.target.value;
      if (v && v !== "live") { CV.primary = v; if (CV.compare === v) CV.compare = null; await ensureRecord(v); renderCurves(); }
    });
    $("cv-compare").addEventListener("change", async (e) => {
      CV.compare = e.target.value || null;
      if (!CV.compare) CV.delta = false;
      await ensureRecord(CV.compare);
      renderCurves();
    });
    $("btn-measure").addEventListener("click", startMeasure);
    $("btn-measure-cancel").addEventListener("click", cancelOp);
  }

  // ---- Tune: read the ratios and voltages, edit per unit, review a plan, apply it from the review dialog ----------------------------------------
  const MIXED = { mixed: true };
  const T = {
    words: null,          // normalised tune.read: {units: {id: {id, cpu, oc, points}}, ...}
    scope: "simple",      // simple (All P-cores / All E-clusters) | unit (12 rows)
    draft: {},            // unit id -> {curveMv?, pts: {ratio: mv}, vt?, ratio?}: only what differs from the words last read
    uiMode: {},           // row id -> "curve" | "points"
    expanded: new Set(),  // row ids whose per-point panel is open
    hex: false,
    curveId: null,        // stored curve behind the preview
    drawn: new Set(UNIT_IDS),
    plan: null,           // the plan under review: {res, intent, title, before, after, state, typed, issuedAt}
    refs: {},             // row id -> {controls...}
    showUnchanged: false,
    chart: null,
    turbo: {},            // kind ("P" | "E") -> the 8 bucket ratios being edited (only kinds that differ from the read); F5
    turboRefs: {},
    pc: {},               // unit -> prediction memo for the current paint (see predictUnit)
    basePending: false,
  };
  V.tn = T;
  const MV_TOL = 0.05, V_TOL = 0.0004;
  // Offsets are equal when they are the same 1/1024 V raw word (the hardware cannot tell -49.8 from -50 mV); other values compare within a tolerance.
  const same = (a, b, tol) => isNum(a) && isNum(b) && (tol === MV_TOL ? Math.round(a * 1.024) === Math.round(b * 1.024) : Math.abs(a - b) <= tol);

  function TN_CURVE_WANT() { return T.curveId; }
  function wUnit(u) { return T.words ? T.words.units[u] || null : null; }
  // The fused points only: a read point flagged oc_point (P2 above the fused count, e.g. EC1 P2 = 7 = 48x after an OC ratio 48) is the OC point, never an extra fused point (G-125).
  function curPts(u) { const w = wUnit(u); return w ? w.points.filter((p) => p.oc_point !== true && isNum(p.ratio) && isNum(p.offset_mv)) : []; }

  // ---- effective (draft over words) and current values --------------------------------------------------------------------------------
  function effPointMv(u, p) {
    const d = T.draft[u];
    if (d && isNum(d.curveMv)) return d.curveMv;
    if (d && d.pts[p.ratio] !== undefined) return d.pts[p.ratio];
    return p.offset_mv;
  }
  function curVt(u) { const w = wUnit(u); return w && w.oc && isNum(w.oc.v_target_v) ? w.oc.v_target_v : null; }
  function curRatio(u) { const w = wUnit(u); return w && w.oc && isNum(w.oc.ratio) ? w.oc.ratio : null; }
  function effVt(u) { const d = T.draft[u]; return d && d.vt !== undefined ? d.vt : curVt(u); }
  function effRatio(u) { const d = T.draft[u]; return d && d.ratio !== undefined ? d.ratio : curRatio(u); }

  function agg(values, tol) {
    const vs = values.filter(isNum);
    if (!vs.length) return null;
    return vs.every((v) => Math.abs(v - vs[0]) <= tol) ? vs[0] : MIXED;
  }
  const curveOf = (units, eff) => agg(units.flatMap((u) => curPts(u).map((p) => (eff ? effPointMv(u, p) : p.offset_mv))), MV_TOL);
  const pointOf = (units, ratio, eff) => agg(units.flatMap((u) => curPts(u).filter((p) => p.ratio === ratio).map((p) => (eff ? effPointMv(u, p) : p.offset_mv))), MV_TOL);
  const vtOf = (units, eff) => agg(units.map((u) => (eff ? effVt(u) : curVt(u))), V_TOL);
  const ratioOf = (units, eff) => agg(units.map((u) => (eff ? effRatio(u) : curRatio(u))), 0.5);

  // ---- CPU facts (SPEC-v3 section 9) ---------------------------------------------------------------------------------------------------
  // F1 point numbering as overclockers know it (SkatterBencher / XTU): points 1..n are fused (offset only), the OC point follows (P 9, E 7).
  function ocPointNo(units) { return Math.max(0, ...units.map((u) => curPts(u).length)) + 1; }
  function ocHeadText(units) { return "Point " + ocPointNo(units) + " \u00b7 OC"; }
  const OC_TIP = "ratio + adaptive/direct voltage; exists once an OC ratio above the top fused ratio is set (written through the P2=0 word)";

  function fusedOf(u) { return T.words && T.words.fused ? T.words.fused[u] || null : null; }
  function baselineRec() {
    const id = baselineId();
    if (!id) return null;
    if (CV.cache[id]) return CV.cache[id];
    if (!T.basePending && T.baseTried !== id) { T.baseTried = id; T.basePending = true; ensureRecord(id).then(() => { T.basePending = false; if (T.words) refreshRows(); }); }
    return null;
  }
  // The measured VID of the stock baseline at a point ratio; the fused top point (0x09) stands in where the baseline has no usable measurement there.
  function baseVid(u, ratio) {
    const rec = baselineRec();
    if (!rec) return null;
    const p = recAllPoints(rec, u).find((q) => q.ratio === ratio && q.status === "ok" && isNum(q.vid_v));
    if (p) return { v: p.vid_v, src: "baseline" };
    const f = fusedOf(u), rc = reachOf(u);
    return f && f.ratio === ratio && !(isNum(rc) && ratio > rc) ? { v: f.v, src: "fused" } : null;   // a top point above the unit's reach has no voltage to stand in for
  }
  // F4: effective V(n) = max(baseline VID(n) + offset(n), V(n-1)). byRatio[ratio] = {v, want, by, src} or null (no measured VID).
  function predictUnit(u) {
    if (T.pc[u]) return T.pc[u];
    const out = { byRatio: {}, any: false };
    let prev = null;
    for (const p of curPts(u).slice().sort((a, b) => a.ratio - b.ratio)) {
      const b = baseVid(u, p.ratio);
      if (!b) { out.byRatio[p.ratio] = null; continue; }
      const want = b.v + effPointMv(u, p) / 1000;
      let v = want, by = null;
      if (prev && want < prev.v - 1e-9) { v = prev.v; by = prev; }
      out.byRatio[p.ratio] = { v: v, want: want, by: by ? by.ratio : null, byPoint: by ? by.point : null, src: b.src, point: p.point };
      out.any = true;
      prev = { v: v, ratio: p.ratio, point: p.point };
    }
    T.pc[u] = out;
    return out;
  }
  // Effective voltage at any ratio: straight between the points, flat above the top one (E 47-48x follow point 6) and below the first.
  function predAt(u, ratio) {
    const pts = Object.keys(predictUnit(u).byRatio).map(Number).filter((r) => predictUnit(u).byRatio[r]).sort((a, b) => a - b).map((r) => ({ r: r, v: predictUnit(u).byRatio[r].v }));
    if (!pts.length) return null;
    if (ratio <= pts[0].r) return pts[0].v;
    if (ratio >= pts[pts.length - 1].r) return pts[pts.length - 1].v;
    const i = pts.findIndex((q) => q.r >= ratio);
    const a = pts[i - 1], b = pts[i];
    return a.v + ((b.v - a.v) * (ratio - a.r)) / (b.r - a.r);
  }
  // F3: the highest ratio a unit can hold (turbo.favored for P-cores, the E table top); null when unknown.
  function reachOf(u) {
    const w = T.words;
    if (unitKind(u) === "P") {
      if (w && w.turbo && w.turbo.favored && isNum(w.turbo.favored[u])) return w.turbo.favored[u];
      const rec = CV.cache[baselineId()] || null;
      if (rec) {
        const pts = recAllPoints(rec, u), ok = pts.filter((p) => p.status === "ok" && isNum(p.ratio)).map((p) => p.ratio);
        if (ok.length && pts.some((p) => p.status === "unreachable" && p.reason === "max_turbo")) return Math.max(...ok);
      }
      return null;
    }
    const t = w && w.turbo && w.turbo.E ? w.turbo.E.ratios : null;
    return t && t.length ? Math.max(...t) : null;
  }

  // F5: turbo bucket tables, read (T.words.turbo) and edited (T.turbo).
  function turboCur(k) { return T.words && T.words.turbo && T.words.turbo[k] ? T.words.turbo[k].ratios : null; }
  function turboEff(k) { return T.turbo[k] || turboCur(k); }
  function turboKinds() { return ["P", "E"].filter((k) => T.turbo[k] && turboCur(k) && T.turbo[k].some((x, i) => x !== turboCur(k)[i])); }
  function turboIntent() {
    const ks = turboKinds();
    if (!ks.length) return null;
    const o = {};
    for (const k of ks) o[k] = T.turbo[k].slice();
    return o;
  }
  // Setting bucket i keeps the table non-increasing: the buckets to its right come down with it (a higher one than its left neighbour is never offered).
  function setBucket(k, i, v) {
    const t = (turboEff(k) || []).slice();
    t[i] = v;
    for (let j = i + 1; j < t.length; j++) t[j] = Math.min(t[j], v);
    if (turboCur(k) && t.every((x, j) => x === turboCur(k)[j])) delete T.turbo[k]; else T.turbo[k] = t;
  }

  // ---- editing ----------------------------------------------------------------------------------------------------------------------------
  function ensureDraft(u) { return T.draft[u] || (T.draft[u] = { pts: {} }); }
  function tidy(u) {
    const d = T.draft[u];
    if (d && d.curveMv === undefined && !Object.keys(d.pts).length && d.vt === undefined && d.ratio === undefined) delete T.draft[u];
  }
  function setCurve(units, mv) {
    for (const u of units) {
      const d = ensureDraft(u);
      d.pts = {};
      const pts = curPts(u);
      if (pts.length && pts.every((p) => same(p.offset_mv, mv, MV_TOL))) delete d.curveMv; else d.curveMv = mv;
      tidy(u);
    }
  }
  function setPoint(units, ratio, mv) {
    for (const u of units) {
      const pt = curPts(u).find((p) => p.ratio === ratio);
      if (!pt) continue;
      const d = ensureDraft(u);
      if (isNum(d.curveMv)) {  // leaving whole-curve editing: spread the curve value over the points first
        for (const p of curPts(u)) if (!same(p.offset_mv, d.curveMv, MV_TOL)) d.pts[p.ratio] = d.curveMv;
        delete d.curveMv;
      }
      if (same(pt.offset_mv, mv, MV_TOL)) delete d.pts[ratio]; else d.pts[ratio] = mv;
      tidy(u);
    }
  }
  function setVt(units, v) {
    for (const u of units) { const d = ensureDraft(u); if (same(curVt(u), v, V_TOL)) delete d.vt; else d.vt = v; tidy(u); }
  }
  function setRatio(units, r) {
    for (const u of units) { const d = ensureDraft(u); if (curRatio(u) === r) delete d.ratio; else d.ratio = r; tidy(u); }
  }
  function unitsChanged() { return UNIT_IDS.filter((u) => T.draft[u]); }

  // The intent contract 9 describes: one row per changed unit, merged to "P" / "E" rows when the Simple scope made them identical.
  function buildIntent() {
    const rows = {};
    for (const u of unitsChanged()) {
      const d = T.draft[u];
      const row = { offset: null, v_target_v: d.vt === undefined ? null : Math.round(d.vt * 10000) / 10000, ratio: d.ratio === undefined ? null : d.ratio };
      if (isNum(d.curveMv)) row.offset = { kind: "curve", mv: d.curveMv };
      else if (Object.keys(d.pts).length) {
        const mv = {};
        for (const r of Object.keys(d.pts).sort((a, b) => a - b)) mv[r] = d.pts[r];
        row.offset = { kind: "points", mv: mv };
      }
      rows[u] = row;
    }
    const perUnit = UNIT_IDS.filter((u) => rows[u]).map((u) => Object.assign({ target: u }, rows[u]));
    const merged = [];
    for (const [g, units] of [["P", UNIT_IDS.slice(0, 8)], ["E", UNIT_IDS.slice(8)]]) {
      const present = units.filter((u) => rows[u]);
      if (!present.length) continue;
      const first = JSON.stringify(rows[units[0]] || null);
      if (present.length === units.length && units.every((u) => JSON.stringify(rows[u]) === first)) merged.push(Object.assign({ target: g }, rows[units[0]]));
      else return { scope: "unit", rows: perUnit };   // the helper takes P / E targets only in the simple scope and unit targets only in the unit scope
    }
    return T.scope === "simple" && merged.length ? { scope: "simple", rows: merged } : { scope: "unit", rows: perUnit };
  }

  // ---- policy ranges (D-032) -> dropdown options -------------------------------------------------------------------------------------------
  function snapMv(mv) { return Math.round(mv * 1.024) / 1.024; }   // the hardware offset is a 1/1024 V raw
  function snapV(v) { return Math.round(v * 1024) / 1024; }
  function offsetValues(cur) {
    const out = [];
    for (let v = POLICY.offsetMin; v <= POLICY.offsetMax; v += POLICY.offsetStep) out.push(v);
    if (isNum(cur) && !out.some((x) => same(x, cur, MV_TOL))) { out.push(Math.round(cur * 100) / 100); out.sort((a, b) => a - b); }
    return out;
  }
  function vtValues(kind, cur) {
    const out = [], max = POLICY.vtMax[kind];
    for (let i = 0; POLICY.vtMin + i * POLICY.vtStep <= max + 1e-9; i++) out.push(Math.round((POLICY.vtMin + i * POLICY.vtStep) * 1000) / 1000);
    if (isNum(cur) && !out.some((x) => same(x, cur, V_TOL))) { out.push(Math.round(cur * 10000) / 10000); out.sort((a, b) => a - b); }
    return out;
  }
  function fillSelect(sel, values, current, fmt, placeholder, tol) {
    const opts = [];
    if (placeholder) {
      const o = el("option", "", placeholder), n = el("option", "", "not set");  // "mixed" (units differ) and "not set" (the word reads 0) are shown, never chosen
      o.value = "__mixed"; n.value = "__none";
      o.disabled = n.disabled = true; o.hidden = n.hidden = true;
      opts.push(o, n);
    }
    for (const v of values) { const o = el("option", "", fmt(v) + (current !== null && same(v, current, tol) ? "  · now" : "")); o.value = String(v); opts.push(o); }
    fill(sel, opts);
  }

  // ---- rows ---------------------------------------------------------------------------------------------------------------------------------
  function rowDefs() {
    if (T.scope === "simple") return [{ id: "P", label: "All P-cores", sub: "8 cores", units: UNIT_IDS.slice(0, 8), kind: "P" },
      { id: "E", label: "All E-clusters", sub: "4 clusters, 16 cores", units: UNIT_IDS.slice(8), kind: "E" }];
    return UNIT_IDS.map((u) => ({ id: u, label: unitLabel(u), sub: unitKind(u) === "P" ? "cpu " + unitCpus(u)[0] : "cpus " + unitCpus(u)[0] + "–" + unitCpus(u)[3], units: [u], kind: unitKind(u) }));
  }

  function buildRows() {
    T.refs = {};
    if (!T.words) { fill($("tn-rows"), []); return; }
    const defs = rowDefs();
    const head = el("div", "tn-row tn-head");
    for (const t of ["Unit", "Voltage offset", "OC point: adaptive target voltage", "OC point: ratio", ""]) {
      const c = el("span", "tn-col", t);
      if (t.indexOf("OC point") === 0) c.title = OC_TIP;
      head.append(c);
    }
    const nodes = [head];
    for (const def of defs) {
      const row = el("div", "tn-rowwrap");
      const line = el("div", "tn-row k-" + def.kind);
      const name = el("div", "tn-unit u-" + (def.units.length === 1 ? def.units[0] : def.kind === "P" ? "P0" : "EC0"));
      const star = def.units.length === 1 ? favMark(def.units[0]) : "";
      const nm = el("span", "tn-name", def.label + star);
      if (star) nm.title = favTitle(def.units[0]);
      name.append(el("span", "kind-dot k-" + def.kind), nm, el("span", "tn-sub muted", def.sub));
      const now = el("span", "tn-now muted", "");
      name.append(now);
      // offset
      const offCell = el("div", "tn-cell");
      const mode = el("div", "seg seg-s");
      segmented(mode, [{ value: "curve", label: "Whole curve" }, { value: "points", label: "Per point" }], T.uiMode[def.id] || "curve", (m) => {
        T.uiMode[def.id] = m;
        if (m === "points") T.expanded.add(def.id); else T.expanded.delete(def.id);
        refreshRows();
      });
      const offSel = el("select", "tn-sel");
      offSel.setAttribute("aria-label", def.label + ": voltage offset for the whole curve");
      offSel.addEventListener("change", () => { setCurve(def.units, Number(offSel.value)); refreshRows(); });
      const offHint = el("span", "tn-hint muted");
      offCell.append(mode, offSel, offHint);
      // target
      const vtCell = el("div", "tn-cell");
      const vtSel = el("select", "tn-sel");
      vtSel.setAttribute("aria-label", def.label + ": adaptive target voltage at the OC point");
      vtSel.addEventListener("change", () => { setVt(def.units, Number(vtSel.value)); refreshRows(); });
      const vtHint = el("span", "tn-hint muted");
      vtCell.append(vtSel, vtHint);
      vtCell.title = OC_TIP;
      // ratio
      const rCell = el("div", "tn-cell");
      const rSel = el("select", "tn-sel");
      rSel.setAttribute("aria-label", def.label + ": OC ratio");
      rSel.addEventListener("change", () => { setRatio(def.units, Number(rSel.value)); refreshRows(); });
      const rHint = el("span", "tn-hint muted");
      rCell.append(rSel, rHint);
      rCell.title = OC_TIP;
      const undo = el("button", "small", "Undo");
      undo.type = "button";
      undo.title = "Put this row back to what was read";
      undo.addEventListener("click", () => { for (const u of def.units) delete T.draft[u]; refreshRows(); });
      line.append(name, offCell, vtCell, rCell, undo);
      const warn = el("ul", "tn-warn");
      warn.hidden = true;
      const panel = el("div", "tn-points");
      panel.id = "tn-pts-" + def.id;
      row.append(line, warn, panel);
      nodes.push(row);
      T.refs[def.id] = { def: def, row: row, line: line, now: now, mode: mode, offSel: offSel, offHint: offHint, vtSel: vtSel, vtHint: vtHint, rSel: rSel, rHint: rHint, undo: undo, panel: panel, warn: warn, oc: null,
        offCell: offCell, vtCell: vtCell, rCell: rCell, ptsKey: "", ptSels: {} };
    }
    fill($("tn-rows"), nodes);
    // option lists depend on the current values; build once per rows build
    for (const id of Object.keys(T.refs)) {
      const r = T.refs[id], def = r.def, kind = def.kind;
      const cur = curveOf(def.units, false);
      fillSelect(r.offSel, offsetValues(isNum(cur) ? cur : null), isNum(cur) ? cur : null, (v) => fmtMv(v), "mixed", MV_TOL);
      const vt = vtOf(def.units, false);
      fillSelect(r.vtSel, vtValues(kind, isNum(vt) ? vt : null), isNum(vt) ? vt : null, (v) => v.toFixed(Math.abs(v * 1000 - Math.round(v * 1000)) < 1e-6 ? 3 : 4) + " V", "mixed", V_TOL);
      const rt = ratioOf(def.units, false);
      const topPt = (u) => { const p = curPts(u); return p.length ? Math.max(...p.map((q) => q.ratio)) : POLICY.ratioMax[kind]; };
      const maxCur = Math.max(POLICY.ratioMax[kind], ...def.units.map((u) => (isNum(curRatio(u)) ? curRatio(u) : 0)));
      const minCur = Math.min(...def.units.map((u) => (isNum(curRatio(u)) ? curRatio(u) : topPt(u))));
      const rv = [];
      for (let q = minCur; q <= maxCur; q++) rv.push(q);
      fillSelect(r.rSel, rv, isNum(rt) ? rt : null, (v) => v + "×", "mixed", 0.5);
    }
    refreshRows();
  }

  function pointRatios(units) {
    return Array.from(new Set(units.flatMap((u) => curPts(u).map((p) => p.ratio)))).sort((a, b) => a - b);
  }

  function pointNo(units, ratio) {
    for (const u of units) { const p = curPts(u).find((q) => q.ratio === ratio); if (p && isNum(p.point)) return p.point; }
    return null;
  }

  function buildPointPanel(r) {
    const ratios = pointRatios(r.def.units);
    const key = ratios.join(",") + "|" + r.def.kind;
    if (r.ptsKey === key) return;
    r.ptsKey = key;
    r.ptSels = {};
    const cells = ratios.map((ratio) => {
      const lab = el("label", "tn-pt");
      const no = pointNo(r.def.units, ratio);
      lab.append(el("span", "tn-pt-ratio", "Point " + (no === null ? "?" : no) + " \u00b7 " + ratio + "\u00d7"), el("span", "tn-pt-tag muted", "fused \u00b7 offset only"));
      const sel = el("select", "tn-sel tn-sel-s");
      sel.setAttribute("aria-label", r.def.label + ": offset at point " + (no === null ? "?" : no) + ", " + ratio + "x");
      const cur = pointOf(r.def.units, ratio, false);
      fillSelect(sel, offsetValues(isNum(cur) ? cur : null), isNum(cur) ? cur : null, (v) => fmtMv(v), "mixed", MV_TOL);
      sel.addEventListener("change", () => { setPoint(r.def.units, ratio, Number(sel.value)); refreshRows(); });
      const eff = el("span", "tn-pt-eff", "");
      const badge = el("span", "tn-badge", "clamped to Vn\u22121");
      badge.hidden = true;
      const reach = el("span", "tn-pt-reach", "");
      reach.hidden = true;
      lab.append(sel, eff, badge, reach);
      r.ptSels[ratio] = { sel: sel, lab: lab, eff: eff, badge: badge, reach: reach };
      return lab;
    });
    if (!cells.length) cells.push(el("p", "na", "n/a: the read returned no V/F points for this unit."));
    // the OC point column: ratio + adaptive target, edited with the two controls above; beside it the fused top point
    const box = el("div", "tn-pt tn-oc");
    box.title = OC_TIP;
    const head = el("span", "tn-pt-ratio", ocHeadText(r.def.units));
    const tag = el("span", "tn-pt-tag muted", "ratio + voltage");
    const rText = el("span", "tn-oc-line", ""), vText = el("span", "tn-oc-line", ""), fText = el("span", "tn-oc-fused muted", "");
    box.append(head, tag, rText, vText, fText);
    r.oc = { box: box, ratio: rText, vt: vText, fused: fText };
    cells.push(box);
    r.eNote = null;
    if (r.def.kind === "E") { r.eNote = el("p", "tn-points-note muted", ""); cells.push(r.eNote); }
    fill(r.panel, cells);
  }

  function fmtVRange(vs, digits) {
    const lo = Math.min(...vs), hi = Math.max(...vs);
    return hi - lo < 0.0005 ? lo.toFixed(digits) + " V" : lo.toFixed(digits) + "\u2013" + hi.toFixed(digits) + " V";
  }

  // Predicted effective voltage, clamp badge and reach caption under each point dropdown (F3 / F4).
  // A prediction exists only for a unit that can reach the point, and only from the user's baseline curve (D-036); never from a CPU read, so it carries no "(fused)" claim for a point above reach.
  function refreshPointFacts(r, ratio) {
    const def = r.def, c = r.ptSels[ratio];
    const base = CV.cache[baselineId()] || null;
    const un = def.units.filter((u) => { const rc = reachOf(u); return isNum(rc) && ratio > rc; });
    const ok = def.units.filter((u) => un.indexOf(u) < 0);
    const infos = ok.map((u) => ({ u: u, pr: predictUnit(u).byRatio[ratio] || null })).filter((x) => x.pr);
    c.eff.title = "";
    if (!ok.length || !baselineId()) c.eff.textContent = "";   // above reach: only the caption below; no baseline: one hint in the card header
    else if (!base) c.eff.textContent = "loading the baseline";
    else if (!infos.length) {
      const bp = recAllPoints(base, ok[0]).find((q) => q.ratio === ratio);
      const why = bp && bp.status === "unreachable" ? bp.reason : null;
      c.eff.textContent = why === "cpufreq_granularity" ? "cannot be pinned" : why === "max_turbo" ? "not measured" : "no measured voltage";
      c.eff.title = "The baseline curve has no usable measurement at this ratio" + (why ? ": " + reasonText(why) : "") + ".";
    }
    else {
      const src = infos.every((x) => x.pr.src === "fused") ? " (fused)" : "";
      c.eff.textContent = "\u2192 " + fmtVRange(infos.map((x) => x.pr.v), 3) + src;
      c.eff.title = "Predicted effective voltage on your baseline curve: baseline VID + offset" + (src ? "; the fused top point read from the CPU stands in for the baseline here" : "") + ". Not a CPU read.";
    }
    const cl = infos.filter((x) => x.pr.by !== null);
    c.badge.hidden = cl.length === 0;
    if (cl.length) {
      c.badge.textContent = "clamped to Vn\u22121" + (def.units.length > 1 ? " (" + cl.map((x) => x.u).join(", ") + ")" : "");
      const x = cl[0];
      c.badge.title = x.u + " " + ratio + "\u00d7 would be " + fmtV(x.pr.want, 3) + ", below the " + x.pr.by + "\u00d7 point; the hardware holds every point at or above the one before it, so it lands on " + fmtV(x.pr.v, 3) + ".";
    }
    c.lab.classList.toggle("unreach", un.length === def.units.length && un.length > 0);
    c.reach.hidden = un.length === 0;
    if (un.length) {
      const tops = Array.from(new Set(un.map(reachOf))), topTxt = tops.length === 1 ? ", tops at " + tops[0] + "\u00d7" : "";
      const noun = def.kind === "E" ? (un.length === 1 ? "this cluster" : "these clusters") : (un.length === 1 ? "this core" : "these cores");
      c.reach.textContent = un.length === def.units.length ? "not reachable on " + noun + topTxt : "not reachable on " + un.join(", ") + topTxt;
      c.reach.title = "The point still shapes the table and can be edited, but " + (un.length === 1 ? un[0] : "these units") + " top" + (un.length === 1 ? "s" : "") + " out at " + reachOf(un[0]) + "\u00d7 (read from the CPU).";
    }
  }

  // The card-header note for every predicted voltage on the page: where it comes from, or how to get one.
  function renderPredNote() {
    const id = baselineId(), rec = id ? baselineRec() : null;
    setText("tn-pred-note", !id ? "Measure a stock curve (Curves) to see predicted voltages."
      : !rec ? "Loading the baseline curve."
        : "Predicted voltages (\u2192 V) are computed on baseline \u2018" + (rec.name || rec.id) + "\u2019 measured " + String(rec.created || "").slice(0, 10) + ": baseline VID plus your offset. They are not read from the CPU.");
  }

  // The E turbo table decides which ratios above the top fused point exist; the note is derived from the table that was read, nothing fixed.
  function eTurboNote(units) {
    const t = turboCur("E");
    if (!t) return "";
    const u = units.find((x) => curPts(x).length), pts = u ? curPts(u) : [];
    if (!pts.length) return "";
    const top = pts.reduce((x, y) => (y.ratio > x.ratio ? y : x)), hi = Math.max(...t);
    if (hi <= top.ratio) return "";
    return "E turbo table: up to " + hi + "\u00d7 (" + t[t.length - 1] + "\u00d7 all-core); ratios above point " + top.point + " (" + top.ratio + "\u00d7) run on point " + top.point + "\u2019s voltage.";
  }

  function refreshOcCell(r) {
    if (!r.oc) return;
    const units = r.def.units;
    const rt = ratioOf(units, true), vt = vtOf(units, true), chR = !same(ratioOf(units, false), rt, 0.5) && isNum(rt), chV = isNum(vt) && !same(vtOf(units, false), vt, V_TOL);
    const fused = units.map(fusedOf);
    const atFused = isNum(rt) && !chR && fused.every((f) => f && f.ratio === rt);
    r.oc.ratio.textContent = "ratio " + (isNum(rt) ? rt + "\u00d7" + (atFused ? " = fused top (no OC point)" : "") : rt === MIXED ? "mixed" : "not set");
    r.oc.vt.textContent = "target " + (isNum(vt) ? vt.toFixed(3) + " V" : vt === MIXED ? "mixed" : "not set");
    r.oc.box.classList.toggle("changed", chR || chV);
    r.oc.box.classList.toggle("unset", !isNum(rt) && !isNum(vt));
    const known = fused.filter(Boolean);
    r.oc.fused.textContent = known.length ? "fused top point (0x09): " + known[0].ratio + "\u00d7, " + fmtVRange(known.map((f) => f.v), 4) : "fused top point: not read";
  }

  // F8 warnings from the values as they will be written.
  function refreshRowWarn(r) {
    const def = r.def, msgs = [];
    const list = (us) => us.join(", ");
    const pos = def.units.filter((u) => curPts(u).some((p) => effPointMv(u, p) > 0.049));
    if (pos.length) msgs.push(["warn", list(pos) + ": a positive offset raises the voltage above the fused curve."]);
    fill(r.warn, msgs.map((m) => el("li", m[0], m[1])));
    r.warn.hidden = msgs.length === 0;
  }

  // fmt (optional): a value the dropdown does not offer (a loaded profile's -28 mV on a 5 mV grid) is added as an extra option, so the control shows it instead of going blank.
  function setSel(sel, value, tol, fmt) {
    for (const o of Array.from(sel.options)) if (o.dataset.extra) o.remove();
    if (value === MIXED) sel.value = "__mixed";
    else if (value === null) sel.value = "__none";
    else sel.value = String(value);
    if (value !== MIXED && value !== null && sel.value !== String(value)) {  // a value between grid steps (never picked, only "now")
      const o = Array.from(sel.options).find((x) => x.value !== "__mixed" && x.value !== "__none" && same(Number(x.value), value, tol));
      if (o) sel.value = o.value;
      else if (fmt && isNum(value)) {
        const extra = Object.assign(el("option", "", fmt(value) + "  \u00b7 edit"), { value: String(value) });
        extra.dataset.extra = "1";
        const after = Array.from(sel.options).find((x) => x.value !== "__mixed" && x.value !== "__none" && Number(x.value) > value);
        sel.insertBefore(extra, after || null);
        sel.value = extra.value;
      }
    }
  }

  function nowText(def) {
    const c = curveOf(def.units, false);
    const lows = def.units.flatMap((u) => curPts(u).map((p) => p.offset_mv));
    const off = isNum(c) ? fmtMv(c) : lows.length ? fmtMv(Math.min(...lows)) + " to " + fmtMv(Math.max(...lows)) : "n/a";
    const vt = vtOf(def.units, false), rt = ratioOf(def.units, false);
    const f = def.units.length === 1 ? fusedOf(def.units[0]) : null;
    return "now " + off + " · " + (isNum(vt) ? vt.toFixed(3) + " V" : vt === MIXED ? "target mixed" : "target n/a") + " · " + (isNum(rt) ? rt + "×" : rt === MIXED ? "ratio mixed" : "ratio n/a") +
      (f ? " · fused top " + f.ratio + "× " + f.v.toFixed(4) + " V" : "");
  }

  function refreshRows() {
    T.pc = {};
    for (const id of Object.keys(T.refs)) {
      const r = T.refs[id], def = r.def;
      const changed = def.units.some((u) => T.draft[u]);
      r.row.classList.toggle("changed", changed);
      r.undo.disabled = !changed;
      r.now.textContent = nowText(def) + (T.hex && def.units.length === 1 && wUnit(def.units[0]) && wUnit(def.units[0]).oc && wUnit(def.units[0]).oc.word ? " · " + wUnit(def.units[0]).oc.word : "");
      const mode = T.uiMode[id] || (isNum(curveOf(def.units, false)) ? "curve" : "points");
      T.uiMode[id] = mode;
      syncSegmented(r.mode, mode);
      const open = mode === "points";
      r.panel.hidden = !open;
      r.offSel.hidden = open;
      r.offHint.hidden = open;
      if (open) buildPointPanel(r);
      const effCurve = curveOf(def.units, true), curCurve = curveOf(def.units, false);
      setSel(r.offSel, effCurve, MV_TOL, (v) => fmtMv(v));
      r.offCell.classList.toggle("changed", !open && !(effCurve === curCurve || (isNum(effCurve) && isNum(curCurve) && same(effCurve, curCurve, MV_TOL))));
      r.offHint.textContent = isNum(effCurve) && !same(effCurve, curCurve, MV_TOL) ? "writes " + fmtMv(snapMv(effCurve), 2) : "";
      let nPtChanged = 0;
      for (const ratio of Object.keys(r.ptSels)) {
        const p = r.ptSels[ratio];
        const e = pointOf(def.units, Number(ratio), true), c = pointOf(def.units, Number(ratio), false);
        setSel(p.sel, e, MV_TOL, (v) => fmtMv(v));
        const ch = !(e === c || (isNum(e) && isNum(c) && same(e, c, MV_TOL)));
        p.lab.classList.toggle("changed", ch);
        if (ch) nPtChanged++;
      }
      if (open) r.offCell.classList.toggle("changed", nPtChanged > 0);
      if (open) for (const ratio of Object.keys(r.ptSels)) refreshPointFacts(r, Number(ratio));
      if (open) refreshOcCell(r);
      if (open && r.eNote) { const nt = eTurboNote(def.units); r.eNote.textContent = nt; r.eNote.hidden = !nt; }
      refreshRowWarn(r);
      const effVtv = vtOf(def.units, true), curVtv = vtOf(def.units, false);
      setSel(r.vtSel, effVtv, V_TOL, (v) => v.toFixed(4) + " V");
      const vtCh = !(effVtv === curVtv || (isNum(effVtv) && isNum(curVtv) && same(effVtv, curVtv, V_TOL)));
      r.vtCell.classList.toggle("changed", vtCh);
      r.vtHint.textContent = vtCh && isNum(effVtv) ? "writes " + snapV(effVtv).toFixed(4) + " V" : "";
      const effR = ratioOf(def.units, true), curR = ratioOf(def.units, false);
      setSel(r.rSel, effR, 0.5, (v) => v + "\u00d7");
      const rCh = !(effR === curR || (isNum(effR) && isNum(curR) && effR === curR));
      r.rCell.classList.toggle("changed", rCh);
      r.rHint.textContent = rCh && isNum(effR) ? ratioHint(def.kind, effR) : (!rCh && curR === null ? "OC ratio reads 0 (not set)" : "");
    }
    updateTuneFoot();
    renderPredNote();
    renderVccia();
    renderTurbo();
    renderTunePreview();
  }

  // The turbo ratio tables (0x1AD P, 0x650 E; bucket 0 = fewest active cores) from tune.read: a ratio at or below bucket 0 does not raise any table.
  function ratioHint(kind, r) {
    const t = T.words && T.words.tables ? T.words.tables[kind === "P" ? "p" : "e"] : null;
    if (!t || !t.length) return "turbo tables not read" + (T.words && T.words.tablesNote ? ": " + T.words.tablesNote : "");
    return r > t[0] ? "above the top turbo bucket (" + t[0] + "\u00d7): the plan raises the table" : "inside the turbo table (top bucket " + t[0] + "\u00d7): no table write";
  }

  function updateTuneFoot() {
    const n = unitsChanged(), tk = turboKinds();
    const msg = $("tn-foot-msg");
    const bits = [];
    if (n.length) bits.push(n.length + (n.length === 1 ? " unit" : " units") + " changed: " + n.join(", "));
    if (tk.length) bits.push("turbo table " + tk.join(" and ") + " changed");
    msg.textContent = bits.length ? bits.join("; ") + ". Nothing is written until you review and confirm." : "No changes yet. Pick a value to start.";
    msg.className = "tn-foot-msg" + (bits.length ? " dirty" : "");
    $("btn-tn-discard").disabled = bits.length === 0;
    updateTuneGate();
  }

  // ---- F6: predicted VccIA ---------------------------------------------------------------------------------------------------------------------
  // The highest voltage the units may request when `which` buckets are the ones in force: "all" = every core loaded (last bucket), "few" = 1-2 cores (first bucket).
  // VID only, on the user's baseline: the regulator headroom and the ring floor are not read from this system, so they are not added here (D-036).
  function vidFor(which) {
    const tp = turboEff("P"), te = turboEff("E");
    if (!tp || !te) return null;
    const idx = which === "all" ? 7 : 0;
    const best = { P: null, E: null };
    for (const u of UNIT_IDS) {
      const k = unitKind(u), want = (k === "P" ? tp : te)[idx], rc = reachOf(u);
      const ratio = isNum(rc) ? Math.min(want, rc) : want;
      const v = predAt(u, ratio);
      if (v !== null && (best[k] === null || v > best[k].v)) best[k] = { unit: u, ratio: ratio, v: v };
    }
    const parts = [best.P, best.E].filter(Boolean);
    if (!parts.length) return null;
    return { top: parts.reduce((a, b) => (b.v > a.v ? b : a)), parts: best };
  }

  // The board Vcore sensor, live from telemetry (a per-board table decides whether the channel exists).
  function renderTuneVcore() {
    const x = isObj(V.last) ? reading(V.last.vcore, 3) : { ok: false };
    setText("tn-vccia-live", x.ok ? "Board Vcore sensor now: " + x.text + " V (live telemetry)." : "");
  }

  function renderVccia() {
    const card = $("tn-vccia-card");
    card.hidden = !T.words || !baselineId();   // without a baseline the one hint is in the Tune header (tn-pred-note)
    if (card.hidden) return;
    renderTuneVcore();
    const line = $("tn-vccia-line"), list = $("tn-vccia-list");
    const all = vidFor("all"), few = vidFor("few");
    if (!all) { line.textContent = "Turbo tables not read, so the highest VID cannot be predicted."; fill(list, []); return; }
    const rec = CV.cache[baselineId()];
    setText("tn-vccia-basis", rec ? "predicted on baseline \u2018" + (rec.name || rec.id) + "\u2019 measured " + String(rec.created || "").slice(0, 10) : "loading the baseline");
    fill(line, [document.createTextNode("Highest predicted VID \u2248 "), el("strong", "", all.top.v.toFixed(3) + " V"), document.createTextNode(", set by " + all.top.unit + " at " + all.top.ratio + "\u00d7 with every core loaded")]);
    const add = (label, part, top) => {
      const li = el("li", top ? "top" : "");
      li.append(el("span", "vc-who", label), el("span", "vc-val mono", part ? fmtV(part.v, 3) : "n/a"), el("span", "vc-at muted", part ? part.unit + " at " + part.ratio + "\u00d7" : ""));
      return li;
    };
    fill(list, [add("P-cores, highest", all.parts.P, all.top === all.parts.P), add("E-clusters, highest", all.parts.E, all.top === all.parts.E)]);
    const fewTxt = few ? " With only 1\u20132 cores loaded " + few.top.unit + " may run " + few.top.ratio + "\u00d7, highest VID \u2248 " + few.top.v.toFixed(3) + " V." : "";
    setText("tn-vccia-note", "A prediction from your baseline, not a measurement. Lowering a unit that does not set the maximum does not lower the highest VID." + fewTxt);
  }

  // ---- F5: turbo ratio tables -------------------------------------------------------------------------------------------------------------------
  function turboLabel(k, i) {
    const w = T.words && T.words.turbo ? T.words.turbo[k] : null;
    const n = w && w.counts && isNum(w.counts[i]) ? w.counts[i] : null;
    return n === null ? "bucket " + i : n + (n === 1 ? " core" : " cores");
  }

  function turboStepChart(k) {
    const W = 380, H = 128, L = 30, R = 10, Tm = 10, B = 22;
    const cur = turboCur(k), eff = turboEff(k);
    const all = cur.concat(eff);
    const lo = Math.min(...all) - 1, hi = Math.max(...all) + 1;
    const x = (i) => L + (i * (W - L - R)) / 7, y = (v) => Tm + ((hi - v) * (H - Tm - B)) / (hi - lo);
    const svg = sv("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": (k === "P" ? "P-core" : "E-core") + " turbo ratio by active-core bucket: " + eff.map((v, i) => turboLabel(k, i) + " " + v + "x").join(", ") }, "turbo-svg");
    const step = (arr) => arr.map((v, i) => (i ? "L" + x(i).toFixed(1) + " " + y(arr[i - 1]).toFixed(1) + "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join("");
    const nodes = [];
    for (const v of [Math.ceil(lo), Math.floor(hi)]) {
      nodes.push(sv("line", { x1: L, x2: W - R, y1: y(v).toFixed(1), y2: y(v).toFixed(1) }, "tb-grid"));
      const t = sv("text", { x: L - 5, y: (y(v) + 4).toFixed(1), "text-anchor": "end" }, "tb-tick");
      t.textContent = v + "\u00d7";
      nodes.push(t);
    }
    for (let i = 0; i < 8; i++) {
      const t = sv("text", { x: x(i).toFixed(1), y: H - 6, "text-anchor": "middle" }, "tb-tick");
      t.textContent = String(T.words.turbo[k].counts && isNum(T.words.turbo[k].counts[i]) ? T.words.turbo[k].counts[i] : i);
      nodes.push(t);
    }
    if (cur.some((v, i) => v !== eff[i])) nodes.push(sv("path", { d: step(cur) }, "tb-line tb-cur"));
    nodes.push(sv("path", { d: step(eff) }, "tb-line k-" + k));
    eff.forEach((v, i) => nodes.push(sv("circle", { cx: x(i).toFixed(1), cy: y(v).toFixed(1), r: 3.5 }, "tb-dot k-" + k + (v !== cur[i] ? " changed" : ""))));
    fill(svg, nodes);
    return svg;
  }

  function renderTurbo() {
    const card = $("tn-turbo-card");
    card.hidden = !T.words;
    if (!T.words) return;
    const host = $("tn-turbo");
    const tb = T.words.turbo;
    if (!tb) {
      T.turboRefs = {};
      fill(host, [el("p", "muted", "Turbo ratio tables not read" + (T.words.tablesNote ? ": " + T.words.tablesNote : ".") + " They cannot be edited without a read.")]);
      setText("tn-turbo-help", "");
      return;
    }
    if (!T.turboRefs.P) buildTurbo();
    for (const k of ["P", "E"]) {
      const r = T.turboRefs[k], eff = turboEff(k), cur = turboCur(k);
      const dirty = turboKinds().indexOf(k) >= 0;
      r.undo.disabled = !dirty;
      r.box.classList.toggle("changed", dirty);
      for (let i = 0; i < 8; i++) {
        const sel = r.sels[i], hi = i === 0 ? POLICY.ratioMax[k] : eff[i - 1];
        const opts = [];
        for (let v = 8; v <= hi; v++) opts.push(Object.assign(el("option", "", v + "\u00d7" + (v === cur[i] ? "  \u00b7 now" : "")), { value: String(v) }));
        fill(sel, opts);
        sel.value = String(eff[i]);
        r.cells[i].classList.toggle("changed", eff[i] !== cur[i]);
      }
      fill(r.chart, [turboStepChart(k)]);
      const fav = T.words.turbo.favored || {};
      const favIds = Object.keys(fav).sort();
      r.note.textContent = k === "P"
        ? (favIds.length ? "Favored ratio per core (0x1C) sits on top of the table: " + favIds.map((u) => u + " " + fav[u] + "\u00d7").join(", ") + "." : "Favored ratios per core were not read.")
        : "E-clusters have no favored core." + (eTurboNote(UNIT_IDS.slice(8)) ? " " + eTurboNote(UNIT_IDS.slice(8)) : "");
    }
    setText("tn-turbo-note", "bucket = number of active cores, fewest first");
    setText("tn-turbo-help", "Ratios may only fall as more cores are active. A value higher than the bucket to its left is not offered, and lowering a bucket brings the ones on its right down with it. A plan that breaks the order is refused with TURBO_ORDER. Counts are read-only.");
  }

  function buildTurbo() {
    const blocks = [];
    T.turboRefs = {};
    for (const k of ["P", "E"]) {
      const box = el("div", "turbo-kind k-" + k);
      const head = el("div", "turbo-head");
      head.append(el("span", "kind-dot k-" + k), el("h3", "", k === "P" ? "P-cores" : "E-clusters"), el("span", "muted", k === "P" ? "0x1AD, counts 0x1AE" : "0x650, counts 0x651"));
      const undo = el("button", "small", "Undo");
      undo.type = "button";
      undo.addEventListener("click", () => { delete T.turbo[k]; refreshRows(); });
      head.append(el("span", "grow"), undo);
      const chart = el("div", "turbo-chart");
      const sels = [], cells = [];
      const row = el("div", "turbo-sels");
      for (let i = 0; i < 8; i++) {
        const lab = el("label", "tn-pt");
        lab.append(el("span", "tn-pt-ratio", turboLabel(k, i)));
        const sel = el("select", "tn-sel tn-sel-s");
        sel.setAttribute("aria-label", (k === "P" ? "P-core" : "E-cluster") + " turbo ratio with " + turboLabel(k, i) + " active");
        sel.addEventListener("change", () => { setBucket(k, i, Number(sel.value)); refreshRows(); });
        lab.append(sel);
        sels.push(sel);
        cells.push(lab);
        row.append(lab);
      }
      const note = el("p", "muted turbo-note", "");
      box.append(head, chart, row, note);
      blocks.push(box);
      T.turboRefs[k] = { box: box, undo: undo, chart: chart, sels: sels, cells: cells, note: note };
    }
    fill($("tn-turbo"), blocks);
  }

  // ---- tune.read result ----------------------------------------------------------------------------------------------------------------------
  function normPoint(p, idx) {
    if (!isObj(p)) return null;
    return { point: isNum(p.point) ? p.point : idx, ratio: isNum(p.ratio) ? p.ratio : null, offset_mv: isNum(p.offset_mv) ? p.offset_mv : null,
      v_target_v: isNum(p.v_target_v) ? p.v_target_v : null, word: typeof p.word === "string" ? p.word : null, mode: typeof p.mode === "string" ? p.mode : null,
      oc_point: p.oc_point === true };
  }

  // tune.read (G-112): units[].id, `tune` = {unit: {cpus: [{cpu, points: [{point 0 = OC word, 1..n, ratio, offset_mv, v_target_v, mode}]}]}} (the decoded
  // copy; null if the line was too large), `tables` = {p: [8 bytes], e: [8 bytes]} (0x1AD / 0x650, bucket 0 first) or null, with `tables_note`. An OC word
  // that reads 0 has ratio and v_target_v null (unset). Without `tune` the raw units[].cpus[].oc / points are decoded the same way.
  function normWords(r) {
    const units = {};
    const tune = isObj(r.tune) ? r.tune : null;
    for (const u of Array.isArray(r.units) ? r.units : []) {
      const id = unitIdOf(u);
      if (!id) continue;
      let pts = null, cpu = null;
      const dec = tune && isObj(tune[id]) && Array.isArray(tune[id].cpus) && isObj(tune[id].cpus[0]) ? tune[id].cpus[0] : null;
      if (dec && Array.isArray(dec.points)) { cpu = dec.cpu; pts = dec.points.map((p) => normPoint(p, isNum(p.point) ? p.point : 0)).filter(Boolean); }
      else {
        const c = isObj(u) && Array.isArray(u.cpus) && isObj(u.cpus[0]) ? u.cpus[0] : null;
        if (!c) continue;
        cpu = c.cpu;
        pts = (isObj(c.oc) ? [normPoint(c.oc, 0)] : []).concat((Array.isArray(c.points) ? c.points : []).map((p, i) => normPoint(p, isNum(p.point) ? p.point : i + 1))).filter(Boolean);
      }
      const raw = isObj(u) && Array.isArray(u.cpus) && isObj(u.cpus[0]) ? u.cpus[0] : null;   // raw words, for the hex view
      for (const p of pts) {
        if (p.word === null && raw) {
          const src = p.point === 0 ? raw.oc : (Array.isArray(raw.points) ? raw.points.find((q) => isObj(q) && q.point === p.point) : null);
          if (isObj(src) && typeof src.word === "string") p.word = src.word;
        }
      }
      const fusedN = id.charAt(0) === "P" ? 8 : 6;   // F1: P points 1..8 are fused (OC = 9), E 1..6 (OC = 7); an older read without the flag is judged by the point number
      for (const p of pts) if (p.point > fusedN) p.oc_point = true;
      units[id] = { id: id, cpu: cpu, oc: pts.find((p) => p.point === 0) || null, points: pts.filter((p) => p.point !== 0) };
    }
    const tables = isObj(r.tables) && Array.isArray(r.tables.p) && Array.isArray(r.tables.e) ? { p: r.tables.p.filter(isNum), e: r.tables.e.filter(isNum) } : null;
    // contract 9 addendum (2026-10-11): fused {cpu: {word, ratio, v}} (cmd 0x09) and turbo {P: {ratios[8], counts[8]}, E: {...}, favored: {cpu: ratio}}
    const unitOfCpu = (c) => { const n = Number(c); return !Number.isInteger(n) || n < 0 ? null : n < 8 ? "P" + n : n < 24 ? "EC" + Math.floor((n - 8) / 4) : null; };
    const fused = {};
    if (isObj(r.fused)) for (const k of Object.keys(r.fused)) {
      const id = unitOfCpu(k), f = r.fused[k];
      if (id && !fused[id] && isObj(f) && isNum(f.v) && isNum(f.ratio)) fused[id] = { word: typeof f.word === "string" ? f.word : null, ratio: f.ratio, v: f.v };
    }
    const tb = (o) => (isObj(o) && Array.isArray(o.ratios) && o.ratios.length === 8 && o.ratios.every(isNum) ? { ratios: o.ratios.slice(), counts: Array.isArray(o.counts) ? o.counts.filter(isNum) : null } : null);
    let turbo = null;
    if (isObj(r.turbo) && tb(r.turbo.P) && tb(r.turbo.E)) {
      turbo = { P: tb(r.turbo.P), E: tb(r.turbo.E), favored: {} };
      if (isObj(r.turbo.favored)) for (const k of Object.keys(r.turbo.favored)) { const id = unitOfCpu(k); if (id && id.charAt(0) === "P" && isNum(r.turbo.favored[k])) turbo.favored[id] = r.turbo.favored[k]; }
    } else if (tables && tables.p.length === 8 && tables.e.length === 8) turbo = { P: { ratios: tables.p.slice(), counts: null }, E: { ratios: tables.e.slice(), counts: null }, favored: {} };
    return { units: units, raw: r, accepted: r.accepted_match, fused: fused, turbo: turbo, complete: r.complete !== false, simulator: r.simulator === true, at: Date.now(), fromPlan: false,
      tables: tables, tablesNote: typeof r.tables_note === "string" ? r.tables_note : null, truncated: r.tune_truncated === true };
  }

  function appliedFromWords(w) {
    // the OC word (P2 = 0) carries the whole-curve offset, so it counts too: a curve offset on it is tuning even when every point reads 0
    const offs = UNIT_IDS.filter((u) => w.units[u]).flatMap((u) => w.units[u].points.concat(w.units[u].oc ? [w.units[u].oc] : []).map((p) => p.offset_mv)).filter(isNum);
    if (!offs.length) return { kind: "unknown", text: "unknown", title: "The read returned no offsets." };
    if (offs.every((x) => Math.abs(x) < MV_TOL)) return { kind: "stock", text: "stock", title: "Every offset read back as 0 mV." };
    const grp = (ids) => agg(ids.filter((u) => w.units[u]).flatMap((u) => w.units[u].points.concat(w.units[u].oc ? [w.units[u].oc] : []).map((p) => p.offset_mv)), MV_TOL);
    const f = (g) => (isNum(g) ? fmtMv(g) : g === MIXED ? "mixed" : "n/a");
    return { kind: "words", text: "tuned (P " + f(grp(UNIT_IDS.slice(0, 8))) + ", E " + f(grp(UNIT_IDS.slice(8))) + ")", title: "Offsets read from the CPU this boot." };
  }

  function agoText(ms) {
    const d = Math.max(0, Math.round((Date.now() - ms) / 1000));
    return d < 60 ? "just now" : d < 3600 ? Math.round(d / 60) + " min ago" : d < 86400 ? Math.round(d / 3600) + " h ago" : Math.round(d / 86400) + " d ago";
  }
  // Where the numbers on the page come from: a fresh read from the CPU, the helper's stored read of this boot, or that read updated by an apply's read-back.
  function renderProvenance() {
    const w = T.words;
    if (!w) { setText("tn-prov", ""); return; }
    const when = isNum(w.readAt) ? clockText(w.readAt) + " (" + agoText(w.readAt) + ")" : null;
    let t;
    if (w.prov === "applied") t = "Updated from the apply\u2019s read-back" + (when ? " at " + when : " (time unknown)") + ". Not a fresh read from the CPU: press Re-read ratios & voltages for one.";
    else if (w.prov === "stored") t = "Showing the helper\u2019s earlier read" + (when ? " from " + when : " (time unknown)") + " (no mailbox operations used).";
    else t = "Read from the CPU" + (when ? " at " + when : " (time unknown)") + ".";
    setText("tn-prov", t + (favMismatch() ? " The favored cores named by the system differ from the 0x1C favored ratios of this read." : ""));
  }
  function readTime(r, meta) {   // epoch ms of the read: the helper's read_at (ISO UTC), else the stored read's epoch seconds, else unknown
    const iso = isObj(meta) && typeof meta.read_at === "string" ? meta.read_at : isObj(r) && typeof r.read_at === "string" ? r.read_at : null;
    const t = iso ? Date.parse(iso) : NaN;
    if (isNum(t)) return t;
    return isObj(meta) && isNum(meta.at) ? meta.at * 1000 : null;
  }
  function clockText(ms) { return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }

  // meta (tune.last): {at (epoch s), applied_at (epoch s | null)}: the words are the helper's stored read of this boot, not a fresh one.
  function setWords(r, meta) {
    const w = normWords(r);
    if (!Object.keys(w.units).length) { setText("tn-status", "The read returned no V/F units for the 12 tuning units. Nothing to edit."); return; }
    FAV.read = favFromRead(w.turbo);
    recomputeFav();
    T.words = w;
    T.draft = {};
    T.turbo = {};
    T.turboRefs = {};
    T.uiMode = {};
    T.expanded = new Set();
    const a = appliedFromWords(w);
    setAppliedChip(a.kind, a.text, a.title);
    $("tn-steps").hidden = true;
    $("tn-console").hidden = false;
    $("tn-preview-card").hidden = false;
    $("tn-words-card").hidden = false;
    w.readAt = readTime(r, meta);
    w.stored = isObj(meta);
    w.appliedSince = isObj(meta) && isNum(meta.applied_at);
    w.prov = w.appliedSince ? "applied" : w.stored ? "stored" : "read";
    if (w.appliedSince) w.readAt = meta.applied_at * 1000;
    setText("tn-sub", "Dropdowns show what the CPU held at the read below and only offer what the policy allows.");
    renderProvenance();
    renderReadCost();
    setText("btn-tune-read", "Re-read ratios & voltages");
    renderScopeNote();
    buildRows();
    renderWordsTable();
    setTuneCurveDefault();
  }

  function renderScopeNote() {
    setText("tn-scope-note", T.scope === "simple" ? "All P-cores move together, and all E-clusters move together." : "12 curves (8 P-cores + 4 E-clusters), each on its own.");
  }

  function renderWordsTable() {
    const w = T.words;
    if (!w) return;
    const rows = [];
    for (const id of UNIT_IDS) {
      const u = w.units[id];
      if (!u) continue;
      const one = (p) => {
        if (T.hex) return (isNum(p.ratio) ? p.ratio + "x " : "") + (typeof p.word === "string" ? p.word : "no raw word");
        return (p.oc_point === true ? "OC point " : "") + (isNum(p.ratio) ? p.ratio + "x " : "") + fmtMv(p.offset_mv, 1) + (p.point === 0 ? (isNum(p.v_target_v) ? " @ " + p.v_target_v.toFixed(3) + " V" : ", target not set") : "");
      };
      const row = el("tr");
      const oc = u.oc;
      row.append(td(isNum(u.cpu) ? u.cpu : "n/a", "n"), td(unitLabel(id)), td(oc ? (isNum(oc.ratio) || T.hex ? one(oc) : "ratio not set, " + one(oc)) : "n/a", oc ? "mono" : "na"),
        td(u.points.length ? u.points.map(one).join(", ") : "n/a", "mono"));
      rows.push(row);
    }
    fill($("vf-body"), rows);
    renderTables(w);
    setText("tn-words-note", (w.simulator ? "simulator data · " : "") + (w.complete ? "complete read" : "incomplete read") + (w.truncated ? " · E-cluster copies trimmed to fit" : "") +
      (T.hex ? " · raw words" : " · decoded to mV; E clusters show their first CPU"));
    renderAccepted(w);
  }

  // Turbo ratio tables (tune.read via rdmsr): the effective limits an OC ratio is judged against. Bucket 0 = the fewest active cores.
  function renderTables(w) {
    const host = $("tn-tables");
    if (!w.tables) { fill(host, [el("p", "muted", "Turbo ratio tables not read" + (w.tablesNote ? ": " + w.tablesNote : ".") + " Asking for an OC ratio is then judged without them.")]); return; }
    const t = el("table", "grid num tbl-turbo");
    t.append(el("caption", "sr", "Turbo ratio limit tables by active-core bucket"));
    const hr = el("tr");
    hr.append(Object.assign(el("th", "", "Turbo table"), { scope: "col" }));
    for (let i = 0; i < 8; i++) hr.append(Object.assign(el("th", "n", "bucket " + i), { scope: "col" }));
    const head = el("thead"), body = el("tbody");
    head.append(hr);
    for (const [k, label] of [["p", "P-cores (0x1AD)"], ["e", "E-cores (0x650)"]]) {
      const tr = el("tr");
      tr.append(el("th", "", label));
      for (let i = 0; i < 8; i++) tr.append(td(isNum(w.tables[k][i]) ? w.tables[k][i] + "×" : "n/a", "n mono"));
      body.append(tr);
    }
    t.append(head, body);
    fill(host, [el("h3", "dlg-sub", "Turbo ratio tables"), t, el("p", "muted", "An OC ratio at or below bucket 0 raises no table, so asking for a ratio the table already reads at bucket 0 changes nothing there. Only a ratio above bucket 0 makes the plan write a table." + (w.tablesNote ? " " + w.tablesNote : ""))]);
  }

  function renderAccepted(w) {
    const m = w.accepted;
    const sum = $("acc-summary");
    if (m === undefined) { sum.textContent = "This helper does not report an accepted-state comparison."; return; }
    if (m === null) { sum.textContent = "No accepted state installed, so there is nothing to compare with."; return; }
    if (!Array.isArray(m)) { sum.textContent = "The accepted-state comparison has an unexpected shape."; return; }
    const bad = m.filter((x) => !isObj(x) || x.match !== true);
    sum.textContent = (bad.length === 0 ? "Matches the accepted state on all " + m.length + " compared cores." : "Differs from the accepted state on " + bad.length + " of " + m.length + " cores: cpu " +
      bad.map((x) => (isObj(x) ? x.cpu : "?")).join(", ") + ".") + (w.complete ? "" : " The read was incomplete, so cores that were not read are missing.");
  }

  // ---- gate and buttons ------------------------------------------------------------------------------------------------------------------------
  function updateTuneGate() {
    const why = gateReason();
    $("btn-tune-read").disabled = why !== "";
    const reading = S.opId !== null && S.opKind === "read";
    $("tn-reason").textContent = reading ? "" : why !== "" ? why : "Ready. This is the only part of Tune that sends mailbox reads.";
    $("tn-run").hidden = !reading;
    $("btn-tune-cancel").disabled = !reading || S.cancelling || S.sessionLost;
    setText("tn-used", S.opsUsed === null ? "n/a" : S.opsUsed);
    const idle = idleGate() === "";
    $("btn-tn-plan").disabled = !idle || (unitsChanged().length === 0 && turboKinds().length === 0);
    $("btn-tn-reset").disabled = !idle || !T.words;
    $("btn-tn-restore").disabled = !idle || !T.words;
    $("btn-pr-save").disabled = unitsChanged().length === 0 || !PROFILE_RE.test($("pr-name").value);
    for (const b of document.querySelectorAll(".pr-act")) b.disabled = !idle;
  }

  async function requestPlan(intent, onError) {
    if (idleGate() !== "") return;
    S.submitting = true;
    updateGate();
    const { status, body } = await api("tune.plan", { intent: intent });
    S.submitting = false;
    updateGate();
    if (status === 200 && body.ok && isObj(body.result) && typeof body.result.plan_id === "string") {
      openPlanDialog(body.result, intent);
    } else {
      const chk = isObj(body.error) && isObj(body.error.detail) ? body.error.detail.check : null;
      if (chk === "NO_READ") {
        onError("This helper session has not read the ratios and voltages, and this plan needs them. Read ratios & voltages first (" + readCostText() + ", each request is a WRMSR).");
        selectTab("tune", false);
        $("btn-tune-read").focus();
        return;
      }
      if (isObj(intent) && intent.turbo !== undefined && isObj(body.error) && body.error.code === "BAD_REQUEST") {
        onError("This helper does not accept turbo table edits (intent.turbo). Undo the turbo table change to review the rest. " + errText(body));
        return;
      }
      onError(errText(body) + (isObj(body.error) && body.error.code === "REFUSED" && chk === "POLICY_RANGE" ? ". The policy refused this; change the value." : "") +
        (chk === "TURBO_ORDER" ? ". Each bucket must be at or below the one to its left (fewer active cores): lower the higher bucket, or raise the ones before it." : ""));
    }
  }

  function describeIntent(intent) {
    if (isObj(intent) && intent.reset !== undefined) return "Reset offsets to 0";
    if (isObj(intent) && intent.restore !== undefined) return "Restore the accepted state";
    if (isObj(intent) && typeof intent.profile === "string") return "Apply profile " + intent.profile;
    return "Review changes";
  }

  // ---- the review dialog -------------------------------------------------------------------------------------------------------------------------
  function normState(x) {
    const out = {};
    const put = (u, e, i) => {
      if (UNIT_IDS.indexOf(u) < 0) return;
      if (e === null) { (out[u] || (out[u] = {}))[i] = { unread: true, point: i, ratio: null, offset_mv: null, v_target_v: null }; return; }
      if (!isObj(e)) return;
      const pt = e.oc === true ? 0 : isNum(e.point) ? e.point : i;
      (out[u] || (out[u] = {}))[pt] = { point: pt, ratio: isNum(e.ratio) ? e.ratio : null, offset_mv: isNum(e.offset_mv) ? e.offset_mv : null, v_target_v: isNum(e.v_target_v) ? e.v_target_v : null,
        effective_v: isNum(e.effective_v) ? e.effective_v : null, clamped_by: isNum(e.clamped_by) ? e.clamped_by : null };
    };
    if (Array.isArray(x)) x.forEach((e, i) => { if (isObj(e)) put(e.unit, e, i); });
    else if (isObj(x)) {
      for (const u of Object.keys(x)) {
        const v = x[u];
        if (Array.isArray(v)) v.forEach((e, i) => put(u, e, i));
        else if (isObj(v) && Object.keys(v).length && Object.keys(v).every((k) => /^\d+$/.test(k))) { for (const k of Object.keys(v)) put(u, v[k], Number(k)); }
        else if (isObj(v)) { if (isObj(v.oc)) put(u, Object.assign({}, v.oc, { point: 0 }), 0); (Array.isArray(v.points) ? v.points : []).forEach((e, i) => put(u, e, i + 1)); }
      }
    }
    return out;
  }

  function planDiff(res) {
    const b = normState(res.before), a = normState(res.after);
    const units = [];
    for (const u of UNIT_IDS) {
      if (!a[u] && !b[u]) continue;
      const pts = Array.from(new Set(Object.keys(a[u] || {}).concat(Object.keys(b[u] || {})).map(Number))).sort((x, y) => x - y);
      const rows = pts.map((pt) => {
        const bb = (b[u] || {})[pt] || null, aa = (a[u] || {})[pt] || null;
        const ch = !bb || !aa || bb.unread === true || bb.ratio !== aa.ratio || !(same(bb.offset_mv, aa.offset_mv, MV_TOL) || (bb.offset_mv === null && aa.offset_mv === null)) ||
          !(same(bb.v_target_v, aa.v_target_v, V_TOL) || (bb.v_target_v === null && aa.v_target_v === null));
        return { pt: pt, b: bb, a: aa, changed: ch };
      });
      units.push({ unit: u, rows: rows, changes: rows.filter((r) => r.changed).length });
    }
    return units;
  }

  function arrow(bv, av, fmt, unread) {
    if (bv === null && av === null) return "n/a";
    if (bv === null && unread) return "unread → " + fmt(av);
    if (bv === null || av === null) return fmt(bv === null ? av : bv);
    return fmt(bv) === fmt(av) ? fmt(av) : fmt(bv) + " → " + fmt(av);
  }

  function openPlanDialog(res, intent) {
    const P = { res: res, intent: intent, title: describeIntent(intent), state: "review", issued: Date.now(), diff: planDiff(res), readback: null };
    T.plan = P;
    setText("dlg-plan-title", P.title);
    renderPlanBody();
    renderPlanFoot();
    openDialog($("dlg-plan"));
    updateModalGate();
    const first = document.getElementById("plan-cancel");  // focus the safe button: Enter must never be one keystroke from a write
    if (first) first.focus();
  }

  function stepKinds(steps) {
    const n = {};
    for (const x of steps) { const k = isObj(x) && typeof x.kind === "string" ? x.kind.replace(/^write_/, "").replace(/_/g, " ") : "step"; n[k] = (n[k] === undefined ? 0 : n[k]) + 1; }
    return Object.keys(n).map((k) => n[k] + " " + k).join(", ");
  }

  function planExpired() { return T.plan && isNum(T.plan.res.expires_s) && Date.now() - T.plan.issued > T.plan.res.expires_s * 1000; }

  function renderPlanBody() {
    const P = T.plan, res = P.res;
    const nodes = [];
    const changedUnits = P.diff.filter((d) => d.changes > 0);
    const tbk = isObj(P.intent) && isObj(P.intent.turbo) ? Object.keys(P.intent.turbo) : [];
    const head = el("div", "plan-head");
    head.append(el("span", "plan-big", changedUnits.length ? changedUnits.length + (changedUnits.length === 1 ? " unit changes" : " units change") : tbk.length ? "Turbo table changes" : "Nothing changes"),
      el("span", "muted", changedUnits.map((d) => d.unit).concat(tbk.map((k) => "turbo " + k)).join(", ")));
    nodes.push(head);
    const vc = isObj(res.vccia) && isNum(res.vccia.predicted_v) ? res.vccia : null;
    if (vc) nodes.push(vcciaNode(vc));
    const ops = el("p", "plan-ops");
    ops.append(el("strong", "", "Each request is a WRMSR. "), document.createTextNode("This plan needs " + (isNum(res.ops_needed) ? res.ops_needed : "an unknown number of") + " operations" +
      (isNum(res.ops_left) ? " of the " + res.ops_left + " left this boot" : "") +
      (Array.isArray(res.steps) && res.steps.length && res.steps.every((x) => isObj(x) && x.kind === "write_msr") ? ". Plain MSR table writes, no mailbox request; each is read back on two CPUs." : ". One mailbox transaction at a time, each read back.")));
    nodes.push(ops);
    const facts = [];
    if (isNum(res.writes)) facts.push(res.writes + " write steps");
    if (isNum(res.reads)) facts.push(res.reads + " read-only requests");
    if (Array.isArray(res.steps)) facts.push("steps: " + stepKinds(res.steps));
    if (facts.length) nodes.push(el("p", "muted plan-facts", facts.join(" · ") + (res.steps_truncated === true ? " (the step list is shortened to fit one message)" : "")));
    if (res.words_assumed === true) nodes.push(el("p", "plan-warn-line", "Some words were not read in this helper session, so the plan assumed their current values. Read the ratios and voltages first for a plan based on what the CPU holds."));
    const gates = Array.isArray(res.gates) ? res.gates.filter(isObj) : [];
    if (gates.length) {
      const g = el("div", "plan-gates");
      for (const x of gates) g.append(pill(String(x.name) + ": " + String(x.status), "s-unknown"));
      nodes.push(g);
    }
    const warns = planWarnings(res);
    if (warns.length) {
      const ul = el("ul", "plan-warn");
      for (const x of warns) {
        const li = el("li", x.cls);
        li.dataset.code = x.code;
        if (x.label) { const b = el("span", "tn-badge", x.label); b.title = x.code; li.append(b, document.createTextNode(" ")); }
        li.append(document.createTextNode(x.text));
        ul.append(li);
      }
      nodes.push(ul);
    }
    if (P.diff.length) {
      const tog = el("button", "link", T.showUnchanged ? "Hide unchanged points" : "Show unchanged points");
      tog.type = "button";
      tog.addEventListener("click", () => { T.showUnchanged = !T.showUnchanged; renderPlanBody(); });
      nodes.push(tog);
    }
    const tbl = el("table", "grid num plan-table");
    tbl.append(el("caption", "sr", "Before and after for each unit and V/F point"));
    const th = el("thead"), thr = el("tr");
    const showEff = P.diff.some((d) => d.rows.some((r) => r.a && isNum(r.a.effective_v)));
    for (const [t, c] of [["Unit", ""], ["Point", ""], ["Ratio", "n"], ["Offset", "n"], ["Target voltage", "n"]].concat(showEff ? [["Effective voltage", "n"]] : [])) { const h = el("th", c, t); h.scope = "col"; thr.append(h); }
    th.append(thr);
    const tb = el("tbody");
    for (const d of P.diff) {
      if (!d.changes && !T.showUnchanged) continue;
      d.rows.forEach((r) => {
        if (!r.changed && !T.showUnchanged) return;
        const tr = el("tr", r.changed ? "is-changed" : "is-same");
        const x = r.a || r.b;
        // G-125: the unit id on every row (the first changed row is not always the first row of the unit)
        tr.append(td(d.unit, "plan-unit"), td(r.pt === 0 ? "OC point" : "pt " + r.pt),
          td(arrow(r.b && r.b.ratio, r.a && r.a.ratio, (v) => (v === null ? "n/a" : v + "×"), r.b && r.b.unread), "n"),
          td(r.pt === 0 && !isNum(x.offset_mv) ? "n/a" : arrow(r.b && r.b.offset_mv, r.a && r.a.offset_mv, (v) => fmtMv(v, v !== null && Math.abs(v - Math.round(v)) < 0.05 ? 0 : 1), r.b && r.b.unread), "n"),
          td(r.pt === 0 ? arrow(r.b && r.b.v_target_v, r.a && r.a.v_target_v, (v) => (v === null ? "n/a" : v.toFixed(3) + " V"), r.b && r.b.unread) : "", "n"));
        if (showEff) {
          const cell = td(r.a && isNum(r.a.effective_v) ? r.a.effective_v.toFixed(3) + " V" : "", "n");
          if (r.a && r.a.clamped_by !== null && r.a.clamped_by !== undefined) {
            const b = el("span", "tn-badge", "clamped to Vn\u22121");
            b.title = "Raised to the voltage of " + clampedByText(d.unit, r.a.clamped_by) + ": the hardware keeps every point at or above the one before it.";
            cell.append(document.createTextNode(" "), b);
          }
          tr.append(cell);
        }
        tb.append(tr);
      });
    }
    tbl.append(th, tb);
    if (P.diff.some((d) => d.changes > 0 || T.showUnchanged)) { nodes.push(el("div", "scroll plan-scroll")); nodes[nodes.length - 1].append(tbl); }
    if (tbk.length) nodes.push(turboDiffNode(P.intent.turbo));
    nodes.push(el("p", "muted plan-revert", "Revert: cold boot the machine, or reapply a profile. The CPU forgets these words on every cold boot."));
    if (P.state === "running" || P.state === "done" || P.state === "error") nodes.push(planResultNode());
    fill($("dlg-plan-body"), nodes);
  }

  // Plan warnings are {code, text} objects (contract 9 addendum, G-114). The code decides the look: the E 40x point is red, the other known codes are amber with
  // a badge, NOTE (and any code this page does not know) is plain text. A bare string (an older helper) is a NOTE.
  const WARN_CODES = {
    CLAMPED: { cls: "warn", label: "clamped" },
    E40_OFFSET: { cls: "bad", label: "E 40\u00d7 offset \u00b7 community hint" },
    E_TARGET_WHEA: { cls: "warn", label: "WHEA risk \u00b7 community hint" },
    POSITIVE_OFFSET: { cls: "warn", label: "positive offset" },
    NO_BASELINE: { cls: "warn", label: "no baseline" },
    TURBO_RAISE: { cls: "warn", label: "turbo raise" },
    NOTE: { cls: "", label: "" },
  };
  function planWarnings(res) {
    const out = [];
    for (const w of Array.isArray(res.warnings) ? res.warnings : []) {
      const code = isObj(w) && typeof w.code === "string" ? w.code : "NOTE";
      const text = isObj(w) ? w.text : w;
      if (typeof text !== "string" || !text) continue;
      const k = Object.prototype.hasOwnProperty.call(WARN_CODES, code) ? WARN_CODES[code] : WARN_CODES.NOTE;
      out.push({ code: code, text: text, cls: k.cls, label: k.label });
    }
    return out;
  }

  // The plan's VccIA prediction: value, label (a prediction), the unit that sets it, the regulator headroom, the 1-2 core case and the change from now.
  function vcciaNode(vc) {
    const box = el("div", "plan-vccia");
    const who = (sb) => (isObj(sb) && typeof sb.unit === "string" ? sb.unit + (isNum(sb.ratio) ? " at " + sb.ratio + "\u00d7" : "") : "");
    const main = el("p", "", "");
    main.append(document.createTextNode("Predicted VccIA \u2248 " + vc.predicted_v.toFixed(2) + " V"));
    if (typeof vc.label === "string" && vc.label) main.append(document.createTextNode(" "), el("span", "tn-badge", vc.label));
    const bits = [];
    if (who(vc.set_by)) bits.push("set by " + who(vc.set_by) + (vc.active_cores === "all" ? " with every core loaded" : ""));
    if (isNum(vc.margin_v)) bits.push("includes " + vc.margin_v.toFixed(3) + " V regulator headroom");
    if (isNum(vc.before_v)) bits.push("now \u2248 " + vc.before_v.toFixed(2) + " V");
    if (bits.length) main.append(document.createTextNode(", " + bits.join(", ")));
    main.append(document.createTextNode(". Lowering a unit that does not set the maximum does not lower Vcore."));
    box.append(main);
    const ll = isObj(vc.light_load) && isNum(vc.light_load.predicted_v) ? vc.light_load : null;
    if (ll) box.append(el("p", "muted", "With only 1\u20132 cores loaded \u2248 " + ll.predicted_v.toFixed(2) + " V" + (who(ll.set_by) ? ", set by " + who(ll.set_by) : "") + "."));
    return box;
  }

  function clampedByText(unit, idx) {
    const u = T.words && T.words.units[unit], pt = u ? u.points.find((p) => p.point === idx) : null;
    return pt && isNum(pt.ratio) ? "the " + pt.ratio + "\u00d7 point (point " + idx + ")" : "point " + idx;
  }

  function turboDiffNode(intent) {
    const box = el("div", "plan-turbo");
    box.append(el("h3", "dlg-sub", "Turbo ratio tables"));
    const t = el("table", "grid num tbl-turbo");
    t.append(el("caption", "sr", "Turbo ratios before and after, by active-core bucket"));
    const hr = el("tr");
    hr.append(Object.assign(el("th", "", "Table"), { scope: "col" }));
    for (let i = 0; i < 8; i++) hr.append(Object.assign(el("th", "n", "bucket " + i), { scope: "col" }));
    const head = el("thead"), body = el("tbody");
    head.append(hr);
    for (const k of Object.keys(intent)) {
      const cur = turboCur(k) || [];
      const tr = el("tr");
      tr.append(el("th", "", (k === "P" ? "P-cores" : "E-clusters")));
      intent[k].forEach((v, i) => tr.append(td(isNum(cur[i]) && cur[i] !== v ? cur[i] + "\u00d7 \u2192 " + v + "\u00d7" : v + "\u00d7", "n mono")));
      body.append(tr);
    }
    t.append(head, body);
    box.append(t);
    return box;
  }

  // G-126: scaling_max_freq of the CPUs whose OC ratio changed: the helper moves it with cpuinfo_max_freq (a cap the user set lower is kept)
  function cpufreqNode(rows) {
    const box = el("div", "cpufreq-box");
    box.append(el("h3", "dlg-sub", "OS frequency cap"),
      el("p", "muted", "scaling_max_freq follows the new ratio so the OS can use it; a cap you set below the old maximum is kept."));
    const t = el("table", "grid num plan-table");
    const thr = el("tr");
    for (const x of ["CPU", "Before", "After", "Result"]) { const h = el("th", "", x); h.scope = "col"; thr.append(h); }
    t.append(el("thead"), el("tbody"));
    t.tHead.append(thr);
    for (const x of rows) {
      const tr = el("tr", x.after_khz !== x.before_khz ? "is-changed" : "");
      const rc = el("td");
      const word = x.after_khz > x.before_khz ? "raised" : x.after_khz < x.before_khz ? "lowered" : "kept";
      rc.append(pill(word, word === "kept" ? "s-neutral" : "s-ok"));
      tr.append(td(x.cpu, "n"), td((x.before_khz / 1e6).toFixed(2) + " GHz", "n"), td((x.after_khz / 1e6).toFixed(2) + " GHz", "n"), rc);
      t.tBodies[0].append(tr);
    }
    const sc = el("div", "scroll plan-scroll");
    sc.append(t);
    box.append(sc);
    return box;
  }

  function planResultNode() {
    const P = T.plan;
    const box = el("div", "plan-result");
    if (P.state === "running") {
      const bar = el("progress");
      bar.id = "plan-bar"; bar.max = 1; bar.value = 0; bar.setAttribute("aria-label", "Apply progress");
      box.append(el("h3", "dlg-sub", "Applying"), bar, Object.assign(el("p", "muted", P.progressText || "waiting for the first step"), { id: "plan-progress" }));
    } else if (P.state === "error") {
      box.append(el("h3", "dlg-sub", "Not applied"), el("p", "bad-text", P.error || "The helper refused."));
    } else if (P.state === "done") {
      const rb = P.readback || [];
      const bad = rb.filter((x) => x.match !== true);
      if (P.partial) {
        const warn = el("div", "partial-box");
        warn.setAttribute("role", "alert");
        warn.append(el("strong", "", "Partial apply: it stopped after " + (isNum(P.writesSent) ? P.writesSent + " of the planned writes" : "some of the planned writes") + "."),
          el("p", "", (typeof P.partial.reason === "string" ? P.partial.reason : "The helper gave no reason.") + (P.partial.tripped === true ? " The guard tripped." : "")),
          el("p", "", "The CPU holds a mix of old and new words. Do not rely on it: use Restore accepted, or cold boot to get back to the fused state. The table below says which words did land."));
        box.append(warn);
      }
      box.append(el("h3", "dlg-sub", "Read-back"));
      const head = el("p", "");
      head.append(pill(bad.length ? bad.length + " of " + rb.length + " words differ" : "all " + rb.length + " words read back as written", bad.length ? "s-bad" : "s-ok"));
      if (bad.length) head.append(document.createTextNode(" Do not trust this state: reapply the accepted profile or cold boot."));
      box.append(head);
      const t = el("table", "grid num plan-table");
      const thr = el("tr");
      for (const x of ["Unit", "Point", "CPU", "Expected", "Actual", "Result"]) { const h = el("th", "", x); h.scope = "col"; thr.append(h); }
      t.append(Object.assign(el("thead"), {}), el("tbody"));
      t.tHead.append(thr);
      for (const x of rb) {
        const tr = el("tr", x.match === true ? "" : "is-changed");
        const rc = el("td");
        rc.append(pill(x.match === true ? "match" : "mismatch", x.match === true ? "s-ok" : "s-bad"));
        tr.append(td(x.point === "turbo" ? "turbo table " + x.unit : x.unit), td(x.point === 0 ? "OC point" : x.point === "turbo" ? "ratios" : "pt " + x.point),
          td(isNum(x.cpu) ? x.cpu : "", "n"), td(x.expected === null || x.expected === undefined ? "n/a" : x.expected, "mono"), td(x.actual === null || x.actual === undefined ? "not read back" : x.actual, "mono"), rc);
        t.tBodies[0].append(tr);
      }
      const sc = el("div", "scroll plan-scroll");
      sc.append(t);
      box.append(sc);
      if (P.cpufreq && P.cpufreq.length) box.append(cpufreqNode(P.cpufreq));
      if (P.ops !== null && P.ops !== undefined) box.append(el("p", "muted", P.ops + " mailbox operations used."));
    }
    return box;
  }

  function renderPlanFoot() {
    const P = T.plan, res = P.res;
    const foot = $("dlg-plan-foot");
    if (P.state === "done" || P.state === "error") {
      const nodes = [];
      if (P.state === "error" && P.stale) {
        const again = el("button", "primary", "Plan again");
        again.type = "button";
        again.title = "Make a fresh plan for the same request";
        again.addEventListener("click", () => { closeDialog($("dlg-plan")); requestPlan(P.intent, (m) => setText("tn-status", "Not planned: " + m)); });
        nodes.push(again);
      }
      if (P.state === "done" && P.changedUnits && P.changedUnits.length) {
        const again = el("button", "primary", "Measure again");
        again.type = "button";
        again.title = "Preselects the changed units in Curves with a Quick run";
        again.addEventListener("click", () => {
          CV.sel = new Set(P.changedUnits); CV.mode = "quick"; syncSegmented($("cv-mode"), "quick"); syncUnitChips();
          closeDialog($("dlg-plan"));
          selectTab("curves", true);
        });
        nodes.push(again);
      }
      if (P.state === "done" && !P.partial) {
        const val = el("button", "", "Validate with a stress screen");
        val.type = "button";
        val.title = "Opens Stress with " + (P.changedUnits && P.changedUnits.length ? "the units this apply changed" : "all units") + " selected. Nothing starts until you press Start.";
        val.addEventListener("click", () => goValidate(P.changedUnits || []));
        nodes.push(val);
      }
      const close = el("button", "", "Close");
      close.type = "button";
      close.addEventListener("click", () => closeDialog($("dlg-plan")));
      nodes.push(close);
      fill(foot, nodes);
      return;
    }
    const box = el("div", "confirm-box");
    box.append(el("p", "confirm-label", "Apply sends this plan once. Its confirmation code is single-use and expires; the helper checks it."));
    const why = el("p", "muted confirm-why");
    why.id = "plan-why";
    why.setAttribute("aria-live", "polite");
    const go = el("button", "primary", "Apply");
    go.type = "button"; go.id = "plan-go"; go.disabled = true;  // enabled only by updateModalGate()
    go.addEventListener("click", commitPlan);
    const cancel = el("button", "", "Cancel");
    cancel.type = "button"; cancel.id = "plan-cancel";
    cancel.addEventListener("click", () => closeDialog($("dlg-plan")));
    const row = el("div", "row-tight");
    row.append(cancel, go);
    fill(foot, [box, why, row]);
    updateModalGate();
  }

  function updateModalGate() {
    const P = T.plan;
    const dlg = $("dlg-plan");
    if (!P || !dlg.open) return;
    const closeBtn = $("btn-plan-close");
    closeBtn.disabled = P.state === "running";
    const go = document.getElementById("plan-go"), why = document.getElementById("plan-why");
    if (!go || !why) return;
    let msg = "";
    const changed = P.diff.some((d) => d.changes > 0);
    if (P.state === "running") msg = "Applying. This window closes when it is done.";
    else if (!changed) msg = "Nothing would change.";
    else if (planExpired()) msg = "This plan expired. Close this window and review again.";
    else msg = applyGate(P.res.ops_needed);
    go.disabled = msg !== "";
    why.textContent = msg === "" ? "Ready. This sends " + (isNum(P.res.ops_needed) ? P.res.ops_needed : "the planned") + " WRMSR requests." : msg;
  }

  async function commitPlan() {
    const P = T.plan;
    if (!P || P.state !== "review") return;
    updateModalGate();
    if (document.getElementById("plan-go").disabled) return;
    P.state = "running";
    P.progressText = "";
    S.submitting = true;
    renderPlanBody();
    renderPlanFoot();
    updateModalGate();
    const { status, body } = await api("tune.apply", { plan_id: P.res.plan_id, typed: "CONFIRM " + P.res.nonce });
    S.submitting = false;
    if (!launched("commit", status, body)) {
      P.state = "error";
      const code = isObj(body.error) ? body.error.code : "";
      P.stale = code === "PLAN_STALE";
      P.error = (code === "PLAN_STALE" ? "A word changed since this plan was made, or the plan is gone (plans do not survive a helper restart). Nothing was written. Plan again. "
        : code === "CONFIRM_MISMATCH" ? "The helper did not accept the confirmation (used already, or wrong; three wrong codes drop the plan). Nothing was written. " : "") + errText(body);
      renderPlanBody();
      renderPlanFoot();
    }
    updateGate();
  }

  function setCommitProgress(step, of, text) {
    const P = T.plan;
    if (!P || P.state !== "running") return;
    P.progressText = text;
    const bar = document.getElementById("plan-bar"), t = document.getElementById("plan-progress");
    if (bar) { bar.max = of; bar.value = Math.min(step, of); }
    if (t) t.textContent = text;
  }

  function onCommitResult(ev) {
    const P = T.plan;
    if (!P) {  // the page was reloaded during the apply: no review dialog to show the read-back in
      const rb0 = ev.ok === true && isObj(ev.result) && Array.isArray(ev.result.readback) ? ev.result.readback : null;
      setText("tn-status", rb0 ? "An apply finished while this page was away: " + rb0.filter((x) => isObj(x) && x.match !== true).length + " of " + rb0.length + " words differ on read-back. Read the ratios and voltages to see the state."
        : "An apply ended while this page was away (" + errText(ev) + "). Read the ratios and voltages to see where things stand.");
      return;
    }
    if (ev.ok === true && isObj(ev.result)) {
      const rb = (Array.isArray(ev.result.readback) ? ev.result.readback : []).filter(isObj);
      P.state = "done";
      P.readback = rb;
      P.cpufreq = (Array.isArray(ev.result.cpufreq) ? ev.result.cpufreq : []).filter((x) => isObj(x) && isNum(x.cpu) && isNum(x.before_khz) && isNum(x.after_khz));  // G-126
      P.partial = ev.result.complete === false ? (isObj(ev.result.stop) ? ev.result.stop : {}) : null;
      P.writesSent = isNum(ev.result.writes_sent) ? ev.result.writes_sent : null;
      P.ops = isNum(ev.result.ops_used) ? ev.result.ops_used : null;
      P.changedUnits = P.diff.filter((d) => d.changes > 0).map((d) => d.unit);
      // fold what was written into the words model, for units whose words all read back as written
      const bad = new Set(rb.filter((x) => x.match !== true).map((x) => x.unit));
      if (P.partial) for (const u of P.changedUnits) bad.add(u);  // a partial apply: no unit is known to hold the planned words
      const after = normState(P.res.after);
      if (T.words) {
        for (const u of P.changedUnits) {
          const w = wUnit(u);
          if (!w || bad.has(u) || !after[u]) continue;
          for (const pt of Object.keys(after[u])) {
            const e = after[u][pt];
            if (Number(pt) === 0) { if (w.oc) Object.assign(w.oc, { ratio: e.ratio, v_target_v: e.v_target_v, offset_mv: e.offset_mv }); }
            else { const p = w.points.find((q) => q.point === Number(pt)); if (p) { p.ratio = e.ratio; p.offset_mv = e.offset_mv; p.word = null; } }
          }
        }
        T.words.fromPlan = true;
        T.words.prov = "applied";
        T.words.readAt = isNum(Date.parse(ev.result.read_at)) ? Date.parse(ev.result.read_at) : Date.now();
        renderProvenance();
        T.draft = {};
        const a = appliedFromWords(T.words);
        setAppliedChip(bad.size ? "unknown" : a.kind, P.partial ? "partial apply, read again" : bad.size ? "unverified" : a.text + ", written this session",
          P.partial ? "The apply stopped part way: read the ratios and voltages to see what the CPU holds." : bad.size ? "Some words did not read back as written." : "Written from the review dialog and read back. Read the ratios and voltages again for a fresh check.");
        buildRows();
      }
      setText("tn-status", P.partial ? "Partial apply: it stopped after " + (isNum(P.writesSent) ? P.writesSent : "some") + " writes. Read the ratios and voltages to see what the CPU holds."
        : "Applied from the review dialog. " + (rb.some((x) => x.match !== true) ? "Some words did not match; see the read-back." : "Every word read back as written."));
    } else {
      P.state = "error";
      const code = isObj(ev.error) ? ev.error.code : "";
      P.error = typeof ev.server_error === "string" ? (ev.server_error === "helper_lost" ? "The helper was lost before it answered. Check the audit log before doing anything else." : ev.server_error)
        : (code === "CANCELLED" ? "Cancelled. Requests already sent are in the audit log. " : "") + errText(ev);
    }
    renderPlanBody();
    renderPlanFoot();
    updateModalGate();
  }

  // ---- preview: the measured curve with the prediction of the pending edit -----------------------------------------------------------------------
  function effOffsetAt(u, ratio, eff) {
    const pts = curPts(u).map((p) => ({ mhz: p.ratio * 100, v: eff ? effPointMv(u, p) : p.offset_mv })).sort((x, y) => x.mhz - y.mhz);
    if (!pts.length) return 0;
    if (ratio * 100 <= pts[0].mhz) return pts[0].v;
    if (ratio * 100 >= pts[pts.length - 1].mhz) return pts[pts.length - 1].v;
    const at = interpAt(pts, ratio * 100);
    return at ? at.v : 0;
  }

  function setTuneCurveDefault() {
    if (!T.curveId || !CV.list.some((c) => c.id === T.curveId)) T.curveId = defaultCurveId();
    if (T.curveId) ensureRecord(T.curveId).then(renderTunePreview);
  }

  function renderTunePreview() {
    if (!T.words) return;
    const sel = $("tn-curve");
    const opts = CV.list.map((c) => Object.assign(el("option", "", curveTitle(c)), { value: c.id }));
    if (!opts.length) opts.push(Object.assign(el("option", "", "No stored curves"), { value: "" }));
    if (sel.options.length !== opts.length || Array.from(sel.options).some((o, i) => o.value !== opts[i].value)) fill(sel, opts);
    if (!CV.list.some((c) => c.id === T.curveId)) T.curveId = defaultCurveId();
    sel.value = T.curveId || "";
    sel.disabled = !CV.list.length;
    if (T.curveId && !CV.cache[T.curveId]) ensureRecord(T.curveId).then(renderTunePreview);
    renderLegend($("tn-legend"), T.drawn, (set) => { T.drawn = set; renderTunePreview(); }, true);
    const rec = T.curveId ? CV.cache[T.curveId] : null;
    const model = { series: [], markers: [], band: null, ymode: "v", empty: null };
    if (!rec) model.empty = CV.list.length ? "Loading the measured curve." : "Measure a curve first (Curves), then the prediction is drawn on it.";
    else {
      for (const u of UNIT_IDS) {
        if (!T.drawn.has(u)) continue;
        const pa = recPoints(rec, u);
        if (pa.length) model.series.push({ key: u, unit: u, label: u, pts: pa.map((p) => ({ mhz: p.mhz, v: p.v })) });
        const d = T.draft[u];
        if (d && pa.length && (d.curveMv !== undefined || Object.keys(d.pts).length)) {
          // F4: with a stock baseline the prediction is its VID + the new offsets at the unit's points, clamped so no point sits below the one before it;
          // without one it is the measured curve plus the change in offset, clamped the same way.
          const pr = predictUnit(u);
          const known = Object.keys(pr.byRatio).map(Number).filter((r) => pr.byRatio[r]).sort((a, b) => a - b);
          let pts, clamped = [];
          if (known.length > 1) {
            pts = known.map((r) => ({ mhz: r * 100, v: pr.byRatio[r].v }));
            clamped = known.filter((r) => pr.byRatio[r].by !== null).map((r) => ({ mhz: r * 100, v: pr.byRatio[r].v, by: pr.byRatio[r].by, r: r }));
          } else {
            pts = pa.map((p) => ({ mhz: p.mhz, v: p.v + (effOffsetAt(u, p.ratio, true) - effOffsetAt(u, p.ratio, false)) / 1000 }));
            for (let i = 1; i < pts.length; i++) if (pts[i].v < pts[i - 1].v) { pts[i] = { mhz: pts[i].mhz, v: pts[i - 1].v }; clamped.push({ mhz: pts[i].mhz, v: pts[i].v, by: pts[i - 1].mhz / 100, r: pts[i].mhz / 100 }); }
          }
          model.series.push({ key: u + ":pred", unit: u, label: u + " (prediction)", dash: true, pts: pts });
          for (const c of clamped) (model.pins || (model.pins = [])).push({ unit: u, kind: "clamp", mhz: c.mhz, v: c.v, title: u + " " + c.r + "\u00d7: clamped up to the " + c.by + "\u00d7 point's voltage (V(n) never below V(n\u22121))." });
        }
        if (d && (d.vt !== undefined || d.ratio !== undefined)) {
          const cr = curRatio(u), cv = curVt(u), er = effRatio(u), ev = effVt(u);
          if (isNum(cr) && isNum(cv)) model.markers.push({ unit: u, mhz: cr * 100, v: cv, hollow: true });
          if (isNum(er) && isNum(ev)) model.markers.push({ unit: u, mhz: er * 100, v: ev, label: u + " target " + ev.toFixed(3) + " V" });
        }
      }
      if (!model.series.length) model.empty = T.drawn.size ? "The selected curve has no points for these units." : "No units drawn. Press a unit below.";
    }
    T.chart.render(model);
    const clampedUnits = Array.from(new Set((model.pins || []).map((q) => q.unit)));
    const kind = rec && isObj(rec.tuning_state) ? rec.tuning_state.kind : null;
    setText("tn-preview-note", "Dashed lines are a prediction, not a measurement: the stock baseline's voltage plus the new offsets at each point (the measured voltage plus the change in offset when no baseline is set). Large hollow rings mark the OC target now, filled rings the new one." +
      (kind ? " Measured curve: " + tuningWords(kind).replace(/^Tuning/, "tuning") + "." : "") + " After Apply, measure again." +
      (clampedUnits.length ? " Where a deeper offset would put a point below the one before it, the hardware raises it: the dashed line steps flat there and a small ring with a surface fill marks it (" + clampedUnits.join(", ") + ")." : ""));
  }

  function initTune() {
    T.chart = makeVfChart($("tn-chart"), { label: "Measured curve with the predicted curve for the pending edit" });
    segmented($("tn-scope"), [{ value: "simple", label: "Simple" }, { value: "unit", label: "Per unit" }], T.scope, (m) => { T.scope = m; renderScopeNote(); buildRows(); });
    $("btn-tune-read").addEventListener("click", startRead);
    $("btn-tune-cancel").addEventListener("click", cancelOp);
    $("btn-tn-hex").addEventListener("click", () => {
      T.hex = !T.hex;
      $("btn-tn-hex").setAttribute("aria-pressed", T.hex ? "true" : "false");
      $("btn-tn-hex").textContent = T.hex ? "Show decoded values" : "Show raw words";
      renderWordsTable();
      refreshRows();
    });
    $("btn-tn-discard").addEventListener("click", () => { T.draft = {}; T.turbo = {}; refreshRows(); });
    $("btn-tn-plan").addEventListener("click", () => {
      const intent = unitsChanged().length ? buildIntent() : {};   // a turbo-only edit sends {turbo} alone
      const tb = turboIntent();
      if (tb) intent.turbo = tb;   // {turbo: {P?: [8], E?: [8]}} alone is a real plan (G-117): plain-MSR table writes, no mailbox word
      requestPlan(intent, (m) => { const f = $("tn-foot-msg"); f.textContent = "Not reviewed: " + m; f.className = "tn-foot-msg bad"; });
    });
    $("btn-tn-reset").addEventListener("click", () => requestPlan({ reset: "stock" }, (m) => setText("tn-status", "Reset not planned: " + m)));
    $("btn-tn-restore").addEventListener("click", () => requestPlan({ restore: "accepted" }, (m) => setText("tn-status", "Restore not planned: " + m)));
    $("tn-curve").addEventListener("change", (e) => { T.curveId = e.target.value || null; if (T.curveId) ensureRecord(T.curveId).then(renderTunePreview); renderTunePreview(); });
    $("btn-plan-close").addEventListener("click", () => closeDialog($("dlg-plan")));
    $("dlg-plan").addEventListener("cancel", (e) => { if (T.plan && T.plan.state === "running") e.preventDefault(); });
    $("dlg-plan").addEventListener("close", () => { if (T.plan && T.plan.state === "review") setText("tn-status", "Review closed. Nothing was written."); });
    setInterval(() => { if ($("dlg-plan").open) updateModalGate(); }, 1000);
  }

  // ---- Stress: a short screen of the CPU or the memory (G-116 ops, G-119 page). No mailbox, no write; a quick screen, never an acceptance gate (D-019). ----
  const ST = {
    kind: "cpu",            // cpu | memory
    method: "integer",      // integer | vector-math | cache
    dur: 300,               // preset length in seconds
    custom: false,          // the custom slider is the length
    cap: null,              // ratio cap or null
    memMb: null,            // null = the helper's default (3/4 of free memory)
    sel: new Set(UNIT_IDS),
    list: [], loaded: false,
    run: null,              // {kind, of, elapsed, errors, units: [unit]} while a screen runs
    result: null,           // the last stress.start result seen by this page
  };
  const ST_DURS = [{ value: 60, label: "1 min" }, { value: 300, label: "5 min" }, { value: 900, label: "15 min" }, { value: 3600, label: "60 min" }, { value: "custom", label: "Custom" }];
  const ST_METHODS = [
    { value: "integer", label: "Integer", title: "Integer arithmetic on every selected core (stress-ng, int64)" },
    { value: "vector-math", label: "Vector math", title: "Matrix products: heavier on the vector units (stress-ng, matrixprod)" },
    { value: "cache", label: "Cache", title: "Cache thrashing on every selected core (stress-ng, cache)" },
  ];
  const ST_MEM_MB = [1024, 2048, 4096, 8192, 16384, 32768];
  const ST_WORDS = { integer: "integer", "vector-math": "vector math", cache: "cache" };

  function stRunning() { return S.opId !== null && S.opKind === "stress"; }
  function stDuration() { return ST.custom ? Number($("st-dur-custom").value) : ST.dur; }
  function fmtLen(s) { return s % 60 === 0 && s >= 60 ? s / 60 + " min" : s < 60 ? s + " s" : fmtClock(s); }
  function mmss(s) { const t = Math.max(0, Math.round(s)), m = Math.floor(t / 60), r = t % 60; return m + ":" + (r < 10 ? "0" : "") + r; }
  function stMean(rec, unit) {   // mean clock of a unit's cpus in a history sample
    const vs = unitCpus(unit).map((c) => (isObj(rec.cores[String(c)]) ? rec.cores[String(c)].mhz : null)).filter(isNum);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  }

  function buildStressControls() {
    segmented($("st-kind"), [{ value: "cpu", label: "CPU", title: "Load the cores you pick with stress-ng" }, { value: "memory", label: "Memory", title: "Test the memory with stressapptest on all CPUs" }], ST.kind,
      (k) => { ST.kind = k; renderStress(); });
    segmented($("st-method"), ST_METHODS, ST.method, (m) => { ST.method = m; renderStress(); });
    segmented($("st-dur"), ST_DURS, ST.dur, (v) => {
      if (v === "custom") ST.custom = true; else { ST.custom = false; ST.dur = v; }
      renderStress();
    });
    $("st-dur-custom").addEventListener("input", () => { ST.custom = true; renderStress(); });
    const cap = $("st-cap");
    cap.append(new Option("No cap", ""));
    for (let r = 60; r >= 8; r--) cap.append(new Option(r + "× (" + r * 100 + " MHz)", String(r)));
    cap.addEventListener("change", () => { ST.cap = cap.value === "" ? null : Number(cap.value); renderStress(); });
    const mem = $("st-mem");
    mem.append(new Option("Automatic: 3/4 of free memory", ""));
    for (const mb of ST_MEM_MB) mem.append(new Option(mb / 1024 + " GB", String(mb)));
    mem.addEventListener("change", () => { ST.memMb = mem.value === "" ? null : Number(mem.value); renderStress(); });
    // units: a mini core map. P-cores in one row, the four E-clusters (4 cores each) beside them.
    const tiles = UNIT_IDS.map((u) => {
      const b = el("button", "mm-tile u-" + u);
      b.type = "button";
      b.dataset.unit = u;
      b.setAttribute("aria-pressed", "true");
      b.title = unitTileTitle(u);
      b.append(el("span", "mm-name", u + favMark(u)), el("span", "mm-cpus", unitKind(u) === "P" ? "cpu " + unitCpus(u)[0] : "cpu " + unitCpus(u)[0] + "–" + unitCpus(u)[3]), el("span", "mm-mhz", ""));
      b.addEventListener("click", () => { if (ST.sel.has(u)) ST.sel.delete(u); else ST.sel.add(u); renderStress(); });
      return b;
    });
    const quick = el("div", "chip-quick");
    for (const [label, units] of [["All", UNIT_IDS], ["P-cores", UNIT_IDS.slice(0, 8)], ["E-clusters", UNIT_IDS.slice(8)], ["None", []]]) {
      const b = el("button", "link", label);
      b.type = "button";
      b.addEventListener("click", () => { ST.sel = new Set(units); renderStress(); });
      quick.append(b);
    }
    const p = el("div", "mm-p"), e = el("div", "mm-e");
    p.append(...tiles.slice(0, 8)); e.append(...tiles.slice(8));
    fill($("st-units"), [p, e, quick]);
  }

  // One place draws the whole page from ST, S and the telemetry history, so a reload (adopting a run) and a click give the same picture.
  function renderStress() {
    const running = stRunning();
    const cpu = ST.kind === "cpu";
    syncSegmented($("st-kind"), ST.kind);
    syncSegmented($("st-method"), ST.method);
    syncSegmented($("st-dur"), ST.custom ? "custom" : ST.dur);
    $("st-f-method").hidden = !cpu;
    $("st-f-units").hidden = !cpu;
    $("st-f-mem").hidden = cpu;
    $("st-f-custom").hidden = !ST.custom;
    const secs = stDuration();
    $("st-dur-val").textContent = fmtLen(secs);
    $("st-dur-custom").setAttribute("aria-valuetext", fmtLen(secs));
    // while a screen runs the setup is read-only
    for (const id of ["st-cap", "st-mem", "st-dur-custom"]) $(id).disabled = running;
    for (const host of ["st-kind", "st-method", "st-dur"]) for (const b of $(host).children) b.disabled = running;
    const live = running && ST.run ? new Set(ST.run.units) : null;
    const last = V.hist.length ? V.hist[V.hist.length - 1] : null;
    for (const b of $("st-units").querySelectorAll(".mm-tile")) {
      const u = b.dataset.unit;
      const on = live ? live.has(u) : ST.sel.has(u);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.disabled = running;
      const m = running && on && last ? stMean(last, u) : null;
      b.querySelector(".mm-mhz").textContent = running && on ? (m === null ? "n/a" : Math.round(m) + " MHz") : "";
    }
    for (const b of $("st-units").querySelectorAll(".chip-quick .link")) b.disabled = running;
    updateStressGate();
    renderStressLive();
    renderStressTemps();
  }

  function updateStressGate() {
    const running = stRunning();
    const why = running ? "" : idleGate();
    const none = ST.kind === "cpu" && ST.sel.size === 0;
    $("btn-stress-start").disabled = running || why !== "" || none;
    $("btn-stress-stop").disabled = !running || S.cancelling || S.sessionLost;
    const secs = stDuration();
    let est;
    if (running) est = S.cancelling ? "Stopping. The tool is ended and the cpufreq limits are put back." : "Running. Stop ends it early; the result is kept either way.";
    else if (why !== "") est = why;
    else if (none) est = "Select at least one unit.";
    else est = "Ready: " + (ST.kind === "cpu" ? ST_WORDS[ST.method] + " load on " + (ST.sel.size === UNIT_IDS.length ? "all 12 units" : ST.sel.size + (ST.sel.size === 1 ? " unit" : " units")) : "memory test of " + (ST.memMb === null ? "3/4 of free memory" : ST.memMb / 1024 + " GB") + " on all CPUs") +
      " for " + fmtLen(secs) + (ST.cap === null ? "" : ", capped at " + ST.cap + "×") + ". It raises power and heat for that time.";
    setText("st-est", est);
  }

  function renderStressLive() {
    const running = stRunning();
    const r = ST.run;
    const of = running && r && isNum(r.of) ? r.of : stDuration();
    const el0 = running && r && isNum(r.elapsed) ? r.elapsed : (!running && ST.result ? ST.result.elapsed_s : 0);
    const f = of > 0 ? clamp(el0 / of, 0, 1) * 100 : 0;
    const errs = running && r ? r.errors : (ST.result ? ST.result.errors : 0);
    $("st-ring").setAttribute("stroke-dasharray", f.toFixed(1) + " 100");
    $("st-ring").setAttribute("class", "ring-used" + (f <= 0 ? " empty" : "") + (isNum(errs) && errs > 0 ? " bad" : ""));
    setText("st-clock", mmss(el0));
    setText("st-of", "of " + fmtLen(of));
    const e = $("st-errors");
    e.textContent = !running && !ST.result ? "no errors counted" : errs === 0 ? (running ? "no errors so far" : "no errors detected") : errs + (errs === 1 ? " error counted" : " errors counted");
    e.className = "st-errors" + (isNum(errs) && errs > 0 ? " bad" : "");
    const ph = running ? (S.cancelling ? "stopping" : "running") : "not running";
    setPill("st-phase", ph, running ? "s-busy" : "s-neutral");
  }

  function renderStressTemps() {
    const s = V.hist.length ? V.hist[V.hist.length - 1] : null;
    setTile("t-st-temp", s && isNum(s.hot) ? s.hot.toFixed(0) : "n/a", "°C", !!s && isNum(s.hot), "");
    setTile("t-st-pkgw", s && isNum(s.pkg) ? s.pkg.toFixed(1) : "n/a", "W", !!s && isNum(s.pkg), "");
    sparkline("sp-st-temp", (x) => x.hot, 0, MIN_SPAN.temp);
    sparkline("sp-st-pkgw", (x) => x.pkg, 1, MIN_SPAN.power);
  }

  async function startStress() {
    if (stRunning() || idleGate() !== "" || (ST.kind === "cpu" && ST.sel.size === 0)) return;
    const secs = stDuration();
    const args = { kind: ST.kind, duration_s: secs };
    let units = [];
    if (ST.kind === "cpu") {
      args.method = ST.method;
      units = UNIT_IDS.filter((u) => ST.sel.has(u));
      if (units.length < UNIT_IDS.length) args.units = units;
    } else {
      units = UNIT_IDS.slice();
      if (ST.memMb !== null) args.memory_mb = ST.memMb;
    }
    if (ST.cap !== null) args.ratio_cap = ST.cap;
    S.submitting = true;
    setText("st-status", "Submitting...");
    updateGate();
    const { status, body } = await api("stress.start", args);
    S.submitting = false;
    if (launched("stress", status, body)) {
      if (S.opId) {
        ST.run = { kind: ST.kind, of: secs, elapsed: 0, errors: 0, units: ST.kind === "cpu" ? units : UNIT_IDS.slice() };
        setText("st-status", "Started (operation " + S.opId + ").");
        $("st-result").hidden = true;
      }
    } else {
      setText("st-status", "Not started: " + (status === 429 ? "too many operations pending" : errText(body)));
    }
    updateGate();
    renderStress();
  }

  async function stopStress() {
    if (!stRunning()) return;
    S.cancelling = true;
    renderStress();
    const { status, body } = await api("stress.stop", {});
    if (!(status === 200 && body.ok)) { S.cancelling = false; setText("st-status", "Stop not accepted: " + errText(body)); }
    else setText("st-status", "Stop requested. The result arrives in a moment.");
    renderStress();
  }

  function stressProgress(ev) {
    if (!ST.run) ST.run = { kind: "cpu", of: isNum(ev.of_s) ? ev.of_s : 0, elapsed: 0, errors: 0, units: UNIT_IDS.slice() };   // adopted after a reload: settings unknown
    if (isNum(ev.elapsed_s)) ST.run.elapsed = ev.elapsed_s;
    if (isNum(ev.of_s)) ST.run.of = ev.of_s;
    if (isNum(ev.errors)) ST.run.errors = ev.errors;
    renderStress();
  }

  function onStressResult(ev) {
    ST.run = null;
    if (ev.ok === true && isObj(ev.result)) {
      ST.result = ev.result;
      setText("st-status", "");
      renderStressResult(ev.result);
    } else {
      ST.result = null;
      const why = typeof ev.server_error === "string" ? (ev.server_error === "helper_lost" ? "The helper was lost during the screen. CPU limits are restored when it restarts." : ev.server_error) : errText(ev);
      setText("st-status", "Failed: " + why);
      $("st-result").hidden = true;
    }
    renderStress();
    loadStressList();
  }

  // ---- the result card. Words: "no errors detected in N min" at most. Nothing here says a setting is proven stable. ----
  function stSummary(r) {
    const n = isNum(r.errors) ? r.errors : 0, ran = fmtLen(Math.round(isNum(r.elapsed_s) ? r.elapsed_s : 0));
    if (r.stopped_by === "cancel") return "Stopped after " + ran + " of " + fmtLen(r.duration_s) + ". " + (n === 0 ? "No errors detected in that time." : n + (n === 1 ? " error" : " errors") + " detected.");
    if (n > 0) return n + (n === 1 ? " error" : " errors") + " detected in " + ran + ". Treat this setting as unstable.";
    if (r.stopped_by === "error") return "The screen ended with a tool error after " + ran + ". See the lines below.";
    if (r.health === "delta") return "No errors detected in " + ran + ", but the machine health check changed. See below.";
    return "No errors detected in " + ran + ".";
  }

  function renderStressResult(r) {
    const card = $("st-result");
    card.hidden = false;
    const n = isNum(r.errors) ? r.errors : 0;
    const bad = n > 0 || r.stopped_by === "error" || r.health === "delta" || (isNum(r.mce_new) && r.mce_new > 0);
    const head = el("div", "card-head");
    const h = el("h2", "", "Result");
    const pills = el("div", "row-tight");
    pills.append(pill(r.stopped_by === "time" ? "ran to the end" : r.stopped_by === "cancel" ? "stopped by you" : "tool error", r.stopped_by === "error" ? "s-bad" : "s-neutral"));
    pills.append(pill(r.health === "clean" ? "health clean" : "health changed", r.health === "clean" ? "s-ok" : "s-bad"));
    if (isNum(r.mce_new) && r.mce_new > 0) pills.append(pill(r.mce_new + " new machine-check " + (r.mce_new === 1 ? "event" : "events"), "s-bad"));
    if (r.cap_valid === true) { const p = pill("cap held", "s-ok"); p.title = "Every loaded unit ran within one ratio of the cap."; pills.append(p); }
    else if (r.cap_valid === false) { const p = pill("cap not held", "s-bad"); p.title = "A loaded unit ran more than one ratio away from the cap, so the screen did not test the capped ratio."; pills.append(p); }
    head.append(h, pills);
    const sum = el("p", "st-sum" + (bad ? " bad" : ""), stSummary(r));
    const note = el("p", "muted", "A quick screen, not an acceptance gate.");
    const stats = el("div", "st-stats");
    const stat = (k, v, cls) => { const d = el("div", "st-stat"); d.append(el("span", "ro-label", k), el("span", "st-stat-v" + (cls ? " " + cls : ""), v)); return d; };
    stats.append(stat("Errors", String(n), n > 0 ? "bad" : ""),
      stat("Ran", fmtClock(Math.round(isNum(r.elapsed_s) ? r.elapsed_s : 0)) + " of " + fmtLen(r.duration_s)),
      stat("Hottest", isNum(r.max_temp_c) ? r.max_temp_c.toFixed(0) + " °C" : "n/a"),
      stat("Peak package power", isNum(r.max_pkg_w) ? r.max_pkg_w.toFixed(1) + " W" : "n/a"));
    const nodes = [head, sum, note, stats];
    const ach = isObj(r.achieved) ? r.achieved : {};
    const units = UNIT_IDS.filter((u) => isObj(ach[u]));
    if (units.length) {   // the same mini map as the picker: the units that ran, each with the clock it held and its usual ratio
      const tile = (u) => {
        const a = ach[u], d = el("div", "mm-tile static u-" + u);
        d.title = unitLabel(u) + ": mean clock and the ratio most often seen under load" + (isFav(u) ? " (" + favTitle(u) + ")" : "");
        d.append(el("span", "mm-name", u + favMark(u)), el("span", "mm-cpus", isNum(a.ratio_mode) ? a.ratio_mode + "× mostly" : "ratio n/a"), el("span", "mm-mhz", isNum(a.mhz_mean) ? a.mhz_mean.toFixed(0) + " MHz" : "n/a"));
        return d;
      };
      const p = el("div", "mm-p"), e = el("div", "mm-e");
      p.append(...units.filter((u) => unitKind(u) === "P").map(tile)); e.append(...units.filter((u) => unitKind(u) === "E").map(tile));
      const map = el("div", "st-map st-ach");
      map.setAttribute("role", "group");
      map.setAttribute("aria-label", "Achieved clock per unit");
      map.append(...[p, e].filter((x) => x.children.length));
      nodes.push(el("h3", "", "Clock each unit held"), map);
    } else {
      nodes.push(el("p", "muted", "Achieved clocks: n/a (no telemetry sampler was running)."));
    }
    const lines = Array.isArray(r.error_lines) ? r.error_lines.filter((x) => typeof x === "string") : [];
    if (lines.length) {
      const ul = el("ul", "st-lines");
      for (const x of lines) ul.append(el("li", "", x));
      nodes.push(el("h3", "", "Notes from the tool and the helper"), ul);
    }
    nodes.push(el("p", "muted", isStr(r.id) ? "Stored as " + r.id + "." : "This result could not be stored on the machine."));
    fill(card, nodes);
  }

  // ---- history (stress.list) ----
  async function loadStressList() {
    const { status, body } = await api("stress.list", {});
    if (status === 200 && body.ok && isObj(body.result) && Array.isArray(body.result.results)) { ST.list = body.result.results.filter(isObj); ST.loaded = true; }
    renderStressHistory(status === 200 && body.ok ? "" : "Stored screens unavailable: " + errText(body));
  }

  function stScreenText(r) {
    if (r.kind === "memory") return "Memory" + (isNum(r.memory_mb) ? " " + (r.memory_mb >= 1024 ? (r.memory_mb / 1024).toFixed(r.memory_mb % 1024 ? 1 : 0) + " GB" : r.memory_mb + " MB") : "") + (isNum(r.ratio_cap) ? ", cap " + r.ratio_cap + "×" : "");
    return "CPU " + (ST_WORDS[r.method] || "load") + (isNum(r.ratio_cap) ? ", cap " + r.ratio_cap + "×" : "");
  }
  function stUnitsText(r) {
    if (r.kind === "memory") return "all CPUs";
    const u = Array.isArray(r.units) ? r.units.filter((x) => typeof x === "string") : [];
    return !u.length || u.length >= UNIT_IDS.length ? "all 12" : u.join(" ");
  }

  function renderStressHistory(problem) {
    const empty = $("st-hist-empty");
    $("st-hist").hidden = ST.list.length === 0;
    empty.hidden = ST.list.length > 0 && !problem;
    if (problem) empty.textContent = problem;
    else if (!ST.list.length) empty.textContent = "No stress screens stored yet. Start one above; the result is kept on this computer.";
    fill($("st-hist-body"), ST.list.map((r) => {
      const tr = el("tr");
      const n = isNum(r.errors) ? r.errors : 0;
      const ended = el("td");
      ended.append(pill(r.stopped_by === "time" ? "full length" : r.stopped_by === "cancel" ? "stopped" : "tool error", r.stopped_by === "error" ? "s-bad" : "s-neutral"));
      const health = el("td");
      health.append(pill(r.health === "clean" ? "clean" : "changed", r.health === "clean" ? "s-ok" : "s-bad"));
      if (isNum(r.mce_new) && r.mce_new > 0) health.append(document.createTextNode(" "), pill(r.mce_new + " MCE", "s-bad"));
      const ran = td(fmtClock(Math.round(isNum(r.elapsed_s) ? r.elapsed_s : 0)), "n", "asked for " + fmtLen(isNum(r.duration_s) ? r.duration_s : 0));
      tr.append(td(fmtDate(r.started)), td(stScreenText(r)), td(stUnitsText(r)), ran, ended, td(String(n), n > 0 ? "n bad-text" : "n"), health,
        td(isNum(r.max_temp_c) ? r.max_temp_c.toFixed(0) + " °C" : "n/a", "n"), td(isNum(r.max_pkg_w) ? r.max_pkg_w.toFixed(0) + " W" : "n/a", "n"));
      return tr;
    }));
  }

  function initStress() {
    buildStressControls();
    $("btn-stress-start").addEventListener("click", startStress);
    $("btn-stress-stop").addEventListener("click", stopStress);
    $("btn-stress-refresh").addEventListener("click", loadStressList);
    renderStress();
  }

  // After a Tune apply: the review window offers a screen of the units it changed (the screen itself is started on the Stress tab).
  function goValidate(units) {
    ST.kind = "cpu";
    ST.sel = new Set(units.length ? units : UNIT_IDS);
    closeDialog($("dlg-plan"));
    selectTab("stress", true);
    renderStress();
  }

  // ---- Profiles ---------------------------------------------------------------------------------------------------------------------------------
  // profiles.list names them; profiles.get (G-123) says what each holds: {contents: {rows: [{target: unit, offset, v_target_v, ratio}], turbo}, summary}, and for an
  // accepted step also {position, of, chain, cumulative} = the state after applying the chain up to that step (what "Load into Tune" loads).
  const PROFILE_RE = /^[a-z0-9._-]{1,64}$/;
  const PR = { list: [], loaded: false, note: "", info: {}, open: new Set(), basis: "applied", seq: 0 };
  V.pr = PR;

  async function loadProfiles() {
    const { status, body } = await api("profiles.list", {});
    PR.loaded = true;
    PR.note = "";
    if (status === 200 && body.ok && isObj(body.result) && Array.isArray(body.result.profiles)) {
      PR.list = body.result.profiles.filter((p) => isObj(p) && typeof p.name === "string");
      PR.note = typeof body.result.note === "string" ? body.result.note : "";   // e.g. "no accepted state directory is configured"
    }
    else { PR.list = []; setText("pr-empty", "Profiles unavailable: " + errText(body)); }
    renderProfiles();
    loadProfileInfo();
  }

  // What each profile holds, a few at a time (each is a small file read in the helper); the list renders again as they arrive.
  async function loadProfileInfo() {
    const seq = ++PR.seq;
    const todo = PR.list.map((p) => p.name).filter((n) => !PR.info[n]).slice(0, 60);
    for (let i = 0; i < todo.length; i += 6) {
      await Promise.all(todo.slice(i, i + 6).map(async (name) => {
        const { status, body } = await api("profiles.get", { name: name });
        PR.info[name] = status === 200 && body.ok && isObj(body.result) && isObj(body.result.contents) && Array.isArray(body.result.contents.rows) ? body.result : { error: errText(body) };
      }));
      if (seq !== PR.seq) return;
      renderProfiles();
    }
  }

  const infoOf = (name) => (PR.info[name] && !PR.info[name].error ? PR.info[name] : null);
  const rowsOf = (c) => (isObj(c) && Array.isArray(c.rows) ? c.rows.filter(isObj) : []);
  // the state a profile stands for when the Compare basis is "as applied": an accepted step is the chain up to and including it
  function stateOf(name, basis) {
    const i = infoOf(name);
    if (!i) return null;
    return basis === "applied" && isObj(i.cumulative) ? i.cumulative : i.contents;
  }

  function offsetText(o) {
    if (!isObj(o)) return "unchanged";
    if (o.kind === "curve") return fmtMv(o.mv) + " (whole curve)";
    if (o.kind === "points" && isObj(o.mv)) return Object.keys(o.mv).sort((a, b) => a - b).map((r) => r + "× " + fmtMv(o.mv[r])).join(", ");
    return "unchanged";
  }
  // a short form for a table cell: all points when there are few, else the count and the range
  function offsetShort(o) {
    if (!isObj(o)) return "–";
    if (o.kind === "curve") return fmtMv(o.mv) + " all";
    const ks = Object.keys(o.mv).sort((a, b) => a - b);
    if (ks.length <= 3) return ks.map((r) => r + "× " + fmtMv(o.mv[r])).join(", ");
    const vs = ks.map((r) => o.mv[r]);
    return ks.length + " points, " + fmtMv(Math.min(...vs)) + " to " + fmtMv(Math.max(...vs));
  }

  function summaryText(s) {
    if (!isObj(s)) return "";
    const bits = [s.units + (s.units === 1 ? " unit" : " units")];
    if (isNum(s.deepest_offset_mv)) bits.push("deepest " + fmtMv(s.deepest_offset_mv));
    if (s.oc_ratios) bits.push(s.oc_ratios + " OC ratio" + (s.oc_ratios === 1 ? "" : "s"));
    if (s.v_targets) bits.push(s.v_targets + " target voltage" + (s.v_targets === 1 ? "" : "s"));
    if (s.turbo) bits.push("turbo table");
    return bits.join(" · ");
  }

  function renderChain() {
    const acc = PR.list.filter((p) => p.source === "accepted");
    $("pr-chain-card").hidden = acc.length === 0;
    if (!acc.length) return;
    setText("pr-chain-note", "Restore accepted applies these in this order. Each step builds on the one before.");
    fill($("pr-chain"), acc.map((p, i) => {
      const li = el("li", "pr-step" + (i === acc.length - 1 ? " tip" : ""));
      const info = infoOf(p.name);
      li.append(el("span", "pr-step-no", String(i + 1)), el("span", "pr-step-name", p.name), el("span", "pr-step-sum muted", info ? summaryText(info.summary) : "reading..."));
      return li;
    }));
  }

  function detailTable(name) {
    const info = infoOf(name);
    const box = el("div", "pr-detail");
    if (!info) { box.append(el("p", "muted", PR.info[name] ? "Contents unavailable: " + PR.info[name].error : "Reading the contents...")); return box; }
    const rows = rowsOf(info.contents);
    if (!rows.length && !isObj(info.contents.turbo)) { box.append(el("p", "muted", "This profile sets nothing.")); return box; }
    const t = el("table", "grid num pr-detail-table");
    t.append(el("caption", "sr", "What " + name + " sets"));
    const head = el("tr");
    for (const h of ["Unit", "Voltage offset", "OC target voltage", "OC ratio"]) head.append(Object.assign(el("th", h === "Unit" || h === "Voltage offset" ? "" : "n", h), { scope: "col" }));
    const th = el("thead"), tb = el("tbody");
    th.append(head);
    for (const r of rows) {
      const tr = el("tr");
      tr.append(td(r.target), td(offsetShort(r.offset), "mono", offsetText(r.offset)), td(isNum(r.v_target_v) ? fmtV(r.v_target_v) : "–", "n mono"), td(isNum(r.ratio) ? r.ratio + "×" : "–", "n mono"));
      tb.append(tr);
    }
    t.append(th, tb);
    const sc = el("div", "scroll");
    sc.append(t);
    box.append(sc);
    const tu = info.contents.turbo;
    if (isObj(tu)) for (const k of ["P", "E"]) if (Array.isArray(tu[k])) box.append(el("p", "muted", k + " turbo table: " + tu[k].map((x) => x + "×").join(" ")));
    if (isNum(info.position) && info.position > 1) box.append(el("p", "muted", "Only this step's own entries are shown. After steps 1 to " + info.position + " the chain holds: " + summaryText(info.cumulative_summary) + "."));
    return box;
  }

  function renderProfiles() {
    $("pr-empty").hidden = PR.list.length > 0;
    if (!PR.list.length && PR.loaded) setText("pr-empty", "No profiles yet. Change something in Tune and save it here." + (PR.note ? " Helper note: " + PR.note : ""));
    renderChain();
    const accepted = PR.list.filter((p) => p.source === "accepted");
    fill($("pr-list"), PR.list.map((p) => {
      const li = el("li", "pr-item");
      const info = infoOf(p.name);
      const main = el("div", "pr-main");
      const where = p.source === "accepted" ? "accepted, step " + (accepted.indexOf(p) + 1) + " of " + accepted.length : "saved";
      main.append(el("span", "pr-name", p.name), pill(where, p.source === "accepted" ? "s-ok" : "s-neutral"));
      if (p.source !== "accepted") main.append(el("span", "muted", fmtDate(p.created)));
      main.append(el("span", "pr-sum muted", info ? summaryText(info.summary) : PR.info[p.name] ? "contents unavailable" : "reading..."));
      const act = el("div", "row-tight");
      const open = PR.open.has(p.name);
      const det = el("button", "", open ? "Hide contents" : "Show contents");
      det.type = "button";
      det.setAttribute("aria-expanded", open ? "true" : "false");
      det.addEventListener("click", () => { if (PR.open.has(p.name)) PR.open.delete(p.name); else PR.open.add(p.name); renderProfiles(); });
      const load = el("button", "pr-load", "Load into Tune");
      load.type = "button";
      load.disabled = !info;
      load.title = "Fills the Tune editor with " + (p.source === "accepted" ? "the state after this step of the accepted chain" : "this profile") + ". Nothing is written until you press Review changes and then Apply.";
      load.addEventListener("click", () => loadIntoTune(p.name));
      const rev = el("button", "pr-act", "Review and apply");
      rev.type = "button";
      rev.title = "Plans this profile against what the CPU holds now and opens the review dialog. Nothing is written until you confirm there.";
      rev.addEventListener("click", () => requestPlan({ profile: p.name }, (m) => setText("pr-status", "Could not plan \"" + p.name + "\": " + m)));
      act.append(det, load, rev);
      if (p.source !== "accepted") {
        const acc = el("button", "", "Propose as accepted");
        acc.type = "button";
        acc.disabled = true;
        acc.title = "Marking a profile as accepted is a deliberate step outside this page (SPEC 6.4): the helper has no op for it.";
        act.append(acc);
      }
      li.append(main, act);
      if (open) li.append(detailTable(p.name));
      return li;
    }));
    const opts = (sel, keep) => {
      fill(sel, PR.list.map((p) => Object.assign(el("option", "", p.name), { value: p.name })));
      if (keep && PR.list.some((p) => p.name === keep)) sel.value = keep;
    };
    const a = $("pr-a").value, b = $("pr-b").value;
    opts($("pr-a"), a);
    opts($("pr-b"), b);
    if (!b && PR.list.length > 1) $("pr-b").value = PR.list[1].name;
    $("pr-basis").hidden = accepted.length === 0;
    renderProfileDiff();
    updateSaveState();
    updateGate();
  }

  // ---- compare: one table, a row per setting that differs, with the change -----------------------------------------------------------
  const unitRank = (u) => UNIT_IDS.indexOf(u);
  function valueAt(o, ratio) {
    if (!isObj(o)) return null;
    if (o.kind === "curve") return o.mv;
    return isObj(o.mv) && isNum(o.mv[ratio]) ? o.mv[ratio] : null;
  }
  // Rows {unit, label, a, b, kind} where the two states differ. A whole-curve value stands for every point of a unit the other side sets point by point.
  function compareRows(ca, cb) {
    const ra = {}, rb = {};
    for (const r of rowsOf(ca)) ra[r.target] = r;
    for (const r of rowsOf(cb)) rb[r.target] = r;
    const out = [];
    const units = Array.from(new Set(Object.keys(ra).concat(Object.keys(rb)))).filter((u) => unitRank(u) >= 0).sort((x, y) => unitRank(x) - unitRank(y));
    for (const u of units) {
      const xa = ra[u] || {}, xb = rb[u] || {};
      const oa = isObj(xa.offset) ? xa.offset : null, ob = isObj(xb.offset) ? xb.offset : null;
      if (oa || ob) {
        const bothCurve = (!oa || oa.kind === "curve") && (!ob || ob.kind === "curve");
        if (bothCurve) {
          const a = oa ? oa.mv : null, b = ob ? ob.mv : null;
          if (a !== b) out.push({ unit: u, label: "Offset, whole curve", a: a, b: b, fmt: fmtMv, delta: fmtDeltaMv });
        } else {
          const ratios = Array.from(new Set([oa, ob].flatMap((o) => (o && o.kind === "points" ? Object.keys(o.mv).map(Number) : [])))).sort((x, y) => x - y);
          for (const r of ratios) {
            const a = valueAt(oa, r), b = valueAt(ob, r);
            if (a !== b) out.push({ unit: u, label: "Offset at " + r + "×", a: a, b: b, fmt: fmtMv, delta: fmtDeltaMv });
          }
        }
      }
      const va = isNum(xa.v_target_v) ? xa.v_target_v : null, vb = isNum(xb.v_target_v) ? xb.v_target_v : null;
      if (va !== vb) out.push({ unit: u, label: "OC target voltage", a: va, b: vb, fmt: (x) => fmtV(x, 3), delta: (a, b) => (isNum(a) && isNum(b) ? fmtMv((b - a) * 1000) : "–") });
      const na = isNum(xa.ratio) ? xa.ratio : null, nb = isNum(xb.ratio) ? xb.ratio : null;
      if (na !== nb) out.push({ unit: u, label: "OC ratio", a: na, b: nb, fmt: (x) => x + "×", delta: (a, b) => (isNum(a) && isNum(b) ? (b - a > 0 ? "+" : b - a < 0 ? MINUS : "") + Math.abs(b - a) + "×" : "–") });
    }
    for (const k of ["P", "E"]) {
      const ta = isObj(ca) && isObj(ca.turbo) && Array.isArray(ca.turbo[k]) ? ca.turbo[k] : null, tb = isObj(cb) && isObj(cb.turbo) && Array.isArray(cb.turbo[k]) ? cb.turbo[k] : null;
      if (!ta && !tb) continue;
      for (let i = 0; i < 8; i++) {
        const a = ta ? ta[i] : null, b = tb ? tb[i] : null;
        if (a !== b) out.push({ unit: k + " turbo", label: "Bucket " + i, a: a, b: b, fmt: (x) => x + "×", delta: (x, y) => (isNum(x) && isNum(y) ? (y - x > 0 ? "+" : y - x < 0 ? MINUS : "") + Math.abs(y - x) + "×" : "–") });
      }
    }
    return out;
  }
  function fmtDeltaMv(a, b) { return isNum(a) && isNum(b) ? fmtMv(b - a) : "–"; }

  function renderProfileDiff() {
    const note = $("pr-diff-note"), tbl = $("pr-diff");
    tbl.hidden = true;
    const pa = PR.list.find((p) => p.name === $("pr-a").value), pb = PR.list.find((p) => p.name === $("pr-b").value);
    if (PR.list.length < 2) { note.textContent = "Needs two profiles."; return; }
    if (!pa || !pb || pa.name === pb.name) { note.textContent = "Pick two different profiles."; return; }
    const ca = stateOf(pa.name, PR.basis), cb = stateOf(pb.name, PR.basis);
    if (!ca || !cb) {
      const bad = !ca ? pa.name : pb.name;
      note.textContent = PR.info[bad] && PR.info[bad].error ? "Contents of \"" + bad + "\" unavailable: " + PR.info[bad].error : "Reading the contents of \"" + bad + "\"...";
      return;
    }
    const rows = compareRows(ca, cb);
    setText("pr-th-a", pa.name);
    setText("pr-th-b", pb.name);
    fill($("pr-diff-body"), rows.map((r) => {
      const tr = el("tr");
      const cell = (x) => td(x === null ? "–" : r.fmt(x), "n mono", x === null ? "not set by this profile" : "");
      tr.append(td(r.unit), td(r.label), cell(r.a), cell(r.b), td(r.delta(r.a, r.b), "n mono"));
      return tr;
    }));
    tbl.hidden = rows.length === 0;
    const basisNote = PR.list.some((p) => p.source === "accepted") ? (PR.basis === "applied" ? " Accepted profiles are compared as applied: the chain up to and including that step." : " Accepted profiles are compared by their own file only.") : "";
    note.textContent = (rows.length ? rows.length + " differences between \"" + pa.name + "\" and \"" + pb.name + "\"; the change is second minus first." : "These two profiles set the same values.") + basisNote;
  }

  // ---- Load into Tune: fill the editor (nothing is written until Review changes, then Apply) -------------------------------------------
  async function loadIntoTune(name) {
    const info = infoOf(name);
    const c = info ? (isObj(info.cumulative) ? info.cumulative : info.contents) : null;
    if (!c) return;
    if (!T.words) { setText("pr-load-status", "Read the ratios and voltages first (Tune): the editor shows each change against what the CPU holds now."); return; }
    if (unitsChanged().length || turboKinds().length) {
      const ok = await confirmDialog("Replace the edits in Tune?", "Tune holds unapplied choices. Loading \"" + name + "\" replaces them. Nothing is written either way.", "Replace");
      if (!ok) return;
    }
    T.draft = {}; T.turbo = {}; T.uiMode = {};
    T.expanded = new Set();
    T.scope = "unit";   // a profile is per unit
    syncSegmented($("tn-scope"), "unit");
    renderScopeNote();
    const skipped = [];
    for (const r of rowsOf(c)) {
      const u = r.target;
      if (!wUnit(u)) { skipped.push(u); continue; }
      const o = r.offset;
      if (isObj(o) && o.kind === "curve") setCurve([u], o.mv);
      else if (isObj(o) && o.kind === "points") {
        for (const k of Object.keys(o.mv)) {
          if (curPts(u).some((p) => p.ratio === Number(k))) setPoint([u], Number(k), o.mv[k]); else skipped.push(u + " " + k + "×");
        }
        T.uiMode[u] = "points";
        T.expanded.add(u);
      }
      if (isNum(r.v_target_v)) setVt([u], r.v_target_v);
      if (isNum(r.ratio)) setRatio([u], r.ratio);
    }
    const tu = isObj(c.turbo) ? c.turbo : {};
    for (const k of ["P", "E"]) {
      const cur = turboCur(k);
      if (Array.isArray(tu[k]) && tu[k].length === 8 && cur && tu[k].some((x, i) => x !== cur[i])) T.turbo[k] = tu[k].slice();
    }
    selectTab("tune", false);
    buildRows();
    const n = unitsChanged().length;
    setText("tn-status", "Loaded \"" + name + "\" into the editor: " + (n ? n + (n === 1 ? " unit differs" : " units differ") + " from what the CPU holds now." : "the CPU already holds these values.") +
      (skipped.length ? " Not shown (no such point in the read): " + skipped.join(", ") + "." : "") + " Nothing is written until you press Review changes and then Apply.");
    setText("pr-load-status", "");
  }

  function updateSaveState() {
    const v = $("pr-name").value;
    const exists = PR.list.some((p) => p.name === v);
    const note = $("pr-name-note");
    if (!v) note.textContent = "Lowercase letters, digits, dot, underscore, hyphen. Up to 64.";
    else if (!PROFILE_RE.test(v)) note.textContent = "Not allowed: use a-z, 0-9, dot, underscore, hyphen (1 to 64).";
    else if (exists) note.textContent = "A profile with this name exists. Names are never reused; pick another.";
    else note.textContent = unitsChanged().length ? "Will save " + unitsChanged().length + (unitsChanged().length === 1 ? " changed unit." : " changed units.") : "Nothing to save: change something in Tune first.";
    $("btn-pr-save").disabled = !PROFILE_RE.test(v) || exists || unitsChanged().length === 0;
  }

  async function saveProfile() {
    const name = $("pr-name").value;
    if (!PROFILE_RE.test(name) || unitsChanged().length === 0) return;
    const { status, body } = await api("profiles.save", { name: name, intent: buildIntent() });
    if (status === 200 && body.ok) {
      setText("pr-status", "Saved \"" + name + "\".");
      $("pr-name").value = "";
      await loadProfiles();
    } else {
      const ex = isObj(body.error) && isObj(body.error.detail) && body.error.detail.check === "EXISTS";
      setText("pr-status", ex ? "A profile named \"" + name + "\" already exists. Pick another name." : "Not saved: " + errText(body));
    }
  }

  function initProfiles() {
    $("btn-pr-refresh").addEventListener("click", () => { PR.info = {}; loadProfiles(); });
    $("pr-name").addEventListener("input", () => { updateSaveState(); });
    $("btn-pr-save").addEventListener("click", saveProfile);
    $("pr-a").addEventListener("change", renderProfileDiff);
    $("pr-b").addEventListener("change", renderProfileDiff);
    segmented($("pr-basis"), [{ value: "applied", label: "As applied", title: "An accepted profile counts as the state after its step of the chain" }, { value: "own", label: "This file only", title: "Only the entries in the profile's own file" }], PR.basis,
      (b) => { PR.basis = b; renderProfileDiff(); });
  }

  // ---- tab entry hooks -----------------------------------------------------------------------------------------------------------------------------
  function onTabShown(name) {
    if (name === "curves") { if (!CV.loaded && S.sseUp) loadCurves(false); renderCurves(); }
    else if (name === "tune") { if (!CV.loaded && S.sseUp) loadCurves(false); renderTunePreview(); updateTuneFoot(); }
    else if (name === "stress") { if (S.sseUp && !ST.loaded) loadStressList(); renderStress(); }
    else if (name === "profiles") { if (S.sseUp) loadProfiles(); updateSaveState(); }
    else if (name === "audit") loadAudit();
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
    api("telemetry.start", { interval_s: S.intervalS });  // read-only sampler; the same interval again is fine, a new one restarts it
    loadPreflight();
    loadAudit();
    loadMem();
    loadCurves(false);
    loadProfiles();
    loadLastRead();
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
    on("vf.point", onVfPoint);
    on("result", onResult);
    on("state", (d) => { if (typeof d.state === "string") { S.hbState = d.state; renderHelper(); updateGate(); } });
  }

  // ---- wiring ----------------------------------------------------------------------------------------------------------------------
  for (const t of TABS) $("tab-" + t).addEventListener("click", () => selectTab(t, false));
  document.querySelector("[role=tablist]").addEventListener("keydown", onTabKey);
  for (const b of document.querySelectorAll("[data-goto]")) b.addEventListener("click", () => selectTab(b.getAttribute("data-goto"), true));

  const prefs = loadPrefs();
  if (isNum(prefs.refresh_s) && REFRESH_STEPS.indexOf(prefs.refresh_s) >= 0) S.intervalS = prefs.refresh_s;
  if ([1, 5, 15].indexOf(prefs.win_min) >= 0) V.winMin = prefs.win_min;
  $("refresh-range").value = String(REFRESH_STEPS.indexOf(S.intervalS));
  $("refresh-val").textContent = refreshLabel(REFRESH_STEPS.indexOf(S.intervalS));
  $("refresh-range").setAttribute("aria-valuetext", refreshLabel(REFRESH_STEPS.indexOf(S.intervalS)));
  $("refresh-range").addEventListener("input", onRefreshInput);

  segmented($("dash-sub"), [{ value: "overview", label: "Overview" }, { value: "cores", label: "Cores" }, { value: "sensors", label: "Sensors" }], V.sub,
    (s) => selectTab("dashboard", false, s));
  segmented($("win-seg"), [{ value: 1, label: "1 min" }, { value: 5, label: "5 min" }, { value: 15, label: "15 min" }], V.winMin,
    (m) => { V.winMin = m; savePrefs(); renderVisible(); });
  $("btn-pause").addEventListener("click", () => setPaused(!V.paused));
  segmented($("map-metric"), Object.keys(METRICS).map((k) => ({ value: k, label: METRICS[k].label })), V.metric,
    (k) => { V.metric = k; V.scaleLock = null; $("btn-scale-lock").setAttribute("aria-pressed", "false"); buildRamp(); renderDie(); });
  segmented($("map-scale"), [{ value: "fixed", label: "Fixed scale" }, { value: "auto", label: "Auto scale" }], V.scaleMode,
    (m) => { V.scaleMode = m; V.scaleLock = null; $("btn-scale-lock").setAttribute("aria-pressed", "false"); $("btn-scale-lock").disabled = m !== "auto"; renderDie(); });
  $("btn-scale-lock").disabled = true;
  $("btn-scale-lock").addEventListener("click", () => {
    if (V.scaleLock) V.scaleLock = null; else V.scaleLock = mapRange();
    $("btn-scale-lock").setAttribute("aria-pressed", V.scaleLock ? "true" : "false");
    renderDie();
  });
  fill($("core-cols"), [["busy", "Busy"], ["ratio", "Ratio"], ["vid", "VID"], ["temp", "Temp"], ["spark", "Trend"]].map(([k, label]) => {
    const b = el("button", "seg-btn", label);   // independent toggles, not a single choice
    b.type = "button";
    b.setAttribute("aria-pressed", "true");
    b.title = "Show or hide the " + label + " column";
    b.addEventListener("click", () => { V.cols[k] = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", V.cols[k] ? "true" : "false"); applyCols(); });
    return b;
  }));
  buildRamp();
  initCharts();
  buildCoreHead();
  syncCoreSet({});
  initCurves();
  initTune();
  initStress();
  initProfiles();
  $("btn-preflight").addEventListener("click", loadPreflight);
  $("btn-audit").addEventListener("click", loadAudit);
  $("btn-mem-refresh").addEventListener("click", loadMem);
  $("btn-mem-all").addEventListener("click", openTimings);
  $("btn-timings-close").addEventListener("click", () => closeDialog($("dlg-timings")));
  setInterval(() => { renderAges(); updateGate(); }, 1000);
  const h0 = parseHash(location.hash);
  if (h0.sub) V.sub = h0.sub;
  selectTab(h0.tab, false);
  renderHelper();
  updateGate();
  connect();
})();
