// components/status-bar.js: top bar, PLAN §4.1: helper state (+ the fix command), mode (+ why), applied-vs-accepted chip, last read, ops this boot.
import { h, clear } from "./dom.js";
import { fmtClock } from "./fmt.js";

const LINK_LABEL = {
  connecting: "connecting",
  connected: "helper connected",
  busy: "helper busy (another client)",
  not_installed: "helper not installed",
  not_allowed: "helper refused this user",
  error: "helper unavailable",
};

const ACCEPTED_LABEL = { unknown: "accepted: unknown", matches: "accepted: matches", equivalent: "accepted: equivalent", stock: "accepted: stock" };

export function linkLabel(link) {
  return LINK_LABEL[link.state] || link.state;
}

export function acceptedLabel(a) {
  return a.kind === "differs" ? `accepted: differs: ${a.n}` : ACCEPTED_LABEL[a.kind] || "accepted: unknown";
}

function chip(id, extra) {
  return h("span", { class: ["chip", extra], id });
}

export function createStatusBar(root, store) {
  clear(root);
  root.setAttribute("role", "banner");
  const brand = h("span", { class: "brand", text: "arltuner" });
  const helper = chip("chip-helper");
  const fix = h("code", { class: "fix", id: "helper-fix", hidden: true });
  const mode = chip("chip-mode");
  const accepted = chip("chip-accepted");
  const lastRead = h("span", { class: "meta num", id: "last-read" });
  const ops = h("span", { class: "meta num", id: "ops-used" });
  root.append(brand, helper, fix, mode, accepted, h("span", { class: "spacer" }), lastRead, ops);

  const unsubs = [
    store.watch(
      (s) => s.link,
      (link) => {
        helper.textContent = linkLabel(link);
        helper.dataset.state = link.state;
        helper.title = link.message || linkLabel(link);
        fix.hidden = !link.fix;
        fix.textContent = link.fix || "";
        fix.title = "Run this in a terminal; the app never runs sudo itself.";
      },
    ),
    store.watch(
      (s) => s.mode,
      (m) => {
        mode.textContent = m.label;
        mode.dataset.state = m.writes ? "ok" : m.label === "mode unknown" ? "unknown" : "warn";
        mode.title = m.writes ? "The helper accepts writes (the CPU's own limits still bound every value)." : `Read-only: ${m.reason}`;
      },
    ),
    store.watch(
      (s) => s.accepted,
      (a) => {
        accepted.textContent = acceptedLabel(a);
        accepted.dataset.state = a.kind === "differs" ? "warn" : a.kind === "unknown" ? "unknown" : "ok";
        accepted.title = a.note || "";
      },
    ),
    store.watch(
      (s) => s.lastReadAt,
      (t) => {
        lastRead.textContent = t ? `last read ${fmtClock(t)}` : "no read this boot";
        lastRead.title = t ? "Time of the last tune.read. Reading is mailbox traffic and is never automatic." : "tune.read has not run since boot.";
      },
    ),
    store.watch(
      (s) => s.helperOp,
      (op) => {
        ops.textContent = op.opsBudget ? `ops ${op.opsUsed} / ${op.opsBudget}` : "ops n/a";
        ops.title = `Helper state: ${op.state}. Mailbox operations used since the helper started, and the budget.`;
        root.dataset.helperState = op.state;
      },
    ),
  ];
  return () => unsubs.forEach((u) => u());
}
