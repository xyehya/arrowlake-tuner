#!/bin/bash
# arrowlake-tuner uninstall (G-106). Run as root:   sudo bash uninstall.sh [--purge]
#
# Usage: uninstall.sh [--purge --yes-reset-budget]   (--purge deletes the audit log, which RESETS the per-boot operation cap: it needs the second flag)
#
# Removes the code (/usr/lib/arltuner), the web files and the polkit action. Idempotent.
# KEEPS /etc/arltuner (policy, accepted state), /var/lib/arltuner (audit log), the tmpfiles.d file (/usr/lib/tmpfiles.d/arltuner.conf) and
# /run/arltuner unless --purge: while /etc/arltuner/policy.toml stays, the CLI counts as installed (D-030), keeps the per-boot budget in
# /var/lib/arltuner/audit.jsonl and takes its lock in /run/arltuner/mailbox.lock, so the operator's boot restore still works after a plain uninstall.
# --purge removes all of them; the CLI then returns to the legacy per-user lock / audit paths and the per-boot operation count (an indicator, default cap 100000)
# restarts from zero, so purge only right before a reboot.
# DESTDIR=/some/dir removes from that tree instead (tests).
set -euo pipefail
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

die() { echo "uninstall.sh: $*" >&2; exit 1; }

PURGE=0
RESET_OK=0
for arg in "$@"; do
    case $arg in
        --purge) PURGE=1 ;;
        --yes-reset-budget) RESET_OK=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $arg (usage: uninstall.sh [--purge --yes-reset-budget])" ;;
    esac
done

[ "$PURGE" -eq 0 ] || [ "$RESET_OK" -eq 1 ] || die "--purge deletes /var/lib/arltuner/audit.jsonl, which RESETS the per-boot operation count (an indicator, default cap 100000) and the budget safety net; add --yes-reset-budget to confirm (nothing was removed)"
[ "$PURGE" -eq 1 ] || [ "$RESET_OK" -eq 0 ] || die "--yes-reset-budget only goes with --purge"

DESTDIR=${DESTDIR:-}
DESTDIR=${DESTDIR%/}
if [ -n "$DESTDIR" ]; then
    case $DESTDIR in /*) ;; *) die "DESTDIR must be an absolute path" ;; esac
else
    [ "$(id -u)" -eq 0 ] || die "must run as root (sudo bash uninstall.sh)"
fi

remove() {  # remove PATH...: each path is a fixed suffix under DESTDIR, never empty
    local p
    for p in "$@"; do
        if [ -e "$p" ] || [ -L "$p" ]; then rm -rf -- "$p"; echo "removed $p"; fi
    done
}

remove "$DESTDIR/usr/lib/arltuner" \
       "$DESTDIR/usr/share/arltuner/web" \
       "$DESTDIR/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy"
rmdir "$DESTDIR/usr/share/arltuner" 2>/dev/null && echo "removed $DESTDIR/usr/share/arltuner" || true

if [ "$PURGE" -eq 1 ]; then
    remove "$DESTDIR/usr/lib/tmpfiles.d/arltuner.conf" "$DESTDIR/run/arltuner" "$DESTDIR/etc/arltuner" "$DESTDIR/var/lib/arltuner"
    echo "purged config, audit log, tmpfiles.d file and lock directory. The CLI is back on the legacy lock/audit paths; its per-boot operation count restarts from zero: reboot before the next hardware write."
else
    for keep in "$DESTDIR/etc/arltuner" "$DESTDIR/var/lib/arltuner" "$DESTDIR/usr/lib/tmpfiles.d/arltuner.conf" "$DESTDIR/run/arltuner"; do
        if [ -e "$keep" ]; then echo "kept $keep (--purge removes it)"; fi
    done
fi
