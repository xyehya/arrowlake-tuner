# Power profile vs. cpufreq capping for V/F measurement (R-001)

Host: lnx (Fedora, kernel 7.2.9-200.fc44, intel_pstate active + HWP, tuned-ppd daemon), reference 285K. Date 2026-10-12 (D-037: sysfs cpufreq,
profile switch, MSR reads only). All findings `[MEASURED lnx 2026-10-12]`. The mechanisms (profiles, EPP, pinning, 100 MHz bins, perf-unit
quantisation) are intel_pstate behaviour on hybrid parts; the exact VIDs, scale factors and unreachable-ratio sets are this chip and kernel's and must be
re-derived elsewhere (the app finds them live: a ratio no cap reaches is reported `cap_granularity`).

**Question:** must the V/F measurement (cap a unit with `scaling_max_freq`, load one pinned CPU, read requested VID from MSR 0x198) set or require a
power profile / governor / EPP? **Answer:** no profile is required; the VID at a given ratio is the same under performance and balanced (and under
power-saver for the ratios it can reach). What matters is (1) **power-saver is not usable as is** (turbo off + EPP `power`), (2) pin `scaling_min_freq`
= `scaling_max_freq` to make the cap profile-proof, (3) some ratios cannot be reached by any cap (hybrid perf quantisation), E 23x among them.

## Method
`powerprofilesctl` is absent; the profile is `net.hadess.PowerProfiles.ActiveProfile` on the system bus (`busctl set-property`), served by tuned-ppd
(power-saver = tuned `powersave`, balanced = `balanced`, performance = `throughput-performance`). Sampling as `src/arltuner/vfdump.py`: cap on every CPU of
the unit (P: the one core; E: the 4 CPUs of the cluster), `stress-ng --cpu 1` pinned to one CPU of the unit (P0 = cpu 0, P1 = cpu 1, EC0 = cpu 8, user
account), 45 samples of 0x198 on that CPU 0.1 s apart after a 0.3 s settle, 2 repetitions per point. Cap order as the app: ratio x 100 MHz, then (ratio+1)
x 100 MHz if the unit settled below. Two windows over the same samples: **A** = 10 samples at 0.3-1.3 s (the task's 0.3 s settle), **B** = samples from
2.5 s on (the app's `SETTLE_S`), ~23 per run. Hit = running ratio 0x198[15:8] equals the target. Max core temperature seen 67 C; MCE count stayed 0.

## 1. What each profile changes (all CPUs, same on every CPU)
| | performance | balanced | power-saver |
|---|---|---|---|
| governor | `performance` | `powersave` | `powersave` |
| EPP (sysfs / HWP field / EPB) | `default` / 0 / 0 | `balance_performance` / 64 / 6 | `power` / 255 / 15 |
| HWP_REQUEST min field (cpu0 P, cpu8 E) | = max (pinned: 92, 68) | 13 / 12 (floor) | 13 / 12 |
| `intel_pstate/no_turbo`, `min_perf_pct` | 0, 100 | 0, 14 | **1**, 14 |
| `cpuinfo_max_freq` P / E | 5700(cpu0)/5500, 4900 | same | **3700 / 3200 MHz** (= base) |
| `scaling_max_freq` after switch | unchanged | unchanged | clamped to the new cpuinfo_max |
| EPP write | **EBUSY** | ok | ok |
Switching profile resets governor, EPP, EPB, no_turbo; it does **not** touch a `scaling_max_freq`/`scaling_min_freq` set by the user (cap 3000000 survived
performance -> power-saver -> balanced -> performance). HWP_CAPABILITIES (0x771) is the same in every profile (cpu0 highest 92, cpu1 88, cpu8 68), apart from the
"most-efficient" byte (bits 23:16) changing on some E cpus (hardware-reported).

## 2. Uncapped behaviour
| | performance | balanced | power-saver |
|---|---|---|---|
| idle E cpu 8, ratio | **48x in 37 of 50 samples**, else 15x | mostly 8x (27/50), 48x 14/50 | 8x, mean 9 |
| idle P cpu 0 / 1, mean ratio | 25 / 26 | 45 / 25 | 9 / 17 |
| 1-thread load P1 | 55x (all samples after 2.5 s: 55x 52/55) | 55x (49/55) | **27x** |
| 1-thread load P0 (favored) | 57x 35/55, rest 55/54/51 | 57x 20/55 | **27x** |
| 1-thread load EC0 | 48x | 48x | **19x** |
| VID at that load, P1 / EC0 | 1.2712 V / 1.1226 V | 1.2712 / 1.1226 | 0.7786 / 0.775 |
E idles at 46-48x under performance (~8x in balanced). Power-saver turns turbo off (ratio ceiling = base, 37 P / 32 E here) **and** its EPP `power`
holds a loaded core at 27x (P) / 19x (E), below even that ceiling.

## 3. Capped measurement (app method) per profile
`kept/total` summed over 2 reps, window A (10 samples at 0.3 s) | window B (app, >= 2.5 s). VID = mean over kept samples in B; both reps identical unless two values shown.
Cap = ratio x 100 MHz unless noted (E 46x needs 4700 MHz, E 48x 4900 MHz: one retry, same in every profile).
| unit | ratio | performance A / B, VID | balanced A / B, VID | power-saver |
|---|---|---|---|---|
| P1 | 30 | 22/22, 45/46, 0.8058 / 0.8049 | 21/22, 46/46, 0.8048 / 0.8057 | 0/46 (runs 27x) |
| P1 | 48 | 22/22, 42/46, 1.0410 | 18/22, 45/46, 1.0410 | 0/46 (27x) |
| P1 | 53 | 18/22, 39/46, 1.1711 | 21/22, 43/46, 1.1711 | 0/46 (27x) |
| P1 | 55 | 21/22, 43/46, 1.2712 | 22/22, 44/46, 1.2712 | 0/46 (27x) |
| P0 | 57 | 14/22, 30/46, 1.3864 | 15/22, 36/46, 1.3864 | 0/46 (27x) |
| EC0 | 30 | 22/22, 46/46, 0.7750 | 22/22, 46/46, 0.7750 | 0/46 (19x) |
| EC0 | 40 | 22/22, 46/46, 0.9700 | 22/22, 46/46, 0.9700 | 0/46 (19x) |
| EC0 | 46 (cap 4700) | 22/22, 46/46, 1.1226 | same | 0/46 (19x) |
| EC0 | 48 (cap 4900) | 22/22, 46/46, 1.1226 | same | 0/46 (19x) |
- **VID is profile-independent**: identical to the VID quantum except P1 30x (0.8048-0.8058 V, a 2.6 mV step, within the TVB/temperature noise) between performance and
  balanced; the spread inside a run is 0 (2.6 mV at 30x). Hit rates differ only by wake-up noise (the 51/42/48x dropouts on P cores), not by profile. P0 57x is the
  intermittent favored-core point in both (64-78 % hit), as G-114 found.
- E-core VID of 46x and 48x is the same 1.1226 V (the top E point is 46x).
- Power-saver: no hit at all, **even at 30x** (below base), because EPP `power` holds the load at 27x / 19x whatever the cap is (HWP min stays 13/12).

## 4. EPP effect (balanced, P1)
| ratio | EPP (HWP field) | A | B | VID | first sample at target |
|---|---|---|---|---|---|
| 48 | performance (16) | 21/22 | 44/46 | 1.0410 | 0.30 s |
| 48 | balance_performance (64) | 20/22 | 44/46 | 1.0410 | 0.30-0.40 s |
| 48 | balance_power (179) | 16/22 | 45/46 | 1.0410 | 0.60 s |
| 48 | power (255) | 0/22 | 0/46 | none (27x) | never |
| 55 | performance / balance_performance / balance_power | 22, 21, 16 /22 | 44, 41, 43 /46 | 1.2712 | 0.30 / 0.30 / 0.40-0.70 s |
| 55 | power (255) | 0/22 | 0/46 | none (27x) | never |
EPP does not change the VID at a fixed achieved ratio; it changes the settle time (slower with a more power-biased EPP) and, at `power`, caps the loaded
ratio at 27x (P) / 19x (E).

## 5. min+max pinning vs max-only
Pinning = `scaling_min_freq` = `scaling_max_freq` = target (HWP min = max). Under `performance` the kernel does this by itself (min = max = cap); under the
powersave profiles max-only leaves HWP min at 13 / 12 and the EPP decides.
| profile | unit, ratio | max-only A / B | pinned A / B | VID |
|---|---|---|---|---|
| balanced | P1 48 | 21/22, 42/46 (dips to 42x) | 21/22, 45/46 | 1.0410 both |
| balanced | EC0 40 | 22/22, 46/46 | 22/22, 46/46 | 0.9700 both |
| power-saver | P1 30 | 0/22, 0/46 (27x) | 21/22, 45/46 | 0.8058 (= performance) |
| power-saver | EC0 30 | 0/22, 0/46 (19x) | 22/22, 46/46 | 0.7750 (= performance) |
| power-saver | P1 48 | 0 (27x) | 0: runs **37x** (clamped to base) | - |
| power-saver | EC0 40 | 0 (19x) | 0: runs **32x** (clamped to base) | - |
Pinning is at least as reliable everywhere and the only thing that works in power-saver; with it the power-saver VIDs at 30x equal the performance ones.
Above base (P 37x / E 32x) power-saver cannot measure anything (`no_turbo=1`; a cap above `cpuinfo_max_freq` is silently clamped, e.g. 4800000 -> 3700000).
Write order matters: raising -> max first, then min; lowering -> min first, then max (min > max is EINVAL). Note: the very first `scaling_min_freq` write after
a reset sometimes read back HWP min 13 for a moment (P1 at 4700000); the pinned runs themselves were stable.

## 6. kHz -> perf -> ratio granularity (cap resolution)
`scaling_max_freq` is quantised to **100 MHz bins**: with N = floor(kHz / 100000), the HWP_REQUEST max field (and min field for `scaling_min_freq`) written by
the kernel depends only on N (verified in 10 MHz steps over the whole range, balanced and performance identical, readback exact). HWP perf units are not
ratios, and the achieved ratio is floor(perf / k):
| core kind | perf p from the bin N | achieved ratio |
|---|---|---|
| E (cpu 8; cpuinfo max 4900 = p 68) | p = ceil(1.377 x N) (fits all N = 8..49; a = 1.376-1.377 gave 0 mismatches, 1.378 one, 1.379 two) | floor(p / 1.4) |
| P cpu 1 (max 5500 = p 88) | p = ceil(1.595 x N) (N = 8..55; ceil(1.6 N) is off by one at N = 42, 47, 52) | floor(p / 1.6) |
| P cpu 0 (max 5700 = p 92) | N = 55, 56, 57 -> 88, 90, 92 (scale slightly different from cpu 1) | floor(p / 1.6) |
The ratio rule held on all 104 high-confidence loaded points (E p 20..68, P p 47..92). Examples (E): N 22 -> p 31 -> 22x, N 23 -> p 32 -> 22x,
N 24 -> p 34 -> 24x (**p 33 = 23x is never produced**); N 15 -> 21 -> 15x; N 30 -> 42 -> 30x, N 31 -> 43 -> 30x; N 46 -> 64 -> 45x, N 47 -> 65 -> 46x; N 48 -> 67 -> 47x;
N 49 -> 68 -> 48x (an E cap never reaches 49x here). Measured E sweep, cap 2200000..2400000 in 10 MHz steps: 22x up to 2390000, 24x at 2400000; 23x never.
Older note superseded: an earlier run (2026-10-02, T-027) found E 30/40/46x all needing the (ratio+1) cap; on kernel 7.2.9 E 30x and 40x hit at ratio x
100 MHz in every profile and only 46x/48x needed +1. The newer measurement wins; the retry logic stays because the set is chip/kernel-dependent.
Caps that give each ratio (smallest cap; the cap -> ratio map is a step function, any kHz inside the 100 MHz bin works):
- **E, cpu 8-11:** 8..12 -> N = ratio; 14-17, 19, 20, 22, 24, 25, 27, 30, 32, 35, 40 -> N = ratio; 28 -> 2900000; 33 -> 3400000; 36 -> 3700000; 37 -> 3800000; 38 -> 3900000;
  41 -> 4200000; 42 -> 4300000; 43 -> 4400000; 44 -> 4500000; 45 -> 4600000; 46 -> 4700000; 47 -> 4800000; 48 -> 4900000. **Unreachable: 13, 18, 21, 23, 26, 29, 31, 34, 39.**
  The machine's E points (8, 15, 23, 30, 40, 46x): 8 -> 800000, 15 -> 1500000, 30 -> 3000000, 40 -> 4000000, 46 -> 4700000 (8x from the no-load HWP map only, the others measured under load); **23x unreachable**.
- **P cpu 1:** N = ratio for 8..41, 43..46, 48..51, 53..55 (and 56 -> 5600000, 57 -> 5700000 on cpu 0); **unreachable: 42, 47, 52**. The machine's P points
  (8, 20, 30, 48, 53, 55, 56, 57x) are all reachable with cap = ratio x 100 MHz. Other P cores (2-7) were not mapped; their scale may differ by a few counts as cpu 0 vs 1 do,
  so the achieved ratio must stay checked per core `[UNVERIFIED]`.
- The only way to ask for a ratio no cap reaches would be a direct write of HWP_REQUEST (MSR 0x774, min = max = 33 for E 23x). Not tested, not allowed here; it is a
  WRMSR outside the OC mailbox and a policy question for the operator `[UNVERIFIED]`.

## Recommendation for the app (measure op; implemented in G-132)
1. Do **not** require or change the power profile, governor or EPP; the VID at a ratio does not depend on them. Never write EPP (EBUSY under `performance`, and not needed when pinned).
2. Set `scaling_min_freq` = `scaling_max_freq` = cap on every CPU of the unit (raise: max then min; lower: min then max) and restore both exactly afterwards. This works in every profile and removes dips.
3. Keep the ratio x 100 MHz then (ratio+1) x 100 MHz order: it covers every reachable ratio (E 28/33/36-38/41-48 need +1).
4. Detect power-saver / turbo off before measuring (`/sys/devices/system/cpu/intel_pstate/no_turbo` = 1, or `cpuinfo_max_freq` of P/E at 3700/3200 MHz): points above 37x (P) / 32x (E) cannot be
   measured; report "turbo is off (power-saver): switch to Balanced or Performance in GNOME Settings" and skip those points instead of reporting MISMATCH. Do not flip `no_turbo` yourself.
5. Mark ratios that no cap can produce as "not reachable by cpufreq cap (hybrid perf step)" rather than MISMATCH: E 23x (neighbours 22x/24x), E 13/18/21/26/29/31/34/39, P cpu 1 42/47/52; their voltage cannot be measured this way, only interpolated or left unknown.
6. Use the app's settle (2.5 s; with 0.3 s the first target sample arrives at 0.3-0.7 s depending on EPP, A-window hit rate 70-80 % at `balance_power`). Tell the user: the profile does not matter except power-saver; a profile switch resets EPP/no_turbo but not the caps; E cores idle at 46-48x only under Performance (cosmetic).

## 7. A cap write takes effect asynchronously `[MEASURED lnx 2026-10-19]`
`scaling_max_freq` is applied through freq_qos and the cpufreq policy update worker: a read 1 ms after writing cpu7 5500000 -> 5400000 still returned 5500000
(audit, runtime.apply), and the file read 5400000 about a second later; cpu7 then ran exactly 5400 MHz under a pinned load. powercap limit writes are synchronous
(PL2 read back immediately the same session). The helper polls the caps / uncore read-back every 10 ms for up to 500 ms (G-232, `ops_runtime.READBACK_*`).
