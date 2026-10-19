#!/bin/bash
# apply-p53p55-e46.sh — restore the state that passed 15/15 compile stress (2026-10-19, uv-p53p55-e46) after a boot.
# Two guarded applies through the INSTALLED root-owned arltuner CLI (isolated python, installed policy):
#   A: P per-point offsets + P turbo buckets 1-2 -> 55 (lower) + E turbo 46x8 + E point 6 offset 0
#   B: P turbo buckets 3-8 -> 55 (raise)   (the guard refuses a table that both lowers and raises in one profile)
# Close the desktop app first only if it is mid-apply; the CLI and the app share the mailbox lock.
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
run() {
  sudo install -m 0644 -o root "$D/$1.toml" "/root/$1.toml"
  sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" /usr/bin/python3 -I -c '
import sys, re
sys.dont_write_bytecode = True
sys.path.insert(0, "/usr/lib/arltuner")
from arltuner.cli import main
def confirm(prompt):
    m = re.search(r"CONFIRM (\S+)", prompt); return "CONFIRM " + m.group(1) + "\n"
sys.exit(main(["apply", sys.argv[1], "--device", "--operator", sys.argv[2], "--policy", "/etc/arltuner/policy.toml", "--allow-taint-bit", "2"], confirm_fn=confirm))
' "/root/$1.toml" "$USER"
}
LOG="$HOME/AI/arrowlake-tuner/captures/lnx/apply-p53p55-e46-$(date +%Y%m%dT%H%M%S).txt"
for step in p53p55-e46-a p53p55-e46-b; do
  echo "=== $step $(date -Is)" | tee -a "$LOG"
  out=$(run $step 2>&1) || { echo "$out" >> "$LOG"; echo "$out" | grep -E "^Result|refused|Stopped|tripped" ; echo "FAILED at $step (log $LOG)"; exit 1; }
  echo "$out" >> "$LOG"
  echo "$out" | grep -E "^Result"
  echo "$out" | grep -q "^Result: OK" || { echo "$out" | grep -E "refused|Stopped|tripped"; echo "FAILED at $step (log $LOG)"; exit 1; }
done
echo "MCE this boot: $(sudo journalctl -k -b --no-pager | grep -ciE 'mce|machine check|hardware error')   log: $LOG"
