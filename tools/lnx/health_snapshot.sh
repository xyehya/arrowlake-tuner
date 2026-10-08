#!/usr/bin/env bash
# health_snapshot.sh - READ-ONLY before/after record for a mailbox sitting (the quick form of the guard's health bracket).
#   bash tools/lnx/health_snapshot.sh LABEL            -> captures/lnx/health-LABEL.txt (taint, PCI count, module count, kernel-log length)
#   bash tools/lnx/health_snapshot.sh LABEL --since FILE
#        -> also prints the kernel-log lines added since FILE's snapshot, minus the expected msr-write notices, and flags any taint/PCI/module change.
# Expected after mailbox traffic: taint bit 2 (CPU_OUT_OF_SPEC) on the first write of a boot; `msr: Write to unrecognized MSR 0x150`,
# `filter_write: N callbacks suppressed` (rate-limited notices of our own writes) and `energy_model: updating cpuN cpu_cap=…` after 0x1D writes.
# Anything else (MCE, AER, PCIe errors, a missing device) = stop and report (docs/SAFETY.md).
set -u
repo="$(cd "$(dirname "$0")/../.." && pwd)"
label="${1:?usage: health_snapshot.sh LABEL [--since FILE]}"
out="$repo/captures/lnx/health-$label.txt"
mkdir -p "$repo/captures/lnx"
{
  echo "time $(date -u +%FT%TZ)"
  echo "taint $(cat /proc/sys/kernel/tainted)"
  echo "pci $(lspci | wc -l)"
  echo "modules $(($(lsmod | wc -l) - 1))"
  echo "klog_lines $(sudo -n journalctl -k -b -q | wc -l)"
} > "$out"
cat "$out"
if [ "${2:-}" = "--since" ] && [ -n "${3:-}" ]; then
  prev="$3"
  for key in taint pci modules; do
    a=$(awk -v k=$key '$1==k{print $2}' "$prev"); b=$(awk -v k=$key '$1==k{print $2}' "$out")
    [ "$a" = "$b" ] || echo "CHANGED $key: $a -> $b"
  done
  from=$(awk '$1=="klog_lines"{print $2}' "$prev")
  echo "--- new kernel lines (expected msr/filter_write/energy_model notices hidden):"
  sudo -n journalctl -k -b -q -o short-monotonic | tail -n +"$((from + 1))" \
    | grep -v -E 'Write to unrecognized MSR|See https://git.kernel.org|filter_write: [0-9]+ callbacks suppressed|energy_model: updating cpu' | head -40
fi
