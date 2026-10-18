// views/tune/power.js: the Power sub-tab of Tune (G-217; PLAN §4.2 / §6.1): Processor Core Power Limit (PL1), Processor Core Turbo Power Limit (PL2), Turbo Boost Power Time Window (tau) in XTU's words.
// Reads power.read (powercap files + MSR 0x610 lock bit + 0x614 info; plain reads, never the OC mailbox), edits the pending values in the store slice `rt` (lib/rtstate.js) and hands them to the apply
// drawer (kind "power": power.plan {power: {pl1_w?, pl2_w?, tau_s?}} | {revert: true}, then power.apply). Writes go to the powercap files only, through the root helper; this module writes nothing.
// D-049: the limits shown are the CPU's own (`cpu_limits.power` of the read: the PL field maximum and the 0x610 lock). A value that breaks a CPU rule is refused by the helper
// at plan time and the refusal is shown in the drawer; a PL2 below PL1 is only a warning here (the CPU accepts it).
//   mount(el, ctx) / unmount();  ctx = subTabContext of views/tune.js ({store, bridge, ...}).
import { h, clear } from "../../components/dom.js";
import { createStepper } from "../../components/stepper.js";
import { isNum } from "../../lib/format.js";
import { ensureRt, editRt, powerCurrent, powerDisagrees, powerDisabledReason } from "../../lib/rtstate.js";
import { normCpuLimits, powerLine, powerRead, READ_FIRST } from "../../lib/cpulimits.js";
import { loadRead, registerRuntimeKinds, PL_NAME } from "./runtime-kinds.js";
import { ensureStyle, head, createReadBar, createActionBar, limitNote, LOST_ON_REBOOT } from "./rt-common.js";

const FIELDS = [
  { key: "pl1_w", label: PL_NAME.pl1_w, unit: "W", step: 1, big: 10, decimals: 0, reg: "powercap constraint_0 (long_term) · MSR 0x610 bits 14:0" },
  { key: "pl2_w", label: PL_NAME.pl2_w, unit: "W", step: 1, big: 10, decimals: 0, reg: "powercap constraint_1 (short_term) · MSR 0x610 bits 46:32" },
  { key: "tau_s", label: PL_NAME.tau_s, unit: "s", step: 1, big: 10, decimals: 1, reg: "powercap constraint_0_time_window_us · MSR 0x610 bits 23:17 (the register keeps 2^Y × (1 + Z/4) time units)" },
];

/** Warnings for the pending values; never block (the CPU accepts them): PL2 below PL1. */
export function powerWarnings(cur, edit) {
  const pl1 = edit.pl1_w ?? cur.pl1_w;
  const pl2 = edit.pl2_w ?? cur.pl2_w;
  const out = [];
  if (isNum(pl1) && isNum(pl2) && pl2 < pl1) out.push({ code: "PL2_BELOW_PL1", text: "PL2 (" + pl2 + " W) is below PL1 (" + pl1 + " W): the CPU treats the lower value as the effective short-term limit. Allowed, but rarely what you want." });
  return out;
}

const fmtVal = (f, v) => (isNum(v) ? (f.decimals ? v.toFixed(f.decimals) : String(Math.round(v))) + " " + f.unit : "–");

let V = null;

export function mount(el, ctx) {
  unmount();
  const { store, bridge } = ctx;
  registerRuntimeKinds();
  ensureRt(store);
  const inst = { el, store, bridge, disposers: [], rows: new Map() };
  V = inst;
  el.classList.add("rt-view");
  ensureStyle();

  const readBar = createReadBar(store, "power", "the power limits", () => loadRead(store, bridge, "power"));
  const actions = createActionBar(store, {
    revertKind: "power",
    canRevert: (rt) => (rt.power.result && rt.power.result.boot ? "" : "Nothing to revert: no power limit was written in this boot (the helper journals the values before the first write)."),
  });
  const info = h("div", { class: "rt-info" });
  const warns = h("ul", { class: "rt-warns", "aria-label": "Warnings" });
  const err = h("p", { class: "rt-error", role: "alert", hidden: true });
  const tbody = h("tbody");
  for (const f of FIELDS) {
    const stepper = createStepper({
      label: f.label, unit: f.unit, step: f.step, big: f.big, decimals: f.decimals, format: (v) => (f.decimals ? v.toFixed(f.decimals) : String(Math.round(v))), title: f.label + " · " + f.reg,
      onCommit: (v) => editRt(store, (e) => {
        const cur = powerCurrent(store.get().rt.power.result)[f.key];
        const power = { ...e.power };
        if (isNum(cur) && Math.abs(cur - v) < 1e-9) delete power[f.key];
        else power[f.key] = v;
        return { ...e, power };
      }),
    });
    const now = h("td", { class: "rt-now" });
    const boot = h("td", { class: "rt-now" });
    const row = h("tr", { dataset: { key: f.key } }, h("th", { scope: "row", text: f.label, title: f.reg }), h("td", {}, stepper.el), now, boot);
    tbody.append(row);
    inst.rows.set(f.key, { f, stepper, now, boot, row });
  }
  const table = h("table", { class: "rt-table", "aria-label": "Package power limits" }, h("thead", {}, h("tr", {}, ["Limit", "New value", "In force now (file · MSR)", "At the first write of this boot"].map((t) => h("th", { scope: "col", text: t })))), tbody);
  el.append(
    head("Power", "Package power limits (RAPL). " + LOST_ON_REBOOT),
    readBar.el,
    err,
    h("div", { class: "rt-section" }, table, info),
    warns,
    actions.el,
  );

  function paint() {
    const s = store.get();
    const r = s.rt.power;
    const result = r.result;
    const cur = powerCurrent(result);
    const edit = s.rt.edits.power;
    const cl = normCpuLimits(result ? result.cpu_limits : null);
    const why = powerDisabledReason(result) || (result && !powerRead(cl) ? READ_FIRST + " (the power limits' own bounds, MSR 0x610 lock and field width, come from that read)" : "");
    err.hidden = r.state !== "error";
    err.textContent = r.state === "error" ? "Power limits could not be read: " + r.error : "";
    for (const { f, stepper, now, boot, row } of inst.rows.values()) {
      const pending = f.key in edit;
      stepper.set({ value: pending ? edit[f.key] : cur[f.key], pending, disabled: !!why, reason: why, was: pending && isNum(cur[f.key]) ? fmtVal(f, cur[f.key]) : "", min: 1 });
      row.classList.toggle("pending", pending);
      clear(now);
      if (result && isNum(cur[f.key])) {
        const file = result.powercap ? result.powercap[f.key] : null;
        const msr = result.msr && result.msr.limit ? result.msr.limit[f.key] : null;
        now.append(fmtVal(f, file), " · ", isNum(msr) ? fmtVal(f, msr) : "MSR n/a");
        if (powerDisagrees(result, f.key)) now.append(" ", h("span", { class: "rt-bad", text: "(file and MSR differ)", title: "powercap and MSR 0x610 should show the same limit" }));
      } else now.append("–");
      boot.textContent = result && result.boot && isNum(result.boot[f.key]) ? fmtVal(f, result.boot[f.key]) : "not changed yet";
    }
    clear(info);
    const lines = [];
    if (why) lines.push(limitNote(why, "warn"));
    else if (result) lines.push(limitNote(powerLine(cl)));
    if (result && result.msr && result.msr.info && isNum(result.msr.info.tdp_w)) lines.push(limitNote("CPU-reported processor base power " + result.msr.info.tdp_w + " W (MSR 0x614 PKG_POWER_INFO); not a ceiling for PL1."));
    if (result && result.boot && result.boot.note) lines.push(limitNote(result.boot.note));
    if (result && !result.boot) lines.push(limitNote("The helper journals the values in force before the first power write of a boot; Revert puts those back."));
    info.append(...lines);
    clear(warns);
    for (const w of powerWarnings(cur, edit)) warns.append(h("li", { dataset: { code: w.code }, text: w.text }));
    readBar.update();
    actions.update();
  }

  inst.disposers.push(store.watch((st) => st.rt, paint));
  inst.disposers.push(store.watch((st) => st.pending, () => actions.update()));
  inst.disposers.push(store.watch((st) => st.link.state, paint));
  inst.disposers.push(
    store.watch((st) => st.link.state, (state) => {
      if (state === "connected" && store.get().rt.power.state === "idle") void loadRead(store, bridge, "power");
    }),
  );
}

export function unmount() {
  const inst = V;
  if (!inst) return;
  V = null;
  for (const d of inst.disposers.splice(0)) d();
  clear(inst.el);
  inst.el.classList.remove("rt-view");
}
