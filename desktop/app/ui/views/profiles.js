// views/profiles.js: the Profiles screen (PLAN §4.4, G-213): the accepted state, the saved profiles, Load into Tune, Review and apply, Set as accepted, Save, Compare, the Windows fingerprint.
// Ported from web/app.js Profiles (web/app.js:4822-5200): loadProfiles / loadProfileInfo 4830-4858, summaryText 4885, renderAcceptedCard 4897, detailTable 4917, renderProfiles 4949,
// compareRows 5004, renderProfileDiff 5049, loadIntoTune 5077, updateSaveState 5116, saveProfile 5132, acceptState 5148, recordFingerprint 5168.
//
// Ops used (all read-only or bookkeeping, no hardware from here): profiles.list, profiles.get, profiles.save {name, intent} | {name, from: "read"} (G-228), profiles.accept {name} | {from: "read"} [+ set_aside_unreadable: true, only from the refusal banner] (keeps the replaced state: previous_saved_as), profiles.diff {a, b} (the confirm step), profiles.fingerprint, tune.last.
// "Review and apply" and "Restore accepted" open the apply drawer (views/tune/apply.js) with a whole-state intent ({profile: name} / {restore: "accepted"}) through the "tune:review" event on
// store.events, `detail.requests` (G-213's one change to apply.js). Nothing is written from this screen: the drawer plans, shows the writes, and applies only on its own Apply.
//
// "Load into Tune" fills the shared Tune draft (store slice `tune`, lib/draft.js) from the profile's contents; the Tune screen shows it as pending edits against the last read.
// A profile saved by the helper (G-148, D-044) is COMPLETE: the whole tuning state. Older ones are "partial (legacy)" (rows = what they changed). There is no other mode: the web page
// has no "as applied" / "this file only" switch either, only that complete / partial distinction, which is ported (kindOf, the compare note).
//
// profiles.save refuses `turbo` and `ring` (only a {scope, rows} intent can be saved, ops_tune.py canonical_intent rows_only): a draft holding either says "apply it first, then save"
// instead of failing at the helper.
import { h, clear } from "../components/dom.js";
import { setScreen, setLastRead, pushToast } from "../store.js";
import { fmtMv, fmtV, isNum, plural, DASH, MINUS, TIMES } from "../lib/format.js";
import { PROFILE_NAME_RE, buildIntent } from "../lib/intent.js";
import { RING_ID, pointAt, unitKind, sameMv, sameV } from "../lib/units.js";
import * as draftLib from "../lib/draft.js";
import { ensureTune, patchTune, createController } from "./tune.js";

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const UNIT_ORDER = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "EC0", "EC1", "EC2", "EC3", RING_ID];
export const NO_ACCEPTED_TEXT = "No accepted state yet: save a profile as accepted (Profiles)";
export const ACCEPTED_MIGRATE_HINT = "sudo python3 -I tools/arltuner.py accepted-migrate";
const INFO_BATCH = 6;
const INFO_MAX = 60;
const DIFF_MAX = 80;

// ---- pure helpers (node --test) ---------------------------------------------------------------------------------------------------------------------------------------------------
export const rowsOf = (c) => (isObj(c) && Array.isArray(c.rows) ? c.rows.filter(isObj) : []);

/** web/app.js:4885. "12 units · deepest −25 mV · 1 OC ratio · 1 target voltage · turbo table". */
export function summaryText(s) {
  if (!isObj(s)) return "";
  const bits = [plural(s.units, "unit")];
  if (isNum(s.deepest_offset_mv)) bits.push("deepest " + fmtMv(s.deepest_offset_mv));
  if (s.oc_ratios) bits.push(plural(s.oc_ratios, "OC ratio"));
  if (s.v_targets) bits.push(plural(s.v_targets, "target voltage"));
  if (s.turbo) bits.push("turbo table");
  return bits.join(" · ");
}

/** web/app.js:4876. The whole offset of a row, in words. */
export function offsetText(o) {
  if (!isObj(o)) return "unchanged";
  if (o.kind === "curve") return fmtMv(o.mv) + " (whole curve)";
  if (o.kind === "points" && isObj(o.mv)) return Object.keys(o.mv).sort((a, b) => a - b).map((r) => r + TIMES + " " + fmtMv(o.mv[r])).join(", ");
  return "unchanged";
}

/** web/app.js:4883. A table cell: every point when there are few, else the count and the range. */
export function offsetShort(o) {
  if (!isObj(o)) return DASH;
  if (o.kind === "curve") return fmtMv(o.mv) + " all";
  if (!isObj(o.mv)) return DASH;
  const ks = Object.keys(o.mv).sort((a, b) => a - b);
  if (ks.length <= 3) return ks.map((r) => r + TIMES + " " + fmtMv(o.mv[r])).join(", ");
  const vs = ks.map((r) => o.mv[r]);
  return ks.length + " points, " + fmtMv(Math.min(...vs)) + " to " + fmtMv(Math.max(...vs));
}

/** "complete" | "partial" | null (unknown): from the list row, else from profiles.get. */
export function kindOf(entry, info) {
  if (isObj(entry) && typeof entry.complete === "boolean") return entry.complete ? "complete" : "partial";
  if (isObj(info) && typeof info.complete === "boolean") return info.complete ? "complete" : "partial";
  return null;
}

const unitRank = (u) => UNIT_ORDER.indexOf(u);
function valueAt(o, ratio) {
  if (!isObj(o)) return null;
  if (o.kind === "curve") return o.mv;
  return isObj(o.mv) && isNum(o.mv[ratio]) ? o.mv[ratio] : null;
}
const stepText = (n) => (n > 0 ? "+" : n < 0 ? MINUS : "") + Math.abs(n) + TIMES;
const deltaMv = (a, b) => (isNum(a) && isNum(b) ? fmtMv(b - a) : DASH);

/**
 * web/app.js:5004. Rows {unit, label, a, b, fmt, delta} where two profiles' contents differ; "second minus first". A whole-curve value stands for every point of a unit the other side sets
 * point by point. `a` / `b` null = the profile does not set that value.
 */
export function compareRows(ca, cb) {
  const ra = {};
  const rb = {};
  for (const r of rowsOf(ca)) ra[r.target] = r;
  for (const r of rowsOf(cb)) rb[r.target] = r;
  const out = [];
  const units = [...new Set([...Object.keys(ra), ...Object.keys(rb)])].filter((u) => unitRank(u) >= 0).sort((x, y) => unitRank(x) - unitRank(y));
  for (const u of units) {
    const xa = ra[u] || {};
    const xb = rb[u] || {};
    const oa = isObj(xa.offset) ? xa.offset : null;
    const ob = isObj(xb.offset) ? xb.offset : null;
    if (oa || ob) {
      const bothCurve = (!oa || oa.kind === "curve") && (!ob || ob.kind === "curve");
      if (bothCurve) {
        const a = oa ? oa.mv : null;
        const b = ob ? ob.mv : null;
        if (a !== b) out.push({ unit: u, label: "Offset, whole curve", a, b, fmt: fmtMv, delta: deltaMv });
      } else {
        const ratios = [...new Set([oa, ob].flatMap((o) => (o && o.kind === "points" ? Object.keys(o.mv).map(Number) : [])))].sort((x, y) => x - y);
        for (const r of ratios) {
          const a = valueAt(oa, r);
          const b = valueAt(ob, r);
          if (a !== b) out.push({ unit: u, label: "Offset at " + r + TIMES, a, b, fmt: fmtMv, delta: deltaMv });
        }
      }
    }
    const va = isNum(xa.v_target_v) ? xa.v_target_v : null;
    const vb = isNum(xb.v_target_v) ? xb.v_target_v : null;
    if (va !== vb) out.push({ unit: u, label: "OC target voltage", a: va, b: vb, fmt: fmtV, delta: (a, b) => (isNum(a) && isNum(b) ? fmtMv((b - a) * 1000) : DASH) });
    const na = isNum(xa.ratio) ? xa.ratio : null;
    const nb = isNum(xb.ratio) ? xb.ratio : null;
    if (na !== nb) out.push({ unit: u, label: "OC ratio", a: na, b: nb, fmt: (x) => x + TIMES, delta: (a, b) => (isNum(a) && isNum(b) ? stepText(b - a) : DASH) });
  }
  for (const k of ["P", "E"]) {
    const ta = isObj(ca) && isObj(ca.turbo) && Array.isArray(ca.turbo[k]) ? ca.turbo[k] : null;
    const tb = isObj(cb) && isObj(cb.turbo) && Array.isArray(cb.turbo[k]) ? cb.turbo[k] : null;
    if (!ta && !tb) continue;
    for (let i = 0; i < 8; i++) {
      const a = ta ? ta[i] : null;
      const b = tb ? tb[i] : null;
      if (a !== b) out.push({ unit: k + " turbo", label: "Bucket " + i, a, b, fmt: (x) => x + TIMES, delta: (x, y) => (isNum(x) && isNum(y) ? stepText(y - x) : DASH) });
    }
  }
  return out;
}

/**
 * web/app.js:5077 (loadIntoTune). A profile's contents -> a Tune draft over `model` (the last read). The draft keeps only what DIFFERS from the read (lib/draft.js drops equal edits), so a
 * complete profile that equals the CPU gives an empty draft. Returns {draft, skipped (units / points the read has no such thing as), clamped (values the editor's limits moved), notes}.
 * Ring rows are not part of a profile's contents (the helper keeps them in the file but profiles.get lists P/E units), so the ring is never touched here.
 */
export function draftFromContents(model, contents) {
  let draft = draftLib.emptyDraft();
  const skipped = [];
  const clamped = [];
  for (const r of rowsOf(contents)) {
    const id = r.target;
    const unit = model.units[id];
    if (!unit || unitKind(id) === "R") {
      skipped.push(String(id));
      continue;
    }
    const o = r.offset;
    if (isObj(o) && o.kind === "curve" && isNum(o.mv)) {
      draft = draftLib.setCurve(model, draft, [id], o.mv);
      const now = draftLib.curveValue(model, draft, [id]);
      if (!(now.state === "same" && sameMv(now.value, o.mv))) clamped.push(id + " " + fmtMv(o.mv));
    } else if (isObj(o) && o.kind === "points" && isObj(o.mv)) {
      for (const k of Object.keys(o.mv).sort((a, b) => a - b)) {
        const ratio = Number(k);
        if (!pointAt(unit, ratio) || !isNum(o.mv[k])) {
          skipped.push(id + " " + k + TIMES);
          continue;
        }
        draft = draftLib.setPoint(model, draft, [id], ratio, o.mv[k]);
        const now = draftLib.pointValue(model, draft, [id], ratio);
        if (!(now.agg.state === "same" && sameMv(now.agg.value, o.mv[k]))) clamped.push(id + " " + k + TIMES + " " + fmtMv(o.mv[k]));
      }
    }
    if (isNum(r.ratio)) {
      draft = draftLib.setOcRatio(model, draft, [id], r.ratio);
      if (draftLib.effRatio(draft, unit) !== r.ratio) clamped.push(id + " OC ratio " + r.ratio + TIMES);
    }
    if (isNum(r.v_target_v)) {
      draft = draftLib.setVt(model, draft, [id], r.v_target_v, { snap: false });
      const v = draftLib.effVt(draft, unit);
      if (!(isNum(v) && sameV(v, r.v_target_v))) clamped.push(id + " target " + fmtV(r.v_target_v));
    }
  }
  const tu = isObj(contents) && isObj(contents.turbo) ? contents.turbo : {};
  for (const kind of ["P", "E"]) {
    const want = tu[kind];
    const cur = draftLib.turboCur(model, kind);
    if (!Array.isArray(want) || want.length !== 8 || !cur) {
      if (Array.isArray(want) && !cur) skipped.push(kind + " turbo table (not read)");
      continue;
    }
    if (!want.some((x, i) => x !== cur[i])) continue;
    for (let i = 0; i < 8; i++) draft = draftLib.setBucket(model, draft, kind, i, want[i]);
    const eff = draftLib.turboEff(model, draft, kind);
    if (want.some((x, i) => x !== eff[i])) clamped.push(kind + " turbo table");
  }
  return { draft, skipped, clamped };
}

/** The Save gate: {ok, note, intent}. `taken` = names already in the library. Ring / turbo edits are refused with the way out (apply them first). */
export function saveGate({ name, taken, model, draft }) {
  if (!name) return { ok: false, note: "Lowercase letters, digits, dot, underscore, hyphen. Up to 64.", intent: null };
  if (!PROFILE_NAME_RE.test(name)) return { ok: false, note: "Not allowed: use a-z, 0-9, dot, underscore, hyphen (1 to 64).", intent: null };
  if (taken.includes(name)) return { ok: false, note: "A profile with this name exists. Names are never reused; pick another.", intent: null };
  if (!model) return { ok: false, note: "Read the CPU state first (Tune): a profile is the complete state of the last read with your changes over it.", intent: null };
  const built = buildIntent(model, draft);
  if (built.error) return { ok: false, note: built.error, intent: null };
  const intent = built.intent;
  if (intent && intent.ring) return { ok: false, note: "Tune holds a ring edit, which a profile cannot carry yet: apply the ring edit first, then save.", intent: null };
  if (intent && intent.turbo) return { ok: false, note: "Tune holds a turbo table edit, which a profile cannot carry yet: apply the turbo edit first, then save.", intent: null };
  const rows = intent && intent.rows ? intent.rows : [];
  const sendable = rows.length ? { scope: "unit", rows } : { scope: "unit", rows: [] }; // no edits = the read as it is (web/app.js:5135)
  const n = rows.length;
  return { ok: true, note: "Will save every unit as the last read holds it" + (n ? ", with " + plural(n, "changed unit") + " from Tune over it." : " (no changes from Tune)."), intent: sendable };
}

export const acceptConfirmText = (what, replaced) =>
  what + " becomes the accepted state. Restore accepted re-applies it after a boot and the dashboard compares the CPU with it. " +
  (replaced.length ? "It replaces the current accepted state (" + replaced.join(", ") + ")." : "There is no accepted state yet, so nothing is replaced.") +
  " It has to be what the CPU holds now: the complete state of the last read is recorded (every unit and fused point). Nothing is written to the CPU.";

// ---- G-228: save the full state without accepting it; an accept keeps the state it replaces --------------------------------------------------------------------------------------------
export const PREVIOUS_PREFIX = "accepted-before-";
export const READ_FIRST_TEXT = "Read CPU state first";
export const isPreviousAccepted = (name) => typeof name === "string" && name.startsWith(PREVIOUS_PREFIX);

/** ["20261018-120000", n] of "accepted-before-20261018-120000[-n]", else null. */
function previousKey(name) {
  const m = /^accepted-before-(\d{8}-\d{6})(?:-(\d+))?$/.exec(String(name));
  return m ? [m[1], m[2] ? Number(m[2]) : 1] : null;
}

/** "2026-10-18 12:00:00 UTC" from a previous-accepted name, else "". */
export function previousStamp(name) {
  const k = previousKey(name);
  return k ? k[0].replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/, "$1-$2-$3 $4:$5:$6 UTC") : "";
}

/** The list order: the accepted state first, the saved profiles as the helper lists them, then the previous accepted states, newest first (stamp, then the -2 / -3 suffix, then created). */
export function savedOrder(list) {
  const prev = (p) => p.source !== "accepted" && isPreviousAccepted(p.name);
  const newest = (a, b) => {
    const ka = previousKey(a.name);
    const kb = previousKey(b.name);
    if (ka && kb) return ka[0] === kb[0] ? kb[1] - ka[1] : ka[0] < kb[0] ? 1 : -1;
    return String(b.created || b.name) < String(a.created || a.name) ? -1 : 1;
  };
  return [...list.filter((p) => p.source === "accepted"), ...list.filter((p) => p.source !== "accepted" && !prev(p)), ...list.filter(prev).sort(newest)];
}

/** The name gate of "Save current state as…": same rules as the Save card, minus the Tune edits (the helper saves the last read as it is). */
export function stateNameGate({ name, taken }) {
  if (!name) return { ok: false, note: "Lowercase letters, digits, dot, underscore, hyphen. Up to 64." };
  if (!PROFILE_NAME_RE.test(name)) return { ok: false, note: "Not allowed: use a-z, 0-9, dot, underscore, hyphen (1 to 64)." };
  if (taken.includes(name)) return { ok: false, note: "A profile with this name exists. Names are never reused; pick another." };
  return { ok: true, note: "Saves every unit as the last read holds it. It is not made the accepted state." };
}

/** "NO_READ" | "STALE_READ" when the helper refused for want of a current read, else null. */
export function readRefusal(error) {
  const c = isObj(error) && isObj(error.detail) ? error.detail.check : null;
  return c === "NO_READ" || c === "STALE_READ" ? c : null;
}

/** True when the helper refused because the accepted state cannot be read (it offers to move the whole directory aside). */
export const unreadableRefusal = (error) => isObj(error) && isObj(error.detail) && error.detail.check === "ACCEPTED_UNREADABLE";

const KIND_FMT = {
  mv: { fmt: fmtMv, delta: deltaMv },
  v: { fmt: fmtV, delta: (a, b) => (isNum(a) && isNum(b) ? fmtMv((b - a) * 1000) : DASH) },
  x: { fmt: (x) => x + TIMES, delta: (a, b) => (isNum(a) && isNum(b) ? stepText(b - a) : DASH) },
  hex: { fmt: (x) => String(x), delta: () => DASH },
};

/**
 * The rows of a profiles.diff answer in the shape renderers use ({unit, label, a, b, fmt, delta}). The helper compares the profiles themselves (every unit's OC word offset / ratio / target voltage,
 * every fused point, the ring, the turbo tables), so nothing is dropped here. `kind`: mv | v | x | hex (the ring's OC word).
 */
export function helperDiffRows(result) {
  if (!isObj(result) || !Array.isArray(result.rows)) return [];
  return result.rows.filter(isObj).map((r) => ({
    unit: String(r.unit), label: String(r.label).replace(/(\d)x$/, "$1" + TIMES), a: r.a === undefined ? null : r.a, b: r.b === undefined ? null : r.b, ...(KIND_FMT[r.kind] || KIND_FMT.hex),
  }));
}

/** [{unit, n}] in row order: how many values differ per unit (the confirm step's per-unit list). */
export function diffByUnit(rows) {
  const out = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (last && last.unit === r.unit) last.n++;
    else out.push({ unit: r.unit, n: 1 });
  }
  return out;
}

export function diffSummary(rows, result = null) {
  if (!rows.length) {
    if (!isObj(result) || !Array.isArray(result.compared)) return "No differences were found.";
    const not = Array.isArray(result.not_compared) && result.not_compared.length ? " Not compared: " + result.not_compared.map((x) => x.what + " (" + x.why + ")").join("; ") + "." : "";
    return "No differences in what was compared: " + result.compared.join("; ") + "." + not;
  }
  const more = isObj(result) && isNum(result.truncated) && result.truncated > 0 ? " (" + result.truncated + " more are not listed)" : "";
  return plural(rows.length, "difference") + " in " + plural(diffByUnit(rows).length, "unit") + " from the current accepted state: " + diffByUnit(rows).map((u) => u.unit + " (" + u.n + ")").join(", ") + "." + more;
}

export const previousKeptText = (replaced) =>
  replaced.length
    ? "The current accepted state (" + replaced.join(", ") + ") is saved first as a profile (accepted-before-<date>), so it can be accepted again."
    : "There is no accepted state to keep.";

export function fmtCreated(iso) {
  return typeof iso === "string" && iso.length >= 16 ? iso.slice(0, 16).replace("T", " ") + " UTC" : "";
}

const errText = (error) => (error && (error.message || error.code)) || "unknown error";

/** Open the apply drawer for a whole-state intent. The drawer plans it (no hardware) and writes only on its own Apply. */
export function reviewIntent(store, intent, label) {
  ensureTune(store);
  store.events.dispatchEvent(new CustomEvent("tune:review", { detail: { requests: [{ kind: "tune", intent, label }] } }));
}

// ---- the stylesheet is loaded by the screen itself (like tune.css / apply.css) ----------------------------------------------------------------------------------------------------
let stylePromise = null;
function ensureStyle() {
  if (stylePromise) return stylePromise;
  stylePromise = new Promise((resolve) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = new URL("../styles/profiles.css", import.meta.url).href;
    link.addEventListener("load", () => resolve());
    link.addEventListener("error", () => resolve());
    document.head.append(link);
  });
  return stylePromise;
}

const pill = (text, state, title) => h("span", { class: "pr-pill", dataset: { state }, title: title || null, text });

// ---- the view ---------------------------------------------------------------------------------------------------------------------------------------------------------------------
export function profilesView() {
  const disposers = [];
  return {
    id: "profiles",
    mount(root, ctx) {
      const { store, bridge } = ctx;
      const toast = ctx.toast || ((kind, text) => pushToast(store, { kind, text }));
      ensureTune(store);
      const tc = createController({ store, bridge, toast }); // only for loadLast(): adopts this boot's read, so Load into Tune and Save work without visiting Tune first
      const S = { list: [], loaded: false, note: "", accepted: null, error: "", info: {}, open: new Set(), seq: 0, ask: null, busy: false, status: "", statusKind: "", notice: null, saveOpen: false };
      const connected = () => store.get().link.state === "connected";
      const infoOf = (name) => (S.info[name] && !S.info[name].error ? S.info[name] : null);
      const entryOf = (name) => S.list.find((p) => p.name === name) || null;
      const say = (text, kind = "") => {
        S.status = text;
        S.statusKind = kind;
        renderStatus();
      };

      const wrap = h("div", { class: "profiles", hidden: true });
      const refreshBtn = h("button", { type: "button", class: "btn", id: "btn-pr-refresh", text: "Refresh", on: { click: () => reload(true) } });
      const status = h("p", { class: "pr-status", id: "pr-status", role: "status" });
      const notice = h("div", { class: "card pr-notice", id: "pr-notice", role: "status", hidden: true });
      const ask = h("div", { class: "card pr-ask", id: "pr-ask", role: "alertdialog", "aria-labelledby": "pr-ask-title", hidden: true });
      const acceptedCard = h("section", { class: "card pr-card", id: "pr-accepted-card", "aria-labelledby": "pr-accepted-h", hidden: true });
      const listCard = h("section", { class: "card pr-card", "aria-labelledby": "pr-list-h" });
      const empty = h("p", { class: "muted", id: "pr-empty", role: "status" });
      const listEl = h("ul", { class: "pr-list", id: "pr-list" });
      const prevNote = h("p", { class: "muted pr-note", id: "pr-prev-note", hidden: true, text: "Previous accepted states are kept as profiles. The app has no delete yet: remove a file from the helper's profiles directory by hand when you no longer need one." });
      listCard.append(h("h2", { id: "pr-list-h", text: "Profiles" }), empty, listEl, prevNote);

      // Save
      const nameInput = h("input", { type: "text", id: "pr-name", class: "pr-name-input", maxlength: "64", autocomplete: "off", spellcheck: "false", "aria-describedby": "pr-name-note", placeholder: "profile-name" });
      const nameNote = h("p", { class: "muted pr-note", id: "pr-name-note" });
      const saveBtn = h("button", { type: "button", class: "btn primary", id: "btn-pr-save", text: "Save Tune edits as…", on: { click: () => saveProfile() } });
      const saveCard = h("section", { class: "card pr-card", "aria-labelledby": "pr-save-h" },
        h("h2", { id: "pr-save-h", text: "Save" }),
        h("p", { class: "muted", text: "A profile is the complete state of the last read with the edits from Tune laid over it. Saving writes a file of the helper; nothing is written to the CPU, and a name is never reused." }),
        h("div", { class: "pr-row" }, h("label", { for: "pr-name", text: "Name" }), nameInput, saveBtn), nameNote);

      // Compare
      const selA = h("select", { id: "pr-a", "aria-label": "First profile", on: { change: () => renderDiff() } });
      const selB = h("select", { id: "pr-b", "aria-label": "Second profile", on: { change: () => renderDiff() } });
      const diffNote = h("p", { class: "muted pr-note", id: "pr-diff-note", role: "status" });
      const thA = h("th", { scope: "col", class: "n", id: "pr-th-a" });
      const thB = h("th", { scope: "col", class: "n", id: "pr-th-b" });
      const diffBody = h("tbody", { id: "pr-diff-body" });
      const diffTable = h("table", { class: "pr-table pr-diff", id: "pr-diff", hidden: true },
        h("caption", { class: "sr", text: "Differences between two profiles" }),
        h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Unit" }), h("th", { scope: "col", text: "Setting" }), thA, thB, h("th", { scope: "col", class: "n", text: "Change" }))), diffBody);
      const compareCard = h("section", { class: "card pr-card", "aria-labelledby": "pr-cmp-h" },
        h("h2", { id: "pr-cmp-h", text: "Compare" }),
        h("div", { class: "pr-row" }, h("label", { for: "pr-a", text: "Profile" }), selA, h("label", { for: "pr-b", text: "against" }), selB),
        diffNote, h("div", { class: "scroll" }, diffTable));

      wrap.append(h("header", { class: "screen-head" }, h("h1", { text: "Profiles" }), h("span", { class: "spacer" }), refreshBtn), status, notice, ask, acceptedCard, listCard, saveCard, compareCard);
      root.append(wrap);
      ensureStyle().then(() => (wrap.hidden = false));

      // ---- rendering ----
      function renderStatus() {
        status.textContent = S.status;
        status.dataset.kind = S.statusKind;
      }

      function setAsk(next) {
        S.ask = next;
        clear(ask);
        ask.hidden = !next;
        if (!next) return;
        const yes = h("button", { type: "button", class: "btn primary", id: "pr-ask-yes", text: next.yes, on: { click: () => { const run = next.run; setAsk(null); void run(); } } });
        ask.append(h("h3", { id: "pr-ask-title", text: next.title }), h("p", { text: next.text }), ...(next.detail ? [next.detail] : []),
          h("div", { class: "pr-row" }, yes, h("button", { type: "button", class: "btn", id: "pr-ask-no", text: "Cancel", on: { click: () => setAsk(null) } })));
        (next.focusCancel ? ask.querySelector("#pr-ask-no") : yes).focus();
      }

      /** A line with buttons under the status (G-228: the toast component has no actions, so "Undo" / "Read CPU state first" live here). {kind, text, actions: [{id, label, run}]} or null. */
      function setNotice(next) {
        S.notice = next;
        clear(notice);
        notice.hidden = !next;
        if (!next) return;
        notice.dataset.kind = next.kind || "";
        notice.append(h("p", { id: "pr-notice-text", text: next.text }), h("div", { class: "pr-row" },
          ...(next.actions || []).map((a) => h("button", { type: "button", class: "btn", id: a.id, text: a.label, on: { click: () => { void a.run(); } } })),
          h("button", { type: "button", class: "btn", id: "pr-notice-close", text: "Dismiss", on: { click: () => setNotice(null) } })));
      }

      const readFirst = (what) => setNotice({ kind: "warn", text: READ_FIRST_TEXT + ": " + what, actions: [{ id: "btn-pr-open-tune", label: "Open Tune", run: () => setScreen(store, "tune") }] });

      function acceptedEntries() {
        return S.list.filter((p) => p.source === "accepted");
      }

      function renderAccepted() {
        acceptedCard.hidden = !S.loaded;
        clear(acceptedCard);
        const acc = acceptedEntries();
        const can = connected() && !S.busy;
        const head = h("h2", { id: "pr-accepted-h", text: "Accepted state" });
        const fromRead = h("button", { type: "button", class: "btn", id: "btn-pr-accept-read", text: "Set the current state as accepted", disabled: !can,
          title: "profiles.accept {from: 'read'}: records the state of this boot's last read as the accepted state. Nothing is written to the CPU.", on: { click: () => askAccept(null) } });
        const finger = h("button", { type: "button", class: "btn", id: "btn-pr-fingerprint", text: "Record CPU fingerprint for Windows", disabled: !can || !acc.length,
          title: "profiles.fingerprint: fingerprint.json beside the accepted state, from this boot's read; the accepted tuning stays as it is.", on: { click: () => recordFingerprint() } });
        let body;
        let restore = null;
        if (!acc.length) body = h("p", { class: "muted", id: "pr-accepted-note", text: NO_ACCEPTED_TEXT + "." });
        else if (acc.length > 1 || acc.some((p) => p.chain === true)) {
          body = h("div", {}, h("p", { class: "pr-accepted-names" }, ...acc.map((p) => h("span", { class: "pr-accepted-name", text: p.name }))),
            h("p", { class: "muted", id: "pr-accepted-note", text: "An older chain of " + acc.length + " profiles. Restore accepted refuses it until it is one profile: run '" + ACCEPTED_MIGRATE_HINT + "' once." }));
        } else {
          const p = acc[0];
          const info = infoOf(p.name);
          const k = kindOf(p, info);
          restore = h("button", { type: "button", class: "btn primary", id: "btn-pr-restore", text: "Restore accepted", disabled: !can,
            title: "Plans the accepted state against what the CPU holds now and opens the review drawer. Nothing is written until you press Apply there.", on: { click: () => reviewIntent(store, { restore: "accepted" }, "Restore accepted: " + p.name) } });
          body = h("div", {}, h("p", { class: "pr-accepted-line" }, h("span", { class: "pr-accepted-name", id: "pr-accepted-name", text: p.name }), k ? kindPill(k) : null,
            h("span", { class: "muted pr-sum", id: "pr-accepted-sum", text: info ? summaryText(info.summary) : S.info[p.name] ? "contents unavailable" : "reading…" })),
          h("p", { class: "muted", id: "pr-accepted-note", text: "Restore accepted applies this profile in one run, writing only what differs from the CPU." }));
        }
        const saveState = h("button", { type: "button", class: "btn", id: "btn-pr-save-state", text: "Save the CPU's current state as…", disabled: !can, "aria-expanded": String(S.saveOpen),
          title: "profiles.save {from: 'read'}: saves the complete state of this boot's last read as a profile. It is not made the accepted state, and nothing is written to the CPU.",
          on: { click: () => { S.saveOpen = !S.saveOpen; renderAccepted(); if (S.saveOpen) stateName.focus(); } } });
        acceptedCard.append(head, body, h("div", { class: "pr-row" }, restore, fromRead, saveState, finger));
        if (S.saveOpen) acceptedCard.append(statePanel);
        renderStateGate();
      }

      // "Save current state as…": the complete profile of the last read, not accepted (G-228)
      const stateName = h("input", { type: "text", id: "pr-state-name", class: "pr-name-input", maxlength: "64", autocomplete: "off", spellcheck: "false", "aria-describedby": "pr-state-note", placeholder: "profile-name",
        on: { input: () => renderStateGate(), keydown: (e) => { if (e.key === "Enter") { e.preventDefault(); void saveState(); } } } });
      const stateNote = h("p", { class: "muted pr-note", id: "pr-state-note" });
      const stateBtn = h("button", { type: "button", class: "btn primary", id: "btn-pr-state-save", text: "Save", on: { click: () => saveState() } });
      const statePanel = h("div", { class: "pr-state", id: "pr-state" },
        h("div", { class: "pr-row" }, h("label", { for: "pr-state-name", text: "Name" }), stateName, stateBtn,
          h("button", { type: "button", class: "btn", id: "btn-pr-state-cancel", text: "Cancel", on: { click: () => { S.saveOpen = false; renderAccepted(); } } })), stateNote);

      function renderStateGate() {
        const gate = stateNameGate({ name: stateName.value, taken: S.list.map((p) => p.name) });
        stateNote.textContent = gate.note;
        stateBtn.disabled = !gate.ok || !connected() || S.busy;
        return gate;
      }

      function kindPill(k) {
        return k === "complete"
          ? pill("complete", "ok", "The whole tuning state: every unit, every fused point, the turbo tables when they were read. Applying it writes only what differs from the CPU.")
          : pill("partial (legacy)", "unknown", "Saved before profiles became complete: it holds only the rows that were changed, applied over what the CPU holds.");
      }

      function detailNode(name) {
        const info = infoOf(name);
        const box = h("div", { class: "pr-detail", dataset: { name } });
        if (!info) {
          box.append(h("p", { class: "muted", text: S.info[name] ? "Contents unavailable: " + S.info[name].error : "Reading the contents…" }));
          return box;
        }
        const rows = rowsOf(info.contents);
        if (!rows.length && !isObj(info.contents.turbo)) {
          box.append(h("p", { class: "muted", text: "This profile sets nothing." }));
          return box;
        }
        const body = h("tbody");
        for (const r of rows) {
          body.append(h("tr", {}, h("td", { text: r.target }), h("td", { class: "mono", title: offsetText(r.offset), text: offsetShort(r.offset) }),
            h("td", { class: "n mono", text: isNum(r.v_target_v) ? fmtV(r.v_target_v) : DASH }), h("td", { class: "n mono", text: isNum(r.ratio) ? r.ratio + TIMES : DASH })));
        }
        box.append(h("div", { class: "scroll" }, h("table", { class: "pr-table" }, h("caption", { class: "sr", text: "What " + name + " sets" }),
          h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Unit" }), h("th", { scope: "col", text: "Voltage offset" }), h("th", { scope: "col", class: "n", text: "OC target voltage" }), h("th", { scope: "col", class: "n", text: "OC ratio" }))), body)));
        const tu = info.contents.turbo;
        if (isObj(tu)) for (const k of ["P", "E"]) if (Array.isArray(tu[k])) box.append(h("p", { class: "muted", text: k + " turbo table: " + tu[k].map((x) => x + TIMES).join(" ") }));
        if (!isObj(tu) && info.complete === true) box.append(h("p", { class: "muted", text: "No turbo tables: they are not part of this profile and are left as they are." }));
        return box;
      }

      /** G-230: an accepted state and the saved profile it was accepted from share one name; `acc` = the entry is the accepted one, so look for its saved twin (else the reverse). */
      const twinned = (p, acc) => S.list.some((q) => q.name === p.name && (q.source === "accepted") !== acc);

      function renderList() {
        empty.hidden = S.loaded && S.list.length > 0;
        if (!S.loaded) empty.textContent = connected() ? "Loading profiles…" : "The helper is not connected.";
        else if (!S.list.length) empty.textContent = S.error ? "Profiles unavailable: " + S.error : "No profiles yet. Change something in Tune and save it here." + (S.note ? " Helper note: " + S.note : "");
        clear(listEl);
        prevNote.hidden = !S.list.some((p) => p.source !== "accepted" && isPreviousAccepted(p.name));
        const can = connected() && !S.busy;
        for (const p of savedOrder(S.list)) {
          const info = infoOf(p.name);
          const k = kindOf(p, info);
          const isAcc = p.source === "accepted";
          const open = S.open.has(p.name);
          const main = h("div", { class: "pr-main" }, h("span", { class: "pr-name", text: p.name }), pill(isAcc ? "accepted" + (twinned(p, true) ? " · also saved" : "") : "saved", isAcc ? "ok" : "neutral", isAcc && twinned(p, true) ? "A saved copy with this name is listed below." : null),
            !isAcc && twinned(p, false) ? h("span", { class: "pr-sub", title: "Set as accepted copied this saved profile to the accepted state; both keep the name.", text: "saved copy of the accepted state" }) : null,
            !isAcc && isPreviousAccepted(p.name) ? h("span", { class: "pr-sub", title: "The accepted state this replaced, kept by Set as accepted.", text: "previous accepted" + (previousStamp(p.name) ? " · replaced " + previousStamp(p.name) : "") }) : null, k ? kindPill(k) : null,
            isAcc ? null : h("span", { class: "muted", text: fmtCreated(p.created) }),
            h("span", { class: "pr-sum muted", text: info ? summaryText(info.summary) : S.info[p.name] ? "contents unavailable" : "reading…" }));
          const actions = h("div", { class: "pr-actions" },
            h("button", { type: "button", class: "btn pr-show", text: open ? "Hide contents" : "Show contents", "aria-expanded": String(open), on: { click: () => { if (S.open.has(p.name)) S.open.delete(p.name); else S.open.add(p.name); renderList(); } } }),
            h("button", { type: "button", class: "btn pr-load", text: "Load into Tune", disabled: !info,
              title: "Fills the Tune editor with this profile (a complete one fills every unit). Nothing is written until you press Review & Apply and then Apply.", on: { click: () => loadIntoTune(p.name) } }),
            h("button", { type: "button", class: "btn pr-review", text: "Review and apply", disabled: !can,
              title: "Plans this profile against what the CPU holds now and opens the review drawer. Nothing is written until you press Apply there.", on: { click: () => reviewIntent(store, { profile: p.name }, "Apply profile " + p.name) } }),
            isAcc ? null : h("button", { type: "button", class: "btn pr-accept", text: "Set as accepted", disabled: !can,
              title: "Makes this profile the accepted state (Restore accepted and the dashboard comparison use it). It must be what the CPU holds now. Nothing is written to the CPU.", on: { click: () => askAccept(p.name) } }));
          const li = h("li", { class: "pr-item", dataset: { name: p.name, source: p.source } }, main, actions);
          if (open) li.append(detailNode(p.name));
          listEl.append(li);
        }
        // the compare selectors keep their choice across a refresh
        // (an accepted profile that was accepted from a saved one is listed twice under one name: one option)
        const names = [...new Set(S.list.map((p) => p.name))];
        const fill = (sel, keep, fallback) => {
          clear(sel);
          for (const n of names) sel.append(h("option", { value: n, text: n }));
          if (keep && names.includes(keep)) sel.value = keep;
          else if (fallback) sel.value = fallback;
        };
        fill(selA, selA.value, names[0]);
        fill(selB, selB.value, names[1]);
        renderDiff();
        renderAccepted();
        renderSave();
      }

      function renderDiff() {
        diffTable.hidden = true;
        const pa = entryOf(selA.value);
        const pb = entryOf(selB.value);
        if (new Set(S.list.map((p) => p.name)).size < 2) return void (diffNote.textContent = "Needs two profiles.");
        if (!pa || !pb || pa.name === pb.name) return void (diffNote.textContent = "Pick two different profiles.");
        const ia = infoOf(pa.name);
        const ib = infoOf(pb.name);
        if (!ia || !ib) {
          const bad = !ia ? pa.name : pb.name;
          return void (diffNote.textContent = S.info[bad] && S.info[bad].error ? 'Contents of "' + bad + '" unavailable: ' + S.info[bad].error : 'Reading the contents of "' + bad + '"…');
        }
        const rows = compareRows(ia.contents, ib.contents);
        thA.textContent = pa.name;
        thB.textContent = pb.name;
        clear(diffBody);
        for (const r of rows) {
          const cell = (x) => h("td", { class: "n mono", title: x === null ? "not set by this profile" : null, text: x === null ? DASH : r.fmt(x) });
          diffBody.append(h("tr", {}, h("td", { text: r.unit }), h("td", { text: r.label }), cell(r.a), cell(r.b), h("td", { class: "n mono", text: r.delta(r.a, r.b) })));
        }
        diffTable.hidden = rows.length === 0;
        const partial = [pa, pb].filter((p) => kindOf(entryOf(p.name), infoOf(p.name)) === "partial").map((p) => p.name);
        const partialNote = partial.length ? ' "' + partial.join('" and "') + '" ' + (partial.length === 1 ? "is" : "are") + " partial (legacy): " + DASH + " marks a value " + (partial.length === 1 ? "it does" : "they do") + " not set." : "";
        diffNote.textContent = (rows.length ? plural(rows.length, "difference") + ' between "' + pa.name + '" and "' + pb.name + '"; the change is second minus first.' : "These two profiles set the same values.") + partialNote;
      }

      function renderSave() {
        const t = store.get().tune;
        const gate = saveGate({ name: nameInput.value, taken: S.list.map((p) => p.name), model: t && t.model, draft: t ? t.draft : draftLib.emptyDraft() });
        nameNote.textContent = gate.note;
        saveBtn.disabled = !gate.ok || !connected() || S.busy;
        return gate;
      }

      // ---- data ----
      async function loadInfo() {
        const seq = ++S.seq;
        const todo = S.list.map((p) => p.name).filter((n) => !S.info[n]).slice(0, INFO_MAX);
        for (let i = 0; i < todo.length; i += INFO_BATCH) {
          await Promise.all(todo.slice(i, i + INFO_BATCH).map(async (name) => {
            const r = await bridge.call("profiles.get", { name });
            S.info[name] = r.ok && isObj(r.result) && isObj(r.result.contents) && Array.isArray(r.result.contents.rows) ? r.result : { error: errText(r.error) };
          }));
          if (seq !== S.seq) return;
          renderList();
        }
      }

      async function reload(dropInfo = false) {
        if (!connected()) return renderList();
        if (dropInfo) S.info = {};
        const r = await bridge.call("profiles.list");
        S.loaded = true;
        S.error = "";
        S.note = "";
        if (r.ok && isObj(r.result) && Array.isArray(r.result.profiles)) {
          S.list = r.result.profiles.filter((p) => isObj(p) && typeof p.name === "string");
          S.note = typeof r.result.note === "string" ? r.result.note : "";
          S.accepted = typeof r.result.accepted === "boolean" ? r.result.accepted : null;
        } else {
          S.list = [];
          S.error = errText(r.error);
        }
        renderList();
        await loadInfo();
      }

      async function refreshAcceptedChip() {
        const last = await bridge.call("tune.last");
        if (last.ok && last.result && last.result.available && last.result.result) {
          setLastRead(store, new Date(last.result.at * 1000).toISOString(), last.result.result.accepted_match ?? null, last.result.result.accepted_note || "");
        }
      }

      // ---- actions ----
      function doLoad(name) {
        const t = store.get().tune;
        const info = infoOf(name);
        if (!t.model || !info) return;
        const { draft, skipped, clamped } = draftFromContents(t.model, info.contents);
        patchTune(store, { draft });
        const d = draftLib.diff(t.model, draft);
        const text = 'Loaded "' + name + '" into the editor: ' + (d.total ? plural(d.unitCount, "unit") + " differ" + (d.unitCount === 1 ? "s" : "") + " from what the CPU holds now." : "the CPU already holds these values.") +
          (skipped.length ? " Not shown (no such point in the read): " + skipped.join(", ") + "." : "") + (clamped.length ? " Moved to the editor's limits: " + clamped.join(", ") + "." : "") +
          " Nothing is written until you press Review & Apply and then Apply.";
        say(text, skipped.length || clamped.length ? "warn" : "ok");
        toast(skipped.length || clamped.length ? "warn" : "info", text);
        setScreen(store, "tune");
      }

      async function loadIntoTune(name) {
        if (!infoOf(name)) return;
        if (!store.get().tune.model && connected()) await tc.loadLast();
        const t = store.get().tune;
        if (!t.model) return say("Read the CPU state first (Tune → Read CPU state): the editor shows each change against what the CPU holds now.", "warn");
        if (!draftLib.isEmpty(t.draft)) {
          return setAsk({ title: "Replace the edits in Tune?", text: 'Tune holds unapplied choices. Loading "' + name + '" replaces them. Nothing is written either way.', yes: "Replace", focusCancel: true, run: () => doLoad(name) });
        }
        doLoad(name);
      }

      /** The confirm step's diff: the accepted state now against the one that would replace it, computed by the helper (profiles.diff) from the profiles themselves. `name` null = the state of
       *  the last read. {rows, result} | {error, read?} | {rows: null} (no accepted state, or an older chain: nothing to compare). */
      async function acceptDiff(name) {
        const acc = acceptedEntries();
        if (acc.length !== 1) return { rows: null, chain: acc.length > 1 };
        const r = await bridge.call("profiles.diff", { a: "accepted", b: name === null ? "read" : name });
        if (r.ok && isObj(r.result) && Array.isArray(r.result.rows)) return { rows: helperDiffRows(r.result), result: r.result };
        return readRefusal(r.error) ? { error: READ_FIRST_TEXT, read: errText(r.error) } : { error: errText(r.error) };
      }

      function diffNode(d) {
        if (d.error) return h("p", { class: "muted pr-note", id: "pr-ask-diff-note", text: "The comparison is unavailable (" + d.error + "); the helper still keeps the replaced state." });
        if (d.rows === null) return h("p", { class: "muted pr-note", id: "pr-ask-diff-note", text: d.chain ? "The current accepted state is an older chain, which the helper refuses to replace until it is one profile: run '" + ACCEPTED_MIGRATE_HINT + "' once." : "There is no accepted state to compare with." });
        const box = h("div", { class: "pr-diff-box", id: "pr-ask-diff" }, h("p", { class: "pr-note", id: "pr-ask-diff-note", text: diffSummary(d.rows, d.result) }));
        if (!d.rows.length) return box;
        const body = h("tbody");
        for (const r of d.rows.slice(0, DIFF_MAX)) {
          const cell = (x) => h("td", { class: "n mono", text: x === null ? DASH : r.fmt(x) });
          body.append(h("tr", {}, h("td", { text: r.unit }), h("td", { text: r.label }), cell(r.a), cell(r.b), h("td", { class: "n mono", text: r.delta(r.a, r.b) })));
        }
        box.append(h("div", { class: "scroll" }, h("table", { class: "pr-table pr-diff", id: "pr-ask-diff-table" }, h("caption", { class: "sr", text: "Per-unit differences between the accepted state and the new one" }),
          h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Unit" }), h("th", { scope: "col", text: "Setting" }), h("th", { scope: "col", class: "n", text: "Accepted now" }), h("th", { scope: "col", class: "n", text: "New" }), h("th", { scope: "col", class: "n", text: "Change" }))), body)));
        if (d.rows.length > DIFF_MAX) box.append(h("p", { class: "muted pr-note", text: "…and " + (d.rows.length - DIFF_MAX) + " more." }));
        return box;
      }

      async function askAccept(name) {
        const replaced = acceptedEntries().map((p) => p.name);
        say("Comparing with the accepted state…");
        const d = await acceptDiff(name);
        say("");
        if (d.error === READ_FIRST_TEXT) return readFirst(d.read);
        setAsk({ title: "Set as accepted?", text: acceptConfirmText(name === null ? "The state of the last read" : '"' + name + '"', replaced) + (replaced.length > 1 ? "" : " " + previousKeptText(replaced)), detail: diffNode(d), yes: "Accept", focusCancel: true, run: () => acceptState(name) });
      }

      async function acceptState(name, setAside = false) {
        if (!connected() || S.busy) return;
        S.busy = true;
        renderList();
        setNotice(null);
        const r = await bridge.call("profiles.accept", { ...(name === null ? { from: "read" } : { name }), ...(setAside ? { set_aside_unreadable: true } : {}) });
        S.busy = false;
        if (r.ok && isObj(r.result)) {
          const replaced = Array.isArray(r.result.replaced) && r.result.replaced.length ? ", replacing " + r.result.replaced.join(", ") : "";
          const prev = typeof r.result.previous_saved_as === "string" ? r.result.previous_saved_as : null;
          const aside = typeof r.result.set_aside_as === "string" ? " The unreadable accepted state was moved, whole, to " + r.result.set_aside_as + " (nothing was deleted)." : "";
          say('Accepted "' + r.result.name + '"' + replaced + "." + aside, "ok");
          if (prev) {
            const text = "Previous accepted state saved as " + prev;
            toast("ok", text);
            setNotice({ kind: "ok", text: text + ". Undo accepts it again if the CPU still holds it; it applies nothing.", actions: [{ id: "btn-pr-undo", label: "Undo", run: () => undoAccept(prev) }] });
          }
          await reload(true);
          await refreshAcceptedChip();
        } else {
          const read = readRefusal(r.error);
          if (read) readFirst(errText(r.error));
          else if (unreadableRefusal(r.error)) {
            setNotice({ kind: "warn", text: errText(r.error), actions: [{ id: "btn-pr-aside", label: "Move it aside and accept", run: () => acceptState(name, true) }] });
          }
          say("Not accepted: " + errText(r.error), "error");
          renderList();
        }
      }

      /** Undo = profiles.accept {name: previous}: it records the previous state only while the CPU holds it, else the helper refuses NOT_APPLIED and we offer Review and apply. */
      async function undoAccept(prev) {
        if (!connected() || S.busy) return;
        S.busy = true;
        renderList();
        const r = await bridge.call("profiles.accept", { name: prev });
        S.busy = false;
        if (r.ok && isObj(r.result)) {
          setNotice(null);
          say('Accepted "' + prev + '" again' + (r.result.previous_saved_as ? "; the state it replaced is saved as " + r.result.previous_saved_as : "") + ".", "ok");
          await reload(true);
          await refreshAcceptedChip();
          return;
        }
        const notApplied = isObj(r.error) && isObj(r.error.detail) && r.error.detail.check === "NOT_APPLIED";
        if (notApplied) {
          setNotice({ kind: "warn", text: errText(r.error), actions: [{ id: "btn-pr-undo-review", label: "Review and apply", run: () => reviewIntent(store, { profile: prev }, "Apply profile " + prev) }] });
        } else if (readRefusal(r.error)) readFirst(errText(r.error));
        else setNotice({ kind: "error", text: "Not accepted: " + errText(r.error), actions: [] });
        renderList();
      }

      async function saveState() {
        const gate = renderStateGate();
        if (!gate.ok || !connected() || S.busy) return;
        const name = stateName.value;
        S.busy = true;
        renderList();
        const r = await bridge.call("profiles.save", { name, from: "read" });
        S.busy = false;
        if (r.ok) {
          say('Saved "' + name + '" (the complete state of the last read). It is not the accepted state.', "ok");
          stateName.value = "";
          S.saveOpen = false;
          setNotice(null);
          await reload();
        } else if (readRefusal(r.error)) {
          say("Not saved: " + errText(r.error), "error");
          readFirst("a saved state is what the CPU holds now, so it needs a current read.");
          renderList();
        } else {
          const exists = isObj(r.error) && isObj(r.error.detail) && r.error.detail.check === "EXISTS";
          say(exists ? 'A profile named "' + name + '" already exists. Pick another name.' : "Not saved: " + errText(r.error), "error");
          if (exists) await reload();
          else renderList();
        }
      }

      async function recordFingerprint() {
        if (!connected() || S.busy) return;
        S.busy = true;
        renderList();
        const r = await bridge.call("profiles.fingerprint");
        S.busy = false;
        renderList();
        if (r.ok && isObj(r.result)) {
          const x = r.result;
          say("CPU fingerprint recorded (" + (x.fingerprint === "complete" ? "complete" : String(x.fingerprint)) + ", " + x.cpus + " CPUs) next to the accepted state (" + (Array.isArray(x.accepted) ? x.accepted.join(" → ") : "") +
            "). Copy the accepted directory to Windows for the restore there.", String(x.fingerprint) === "complete" ? "ok" : "warn");
        } else say("Fingerprint not recorded: " + errText(r.error), "error");
      }

      async function saveProfile() {
        const gate = renderSave();
        if (!gate.ok || !connected() || S.busy) return;
        const name = nameInput.value;
        S.busy = true;
        renderSave();
        const r = await bridge.call("profiles.save", { name, intent: gate.intent });
        S.busy = false;
        if (r.ok) {
          const n = gate.intent.rows.length;
          say('Saved "' + name + '" (complete: every unit of the last read' + (n ? " with your changes" : "") + ").", "ok");
          nameInput.value = "";
          await reload();
        } else {
          const exists = isObj(r.error) && isObj(r.error.detail) && r.error.detail.check === "EXISTS";
          say(exists ? 'A profile named "' + name + '" already exists. Pick another name.' : "Not saved: " + errText(r.error), "error");
          if (exists) await reload();
          else renderSave();
        }
      }

      nameInput.addEventListener("input", () => renderSave());
      nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          void saveProfile();
        }
      });

      // ---- wiring ----
      renderStatus();
      renderList();
      disposers.push(
        store.watch(
          (s) => s.link.state,
          (state) => {
            if (state === "connected") {
              if (!S.loaded) void reload();
              if (!store.get().tune.model && store.get().tune.read.state === "idle") void tc.loadLast(); // this boot's read, if the helper has one (read-only)
            } else renderList();
          },
        ),
      );
      disposers.push(store.watch((s) => s.tune.model, () => renderSave()));
      disposers.push(store.watch((s) => s.tune.draft, () => renderSave()));
    },
    unmount() {
      for (const d of disposers.splice(0)) d();
    },
  };
}
