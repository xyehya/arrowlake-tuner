# G-111 — helper: V/F curve measurement, curve store, memory reader, telemetry restart

- **Status:** ready · **Host:** lnx · **Safety tier:** T0 (simulator, fake sysfs, fake /dev/mem only) · **Branch:** `task/G-111-measure-curves-mem`
- **Depends on:** main @ SPEC-v3 commit. Parallel with G-112 (tune/policy) and G-113 (web).

## Goal
Implement the contract 9 ops `vf.measure`, `curves.list/get/update/delete`, `mem.read`, and the `telemetry.start` restart rule in the root helper,
with simulator models so the GUI shows realistic data under `--sim`. SPEC-v3 §2 (memory card), §3 (Curves).

## Read first (only these)
`docs/SPEC-v3.md` §2–3 · `docs/ARCHITECTURE.md` contracts 8 + 9 · `docs/knowledge/memory-mchbar.md` · `docs/knowledge/telemetry-sources.md` ·
`src/arltuner/vfdump.py` (measure part: `MeasureIO`, `measure_group`, `SysfsMeasureIO`, `caps_to_try`) · `src/arltuner/helper/` (core, ops,
state, main, trust) · `src/arltuner/web/server.py` (`OPS`).

## Owns
New: `src/arltuner/curves/` (record schema + root store), `src/arltuner/telemetry/mem.py`, `src/arltuner/helper/ops_measure.py`, `tests/curves/`,
`tests/helper/test_measure*.py`, `tests/telemetry/test_mem.py`. Edit: `src/arltuner/helper/state.py` (add `measuring`), `src/arltuner/helper/core.py`
(telemetry restart; journal recovery at start), `src/arltuner/helper/ops.py` (**only** `op_telemetry_start` + register your ops in `builtin_ops`),
`src/arltuner/helper/main.py` (sim/device factories for your IO), `src/arltuner/vfdump.py` (measure part only, if needed), `src/arltuner/web/server.py`
(`OPS` entries for your ops only), `install.sh`/`uninstall.sh` only to create/keep `/var/lib/arltuner/curves` (root 0700).
**Not:** `guard/`, `policies/`, `controls/`, `profiles/`, `web/` (G-112/G-113).

## Requirements
- **Measurement** per SPEC-v3 §3: reuse `measure_group` (cap search, at-target filter, MISMATCH); quick/dense ratio sets (P points 8,20,30,48,53,
  55,56,57; E 8,15,23,30,40,46; dense 8..max turbo ratio per unit from sysfs `cpuinfo_max_freq`); all CPUs of a unit loaded with pinned spinners
  (prefer `/usr/bin/python3 -I -c` busy loops with `sched_setaffinity`, fixed env, cwd /; no shell); settle 0.3 s, 10 × 0.1 s samples (constants);
  `unreachable` when the cap search cannot reach the ratio. **Journal** (`/run/arltuner/measure-journal.json`, root 0600) of every touched
  sysfs value **before** the first change; restore on success, cancel, exception, and at helper start if a stale journal exists; spinners killed
  in every path. EPP set to `balance_performance` for the run when the file exists (restore after). Emits `progress` + `vf.point` events. Cancel.
- **Store**: records per contract 9 under `/var/lib/arltuner/curves/` with contract 8 file rules (O_NOFOLLOW, owner, mode; atomic write via temp
  + rename in the same dir); `tuning_state` from the helper's last `vf.dump`/`tune.read` words this boot if any (a small in-memory hook other ops can
  set), else `unknown`. ≤ 200 records; one baseline.
- **mem.read** per `memory-mchbar.md`: injectable roots (`pci_config`, `devmem`, `dmi_root`, `hwmon_root`); compiled whitelist only; host-bridge
  ID check; plausibility; instance agreement count; SMBIOS type 17 from `/sys/firmware/dmi/entries/17-*/raw` (stdlib parse); spd5118 temps.
  Every failure ⇒ `available: false` + reason. Tests encode the fixture values into synthetic words.
- **telemetry.start** restart rule (contract 9).
- **Simulator** (`--sim`): `SimMeasureIO` producing a plausible curve per unit (e.g. P ~0.70 V at 8× rising to ~1.40 V at 57×, per-core spread
  ±25 mV, E ~0.65→1.15 V; 56/57× unreachable except P0/P3), with real-time pacing scaled down (quick run ≈ 10 s in sim); sim `mem.read` returns
  the knowledge fixture. Store in `--sim` goes to a scratch dir passed by argv (`--state-dir`), never `/var/lib`.

## Acceptance
`python3 tools/run_tests.py` green. Tests: journal restore in all four paths (incl. simulated helper crash + restart), MISMATCH/unreachable,
cancel ⇒ partial stored, store trust rules, mem decode = fixture, bad host bridge / disabled MCHBAR / short read ⇒ unavailable, telemetry restart.
No hardware, no sudo, never `~/AI/turbostat*`.

## Report-back (≤ 300 words)
Ops implemented + any contract deviation (exact) · files · test count · what only the real machine can show.
