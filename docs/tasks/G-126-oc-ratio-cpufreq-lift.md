# G-126 — an OC ratio raise must lift the OS frequency ceiling

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-126-oc-ratio-cpufreq-lift`

## Goal (`docs/knowledge/tuning-results.md` "Turbo-table writes and E OC 49×")
After a `tune.apply` that raises an OC ratio, the kernel raises `cpuinfo_max_freq` (HWP highest) but keeps `scaling_max_freq`, so the new ratio is
never used. Fix in the helper apply path (OS setting, not a mailbox write):
1. After a successful apply that changed an OC ratio (or `0x1D`) for some CPUs, for each of those CPUs: re-read `cpuinfo_max_freq`; if
   `scaling_max_freq` equals the **old** cpuinfo max (i.e. the user had no own cap), set it to the new `cpuinfo_max_freq`. On a lowering apply,
   if `scaling_max_freq` > new `cpuinfo_max_freq`, lower it to match (the kernel may already clamp; read back). Report per CPU in the apply
   result: `cpufreq: [{cpu, before_khz, after_khz}]`; the page shows it in the read-back section.
2. Never touch a CPU whose `scaling_max_freq` the user had set below the old max (respect user caps); never write while a measure/stress journal
   is active (those own the caps; they restore to the journaled value — make the journal restore use the *current* cpuinfo max when the journaled
   value equalled the old max).
3. Sim + fake sysfs tests: raise 48→49 lifts 4.9→5.0 GHz; revert 49→48 lowers; user cap kept; journal interplay.

## Read first
`docs/knowledge/tuning-results.md` (2026-10-12 sections) · `src/arltuner/helper/ops_tune.py` (apply) · `src/arltuner/helper/ops_measure.py` (journal, sysfs
IO) · `src/arltuner/msr/sim.py` · `web/app.js` (read-back rendering).

## Owns
`src/arltuner/helper/ops_tune.py`, `src/arltuner/helper/ops_measure.py` (journal restore rule only), `src/arltuner/msr/sim.py` (cpuinfo max follows 0x1D),
`web/app.js` + `tests/web/*` (render `cpufreq`), tests for these.
