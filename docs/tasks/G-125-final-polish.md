# G-125 — final polish from the real page-driven write (2026-10-12)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-125-final-polish`

## Goal (screenshots of the real page: Tune pre-populated, Review, read-back, Restore)
1. **Tune grid, Simple scope:** a read point with `oc_point: true` (e.g. EC1 P2 = 7 = 48× after an OC ratio 48) must render as the **OC point**
   ("Point 7 · OC"), never as an extra "fused · offset only" column; the union of ratios across units must not invent fused points.
2. **Plan caveat texts** (`controls/model.py` / `report.py` / `ops_tune.py` notes): update stale wording with the measured facts in
   `docs/knowledge/tuning-results.md` (2026-10-11/12): per-point writes on Linux are MEASURED (read back, VID moved); arltuner **does** model
   V(n) ≥ V(n−1) (vfmodel clamp, CLAMPED warnings) though it cannot read lower fused voltages; the "baseline 0x0 cannot be restored" note is true
   only for an OC point (no-OC-point words revert exactly, G-124). Keep evidence tags. Fewer, shorter notes are better.
3. **"Some words were not read in this helper session":** do not warn when the words come from `tune.last` of this boot (restored read); warn
   only when the plan truly assumed words.
4. **Restore/plan before-after table:** the unit cell is blank for some rows (Restore dialog, P7 pt 6) — always show the unit id.

## Read first
`docs/knowledge/tuning-results.md` (2026-10-11/12 sections) · `web/app.js` (Tune grid, plan dialog) · `src/arltuner/controls/model.py`, `report.py` ·
`src/arltuner/helper/ops_tune.py` (notes, `words_assumed`) · `tests/web/*`.

## Owns
`web/*`, `tests/web/*`, `src/arltuner/controls/model.py`, `src/arltuner/controls/report.py`, `src/arltuner/helper/ops_tune.py` (notes + assumed-words
flag only), tests for these.
