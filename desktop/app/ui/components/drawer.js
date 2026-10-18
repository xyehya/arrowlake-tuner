// components/drawer.js: the bottom drawer of PLAN §4.3 (review & apply, results). Slides up over the stage, never modal: the window behind it stays
// usable, there is no scrim, no focus trap, Escape closes it. Content is whatever DOM the caller hands to setContent(); G-210 (views/tune/apply.js) fills it with the apply flow.
import { h, icon, clear } from "./dom.js";
import { closeDrawer } from "../store.js";

export function createDrawer(root, store) {
  clear(root);
  root.setAttribute("role", "region");
  root.setAttribute("aria-labelledby", "drawer-title");
  const title = h("h2", { class: "drawer-title", id: "drawer-title" });
  const close = h("button", { type: "button", class: "btn icon-btn", id: "drawer-close", title: "Close (Esc)", "aria-label": "Close", on: { click: () => closeDrawer(store) } }, icon("close"));
  const body = h("div", { class: "drawer-body", id: "drawer-body" });
  const footer = h("div", { class: "drawer-footer", id: "drawer-footer" });
  root.append(h("div", { class: "drawer-head" }, title, h("span", { class: "spacer" }), close), body, footer);
  root.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closeDrawer(store);
    }
  });

  const unsub = store.watch(
    (s) => s.drawer,
    (d) => {
      title.textContent = d.title;
      root.classList.toggle("open", d.open);
      root.inert = !d.open; // closed: not focusable, not read out
      root.setAttribute("aria-hidden", String(!d.open));
    },
  );
  return {
    /** Replace the drawer's body and footer. `footer` may be null. */
    setContent(bodyNode, footerNodes = []) {
      clear(body).append(...[].concat(bodyNode || []));
      clear(footer).append(...footerNodes);
    },
    /** Focus the first element matching `selector` in the drawer (G-210: the safe button, never the one that writes). Returns whether something took focus. */
    focus(selector) {
      const el = root.querySelector(selector);
      if (!el || el.disabled) return false;
      el.focus();
      return true;
    },
    body,
    footer,
    dispose: unsub,
  };
}
