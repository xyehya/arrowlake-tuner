# R-001 — does the GNOME power profile matter for V/F measurement and cpufreq capping?

- **Status:** ready · **Host:** lnx (real machine, D-037 exception) · **Tier:** T1 (OS frequency settings + MSR reads; **no WRMSR, no mailbox**)
- **Branch:** `task/R-001-power-profile-capping` · output `docs/knowledge/power-profile-capping.md`

## Question (operator)
"Does the GNOME performance mode actually matter? On performance the E-cores usually sit around 46–48×, but I think the capping works either
way — as long as it's not power-saver, which might limit the max frequency."
Answer for the app: must the V/F measurement (cap a unit with `scaling_max_freq`, load it pinned, read the requested VID from MSR 0x198) set or
require a profile/governor/EPP, or does it work the same under every profile? Context: under `performance` the kernel refuses EPP writes (EBUSY,
seen 2026-10-12); E-core caps at 2300 MHz land on a neighbour ratio (hybrid perf scaling).

## Rules (D-037)
- Allowed: `powerprofilesctl set …` (or the tuned-ppd equivalent present here — find out which daemon is running), writes to
  `/sys/devices/system/cpu/cpu*/cpufreq/{scaling_max_freq,scaling_min_freq,energy_performance_preference}`, `stress-ng` pinned with `taskset`
  (as the normal user, never root), `sudo` **reads** of `/dev/cpu/N/msr` (`rdmsr`-style pread, Python is fine) for 0x198 (PERF_STATUS: ratio
  [15:8], VID [47:32] /8192 V), 0x774 (HWP_REQUEST), 0x771 (HWP_CAPABILITIES), 0xE7/0xE8 (MPERF/APERF), 0x19C (thermal status), plus sysfs/turbostat.
- **Forbidden:** any MSR write, any OC-mailbox request (MSR 0x150), `tools/arltuner.py` apply/restore, the arltuner GUI/helper ops, BIOS, installing
  software. The arltuner GUI is running idle on this machine: leave it alone (do not stop it, do not use it).
- Safety: before starting, record the full initial state (profile, governor, EPP, min/max per CPU) to a file and install a `trap` that restores it
  on exit; at the end restore and **verify** byte-for-byte. Stop if any core > 90 °C or `sudo dmesg | grep -ciE "mce|machine check|hardware
  error"` rises. Short loads (≤ 10 s per sample point), one variable at a time.
- Read `tools/lnx/vfcurve.py` and `src/arltuner/vfdump.py` (the app's method) first and reuse the same sampling (settle 0.3 s, 10 samples, keep
  samples where the achieved ratio equals the target).

## Method
1. **Inventory per profile** (power-saver, balanced, performance): governor, intel_pstate status, EPP and EPB per CPU, scaling/cpuinfo min/max,
   HWP_REQUEST fields (min, max, desired, EPP) on CPU 0, 1, 8. Note what each profile changes.
2. **Uncapped behaviour** per profile: idle 5 s and single-thread pinned load 8 s on P0 (favored), P1, CPU 8 (EC0): achieved ratio distribution
   (from 0x198 and APERF/MPERF), VID mean/min/max. Does power-saver cap the max ratio? Does performance raise idle ratios (E at 46–48×)?
3. **Capped measurement** per profile, with the app's method (cap `scaling_max_freq` of the unit's cpus, pinned load on one cpu of the unit, other
   cpus idle): P1 at 30/48/53/55×, P0 at 57×, EC0 at 30/40/46/48×. Record hit rate (kept/total), VID mean, spread. Compare across profiles: is the
   VID at a given ratio the same (within one VID step, ~5 mV)? Is the hit rate different?
4. **EPP effect** within `balanced`: repeat 3 for P1 at 48/55× with EPP `performance` vs `balance_performance` vs `power` (write allowed in
   balanced). Does EPP change the VID at a fixed achieved ratio, or only the hit rate/settle time?
5. **Min+max pinning vs max-only:** P1 48× and EC0 40× with `scaling_min_freq = scaling_max_freq = target` vs max only, under balanced and
   power-saver. Which is more reliable?
6. **E 23× granularity:** for EC0, sweep `scaling_max_freq` 2200000…2400000 kHz in 10 MHz steps (load pinned on cpu 8): achieved ratio and the
   HWP_REQUEST max field written by the kernel. Derive the kHz→perf conversion for E-cores and the cap value that yields 23× (and 15×, 30×).
   Same quick check for P1 (cap → HWP max field) to confirm P is 1:1.

## Deliverable
`docs/knowledge/power-profile-capping.md`: every finding tagged `[MEASURED lnx 2026-10-12]`, compact tables (profile × unit × ratio → hit rate,
VID mean, spread), the E kHz→perf formula with the cap values that hit each E ratio, and a **Recommendation for the app** section (≤ 10 lines):
what the measure op should do about profile/governor/EPP/min-pinning, and what it should tell the user. Raw logs stay in the worktree (not committed),
only small summary tables in the doc. Commit `R-001: ...` with the Co-Authored-By line.
