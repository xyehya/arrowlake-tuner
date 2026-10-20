#!/bin/bash
# restore-good.sh [PROFILE] — re-apply the KNOWN-GOOD state (default good-p53p55-fav-e46m100; or good-p53p55-fav-e48-1185), see GOOD-STATE.md, through the installed arltuner CLI.
# One guarded apply (installed root-owned tree, installed policy, --allow-taint-bit 2 for the first MSR write of a boot). Safe to run at any time.
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"; N=${1:-good-p53p55-fav-e46m100}
sudo install -m 0644 -o root "$D/$N.toml" "/root/$N.toml"
LOG="$HOME/AI/arrowlake-tuner/captures/lnx/restore-good-$(date +%Y%m%dT%H%M%S).txt"
sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" /usr/bin/python3 -I -c '
import sys, re
sys.dont_write_bytecode = True
sys.path.insert(0, "/usr/lib/arltuner")
from arltuner.cli import main
def confirm(prompt):
    m = re.search(r"CONFIRM (\S+)", prompt); return "CONFIRM " + m.group(1) + "\n"
sys.exit(main(["apply", sys.argv[1], "--device", "--operator", sys.argv[2], "--policy", "/etc/arltuner/policy.toml", "--allow-taint-bit", "2"], confirm_fn=confirm))
' "/root/$N.toml" "$USER" > "$LOG" 2>&1 || true
grep -E "^Result|refused|Stopped|tripped" "$LOG" | head -3
bad=$(sed -n '/^Apply report/,$p' "$LOG" | grep -E "cmd 0x11 write|msr 0x(1ad|650) write" | grep -vc "read-back equal" || true)
echo "writes not read back equal: $bad   MCE this boot: $(sudo journalctl -k -b --no-pager | grep -ciE 'mce|machine check|hardware error')   log: $LOG"
grep -q "^Result: OK" "$LOG" && [ "$bad" = 0 ]
