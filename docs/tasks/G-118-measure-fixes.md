# G-118 — measurement fixes from the first real runs (2026-10-11)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-118-measure-fixes` · **Depends on:** G-114 merged

## Goal
Fix what the real Quick runs showed (`docs/knowledge/tuning-results.md`, 19:06 UTC section):
1. **Max ratio per unit from the turbo tables, not `cpuinfo_max_freq`:** P unit max = its favored ratio (0x1C `[7:0]` via plain rdmsr? no — use
   the turbo table top bucket for favored P0/P3 = 57, others = their 0x1C-reported favored value read through the existing tune.read cache when
   present, else `cpuinfo_max_freq/100` for P); **E max = max(0x650 buckets)** (48 today) — E `cpuinfo_max_freq` is 4.9 GHz because of the
   intel_pstate N→N−1 mapping, so 49× must not be offered. Read 0x650/0x1AD by plain rdmsr (already allowlisted for telemetry? if not, reuse
   the helper's tune read path / TelemetryMsr allowlist per D-026 — report what you chose).
2. **Retry pass:** points that end `mismatch` or `unreachable/active_cores` get one second attempt at the end of the run (same unit, all other
   units idle), with the longer sampling window; keep the better result; `kept/total` reflect the attempt reported.
3. **Baseline:** `vf.measure` arg `baseline?: bool` marks the new record baseline when complete (unsets the old one); a record measured right
   after a complete `tune.read` with all offsets 0 gets `tuning_state.kind = "stock"` (contract 9 allows it).
4. **Points table in the record:** each point gets `fused_point: int|null` (1-based index in the unit's fused table when the ratio is a fused
   point, `"oc"` for the OC ratio) so the UI can mark fused vs in-between ratios.

## Read first
`docs/knowledge/tuning-results.md` (2026-10-11 sections) · `docs/knowledge/platform-arl-s.md` §3 · `docs/ARCHITECTURE.md` contract 9 + addenda ·
`src/arltuner/helper/ops_measure.py` · `src/arltuner/vfdump.py` (measure) · `src/arltuner/curves/` · `src/arltuner/telemetry/msrio.py`.

## Owns
`src/arltuner/helper/ops_measure.py`, `src/arltuner/vfdump.py` (measure part), `src/arltuner/curves/`, `src/arltuner/telemetry/msrio.py` (allowlist
additions only if needed, D-026 rule: read-only MSRs), `src/arltuner/web/server.py` (vf.measure arg schema only), tests for these. **Not** `web/`,
`controls/`, `ops_tune.py`, stress files.

## Acceptance
Suite green; sim tests: E dense stops at 48, no 49; retry recovers an intermittent 57×; baseline flag moves; stock tagging; fused_point labels.
