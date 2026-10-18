// components/stepper.js: the numeric field every Tune editor uses (G-208): typed value, ± buttons, ArrowUp/Down (step, Shift = big step), wheel while focused, Enter/blur commit, Escape revert.
// One commit path: every way of changing the value ends in `onCommit(number)`; the owner puts the number into the shared draft and calls `set()` again with what the draft says.
// A field that stands for several units shows an aggregate: `mixed` ({min,max}) displays "mixed (min…max unit)" as the placeholder with an empty value; typing sets all.
// Arrows on a mixed field step from the highest value (up) / the lowest (down), so one press moves every unit in the direction pressed.
import { h } from "./dom.js";
import { MINUS, fmtMvNum, isNum } from "../lib/format.js";

/** Typed text -> number | null. Accepts "-35", "−35", "+5", " 35 mV", "1,250" (decimal comma) when `decimals` > 0. Anything else is null. */
export function parseInput(text, { decimals = 0, unit = "" } = {}) {
  let t = String(text ?? "").trim().replace(/−|–|‒/g, "-");
  if (unit && t.toLowerCase().endsWith(unit.toLowerCase())) t = t.slice(0, -unit.length).trim();
  t = t.replace(/[x×]$/i, "").trim();
  if (decimals > 0) t = t.replace(",", ".");
  const re = decimals > 0 ? /^[+-]?(\d+\.?\d*|\.\d+)$/ : /^[+-]?\d+$/;
  if (!re.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** The next/previous multiple of `step` strictly beyond `cur` (arrow keys land on the grid: -19 + up = -15, not -14). */
export function gridStep(cur, dir, step) {
  const q = cur / step;
  const eps = 1e-9;
  const n = dir > 0 ? Math.floor(q + eps) + 1 : Math.ceil(q - eps) - 1;
  return Math.round(n * step * 1e6) / 1e6;
}

/** "mixed (−50…−35 mV)" from {min,max} and the formatter of one number. */
export function mixedText(m, fmt, unit = "") {
  return "mixed (" + fmt(m.min) + "…" + fmt(m.max) + (unit ? " " + unit : "") + ")";
}

const signedMv = (v) => fmtMvNum(v);

/**
 * opts: label (accessible name), unit ("mV" | "V" | "×"), hideUnit (the column header says it), step, big, min, max, decimals, format(v) -> text, title (tooltip: register / source), onCommit(number), emptyStart (value arrows start from when the field holds none).
 * Returns {el, input, set(state), focus(), commitText()}. state: {value, mixed:{min,max}, pending, disabled, reason, placeholder, was}.
 */
export function createStepper(opts) {
  const { label, unit = "mV", step = 5, big = 25, decimals = 0, onCommit } = opts;
  const format = opts.format || (decimals > 0 ? (v) => v.toFixed(decimals) : signedMv);
  const st = { value: null, mixed: null, pending: false, disabled: false, min: opts.min, max: opts.max, emptyStart: opts.emptyStart };
  let dirty = false;

  const input = h("input", {
    class: "step-input num",
    type: "text",
    inputmode: decimals > 0 ? "decimal" : "numeric",
    role: "spinbutton",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": label,
    title: opts.title || label,
  });
  const dec = h("button", { type: "button", class: "step-btn", tabindex: "-1", "aria-label": "Decrease " + label, title: "Decrease by " + step + " " + unit, text: MINUS });
  const inc = h("button", { type: "button", class: "step-btn", tabindex: "-1", "aria-label": "Increase " + label, title: "Increase by " + step + " " + unit, text: "+" });
  const was = h("span", { class: "step-was", hidden: true });
  const el = h("span", { class: "stepper", dataset: { state: "empty" } }, dec, input, inc, opts.hideUnit ? null : h("span", { class: "step-unit", text: unit }), was);

  const clamp = (v) => (isNum(st.min) && v < st.min ? st.min : isNum(st.max) && v > st.max ? st.max : v);

  function show() {
    dirty = false;
    let state = "value";
    if (st.mixed) {
      state = "mixed";
      input.value = "";
      input.placeholder = mixedText(st.mixed, format, "");
      input.removeAttribute("aria-valuenow");
      input.setAttribute("aria-valuetext", mixedText(st.mixed, format, unit));
    } else if (isNum(st.value)) {
      input.value = format(st.value);
      input.placeholder = "";
      input.setAttribute("aria-valuenow", String(st.value));
      input.setAttribute("aria-valuetext", format(st.value) + " " + unit);
    } else {
      state = "empty";
      input.value = "";
      input.placeholder = st.placeholder || "–";
      input.removeAttribute("aria-valuenow");
      input.setAttribute("aria-valuetext", "none");
    }
    if (isNum(st.min)) input.setAttribute("aria-valuemin", String(st.min));
    if (isNum(st.max)) input.setAttribute("aria-valuemax", String(st.max));
    el.dataset.state = state;
    el.classList.toggle("pending", st.pending);
    el.classList.toggle("disabled", st.disabled);
    input.disabled = st.disabled;
    dec.disabled = inc.disabled = st.disabled;
    input.removeAttribute("aria-invalid");
    if (st.reason) input.title = st.reason;
    else input.title = opts.title || label;
    was.hidden = !(st.pending && st.was);
    was.textContent = st.was ? "was " + st.was : "";
  }

  function commit(v) {
    if (st.disabled || !isNum(v)) return;
    const c = clamp(v);
    if (!st.mixed && isNum(st.value) && c === st.value) {
      show();
      return;
    }
    onCommit(c);
  }

  function nudge(dir, size) {
    if (st.disabled) return;
    if (!st.mixed && !isNum(st.value) && isNum(st.emptyStart)) {
      commit(st.emptyStart); // a field with no value starts at its first allowed value
      return;
    }
    const cur = st.mixed ? (dir > 0 ? st.mixed.max : st.mixed.min) : isNum(st.value) ? st.value : 0;
    const to = gridStep(cur, dir, size);
    if ((dir < 0 && isNum(st.min) && to < st.min) || (dir > 0 && isNum(st.max) && to > st.max)) {
      // past the window: the end of it when a big step overshoots, nothing when already there or outside (a stock value below an OC window must not jump up)
      const edge = dir < 0 ? st.min : st.max;
      if (st.mixed || (isNum(st.value) && (dir < 0 ? st.value > edge : st.value < edge))) commit(edge);
      return;
    }
    commit(to);
  }

  function commitText() {
    if (st.disabled) return;
    const text = input.value;
    if (text.trim() === "") {
      show();
      return;
    }
    const n = parseInput(text, { decimals, unit });
    if (n === null) {
      input.setAttribute("aria-invalid", "true");
      input.title = "Not a number: " + text;
      dirty = false;
      return;
    }
    dirty = false;
    commit(n);
  }

  input.addEventListener("input", () => {
    dirty = true;
    input.removeAttribute("aria-invalid");
  });
  input.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === "ArrowUp" || e.key === "ArrowDown" || e.key === "PageUp" || e.key === "PageDown") {
      e.preventDefault();
      const dir = e.key === "ArrowUp" || e.key === "PageUp" ? 1 : -1;
      nudge(dir, e.shiftKey || e.key.startsWith("Page") ? big : step);
    } else if (e.key === "Enter") {
      e.preventDefault();
      commitText();
    } else if (e.key === "Escape" && (dirty || input.getAttribute("aria-invalid"))) {
      e.preventDefault();
      e.stopPropagation();
      show();
    }
  });
  input.addEventListener("blur", () => {
    if (dirty) commitText();
  });
  input.addEventListener("focus", () => input.select());
  input.addEventListener(
    "wheel",
    (e) => {
      if (document.activeElement !== input || st.disabled || e.deltaY === 0) return; // an unfocused field lets the page scroll
      e.preventDefault();
      nudge(e.deltaY < 0 ? 1 : -1, e.shiftKey ? big : step);
    },
    { passive: false },
  );
  dec.addEventListener("click", () => nudge(-1, step));
  inc.addEventListener("click", () => nudge(1, step));

  show();
  return {
    el,
    input,
    focus: () => input.focus(),
    commitText,
    set(state) {
      st.value = isNum(state.value) ? state.value : null;
      st.mixed = state.mixed || null;
      st.pending = !!state.pending;
      st.disabled = !!state.disabled;
      st.reason = state.reason || "";
      st.placeholder = state.placeholder || "";
      st.was = state.was || "";
      if (state.min !== undefined) st.min = state.min;
      if (state.max !== undefined) st.max = state.max;
      if (state.emptyStart !== undefined) st.emptyStart = state.emptyStart;
      if (document.activeElement === input && dirty) return; // never overwrite what is being typed
      show();
    },
  };
}
