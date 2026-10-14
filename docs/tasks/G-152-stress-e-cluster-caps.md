# G-152 — Stress: E-core ratio caps that hold (whole cluster, kernel frequency map), exact "cap held" report

- **Status:** done (merged 2026-10-14)
- **Host:** lnx
- **Safety tier:** T0 (worker: simulator + fake sysfs only)
- **Branch/worktree:** `task/G-152-stress-e-caps`
- **Depends on:** none (disjoint from G-151)

## Goal
The Stress op caps each selected CPU at `ratio × 100 MHz` and calls a run valid when every unit settled **within one bin** of the cap (SPEC §9.2).
On the reference 285K that is wrong for E-cores in two ways [MEASURED lnx 2026-10-14, `captures/lnx/esplit-tests-20261014.txt`,
`docs/knowledge/platform-arl-s.md` "E-cluster clock and cpufreq caps"]:
- the four E-cores of a cluster run **one ratio**: a cap on one E-core alone did not hold it (cap 4.7 GHz on one core ran 48×); capping all four held 46×;
- the E frequency map is not `ratio × 100 MHz`: `cpuinfo_max_freq` 4.9 GHz ↔ 48×, 4.7 GHz ↔ 46× (G-147), caps 4.7 / 4.8 / 4.9 GHz held 46× / 47× / 48×,
  a 4.6 GHz cap ran 44–45×. P-cores: `ratio × 100 MHz` holds (5.7 GHz ↔ 57×).
So an "E 46× stress" runs 45× (or 48×) and is still reported valid. The operator is screening a 46× undervolt on the E-cores right now.

## Fixed decisions
1. **Cap value per unit from the kernel's own map:** kHz(r) = r × `cpuinfo_max_freq(unit)` / `max_ratio(unit)`, rounded to the nearest 100 MHz,
   where `max_ratio` is the unit's top ratio as the measure code already derives it (turbo table / `0x1C`; see `ops_measure.py` around the
   "maximum ratio of each unit" note). P-cores give `r × 100 MHz` as today. If the unit's max ratio is unknown, keep `r × 100 MHz` and say so in the
   result. Reuse the helper code the measure path has; do not duplicate.
2. **E-clusters are capped whole:** every CPU of each E-cluster that the run touches is capped (journaled and restored exactly like the selected
   CPUs, all exit paths). The load still runs only on the selected CPUs.
3. **Report:** the result says "cap held at N×" only when the unit's ratio mode equals the cap (or the unit's reachable max when lower); a unit
   that settled one bin off is shown as "ran M× (cap N×)" — a warning, not "held". `_cap_valid` keeps SPEC §9.2's one-bin validity for the
   run verdict, but the exact/off-by-one distinction is in the result and on the Stress page.
4. Stress page text (web/, Stress section only): one line that E-cores are capped per cluster.

## Read first (only these)
- `src/arltuner/helper/ops_stress.py` (cap, journal, `_cap_valid`, achieved), `src/arltuner/helper/ops_measure.py` (max ratio per unit, caps)
- `docs/SPEC.md` §9.2, `docs/knowledge/platform-arl-s.md` (E-cluster clock line), the Stress section of `web/app.js`
- tests of the stress op (`tests/` — grep `stress`)

## Owns
`src/arltuner/helper/ops_stress.py`, `src/arltuner/helper/measure_sim.py` (if the fake needs a cluster/frequency model), `web/app.js` + `web/index.html`
Stress section only, stress tests. Not `src/arltuner/msr/sim.py`, not `controls/`, `ops_tune.py`, `profiles/` (G-151 owns them), not docs/guide.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green.
- Fake-sysfs tests: E run on CPUs 12–13 at 46× caps 12–15 at 4.7 GHz (E `cpuinfo_max_freq` 4.9 GHz, max 48×), journals and restores all four; P run
  at 55× caps at 5.5 GHz; a simulated E run that settles at 45× under a 46× cap reports "ran 45× (cap 46×)", not "held".
- No change to which programs run or how (trust checks, user, EPP rules unchanged).

## Report-back (≤ 250 words)
What changed per decision, test count line, anything not done as fixed.
