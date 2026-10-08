#!/usr/bin/env bash
# restore_tuning.sh - after any Fedora boot: bring the CPU back to the accepted tuning (profiles/CURRENT) and PROVE it word for word.
#   bash tools/lnx/restore_tuning.sh                 # operator: type CONFIRM once (D-020, same as tools/win/restore_tuning.ps1)
#   bash tools/lnx/restore_tuning.sh --orchestrated  # orchestrator session only, after the operator's go-ahead in chat (no prompt)
#   bash tools/lnx/restore_tuning.sh --check [--orchestrated]  # read-only: vf-dump + compare, no writes
# Linux twin of tools/win/restore_tuning.ps1. Steps: health snapshot -> apply each CURRENT profile (guarded; the planner steps from what it reads,
# so it works after a cold boot = stock and after a warm reboot = CPUs 1-7/E kept, CPU 0 reset) -> vf-dump -> compare with profiles/EXPECTED-words.txt
# -> health snapshot. One confirmation per run (operator 2026-10-08, D-020 extended to Linux): after CONFIRM, the guard's per-command tokens are
# answered by tools/lnx/orchestrator_confirm.py; every guard check (policy limits, per-write read-back, word compare) is unchanged.
# Stops at the first failure. Revert = cold boot. The first MSR write of a boot sets taint bit 2 (CPU_OUT_OF_SPEC): expected.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$repo"
orch=0; check=0; for a in "$@"; do case "$a" in --orchestrated) orch=1;; --check) check=1;; *) echo "unknown arg $a"; exit 2;; esac; done; ts="$(date +%Y%m%dT%H%M%S)"
audit="--audit $HOME/.local/state/linuxmon/audit.jsonl"
policy="$(awk -F= '/^policy *=/{gsub(/[ "]/,"",$2); print $2}' profiles/CURRENT)"
mapfile -t profiles < <(awk '/^profile *=/{sub(/^profile *= */,""); gsub(/"/,""); print}' profiles/CURRENT)
[ -n "$policy" ] && [ "${#profiles[@]}" -gt 0 ] || { echo "STOP: profiles/CURRENT has no policy/profile lines"; exit 2; }
pgrep -x OCCT >/dev/null 2>&1 && pgrep -u root -x OCCT >/dev/null 2>&1 && { echo "STOP: OCCT runs as root (it writes the mailbox) - close it"; exit 2; }

if [ "$orch" = 0 ]; then  # one confirmation for the whole run
  echo "Policy:   $policy"; printf 'Profile:  %s\n' "${profiles[@]}"; echo "Expected: profiles/EXPECTED-words.txt"
  [ "$check" = 1 ] && echo "Mode: --check (mailbox reads only, no offset writes)" || echo "Mode: APPLY the profiles above, then read back and compare"
  read -r -p "Type CONFIRM to proceed: " ans
  [ "$ans" = "CONFIRM" ] || { echo "not confirmed - nothing was sent"; exit 1; }
  orch=1; sudo -v
fi

lm() {  # one guarded arltuner command (run confirmed above / in chat); full output in the capture file
  local tag="$1"; shift; local f="captures/lnx/restore-$ts-$tag.txt"
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" python3 tools/lnx/orchestrator_confirm.py python3 tools/arltuner.py "$@" --device --policy "$policy" --allow-taint-bit 2 $audit > "$f" 2>&1 \
    || { echo "STOP: $tag failed (see $f)"; tail -5 "$f"; exit 1; }
  grep -E '^Result|REFUSED|refused' "$f" | sed 's/^/  /' || true
}

bash tools/lnx/health_snapshot.sh "restore-$ts-before" > "captures/lnx/health-restore-$ts-before.txt" 2>&1
if [ "$check" = 0 ]; then
  for prof in "${profiles[@]}"; do echo "=== apply $prof"; lm "apply-$(basename "$prof" .toml)" apply "$prof"; done
fi
echo "=== read-back"
lm after vf-dump --json "captures/lnx/restore-$ts-after.json"
python3 - "captures/lnx/restore-$ts-after.json" profiles/EXPECTED-words.txt <<'PY'
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
