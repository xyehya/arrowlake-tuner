#!/usr/bin/env bash
# Build the arltuner-desktop AppImage (PLAN §7). Host: lnx. No npm; cargo fetches crates, and
# `cargo tauri build` fetches linuxdeploy into ~/.cache/tauri on first use (operator-approved).
# Output: desktop/dist/arltuner-desktop-<version>-x86_64.AppImage (gitignored) + its sha256.
# Environment knobs: NO_STRIP=true (linuxdeploy strip fails on some hosts); APPIMAGE_EXTRACT_AND_RUN=1 (no FUSE).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
desktop="$(dirname "$here")"
tauri_dir="$desktop/app/src-tauri"
dist="$desktop/dist"

# The dev-socket seam must never be in the shipped build (build.rs also refuses it in release profiles).
for a in "$@"; do
    case "$a" in
        *dev-socket*) echo "refused: feature dev-socket must not be enabled for the AppImage" >&2; exit 2 ;;
    esac
done
if grep -Eq '^[[:space:]]*default[[:space:]]*=.*dev-socket' "$tauri_dir/Cargo.toml"; then
    echo "refused: feature dev-socket is enabled by default in Cargo.toml" >&2
    exit 2
fi
if [[ "${CARGO_BUILD_FEATURES:-}${CARGO_FEATURES:-}" == *dev-socket* ]]; then
    echo "refused: feature dev-socket requested via environment" >&2
    exit 2
fi

# Prove the guard still bites (a release check with dev-socket must fail) before building anything.
bash "$here/check-release-guard.sh"

version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$tauri_dir/Cargo.toml" | head -n1)"
[[ -n "$version" ]] || { echo "cannot read version from Cargo.toml" >&2; exit 1; }

cd "$tauri_dir"
cargo tauri build --bundles appimage "$@"

target="${CARGO_TARGET_DIR:-$tauri_dir/target}"
bundle="$(ls -1 "$target"/release/bundle/appimage/*.AppImage 2>/dev/null | head -n1 || true)"
[[ -n "$bundle" ]] || { echo "no AppImage found under $target/release/bundle/appimage" >&2; exit 1; }

mkdir -p "$dist"
out="$dist/arltuner-desktop-${version}-x86_64.AppImage"
cp -f "$bundle" "$out"
chmod 0755 "$out"
echo "built: $out"
sha256sum "$out"
