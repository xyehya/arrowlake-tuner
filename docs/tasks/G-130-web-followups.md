# G-130 — web follow-ups to G-127/G-128

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-130-web-followups` · **Depends on:** G-127 merged (G-128 in review, contract below)

## Goal
1. **Apply race:** with a fast helper (`apply_delay=0.01` in `tests/web/fake_v3.py`) the review dialog stays at "Applying" — the `tune.apply` 202
   reply and the SSE `result` event race (result arrives before the page registers the id?). Find and fix in `web/app.js` (buffer results by id
   that arrive before the page knows the id, or equivalent). Test with delay 0 and 0.01 (browser test).
2. **Preflight `"warn"` status** (G-128: unknown CPU stepping → a check with status `"warn"`; mode unchanged): render it on System/preflight and
   any badge as a warning (amber), not unknown/fail. Fake emits one in a test.
3. **VccIA from G-128:** `tune.plan` `vccia.ring` and `margin_v` are now `null`, label text changed. Fake mirrors that; the page must not show
   "null"/NaN; tests.
4. **Unit lists from topology:** replace the fixed `UNIT_IDS` (8 P + 4 E, `8+4n` cpus) with the `preflight.topology.units` list (fallback to the
   current fixed list only when topology is absent). All tabs (dashboard map, Curves, Tune, Stress, Profiles) iterate the live list. Fake test with
   6P+8E (EC0 = cpus 6–9, EC1 = 10–13) renders 8 units everywhere, no errors in console.

## Read first
`web/app.js`, `tests/web/fake_v3.py`, `tests/web/test_tune_live_truth.py`, `tests/web/v3_harness.py`.

## Owns
`web/*`, `tests/web/*`.

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green; the new tests above.
