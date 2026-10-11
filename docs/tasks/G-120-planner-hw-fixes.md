# G-120 — planner fixes from the first real write sittings

- **Status:** ready (after G-119 merges) · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-120-planner-hw-fixes`

## Goal
Make `tune.plan`/`tune.apply` match what the hardware did on 2026-10-11 (`docs/knowledge/tuning-results.md`, "First write sittings"):
1. **OC word shape:** when a unit has **no OC point** (its OC ratio ≤ the top fused ratio: P0's 57, all others 0/≤46 E), a whole-curve offset
   writes `ratio | offset<<21` with V target **0** (the hardware drops a target there: wrote `0xFEC5B439`, read `0xFEC00039`). Only when an OC
   ratio above the top fused ratio is set (OC point exists) may the word carry an adaptive target. The read-back compare must expect exactly the
   word the hardware keeps (no false trip).
2. **CLAMPED warnings:** none for points the plan does not change (spurious "57× offset +0.0 clamped" on every non-favored core); **add** CLAMPED
   when an OC-point adaptive target is below the top fused point's effective V (EC1 1.117 V < point 6 1.1375 V ⇒ clamped).
3. **Prediction for the OC point:** effective V at the OC ratio = max(target, V(top fused point)); bins between top fused point and OC ratio
   interpolate (47× = midpoint of 46× and 48×) — vfmodel.
4. **Stock tagging (`ops_measure`):** `stock` only if every OC word is the cold-boot word (P0 `0x39`, others 0 — read from the first `tune.read`
   of the boot / platform file) **and** all offsets are 0; any `tune.apply` this boot ⇒ not stock.
5. **Accepted compare:** treat an XTU-shaped OC word whose target equals the fused 0x09 voltage and offset 0 (e.g. `0x5B439`) as equal to the
   bare-ratio word (`0x39`); report `equivalent: true` with both words.

## Read first
`docs/knowledge/tuning-results.md` (2026-10-11 write section) · `docs/knowledge/platform-285k.md` §3–4 · `docs/knowledge/registers.md` (V/F word) ·
`src/arltuner/helper/ops_tune.py` · `src/arltuner/vfmodel.py` · `src/arltuner/controls/planner.py` · `src/arltuner/helper/ops_measure.py` (stock rule) ·
`src/arltuner/helper/ops.py` (`accepted_match`) · `src/arltuner/msr/sim.py`.

## Owns
`src/arltuner/helper/ops_tune.py`, `src/arltuner/vfmodel.py`, `src/arltuner/controls/planner.py`, `src/arltuner/helper/ops_measure.py` (stock rule only),
`src/arltuner/helper/ops.py` (`accepted_match` only), `src/arltuner/msr/sim.py` (model: drop V target on a non-OC-point OC word; add P2 = top+1 OC
point when OC ratio > top fused), tests for these.

## Acceptance
Suite green; sim reproduces the five hardware behaviours above and the tests pin them.
