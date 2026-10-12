# G-132 — measure/stress from R-001: min=max pinning, no_turbo, "not reachable by cap", stress EPP, user-cap reason

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-132-measure-pinning-noturbo` · **Depends on:** G-131, R-001 merged

## Evidence
`docs/knowledge/power-profile-capping.md` (R-001, [MEASURED lnx 2026-10-12]) — read it whole. Summary: profile/governor/EPP do not change the VID at a
ratio (performance = balanced); power-saver sets `intel_pstate/no_turbo=1` (P ≤ 37×, E ≤ 32×) and EPP `power` holds loaded cores below target;
min=max pinning is at least as reliable everywhere and fixes the EPP problem; some ratios are unreachable by any cpufreq cap (E 13,18,21,23,26,29,31,
34,39; P1 42,47,52) because of the kHz→perf→ratio rounding; E 23× (a fused point) is one of them.

## Goal
1. **Pinning:** the measure op sets `scaling_min_freq = scaling_max_freq = cap` on the unit's cpus (journal both, restore both — min first when lowering,
   max first when raising, so the kernel never rejects the pair). Keep the existing cap order and the 2.5 s settle. Do not touch profile/governor/EPP
   (drop the EPP write entirely; keep the G-131 note only if useful).
2. **no_turbo:** read `/sys/devices/system/cpu/intel_pstate/no_turbo`; when 1, the result carries one note ("Turbo is off (power-saver profile?):
   ratios above base cannot be reached — switch to Balanced or Performance") and points above the base ratio (`base_frequency`/`cpuinfo` base or
   the 0xCE-derived base if available; else the highest ratio reached at the first point) are `unreachable` with reason `turbo_off`, not attempted. The
   Curves page shows the note before a run too (a sysfs read via an existing op or preflight field — your choice, keep the contract additive).
3. **Not reachable by cap:** new reason `cap_granularity` for a point the G-131 search could not hit (replaces `cpufreq_granularity` for new
   records; old records still render). Text: "Linux cannot cap this core to exactly N× (frequency rounding); the nearest are A× and B×" — fill A/B
   from what the search saw. Bound the search time (it should not add more than ~10 s per unreachable point).
4. **User cap reason:** a point skipped because the user's own `scaling_max_freq` is below its frequency gets reason `user_cap` (not `max_turbo`).
5. **Stress EPP flood:** `helper/ops_stress.py` ~649–653: same treatment as G-131 (skip under performance, one aggregated line otherwise).
6. Contract: document the new reasons and point fields (`cap_khz`, `retried`, reasons `turbo_off`, `cap_granularity`, `user_cap`) in
   `docs/ARCHITECTURE.md` contract 9 addendum; update the web reason texts.
7. Correct `docs/knowledge/` where R-001 contradicts older notes (T-027 "+1 cap" note) — one line each, citing R-001.
8. **Interpolation (operator, 2026-10-12):** V/F frequencies between points are interpolated by the CPU (54x measured at the midpoint of 53x/55x). (a) A fused V/F point that could not be
   measured (e.g. E 23x `cap_granularity`) with measured V/F points on both sides gets `status: "interpolated"`, `vid_v` = linear between them (clamped to the monotonic rule), `from: [A, B]`,
   `exact` when equal; never `ok`; not above reach / without a neighbour. (b) The chart plots V/F points only (fused + OC), straight lines between them, interpolated points as hollow markers,
   the "not measured" row only for V/F points with no value; in-between ratios stay in the Table. (c) Tune predictions use the interpolated value and label it "(interpolated)".

## Owns
`src/arltuner/helper/ops_measure.py`, `src/arltuner/helper/ops_stress.py`, `src/arltuner/helper/measure_sim.py`, `src/arltuner/vfdump.py`,
`src/arltuner/helper/ops.py` (only if you add a preflight field), `src/arltuner/platform/preflight.py` (same), `web/*`, `tests/**`,
`docs/ARCHITECTURE.md`, `docs/knowledge/*.md` (corrections only).

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green; tests for pinning order + restore, no_turbo skip + note, cap_granularity text, user_cap, stress EPP.
