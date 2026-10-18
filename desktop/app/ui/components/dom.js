// components/dom.js: tiny element helpers. Strict-CSP rules (PLAN §2): text only via textContent, classes via classList, no style attributes,
// no inline handlers, no innerHTML. `h()` refuses `style` and `on*` attributes so a later view cannot slip one in by accident.

const SVG_NS = "http://www.w3.org/2000/svg";

function applyProps(el, props) {
  for (const [key, val] of Object.entries(props || {})) {
    if (val === undefined || val === null || val === false) continue;
    if (key === "class") {
      for (const c of [].concat(val)) if (c) el.classList.add(...String(c).split(/\s+/).filter(Boolean));
    } else if (key === "text") {
      el.textContent = String(val);
    } else if (key === "on") {
      for (const [ev, fn] of Object.entries(val)) el.addEventListener(ev, fn);
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(val)) el.dataset[k] = String(v);
    } else if (key === "props") {
      Object.assign(el, val); // DOM properties (value, checked, disabled, hidden, type, ...)
    } else if (key === "style" || /^on/i.test(key)) {
      throw new Error(`dom.h: attribute "${key}" is not allowed (strict CSP)`);
    } else {
      el.setAttribute(key, val === true ? "" : String(val));
    }
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props);
  append(el, children);
  return el;
}

export function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.classList.add(...String(v).split(/\s+/).filter(Boolean));
    else if (k === "style" || /^on/i.test(k)) throw new Error(`dom.svg: attribute "${k}" is not allowed (strict CSP)`);
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

/** A mask-icon span; the shape comes from icons/<name>.svg through styles/icons.css, the colour from currentColor. */
export function icon(name, extra) {
  return h("span", { class: ["icon", extra], dataset: { icon: name }, "aria-hidden": "true" });
}

export function button(label, props = {}, ...children) {
  const { class: cls, on, ...rest } = props;
  return h("button", { type: "button", class: ["btn", cls], on, ...rest }, label, ...children);
}
