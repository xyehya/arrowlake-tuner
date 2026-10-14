# 08 Stability testing

This page explains how to decide whether a tuning is stable: what the **Stress** tab does and does not tell you, the longer acceptance gate the
project used, and how to watch for machine-check errors. It is for anyone who has applied a change.

**The one rule: a quick screen is not an acceptance gate.** The Stress tab says so in a banner ("Quick screen - NOT an acceptance gate ... Before you trust a setting, run a long self-checking load (a Linpack-class test for an hour or more)") and on every result ("A quick screen, not an acceptance gate."), and the
code deliberately never prints "pass" or "stable" (D-019). It can only tell you "no errors detected in N min" or that something went wrong.

## 1. Why a long test is needed
On the reference chip, unstable undervolts produced **silent wrong results**: the stress tool reported "CPU error detected" while the kernel logged no
WHEA event, no machine check and no system error [MEASURED win 2026-10-04, tuning-results.md §1]. Consequences:

- A clean kernel log does **not** prove stability.
- Short and weak tests passed states that a long Linpack-class run failed: OCCT SSE for 10-15 minutes, a 20-minute CPU+RAM run, a clock cycler, a 1-thread
  test and per-core `stress-ng --verify` sweeps all passed states that OCCT Linpack 2021 later failed [MEASURED win 2026-10-04, tuning-results.md §2].
- Time to failure is noisy: the same offsets failed after about 50 minutes in one run and about 2 minutes in another. Only a full pass counts, not how long a
  failure took.
- The failing run does not tell you which core: OCCT reported "core 0" when core 0 was at stock and the cause was an offset on an E-cluster. Bisect by
  reverting groups of offsets, one variable at a time.

## 2. The Stress tab (quick screens)
The Stress tab loads the CPU or the memory for a few minutes and counts errors. It writes no ratios and no voltages; it can cap a ratio through
cpufreq, which is restored when the screen ends. Nothing is sent to the mailbox.

| Control | Options |
|---|---|
| **Screen** | **CPU** (stress-ng on the cores you pick) or **Memory** (stressapptest on all CPUs) |
| **CPU load** | **Integer** (integer arithmetic, stress-ng `int64`), **Vector math** (matrix products, `matrixprod`; heavier on the vector units), **Cache** (cache thrashing) |
| **Memory to test** | Automatic (3/4 of free memory) or 1, 2, 4, 8, 16, 32 GB; the helper refuses a size that does not leave 2 GB free |
| **Length** | 1, 5, 15 or 60 minutes, or **Custom** (30 s to 60 min in 30 s steps) |
| **Ratio cap** | No cap, or 8x ... 60x. The cap is applied to the units that run |
| **Units to load** | a mini core map (P-cores and E-clusters, favored cores starred); quick buttons All / P-cores / E-clusters / None |

Press **Start**; **Stop** cancels. While it runs, the card shows elapsed time, errors so far and clocks. The tool runs the stress program as **your
user, never root** (`stress.start` is refused without a known invoking user), from `/usr/bin/stress-ng` or `/usr/bin/stressapptest`.

The **Result** card shows, at most, these words: "No errors detected in N min", or "N errors detected in N min. Treat this setting as unstable", or that the
screen ended with a tool error. Alongside it:

| Field | Meaning |
|---|---|
| Errors | stress-ng `--verify` / stressapptest error count |
| Ran | actual against requested length (`stopped_by`: ran to the end, stopped by you, tool error) |
| Hottest / Peak package power | from the telemetry sampler during the run |
| health clean / changed | a before-and-after check of the kernel taint word, PCI devices and loaded modules; "changed" means look at the lines below |
| new machine-check events | machine-check lines in the kernel log during the run |
| cap held / not held | with a ratio cap: every loaded unit ran within one ratio of the cap; "not held" means the screen did not test the capped ratio |
| Clock each unit held | mean clock and most common ratio per unit |

Earlier screens are listed under **Earlier screens** (up to 50 shown, 200 kept in `/var/lib/arltuner/stress/`, root-owned).

### Using it well
- Test the units you changed: after **Apply**, the review dialog offers **Validate with a stress screen**, which opens Stress with the changed units
  selected. Nothing starts until you press Start.
- Use a ratio cap to test a specific point: cap the unit at the ratio of the point you changed (for example 53x) so the screen runs that point. Check
  "cap held".
- When you cap an E-cluster, cap **all four** of its cores: they run one ratio, and a cap on one core alone did not hold it below its neighbours' on the reference chip [MEASURED lnx 2026-10-14, platform-arl-s.md §3]. Whether the Stress tab's cap does this for a whole cluster is not stated in the repo's docs; check "cap held".
- Run a **vector-math** screen as well as integer: different loads exercise different parts of the core, and single-thread failures show up under different
  workloads [favored-cores.md §4].
- The reference-chip experience is that quick screens are good for ruling out an obvious failure within minutes, and useless for declaring success.

## 3. The acceptance gate used on the reference chip
The project's gate (D-019, 2026-10-04) for calling a state accepted [MEASURED win 2026-10-04, lnx 2026-10-07/08, tuning-results.md §2]:

1. OCCT **Linpack 2021** (24 threads, 2048 MB), **60 minutes**, no errors; and
2. OCCT **CPU, SSE, variable load**, **60 minutes**, no errors;
3. OCCT run **unprivileged** (never as root), OCCT and XTU closed while applying;
4. no new machine check or WHEA events; the Windows event count and the kernel log watched.

Both passed on Windows (2026-10-04) and on Linux (2026-10-07/08) for the accepted state there. This gate is the project's choice, not a standard. OCCT
and Linpack are not shipped or launched by this tool; install and run them yourself. Alternatives named in the repo: y-cruncher, Prime95 small-FFT, a
a newer Linpack build (not evaluated here) [favored-cores.md §4, examples/reference-285k/notes/tuning-history.md §6]. Whatever you choose:

- it must **self-check** its results (a test that only loads the CPU cannot catch silent corruption);
- run it long, at the voltages and ratios you will really use, with the cooling you will really have;
- repeat it after a cold boot; and re-run it after any change to BIOS settings or a new BIOS.

## 4. Machine-check and kernel log
Silent errors leave no log line, but real hardware errors can. After a test:

```
sudo journalctl -k -b | grep -i -E 'mce|machine check|hardware error|aer|whea'
```

The Stress result already counts new machine-check lines. The first MSR write of a boot produces expected `msr: Write to unrecognized MSR 0x150 ...`
lines and sets taint bit 2; those are normal and are excluded from the health comparison [MEASURED lnx 2026-10-01, linux-path.md §1, §3]. On the
reference machine every test sitting recorded zero machine-check lines. On Windows, the WHEA event count is the equivalent signal (unchanged at 38
across the gate there).

## 5. Per-core isolation (optional, command line)
`tools/lnx/percore_stress.py` is a read-only research script (it reads MSR `0x198`; it writes only cpufreq caps, always restored). For each CPU in
turn it runs `stress-ng --cpu 1 --taskset CPU --cpu-method all --verify` uncapped, then capped at each ratio from `--from-ratio` (default 51) to the
core's maximum, logging running ratio, VID and temperature every second, and **stops at the first failure**, naming the CPU and ratio. It also
scans the kernel log for machine-check, hardware-error, segfault and lockup lines.

```
sudo python3 tools/lnx/percore_stress.py --cpus 0-7 --from-ratio 51 --seconds 60
```
Options: `--cpus`, `--from-ratio`, `--to-ratio`, `--no-uncapped`, `--seconds`, `--out`. Results go to `captures/lnx/percore-<timestamp>.txt` and `.json`.
It exercises the lower points and the interpolated ratios between points, which OCCT core cycling mostly skips. It is a screen, not the gate.

The favored-core notes (favored-cores.md §4) sketch a protocol for finding each core's minimum stable voltage: step one core's point offset down by
5 mV at a time, 10 minutes per step with a self-checking single-thread load, back off 10 mV at the first error and confirm with 60 minutes. That is a
plan, not a measured result [UNVERIFIED as a procedure].

## 6. Decision table
| Observation | Action |
|---|---|
| Crash, freeze, reboot under load | cold boot (reverts all); go back to the last good state; make the change smaller |
| Stress screen reports errors | treat as unstable; revert the last change; bisect by groups |
| Screen clean, gate not yet run | not accepted; do not rely on it |
| Gate passed, health clean, temperatures acceptable | candidate for **Set as accepted**: apply it, read it back (Tune), then accept; the accepted state is the complete state of that read ([09](09-profiles-and-restore.md)) |
| Gate passed but a different workload (games, compile, memory-heavy) misbehaves | the gate did not cover it; back off or extend the test |

Next: [09 Profiles and restore](09-profiles-and-restore.md) · Previous: [07 Overclocking](07-overclocking.md)
