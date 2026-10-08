#!/usr/bin/env python3
"""vfcurve.py - measure the requested core voltage at fixed ratios on one group of CPUs (an E-cluster or one P-core). READ-ONLY for MSRs.

    sudo python3 tools/lnx/vfcurve.py --cpus 8-11 --ratios 40,46,48 [--out FILE]

How (measured method, docs/research/lnx-first-read.md "E cluster 1 V/F curve at stock"):
  * caps the group's cpufreq ``scaling_max_freq`` so the cores run at the target ratio: first cap = ratio x 100 MHz; if the core settles
    BELOW the target (intel_pstate on E-cores at high ratios maps N x 100 MHz to N-1), retry with (ratio + 1) x 100 MHz (same search as
    ``arltuner vf-dump --measure``, T-027); the running ratio is CHECKED (MSR 0x198[15:8]) and only samples AT the target are kept;
  * loads the group with ``stress-ng --cpu <n> --taskset <cpus>`` for a few seconds;
  * samples the requested VID = MSR 0x198[47:32] / 8192 V on the first CPU of the group (plain rdmsr, no mailbox, no taint);
  * restores every CPU's original cap, also on error / Ctrl-C.
E-cores share one regulator per cluster of four, so measure a whole cluster (all four CPUs loaded). A sample whose running ratio is not the
target is reported as MISMATCH, never silently used. Writes only cpufreq sysfs (an ordinary OS setting), never an MSR.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import time

MSR_PERF_STATUS = 0x198


def parse_cpus(text: str) -> list[int]:
    cpus: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        cpus.extend(range(int(lo), int(hi or lo) + 1))
    return cpus


def rdmsr(cpu: int, msr: int) -> int:
    fd = os.open(f"/dev/cpu/{cpu}/msr", os.O_RDONLY)
    try:
        return struct.unpack("<Q", os.pread(fd, 8, msr))[0]
    finally:
        os.close(fd)


def cap_path(cpu: int) -> str:
    return f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_max_freq"


def set_cap(cpus: list[int], khz: int) -> None:
    for cpu in cpus:
        with open(cap_path(cpu), "w") as handle:
            handle.write(str(khz))


def sample(cpus: list[int], khz: int, settle_s: float, samples: int) -> tuple[list[float], list[int]]:
    set_cap(cpus, khz)
    load = subprocess.Popen(
        ["stress-ng", "--cpu", str(len(cpus)), "--taskset", ",".join(map(str, cpus)), "--timeout", f"{int(settle_s + samples * 0.1 + 2)}s", "--quiet"]
    )
    try:
        time.sleep(settle_s)
        vids, ratios = [], []
        for _ in range(samples):
            word = rdmsr(cpus[0], MSR_PERF_STATUS)
            vids.append(((word >> 32) & 0xFFFF) / 8192)
            ratios.append((word >> 8) & 0xFF)
            time.sleep(0.1)
    finally:
        load.wait()
    return vids, ratios


def measure(cpus: list[int], ratio: int, settle_s: float, samples: int, min_valid: int = 5) -> tuple[list[float], list[int], int]:
    """VIDs of the samples taken AT ``ratio`` (exact cap first, then ratio+1 if the core settled below), all running ratios seen, cap used."""
    for khz in (ratio * 100_000, (ratio + 1) * 100_000):
        vids, ratios = sample(cpus, khz, settle_s, samples)
        kept = [v for v, r in zip(vids, ratios) if r == ratio]
        if len(kept) >= min_valid or max(ratios) > ratio:
            return kept, ratios, khz
    return kept, ratios, khz


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cpus", required=True, help="CPU list, e.g. 8-11 (one E-cluster) or 4 (one P-core)")
    ap.add_argument("--ratios", required=True, help="comma list of target ratios, e.g. 40,46,48")
    ap.add_argument("--settle", type=float, default=2.5, help="seconds of load before sampling (default 2.5)")
    ap.add_argument("--samples", type=int, default=30, help="VID samples per ratio, 0.1 s apart (default 30)")
    ap.add_argument("--out", help="also append the result lines to this file")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo (needs /dev/cpu/*/msr and cpufreq sysfs)")
    if shutil.which("stress-ng") is None:
        sys.exit("stress-ng not found")
    cpus = parse_cpus(args.cpus)
    originals = {cpu: open(cap_path(cpu)).read().strip() for cpu in cpus}
    lines = []
    try:
        for ratio in (int(r) for r in args.ratios.split(",")):
            kept, ratios, khz = measure(cpus, ratio, args.settle, args.samples)
            ok = len(kept) >= 5
            line = (
                f"cpus {args.cpus} ratio {ratio}x: "
                + (f"VID {min(kept):.4f}..{max(kept):.4f} V  [{len(kept)}/{len(ratios)} samples at {ratio}x, cap {khz // 1000} MHz]" if ok
                   else f"MISMATCH: running ratio {min(ratios)}..{max(ratios)}, {len(kept)} samples at {ratio}x (no voltage)")
            )
            print(line, flush=True)
            lines.append(line)
    finally:
        for cpu, khz in originals.items():
            with open(cap_path(cpu), "w") as handle:
                handle.write(khz)
        print(f"restored scaling_max_freq on cpus {args.cpus}")
    if args.out:
        with open(args.out, "a") as handle:
            handle.write(f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n" + "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
