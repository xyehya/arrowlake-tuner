# G-139 — Tune page polish after the live check of G-133

- **Status:** in progress · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-139-tune-polish` · **Depends on:** G-133 merged

Found by the orchestrator on the reference machine in Chrome (2026-10-12):
1. **First visit after login shows no rows** (status text empty, only the Read button) although `tune.last` is available; a page reload
   shows "Showing the read kept by the helper". Find the race (login redirect → Tune tab before the helper/SSE is ready, or `tune.last`
   requested once and never retried) and fix it so the kept read appears on the first visit. Test it.
2. **Point cells above a core's limit are wider** than the others (P1/P2 points 7/8 "above this core's 55× limit": the tag text widens the
   cell). Keep every point cell the same width; wrap or shorten the tag ("above 55× limit"), full text in the title.
3. **EC2-like retained target** (ratio = fused top, target kept in the word): the disabled adaptive-voltage dropdown shows "1.1367 V · now",
   which reads as active. Show "not set" (disabled) unless hex view is on, where it shows the retained value with "(inactive)".
4. The Tune read-cost text ("about one operation per V/F point of each unit") and any ~132 estimate: G-137 adds 0x01/0x28 per kind and
   0x1C per E cluster (~140 on 8P+16E). Compute the shown estimate from the live topology, not a number.

## Owns
`web/*`, `tests/web/**`. No backend changes.

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green.
