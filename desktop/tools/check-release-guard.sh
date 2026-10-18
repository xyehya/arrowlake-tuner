#!/usr/bin/env bash
# Must-fail check: a release build with the dev-socket feature has to be refused (build.rs panic / compile_error!).
# Exit 0 = refused for the right reason (good); non-zero = the guard is broken. Host: lnx. No hardware.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tauri_dir="$(dirname "$here")/app/src-tauri"

cd "$tauri_dir"
if out="$(cargo check --release --features dev-socket 2>&1)"; then
    echo "GUARD BROKEN: 'cargo check --release --features dev-socket' succeeded" >&2
    exit 1
fi
if ! grep -q 'dev-socket. must not be enabled in a release build' <<<"$out"; then
    echo "GUARD UNCLEAR: the release+dev-socket check failed, but not with the guard message:" >&2
    tail -n 20 <<<"$out" >&2
    exit 1
fi
echo "ok: release build with dev-socket is refused"
