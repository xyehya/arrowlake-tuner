# G-133 — Tune page: OC ratio model, live policy limits, clearer labels

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-133-tune-oc-model-labels` · **Depends on:** G-132 merged

## Operator feedback (2026-10-12, after "Re-read ratios & voltages"; summarised)
1. "Simple" / "Per unit" are not concise → **All-Core** / **Per-Core**.
2. "Whole curve" → **Whole Curve Offset**; "Per point" → **Per V/F point Offsets**.
3. Per-Core: the logic of the two top dropdowns is unclear. Use the technical names. **OC ratio** = a ratio above what the unit runs at stock
   (favored P > 57×, non-favored P > 55×, E > 46× on this chip — read live, never fixed). With no OC ratio configured the **adaptive voltage**
   cannot be set (it belongs to the OC point only; other ratios are offset only). Empty box → "No OC ratio configured". The ratio dropdown
   today offers only 57× ⇒ no control over any OC ratio.
4. E-clusters: the cards do not reflect what the clusters run (all at 48× from the BIOS / turbo table).
5. Turbo-ratio-limit text does not belong in the per-core/per-cluster cards; keep it in the turbo section.

## Evidence (live read, fixture `tests/web/fixtures/tune-last-285k-2026-10-12.json` = `tune.last` on the reference machine [MEASURED lnx 2026-10-12])
- Every P-core has 8 fused points (8, 20, 30, 48, 53, 55, **56, 57**); the fused top (0x09) is 57× on **every** P-core. On non-favored cores the
  56/57× words are flat placeholders ≈ the core's 55× VID (`docs/knowledge/favored-cores.md` §2). `turbo.favored` (0x1C) = P0/P3 57, others 55.
- E: 6 fused points (8…46), fused top 46×. `tables.e` = 48×8 (that is the "48×"). OC words: EC0/EC3 0 (none), **EC1 48× + 1.1367 V** (OC point
  7 exists), **EC2 46× + 1.1367 V** (= fused top ⇒ no OC point; target retained in the word, inactive). Above 46× a cluster with no OC point runs
  on point 6's voltage (`tuning-results.md` F2). P0 OC word 57× (= fused top, no OC point; the hardware drops a target there, line 109, D-035).
- An OC ratio cannot be cleared to 0 at runtime (`tuning-results.md` line 112); a cold boot clears it.
- **`POLICY` in `web/app.js:1193` is a hard-coded literal** (ratioMax P 57 / E 49, vtMax, offsets) — a D-036 violation. The ratio dropdown range
  (`buildRows`, ~2885) is floor = current ratio or top point, ceiling = that literal ⇒ P shows only 57×; E at 48 cannot go back.

## Goal
1. **Live limits (D-036).** The page takes offset bounds, V-target max per kind and ratio max per kind from the helper's live, bound policy
   ("auto" resolved), not from a literal. Add them additively to an existing response (`tune.read`/`tune.last` result, or preflight) — your
   choice; document in `docs/ARCHITECTURE.md`. Without them (old helper) the page shows the controls disabled with a one-line reason, never a guess.
2. **Labels:** scope segmented control **All-Core** / **Per-Core** (keep internal values); offset control **Whole Curve Offset** / **Per V/F point
   Offsets**. Update every visible string, aria-label and the scope note that uses the old names; the user guide table (`docs/USER-GUIDE.md`).
3. **OC model, ratio first.** Column order: Unit · Voltage offset · **OC ratio** · **Adaptive voltage (OC point)** · Undo. Per unit, with
   `stockMax` = the unit's 0x1C ratio (`turbo.favored` for P; for E the fused top) and `fusedTop` = its top fused point ratio:
   | effective OC ratio | OC box / hint text | adaptive voltage control |
   |---|---|---|
   | none (word 0) or ≤ stockMax | "No OC ratio configured" | disabled, title "set an OC ratio first: the adaptive voltage belongs to the OC point" |
   | P only, stockMax < r ≤ fusedTop | "OC ratio r× · uses fused point n (offset only)" | disabled, same reason |
   | r > fusedTop | "OC point n · r× · adaptive V" | enabled |
   Dropdown: first option "No OC ratio" (selectable only while the unit has none; if one is set, show it disabled with title "an OC ratio cannot
   be cleared at runtime; a cold boot clears it"), then stockMax+1 … live ratio max. When live ratio max ≤ stockMax (e.g. P with policy 57) show
   the dropdown disabled with a muted hint "OC ratios above N× are not allowed by your policy (`/etc/arltuner/policy.toml` ratio_max)".
   Choosing an OC ratio above fusedTop with no target set: the plan must not send a zero target (D-035) — prefill the target with the fused
   top voltage (0x09) rounded to the dropdown grid and mark it changed, so the user sees what will be written. EC2-like words (target retained,
   ratio = fused top) render as "No OC ratio configured" with a muted "(word holds 46× + 1.137 V, inactive)" only when hex view is on.
   All-Core rows: the same rules over the group; "mixed" stays shown-never-chosen.
4. **Header line** per unit, plain: `offsets −35…0 mV · OC ratio: none · runs up to 48×` (runs-up-to = min(live max ratio of the unit from the
   turbo table bucket 0 and its 0x1C/OC ratio), tooltip says where it comes from). Drop "target n/a · ratio n/a" and the fused-top text from it.
5. **Remove from per-unit panels:** the E turbo note (`eTurboNote`), `ratioHint` turbo text in the ratio cell, the "fused top point (0x09)"
   line (move it to the OC box tooltip). Replace the E note with one V/F fact when the cluster has no OC point and runs above its top point:
   "47–48× run on point 6's voltage (no OC point)". Turbo text stays in the turbo section only.
6. **Points above a P-core's stockMax** (56/57× on non-favored): tag "fused · offset only · above this core's 55× limit", muted, still editable,
   no predicted voltage; no claim about whether the core can run them (open question, `favored-cores.md` §5).
7. **Interpolated predictions:** a point the marked baseline did not measure (e.g. E 23× on a pre-G-132 baseline, status unreachable) is
   predicted by linear interpolation between the baseline's measured neighbours (monotonic clamp), labelled "(interpolated)", instead of
   "no measured voltage". Applies to old and new baselines.

## Owns
`web/*`, `src/arltuner/helper/ops_tune.py` (or wherever `tune.read`/`tune.last` builds its result — additive fields only), `src/arltuner/web/*`
(only if the server filters fields), `tests/**`, `docs/ARCHITECTURE.md`, `docs/USER-GUIDE.md`, `README.md` (label names only).
Do not touch the guard, the policy files, or any hardware path. No hardware access.

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green. Tests driven by the fixture: P0/P3 vs P1 dropdown ranges with live max 57 (disabled,
policy hint) and with a test policy max 60 (58–60 enabled; 56–57 middle band on P1 only); target disabled with no OC ratio; EC1 OC point enabled;
EC2 "No OC ratio configured"; EC0 header "runs up to 48×"; no turbo text inside per-unit panels; new labels; E 23× interpolated prediction on a
baseline whose 23× is unreachable; page without live limits shows disabled controls. Report: screenshots-equivalent text dump of one P-favored,
one P-non-favored and two E rows (EC1, EC2) in Per-Core mode.
