// Every helper op the UI calls must be on the Rust link's allowlist (ops.rs ALLOWED_OPS); the mock bridge does not enforce it, so without this
// a missing entry only shows on the real AppImage ("refused locally: op ... is not allowed"; G-229's throttle.tail, found live 2026-10-19).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ui = join(dirname(fileURLToPath(import.meta.url)), "..");
const opsRs = readFileSync(join(ui, "..", "src-tauri", "src", "helper", "ops.rs"), "utf8");

function allowed() {
  const m = opsRs.match(/ALLOWED_OPS: &\[&str\] = &\[([\s\S]*?)\];/);
  assert.ok(m, "ALLOWED_OPS not found in ops.rs");
  return new Set([...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]));
}

function sources(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name !== "tests" && name !== "fixtures") out.push(...sources(p));
    } else if (name.endsWith(".js")) out.push(p);
  }
  return out;
}

test("every op the UI calls is on the Rust allowlist", () => {
  const ok = allowed();
  const used = new Map();
  // bridge.call("op", ...) / .call("op") and op: "x.y" request specs
  const re = /\.call\(\s*["']([a-z_]+(?:\.[a-z_]+)?)["']|\bop:\s*["']([a-z_]+\.[a-z_]+)["']/g;
  for (const f of sources(ui)) {
    for (const m of readFileSync(f, "utf8").matchAll(re)) {
      const op = m[1] || m[2];
      if (!used.has(op)) used.set(op, f.slice(ui.length + 1));
    }
  }
  assert.ok(used.size >= 10, `found only ${used.size} op calls: the scan is broken`);
  const missing = [...used].filter(([op]) => !ok.has(op)).map(([op, f]) => `${op} (${f})`);
  assert.deepEqual(missing, [], "UI calls ops the Rust link refuses");
});
