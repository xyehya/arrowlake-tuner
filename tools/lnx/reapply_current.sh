#!/usr/bin/env bash
# reapply_current.sh - re-apply YOUR accepted tuning state after a boot (a cold boot clears every point offset).
#   bash tools/lnx/reapply_current.sh          # interactive: you type CONFIRM <token> once per profile
#   bash tools/lnx/reapply_current.sh --check  # read-only: dump the point words (vf-dump, no writes) and compare with the expected state
#   --from DIR     the accepted state (default /etc/arltuner/accepted: CURRENT, EXPECTED-words.txt, profiles/*.toml; the app writes it)
#   --policy FILE  the root policy for the guarded writes (default /etc/arltuner/policy.toml)
# Runs each profile listed in the accepted CURRENT through the guard (`arltuner apply --device`), in order. With no accepted state this stops:
# save a profile as accepted in the app first (D-036). Safe on a warm boot too: the planner reads every point first and only writes what differs.
# The first MSR write of a boot sets kernel taint bit 2 (CPU_OUT_OF_SPEC) - expected, allowed via --allow-taint-bit 2.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo"
check=0; from=/etc/arltuner/accepted; policy=/etc/arltuner/policy.toml
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=1;;
    --from) from="${2:?--from needs a directory}"; shift;;
    --policy) policy="${2:?--policy needs a file}"; shift;;
    *) echo "unknown arg $1"; exit 2;;
  esac
  shift
done
[ -f "$from/CURRENT" ] || { echo "STOP: no accepted state in $from. Save a profile as accepted in the app first (Profiles > Set as accepted)."; exit 2; }
[ -f "$policy" ] || { echo "STOP: policy $policy not found (run install.sh, or pass --policy FILE)"; exit 2; }
mapfile -t profiles < <(awk '/^profile *=/{sub(/^profile *= */,""); gsub(/"/,""); print}' "$from/CURRENT")
[ "${#profiles[@]}" -gt 0 ] || { echo "STOP: $from/CURRENT lists no profile"; exit 2; }
for p in "${profiles[@]}"; do
  printf '%s\n' "$p" | grep -Eq '^profiles/[A-Za-z0-9._-]+\.toml$' && [[ "$p" != *..* ]] && [ -f "$from/$p" ] || { echo "STOP: $from/CURRENT names an unusable profile '$p'"; exit 2; }
done
mkdir -p captures/lnx

if [ "$check" = 1 ]; then
  # full output on the terminal (the CONFIRM prompt must stay visible); look for the offsets in the point tables
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" HOME="$HOME" python3 -I tools/arltuner.py vf-dump --device --policy "$policy" --allow-taint-bit 2
  exit 0
fi

bash tools/lnx/health_snapshot.sh before-reapply >/dev/null
for prof in "${profiles[@]}"; do
  echo "=== applying $from/$prof (policy $policy) ==="
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" HOME="$HOME" python3 -I tools/arltuner.py apply "$from/$prof" --device --policy "$policy" --allow-taint-bit 2 \
    | tee "captures/lnx/reapply-$(basename "$prof" .toml)-$(date +%Y%m%dT%H%M%S).txt"  # unfiltered: the CONFIRM prompt must be visible
done
bash tools/lnx/health_snapshot.sh after-reapply --since captures/lnx/health-before-reapply.txt | tail -4
echo "done. Verify with: bash tools/lnx/reapply_current.sh --check"
