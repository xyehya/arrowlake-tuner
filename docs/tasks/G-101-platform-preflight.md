# G-101 — platform file + preflight
- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-101-platform-preflight` · **Depends on:** none

## Goal
Move the platform facts (CPUID, topology, V/F point ratios, fused words `FUSED_POINT`, turbo-table defaults, sensor chip) out of code constants into
`platforms/arl-s-285k.toml`, loaded and validated; implement the preflight checks of SPEC §3 as pure functions over injectable sysfs/proc roots.

## Read first
`docs/SPEC.md` §3 · `docs/knowledge/platform-arl-s.md` · `src/arltuner/topology/` · grep `FUSED_POINT` in `src/arltuner`.

## Owns
`platforms/`, `src/arltuner/platform/`, `tests/platform/`; plus, only to switch them to the platform file (no behaviour change): `src/arltuner/topology/`,
`src/arltuner/msr/__init__.py`, `src/arltuner/msr/measured_baseline.py`, `src/arltuner/msr/sim.py`, the `FUSED_POINT` lines of `src/arltuner/cli.py`.

## Deliverables
Loader + schema (stdlib `tomllib`), `preflight(root=Path("/"))` returning named results (pass / read-only / refuse + reason) for: CPUID match,
hypervisor flag, lockdown bracketed word, msr device/module/allow_writes, pkexec + polkit, Python version, optional tools. Existing code reads the
fused words / topology from the platform file (behaviour unchanged; all existing tests green).

## Acceptance
Tests with fake roots for every check and every failure; `python3 tools/run_tests.py` green; no hardware access.

## Non-goals
Fused-word identity check over the mailbox (that is G-102's `vf.dump`); other CPUs.
