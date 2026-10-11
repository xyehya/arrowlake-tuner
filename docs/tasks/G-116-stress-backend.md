# G-116 — helper: stress / validation ops (SPEC §9; XTU "Stress test" equivalent)

- **Status:** ready · **Host:** lnx · **Tier:** T0 (simulator / fake binaries only) · **Branch:** `task/G-116-stress-backend` · **Depends on:** v3

## Goal
Give the app XTU's validation loop on Linux: after tuning, run a CPU or memory stress screen (optionally per-core and at a capped ratio) with live
telemetry, error detection, and a stored result log. SPEC §9 rules (R7, R10, R11, R27, R28) are binding. Not an acceptance gate (D-019 banner).

## Read first
`docs/SPEC.md` §9 · `docs/SPEC-v3.md` §1, §9 · `docs/ARCHITECTURE.md` contracts 8, 9 · `tools/lnx/percore_stress.py` · `src/arltuner/helper/ops_measure.py`
(its cpufreq journal + spinner patterns — reuse, don't duplicate) · `src/arltuner/helper/state.py`.

## Owns
New `src/arltuner/helper/ops_stress.py`, new `src/arltuner/stress/` (command builders, output parsers, result store), `tests/stress/`,
`tests/helper/test_stress*.py`; `src/arltuner/helper/ops.py` (**only** registration), `src/arltuner/helper/state.py` (state `stressing` if absent),
`src/arltuner/web/server.py` (`OPS` entries only), `install.sh` (create `/var/lib/arltuner/stress` 0700). **Not** `web/`, `ops_tune.py`, `ops_measure.py`
internals beyond importing shared helpers (if a helper must move, keep a re-export).

## Contract (add as contract 9 addendum "G-116" in your branch's `docs/ARCHITECTURE.md`)
- `stress.start {kind: "cpu"|"memory", method?: "integer"|"vector-math"|"cache" (cpu), units?: [unit] (default all), ratio_cap?: int|null,
  duration_s: 30..3600, memory_mb?: 256..(MemAvailable-2048) (memory)}` (long; state `stressing`) ⇒ result `{id, kind, started, ended,
  duration_s, stopped_by: "time"|"cancel"|"error", errors: int, error_lines: [str ≤ 20], achieved: {unit: {mhz_mean, ratio_mode}}, cap_valid:
  bool|null, max_temp_c, max_pkg_w, mce_new: int, health: "clean"|"delta", record_path}`. `progress` every 2 s `{elapsed_s, of_s, errors}`.
- `stress.stop {}` = cancel. `stress.list {}` ⇒ last 50 results (summary rows). Results root-owned in `/var/lib/arltuner/stress/`.
- Children run **as the invoking user** (`PKEXEC_UID`; drop via `preexec_fn` setgid/setgroups/setuid before exec; refuse if unknown), absolute
  `/usr/bin/stress-ng` / `/usr/bin/stressapptest` trust-checked like perf, fixed env, own process group, killed on stop/exit; args from enums only.
  cpu: `stress-ng --cpu N --taskset LIST --cpu-method <mapped> --verify --metrics-brief --timeout Ts` (map: integer→`int64`, vector-math→`matrixprod`,
  cache→`--cache` stressor); memory: `stressapptest -s T -M MB -W` (parse "Status: PASS/FAIL", miscompare lines). Ratio cap reuses the measure
  journal (scaling_max_freq + EPP) and is restored in every path incl. stale-journal at start; `cap_valid` = achieved within 1 bin of the cap.
- Health bracket: count new MCE / hardware-error kernel lines (`/dev/kmsg` read from the start offset, or `journalctl -k` as fallback) and the
  existing health snapshot before/after.

## Acceptance
Suite green with fake binaries (scripts under tests that emit recorded stress-ng/stressapptest output, incl. a verify failure and a miscompare);
privilege-drop tested with an injectable setter; journal restore paths; cancel kills the process group. No real stress runs, no sudo.
