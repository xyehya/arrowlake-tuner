#!/usr/bin/env bash
# preflight.sh - READ-ONLY state record before a mailbox sitting (T-007 pre-flight item 4). No MSR access, no mailbox traffic.
# Writes captures/lnx/preflight-<UTC timestamp>.txt and prints it. Safe to run as a normal user (some lines need root; they say so).
set -u
repo="$(cd "$(dirname "$0")/../.." && pwd)"
out_dir="$repo/captures/lnx"
mkdir -p "$out_dir"
out="$out_dir/preflight-$(date -u +%Y%m%dT%H%M%SZ).txt"

{
  echo "== preflight $(date -u +%FT%TZ) =="
  echo "host: $(hostname)  kernel: $(uname -r)  python: $(python3 --version 2>&1)"
  echo "repo: $(git -C "$repo" log --oneline -1 2>/dev/null)   dirty files: $(git -C "$repo" status --short 2>/dev/null | wc -l)"
  echo "kernel taint word (/proc/sys/kernel/tainted): $(cat /proc/sys/kernel/tainted 2>/dev/null)"
  echo "msr module allow_writes: $(cat /sys/module/msr/parameters/allow_writes 2>/dev/null || echo 'n/a (msr built in or not loaded)')"
  echo "msr device nodes: $(ls /dev/cpu/*/msr 2>/dev/null | wc -l) present (expect 24)   cpu4: $(ls -l /dev/cpu/4/msr 2>&1 | head -1)"
  echo "lsmod msr: $(lsmod 2>/dev/null | grep -E '^msr' || echo none)"
  echo "secure boot: $(mokutil --sb-state 2>&1 | head -1)"
  echo "lockdown: $(cat /sys/kernel/security/lockdown 2>/dev/null || echo n/a)"
  echo "lspci device count: $(lspci 2>/dev/null | wc -l)"
  echo "boot id: $(cat /proc/sys/kernel/random/boot_id 2>/dev/null)   uptime: $(uptime -p 2>/dev/null)"
  echo "other tuning tools running (must be none): $(ps -eo pid,args | grep -i -E 'occt|hwinfo|a-tuning|throttlestop|xtu' | grep -v grep | head -3 || true)"
  echo "audit log: ${XDG_STATE_HOME:-$HOME/.local/state}/linuxmon/audit.jsonl  ($(wc -l < "${XDG_STATE_HOME:-$HOME/.local/state}/linuxmon/audit.jsonl" 2>/dev/null || echo 'absent') lines)"
  echo "recent kernel lines mentioning msr/mce/aer (last boot, last 15):"
  journalctl -k -b -o short-monotonic 2>/dev/null | grep -i -E 'msr|mce|machine check|aer|taint' | tail -15 || echo "  (journalctl needs permission: run with sudo to include)"
} | tee "$out"
echo
echo "saved: $out"
