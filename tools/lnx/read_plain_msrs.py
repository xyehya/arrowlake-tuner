#!/usr/bin/env python3
"""read_plain_msrs.py - plain rdmsr of the allowlisted non-mailbox MSRs. READ-ONLY: no wrmsr, no OC mailbox, no kernel taint.

    sudo python3 tools/lnx/read_plain_msrs.py [--cpus 0,8,20] [--msrs 0x650,0x1AD]

Default: the T-023 readable set (guard.policy.PLAIN_MSR_READABLE) on CPU 0 and one CPU per E-cluster: 0x1AD / 0x1AE (P turbo ratios /
bucket counts), 0x650 / 0x651 (E turbo ratios / bucket counts), 0xCE (PLATFORM_INFO), 0x194 (FLEX_RATIO: [20] OC lock), plus 0x198
(PERF_STATUS: [15:8] ratio, [47:32]/8192 requested VID). Decodes are in docs/knowledge/registers.md. Any other MSR is refused, so this can
never become a blind sweep.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

ALLOWED = {0x1AD, 0x1AE, 0x650, 0x651, 0xCE, 0x194, 0x198}


def rdmsr(cpu: int, msr: int) -> int:
    fd = os.open(f"/dev/cpu/{cpu}/msr", os.O_RDONLY)
    try:
        return struct.unpack("<Q", os.pread(fd, 8, msr))[0]
    finally:
        os.close(fd)


def decode(msr: int, v: int) -> str:
    if msr in (0x1AD, 0x650):
        return "ratios by bucket " + "/".join(str((v >> (8 * k)) & 0xFF) for k in range(8))
    if msr in (0x1AE, 0x651):
        return "bucket core counts " + "/".join(str((v >> (8 * k)) & 0xFF) for k in range(8))
    if msr == 0xCE:
        return f"base ratio {(v >> 8) & 0xFF}, [24] OC avail {(v >> 24) & 1}, [28] programmable TRL {(v >> 28) & 1}"
    if msr == 0x194:
        return f"[20] OC lock {(v >> 20) & 1}, [19:17] OC bins {(v >> 17) & 7}"
    if msr == 0x198:
        return f"ratio {(v >> 8) & 0xFF}, requested VID {((v >> 32) & 0xFFFF) / 8192:.4f} V"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cpus", default="0,8,12,16,20")
    ap.add_argument("--msrs", default="0x1AD,0x1AE,0x650,0x651,0xCE,0x194")
    args = ap.parse_args()
    msrs = [int(m, 0) for m in args.msrs.split(",")]
    bad = [hex(m) for m in msrs if m not in ALLOWED]
    if bad:
        sys.exit(f"refused: {bad} not in the read allowlist {sorted(map(hex, ALLOWED))}")
    for cpu in (int(c) for c in args.cpus.split(",")):
        for msr in msrs:
            v = rdmsr(cpu, msr)
            print(f"cpu {cpu:2d} MSR {msr:#06x} = {v:#018x}  {decode(msr, v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
