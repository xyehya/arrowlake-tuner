#!/usr/bin/env bash
# reapply_current.sh - re-apply the CURRENT tuning state after a boot (a cold boot clears every point offset; see profiles/CURRENT).
#   bash tools/lnx/reapply_current.sh          # interactive: you type CONFIRM <token> once per profile
#   bash tools/lnx/reapply_current.sh --check  # read-only: dump the point words (vf-dump, no writes) and compare with the expected state
# Runs each profile listed in profiles/CURRENT through the guard (`arltuner apply --device`), in order, with the session policy named there.
# Safe on a warm boot too: the planner reads every point first and only writes what differs (identical words are rewritten, harmless).
# The first MSR write of a boot sets kernel taint bit 2 (CPU_OUT_OF_SPEC) - expected, allowed via --allow-taint-bit 2.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo"
policy="$(awk -F= '/^policy *=/{gsub(/[ "]/,"",$2); print $2}' profiles/CURRENT)"
mapfile -t profiles < <(awk '/^profile *=/{sub(/^profile *= */,""); gsub(/"/,""); print}' profiles/CURRENT)
[ -n "$policy" ] && [ "${#profiles[@]}" -gt 0 ] || { echo "profiles/CURRENT has no policy/profile lines"; exit 2; }

if [ "${1:-}" = "--check" ]; then
  # full output on the terminal (the CONFIRM prompt must stay visible); look for the offsets in the point tables
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" HOME="$HOME" python3 tools/arltuner.py vf-dump --device --policy "$policy" --allow-taint-bit 2
  exit 0
fi

bash tools/lnx/health_snapshot.sh before-reapply >/dev/null
for prof in "${profiles[@]}"; do
  echo "=== applying $prof (policy $policy) ==="
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" HOME="$HOME" python3 tools/arltuner.py apply "$prof" --device --policy "$policy" --allow-taint-bit 2 \
    | tee "captures/lnx/reapply-$(basename "$prof" .toml)-$(date +%Y%m%dT%H%M%S).txt"  # unfiltered: the CONFIRM prompt must be visible
done
bash tools/lnx/health_snapshot.sh after-reapply --since captures/lnx/health-before-reapply.txt | tail -4
echo "done. Verify with: bash tools/lnx/reapply_current.sh --check"
