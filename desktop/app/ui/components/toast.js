// components/toast.js: transient notices. State lives in the store (pushToast / dismissToast); this renders it and runs the expiry timers.
// Errors stay until dismissed (ttlMs 0); aria-live is polite, errors are role=alert.
import { h, icon, clear } from "./dom.js";
import { dismissToast } from "../store.js";

const ICON = { info: "info", ok: "check", warn: "alert", error: "alert" };

export function createToaster(root, store, { schedule = setTimeout, cancel = clearTimeout } = {}) {
  clear(root);
  root.setAttribute("role", "region");
  root.setAttribute("aria-label", "Notices");
  root.setAttribute("aria-live", "polite");
  const timers = new Map(); // id -> handle
  const nodes = new Map(); // id -> element

  const unsub = store.watch(
    (s) => s.toasts,
    (toasts) => {
      const live = new Set(toasts.map((t) => t.id));
      for (const [id, el] of nodes) {
        if (!live.has(id)) {
          el.remove();
          nodes.delete(id);
          if (timers.has(id)) cancel(timers.get(id));
          timers.delete(id);
        }
      }
      for (const t of toasts) {
        if (nodes.has(t.id)) continue;
        const el = h(
          "div",
          { class: ["toast", t.kind], dataset: { kind: t.kind }, role: t.kind === "error" ? "alert" : "status" },
          icon(ICON[t.kind]),
          h("span", { class: "toast-text", text: t.text }),
          h("button", { type: "button", class: "btn icon-btn", title: "Dismiss", "aria-label": "Dismiss", on: { click: () => dismissToast(store, t.id) } }, icon("close")),
        );
        nodes.set(t.id, el);
        root.append(el);
        if (t.ttlMs > 0) timers.set(t.id, schedule(() => dismissToast(store, t.id), t.ttlMs));
      }
    },
  );
  return () => {
    unsub();
    for (const handle of timers.values()) cancel(handle);
    timers.clear();
  };
}
