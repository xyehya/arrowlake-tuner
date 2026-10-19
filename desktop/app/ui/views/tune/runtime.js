// views/tune/runtime.js: the "Ratio caps" sub-tab of Tune (G-217; PLAN §4.2 / §6.4, the old "Ratios & parking" tab). One maximum-ratio cap per P-core and per E-cluster (the XTU per-core ratio analogue:
// the clock a core runs at is min(its turbo ratio, its cap)), written as cpufreq/scaling_max_freq through the root helper: runtime.plan {caps: {"<cpu>": ratio}} / {revert: true}, runtime.apply.
// An E cluster runs one ratio, so its cap is one field that applies to all 4 cores of the cluster (the intent names every online CPU of it). A ratio the cpufreq grid cannot hit is capped one bin lower and
// the PLAN says so ("capped at 22×" in the drawer); this view does not predict it.
// Parking is NOT offered (D-048: a parked CPU renumbers the topology); the per-CPU online state is shown read-only. D-049: the window shown is the CPU's own (`cpu_limits.caps_khz`: the cpufreq range of
// that CPU); unread -> disabled with "Read CPU state first".
//   mount(el, ctx) / unmount();  ctx = subTabContext of views/tune.js.
import { h, clear } from "../../components/dom.js";
import { createStepper } from "../../components/stepper.js";
import { isNum, TIMES } from "../../lib/format.js";
import { ensureRt, editRt, capRows } from "../../lib/rtstate.js";
import { normCpuLimits, capWindow, capsLine, READ_FIRST } from "../../lib/cpulimits.js";
import { loadRead, registerRuntimeKinds } from "./runtime-kinds.js";
import { ensureStyle, head, createReadBar, createActionBar, limitNote, LOST_ON_REBOOT } from "./rt-common.js";

/** The cap window of a unit in ratios: the strictest over its CPUs. null when the CPU limits are not read. */
export function unitWindow(cl, cpus) {
  const ws = cpus.map((c) => capWindow(cl, c)).filter(Boolean);
  const mins = ws.map((w) => w.min).filter(isNum);
  return ws.length ? { min: mins.length ? Math.max(...mins) : null, max: Math.min(...ws.map((w) => w.max)) } : null;
}

/** Why the cap fields are off ("" = usable). */
export function capsDisabledReason(result) {
  if (!result) return "Read the ratio caps first";
  if (Array.isArray(result.offline) && result.offline.length) return "CPU " + result.offline.join(", ") + " is offline: runtime writes are refused while any CPU is offline (D-048)";
  if (!capRows(result).length) return "the helper reported no cpufreq interface";
  return "";
}

let V = null;

export function mount(el, ctx) {
  unmount();
  const { store, bridge } = ctx;
  registerRuntimeKinds();
  ensureRt(store);
  const inst = { el, store, bridge, disposers: [], units: new Map(), key: "" };
  V = inst;
  el.classList.add("rt-view");
  ensureStyle();

  const readBar = createReadBar(store, "runtime", "the ratio caps", () => loadRead(store, bridge, "runtime"));
  const actions = createActionBar(store, {
    revertKind: "runtime",
    canRevert: (rt) => (rt.runtime.result && rt.runtime.result.journal ? "" : "Nothing to revert: no runtime value was written in this boot (the helper journals the values before the first write)."),
  });
  const err = h("p", { class: "rt-error", role: "alert", hidden: true });
  const notice = h("div", { class: "rt-info" });
  const tbody = h("tbody");
  const table = h("table", { class: "rt-table", "aria-label": "Maximum ratio per core" }, h("thead", {}, h("tr", {}, ["Core", "CPUs (online)", "Maximum ratio", "In force now", "cpufreq range"].map((t) => h("th", { scope: "col", text: t })))), tbody);
  const warns = h("ul", { class: "rt-warns", "aria-label": "Notes" });
  el.append(
    head("Ratio caps", "A maximum ratio per P-core and per E-cluster (effective ratio = the lower of the turbo table and the cap). " + LOST_ON_REBOOT),
    readBar.el,
    err,
    h("div", { class: "rt-section" }, table, notice),
    warns,
    actions.el,
  );

  function buildRows(groups) {
    clear(tbody);
    inst.units.clear();
    for (const g of groups) {
      const stepper = createStepper({
        label: "Maximum ratio of " + g.unit, unit: TIMES, step: 1, big: 5, decimals: 0, format: (v) => String(Math.round(v)),
        title: g.unit + " · cpufreq scaling_max_freq of " + g.cpus.map((c) => "cpu" + c).join(", ") + " (the ratio shown is the one the cores run: the kernel kHz of an E-core is up to one ratio higher)",
        onCommit: (v) => editRt(store, (e) => {
          const row = capRows(store.get().rt.runtime.result).find((r) => r.unit === g.unit);
          const caps = { ...e.caps };
          if (row && row.capRatio === v && !row.mixedCap) delete caps[g.unit];
          else caps[g.unit] = v;
          return { ...e, caps };
        }),
      });
      const cpus = h("td", { class: "rt-cpus" });
      const now = h("td", { class: "rt-now" });
      const range = h("td", { class: "rt-now" });
      const label = h("th", { scope: "row", text: g.unit + (g.kind === "E" ? " (cluster)" : ""), title: g.kind === "E" ? "The 4 cores of an E-cluster share one ratio: the cap applies to all 4 cores of the cluster" : "P-core " + g.unit });
      const row = h("tr", { dataset: { unit: g.unit, kind: g.kind } }, label, cpus, h("td", {}, stepper.el), now, range);
      tbody.append(row);
      inst.units.set(g.unit, { g, stepper, cpus, now, range, row });
    }
  }

  function paint() {
    const s = store.get();
    const r = s.rt.runtime;
    const result = r.result;
    err.hidden = r.state !== "error";
    err.textContent = r.state === "error" ? "The ratio caps could not be read: " + r.error : "";
    const groups = capRows(result);
    const key = groups.map((g) => g.unit + ":" + g.cpus.join(",")).join("|");
    if (key !== inst.key) {
      inst.key = key;
      buildRows(groups);
    }
    const cl = normCpuLimits(result ? result.cpu_limits : null);
    const noRange = !!result && !groups.some((g) => unitWindow(cl, g.cpus));
    const why = capsDisabledReason(result) || (noRange ? READ_FIRST + " (the CPU's cpufreq limits come from that read)" : "");
    const edit = s.rt.edits.caps;
    for (const g of groups) {
      const u = inst.units.get(g.unit);
      if (!u) continue;
      u.g = g;
      const win = unitWindow(cl, g.cpus);
      const pending = g.unit in edit;
      u.stepper.set({ value: pending ? edit[g.unit] : g.capRatio, mixed: !pending && g.mixedCap ? { min: g.capMin, max: g.capRatio } : null, pending, disabled: !!why || g.online.length === 0, reason: why || (g.online.length ? "" : g.unit + " is offline"), was: pending && isNum(g.capRatio) ? g.capRatio + TIMES : "", min: win ? win.min : undefined, max: win ? (isNum(g.maxRatio) ? Math.min(win.max, g.maxRatio) : win.max) : undefined });
      u.row.classList.toggle("pending", pending);
      clear(u.cpus);
      for (const c of g.cpus) {
        const on = g.online.includes(c);
        u.cpus.append(h("span", { class: "rt-cpu", title: "cpu" + c + (on ? " online" : " offline (read-only)") }, h("span", { class: on ? "rt-dot" : "rt-dot off" }), String(c), " "));
      }
      const kernelMhz = isNum(g.capKhz) ? Math.round(g.capKhz / 1000) : null;
      u.now.textContent = kernelMhz === null || !isNum(g.capRatio) ? "–" : g.capRatio + TIMES + " (" + (kernelMhz === g.capRatio * 100 ? "" : "kernel ") + kernelMhz + " MHz)";
      u.range.textContent = capsLine(cl, g.online.length ? g.online : g.cpus) ? capsLine(cl, g.online.length ? g.online : g.cpus).replace(/^cpufreq /, "").replace(/ \(cpu.*\)$/, "") : "–";
      u.range.title = win ? "ratios " + win.min + TIMES + "–" + (isNum(g.maxRatio) ? Math.min(win.max, g.maxRatio) : win.max) + TIMES : "";
    }
    clear(notice);
    if (why) notice.append(limitNote(why, "warn"));
    else notice.append(limitNote("Range = the cpufreq limits the kernel reports for that CPU (cpuinfo_min/max_freq). An E-cluster field applies to all 4 cores of the cluster."));
    notice.append(limitNote("Core parking is not offered (D-048): the online state of each CPU is shown read-only."));
    clear(warns);
    if (result && result.note) warns.append(h("li", { text: result.note }));
    readBar.update();
    actions.update();
  }

  inst.disposers.push(store.watch((st) => st.rt, paint));
  inst.disposers.push(store.watch((st) => st.pending, () => actions.update()));
  inst.disposers.push(
    store.watch((st) => st.link.state, (state) => {
      paint();
      if (state === "connected" && store.get().rt.runtime.state === "idle") void loadRead(store, bridge, "runtime");
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
