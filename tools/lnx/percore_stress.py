#!/usr/bin/env python3
"""percore_stress.py - per-core stability sweep across the ratio spectrum. READ-ONLY for MSRs (plain rdmsr 0x198); writes cpufreq caps only.

    sudo python3 tools/lnx/percore_stress.py [--cpus 0-7] [--from-ratio 52] [--seconds 60] [--out captures/lnx/percore-X]

For every CPU (one at a time; P-cores by default):
  1. UNCAPPED: `stress-ng --cpu 1 --taskset CPU --cpu-method all --verify` for --seconds (the core runs at whatever the hardware allows);
  2. CAPPED: the same at every ratio from --from-ratio up to the core's max (cpuinfo_max_freq: 57x favored, 55x others), using a cpufreq
     `scaling_max_freq` cap (exact cap first, ratio+1 if the core settles below - same search as vfdump --measure, T-027).
Every second: MSR 0x198 on that CPU (running ratio [15:8], VID [47:32]/8192) and the coretemp reading. After every step: stress-ng's verdict
(exit code + "failed: 0", --verify catches silent miscalculation) and the kernel-log lines added since the sweep began (MCE / machine check / hardware error
/ segfault lines). The sweep STOPS at the first failure and names the CPU and ratio. Caps are always restored (also on Ctrl-C).
Why: OCCT core cycling mostly runs each core at its max ratio; this also exercises the lower points (53x, 55x on favored cores) and the
interpolated ratios between points (52x, 54x, 56x). Results: <out>.txt (human) and <out>.json.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import struct
import subprocess
import sys
import time

BAD_KLOG = re.compile(r"\bmce\b|machine check|hardware error|segfault|general protection|BUG:|soft lockup|hard LOCKUP|rcu.*stall", re.I)


def parse_cpus(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


def rdmsr(cpu: int, msr: int) -> int:
    fd = os.open(f"/dev/cpu/{cpu}/msr", os.O_RDONLY)
    try:
        return struct.unpack("<Q", os.pread(fd, 8, msr))[0]
    finally:
        os.close(fd)


def sysfs(cpu: int, name: str) -> str:
    return f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/{name}"


def read_int(path: str) -> int:
    with open(path) as handle:
        return int(handle.read().strip())


def set_cap(cpu: int, khz: int) -> None:
    with open(sysfs(cpu, "scaling_max_freq"), "w") as handle:
        handle.write(str(khz))


def core_temp(cpu: int) -> float | None:
    """coretemp 'Core N' for this CPU's core_id (P-cores have their own sensor; E-cores may not)."""
    try:
        core_id = read_int(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id")
    except OSError:
        return None
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(f"{hw}/name").read().strip() != "coretemp":
                continue
        except OSError:
            continue
        for label in glob.glob(f"{hw}/temp*_label"):
            if open(label).read().strip() == f"Core {core_id}":
                return read_int(label.replace("_label", "_input")) / 1000
    return None


def klog_lines() -> list[str]:
    return subprocess.run(["journalctl", "-k", "-b", "-q", "-o", "short-monotonic"], capture_output=True, text=True).stdout.splitlines()


def klog_new_bad(start_count: int) -> list[str]:
    """Kernel-log lines added since the sweep began that look like hardware/stability trouble (expected msr notices excluded)."""
    return [line for line in klog_lines()[start_count:] if BAD_KLOG.search(line) and "Write to unrecognized MSR" not in line]


def run_step(cpu: int, seconds: int) -> dict:
    samples: list[tuple[int, float]] = []
    proc = subprocess.Popen(
        ["stress-ng", "--cpu", "1", "--taskset", str(cpu), "--cpu-method", "all", "--verify", "--timeout", f"{seconds}s", "--metrics-brief"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    ratios, vids, temps = [], [], []
    time.sleep(2)
    while proc.poll() is None:
        word = rdmsr(cpu, 0x198)
        ratios.append((word >> 8) & 0xFF)
        vids.append(((word >> 32) & 0xFFFF) / 8192)
        samples.append((ratios[-1], vids[-1]))
        t = core_temp(cpu)
        if t is not None:
            temps.append(t)
        time.sleep(1)
    output = proc.stdout.read() if proc.stdout else ""
    failed = re.search(r"failed: (\d+)", output)
    ok = proc.returncode == 0 and failed is not None and failed.group(1) == "0"
    return {
        "ok": ok, "rc": proc.returncode, "stress_tail": output.strip().splitlines()[-3:],
        "ratio_min": min(ratios, default=None), "ratio_max": max(ratios, default=None),
        "ratio_mode": max(set(ratios), key=ratios.count) if ratios else None,
        "vid_min": min(vids, default=None), "vid_max": max(vids, default=None),
        "temp_max": max(temps, default=None),
        "samples": samples,
    }


def at_target(res: dict, ratio: int) -> int:
    """How many 1-s samples ran exactly at the target ratio."""
    return sum(1 for r, _ in res["samples"] if r == ratio)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cpus", default="0-7")
    ap.add_argument("--from-ratio", type=int, default=51)  # 52x cannot be pinned on P-cores via cpufreq (caps 5.1-5.29 GHz run 51x, 5.3 GHz runs 53x)
    ap.add_argument("--to-ratio", type=int, default=0, help="highest ratio to test (default: the core max)")
    ap.add_argument("--no-uncapped", action="store_true", help="skip the uncapped step")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--out", default=f"captures/lnx/percore-{time.strftime('%Y%m%dT%H%M%S')}")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo (MSR reads, cpufreq caps, kernel log)")
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    klog_start = len(klog_lines())
    cpus = parse_cpus(args.cpus)
    originals = {cpu: read_int(sysfs(cpu, "scaling_max_freq")) for cpu in cpus}
    results: list[dict] = []
    failed_at = None
    lines: list[str] = []

    def log(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    try:
        for cpu in cpus:
            top = read_int(sysfs(cpu, "cpuinfo_max_freq")) // 100_000
            last = min(top, args.to_ratio) if args.to_ratio else top
            steps: list[int | None] = ([] if args.no_uncapped else [None]) + list(range(args.from_ratio, last + 1))
            for ratio in steps:
                if ratio is None:
                    set_cap(cpu, originals[cpu])
                else:
                    set_cap(cpu, ratio * 100_000)
                res = run_step(cpu, args.seconds)
                if ratio is not None and at_target(res, ratio) < max(5, args.seconds // 4) and (res["ratio_max"] or 0) < ratio:
                    # the exact cap never let the core reach the target: retry with +1 (intel_pstate rounding); a cap that reached it is kept
                    set_cap(cpu, (ratio + 1) * 100_000)
                    res = run_step(cpu, args.seconds)
                bad = klog_new_bad(klog_start)
                if ratio is not None:
                    hit = [v for r, v in res["samples"] if r == ratio]
                    res["at_target"] = len(hit)
                    res["vid_at_target"] = (min(hit), max(hit)) if hit else None
                res.update(cpu=cpu, target=("uncapped" if ratio is None else ratio), klog=bad)
                results.append(res)
                tag = "uncapped" if ratio is None else f"{ratio}x"
                verdict = "PASS" if res["ok"] and not bad else "FAIL"
                log(f"cpu {cpu} {tag:>8}: {verdict}  ran {res['ratio_min']}..{res['ratio_max']}x (mostly {res['ratio_mode']}x)  "
                    f"VID {res['vid_min']:.4f}..{res['vid_max']:.4f} V  Tmax {res['temp_max']} C"
                    + ("" if ratio is None else f"  [{res['at_target']}/{len(res['samples'])} s at {ratio}x"
                       + (f", VID {res['vid_at_target'][0]:.4f}" if res['vid_at_target'] else "") + "]")
                    + ("" if res["ok"] else f"  stress-ng: {res['stress_tail']}") + (f"  KERNEL: {bad[:3]}" if bad else ""))
                if verdict == "FAIL":
                    failed_at = {"cpu": cpu, "target": tag}
                    raise StopIteration
            set_cap(cpu, originals[cpu])
    except StopIteration:
        log(f"STOPPED at the first failure: cpu {failed_at['cpu']} {failed_at['target']}")
    finally:
        for cpu, khz in originals.items():
            set_cap(cpu, khz)
        log(f"restored scaling_max_freq on cpus {args.cpus}")
        with open(args.out + ".json", "w") as handle:
            json.dump({"since": since, "failed_at": failed_at, "results": results}, handle, indent=1)
        with open(args.out + ".txt", "w") as handle:
            handle.write("\n".join(lines) + "\n")
    return 1 if failed_at else 0


if __name__ == "__main__":
    sys.exit(main())
