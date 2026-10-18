// node --test "desktop/app/ui/tests/*.test.mjs"   (G-219: Setup screen model + copy, bridge settings/notify passthroughs; no DOM, no network, no hardware)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Bridge, createTauriTransport, normalizeLink } from "../bridge.js";
import { createMockTransport } from "../fixtures/mock-backend.js";
import { setupModel, needsSetup, linkKind, addUserCommand, copyText, setupView, SETUP_KINDS } from "../views/setup.js";

const link = (raw) => normalizeLink(raw);

// ---------------------------------------------------------------- setup model
test("setup: which links need the setup screen", () => {
  assert.equal(needsSetup(link({ state: "connected" })), false);
  assert.equal(needsSetup(link({ state: "connecting" })), false);
  assert.equal(needsSetup(null), false);
  for (const kind of ["missing", "unavailable", "denied", "not_allowed", "busy", "untrusted"]) assert.equal(needsSetup(link({ connected: false, kind, message: "m" })), true, kind);
  assert.equal(needsSetup(link({ state: "not_installed" })), true);
  assert.equal(needsSetup(link({ state: "not_allowed" })), true);
  assert.equal(needsSetup(link({ state: "busy" })), true);
  // failures with nothing the operator can set up stay on the normal screens (status bar + toast)
  assert.equal(needsSetup(link({ state: "error", kind: "timeout", message: "t" })), false);
  assert.equal(needsSetup(link({ state: "error", kind: "no_runtime", message: "no desktop runtime" })), false);
  assert.equal(needsSetup(link({ state: "error" })), false);
});

test("setup: the kind comes from the helper-link kind, else from the bridge state", () => {
  assert.equal(linkKind({ state: "error", kind: "untrusted" }), "untrusted");
  assert.equal(linkKind({ state: "not_installed", kind: null }), "missing");
  assert.equal(linkKind({ state: "not_allowed", kind: null }), "not_allowed");
  assert.equal(linkKind({ state: "busy", kind: null }), "busy");
  assert.equal(linkKind({ state: "not_installed", kind: "unavailable" }), "unavailable"); // kind wins: a refused connect is not a missing install
  assert.equal(linkKind({ state: "error", kind: "closed" }), "closed");
  assert.equal(linkKind(undefined), "other");
});

test("setup: each kind names its fix and the exact command", () => {
  const msg = (kind, message = `msg for ${kind}`) => setupModel(link({ connected: false, kind, message }));
  assert.equal(msg("missing").command, "sudo bash install.sh");
  assert.equal(msg("unavailable").command, "sudo systemctl enable --now arltuner-helper.socket");
  assert.equal(msg("denied").command, "sudo bash install.sh");
  assert.equal(msg("not_allowed").command, "sudo bash install.sh --add-user $USER");
  assert.equal(msg("busy").command, null);
  assert.equal(msg("untrusted").command, null);
  assert.equal(msg("untrusted").tone, "error");
  for (const kind of SETUP_KINDS) {
    const m = msg(kind);
    assert.ok(m.title && m.text === `msg for ${kind}`, kind); // the link's own message (the named fix text) is shown as it came
    assert.ok(m.steps, kind);
    assert.match(m.note, /./);
  }
  assert.match(msg("busy").steps, /Close the other arltuner/);
  // no message from the backend: a default text, never empty
  assert.ok(setupModel(link({ state: "not_installed" })).text.length > 10);
  assert.ok(setupModel(link({ state: "busy" })).text.length > 10);
});

test("setup: add-user command uses the given login name, else the shell's $USER; never anything that is not a plain name", () => {
  assert.equal(addUserCommand("yk"), "sudo bash install.sh --add-user yk");
  assert.equal(addUserCommand("Svc_user-1"), "sudo bash install.sh --add-user Svc_user-1");
  for (const bad of ["", "a b", "a;rm -rf /", "$(id)", "-x", "x".repeat(33), "ü", null, undefined, 5])
    assert.equal(addUserCommand(bad), "sudo bash install.sh --add-user $USER", String(bad));
  assert.equal(setupModel(link({ state: "not_allowed" }), { user: "yk" }).command, "sudo bash install.sh --add-user yk");
});

test("setup: the model is total (odd input never throws)", () => {
  for (const l of [null, undefined, {}, { state: 5, message: 7 }, { kind: "weird" }, { state: "error", kind: "root", message: "run as your user" }]) {
    const m = setupModel(l);
    assert.ok(m.title && typeof m.text === "string");
    assert.equal(m.command, null);
  }
});

// ---------------------------------------------------------------- copy
test("copyText: Clipboard API first", async () => {
  const seen = [];
  assert.equal(await copyText("sudo bash install.sh", { clipboard: { writeText: async (t) => seen.push(t) }, doc: null }), "copied");
  assert.deepEqual(seen, ["sudo bash install.sh"]);
});

test("copyText: a refused clipboard falls back to the selection route, and reports failure when there is none", async () => {
  const refuse = { writeText: async () => { throw new Error("denied"); } };
  assert.equal(await copyText("x", { clipboard: refuse, doc: null }), "failed");
  assert.equal(await copyText("x", { clipboard: undefined, doc: null }), "failed");
  const made = [];
  const doc = {
    body: { append: (el) => made.push(el) },
    createElement: () => ({ select() {}, remove() { this.removed = true; } }),
    execCommand: (c) => c === "copy",
  };
  assert.equal(await copyText("x", { clipboard: refuse, doc }), "copied");
  assert.equal(made.length, 1);
  assert.equal(made[0].value, "x");
  assert.equal(made[0].removed, true);
  assert.equal(await copyText("x", { clipboard: refuse, doc: { ...doc, execCommand: () => false } }), "failed");
  assert.equal(await copyText("x", { clipboard: refuse, doc: { ...doc, execCommand: () => { throw new Error("no"); } } }), "failed");
});

test("setup view: a view object, and the source keeps to the strict-CSP rules", () => {
  const v = setupView();
  assert.equal(v.id, "setup");
  assert.equal(typeof v.mount, "function");
  v.unmount(); // before mount: harmless
  const src = readFileSync(fileURLToPath(new URL("../views/setup.js", import.meta.url)), "utf8");
  assert.doesNotMatch(src, /innerHTML|outerHTML|insertAdjacentHTML|\.style\b|style:/);
  assert.doesNotMatch(src, /child_process|invoke\(|__TAURI__/); // never runs anything; no direct IPC (bridge.js is the only file that touches __TAURI__)
});

// ---------------------------------------------------------------- bridge: settings / notify
test("bridge: the Tauri transport maps settings and notify onto their commands", async () => {
  const calls = [];
  const tauri = {
    core: { invoke: async (cmd, args) => (calls.push([cmd, args]), { ok: true, result: { refresh_s: 1 } }) },
    event: { listen: async () => () => {} },
  };
  const bridge = new Bridge(createTauriTransport(tauri), "tauri");
  assert.deepEqual(await bridge.settingsGet(), { ok: true, result: { refresh_s: 1 } });
  await bridge.settingsSet({ refresh_s: 2 });
  await bridge.notify("done", "all good");
  await bridge.notify("only a title");
  assert.deepEqual(calls, [
    ["settings_get", undefined],
    ["settings_set", { patch: { refresh_s: 2 } }],
    ["notify", { title: "done", body: "all good" }],
    ["notify", { title: "only a title", body: "" }],
  ]);
});

test("bridge: argument checks, backend errors and a transport without the passthroughs", async () => {
  const bridge = new Bridge(createMockTransport({ intervalMs: 10, stepMs: 1 }), "mock");
  assert.throws(() => bridge.settingsSet(null), TypeError);
  assert.throws(() => bridge.settingsSet([1]), TypeError);
  assert.throws(() => bridge.notify(""), TypeError);
  assert.throws(() => bridge.notify(5), TypeError);
  const res = await bridge.settingsGet();
  if (res.ok) return; // a mock that grows settings later is fine; today it has none
  assert.equal(res.error.code, "UNSUPPORTED");

  const boom = new Bridge({ connect: async () => ({}), call: async () => ({}), listen: () => () => {}, settingsGet: async () => { throw new Error("ipc down"); } }, "x");
  assert.deepEqual(await boom.settingsGet(), { ok: false, error: { code: "BRIDGE_ERROR", message: "ipc down" } });
  const odd = new Bridge({ connect: async () => ({}), call: async () => ({}), listen: () => () => {}, settingsGet: async () => "nope" }, "x");
  assert.equal((await odd.settingsGet()).error.code, "BRIDGE_ERROR");
});
