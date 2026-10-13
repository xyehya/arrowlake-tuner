#!/usr/bin/env bash
# restore_tuning.sh - after any Fedora boot: bring the CPU back to YOUR accepted tuning and PROVE it word for word.
#   bash tools/lnx/restore_tuning.sh                 # operator: type CONFIRM once (D-020, same as tools/win/restore_tuning.ps1)
#   bash tools/lnx/restore_tuning.sh --orchestrated  # orchestrator session only, after the operator's go-ahead in chat (no prompt)
#   bash tools/lnx/restore_tuning.sh --check [--orchestrated]  # read-only: vf-dump + compare, no writes
#   --from DIR     the accepted state to restore (default /etc/arltuner/accepted: CURRENT naming ONE profile, EXPECTED-words.txt, profiles/<name>.toml)
#   --policy FILE  the root policy for the guarded writes (default /etc/arltuner/policy.toml)
# The accepted state is the user's (D-036): the app writes it (Profiles > Set as accepted); nothing in this repo is one. With none, this stops.
# Linux twin of tools/win/restore_tuning.ps1. Steps: health snapshot -> apply THE accepted profile in one guarded run (G-148, D-044: one complete profile;
# the planner reads every word and writes only the ones that differ, splitting a change larger than the policy's step limits into rounds within the run,
# so it works after a cold boot = stock and after a warm reboot) -> vf-dump -> compare with EXPECTED-words.txt -> health snapshot.
# An accepted state that is still a chain (CURRENT with several 'profile =' lines, written before G-148) is refused: run 'accepted-migrate' once.
# G-147: a residual OC point (an OC ratio the accepted state lacks; only a cold boot clears it) is rewritten so it runs the accepted voltage and reported
# EQUIVALENT ("residual OC point (cleared by a cold boot); voltages as accepted"), which is a success; only a real difference is BAD. One confirmation per run (operator 2026-10-08, D-020 extended to Linux): after CONFIRM, the guard's per-command tokens are
# answered by tools/lnx/orchestrator_confirm.py; every guard check (policy limits, per-write read-back, word compare) is unchanged.
# Stops at the first failure. Revert = cold boot. The first MSR write of a boot sets taint bit 2 (CPU_OUT_OF_SPEC): expected.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$repo"
orch=0; check=0; from=/etc/arltuner/accepted; policy=/etc/arltuner/policy.toml
while [ $# -gt 0 ]; do
  case "$1" in
    --orchestrated) orch=1;;
    --check) check=1;;
    --from) from="${2:?--from needs a directory}"; shift;;
    --policy) policy="${2:?--policy needs a file}"; shift;;
    *) echo "unknown arg $1"; exit 2;;
  esac
  shift
done
ts="$(date +%Y%m%dT%H%M%S)"
[ -f "$from/CURRENT" ] && [ -f "$from/EXPECTED-words.txt" ] \
  || { echo "STOP: no accepted state in $from. Save a profile as accepted in the app first (Profiles > Set as accepted)."; exit 2; }
[ -f "$policy" ] || { echo "STOP: policy $policy not found (run install.sh, or pass --policy FILE)"; exit 2; }
# CURRENT names the one accepted profile (a legacy 'policy =' line is ignored: the policy is --policy / the installed one)
mapfile -t profiles < <(awk '/^profile *=/{sub(/^profile *= */,""); gsub(/"/,""); print}' "$from/CURRENT")
[ "${#profiles[@]}" -gt 0 ] || { echo "STOP: $from/CURRENT lists no profile"; exit 2; }
if [ "${#profiles[@]}" -gt 1 ]; then  # G-148 (D-044): never replay a chain
  echo "STOP: $from/CURRENT lists ${#profiles[@]} profiles (an accepted chain from before G-148). The accepted state is ONE complete profile now (D-044)."
  echo "      Migrate it once (it builds the profile from EXPECTED-words.txt, never from a read, and keeps a backup):"
  echo "      sudo python3 -I tools/arltuner.py accepted-migrate --dir $from --dry-run   # look first, then run it without --dry-run"
  exit 2
fi
prof="${profiles[0]}"
printf '%s\n' "$prof" | grep -Eq '^profiles/[A-Za-z0-9._-]+\.toml$' && [[ "$prof" != *..* ]] && [ -f "$from/$prof" ] || { echo "STOP: $from/CURRENT names an unusable profile '$prof'"; exit 2; }
pgrep -x OCCT >/dev/null 2>&1 && pgrep -u root -x OCCT >/dev/null 2>&1 && { echo "STOP: OCCT runs as root (it writes the mailbox) - close it"; exit 2; }
mkdir -p captures/lnx

if [ "$orch" = 0 ]; then  # one confirmation for the whole run
  echo "Policy:   $policy"; echo "Profile:  $from/$prof"; echo "Expected: $from/EXPECTED-words.txt"
  [ "$check" = 1 ] && echo "Mode: --check (mailbox reads only, no offset writes)" || echo "Mode: APPLY the profile above in one run (only the words that differ), then read back and compare"
  read -r -p "Type CONFIRM to proceed: " ans
  [ "$ans" = "CONFIRM" ] || { echo "not confirmed - nothing was sent"; exit 1; }
  orch=1; sudo -v
fi

lm() {  # one guarded arltuner command (run confirmed above / in chat); full output in the capture file
  local tag="$1"; shift; local f="captures/lnx/restore-$ts-$tag.txt"
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" HOME="$HOME" python3 -I tools/lnx/orchestrator_confirm.py python3 -I tools/arltuner.py "$@" --device --policy "$policy" --allow-taint-bit 2 > "$f" 2>&1 \
    || { echo "STOP: $tag failed (see $f)"; tail -5 "$f"; exit 1; }
  grep -E '^Result|REFUSED|refused' "$f" | sed 's/^/  /' || true
}

bash tools/lnx/health_snapshot.sh "restore-$ts-before" > "captures/lnx/health-restore-$ts-before.txt" 2>&1
if [ "$check" = 0 ]; then
  # G-147: the apply also looks for a residual OC point (an OC ratio the accepted profile does not have; only a cold boot clears it) and rewrites it with a V target below the
  # accepted top fused point, so the unit runs the accepted voltage. G-148: one run, only the words that differ, rounds within the step limits (judged before the first write).
  # apply --device also lets scaling_max_freq follow a cpuinfo_max_freq that a 0x1D moved (never raising a cap you lowered).
  echo "=== apply $prof"; lm "apply-$(basename "$prof" .toml)" apply "$from/$prof" --neutralize-residual
fi
echo "=== read-back"
lm after vf-dump --json "captures/lnx/restore-$ts-after.json"
# the verdict is the CLI's compare-accepted (shared with the Windows script): identical words are OK; a residual OC point that runs the accepted voltages (or an OC word that is the
# same state, e.g. 0x5B439 vs the cold 0x39) is EQUIVALENT and counts as success; anything else is BAD. fingerprint.json (written by 'Set as accepted') carries the fused words that
# tell a residual OC point from an OC point the accepted state has; without it only identical words match.
fp=(); [ -f "$from/fingerprint.json" ] && fp=(--fingerprint "$from/fingerprint.json")
python3 -I tools/arltuner.py compare-accepted --readback "captures/lnx/restore-$ts-after.json" --expected "$from/EXPECTED-words.txt" "${fp[@]}"
bash tools/lnx/health_snapshot.sh "restore-$ts-after" --since "captures/lnx/health-restore-$ts-before.txt" | tail -3
