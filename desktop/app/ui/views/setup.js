// views/setup.js: Setup screen (PLAN §7 "First run without the helper"): shown by the shell while the helper link is missing / not allowed / busy / untrusted.
// It names the fix (the link's own message, the web GUI's wording) and offers the exact command with a copy button, plus Retry (helper_connect).
// The app never runs sudo and never installs anything: the operator pastes the command into a terminal, then presses Retry.
import { h, clear } from "../components/dom.js";
import { installStyles } from "../components/spark.js";
import { FIXES } from "../bridge.js";
import { setLink } from "../store.js";

/** Install script, run from a checkout of this repository. */
const INSTALL = "sudo bash install.sh";
const ENABLE_SOCKET = "sudo systemctl enable --now arltuner-helper.socket";
const USER_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,31}$/;

/** Link kinds (Rust `Kind::as_str`) the setup screen explains. Anything else not connected gets the generic panel. */
export const SETUP_KINDS = ["missing", "unavailable", "denied", "not_allowed", "busy", "untrusted"];

/** The kind of a normalized link ({state, kind, message, fix}); the bridge's state names stand in when the backend gave no kind. */
export function linkKind(link) {
  const l = link || {};
  if (SETUP_KINDS.includes(l.kind)) return l.kind;
  if (l.state === "not_installed") return "missing";
  if (l.state === "not_allowed") return "not_allowed";
  if (l.state === "busy") return "busy";
  return l.kind || "other";
}

/** True when the shell should show this screen instead of the work screens: a link that is down for a reason the operator can fix. */
export function needsSetup(link) {
  if (!link || link.state === "connected" || link.state === "connecting") return false;
  return SETUP_KINDS.includes(linkKind(link));
}

/** `sudo bash install.sh --add-user <user>`; `$USER` (expanded by the operator's own shell before sudo) unless a plain login name is given. */
export function addUserCommand(user) {
  const name = typeof user === "string" && USER_RE.test(user) ? user : "$USER";
  return `${INSTALL} --add-user ${name}`;
}

/**
 * What the screen says for a link: {kind, title, text, steps, command, note, tone}. `command` is the exact text to copy (null when there is none to run).
 * `text` is the link's own message when it has one (it already names the fix), else the named fix from the bridge.
 */
export function setupModel(link, { user } = {}) {
  const l = link || {};
  const kind = linkKind(l);
  const message = typeof l.message === "string" ? l.message : "";
  const base = { kind, text: message, steps: "", command: null, note: "The app never runs sudo or installs anything itself.", tone: "warn" };
  switch (kind) {
    case "missing":
      return {
        ...base,
        title: "The arltuner helper is not installed",
        text: message || "The helper is not installed or not started.",
        steps: "In a terminal, from a checkout of the arltuner repository, run this, then press Retry:",
        command: INSTALL,
      };
    case "unavailable":
      return {
        ...base,
        title: "The arltuner helper is not running",
        text: message || "Nothing is listening on the helper socket.",
        steps: "In a terminal run this, then press Retry:",
        command: ENABLE_SOCKET,
      };
    case "denied":
      return {
        ...base,
        title: "The helper socket cannot be opened",
        text: message || "Permission denied on the helper socket.",
        steps: "Reinstall to repair the socket's mode. From a checkout of the arltuner repository, run this, then press Retry:",
        command: INSTALL,
      };
    case "not_allowed":
      return {
        ...base,
        title: "Your user is not allowed to use the helper",
        text: message || "The helper's allowlist does not include this user.",
        steps: "From a checkout of the arltuner repository, add your user to the helper's allowlist, then press Retry (no re-login needed):",
        command: addUserCommand(user),
      };
    case "busy":
      return {
        ...base,
        title: "Another arltuner client is connected",
        text: message || FIXES.busy,
        steps: FIXES.busy,
        note: "The helper serves one client at a time. Nothing needs to be installed.",
      };
    case "untrusted":
      return {
        ...base,
        title: "The helper socket is not trustworthy",
        text: message || "The helper socket is not owned by root.",
        steps: "This is a safety refusal, not a missing step: the app only talks to a socket that root owns and root listens on. Do not work around it; find out who created the socket.",
        note: "Retrying does not change the check.",
        tone: "error",
      };
    default:
      return {
        ...base,
        title: "The helper is not reachable",
        text: message || "The app could not connect to the helper.",
        steps: "Press Retry. If it keeps failing, the System screen lists what the helper reports.",
      };
  }
}

/**
 * Copy `text` to the clipboard: the async Clipboard API first, then a hidden textarea + execCommand (older webviews).
 * Resolves "copied" or "failed". `clipboard` / `doc` are injectable for tests.
 */
export async function copyText(text, { clipboard = globalThis.navigator?.clipboard, doc = globalThis.document } = {}) {
  if (clipboard && typeof clipboard.writeText === "function") {
    try {
      await clipboard.writeText(text);
      return "copied";
    } catch {
      /* fall through to the selection route */
    }
  }
  if (!doc || !doc.body || typeof doc.execCommand !== "function") return "failed";
  const ta = doc.createElement("textarea");
  ta.value = text;
  ta.readOnly = true;
  ta.className = "setup-offscreen";
  doc.body.append(ta);
  ta.select();
  let ok = false;
  try {
    ok = doc.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  return ok ? "copied" : "failed";
}

const CSS = `
.setup { max-width: 720px; display: flex; flex-direction: column; gap: var(--space-4); }
.setup-card { display: flex; flex-direction: column; gap: var(--space-3); border-left: 3px solid var(--warn); }
.setup-card[data-tone="error"] { border-left-color: var(--danger); }
.setup-card[data-tone="ok"] { border-left-color: var(--ok); }
.setup-card h2 { margin: 0; font-size: var(--fs-lg); }
.setup-card p { margin: 0; }
.setup-cmd { display: flex; align-items: stretch; gap: var(--space-2); }
.setup-cmd code { flex: 1; min-width: 0; padding: var(--space-2) var(--space-3); background: var(--surface-3); border: 1px solid var(--border); border-radius: var(--radius-sm); font-family: var(--font-mono); font-size: var(--fs-md); overflow-x: auto; white-space: pre; user-select: all; }
.setup-actions { display: flex; align-items: center; gap: var(--space-3); }
.setup-status { color: var(--text-dim); font-size: var(--fs-sm); }
.setup-offscreen { position: fixed; left: -9999px; top: 0; opacity: 0; }
`;

export function setupView() {
  let off = null;
  let alive = false;
  let outcome = ""; // what the last Retry found; survives the re-render so an unchanged failure is still visibly "tried again"
  return {
    id: "setup",
    mount(root, ctx) {
      alive = true;
      installStyles("setup", CSS);
      const body = h("div", { class: "setup" });
      root.append(h("header", { class: "screen-head" }, h("h1", { text: "Setup" })), body);

      const retry = async (btn, status) => {
        btn.disabled = true;
        status.textContent = "Connecting…";
        try {
          const link = await ctx.bridge.connect();
          if (!alive) return;
          outcome = link.state === "connected" ? "" : `Still not connected (${new Date().toLocaleTimeString()}).`;
          setLink(ctx.store, link); // a new link object: the watcher below re-renders with the new message and `outcome`
          if (link.state === "connected") {
            if (ctx.onConnected) ctx.onConnected(link);
            else ctx.toast("info", "Connected to the helper.");
          }
          return;
        } catch (e) {
          if (alive) status.textContent = `Retry failed: ${e && e.message ? e.message : e}`;
        }
        if (alive) btn.disabled = false;
      };

      const render = (link) => {
        clear(body);
        if (link && link.state === "connected") {
          body.append(h("div", { class: "card setup-card", dataset: { tone: "ok" } }, h("h2", { text: "Connected to the helper" }), h("p", { class: "muted", text: "Nothing to set up." })));
          return;
        }
        const m = setupModel(link);
        const status = h("span", { class: "setup-status", role: "status", text: outcome });
        const retryBtn = h("button", { class: "btn primary", type: "button", text: "Retry" });
        retryBtn.addEventListener("click", () => retry(retryBtn, status));
        const parts = [h("h2", { text: m.title }), h("p", { text: m.text })];
        if (m.steps) parts.push(h("p", { class: "muted", text: m.steps }));
        if (m.command) {
          const copyBtn = h("button", { class: "btn", type: "button", text: "Copy" });
          copyBtn.addEventListener("click", async () => {
            const r = await copyText(m.command);
            if (!alive) return;
            status.textContent = r === "copied" ? "Copied. Paste it into a terminal." : "Could not copy: select the command and press Ctrl+C.";
          });
          parts.push(h("div", { class: "setup-cmd" }, h("code", { text: m.command, "aria-label": "command to run" }), copyBtn));
        }
        parts.push(h("div", { class: "setup-actions" }, retryBtn, status));
        parts.push(h("p", { class: "muted", text: m.note }));
        body.append(h("div", { class: "card setup-card", dataset: { tone: m.tone, kind: m.kind } }, ...parts));
      };
      off = ctx.store.watch((s) => s.link, render);
    },
    unmount() {
      alive = false;
      if (off) off();
      off = null;
    },
  };
}
