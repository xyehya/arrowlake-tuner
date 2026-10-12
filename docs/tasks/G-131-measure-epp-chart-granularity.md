# G-131 — measurement: EPP under the performance governor, unmeasured points on the chart, E 23× granularity

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-131-measure-epp-chart` · **Depends on:** G-129 merged

## Evidence (operator run 2026-10-12, quick curve "test-yk", GNOME power profile = Performance)
Governor `performance`, intel_pstate `active`, EPP reads `default`. Result: 96 points, data sane (P 8–55× ok on all P, 56/57× ok on P0/P3 only;
non-favored P 56/57× `unreachable/max_turbo`; every E cluster 23× `unreachable/cpufreq_granularity`; E 8/15/30× all 0.775 V = the E VID floor).
Problems shown (twice: once inline in the status line, once as a list, truncated at cpu 9):
`energy_performance_preference of cpu N was not set (Device or resource busy); the run continues` for every CPU.
Chart: hollow dashed rings drawn at 56×/57× at the **last measured voltage** of the non-favored P-cores (and E 23×) — look like data artifacts.

## Goal
1. **EPP:** in intel_pstate active mode with the `performance` governor the kernel pins EPP to performance and refuses writes with EBUSY — that is
   the state the run wants anyway. `ops_measure.py`: read `scaling_governor` first; `performance` ⇒ do not write EPP and do not journal it; add
   one note `"governor performance: EPP already at performance"`. Any other EPP failure ⇒ **one** aggregated problem
   (`"energy_performance_preference not set on cpus 0–23 (Device or resource busy)"`, ranges compacted). Same aggregation on the journal restore
   path (never an EBUSY flood on restore). Tests with fake sysfs raising EBUSY.
2. **Problems display (web):** the status line shows the count ("2 notes — see below"), the list shows each once; no duplication, no truncation.
3. **Chart:** do not draw unreachable/unmeasured points inside the plot. Instead: a small "not measured" row under the x-axis (tick marks at the
   ratio, unit colour, tooltip with unit + reason, e.g. "P1 56×: above this core's max ratio 55×"), plus one caption line grouping them
   ("not measured: P1, P2, P4–P7 at 56–57× (above core max 55×) · EC0–EC3 at 23× (cpufreq cannot pin this ratio)"). The fused-top pins on a
   baseline stay. Table view unchanged.
4. **E 23× granularity:** E-core cpufreq caps are converted to HWP perf units with the hybrid scaling factor, so a cap of 2 300 000 kHz lands on a
   neighbour ratio. When a point classifies `cpufreq_granularity`, retry once with a small search on the cap value (e.g. target ±25/±50 kHz
   steps up to ±99 MHz, reading back `scaling_max_freq` and the achieved ratio) until the achieved mode ratio equals the target; record the cap
   used (`cap_khz`). Still not hit ⇒ keep `unreachable/cpufreq_granularity`. Sim/fake must model the rounding (perf = round(khz / scale), E scale
   from `cpuinfo_max_freq`/`highest_perf`) so the test proves the search finds 23×.
5. **Review the measurement logic** per unit (P-core = one cpu; E-cluster = 4 cpus sharing one VID): which cpus are capped, which loaded, how the
   VID sample is read and filtered (`kept/total`), how the other units are held during a point, and whether the other cores' activity can raise a
   unit's sample. Fix real bugs you find (tests); report the rest in the report-back (≤ 10 lines) — do not change the method silently.

## Read first
`src/arltuner/helper/ops_measure.py`, `src/arltuner/vfdump.py` (measure loop), `src/arltuner/helper/measure_sim.py`, `web/app.js` (Curves chart:
`curvePins`, `recPoints`, status/problems line), `tests/helper/test_measure*.py`, `tests/web/test_cpu_facts_page.py`.

## Owns
`src/arltuner/helper/ops_measure.py`, `src/arltuner/helper/measure_sim.py`, `src/arltuner/vfdump.py`, `web/*`, `tests/helper/test_measure*.py`, `tests/web/*`, `tests/test_vfdump.py`.

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green; the tests named above; no hardware.
