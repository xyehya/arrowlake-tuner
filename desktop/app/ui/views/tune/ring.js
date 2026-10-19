// views/tune/ring.js: the "Ring & uncore" sub-tab of Tune (G-217; PLAN §4.2 / §6.2 / §6.3): the ring's V/F point offsets (the Cores inspector's curve editor on the ring unit; the offsets live in the Tune
// draft and go out through tune.plan {ring: {points}}, G-212) and the uncore (ring) min / max ratio (runtime.plan {uncore: {min_ratio?, max_ratio?}}, kind "runtime" of the apply drawer).
// The uncore ratios are sysfs writes through the root helper (intel_uncore_frequency/package_00_die_00/{min,max}_freq_khz), runtime only: lost on reboot. D-049: the window shown is the CPU's own
// (`cpu_limits.uncore_khz` = the driver's initial_min/max_freq_khz). Without that read the fields are disabled with "Read CPU state first".
//   mount(el, ctx) / unmount();  ctx = subTabContext of views/tune.js.
import { h, clear } from "../../components/dom.js";
import { createStepper } from "../../components/stepper.js";
import { isNum, TIMES } from "../../lib/format.js";
import { sections, RING_ID } from "../../lib/units.js";
import { ensureRt, editRt, revertReason, uncoreState } from "../../lib/rtstate.js";
import { normCpuLimits, uncoreWindow, uncoreLine, READ_FIRST } from "../../lib/cpulimits.js";
import { createCurveEditor } from "./curve-editor.js";
import { loadRead, registerRuntimeKinds } from "./runtime-kinds.js";
import { ensureStyle, head, createReadBar, createActionBar, readOnMount, limitNote, LOST_ON_REBOOT } from "./rt-common.js";

const UNCORE = [
  { key: "min_ratio", label: "Uncore minimum ratio", file: "min_freq_khz" },
  { key: "max_ratio", label: "Uncore maximum ratio", file: "max_freq_khz" },
];

/** Why the uncore fields are off ("" = usable). */
export function uncoreDisabledReason(result, cl) {
  if (!result) return "Read the uncore state first";
  if (!result.uncore) return result.uncore_note || "this machine has no intel_uncore_frequency interface";
  if (Array.isArray(result.offline) && result.offline.length) return "CPU " + result.offline.join(", ") + " is offline: runtime writes are refused while any CPU is offline";
  if (!uncoreWindow(cl)) return READ_FIRST + " (the uncore window comes from that read)";
  return "";
}

let V = null;

export function mount(el, ctx) {
  unmount();
  const { store, bridge } = ctx;
  registerRuntimeKinds();
  ensureRt(store);
  const inst = { el, store, bridge, disposers: [], editor: null, ringKey: null };
  V = inst;
  el.classList.add("rt-view");
  ensureStyle();

  const readBar = createReadBar(store, "runtime", "the uncore state", () => loadRead(store, bridge, "runtime"));
  const actions = createActionBar(store, {
    revertKind: "runtime",
    canRevert: (rt) => revertReason(rt, "runtime"),
  });
  const err = h("p", { class: "rt-error", role: "alert", hidden: true });
  const ringHost = h("div", { class: "rt-ring" });
  const ringSection = h("section", { class: "rt-section", dataset: { section: "ring-vf" }, "aria-label": "Ring V/F points" }, h("h3", { text: "Ring V/F points" }), ringHost);

  const tbody = h("tbody");
  const rows = new Map();
  for (const u of UNCORE) {
    const stepper = createStepper({
      label: u.label, unit: TIMES, step: 1, big: 4, decimals: 0, format: (v) => String(Math.round(v)), title: u.label + " · /sys/devices/system/cpu/intel_uncore_frequency/package_00_die_00/" + u.file + " (kHz = ratio × 100000)",
      onCommit: (v) => editRt(store, (e) => {
        const cur = uncoreState(store.get().rt.runtime.result);
        const uncore = { ...e.uncore };
        const now = cur ? (u.key === "min_ratio" ? cur.min : cur.max) : null;
        if (now === v) delete uncore[u.key];
        else uncore[u.key] = v;
        return { ...e, uncore };
      }),
    });
    const now = h("td", { class: "rt-now" });
    const init = h("td", { class: "rt-now" });
    const row = h("tr", { dataset: { key: u.key } }, h("th", { scope: "row", text: u.label }), h("td", {}, stepper.el), now, init);
    tbody.append(row);
    rows.set(u.key, { u, stepper, now, init, row });
  }
  const info = h("div", { class: "rt-info" });
  const warns = h("ul", { class: "rt-warns", "aria-label": "Uncore notes" });
  const table = h("table", { class: "rt-table", "aria-label": "Uncore ratio" }, h("thead", {}, h("tr", {}, ["Limit", "New value", "In force now", "Driver's initial value"].map((t) => h("th", { scope: "col", text: t })))), tbody);
  const uncoreSection = h("section", { class: "rt-section", dataset: { section: "uncore" }, "aria-label": "Uncore ratio" }, h("h3", { text: "Uncore (ring) ratio" }), table, info, warns);
  el.append(head("Ring & uncore", "The ring's V/F offsets and the uncore (ring) clock range. " + LOST_ON_REBOOT), readBar.el, err, ringSection, uncoreSection, actions.el);

  function paintRing() {
    const t = ctx.tune.state();
    const model = t.model;
    if (!model || !model.units[RING_ID]) {
      if (inst.editor) {
        inst.editor.dispose();
        inst.editor = null;
        inst.ringKey = null;
      }
      clear(ringHost).append(h("p", { class: "muted", text: model ? "This read has no ring unit." : READ_FIRST + ": the ring's V/F points are read with the Cores (Tune → Read CPU state). Nothing is written by reading." }));
      return;
    }
    if (inst.ringKey !== model) {
      if (inst.editor) inst.editor.dispose();
      clear(ringHost);
      inst.ringKey = model;
      const section = sections(model, [RING_ID]).find((s) => s.kind === "R");
      inst.editor = createCurveEditor(ctx.tune, section);
      ringHost.append(inst.editor.el, h("p", { class: "rt-note", text: "Ring offsets are part of the V/F draft: they are reviewed and applied together with the other V/F edits (the ring's OC word is never written, D-045)." }));
    }
    inst.editor.update();
  }

  function paintUncore() {
    const s = store.get();
    const r = s.rt.runtime;
    const result = r.result;
    const cl = normCpuLimits(result ? result.cpu_limits : null);
    const cur = uncoreState(result);
    const edit = s.rt.edits.uncore;
    const why = uncoreDisabledReason(result, cl);
    const win = uncoreWindow(cl);
    err.hidden = r.state !== "error";
    err.textContent = r.state === "error" ? "The uncore state could not be read: " + r.error : "";
    for (const { u, stepper, now, init, row } of rows.values()) {
      const nowV = cur ? (u.key === "min_ratio" ? cur.min : cur.max) : null;
      const initV = cur ? (u.key === "min_ratio" ? cur.initialMin : cur.initialMax) : null;
      const pending = u.key in edit;
      stepper.set({ value: pending ? edit[u.key] : nowV, pending, disabled: !!why, reason: why, was: pending && isNum(nowV) ? nowV + TIMES : "", min: win ? win.min : undefined, max: win ? win.max : undefined });
      row.classList.toggle("pending", pending);
      now.textContent = isNum(nowV) ? nowV + TIMES : "–";
      init.textContent = isNum(initV) ? initV + TIMES : "–";
    }
    clear(info);
    if (why) info.append(limitNote(why, "warn"));
    else info.append(limitNote(uncoreLine(cl)));
    info.append(limitNote("Minimum may not exceed maximum. Lowering the maximum caps the ring clock (less cache and memory bandwidth, less power); the driver's own values come back at reboot or with Revert."));
    clear(warns);
    const lo = edit.min_ratio ?? (cur ? cur.min : null);
    const hi = edit.max_ratio ?? (cur ? cur.max : null);
    if (Object.keys(edit).length && isNum(lo) && isNum(hi) && lo > hi) warns.append(h("li", { dataset: { code: "UNCORE_MIN_ABOVE_MAX" }, text: "Uncore minimum " + lo + TIMES + " is above the maximum " + hi + TIMES + ": this is not sent." }));
    if (cur && cur.min === cur.initialMin && cur.max === cur.initialMax && !Object.keys(edit).length) info.append(limitNote("The uncore range is at the driver's initial values."));
    readBar.update();
    actions.update();
  }

  inst.disposers.push(store.watch((st) => st.rt, paintUncore));
  inst.disposers.push(store.watch((st) => st.pending, () => actions.update())); // the V/F draft is counted by the pending bar
  inst.disposers.push(store.watch((st) => st.tune, paintRing));
  inst.disposers.push(
    store.watch((st) => st.link.state, (state) => {
      paintUncore();
      if (state === "connected" && store.get().rt.runtime.state === "idle") void loadRead(store, bridge, "runtime");
    }),
  );
  readOnMount(store, bridge, "runtime"); // refresh: the journal of this boot may have changed since the last visit (or never been read)
}

export function unmount() {
  const inst = V;
  if (!inst) return;
  V = null;
  for (const d of inst.disposers.splice(0)) d();
  if (inst.editor) inst.editor.dispose();
  clear(inst.el);
  inst.el.classList.remove("rt-view");
}
