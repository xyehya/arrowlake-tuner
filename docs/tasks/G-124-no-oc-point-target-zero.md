# G-124 — no-OC-point OC words always target 0; plan step list matches what is sent

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-124-no-oc-point-target-zero`

## Goal (`docs/knowledge/tuning-results.md` "Offset-only OC words")
1. `controls.planner.kept_word` / the guard's revert copy / the sim: an OC word **without an OC point** (ratio ≤ top fused) is always written and
   expected with **V target 0**, whatever the offset (hardware kept `0x00000039` for a written `0x0005B439` when no 0x1D followed). The guard's
   D-035 rule already allows it. Reverts likewise. Only an OC point (ratio > top fused) carries a target.
2. `tune.plan` predicted `steps` / `ops_needed` must list exactly what the executor sends (offset-only ⇒ `write_vf` only per CPU; no
   `write_favored`/`write_granular`; `write_percore_enable` only if it will actually be sent).
3. Sim reproduces 1 (drops a target on a no-OC-point word regardless of offset); tests pin it.

## Read first
`docs/knowledge/tuning-results.md` (2026-10-12 section) · `docs/DECISIONS.md` D-035 · `src/arltuner/controls/planner.py` · `src/arltuner/guard/guard.py`
(`_revert_vf_word`) · `src/arltuner/guard/policy.py` · `src/arltuner/helper/ops_tune.py` (step prediction) · `src/arltuner/msr/sim.py`.

## Owns
`src/arltuner/controls/`, `src/arltuner/guard/guard.py` (revert only), `src/arltuner/helper/ops_tune.py` (step prediction only), `src/arltuner/msr/sim.py`,
tests for these.
