// views/tune/oc.js: the OC section of the inspector (PLAN §4.2): OC ratio (stock max + 1 ... policy ∩ CPU max), the adaptive target voltage (5 mV grid, <= policy ∩ CPU max), the "48x offset"
// helper (G-150: the target as an offset from the fused top point's voltage) and the "no OC point" state. Static/override mode is never offered (D-016).
// All state changes go through lib/draft.js (setOcRatio / setVt / setOcOffset); this file only shows them. The wording of the OC state is the web UI's (web/app.js:2733-2749, G-150).
// A kind section (P or E) is edited on its own: P and E have separate OC ratios, so the inspector builds one of these per kind.
import { h } from "../../components/dom.js";
import { createStepper } from "../../components/stepper.js";
import { isNum, fmtV, fmtMv, fmtAgg, fmtRatio, TIMES, DASH } from "../../lib/format.js";
import { aggregate, sameMv, ocClass, aboveTop, fusedTopOf, ocRatioWindow } from "../../lib/units.js";
import { curRatio, effRatio, effVt, ocRatioValue, vtValue, ocOffsetOf, setOcRatio, setVt, setOcOffset, clearOcEdits } from "../../lib/draft.js";
import { limitsFor, vtWindow, GRID } from "../../lib/limits.js";
import { modelCpuLine } from "../../lib/cpulimits.js";

const units = (model, ids) => ids.map((id) => model.units[id]).filter((u) => u && u.kind !== "R");

/** Point number of the OC point of a kind's table: one after its last fused point (P 9, E 7). */
export const ocPointNo = (us) => (us.length ? Math.max(...us.map((u) => u.points.length)) + 1 : null);

/** {cls: none|middle|oc|unknown|mixed, ratio (agg), text}: the OC state of a set of units, as read (eff false) or as edited. */
export function ocInfo(model, draft, ids, eff = true) {
  const us = units(model, ids);
  const cls = us.map((u) => ocClass(u, eff ? effRatio(draft, u) : curRatio(u)));
  const cls0 = cls.length && cls.every((c) => c === cls[0]) ? cls[0] : "mixed";
  const rt = ocRatioValue(model, draft, ids, eff);
  const rtxt = rt.state === "same" ? rt.value + TIMES : rt.state === "mixed" ? "mixed ratios" : DASH;
  const abs = us.map((u) => aboveTop(model, u, eff ? effRatio(draft, u) : curRatio(u)));
  const ab = cls0 === "none" && abs.length && abs.every((a) => a && a.ft === abs[0].ft && a.max === abs[0].max) ? abs[0] : null;
  let text;
  if (!us.length) text = "";
  else if (ab) text = "OC ratio " + ab.max + TIMES + " (fused top " + ab.ft + TIMES + ", set at boot) · no OC point";
  else if (cls0 === "none") text = "No OC ratio configured";
  else if (cls0 === "unknown") text = "OC ratio not judged: this unit's stock ratio was not read";
  else if (cls0 === "mixed") text = "mixed: the units differ";
  else if (cls0 === "middle") text = "OC ratio " + rtxt + " (fused top " + fusedTopOf(us[0]) + TIMES + ") · on a fused point: offset only";
  else text = "OC ratio " + rtxt + " (fused top " + fusedTopOf(us[0]) + TIMES + ") · OC point " + ocPointNo(us) + " · adaptive V";
  return { cls: cls0, ratio: rt, text };
}

/** Units of `ids` the 48x-offset helper applies to (an OC ratio above the fused top, or a boot-set ratio above it). */
export function offsetUnits(model, draft, ids) {
  return units(model, ids).filter((u) => ocClass(u, effRatio(draft, u)) === "oc" || aboveTop(model, u, effRatio(draft, u)));
}

const eqAgg = (a, b, eq) => a.state === b.state && (a.state === "none" || (a.state === "same" ? eq(a.value, b.value) : eq(a.min, b.min) && eq(a.max, b.max)));
const sameN = (a, b) => a === b;

/** The OC section of one kind section ({kind, ids}). Returns {el, update(), dispose()}. */
export function createOcSection(tc, section) {
  const kind = section.kind;
  const ids = section.ids;
  const edit = (fn) => tc.edit(fn);

  const info = h("p", { class: "oc-info" });
  const ratioField = createStepper({
    label: kind + " OC ratio", unit: "×", step: 1, big: 5, format: (v) => String(v),
    title: "OC ratio: the highest ratio the unit may run. mailbox 0x11 P2 = 0 (the OC word) · docs/knowledge/registers.md. Above the fused top it creates an OC point.",
    onCommit: (v) => edit((m, d) => setOcRatio(m, d, ids, v)),
  });
  const ratioBack = h("button", { type: "button", class: "btn small", text: "Back to read", title: "Drop the OC ratio edit", on: { click: () => edit((m, d) => clearOcEdits(m, d, ids)) } });
  const vtField = createStepper({
    label: kind + " adaptive target voltage", unit: "V", step: GRID.vtStep, big: 0.025, decimals: 3, min: GRID.vtMin,
    title: "Adaptive target voltage of the OC point (5 mV grid): the CPU raises it to the top fused point's voltage if lower. Adaptive mode only; static/override is never offered (D-016).",
    onCommit: (v) => edit((m, d) => setVt(m, d, ids, v)),
  });
  const offField = createStepper({
    label: kind + " OC offset from the fused top voltage", unit: "mV", step: 5, big: 25,
    title: "The OC target as an offset from the fused top point's voltage (cmd 0x09), as XTU shows it. Same field as the target, in mV.",
    onCommit: (v) => edit((m, d) => setOcOffset(m, d, ids, v)),
  });
  const offTop = h("button", { type: "button", class: "btn small", text: "= top point", title: "Same voltage as the top fused point (drops the OC point you drafted)", on: { click: () => edit((m, d) => setOcOffset(m, d, ids, null)) } });
  const ratioHint = h("p", { class: "limit-line muted" });
  const vtHint = h("p", { class: "limit-line muted" });
  const rules = h("p", { class: "muted oc-rules", text: "The OC point can only be cleared by a cold boot. Static/override voltage is never offered (D-016)." });

  const row = (label, ...cells) => h("div", { class: "oc-row" }, h("span", { class: "field-label", text: label }), h("span", { class: "oc-cells" }, ...cells));
  const offLabel = h("span", { class: "field-label", text: "OC offset" });
  const el = h("div", { class: "oc-section", dataset: { kind } }, h("h4", { text: "OC" }), info, row("OC ratio", ratioField.el, ratioBack), ratioHint,
    row("Target voltage", vtField.el), vtHint, h("div", { class: "oc-row" }, offLabel, h("span", { class: "oc-cells" }, offField.el, offTop)), rules);

  function update() {
    const { model, draft } = tc.state();
    const us = units(model, ids);
    const lim = limitsFor(model, kind);
    const w = ocRatioWindow(model, ids);
    const rNow = ocRatioValue(model, draft, ids, true);
    const rRead = ocRatioValue(model, draft, ids, false);
    const oi = ocInfo(model, draft, ids, true);
    info.textContent = oi.text;
    info.dataset.cls = oi.cls;
    const noWin = w.min === null || w.reason;
    ratioField.set({
      value: rNow.state === "same" ? rNow.value : null,
      mixed: rNow.state === "mixed" ? rNow : null,
      placeholder: "none",
      pending: !eqAgg(rNow, rRead, sameN),
      was: rRead.state === "same" ? rRead.value + TIMES : rRead.state === "none" ? "none" : "mixed",
      disabled: w.min === null,
      reason: w.min === null ? w.reason : "",
      min: w.min === null ? undefined : w.min,
      max: w.max === null ? undefined : w.max,
      emptyStart: w.min === null ? undefined : w.min,
    });
    ratioBack.disabled = !ids.some((id) => draft.units[id] && (draft.units[id].ratio !== undefined || draft.units[id].vt !== undefined));
    ratioHint.textContent = noWin && w.reason ? w.reason + "." : modelCpuLine(model, kind, "ratio") + (w.min !== null ? " Choosable: " + w.min + TIMES + "…" + w.max + TIMES + "." : "");
    const ocUnits = us.filter((u) => ocClass(u, effRatio(draft, u)) === "oc");
    const vw = vtWindow(model, kind);
    const vNow = vtValue(model, draft, ids, true);
    const vRead = vtValue(model, draft, ids, false);
    const vtReason = !lim.known ? "policy limits not reported" : !vw ? "your policy states no target-voltage ceiling" : !ocUnits.length ? "needs an OC ratio above the fused top (" + (us.length ? fusedTopOf(us[0]) + TIMES : DASH) + ")" : "";
    vtField.set({
      value: vNow.state === "same" ? vNow.value : null,
      mixed: vNow.state === "mixed" ? vNow : null,
      placeholder: ocUnits.length ? "not set" : DASH,
      pending: !eqAgg(vNow, vRead, (a, b) => Math.abs(a - b) < 0.0004),
      was: vRead.state === "same" ? fmtV(vRead.value) : vRead.state === "none" ? "" : "mixed",
      disabled: !!vtReason,
      reason: vtReason,
      min: GRID.vtMin,
      max: vw ? vw.max : undefined,
    });
    vtHint.textContent = vtReason ? vtReason + "." : modelCpuLine(model, kind, "vt");
    const ou = offsetUnits(model, draft, ids);
    const offs = aggregate(ou.map((u) => ocOffsetOf(model, draft, u.id)), sameMv);
    const offsRead = aggregate(ou.map((u) => ocOffsetOf(model, { units: {}, turbo: {} }, u.id)), sameMv);
    const r0 = ou.length ? effRatio(draft, ou[0]) : null;
    const topR = ou.length ? fusedTopOf(ou[0]) : null;
    offLabel.textContent = isNum(r0) ? r0 + TIMES + " offset" : "OC offset";
    offField.set({
      value: offs.state === "same" ? offs.value : null,
      mixed: offs.state === "mixed" ? offs : null,
      placeholder: ou.length ? "= top point" : DASH,
      pending: !eqAgg(offs, offsRead, sameMv),
      was: offsRead.state === "same" ? fmtMv(offsRead.value) : "",
      disabled: !ou.length || !vw,
      reason: !ou.length ? "needs an OC ratio above the fused top (" + (us.length && isNum(fusedTopOf(us[0])) ? fusedTopOf(us[0]) + TIMES : DASH) + ")" : !vw ? "your policy states no target-voltage ceiling" : "",
    });
    offTop.disabled = !ou.length || !vw;
    offField.el.title = "mV above the fused top point's voltage" + (isNum(topR) ? " (point at " + fmtRatio(topR) + ")" : "") + "; the CPU raises a lower target to it [MEASURED lnx 2026-10-11].";
  }
  return { el, update, dispose() {} };
}
