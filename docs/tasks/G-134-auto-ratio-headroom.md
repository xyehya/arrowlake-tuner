# G-134 — policy "auto" ratio ceiling = 0x1C fused max + 3 (D-038)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-134-auto-ratio-headroom` · **Depends on:** —

## Goal
`src/arltuner/guard/policy.py` `fused_ratio_cap`: the `"auto"` ceiling becomes `max(0x1C[7:0], 0x1C[15:8]) + AUTO_OC_HEADROOM` (= 3, a named
constant citing D-038), still clamped to `SANITY_RATIO_MAX`, still `None` when no 0x1C of the kind was read. Update its docstring, the
`policies/default.toml` header comment (ratio line), and every test that asserted the old auto value. Add tests: P words 57 ⇒ 60; E 46 ⇒ 49;
a word near the sanity bound clamps; no word ⇒ None.

## Owns
`src/arltuner/guard/policy.py` (this function + constant only), `policies/default.toml` (comment only), `tests/guard/**`, any other test that
asserts the auto ratio value. Do not touch `web/*` (G-133 owns it) or docs.

## Acceptance
`python3 tools/run_tests.py` green.
