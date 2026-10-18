// views/tune/grid.js: the tile grid of the Cores tab (PLAN §4.2): P0..P7, EC0..EC3 (four CPUs each) and the Ring; Ratio / Monitors toggle like XTU; quick chips (All P, All E, Favored, All).
// Tiles are toggle buttons in a roving-tabindex group: arrows move, Enter/Space selects, Shift = range, Ctrl = toggle (Ctrl+Space from the keyboard), Ctrl+A = all.
// The pure parts (selection rules, tile faces) are exported for node tests; the DOM part keeps its tiles and only rewrites their text, so focus survives every update.
import { h, clear } from "../../components/dom.js";
import { isNum, fmtMv, fmtRatio, TIMES } from "../../lib/format.js";
import { RING_ID, unitKind, runsUpTo, ocClass, favoredIds, quickSelections } from "../../lib/units.js";
import { effPointMv, effRatio } from "../../lib/draft.js";

/** Selection after a click / key on tile `id`. Returns {ids (model order), anchor}. */
export function nextSelection(model, current, anchor, id, { ctrl = false, shift = false } = {}) {
  const order = model.order;
  let set;
  if (shift && order.includes(anchor)) {
    const a = order.indexOf(anchor);
    const b = order.indexOf(id);
    const range = order.slice(Math.min(a, b), Math.max(a, b) + 1);
    set = new Set(ctrl ? [...current, ...range] : range);
    return { ids: order.filter((x) => set.has(x)), anchor };
  }
  if (ctrl) {
    set = new Set(current);
    if (set.has(id)) set.delete(id);
    else set.add(id);
    return { ids: order.filter((x) => set.has(x)), anchor: id };
  }
  return { ids: [id], anchor: id };
}

export const sameIds = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

const leafNum = (l) => (l && typeof l === "object" && l.status !== "unavailable" && typeof l.value === "number" && Number.isFinite(l.value) ? l.value : null);

/** Live (Monitors) numbers of a unit from a telemetry `values` object: highest clock of its CPUs, hottest per-core temp if reported, highest VID. Null = n/a. */
export function liveFace(unit, values) {
  const cores = (values && values.cores) || {};
  const pick = (f) => {
    const xs = unit.cpus.map((c) => leafNum(cores[String(c)] && cores[String(c)][f])).filter(isNum);
    return xs.length ? Math.max(...xs) : null;
  };
  const temps = (values && values.temps) || {};
  const tv = unit.cpus.map((c) => leafNum(temps["Core " + c] || temps["cpu" + c] || temps["CPU " + c])).filter(isNum);
  return { mhz: pick("mhz"), temp: tv.length ? Math.max(...tv) : null, vid: pick("vid") };
}

/** What a tile shows in Ratio mode. */
export function tileFace(model, draft, id, favored) {
  const unit = model.units[id];
  if (unit.kind === "R") {
    const offs = unit.points.map((p) => effPointMv(draft, unit, p)).filter(isNum);
    return { id, kind: "R", title: "Ring", sub: unit.points.length + " V/F points", big: null, bigLabel: "ring", badge: null, deepest: offs.length ? Math.min(...offs) : null, pending: !!draft.units[id], favored: false };
  }
  const run = runsUpTo(model, unit);
  const r = effRatio(draft, unit);
  const cls = ocClass(unit, r);
  const big = isNum(run) || isNum(r) ? Math.max(isNum(run) ? run : 0, isNum(r) ? r : 0) : null;
  const offs = unit.points.map((p) => effPointMv(draft, unit, p)).filter(isNum);
  return {
    id,
    kind: unit.kind,
    title: unit.kind === "P" ? "P" + id.slice(1) : id,
    sub: unit.kind === "P" ? "cpu " + unit.cpu : "cpu " + unit.cpus[0] + "–" + unit.cpus[unit.cpus.length - 1],
    big,
    bigLabel: "max",
    badge: cls === "oc" ? "OC " + r + TIMES : cls === "middle" ? "OC " + r + TIMES + " (offset only)" : null,
    deepest: offs.length ? Math.min(...offs) : null,
    pending: !!draft.units[id],
    favored: favored.includes(id),
  };
}

const GHZ = (mhz) => (isNum(mhz) ? (mhz / 1000).toFixed(2) + " GHz" : "n/a");

/** The whole grid. `tc` is the Tune controller (state(), setSelection(), setTileMode()). Returns {el, update(), dispose()}. */
export function createGrid(tc) {
  let built = null; // the model the tiles were built for
  const tiles = new Map();
  const el = h("section", { class: "tune-grid", "aria-label": "Cores" });
  const chips = h("div", { class: "tile-chips", role: "group", "aria-label": "Quick selection" });
  const mode = h("div", { class: "seg", role: "group", "aria-label": "Tile contents" });
  const rowP = h("div", { class: "tile-row p-row", role: "group", "aria-label": "Performance cores" });
  const rowE = h("div", { class: "tile-row e-row", role: "group", "aria-label": "Efficient-core clusters and ring" });
  const hint = h("p", { class: "muted tile-hint" });
  const modeBtns = {
    ratio: h("button", { type: "button", class: "seg-btn", "aria-pressed": "true", text: "Ratio", title: "Tiles show the ratio, OC state and deepest offset", on: { click: () => tc.setTileMode("ratio") } }),
    live: h("button", { type: "button", class: "seg-btn", "aria-pressed": "false", text: "Monitors", title: "Tiles show the live clock, temperature and VID (telemetry)", on: { click: () => tc.setTileMode("live") } }),
  };
  mode.append(modeBtns.ratio, modeBtns.live);
  el.append(h("div", { class: "grid-bar" }, chips, h("span", { class: "spacer" }), mode), rowP, rowE, hint);

  const chipEls = {};
  function build(model) {
    built = model;
    tiles.clear();
    clear(rowP);
    clear(rowE);
    clear(chips);
    const q = quickSelections(model);
    const defs = [
      ["P", "All P", q.P, "Select every P-core"],
      ["E", "All E", q.E, "Select every E-cluster"],
      ["favored", "Favored ★", q.favored, q.favored.length ? "Select the favored cores (highest 0x1C ratio)" : "All P-cores have the same favored ratio, so none stands out"],
      ["all", "All", q.all, "Select every P-core and E-cluster (the ring is selected on its own)"],
    ];
    for (const [key, label, ids, title] of defs) {
      const b = h("button", { type: "button", class: "chip toggle", text: label, title, "aria-pressed": "false", dataset: { chip: key }, on: { click: () => tc.setSelection(ids, ids[0] || null) } });
      b.disabled = ids.length === 0;
      chipEls[key] = { el: b, ids };
      chips.append(b);
    }
    for (const id of model.order) {
      const kind = unitKind(id);
      const t = {
        name: h("span", { class: "tile-name", text: "" }),
        star: h("span", { class: "tile-star", text: "★", title: "Favored core: the highest 0x1C ratio", hidden: true }),
        dot: h("span", { class: "tile-dot", title: "Has pending changes", hidden: true }),
        big: h("span", { class: "tile-big num" }),
        bigLabel: h("span", { class: "tile-biglabel" }),
        badge: h("span", { class: "tile-badge", hidden: true }),
        sub: h("span", { class: "tile-sub" }),
        foot: h("span", { class: "tile-foot num" }),
      };
      const btn = h(
        "button",
        {
          type: "button",
          class: ["tile", "kind-" + kind],
          dataset: { unit: id, kind },
          "aria-pressed": "false",
          tabindex: "-1",
          on: { click: (e) => pick(id, e.ctrlKey || e.metaKey, e.shiftKey), keydown: (e) => keyOn(e, id) },
        },
        h("span", { class: "tile-head" }, t.name, t.star, t.dot, h("span", { class: "spacer" }), t.sub),
        h("span", { class: "tile-body" }, t.big, t.bigLabel),
        t.badge,
        t.foot,
      );
      tiles.set(id, { btn, t, kind });
      (kind === "P" ? rowP : rowE).append(btn);
    }
  }

  function pick(id, ctrl, shift) {
    const s = tc.state();
    const sel = nextSelection(s.model, s.selection, s.anchor, id, { ctrl, shift });
    tc.setSelection(sel.ids, sel.anchor);
  }

  function focusTile(id) {
    const tile = tiles.get(id);
    if (!tile) return;
    for (const x of tiles.values()) x.btn.tabIndex = -1;
    tile.btn.tabIndex = 0;
    tile.btn.focus();
  }

  function neighbour(id, dx, dy) {
    const ids = [...tiles.keys()];
    const i = ids.indexOf(id);
    if (dx) return ids[Math.min(ids.length - 1, Math.max(0, i + dx))];
    // up/down by geometry: the nearest row above/below, and in it the tile whose centre is closest
    const me = tiles.get(id).btn.getBoundingClientRect();
    const cx = me.left + me.width / 2;
    const cy = me.top + me.height / 2;
    let best = null;
    for (const [other, tile] of tiles) {
      const r = tile.btn.getBoundingClientRect();
      const oy = r.top + r.height / 2 - cy;
      if (dy > 0 ? oy < me.height / 2 : oy > -me.height / 2) continue;
      const score = [Math.abs(oy), Math.abs(r.left + r.width / 2 - cx)];
      if (best === null || score[0] < best.s[0] - 4 || (Math.abs(score[0] - best.s[0]) <= 4 && score[1] < best.s[1])) best = { id: other, s: score };
    }
    return best ? best.id : id;
  }

  function keyOn(e, id) {
    const k = e.key;
    if (k === "ArrowLeft" || k === "ArrowRight" || k === "ArrowUp" || k === "ArrowDown") {
      e.preventDefault();
      focusTile(neighbour(id, k === "ArrowLeft" ? -1 : k === "ArrowRight" ? 1 : 0, k === "ArrowUp" ? -1 : k === "ArrowDown" ? 1 : 0));
    } else if (k === "Home" || k === "End") {
      e.preventDefault();
      const ids = [...tiles.keys()];
      focusTile(k === "Home" ? ids[0] : ids[ids.length - 1]);
    } else if (k === "Enter" || k === " ") {
      e.preventDefault();
      pick(id, e.ctrlKey || e.metaKey, e.shiftKey);
    } else if ((k === "a" || k === "A") && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      const all = tc.state().model.order.filter((x) => x !== RING_ID);
      tc.setSelection(all, all[0] || null);
    }
  }

  function update() {
    const s = tc.state();
    const model = s.model;
    if (!model) return;
    if (built !== model) build(model);
    const fav = favoredIds(model);
    const live = s.tileMode === "live";
    const values = live ? tc.store.get().telemetry.values : null;
    modeBtns.ratio.setAttribute("aria-pressed", String(!live));
    modeBtns.live.setAttribute("aria-pressed", String(live));
    for (const [id, { btn, t }] of tiles) {
      const f = tileFace(model, s.draft, id, fav);
      const unit = model.units[id];
      const selected = s.selection.includes(id);
      btn.setAttribute("aria-pressed", String(selected));
      btn.classList.toggle("selected", selected);
      btn.classList.toggle("pending", f.pending);
      t.name.textContent = f.title;
      t.sub.textContent = f.sub;
      t.star.hidden = !f.favored;
      t.dot.hidden = !f.pending;
      const label = [f.title, unit.kind === "R" ? "ring" : unit.kind === "P" ? "P-core" : "E-cluster", f.favored ? "favored" : null, f.pending ? "pending changes" : null].filter(Boolean).join(", ");
      btn.setAttribute("aria-label", label);
      if (live) {
        const lf = liveFace(unit, values);
        t.big.textContent = unit.kind === "R" ? "–" : GHZ(lf.mhz);
        t.bigLabel.textContent = "clock";
        t.badge.hidden = true;
        t.foot.textContent = unit.kind === "R" ? "no per-core live data" : (isNum(lf.temp) ? lf.temp.toFixed(0) + " °C" : "temp n/a") + " · " + (isNum(lf.vid) ? lf.vid.toFixed(3) + " V" : "VID n/a");
      } else {
        t.big.textContent = unit.kind === "R" ? String(unit.points.length) : isNum(f.big) ? fmtRatio(f.big) : "–";
        t.bigLabel.textContent = unit.kind === "R" ? "V/F points" : f.bigLabel;
        t.badge.hidden = !f.badge;
        t.badge.textContent = f.badge || "";
        t.foot.textContent = isNum(f.deepest) ? fmtMv(f.deepest) : "no offsets";
        t.foot.title = "Deepest offset on this unit's V/F points" + (unit.kind === "R" ? "" : "; the big number is the highest ratio it runs now");
      }
    }
    let rove = false;
    for (const { btn } of tiles.values()) {
      const keep = document.activeElement === btn;
      if (keep) rove = true;
    }
    if (!rove) {
      for (const x of tiles.values()) x.btn.tabIndex = -1;
      const first = s.selection.find((id) => tiles.has(id)) || [...tiles.keys()][0];
      if (first) tiles.get(first).btn.tabIndex = 0;
    }
    for (const [key, c] of Object.entries(chipEls)) c.el.setAttribute("aria-pressed", String(c.ids.length > 0 && sameIds(c.ids, s.selection)));
    hint.textContent = live ? "Live clock, temperature and VID per tile (telemetry; n/a where the helper cannot read them)." : "Click a tile to edit it; Ctrl-click adds, Shift-click selects a range. Arrow keys move, Enter selects.";
  }

  const off = tc.store.watch((st) => st.tune, () => update());
  const offTel = tc.store.watch((st) => st.telemetry.values, () => {
    if (tc.state().tileMode === "live") update();
  });
  return { el, update, dispose: () => { off(); offTel(); } };
}
