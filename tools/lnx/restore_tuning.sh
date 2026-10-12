#!/usr/bin/env bash
# restore_tuning.sh - after any Fedora boot: bring the CPU back to YOUR accepted tuning and PROVE it word for word.
#   bash tools/lnx/restore_tuning.sh                 # operator: type CONFIRM once (D-020, same as tools/win/restore_tuning.ps1)
#   bash tools/lnx/restore_tuning.sh --orchestrated  # orchestrator session only, after the operator's go-ahead in chat (no prompt)
#   bash tools/lnx/restore_tuning.sh --check [--orchestrated]  # read-only: vf-dump + compare, no writes
#   --from DIR     the accepted state to restore (default /etc/arltuner/accepted: CURRENT, EXPECTED-words.txt, profiles/*.toml)
#   --policy FILE  the root policy for the guarded writes (default /etc/arltuner/policy.toml)
# The accepted state is the user's (D-036): the app writes it (Profiles > Set as accepted); nothing in this repo is one. With none, this stops.
# Linux twin of tools/win/restore_tuning.ps1. Steps: health snapshot -> apply each accepted profile (guarded; the planner steps from what it reads,
# so it works after a cold boot = stock and after a warm reboot = CPUs 1-7/E kept, CPU 0 reset) -> vf-dump -> compare with EXPECTED-words.txt
# -> health snapshot. One confirmation per run (operator 2026-10-08, D-020 extended to Linux): after CONFIRM, the guard's per-command tokens are
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
# CURRENT lists the profiles in apply order (a legacy 'policy =' line is ignored: the policy is --policy / the installed one)
mapfile -t profiles < <(awk '/^profile *=/{sub(/^profile *= */,""); gsub(/"/,""); print}' "$from/CURRENT")
[ "${#profiles[@]}" -gt 0 ] || { echo "STOP: $from/CURRENT lists no profile"; exit 2; }
for p in "${profiles[@]}"; do
  printf '%s\n' "$p" | grep -Eq '^profiles/[A-Za-z0-9._-]+\.toml$' && [[ "$p" != *..* ]] && [ -f "$from/$p" ] || { echo "STOP: $from/CURRENT names an unusable profile '$p'"; exit 2; }
done
pgrep -x OCCT >/dev/null 2>&1 && pgrep -u root -x OCCT >/dev/null 2>&1 && { echo "STOP: OCCT runs as root (it writes the mailbox) - close it"; exit 2; }
mkdir -p captures/lnx

if [ "$orch" = 0 ]; then  # one confirmation for the whole run
  echo "Policy:   $policy"; printf 'Profile:  %s\n' "${profiles[@]/#/$from/}"; echo "Expected: $from/EXPECTED-words.txt"
  [ "$check" = 1 ] && echo "Mode: --check (mailbox reads only, no offset writes)" || echo "Mode: APPLY the profiles above, then read back and compare"
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
  for prof in "${profiles[@]}"; do echo "=== apply $prof"; lm "apply-$(basename "$prof" .toml)" apply "$from/$prof"; done
fi
echo "=== read-back"
lm after vf-dump --json "captures/lnx/restore-$ts-after.json"
python3 -I - "captures/lnx/restore-$ts-after.json" "$from/EXPECTED-words.txt" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
got = {}
for u in d["units"]:
    c = u["cpus"][0]
    got[c["cpu"]] = " ".join(f"{int(w, 16):X}" for w in [c["oc"]["word"]] + [p["word"] for p in c["points"]])
bad = 0
for line in open(sys.argv[2]):
    if not line.strip() or line.startswith("#"): continue
    cpu, *words = line.split(); want = " ".join(words); g = got.get(int(cpu), "<missing>")
    ok = g == want; bad += not ok
    print(f"  cpu{cpu:<3}{'OK ' if ok else 'BAD'} {g}" + ("" if ok else f"\n         want {want}"))
print("RESTORED: all cores/clusters match the accepted state." if not bad else f"STOP: {bad} core(s)/cluster(s) differ - report; cold boot = stock")
sys.exit(1 if bad else 0)
PY
bash tools/lnx/health_snapshot.sh "restore-$ts-after" --since "captures/lnx/health-restore-$ts-before.txt" | tail -3
