#!/usr/bin/env python3
"""Run the whole offline test suite (stdlib unittest). Touches no hardware: tests use the simulator / fake files only."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

# ---------------------------------------------------------------------------
# Desktop test hooks (G-203): also run the UI's node:test files and the Tauri
# crate's Rust tests when `node` / `cargo` are on PATH. A missing tool prints
# "skipped: <tool> missing" and does not fail the suite; a failing run does.
# ARLTUNER_SKIP_DESKTOP_TESTS=1 skips both hooks (e.g. on a Python-only host).
# Stdlib only, no npm, fixed subprocess timeouts.
# ---------------------------------------------------------------------------
UI_TESTS_DIR = ROOT / "desktop/app/ui/tests"
CARGO_MANIFEST = ROOT / "desktop/app/src-tauri/Cargo.toml"
HOOK_TIMEOUT_S = 600  # cold Rust build of the Tauri crate takes minutes


def run_desktop_hooks() -> bool:
    """Run the node/cargo hooks. Returns True when every hook that ran passed."""
    if os.environ.get("ARLTUNER_SKIP_DESKTOP_TESTS") == "1":
        print("skipped: desktop test hooks (ARLTUNER_SKIP_DESKTOP_TESTS=1)")
        return True
    ok = True

    # (a) node --test over the UI test directory, only if it holds *.test.mjs files.
    # The directory's files are listed explicitly: node ≥ 24 rejects a bare directory
    # argument ("Cannot find module .../tests"), while a file list works on 18 through 24.
    node = shutil.which("node")
    ui_test_files = sorted(UI_TESTS_DIR.glob("*.test.mjs"))
    if node is None:
        print("skipped: node missing")
    elif not ui_test_files:
        print(f"skipped: no *.test.mjs in {UI_TESTS_DIR.relative_to(ROOT)}")
    else:
        print(f"\n== node --test {UI_TESTS_DIR.relative_to(ROOT)} ==", flush=True)
        try:
            rc = subprocess.run(
                [node, "--test", *map(str, ui_test_files)], cwd=str(ROOT), timeout=HOOK_TIMEOUT_S
            ).returncode
        except subprocess.TimeoutExpired:
            print(f"node --test timed out after {HOOK_TIMEOUT_S}s")
            ok = False
        else:
            if rc != 0:
                print(f"node --test failed (exit {rc})")
                ok = False

    # (b) cargo test for the desktop crate, with a shared cache target dir by default.
    cargo = shutil.which("cargo")
    if cargo is None:
        print("skipped: cargo missing")
    else:
        env = dict(os.environ)
        env.setdefault("CARGO_TARGET_DIR", str(Path.home() / ".cache" / "arltuner-desktop-target"))
        print(f"\n== cargo test --manifest-path {CARGO_MANIFEST.relative_to(ROOT)} ==", flush=True)
        try:
            rc = subprocess.run(
                [cargo, "test", "--manifest-path", str(CARGO_MANIFEST)],
                cwd=str(ROOT),
                env=env,
                timeout=HOOK_TIMEOUT_S,
            ).returncode
        except subprocess.TimeoutExpired:
            print(f"cargo test timed out after {HOOK_TIMEOUT_S}s")
            ok = False
        else:
            if rc != 0:
                print(f"cargo test failed (exit {rc})")
                ok = False

    return ok


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    hooks_ok = run_desktop_hooks()
    sys.exit(0 if result.wasSuccessful() and hooks_ok else 1)
