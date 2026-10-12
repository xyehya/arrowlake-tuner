# G-115 — UI: show the CPU facts (SPEC-v3 §9) on Curves and Tune

- **Status:** ready · **Host:** lnx · **Tier:** T0 (fake helper/sim) · **Branch:** `task/G-115-cpu-facts-ui` · **Depends on:** v3 merged; parallel G-114

## Goal
Make the page teach and enforce the real V/F model (SPEC-v3 §9) using the contract 9 addendum shapes.

## Read first
`docs/SPEC-v3.md` §3, §4, §9 · `docs/ARCHITECTURE.md` contract 9 + 2026-10-11 addendum · `docs/knowledge/platform-285k.md` §3–5 · `web/` · `tests/web/`.

## Owns
`web/*`, `tests/web/*` (update `fake_helper.py`/`fake_v3.py` to the addendum shapes and the F1 tables — no invented per-core tables).

## Requirements
- **Tune grid (F1/F2):** columns are the unit's real points from `tune.read` (P 8…57×, E 8…46×) plus an **OC point** column (P2 = 0: ratio +
  adaptive V target; shows "not set" when 0; P0 shows BIOS 57×). E row note: "47–48× run on point 6 (turbo table); an E OC ratio adds an OC point".
- **Per-point editor (F4):** under each dropdown show the predicted effective V (`effective_v`) and a "clamped to Vn−1" badge when `clamped_by`;
  preview chart draws the clamped prediction (step where clamped). Points above a core's reach (56/57× on non-favored) shown dimmed "not reachable
  on this core" but editable (they still shape the table).
- **Turbo editor (F5):** P/E bucket tables (active cores → ratio) as a small step chart + dropdowns; non-increasing enforced in the UI (a higher
  value than the bucket to its left is not offered) and `TURBO_ORDER` refusals explained.
- **VccIA panel (F6):** "Predicted VccIA ≈ X V, set by <unit> at <ratio>×" with the contributors listed (P max, E max, ring 1.20 V) and the note
  that lowering others won't lower Vcore.
- **Fused (F7):** Tune shows each unit's fused top point (0x09) next to its OC point; Curves marks the baseline curve "stock = fused table as
  applied" and draws the 0x09 top-point value as a marker to cross-check.
- **Warnings (F8):** E 40× offset ⇒ red warning; E target > 1.2 V ⇒ WHEA warning; positive offsets ⇒ amber.
- **Curves (F3/F9):** unreachable points shown as hollow markers with the reason on hover (`max_turbo`, `cpufreq_granularity`, `active_cores`).
- Keep all G-110/G-113 constraints (CSP, ops allowlist, accessibility). Typed nonce confirm stays as is (operator decision pending).

## Acceptance
Suite green; screenshots (headless Chrome, sim/fake) of Tune per-point with a clamp, turbo editor, VccIA panel, Curves with reasons; saved to the session scratchpad.
