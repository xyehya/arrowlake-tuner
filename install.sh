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
#   /etc/arltuner/{policy.toml,helper.toml}   operator-owned limits: seeded from the checkout (policies/default.toml) ONLY when absent
#                                          (--reseed-config replaces them)
#   /etc/arltuner/accepted/                the USER's accepted state (D-036): never created, changed or removed by this script. The app writes it
#                                          (Profiles > Set as accepted); a fresh install has none ("No accepted state yet")
#   /var/lib/arltuner/audit.jsonl          0600; seeded once from the invoking user's per-user ~/.local/state/arltuner/audit.jsonl
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
    USER_AUDIT_OVERRIDE=${ARLTUNER_USER_AUDIT:-}
    PROC_ROOT=${ARLTUNER_PROC:-/proc}
else
    [ "$(id -u)" -eq 0 ] || die "must run as root (sudo bash install.sh)"
    PYTHON=/usr/bin/python3  # the interpreter named in the launcher's shebang
    USER_AUDIT_OVERRIDE=
    PROC_ROOT=/proc
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

# --- a running helper keeps lazily importing from the tree we are about to replace: refuse until it is stopped ---------------------------
helper_pids() {  # PIDs running the helper: argv[0] is the helper itself, or a wrapper (python*, pkexec, sudo, env) whose first non-option argument is it
    local c pid i base a skip
    local -a argv
    for c in "$PROC_ROOT"/[0-9]*/cmdline; do
        [ -r "$c" ] || continue
        pid=${c%/cmdline}; pid=${pid##*/}
        [ "$pid" != "$$" ] || continue
        argv=()
        while IFS= read -r -d '' a || [ -n "$a" ]; do argv+=("$a"); done < "$c" 2>/dev/null || true
        [ "${#argv[@]}" -gt 0 ] || continue
        base=${argv[0]##*/}
        case $base in
            arltuner-helper|arltuner-helper.py) echo "$pid"; continue ;;
            python|python3|python3.*|pkexec|sudo|env) ;;
            *) continue ;;   # vim/grep/less ... with the helper's name as an argument is not the helper
        esac
        skip=0
        for ((i = 1; i < ${#argv[@]}; i++)); do
            a=${argv[i]}
            if [ "$skip" = 1 ]; then skip=0; continue; fi
            case $a in
                -u|--user|-g|--group) skip=1; continue ;;   # an option value (pkexec --user root PATH) is not the program
                -*) continue ;;
            esac
            case ${a##*/} in
                arltuner-helper|arltuner-helper.py) echo "$pid"; break ;;
                python|python3|python3.*|pkexec|sudo|env) continue ;;   # a wrapper around a wrapper (e.g. env then python3 PATH): keep looking for the program
            esac
            break   # any other first non-option argument is the program; later ones are its data
        done
    done
}
RUNNING=$(helper_pids | tr '\n' ' ')
[ -z "$RUNNING" ] || die "an arltuner-helper process is running (pid ${RUNNING% }): stop the GUI / helper first, then re-run the install"

# --- sanity: sources and interpreter -------------------------------------------------------------------------------------------------
# Every directory we copy from, and every file we need, must be a real entry of THIS checkout: no symlink, and its realpath stays under $SRC.
for f in src platforms web packaging policies; do
    [ -d "$SRC/$f" ] || die "incomplete checkout: $SRC/$f is missing"
done
for f in src src/arltuner src/arltuner/__init__.py platforms web packaging policies policies/default.toml packaging/arltuner-helper packaging/selfcheck.py \
         packaging/io.github.arrowlake-tuner.helper.policy packaging/arltuner.tmpfiles.conf; do
    [ -e "$SRC/$f" ] || die "incomplete checkout: $SRC/$f is missing"
    [ ! -L "$SRC/$f" ] || die "$SRC/$f is a symlink; refusing"
    real=$(realpath -e -- "$SRC/$f") || die "cannot resolve $SRC/$f"
    case $real in "$SRC"/*) ;; *) die "$SRC/$f resolves to $real, outside the checkout; refusing" ;; esac
done
(cd / && "$PYTHON" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)') || die "$PYTHON must be Python >= 3.12 (D-024)"

# --- who is installing: the per-user audit log is read as that user, never as root -------------------------------------------------------
INVOKER=""
if [ -n "${PKEXEC_UID:-}" ]; then
    case $PKEXEC_UID in *[!0-9]*) die "PKEXEC_UID is not a number" ;; esac
    FROM_PK=$(getent passwd "$PKEXEC_UID" | cut -d: -f1 || true)
    [ -n "$FROM_PK" ] || die "PKEXEC_UID=$PKEXEC_UID is not a known user"
    INVOKER=$FROM_PK
fi
if [ -n "${SUDO_USER:-}" ]; then
    if [ -n "$INVOKER" ] && [ "$INVOKER" != "$SUDO_USER" ]; then
        die "PKEXEC_UID ($INVOKER) and SUDO_USER ($SUDO_USER) disagree; refusing to guess whose per-user audit log to seed"
    fi
    INVOKER=$SUDO_USER
fi
[ "$INVOKER" != root ] || INVOKER=""  # never root

copy_one() {  # copy_one SRC DST MODE: copy WITHOUT dereferencing, then insist the result is a regular file (a source swapped for a link is not followed)
    local from=$1 to=$2 mode=$3
    mkdir -p -- "$(dirname -- "$to")"
    rm -f -- "$to"
    cp --no-dereference --no-preserve=all -- "$from" "$to" || die "cannot copy $from"
    if [ -L "$to" ] || [ ! -f "$to" ]; then rm -f -- "$to"; die "$from changed while it was being copied (not a regular file); refusing"; fi
    chmod "$mode" "$to"
    if [ "$(id -u)" -eq 0 ]; then chown root:root "$to"; fi
}

copy_files() {  # copy_files SRCDIR DSTDIR [find-args...]: regular files only, never symlinks, no __pycache__, mode 0644
    local from=$1 to=$2 rel
    shift 2
    (cd "$from" && find . -name __pycache__ -prune -o -type f "$@" -print0) | while IFS= read -r -d '' rel; do
        rel=${rel#./}
        copy_one "$from/$rel" "$to/$rel" 0644
    done
}

swap_dir() {  # swap_dir NEW FINAL: FINAL is never missing afterwards - the old tree moves aside, the new one moves in, a failure moves the old one back
    local new=$1 final=$2 old=$2.old.$$
    if [ -e "$final" ] || [ -L "$final" ]; then
        rm -rf -- "$old"
        mv -T -- "$final" "$old" || die "cannot move $final aside; nothing was changed"
        if ! mv -T -- "$new" "$final"; then
            mv -T -- "$old" "$final" || echo "CRITICAL: could not restore $final from $old" >&2
            die "could not move the new tree to $final; rolled back to the old one"
        fi
        rm -rf -- "$old"
    else
        mv -T -- "$new" "$final" || die "could not move the new tree to $final"
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
trap 'rm -rf "$NEW" "${WEB}.new.$$" "$ETC/.tmp.$$" "$STATE/.audit.new.$$"' EXIT
rm -rf "$NEW"
install -d -m 0755 ${OWN[@]+"${OWN[@]}"} "$NEW" "$NEW/arltuner" "$NEW/platforms"
copy_files "$SRC/src/arltuner" "$NEW/arltuner" -name '*.py'
copy_files "$SRC/platforms" "$NEW/platforms" -name '*.toml'
copy_one "$SRC/packaging/arltuner-helper" "$NEW/arltuner-helper" 0755
copy_one "$SRC/packaging/selfcheck.py" "$NEW/arltuner-selfcheck" 0755
# From here on only the installed copy is executed (isolated mode, cwd /): a planted compileall.py / sitecustomize in the checkout cannot run as root.
(cd / && "$PYTHON" -I -m compileall -q -f -d "$FINAL_PKG" "$NEW/arltuner") || die "compileall failed"
normalize "$NEW"
chmod 0755 "$NEW/arltuner-helper" "$NEW/arltuner-selfcheck"
swap_dir "$NEW" "$LIB"

# --- 2. static web files ------------------------------------------------------------------------------------------------------------
mkdir -p "$DESTDIR/usr/share"
install -d -m 0755 ${OWN[@]+"${OWN[@]}"} "$DESTDIR/usr/share/arltuner"
rm -rf "$WEB.new.$$"
install -d -m 0755 ${OWN[@]+"${OWN[@]}"} "$WEB.new.$$"
copy_files "$SRC/web" "$WEB.new.$$" ! -name '.*'
normalize "$WEB.new.$$"
swap_dir "$WEB.new.$$" "$WEB"

# --- 3. polkit action + tmpfiles.d --------------------------------------------------------------------------------------------------
copy_one "$SRC/packaging/io.github.arrowlake-tuner.helper.policy" "$POLKIT" 0644
copy_one "$SRC/packaging/arltuner.tmpfiles.conf" "$TMPF" 0644
if [ -z "$DESTDIR" ]; then
    if command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create "$TMPF" || die "systemd-tmpfiles --create failed"
    else
        install -d -m 0755 -o root -g root /run/arltuner
    fi
fi

# --- 4. /etc/arltuner: seeded from the repo only when absent ------------------------------------------------------------------------
install -d -m 0755 ${OWN[@]+"${OWN[@]}"} "$ETC"

# D-032 (operator yes, 2026-10-11) / D-036: the root policy that /etc/arltuner/policy.toml is seeded from is the generic advanced-user policy
# (offsets +-200 mV; ratio, V-target ceilings and the CPU sets are "auto": derived from the CPU in the session). No machine-specific numbers.
POLICY_SRC=policies/default.toml
[ -f "$SRC/$POLICY_SRC" ] && [ ! -L "$SRC/$POLICY_SRC" ] || die "incomplete checkout: $SRC/$POLICY_SRC is not a regular file"

if [ -e "$ETC/policy.toml" ] && [ "$RESEED" -eq 0 ]; then
    if cmp -s "$SRC/$POLICY_SRC" "$ETC/policy.toml"; then
        echo "policy.toml: unchanged"
    else
        echo "policy.toml: KEPT (it differs from $POLICY_SRC; operator-owned limits are never overwritten; --reseed-config replaces it)"
    fi
else
    copy_one "$SRC/$POLICY_SRC" "$ETC/policy.toml" 0644
    echo "policy.toml: installed from $POLICY_SRC"
fi

if [ ! -e "$ETC/helper.toml" ] || [ "$RESEED" -eq 1 ]; then
    printf '%s\n' \
        '# arltuner root helper settings (read as root, trust-checked on every vf.dump). Only this key exists.' \
        '# The first MSR write of a boot sets kernel taint bit 2 (CPU_OUT_OF_SPEC): expected [MEASURED lnx 2026-10-07, docs/knowledge/linux-path.md].' \
        'allow_taint_bits = [2]' > "$ETC/.tmp.$$"
    copy_one "$ETC/.tmp.$$" "$ETC/helper.toml" 0644
    rm -f "$ETC/.tmp.$$"
    echo "helper.toml: installed (allow_taint_bits = [2])"
else
    echo "helper.toml: kept"
fi

# The accepted state (/etc/arltuner/accepted) is user data (D-036): it is not seeded, replaced or removed here. An existing one (this machine's earlier
# installs, or one saved in the app) is left exactly as it is; a fresh install has none until the user saves a profile as accepted.
if [ -d "$ETC/accepted" ]; then echo "accepted/: kept (the user's accepted state is never touched)"; else echo "accepted/: none yet (created by Profiles > Set as accepted)"; fi

# --- 5. /var/lib/arltuner: audit log, seeded once from the per-user dev log (D-030) ------------------------------------------------
mkdir -p "$DESTDIR/var/lib"
install -d -m 0755 ${OWN[@]+"${OWN[@]}"} "$STATE"
install -d -m 0700 ${OWN[@]+"${OWN[@]}"} "$STATE/curves"  # G-111: measured V/F curves (root-only, kept across upgrades); the helper refuses anything looser
install -d -m 0700 ${OWN[@]+"${OWN[@]}"} "$STATE/stress"  # G-116: stress run results (root-only, kept across upgrades; the helper refuses anything looser)
install -d -m 0700 ${OWN[@]+"${OWN[@]}"} "$STATE/profiles"  # G-112: saved tuning intents (root-only, kept across upgrades; never overwritten by the helper)
install -d -m 0700 ${OWN[@]+"${OWN[@]}"} "$STATE/state"  # G-123: last-read.json (this boot's tune.read, root-only; stale boots are ignored by the helper)

as_user() {  # run a command as the invoking user (root only reads the per-user log THROUGH that user's permissions)
    if [ "$(id -u)" -eq 0 ]; then runuser -u "$INVOKER" -- "$@"; else "$@"; fi
}

user_audit_source() {  # prints the per-user log path of the invoking user, or nothing
    local home=""
    if [ -n "$USER_AUDIT_OVERRIDE" ]; then printf '%s\n' "$USER_AUDIT_OVERRIDE"; return 0; fi
    if [ -n "$INVOKER" ]; then
        home=$(getent passwd "$INVOKER" | cut -d: -f6 || true)
    elif [ "$(id -u)" -ne 0 ]; then
        home=$HOME  # a non-root DESTDIR test run
    fi
    [ -n "$home" ] || return 0
    printf '%s/.local/state/arltuner/audit.jsonl\n' "$home"
}

if [ -e "$AUDIT" ] || [ -L "$AUDIT" ]; then
    echo "audit.jsonl: kept (never overwritten)"
else
    USER_LOG=""
    if [ "$(id -u)" -ne 0 ] || [ -n "$INVOKER" ]; then USER_LOG=$(user_audit_source || true); fi
    TMPAUDIT=$STATE/.audit.new.$$
    ( umask 077; : > "$TMPAUDIT" )
    if [ "$(id -u)" -eq 0 ] && [ -n "$USER_LOG" ]; then
        command -v runuser >/dev/null 2>&1 || { rm -f "$TMPAUDIT"; die "runuser is needed to read $USER_LOG safely; install util-linux"; }
    fi
    if [ -n "$USER_LOG" ] && as_user test -f "$USER_LOG" && as_user test ! -L "$USER_LOG"; then
        # Read AS THE USER (a symlinked parent directory must not let root copy some other file into a log the GUI can tail), at most 64 MiB + 1 byte.
        as_user head -c 67108865 -- "$USER_LOG" > "$TMPAUDIT" || { rm -f "$TMPAUDIT"; die "could not read the per-user audit log $USER_LOG${INVOKER:+ as $INVOKER}"; }
        size=$(stat -c %s "$TMPAUDIT")
        [ "$size" -le 67108864 ] || { rm -f "$TMPAUDIT"; die "per-user audit log $USER_LOG is larger than 64 MiB; seed it by hand (the per-boot count must survive)"; }
        echo "audit.jsonl: seeded from $USER_LOG ($size bytes)"
    else
        echo "audit.jsonl: created empty (no per-user log found${USER_LOG:+ at $USER_LOG})"
        if [ -z "$USER_LOG" ] && [ -z "$DESTDIR" ]; then
            echo "WARNING: no invoking user (PKEXEC_UID / SUDO_USER, not root) found, so no per-user audit log was seeded. If ~/.local/state/arltuner/audit.jsonl exists" >&2
            echo "         and this boot already spent mailbox operations, stop here: the per-boot operation count (an indicator, default cap 100000) restarts from zero (D-030)." >&2
        fi
    fi
    chmod 0600 "$TMPAUDIT"
    if [ "$(id -u)" -eq 0 ]; then chown root:root "$TMPAUDIT"; fi
    mv -T -- "$TMPAUDIT" "$AUDIT"
fi

# --- 6. self-check: the installed tree, as the helper will see it --------------------------------------------------------------------
if [ -z "$DESTDIR" ]; then
    (cd / && "$PYTHON" -I /usr/lib/arltuner/arltuner-selfcheck) || die "post-install self-check FAILED: the helper would refuse to start. Run uninstall.sh or repair the files named above."
else
    echo "self-check: skipped (DESTDIR install: ownership cannot be verified)"
fi
echo "arltuner installed. Start the GUI helper with:  pkexec /usr/lib/arltuner/arltuner-helper --device  (the GUI launcher does this)"
