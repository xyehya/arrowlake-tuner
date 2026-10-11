# G-123 — UX continuity: last read per boot, profile contents, indicator budget

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-123-ux-continuity` · **Depends on:** G-121 merged; parallel G-122

## Goal (from the 2026-10-11 real-data screenshot pass)
1. **Last read per boot:** a complete `tune.read` is stored root-owned (`/var/lib/arltuner/state/last-read.json`, 0600, contract-8 file rules)
   with `boot_id` + time; new op **`tune.last {}`** ⇒ the stored result if its `boot_id` is the current boot, else `{available: false}`. The helper
   restores `last_words` from it lazily (first tune.* call) so `tune.plan` works after a helper restart without a new read. The page calls
   `tune.last` on load: Tune is pre-populated ("read at HH:MM this boot · Re-read"), the applied chip shows real state.
2. **Profile contents:** new op **`profiles.get {name}`** ⇒ `{name, source, intent-like contents: per unit/point offset_mv, ratio, v_target_v,
   turbo}` for saved profiles and for the installed accepted chain (`/etc/arltuner/accepted/` CURRENT + profile files, trust-checked). Profiles
   tab: show the accepted chain in order (e.g. `e-p6-split3 → p-boot-ramp → p-step7`), per-profile summary (units touched, deepest offset),
   working compare (table + Δ), and **Load into Tune** (fills the editor; nothing written until Review → Apply).
3. **Budget = indicator (D-032):** System + Dashboard card show "Mailbox ops this boot: N" (+ small sparkline of ops over the session), no
   countdown ring of 100000; keep the per-boot cap visible as a small note only.
4. Read estimate text on Tune: use the real count (112 V/F reads + 12 fused reads; show "≈ 124–136").
5. Curves: default selection = the newest **baseline** curve, else the newest complete all-units curve.

## Read first
`docs/ARCHITECTURE.md` contract 9 + addenda · `src/arltuner/helper/ops_tune.py` · `src/arltuner/profiles/` · `src/arltuner/helper/ops.py` (`_load_accepted`) ·
`src/arltuner/helper/trust.py` · `web/*` · `tests/web/*`.

## Owns
`src/arltuner/helper/ops_tune.py`, `src/arltuner/profiles/`, `src/arltuner/web/server.py` (OPS entries for new ops), `install.sh` (state dir 0700),
`web/*`, `tests/web/*`, tests for the rest. **Not** `controls/`, `guard/`, `ops_measure.py`, `core.py` (G-122).

## Acceptance
Suite green; contract 9 addendum "G-123" in `docs/ARCHITECTURE.md`; screenshots (sim/fake) of Tune pre-populated, Profiles compare + chain,
budget indicator as `.../scratchpad/u-*.png`.
