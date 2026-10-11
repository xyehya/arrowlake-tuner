# G-112 — D-032 advanced policy + helper tune ops (read / plan / apply / reset / restore / profiles)

- **Status:** ready · **Host:** lnx · **Safety tier:** T0 (simulator only) · **Branch:** `task/G-112-policy-tune-ops`
- **Depends on:** main @ SPEC-v3 commit. Parallel with G-111 and G-113.

## Goal
Make the D-032 limits real and expose tuning through the root helper (contract 9 `tune.*`, `profiles.*`), reusing the existing controls/profiles
plan-apply engine (the CLI `apply`/`point-offset` path) — no new mailbox logic. SPEC-v3 §4–6.

## Read first (only these)
`docs/DECISIONS.md` D-015/D-016/D-017/D-029/D-032/D-033 · `docs/SPEC-v3.md` §4–6 · `docs/ARCHITECTURE.md` contracts 5, 6, 8, 9 + T-023/T-025/T-028
updates · `docs/knowledge/registers.md` (V/F data word, voltage semantics) · `src/arltuner/guard/policy.py` · `src/arltuner/controls/` ·
`src/arltuner/profiles/` · `src/arltuner/helper/` (ops: `op_vf_dump`, `write_gate`; core; state) · `policies/session-2026-10-02-points.toml`.

## Owns
`src/arltuner/guard/policy.py` (sanity offset ceiling 100 → 200 mV; nothing else loosened in code), `policies/advanced-2026-10-11.toml` (new),
`install.sh` (seed source = the advanced policy; `--reseed-config` behaviour unchanged), new `src/arltuner/helper/ops_tune.py`, new
`src/arltuner/profiles/store.py` (root store `/var/lib/arltuner/profiles/`), `src/arltuner/controls/` (adapters only if needed), `src/arltuner/helper/ops.py`
(**only** registering your ops in `builtin_ops`), `src/arltuner/web/server.py` (`OPS` entries for your ops only), `tests/guard/`, `tests/policy*`,
`tests/helper/test_tune*.py`, `tests/profiles/`, `tests/install/` (seed-source assertions only).
**Not:** `curves/`, `telemetry/`, `helper/state.py`, `helper/core.py` telemetry, `web/` (G-111/G-113).

## Requirements
- **Policy file** = D-032 numbers exactly (copy the allow entries of the session-2026-10-02 policy, extend P2 ranges as the per-point and OC-point
  writes need, ratio/turbo-table entries per T-023). The guard still validates every request; one transaction at a time; health gate; audit.
- **D-029 carried issue:** a non-long write op must go through `machine.begin` (state `applying`) — fix it for every write op you add.
- **`tune.read`** = `vf.dump` + decoded per unit/point `{ratio, offset_mv, v_target_v, mode}` (registers.md V/F data word; mV = raw·1000/1024).
  Set the helper's "words in force" hook (coordinate name with G-111: `helper.last_words`, a dict cpu → [int], set after a complete read).
- **`tune.plan`**: intent ⇒ a `Profile`/targets for the existing planner (simple scope = same values for all P / all E units; per-unit; per-point
  or whole-curve offsets; OC-point adaptive V target only on the OC point; ratio incl. the T-023 turbo-table/favored-core writes). `{profile}`,
  `{reset: "stock"}` (offsets 0, OC ratio/V target back to the read fused/current state), `{restore: "accepted"}` (profiles/CURRENT chain).
  Result = contract 8 plan + `before`/`after` + `warnings`; range violations ⇒ `REFUSED POLICY_RANGE`.
- **`tune.apply`**: contract 8 apply (nonce, expiry, single use, preflight, write gate) ⇒ steps + read-back compare.
- **`profiles.list/save`** per contract 9.

## Acceptance
`python3 tools/run_tests.py` green. Simulator tests: simple/per-unit/per-point/whole-curve intents ⇒ expected words; OC-point-only V target;
±200 mV accepted, ±205 refused; ratio > policy refused; nonce replay/expiry/stale plan; reset/restore plans; read-back mismatch reported; the
existing CLI tests still pass (CLI behaviour unchanged except the new limits when a policy sets them). No hardware, no sudo.

## Report-back (≤ 300 words)
Ops + any contract deviation (exact) · policy diff vs session-2026-10-02 · files · test count · open questions.
