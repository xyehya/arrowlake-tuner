# G-117 — turbo-only plans, UI on warning codes, sim offsets at stock

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-117-turbo-plans-warning-codes` · **Depends on:** G-114, G-115 merged

## Goal
Close the G-114/G-115 gaps so the Tune page's turbo editor and warnings work end to end:
1. **Turbo-only plans:** `controls/planner.py` (+ executor/report as needed) can plan and apply turbo-table writes (0x1AD with 0x1AE counts
   unchanged, 0x650 with 0x651 unchanged; plain-MSR path of T-023, guarded `msr_allow` entries) **without** any core/point write in the same plan.
   `tune.plan {intent: {turbo: {...}}}` alone ⇒ a real plan (no more `TURBO_ONLY_UNSUPPORTED`); F5 order enforced; read-back compares the tables.
   The favored-core ratio (0x1C/0x1D) is not part of turbo edits. CLI behaviour for existing commands unchanged.
2. **UI:** `web/app.js` uses `warnings[].code` (`CLAMPED`, `E40_OFFSET`, `E_TARGET_WHEA`, `POSITIVE_OFFSET`, `NO_BASELINE`, `TURBO_RAISE`,
   `NOTE`) instead of regexes; shows `vccia.label`/`margin_v`/`light_load`; turbo editor sends turbo-only plans; fake helper mirrors G-114's real
   shapes (see `src/arltuner/helper/ops_tune.py`).
3. **Sim:** `SimConfig.measured()` starts with point offsets 0 (cold-boot stock, as on the machine today), keeping the old offsets only as an opt-in.

## Read first
`docs/SPEC-v3.md` §4, §9 · `docs/ARCHITECTURE.md` contract 9 + addenda · `src/arltuner/controls/` · `src/arltuner/helper/ops_tune.py` ·
`src/arltuner/msr/sim.py` · `web/app.js` (Tune) · `tests/web/fake_v3.py`.

## Owns
`src/arltuner/controls/`, `src/arltuner/helper/ops_tune.py`, `src/arltuner/msr/sim.py`, `web/*`, `tests/web/*`, `tests/controls*/`, `tests/helper/test_tune*.py`,
`tests/helper/test_cpu_facts.py`. **Not** stress files (G-116 in flight: `helper/ops_stress.py`, `stress/`).

## Acceptance
Suite green; sim tests: turbo-only P and E plans apply and read back; order violation refused; mixed plan still works; UI warning badges by code.
