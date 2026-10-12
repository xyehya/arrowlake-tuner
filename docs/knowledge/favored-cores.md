# Favored cores: what they are, how they are read, how to characterise cores

General for Arrow Lake-S. The reference chip's per-core tables are in `examples/reference-285k/notes/` (one sample, not a spec).

## 1. What favored cores are
- Intel characterises each core at manufacturing test and **fuses** the result; firmware (pcode) publishes it per core as `HWP_CAPABILITIES`
  Highest_Performance (MSR 0x771) and ACPI `_CPC` highest performance. Linux `intel_pstate` feeds that ranking to the scheduler
  (`sched_set_itmt_core_prio()`, ITMT = Turbo Boost Max 3.0); the OS only consumes it.
  [DOC https://blog.linuxplumbersconf.org/2016/ocw/system/presentations/3813/original/plumbers_fcore_v2.pdf]
  [DOC https://lkml.iu.edu/hypermail/linux/kernel/1610.2/04086.html]
- Patents describe the mechanism: per-core attributes fused "programmed at manufacturing time", a favored core may run a higher frequency and/or a lower
  voltage [DOC https://patents.google.com/patent/US10922143B2/en]; per-core Vmin fused in high-volume manufacturing and ranked by pcode/BIOS by an
  efficiency criterion [DOC https://patents.google.com/patent/US12073227B2/en].
- On ARL-S a favored P-core has a higher fused top ratio than the others; the OC mailbox shows it per core in cmd `0x1C` (`[15:8]` = fused ratio, `[7:0]`
  = current favored ratio, which follows a `0x1D` write) (`registers.md`) [MEASURED win 2026-10-01, lnx 2026-10-11]. XTU's ARL-S constants name
  "2 favored cores" [DECOMPILED XTU, `registers.md` "Not the mailbox"]; ThrottleStop's "Per Core" setting is this favored ratio (`0x1C`/`0x1D`)
  [DECOMPILED win-throttlestop-fivr.md].
- The turbo table gives the top ratio only to the 1-2 active-core buckets, so a favored core reaches it only with few cores busy [MEASURED lnx
  2026-10-11].
- No evidence that a board vendor can re-designate favored cores; boards expose per-core ratio limits and V/F offsets only [UNVERIFIED].

## 2. How the app reads them (live, D-036)
- `src/arltuner/topology/live.py`: favored = the P-cores with the highest `/sys/devices/system/cpu/cpuN/acpi_cppc/highest_perf`; if that file is
  missing on any P-core, the highest `cpufreq/cpuinfo_max_freq`; **none** when all P-cores are equal. `favored_source` names the source used.
- After a `tune.read`, `0x1C[15:8]` per core is the cross-check (done by the caller, never an input to the topology).
- Both sysfs values follow runtime writes: a `0x1D` write raises HWP highest and `cpuinfo_max_freq` (an E-core went 0x44 -> 0x45, 4.9 -> 5.0 GHz)
  [MEASURED lnx 2026-10-12], so detection shows the state the firmware and earlier writes left, not a fuse readout. The fused byte `0x1C[15:8]` does
  not move.
- Example from one chip (285K): favored CPU 0 and 3, `highest_perf` 92 vs 88, `0x1C` `0x3939` vs `0x3737` [MEASURED lnx 2026-09-29].

## 3. What is known about the criterion
- The ARL ranking criterion (Fmax at a voltage ceiling, VID, Vmin) is not published [UNVERIFIED].
- **Favored is not simply the lowest VID at a common point:** on the reference chip two non-favored cores were within 20 mV of a favored core at 53x/55x
  [MEASURED lnx 2026-10-02, one chip].
- **The top points of non-favored cores are flat placeholders:** on the reference chip each non-favored core's fused 57x word equals its own 55x stock VID
  within 2 mV, while on the favored cores the 55->57x slope is real (+127 mV) [MEASURED lnx 2026-10-02, one chip]. This refines the community lead that
  non-favored top points are "pre-defined from the favored cores" [COMMUNITY
  https://skatterbencher.com/2025/03/16/skatterbencher-84-core-ultra-9-285k-overclocked-to-5800-mhz/]: they are not copies.
  Working hypothesis: favored = the cores that passed a top-ratio Fmax test within the voltage ceiling; VID is a by-product [UNVERIFIED].
- **Fused VID spread is mostly guard band, not true Vmin:** in an all-core 60-min gate every P-core held 55x at ~1.27-1.29 V, so the cores with the
  worst fused VID had the most undervolt room [MEASURED win 2026-10-04 / lnx 2026-10-08, one chip]. Caveat: all-core test; per-core Vmin was not
  measured.

## 4. How to characterise cores
| Metric | How | Indicates |
|---|---|---|
| M1 fused/stock VID per point | stock measured curve (read-only) + cmd `0x09` top point | the factory's view, guard band included |
| M2 Vmin at a fixed ratio | 1-thread self-checking load pinned to one core; step that core's point offset down | real voltage margin (the main bin metric) |
| M3 Fmax at a fixed V | per-core OC ratio + V target, 1-thread load | frequency headroom (the TBM3-style criterion) |
| M4 margin = M1 - M2 | derived | guard band the fuse carried |
| M5 temperature and core power at fixed ratio + VID | pinned cap; per-core `0x19C` temperature, RAPL | leakage / thermal position (TVB ~1.1 mV/C [COMMUNITY]) |
| HWP highest / `0x1C` | read-only | the OS-visible ranking only |
Protocol sketch (writes need the operator's go-ahead per `docs/SAFETY.md`):
1. Baseline: stock state, health gate, stock measured curve.
2. Isolate: load one P-core with `taskset -c N`, others idle and stock; fix the ratio with `scaling_min_freq` = `scaling_max_freq` (some ratios are not
   reachable by a cap, `power-profile-capping.md` §6). Each P-core has its own DLVR, so one core's offset does not move the others.
3. Loads must self-check, because failures are silent (`tuning-results.md` §1): y-cruncher 1T, Prime95 small-FFT 1 worker, Linpack 1T residual check,
   `stress-ng --verify`; rotate them.
4. M2 per core: lower that core's point offset in small steps (e.g. 5 mV), ~10 min per step; at the first error back off (e.g. +10 mV) and confirm
   with a 60-min run; record Vmin, the failing load and the temperature.
5. M3 (raises V/ratio): OC ratio one bin above the core's top at a fixed target voltage no higher than the favored cores' fused top VID; pass/fail per core.
6. M5: same cap and VID on each core, same fan curve and ambient; record steady-state temperature and power.
7. Rank by M2 (primary), then M3, then M5; re-run M2 on the top cores after a cold boot.

## 5. Open questions
- Does a non-favored P-core accept an OC ratio above its own top (56/57x on the 285K) at all? Does a P OC ratio above the fused top add a readable point?
- Where does HWiNFO's "Favored Cores List" come from? What formula does ASRock's CPU Indicator use?
- Aging: favored cores spend the most time at the highest VID. Intel states ARL is not affected by the Raptor Lake Vmin shift
  [COMMUNITY https://hothardware.com/news/arrow-lake-and-mobile-immune-crashing-issue]; not confirmed independently.
