// views/dashboard.js: Dashboard screen (PLAN §4.4): live tiles, core map, history charts, mailbox-ops card, memory card.
// Port of web/app.js "Dashboard > Overview": the six readout tiles + limit reasons (setTile :109-115, tile fill :513-560), the core map (:640-700), the
// power/temperature charts (makeChart :721), "Mailbox ops this boot" (renderHelper :476-494, drawOpsSpark :470-475) and the Memory card (loadMem/renderMem
// :1464-1510). Data comes from the store (telemetry samples/history are already derived there); the view only presents it. Differences from the page: the
// core map is built from preflight.topology (D-036), and mem.read is a button, never automatic (each mem.read is a root helper request; the H1 sitting allows
// only preflight/telemetry/audit.tail/profiles.list/tune.last).
// Every reading that is unavailable shows "n/a" and its reason (tile text, tooltip, chart overlay), never 0 and never a guess.
import { h, svg, clear } from "../components/dom.js";
import { fmtNum, fmtClock, NA } from "../components/fmt.js";
import { SERIES } from "../store.js";
import { createSpark, installStyles, installScreenStyles, segmented } from "../components/spark.js";
import { createChart } from "../components/chart.js";
import { createCoreMap, METRICS, METRIC_KEYS } from "../components/coremap.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const isNum = (x) => typeof x === "number" && Number.isFinite(x);

export const HISTORY_S = 900; // chart buffer: 15 minutes (web HISTORY_MS)
export const WINDOWS = [
  { value: "60", label: "1 min", s: 60 },
  { value: "300", label: "5 min", s: 300 },
  { value: "900", label: "15 min", s: 900 },
];
export const OPS_KEEP = 180;

// ---------------------------------------------------------------- pure models (unit-tested)

/** A live tile from a store sample ({value, reason, source, unit}) and its history. `def` is the SERIES entry. */
export function tileModel(def, sample, history = []) {
  if (!sample) {
    return { key: def.key, label: def.label, na: true, text: NA, unit: "", reason: "no telemetry yet", source: "", values: [], title: `${def.label}: n/a · no telemetry yet` };
  }
  const have = sample.value !== null && sample.value !== undefined && Number.isFinite(sample.value);
  return {
    key: def.key,
    label: def.label,
    na: !have,
    text: have ? fmtNum(sample.value, def.digits) : NA,
    unit: have ? def.unit : "",
    reason: have ? "" : sample.reason || "not reported",
    source: sample.source || "",
    values: history,
    title: have ? `${def.label}${sample.source ? " · " + sample.source : ""}` : `${def.label}: n/a · ${sample.reason || "not reported"}${sample.source ? " (" + sample.source + ")" : ""}`,
  };
}

/** The limit-reasons tile from store.telemetry.limits ({state: 'na'|'none'|'raw', raw, reason, source}). Named reasons arrive with the power controls (PLAN §6.1). */
export function limitTileModel(l) {
  if (!l || l.state === "na") {
    const reason = (l && l.reason) || "no telemetry yet";
    return { na: true, tone: "", text: NA, sub: reason, title: `Limit reasons: n/a · ${reason}${l && l.source ? " (" + l.source + ")" : ""}` };
  }
  if (l.state === "none") return { na: false, tone: "ok", text: "none", sub: "no limit active", title: `No limit bit set (${l.source || "limit-reason word"})` };
  return {
    na: false,
    tone: "warn",
    text: `0x${l.raw.toString(16).toUpperCase()}`,
    sub: "raw limit bits (named reasons come with the power controls)",
    title: `Raw ${l.source || "limit-reason"} word`,
  };
}

/** Append the newest telemetry to the chart buffer: [{t, v:{series key: number|null}}], capped to HISTORY_S seconds, one entry per sample time. */
export function appendChartSample(buf, telemetry, keepS = HISTORY_S) {
  const t = telemetry && Number.isFinite(telemetry.t) ? telemetry.t : null;
  if (t === null || !telemetry.samples) return buf;
  if (buf.length && buf[buf.length - 1].t >= t) return buf;
  const v = {};
  for (const s of SERIES) v[s.key] = telemetry.samples[s.key] ? telemetry.samples[s.key].value : null;
  const next = [...buf, { t, v }];
  const cut = t - keepS;
  return next[0].t < cut ? next.filter((p) => p.t >= cut) : next;
}

/** Reasons for series that have no reading, for the chart overlay: {key: reason}. */
export function chartReasons(telemetry) {
  const out = {};
  if (!telemetry || !telemetry.samples) return out;
  for (const s of SERIES) {
    const smp = telemetry.samples[s.key];
    if (smp && smp.value === null && smp.reason) out[s.key] = smp.reason;
  }
  return out;
}

/** Ops-this-boot card model from store.helperOp. */
export function opsModel(op) {
  const used = op && isNum(op.opsUsed) ? op.opsUsed : null;
  const budget = op && isNum(op.opsBudget) && op.opsBudget > 0 ? op.opsBudget : null;
  return {
    used,
    budget,
    fraction: used !== null && budget !== null ? Math.min(1, used / budget) : null,
    usedText: used === null ? NA : String(used),
    capText: budget === null ? "per-boot cap n/a" : `per-boot cap ${budget}`,
    state: op && op.state ? op.state : null,
  };
}

/** Memory card model from a mem.read result (web renderMem :1484). -> {available:false, reason} | {available:true, headline, primary, rows:[[k,v]], dimms, summary} */
export function memModel(r) {
  if (!isObj(r)) return { available: false, reason: "the helper gave no answer" };
  if (r.available !== true) return { available: false, reason: typeof r.reason === "string" && r.reason ? r.reason : "the helper gave no reason" };
  const t = isObj(r.timings) ? r.timings : {};
  const tm = (k) => (isNum(t[k]) ? String(t[k]) : NA);
  const cc = isObj(r.cross_check) ? r.cross_check : {};
  const dimms = (Array.isArray(r.dimms) ? r.dimms : []).filter(isObj).map((d) => ({
    locator: String(d.locator ?? ""),
    desc: [d.size, d.part, d.manufacturer].filter(Boolean).join(" · "),
    temp: isObj(d.temp_c) ? (isNum(d.temp_c.value) && d.temp_c.status !== "unavailable" ? d.temp_c.value : null) : isNum(d.temp_c) ? d.temp_c : null,
    tempReason: isObj(d.temp_c) && typeof d.temp_c.reason === "string" ? d.temp_c.reason : "no reading",
  }));
  return {
    available: true,
    headline: `${isNum(r.mts) ? "DDR5-" + r.mts : "DDR5"} · Gear ${isNum(r.gear) ? r.gear : NA} · ${typeof r.cmd_rate === "string" ? r.cmd_rate : NA}`,
    primary: ["tCL", "tRCD", "tRP", "tRAS"].map(tm).join("-"),
    rows: [
      ["tRFC", tm("tRFC")],
      ["tREFI", tm("tREFI")],
      ["Memory clock", isNum(r.qclk_mhz) ? `${r.qclk_mhz.toFixed(1)} MHz` : NA],
      ["BCLK", isNum(r.bclk_mhz) ? `${r.bclk_mhz.toFixed(1)} MHz` : NA],
      ["Peak bandwidth", isNum(r.max_bandwidth_mbs) ? `${(r.max_bandwidth_mbs / 1000).toFixed(1)} GB/s` : NA],
      ["Cross-check", cc.agree === true ? "agrees" : cc.agree === false ? "disagrees" : NA],
    ],
    dimms,
    summary: `${dimms.length} ${dimms.length === 1 ? "module" : "modules"}${isNum(r.instances_ok) ? `, read from ${r.instances_ok} controller instances` : ""}`,
  };
}

/** "3 s ago" style age for the header; null time = "no samples yet". */
export function ageText(t, nowS) {
  if (!isNum(t)) return "no samples yet";
  const d = Math.max(0, Math.round(nowS - t));
  return d < 2 ? "just now" : d < 120 ? `${d} s ago` : `${Math.round(d / 60)} min ago`;
}

// ---------------------------------------------------------------- view state kept across mounts (per store)
const PREFS = new WeakMap(); // store -> {metric, scale, windowS}
const CHART_BUF = new WeakMap(); // store -> samples (filled while the screen is open)
const OPS_BUF = new WeakMap(); // store -> numbers
const MEM = new WeakMap(); // store -> memory card state {phase, result, error}

const CSS = `
.dash-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: var(--space-3); }
.tile { display: flex; flex-direction: column; gap: 2px; min-width: 0; padding: var(--space-2) var(--space-3); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.tile-label { font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-dim); }
.tile-read { display: flex; align-items: baseline; gap: 4px; min-height: 30px; }
.tile-value { font-size: 26px; font-weight: 600; line-height: 1.15; font-variant-numeric: tabular-nums; }
.tile-unit { font-size: var(--fs-sm); color: var(--text-dim); }
.tile.na .tile-value { font-size: var(--fs-lg); font-weight: 500; color: var(--text-faint); }
.tile[data-tone="ok"] .tile-value { color: var(--ok); }
.tile[data-tone="warn"] .tile-value { color: var(--warn); }
.tile-sub { font-size: var(--fs-xs); color: var(--text-faint); line-height: 1.3; min-height: 2.6em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.dash-main { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: var(--space-4); align-items: start; }
.dash-side { display: flex; flex-direction: column; gap: var(--space-4); }
.dash-charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap: var(--space-4) var(--space-5); }
.ops { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: var(--space-3); align-items: center; }
.ops-ring { width: 64px; height: 64px; }
.ops-ring circle { fill: none; stroke-width: 6; }
.ops-ring .bg { stroke: var(--surface-3); }
.ops-ring .fg { stroke: var(--ok); stroke-linecap: round; }
.ops-ring .fg.zero { display: none; }
.ops-big { font-size: var(--fs-xl); font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.1; }
.ops-spark { grid-column: 1 / -1; }
.mem-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(220px, 1fr); gap: var(--space-5); }
.mem-big { font-size: var(--fs-lg); font-weight: 600; margin-right: var(--space-3); }
.mem-prim { font-family: var(--font-mono); color: var(--text-dim); }
.dimm-list { list-style: none; margin: var(--space-2) 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-sm); }
.dimm-list li { display: grid; grid-template-columns: 90px minmax(0, 1fr) 64px; gap: var(--space-2); }
.dimm-list .t { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 1100px) { .dash-main, .mem-grid { grid-template-columns: minmax(0, 1fr); } }
`;

function el(tag, cls, text) {
  return h(tag, { class: cls, text });
}

export function dashboardView() {
  const subs = [];
  let timer = null;
  return {
    id: "dashboard",
    mount(root, ctx) {
      const { store, bridge } = ctx;
      installScreenStyles();
      installStyles("dashboard", CSS);
      const prefs = PREFS.get(store) || { metric: "mhz", scale: "fixed", windowS: 60 };
      PREFS.set(store, prefs);
      if (!CHART_BUF.has(store)) CHART_BUF.set(store, []);
      if (!OPS_BUF.has(store)) OPS_BUF.set(store, []);

      // ---- header
      const meta = h("span", { class: "sc-meta", id: "dash-meta" });
      const head = h("header", { class: "screen-head" }, h("h1", { text: "Dashboard" }), meta);

      // ---- tiles
      const tiles = new Map();
      const tilesBox = h("section", { class: "dash-tiles", "aria-label": "Live readings" });
      for (const def of SERIES) {
        const spark = createSpark({ w: 160, h: 30 });
        const value = el("span", "tile-value num", NA);
        const unit = el("span", "tile-unit", "");
        const sub = el("div", "tile-sub", "");
        const tile = h("div", { class: "tile", dataset: { series: def.key } }, el("div", "tile-label", def.label), h("div", { class: "tile-read" }, value, unit), spark.el, sub);
        tiles.set(def.key, { tile, value, unit, sub, spark });
        tilesBox.append(tile);
      }
      const limVal = el("span", "tile-value num", NA);
      const limSub = el("div", "tile-sub", "");
      const limTile = h("div", { class: "tile", dataset: { series: "limits" } }, el("div", "tile-label", "Limit reasons"), h("div", { class: "tile-read" }, limVal), limSub);
      tilesBox.append(limTile);

      // ---- core map
      const map = createCoreMap();
      const metricSeg = segmented(METRIC_KEYS.map((k) => ({ value: k, label: METRICS[k].label })), prefs.metric, (v) => { prefs.metric = v; drawMap(); }, { label: "Core map metric" });
      const scaleSeg = segmented([{ value: "fixed", label: "Fixed" }, { value: "auto", label: "Auto" }], prefs.scale, (v) => { prefs.scale = v; drawMap(); }, { label: "Core map colour scale" });
      const mapCard = h("section", { class: "dcard", id: "dash-map" }, h("div", { class: "dcard-head" }, h("h2", { text: "Cores" }), metricSeg.el, h("span", { class: "spacer" }), scaleSeg.el), map.el);
      const drawMap = () => {
        const st = store.get();
        map.update(st.preflight && st.preflight.topology ? st.preflight.topology.units : [], st.telemetry.values, prefs);
      };

      // ---- ops card
      const opsSpark = createSpark({ w: 260, h: 28 });
      const opsBig = el("div", "ops-big num", NA);
      const opsCap = el("div", "meta", "");
      const opsState = el("div", "meta", "");
      const CIRC = 2 * Math.PI * 26;
      const ringFg = svg("circle", { class: "fg", cx: 32, cy: 32, r: 26, transform: "rotate(-90 32 32)", "stroke-dasharray": `0 ${CIRC.toFixed(2)}` });
      const ringSvg = svg("svg", { class: "ops-ring", viewBox: "0 0 64 64", "aria-hidden": "true" }, svg("circle", { class: "bg", cx: 32, cy: 32, r: 26 }), ringFg);
      const opsCard = h(
        "section",
        { class: "dcard", id: "dash-ops" },
        h("div", { class: "dcard-head" }, h("h2", { text: "Mailbox ops this boot" })),
        h("div", { class: "ops" }, ringSvg, h("div", {}, opsBig, opsCap, opsState), h("div", { class: "ops-spark" }, opsSpark.el)),
        h("p", { class: "dcard-note", text: "The guard refuses past the cap; it is a limit, not a target. Telemetry is not mailbox traffic." }),
      );

      // ---- charts
      const winSeg = segmented(WINDOWS.map((w) => ({ value: w.value, label: w.label })), String(prefs.windowS), (v) => { prefs.windowS = Number(v); charts.forEach((c) => c.setWindow(prefs.windowS)); }, { label: "Chart window" });
      const charts = [
        createChart({ title: "Power", unit: "W", digits: 1, minSpan: 20, windowS: prefs.windowS, name: "power", series: [{ key: "pkg_power", label: "Package" }, { key: "core_power", label: "Core" }] }),
        createChart({ title: "Hottest sensor", unit: "°C", digits: 0, minSpan: 10, windowS: prefs.windowS, name: "temp", series: [{ key: "temp", label: "" }] }),
        createChart({ title: "Highest clock", unit: "MHz", digits: 0, minSpan: 500, windowS: prefs.windowS, name: "clock", series: [{ key: "clock", label: "" }] }),
        createChart({ title: "Vcore", unit: "V", digits: 3, minSpan: 0.05, windowS: prefs.windowS, name: "vcore", series: [{ key: "vcore", label: "" }] }),
      ];
      const chartCard = h("section", { class: "dcard", id: "dash-charts" }, h("div", { class: "dcard-head" }, h("h2", { text: "History" }), winSeg.el), h("div", { class: "dash-charts" }, ...charts.map((c) => c.el)));

      // ---- memory card
      const memBody = h("div", { id: "mem-body" });
      const memBtn = h("button", { type: "button", class: "btn", id: "mem-read", text: "Read memory", on: { click: () => readMemory() } });
      const memCard = h("section", { class: "dcard", id: "dash-mem" }, h("div", { class: "dcard-head" }, h("h2", { text: "Memory" }), h("span", { class: "spacer" }), memBtn), memBody);

      root.append(h("div", { class: "sc dash" }, head, tilesBox, h("div", { class: "dash-main" }, mapCard, h("div", { class: "dash-side" }, opsCard)), chartCard, memCard));

      // ---- drawing
      const drawTiles = (st) => {
        const t = st.telemetry;
        for (const def of SERIES) {
          const m = tileModel(def, t.samples ? t.samples[def.key] : null, t.history[def.key] || []);
          const c = tiles.get(def.key);
          c.value.textContent = m.text;
          c.unit.textContent = m.unit;
          c.sub.textContent = m.na ? `n/a · ${m.reason}` : m.source;
          c.tile.classList.toggle("na", m.na);
          c.tile.title = m.title;
          c.spark.draw(m.values);
        }
        const lm = limitTileModel(t.limits);
        limVal.textContent = lm.text;
        limSub.textContent = lm.sub;
        limTile.classList.toggle("na", lm.na);
        limTile.dataset.tone = lm.tone;
        limTile.title = lm.title;
      };
      const drawCharts = (st) => {
        const buf = appendChartSample(CHART_BUF.get(store), st.telemetry);
        CHART_BUF.set(store, buf);
        const now = Date.now() / 1000;
        const reasons = chartReasons(st.telemetry);
        for (const c of charts) c.update(buf, buf.length ? Math.max(now, buf[buf.length - 1].t) : now, reasons);
      };
      const drawOps = (st) => {
        const m = opsModel(st.helperOp);
        opsBig.textContent = m.usedText;
        opsCap.textContent = m.capText;
        opsState.textContent = m.state ? `helper ${m.state}` : "helper state n/a";
        ringFg.setAttribute("stroke-dasharray", `${((m.fraction ?? 0) * CIRC).toFixed(2)} ${CIRC.toFixed(2)}`);
        ringFg.classList.toggle("zero", !m.fraction);
        const buf = OPS_BUF.get(store);
        if (m.used !== null && buf[buf.length - 1] !== m.used) OPS_BUF.set(store, [...buf, m.used].slice(-OPS_KEEP));
        opsSpark.draw(OPS_BUF.get(store));
      };
      const drawMeta = () => {
        const t = store.get().telemetry.t;
        const s = store.get();
        const live = isNum(t) && Date.now() / 1000 - t < 6;
        meta.textContent = s.link.state !== "connected" ? `helper ${s.link.state.replace("_", " ")} · no live data` : `${live ? "live" : "waiting"} · last sample ${ageText(t, Date.now() / 1000)}${isNum(t) ? ` (${fmtClock(t)})` : ""}`;
        meta.dataset.live = live ? "1" : "0";
      };

      const drawMem = () => {
        const m = MEM.get(store) || { phase: "idle" };
        clear(memBody);
        memBtn.disabled = m.phase === "loading";
        memBtn.textContent = m.phase === "idle" ? "Read memory" : "Read again";
        if (m.phase === "idle") {
          memBody.append(h("p", { class: "na", text: "n/a · not read yet. Memory settings are static between boots: press Read memory to ask the helper once." }));
        } else if (m.phase === "loading") {
          memBody.append(h("p", { class: "muted", text: "Reading…" }));
        } else if (m.phase === "error") {
          memBody.append(h("p", { class: "na", text: `n/a · ${m.error}` }));
        } else {
          const mm = memModel(m.result);
          if (!mm.available) {
            memBody.append(h("p", { class: "na", text: `n/a · ${mm.reason}` }));
          } else {
            const list = h("ul", { class: "dimm-list" });
            for (const d of mm.dimms) {
              list.append(h("li", {}, h("span", { text: d.locator }), h("span", { class: "muted", text: d.desc }), h("span", { class: ["t", d.temp === null ? "na" : ""], title: d.temp === null ? `n/a · ${d.tempReason}` : "", text: d.temp === null ? NA : `${d.temp.toFixed(1)} °C` })));
            }
            if (!mm.dimms.length) list.append(h("li", { class: "na" }, "n/a · no DIMM data from SMBIOS"));
            memBody.append(
              h(
                "div",
                { class: "mem-grid" },
                h("div", {}, h("div", {}, h("span", { class: "mem-big", text: mm.headline }), h("span", { class: "mem-prim", text: mm.primary })), h("p", { class: "meta", text: mm.summary }), list),
                h("dl", { class: "kv" }, ...mm.rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
              ),
            );
          }
        }
      };
      async function readMemory() {
        MEM.set(store, { phase: "loading" });
        drawMem();
        const res = await bridge.call("mem.read", {});
        MEM.set(store, res.ok ? { phase: "done", result: res.result } : { phase: "error", error: res.error && res.error.message ? res.error.message : "the helper could not read memory" });
        drawMem();
      }

      subs.push(
        store.watch((s) => s.telemetry, (_t, st) => { drawTiles(st); drawCharts(st); drawMap(); drawMeta(); }),
        store.watch((s) => s.preflight, () => drawMap()),
        store.watch((s) => s.helperOp, (_o, st) => drawOps(st)),
        store.watch((s) => s.link, () => drawMeta()),
      );
      drawMem();
      timer = setInterval(drawMeta, 1000);
    },
    unmount() {
      subs.splice(0).forEach((u) => u());
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}
