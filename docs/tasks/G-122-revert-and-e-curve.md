# G-122 — follow-ups of G-120: revert words, E whole-curve, durable "tuned this boot"

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-122-revert-and-e-curve`

## Goal
1. **Revert gap:** `guard._revert_vf_word` fills a zero V target from the fused voltage; for a no-OC-point baseline like `0xFEC00039` the revert
   would be `0xFEC5B439`, which the hardware drops ⇒ read-back trip. Use `controls.planner.kept_word` / `has_oc_point` so a revert writes the word
   the hardware keeps; test it.
2. **E whole-curve offset:** must NOT create an OC point. Today it builds the XTU word at ratio 48 (E turbo) ⇒ an OC point at 48 appears
   (measured: OC ratio above 46 adds P2 = 7). Whole-curve offset on a unit with no OC point = OC word `ratio_current_or_top_fused | offset<<21`,
   target 0 (D-035), for E as for P; an OC ratio is only written when the user sets one.
3. `XTU_SHAPE_CAVEAT` in `controls/model.py`: update the wording (zero target now sent in the D-035 case).
4. `tuned_this_boot` durable: derive it from the audit log (any `0x11` write result this `boot_id`) at helper start, so a restart keeps `stock`
   tagging honest.

## Read first
`docs/DECISIONS.md` D-035 · `docs/knowledge/tuning-results.md` (2026-10-11 write section) · `src/arltuner/guard/guard.py` (`_revert_vf_word`) ·
`src/arltuner/controls/planner.py` (`kept_word`, `has_oc_point`, `_vf_words`) · `src/arltuner/controls/model.py` · `src/arltuner/helper/ops_measure.py`
(`_tuning_state`) · `src/arltuner/guard/audit.py`.

## Owns
`src/arltuner/guard/guard.py` (revert only), `src/arltuner/controls/`, `src/arltuner/helper/ops_measure.py` (`_tuning_state` + start-up derivation),
`src/arltuner/helper/core.py` (flag init only), tests for these. **Not** stress files (G-121).

## Acceptance
Suite green; sim: revert of a `0xFEC00039` baseline restores it exactly; E whole-curve −10 mV leaves the OC ratio 0 and adds no point; restart keeps
`tuned_this_boot`.
