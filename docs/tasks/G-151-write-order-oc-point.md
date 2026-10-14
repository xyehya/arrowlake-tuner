# G-151 — Write order: no ratio's voltage dips during an apply/restore; restore of an accepted state that holds an OC point

- **Status:** ready
- **Host:** lnx
- **Safety tier:** T0 (worker: simulator + fake sysfs only)
- **Branch/worktree:** `task/G-151-write-order`
- **Depends on:** G-150 (merged)

## Goal
2026-10-14 the operator split the E curve live: an OC point at 48× holding the old 48× voltage (EC0 `0x00047E30` = 1.1230 V, EC1-3 `0x00047330`
= 1.1123 V) and point 6 lowered by 15 mV (EC0 `0xF9A0002E` −49.8 mV, EC1-3 `0xFAE0002E` −40.0 mV). The orchestrator applied it as **two plans** (OC
word first, then point 6) because one plan wrote **all point words first, then the OC words**: in between, 48× ran on the lowered point 6 — a
voltage never tested. If this state becomes the accepted one, every cold-boot Restore accepted (G-148 one shot) reproduces that window on all 16
E-cores. Also new: once the OC point exists the CPU reports a 7th E point word (`P2 = 7` → `0x00000030`, ratio 48, offset 0; the GUI already treats it
as the OC point, G-125); after a cold boot it does not exist until the OC word is written. Make every plan transition-safe and make restore of
such a state work in one shot.

## Fixed decisions (do not re-decide; report if one is impossible)
1. **Ordering rule (planner, all plans: GUI apply, CLI apply, restore):** within one unit, no ratio's requested voltage may go below
   min(before, after) at any point of the sequence. Implement as: **writes that raise a voltage go before writes that lower one.** An OC word that
   creates the OC point or raises its target, and a point offset increase, are raising; a point offset decrease and an OC target decrease are
   lowering. Model the voltage per ratio with the existing predictions (top point flat above it without an OC point; linear between the top fused
   point and the OC point; the CPU raises an OC target below the top point to it). Where both orders break the rule (should not happen with
   per-word steps), split further or refuse with a clear check name — never silently. `0x1D`/`0x27` follow-ups stay with their `0x11` word.
   Step-limit splitting (G-148) keeps working with the new order.
2. **The OC point's table slot** (point `n+1`, flagged `oc_point`) is never written as a fused point and never diffed as a missing point before
   the OC word exists. Compare (helper `accepted_match`, `restore_tuning.sh --check`, Windows compare, restore result): a missing `P2 = n+1`
   word on a unit whose OC word is not written yet is not a separate failure; after the OC word it must read `ratio = OC ratio, offset 0`.
   Accept: EXPECTED-words and the complete profile keep holding it as read (decide whether the profile stores it or derives it; document).
3. **Simulator:** `oc_adds_point` on for the reference model where needed; a 46×/48× split fixture equal to the live words above.

## Read first (only these)
- `src/arltuner/controls/planner.py` (`build_plan` step order, step-limit splitting), `controls/executor.py`, `controls/residual.py`
- `src/arltuner/msr/sim.py` (`oc_adds_point`, `drop_target_without_oc_point`, clamp), `src/arltuner/helper/ops_tune.py` (restore, `accepted_match`)
- `src/arltuner/profiles/` (complete profile, EXPECTED-words derivation), `tools/lnx/restore_tuning.sh`, `tools/win/restore_tuning.ps1`
- `docs/knowledge/platform-arl-s.md` (OC point, clamp, linear between top point and OC point), `docs/tasks/G-147-*.md`, `docs/tasks/G-148-*.md`

## Owns
`src/arltuner/controls/`, `src/arltuner/helper/ops_tune.py`, `src/arltuner/profiles/`, `src/arltuner/msr/sim.py`, `src/arltuner/cli.py`,
`tools/lnx/restore_tuning.sh`, `tools/win/restore_tuning.ps1`, tests for these. Not `web/`, not `docs/guide/`, not STATE/DECISIONS.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green (2912 on main).
- Simulator flows with the split fixture: (a) **cold boot** (stock words, no OC point) → Restore accepted → one shot, the E OC words are written
  before the point-6 writes, ratio 48 + the exact targets (never zero, never `vtAuto`), `0x1D` = 48 and no favored decrease, compare OK incl.
  the 7th point; (b) warm boot with the split live → 0 writes, OK; (c) **revert** to the pre-split accepted state (OC word 0, point 6 −35/−25)
  from the split → point 6 raised first, then the OC word neutralised (G-147), EQUIVALENT; (d) the same intent as one GUI plan (OC + point 6
  together) → OC word first; (e) a pure undervolt (point offsets down only) keeps today's order.
- A test asserts the rule generally: for every step prefix of the plans above, every ratio's predicted voltage ≥ min(before, after).
- Windows script: same order and the 7th-point compare rule; static checks only (no pwsh here) — say so. Check whether
  `examples/reference-285k/notes/tuning-history.md` line "`$Expected` in `tools/win/restore_tuning.ps1`" is stale (the script reads the words
  file) and report; do not edit the notes.
- No guard/policy change; no zero ratio ever sent.

## Non-goals
No hardware. No new tuning features. No GUI changes.

## Report-back (≤ 300 words)
The ordering implementation and where it lives, the 7th-point decision, test count line, anything not done as fixed.
