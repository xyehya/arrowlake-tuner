#!/bin/bash
# arrowlake-tuner system install (G-106; SPEC 4.4 / 11, D-024, D-030). Run as root:   sudo bash install.sh [--reseed-config]
#
# Layout (root:root; dirs 0755, files 0644 unless noted):
#   /usr/lib/arltuner/arltuner/            the Python package, precompiled here (root-owned __pycache__)
#   /usr/lib/arltuner/platforms/*.toml     platform files
#   /usr/lib/arltuner/arltuner-helper      0755 launcher: the polkit exec.path;  pkexec /usr/lib/arltuner/arltuner-helper --device
#   /usr/lib/arltuner/arltuner-selfcheck   0755 post-install trust check
#   /usr/share/arltuner/web/               static GUI files
#   /usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy      auth_admin everywhere, no _keep
#   /usr/lib/tmpfiles.d/arltuner.conf      /run/arltuner 0755 root (mailbox lock directory)
#   /etc/arltuner/{policy.toml,helper.toml}   operator-owned limits: seeded from the checkout ONLY when absent (--reseed-config replaces them)
#   /etc/arltuner/accepted/                CURRENT + EXPECTED-words.txt + the profiles it lists: replaced from the checkout on EVERY run (the repo is
#                                          the acceptance record, and the dashboard compares live words against this copy)
#   /var/lib/arltuner/audit.jsonl          0600; seeded once from the invoking user's legacy ~/.local/state/linuxmon/audit.jsonl
#
# The checkout is only ever COPIED. After the copy, the only Python that runs (compileall, the self-check) is the installed, root-owned tree, in
# isolated mode, with cwd /. Idempotent: a second run replaces code/web/polkit/tmpfiles and leaves policy.toml, helper.toml and the audit log alone.
# DESTDIR=/some/dir installs into that tree instead (tests, packaging): no chown unless root, no systemd-tmpfiles, no live self-check.
set -euo pipefail
umask 022
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

die() { echo "install.sh: $*" >&2; exit 1; }

RESEED=0
for arg in "$@"; do
    case $arg in
        --reseed-config) RESEED=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $arg (usage: install.sh [--reseed-config])" ;;
    esac
done

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
DESTDIR=${DESTDIR:-}
DESTDIR=${DESTDIR%/}
if [ -n "$DESTDIR" ]; then
    case $DESTDIR in /*) ;; *) die "DESTDIR must be an absolute path" ;; esac
    PYTHON=${ARLTUNER_PYTHON:-/usr/bin/python3}  # test hooks are honoured only for a DESTDIR install, never for a real one
    LEGACY_AUDIT_OVERRIDE=${ARLTUNER_LEGACY_AUDIT:-}
else
    [ "$(id -u)" -eq 0 ] || die "must run as root (sudo bash install.sh)"
    PYTHON=/usr/bin/python3  # the interpreter named in the launcher's shebang
    LEGACY_AUDIT_OVERRIDE=
fi

LIB=$DESTDIR/usr/lib/arltuner
WEB=$DESTDIR/usr/share/arltuner/web
POLKIT=$DESTDIR/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy
TMPF=$DESTDIR/usr/lib/tmpfiles.d/arltuner.conf
ETC=$DESTDIR/etc/arltuner
STATE=$DESTDIR/var/lib/arltuner
AUDIT=$STATE/audit.jsonl
FINAL_PKG=/usr/lib/arltuner/arltuner  # recorded in the .pyc files (compileall -d): the path the code will run from

if [ "$(id -u)" -eq 0 ]; then OWN=(-o root -g root); else OWN=(); fi

# --- sanity: sources and interpreter -------------------------------------------------------------------------------------------------
for f in src/arltuner/__init__.py platforms packaging/arltuner-helper packaging/selfcheck.py packaging/io.github.arrowlake-tuner.helper.policy \
         packaging/arltuner.tmpfiles.conf profiles/CURRENT profiles/EXPECTED-words.txt web; do
    [ -e "$SRC/$f" ] || die "incomplete checkout: $SRC/$f is missing"
    [ ! -L "$SRC/$f" ] || die "$SRC/$f is a symlink; refusing"
done
(cd / && "$PYTHON" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)') || die "$PYTHON must be Python >= 3.12 (D-024)"

copy_files() {  # copy_files SRCDIR DSTDIR [find-args...]: regular files only, never symlinks, no __pycache__, mode 0644
    local from=$1 to=$2 rel
    shift 2
    (cd "$from" && find . -name __pycache__ -prune -o -type f "$@" -print0) | while IFS= read -r -d '' rel; do
        rel=${rel#./}
        install -D -m 0644 "${OWN[@]}" "$from/$rel" "$to/$rel"
    done
}

swap_dir() {  # swap_dir NEW FINAL: replace FINAL by NEW (old tree removed afterwards)
    local new=$1 final=$2
    if [ -e "$final" ] || [ -L "$final" ]; then
        rm -rf "$final.old.$$"
        mv "$final" "$final.old.$$"
        mv "$new" "$final"
        rm -rf "$final.old.$$"
    else
        mv "$new" "$final"
    fi
}

normalize() {  # normalize DIR: dirs 0755, files 0644, root-owned when we can be
    find "$1" -type d -exec chmod 0755 {} +
    find "$1" -type f -exec chmod 0644 {} +
    if [ "$(id -u)" -eq 0 ]; then chown -R root:root "$1"; fi
}

# --- 1. code: copy, precompile in place, then swap in -----------------------------------------------------------------------------------
mkdir -p "$DESTDIR/usr/lib/tmpfiles.d" "$DESTDIR/usr/share/polkit-1/actions"  # system directories: created if missing, never re-moded
NEW=$DESTDIR/usr/lib/.arltuner.new.$$
trap 'rm -rf "$NEW" "${WEB}.new.$$" "$ETC/.accepted.new.$$" "$ETC/.tmp.$$" "$STATE/.audit.new.$$"' EXIT
rm -rf "$NEW"
install -d -m 0755 "${OWN[@]}" "$NEW" "$NEW/arltuner" "$NEW/platforms"
copy_files "$SRC/src/arltuner" "$NEW/arltuner" -name '*.py'
copy_files "$SRC/platforms" "$NEW/platforms" -name '*.toml'
install -m 0755 "${OWN[@]}" "$SRC/packaging/arltuner-helper" "$NEW/arltuner-helper"
install -m 0755 "${OWN[@]}" "$SRC/packaging/selfcheck.py" "$NEW/arltuner-selfcheck"
# From here on only the installed copy is executed (isolated mode, cwd /): a planted compileall.py / sitecustomize in the checkout cannot run as root.
(cd / && "$PYTHON" -I -m compileall -q -f -d "$FINAL_PKG" "$NEW/arltuner") || die "compileall failed"
normalize "$NEW"
chmod 0755 "$NEW/arltuner-helper" "$NEW/arltuner-selfcheck"
swap_dir "$NEW" "$LIB"

# --- 2. static web files ------------------------------------------------------------------------------------------------------------
mkdir -p "$DESTDIR/usr/share"
install -d -m 0755 "${OWN[@]}" "$DESTDIR/usr/share/arltuner"
rm -rf "$WEB.new.$$"
install -d -m 0755 "${OWN[@]}" "$WEB.new.$$"
copy_files "$SRC/web" "$WEB.new.$$" ! -name '.*'
normalize "$WEB.new.$$"
swap_dir "$WEB.new.$$" "$WEB"

# --- 3. polkit action + tmpfiles.d --------------------------------------------------------------------------------------------------
install -m 0644 "${OWN[@]}" "$SRC/packaging/io.github.arrowlake-tuner.helper.policy" "$POLKIT"
install -m 0644 "${OWN[@]}" "$SRC/packaging/arltuner.tmpfiles.conf" "$TMPF"
if [ -z "$DESTDIR" ]; then
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create "$TMPF" || die "systemd-tmpfiles --create failed"
    else
        install -d -m 0755 -o root -g root /run/arltuner
    fi
fi

# --- 4. /etc/arltuner: seeded from the repo only when absent ------------------------------------------------------------------------
install -d -m 0755 "${OWN[@]}" "$ETC"

current_values() {  # current_values KEY: the values of "KEY = value" lines of profiles/CURRENT, each validated
    local key=$1 v
    sed -n "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*\\(.*[^[:space:]]\\)[[:space:]]*\$/\\1/p" "$SRC/profiles/CURRENT" | while IFS= read -r v; do
        printf '%s\n' "$v" | grep -Eq '^(profiles|policies)/[A-Za-z0-9._-]+$' || die "profiles/CURRENT: unacceptable path '$v'"
        case $v in *..*) die "profiles/CURRENT: unacceptable path '$v'" ;; esac
        [ -f "$SRC/$v" ] && [ ! -L "$SRC/$v" ] || die "profiles/CURRENT names $v, which is not a regular file"
        printf '%s\n' "$v"
    done
}

POLICY_SRC=$(current_values policy | head -n 1)
[ -n "$POLICY_SRC" ] || die "profiles/CURRENT has no 'policy =' line"

if [ -e "$ETC/policy.toml" ] && [ "$RESEED" -eq 0 ]; then
    if cmp -s "$SRC/$POLICY_SRC" "$ETC/policy.toml"; then
        echo "policy.toml: unchanged"
    else
        echo "policy.toml: KEPT (it differs from $POLICY_SRC; operator-owned limits are never overwritten; --reseed-config replaces it)"
    fi
else
    install -m 0644 "${OWN[@]}" "$SRC/$POLICY_SRC" "$ETC/policy.toml"
    echo "policy.toml: installed from $POLICY_SRC"
fi

if [ ! -e "$ETC/helper.toml" ] || [ "$RESEED" -eq 1 ]; then
    printf '%s\n' \
        '# arltuner root helper settings (read as root, trust-checked on every vf.dump). Only this key exists.' \
        '# The first MSR write of a boot sets kernel taint bit 2 (CPU_OUT_OF_SPEC): expected [MEASURED lnx 2026-10-07, docs/knowledge/linux-path.md].' \
        'allow_taint_bits = [2]' > "$ETC/.tmp.$$"
    install -m 0644 "${OWN[@]}" "$ETC/.tmp.$$" "$ETC/helper.toml"
    rm -f "$ETC/.tmp.$$"
    echo "helper.toml: installed (allow_taint_bits = [2])"
else
    echo "helper.toml: kept"
fi

if true; then  # accepted state follows the repo on every run (not coupled to --reseed-config)
    ACC=$ETC/.accepted.new.$$
    rm -rf "$ACC"
    install -d -m 0755 "${OWN[@]}" "$ACC" "$ACC/profiles"
    install -m 0644 "${OWN[@]}" "$SRC/profiles/CURRENT" "$ACC/CURRENT"
    install -m 0644 "${OWN[@]}" "$SRC/profiles/EXPECTED-words.txt" "$ACC/EXPECTED-words.txt"
    PROFILES=$(current_values profile)  # a captured value, so a refused path aborts the install (a process substitution would not)
    while IFS= read -r prof; do
        [ -n "$prof" ] || continue
        install -D -m 0644 "${OWN[@]}" "$SRC/$prof" "$ACC/$prof"
    done <<< "$PROFILES"
    swap_dir "$ACC" "$ETC/accepted"
    echo "accepted/: replaced from profiles/CURRENT"
fi

# --- 5. /var/lib/arltuner: audit log, seeded once from the legacy per-user log (D-030) ------------------------------------------------
mkdir -p "$DESTDIR/var/lib"
install -d -m 0755 "${OWN[@]}" "$STATE"

legacy_audit_source() {  # prints the legacy log path of the invoking user, or nothing
    if [ -n "$LEGACY_AUDIT_OVERRIDE" ]; then printf '%s\n' "$LEGACY_AUDIT_OVERRIDE"; return 0; fi
    local user="" uid="" home=""
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        user=$SUDO_USER
    elif [ -n "${PKEXEC_UID:-}" ]; then
        uid=$PKEXEC_UID
        user=$(getent passwd "$uid" | cut -d: -f1 || true)
    fi
    if [ -z "$user" ] && [ "$(id -u)" -ne 0 ]; then home=$HOME; else
        [ -n "$user" ] || return 0
        home=$(getent passwd "$user" | cut -d: -f6 || true)
    fi
    [ -n "$home" ] || return 0
    printf '%s/.local/state/linuxmon/audit.jsonl\n' "$home"
}

if [ -e "$AUDIT" ] || [ -L "$AUDIT" ]; then
    echo "audit.jsonl: kept (never overwritten)"
else
    LEGACY=$(legacy_audit_source || true)
    TMPAUDIT=$STATE/.audit.new.$$
    ( umask 077; : > "$TMPAUDIT" )
    if [ -n "$LEGACY" ] && [ -f "$LEGACY" ] && [ ! -L "$LEGACY" ]; then
        # Read it AS THE USER (a symlinked parent directory must not let root copy some other file into a log the GUI can tail) and cap its size.
        size=$(stat -c %s "$LEGACY")
        [ "$size" -le 67108864 ] || { rm -f "$TMPAUDIT"; die "legacy audit log $LEGACY is larger than 64 MiB; seed it by hand (the per-boot count must survive)"; }
        if [ -n "$LEGACY_AUDIT_OVERRIDE" ]; then
            cat -- "$LEGACY" > "$TMPAUDIT"  # test hook (DESTDIR installs only)
        elif [ "$(id -u)" -eq 0 ]; then
            LEGACY_USER=${SUDO_USER:-}
            [ -n "$LEGACY_USER" ] || LEGACY_USER=$(getent passwd "${PKEXEC_UID:-}" | cut -d: -f1)
            command -v runuser >/dev/null 2>&1 || { rm -f "$TMPAUDIT"; die "runuser is needed to read $LEGACY safely; install util-linux"; }
            runuser -u "$LEGACY_USER" -- cat -- "$LEGACY" > "$TMPAUDIT" || { rm -f "$TMPAUDIT"; die "could not read the legacy audit log $LEGACY as $LEGACY_USER"; }
        else
            cat -- "$LEGACY" > "$TMPAUDIT"
        fi
        echo "audit.jsonl: seeded from $LEGACY ($size bytes)"
    else
        echo "audit.jsonl: created empty (no legacy log found${LEGACY:+ at $LEGACY})"
        if [ -z "$LEGACY" ] && [ -z "$DESTDIR" ]; then
            echo "WARNING: no invoking user (SUDO_USER / PKEXEC_UID) found, so no legacy audit log was seeded. If ~/.local/state/linuxmon/audit.jsonl exists" >&2
            echo "         and this boot already spent mailbox operations, stop here: the per-boot count (cap 600) restarts from zero (D-030)." >&2
        fi
    fi
    chmod 0600 "$TMPAUDIT"
    if [ "$(id -u)" -eq 0 ]; then chown root:root "$TMPAUDIT"; fi
    mv "$TMPAUDIT" "$AUDIT"
fi

# --- 6. self-check: the installed tree, as the helper will see it --------------------------------------------------------------------
if [ -z "$DESTDIR" ]; then
    (cd / && "$PYTHON" -I /usr/lib/arltuner/arltuner-selfcheck) || die "post-install self-check FAILED: the helper would refuse to start. Run uninstall.sh or repair the files named above."
else
    echo "self-check: skipped (DESTDIR install: ownership cannot be verified)"
fi
echo "arltuner installed. Start the GUI helper with:  pkexec /usr/lib/arltuner/arltuner-helper --device  (the GUI launcher does this)"
