// views/tune/rt-common.js: the pieces the three runtime tabs share (G-217): the stylesheet, the head with the "lost on reboot" note, the read bar and the action bar (pending count, Review & Apply, Discard, Revert).
// Runtime settings are written through the root helper to sysfs / powercap; nothing here writes anything: Review & Apply only opens the apply drawer, which plans first (no hardware) and applies on its own button.
import { h } from "../../components/dom.js";
import { plural } from "../../lib/format.js";
import { fmtClock } from "../../components/fmt.js";
import { pendingCounts, discardRt } from "../../lib/rtstate.js";
import { requestReview, revertRequest } from "./runtime-kinds.js";

export const LOST_ON_REBOOT = "Runtime setting: lost on reboot (the firmware / kernel restores its own value at the next boot).";

let stylePromise = null;
export function ensureStyle() {
  if (stylePromise) return stylePromise;
  stylePromise = new Promise((resolve) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = new URL("../../styles/runtime.css", import.meta.url).href;
    link.addEventListener("load", () => resolve());
    link.addEventListener("error", () => resolve());
    document.head.append(link);
  });
  return stylePromise;
}

/** Heading + one line + the lost-on-reboot chip. */
export function head(title, sub) {
  return h("div", { class: "rt-head" }, h("h2", { class: "rt-title", text: title }), h("span", { class: "rt-chip", title: LOST_ON_REBOOT, text: "lost on reboot" }), h("p", { class: "rt-sub muted", text: sub }));
}

/** "Re-read" + the state of the last read of one helper op ("power" | "runtime"). */
export function createReadBar(store, key, label, onRead) {
  const btn = h("button", { type: "button", class: "btn small", id: "rt-read-" + key, text: "Re-read", title: "Read " + label + " again (read-only: plain MSR / sysfs reads, no OC-mailbox traffic)", on: { click: () => onRead() } });
  const text = h("span", { class: "rt-read-text muted", role: "status" });
  const el = h("div", { class: "rt-readbar" }, btn, text);
  function update() {
    const r = store.get().rt[key];
    const connected = store.get().link.state === "connected";
    btn.disabled = r.state === "reading" || !connected;
    text.textContent = r.state === "reading" ? "Reading…" : r.state === "error" ? "Read failed: " + r.error : r.state === "ready" ? "Read at " + fmtClock(r.at) : "Not read yet.";
    text.dataset.state = r.state;
  }
  return { el, update };
}

/**
 * pending count + Review & Apply + Discard (+ Revert to boot values when `revertKind` is given). `canRevert(rt)` -> "" | reason it is not offered.
 * Review opens the drawer with every pending runtime edit (and the Tune V/F draft): the drawer plans (no hardware) and applies on its own button.
 */
export function createActionBar(store, { revertKind = null, canRevert = () => "" } = {}) {
  const count = h("span", { class: "rt-pending", role: "status" });
  const review = h("button", { type: "button", class: "btn primary", id: "rt-review", text: "Review & Apply", on: { click: () => requestReview(store) } });
  const discard = h("button", { type: "button", class: "btn", id: "rt-discard", text: "Discard", title: "Drop the pending edits of the runtime controls", on: { click: () => discardRt(store) } });
  const revert = revertKind
    ? h("button", { type: "button", class: "btn", id: "rt-revert", text: "Revert to boot values", on: { click: () => requestReview(store, [revertRequest(revertKind)]) } })
    : null;
  const el = h("div", { class: "rt-actions" }, count, h("span", { class: "spacer" }), discard, review, revert);
  function update() {
    const s = store.get();
    const n = pendingCounts(s.rt).total;
    const vf = s.pending ? s.pending.changes : 0; // the Tune V/F draft rides along in the same drawer
    count.textContent = n || vf ? [n ? plural(n, "pending runtime change") : "", vf ? plural(vf, "pending V/F change") : ""].filter(Boolean).join(" · ") : "No pending changes";
    count.classList.toggle("on", n > 0 || vf > 0);
    review.disabled = (n === 0 && vf === 0) || s.link.state !== "connected";
    review.title = n === 0 && vf === 0 ? "Nothing to review" : "Plans the changes (no hardware is touched) and shows them in the Review drawer";
    const e = s.rt.edits;
    discard.disabled = Object.keys(e.power).length + Object.keys(e.caps).length + Object.keys(e.uncore).length === 0; // also an edit the plan would refuse (min above max) can be dropped
    discard.title = "Drop the pending edits of the runtime controls (V/F edits: Discard in the pending bar)";
    if (revert) {
      const why = canRevert(s.rt);
      revert.disabled = !!why || s.link.state !== "connected";
      revert.title = why || "Puts back the values the helper journaled before the first runtime write of this boot (reviewed first)";
    }
  }
  return { el, update };
}

/** A limit / source line under a control. */
export const limitNote = (text, cls = "") => h("p", { class: ["rt-limit", cls], text });
