// components/placeholder.js: what a screen shows until its task lands. Each real view (views/<id>.js) replaces the entry in app.js's VIEWS map.
import { h } from "./dom.js";
import { SCREENS } from "./nav-rail.js";

export function placeholderView(id) {
  const s = SCREENS.find((x) => x.id === id);
  return {
    id,
    mount(root) {
      root.append(
        h("header", { class: "screen-head" }, h("h1", { text: s.label })),
        h("div", { class: "card placeholder" }, h("p", { text: s.blurb }), h("p", { class: "muted", text: `This screen arrives with ${s.task}. The frame around it (status bar, monitoring strip, pending bar, toasts, drawer) is already live.` })),
      );
    },
    unmount() {},
  };
}
