// components/pending-bar.js: PLAN §4.1: "N changes on M units · Discard · Review & Apply", shown whenever the draft differs from the CPU.
// It only reports and forwards clicks; the draft itself arrives with G-207 (store.pending is set by whoever owns the draft).
import { h, clear } from "./dom.js";
import { plural } from "./fmt.js";

export function pendingText(p) {
  return `${plural(p.changes, "change")} on ${plural(p.units, "unit")}`;
}

export function createPendingBar(root, store, { onDiscard, onReview }) {
  clear(root);
  root.setAttribute("role", "region");
  root.setAttribute("aria-label", "Pending changes");
  const text = h("span", { class: "pending-text num", id: "pending-text" });
  const discard = h("button", { type: "button", class: "btn", id: "pending-discard", text: "Discard", on: { click: () => onDiscard() } });
  const review = h("button", { type: "button", class: "btn primary", id: "pending-review", text: "Review & Apply", on: { click: () => onReview() } });
  root.append(h("span", { class: "pending-dot", "aria-hidden": "true" }), text, h("span", { class: "spacer" }), discard, review);
  return store.watch(
    (s) => s.pending,
    (p) => {
      root.hidden = p.changes <= 0;
      text.textContent = pendingText(p);
    },
  );
}
