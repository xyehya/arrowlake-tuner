#!/usr/bin/env bash
# reapply_current.sh - kept as a name only (G-148, D-044): the accepted state is ONE complete profile now, so re-applying it IS the restore.
#   bash tools/lnx/reapply_current.sh [--check] [--from DIR] [--policy FILE] [--orchestrated]
# runs tools/lnx/restore_tuning.sh with the same arguments (default --from /etc/arltuner/accepted, --policy /etc/arltuner/policy.toml): one CONFIRM,
# one guarded apply that writes only the words that differ, then the read-back compared with EXPECTED-words.txt. See restore_tuning.sh.
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/restore_tuning.sh" "$@"
