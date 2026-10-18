// views/tune/inspector.js: the inspector of the Cores tab (PLAN §4.2) for the selected tiles: a header (name(s), fused top ratio, OC state, favored flag), one section per kind present
// (P-cores, E-clusters, Ring: their points differ, so P and E selected together are two sections), each with the V/F curve editor and, for P/E, the OC section; the hardware rules shown inline.
// The sections are built when the selection (or the read) changes and only refreshed in place afterwards, so a field keeps focus while its value is edited.
import { h, clear } from "../../components/dom.js";
import { fmtAgg, fmtRatio, plural } from "../../lib/format.js";
import { aggregate, sections, fusedTopOf, stockMaxOf, unitLabel, favoredIds } from "../../lib/units.js";
import { warnings, discardUnits } from "../../lib/draft.js";
import { createCurveEditor } from "./curve-editor.js";
import { createOcSection, ocInfo } from "./oc.js";

const KIND_NAME = { P: "P-core", E: "E-cluster", R: "Ring" };
const KIND_PLURAL = { P: "P-cores", E: "E-clusters", R: "Ring" };

/** "P-core 0" / "All 8 P-cores" / "3 P-cores + 2 E-clusters" for a selection. */
export function selectionTitle(model, ids) {
  const live = ids.filter((id) => model.units[id]);
  if (!live.length) return "Nothing selected";
  if (live.length === 1) return unitLabel(live[0]);
  const parts = [];
  for (const kind of ["P", "E", "R"]) {
    const n = live.filter((id) => model.units[id].kind === kind).length;
    if (!n) continue;
    const total = model.order.filter((id) => model.units[id].kind === kind).length;
    parts.push(kind === "R" ? "Ring" : (n === total ? "All " + n : String(n)) + " " + (n === 1 ? KIND_NAME[kind] : KIND_PLURAL[kind]));
  }
  return parts.join(" + ");
}

/** Facts line of a section: fused top ratio, stock max (P), favored. Aggregates say "mixed (a…b)" when the units differ. */
export function sectionFacts(model, section) {
  const us = section.ids.map((id) => model.units[id]);
  if (section.kind === "R") return ["cache / ring fabric", us[0].points.length + " V/F points, keyed by the P2 point number"];
  const ft = aggregate(us.map(fusedTopOf));
  const facts = ["fused top " + fmtAgg(ft, fmtRatio)];
  if (section.kind === "P") {
    const sm = aggregate(us.map(stockMaxOf));
    facts.push("own max ratio " + fmtAgg(sm, fmtRatio) + " (0x1C)");
  } else {
    facts.push(plural(us.length, "cluster") + ", four CPUs share one ratio each");
  }
  return facts;
}

const RULES = {
  all: "A point's offset cannot take its voltage below the previous point's: the CPU raises it (↑ in Predicted V).",
  E: "E-clusters share one ratio per cluster. Do not offset the 40× point (OCCT errors on one chip, a hint not a limit).",
  OC: "The OC point can only be cleared by a cold boot.",
  R: "Ring offsets are keyed by the P2 point number; the ring's OC word is never written (D-045).",
};

export function createInspector(tc) {
  const el = h("section", { class: "inspector", "aria-label": "Inspector" });
  const head = h("header", { class: "insp-head" });
  const body = h("div", { class: "insp-body" });
  const foot = h("div", { class: "insp-rules muted" });
  el.append(head, body, foot);
  let sig = null;
  let builtFor = null;
  let parts = []; // [{section, editor, oc, header: {meta, reset}}]
  let titleEl = null;
  let metaEl = null;
  let warnEl = null;

  function rebuild(model, selection) {
    for (const p of parts) {
      p.editor.dispose();
      if (p.oc) p.oc.dispose();
    }
    parts = [];
    clear(head);
    clear(body);
    clear(foot);
    const ids = selection.filter((id) => model.units[id]);
    if (!ids.length) {
      head.append(h("h2", { text: "Nothing selected" }));
      body.append(h("p", { class: "muted", text: "Select a tile, or use the quick chips (All P, All E, Favored, All). Ring is selected on its own." }));
      return;
    }
    titleEl = h("h2", { class: "insp-title" });
    metaEl = h("p", { class: "insp-meta muted" });
    warnEl = h("div", { class: "insp-warn", role: "status" });
    head.append(titleEl, metaEl, warnEl);
    if (ids.length > 1) head.append(h("p", { class: "insp-ids muted", text: ids.join(" · ") }));
    const rules = [RULES.all];
    for (const section of sections(model, ids)) {
      const editor = createCurveEditor(tc, section);
      const oc = section.kind === "R" ? null : createOcSection(tc, section);
      const facts = h("p", { class: "muted sec-facts" });
      const reset = h("button", { type: "button", class: "btn small", text: "Discard edits", title: "Drop the pending edits of these units", on: { click: () => tc.edit((m, d) => discardUnits(d, section.ids)) } });
      const sec = h(
        "section",
        { class: "insp-section", dataset: { kind: section.kind }, "aria-label": KIND_PLURAL[section.kind] },
        h("div", { class: "sec-head" }, h("h3", { text: section.kind === "R" ? "Ring V/F curve" : KIND_PLURAL[section.kind] + " V/F curve" + (section.ids.length > 1 ? " (" + section.ids.length + ")" : "") }), facts, h("span", { class: "spacer" }), reset),
        editor.el,
        oc ? oc.el : null,
      );
      body.append(sec);
      parts.push({ section, editor, oc, facts, reset });
      if (section.kind === "E") rules.push(RULES.E);
      if (section.kind === "R") rules.push(RULES.R);
      if (oc) rules.push(RULES.OC);
    }
    foot.append(h("h4", { text: "Hardware rules" }), h("ul", {}, [...new Set(rules)].map((t) => h("li", { text: t }))));
  }

  function update() {
    const s = tc.state();
    const model = s.model;
    if (!model) return;
    const key = s.selection.join(",");
    if (builtFor !== model || sig !== key || !head.firstChild) {
      builtFor = model;
      sig = key;
      rebuild(model, s.selection);
    }
    const ids = s.selection.filter((id) => model.units[id]);
    if (!ids.length) return;
    const fav = favoredIds(model);
    titleEl.textContent = selectionTitle(model, ids) + (ids.length === 1 && fav.includes(ids[0]) ? " ★" : "");
    titleEl.title = ids.length === 1 && fav.includes(ids[0]) ? "Favored core (highest 0x1C ratio)" : "";
    const sub = ids.length === 1 && model.units[ids[0]].sub ? model.units[ids[0]].sub : "";
    const oc = ids.some((id) => model.units[id].kind !== "R") ? ocInfo(model, s.draft, ids.filter((id) => model.units[id].kind !== "R"), true).text : "";
    metaEl.textContent = [sub, ...(parts.length === 1 ? sectionFacts(model, parts[0].section) : []), oc && parts.length === 1 ? oc : ""].filter(Boolean).join(" · ");
    clear(warnEl);
    for (const w of warnings(model, s.draft)) if (ids.includes(w.unit)) warnEl.append(h("p", { class: "warn-line", text: w.text }));
    for (const p of parts) {
      p.editor.update();
      if (p.oc) p.oc.update();
      p.facts.textContent = parts.length > 1 ? sectionFacts(model, p.section).join(" · ") : "";
      p.reset.disabled = !p.section.ids.some((id) => s.draft.units[id]);
    }
  }

  const off = tc.store.watch((st) => st.tune, () => update());
  return { el, update, dispose: () => off() };
}
