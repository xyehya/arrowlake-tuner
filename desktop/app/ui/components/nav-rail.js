// components/nav-rail.js: left rail, PLAN §4.1: Dashboard · Tune · Curves · Stress · Profiles · System · Audit, icons + labels, keys Alt+1..7 (G-234).
import { h, icon, clear } from "./dom.js";

export const SCREENS = [
  { id: "dashboard", label: "Dashboard", key: "1", task: "G-206", blurb: "Core map, live tiles, charts and the memory card." },
  { id: "tune", label: "Tune", key: "2", task: "G-208", blurb: "Core tile grid, V/F curve editor, turbo ratios, power, ring and parking." },
  { id: "curves", label: "Curves", key: "3", task: "G-218", blurb: "Measure, compare and keep V/F curves." },
  { id: "stress", label: "Stress", key: "4", task: "G-218", blurb: "Quick screens with caps and history. A screen is never a pass (D-019)." },
  { id: "profiles", label: "Profiles", key: "5", task: "G-213", blurb: "Accepted state, saved profiles, compare and load into Tune." },
  { id: "system", label: "System", key: "6", task: "G-206", blurb: "Preflight, limits with their sources, helper and settings." },
  { id: "audit", label: "Audit", key: "7", task: "G-206", blurb: "The helper's audit log tail, filterable." },
];

/** Screen id for a pressed Alt+digit ("1".."7"), or null. G-234: a bare digit never switches (focus can sit on a non-field after a click, so typing a value would change screens); Alt works from a field too. */
export function screenForKey(key, { ctrl = false, alt = false, meta = false } = {}) {
  if (!alt || ctrl || meta) return null;
  const s = SCREENS.find((x) => x.key === key);
  return s ? s.id : null;
}

export function createNavRail(root, store, { onSelect }) {
  const items = new Map();
  clear(root);
  root.setAttribute("role", "navigation");
  root.setAttribute("aria-label", "Screens");
  for (const s of SCREENS) {
    const a = h(
      "a",
      { class: "nav-item", href: `#/${s.id}`, dataset: { screen: s.id }, id: `nav-${s.id}`, title: `${s.label} (Alt+${s.key})`, on: { click: (e) => { e.preventDefault(); onSelect(s.id); } } },
      icon(s.id),
      h("span", { class: "nav-label", text: s.label }),
      h("kbd", { class: "nav-key", text: `Alt+${s.key}` }),
    );
    items.set(s.id, a);
    root.append(a);
  }
  return store.watch(
    (st) => st.screen,
    (screen) => {
      for (const [id, a] of items) {
        const on = id === screen;
        a.classList.toggle("active", on);
        if (on) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      }
    },
  );
}
