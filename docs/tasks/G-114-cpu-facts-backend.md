# G-114 — backend: CPU facts (SPEC-v3 §9) in sim, measurement, tune.read and tune.plan

- **Status:** ready · **Host:** lnx · **Tier:** T0 (simulator only) · **Branch:** `task/G-114-cpu-facts-backend` · **Depends on:** v3 merged

## Goal
Make the backend respect SPEC-v3 §9 F1–F9 and the contract 9 addendum (2026-10-11): real point tables in the simulator, fused top point and turbo
tables in `tune.read`, clamp/VccIA/turbo-order in `tune.plan`, max-turbo ratios + reachability reasons in `vf.measure`, and fix `mem.read`.

## Read first
`docs/SPEC-v3.md` §3, §4, §9 · `docs/ARCHITECTURE.md` contract 9 + its 2026-10-11 addendum · `docs/knowledge/platform-285k.md` §2–5, §7 ·
`docs/knowledge/tuning-results.md` (stock measured curve) · `docs/knowledge/memory-mchbar.md` (Frequency) · `src/arltuner/msr/` (simulator) ·
`src/arltuner/helper/ops_tune.py`, `ops_measure.py` · `src/arltuner/telemetry/mem.py` · `src/arltuner/vfdump.py`.

## Owns
`src/arltuner/msr/` (simulator tables only), `src/arltuner/helper/ops_tune.py`, `src/arltuner/helper/ops_measure.py`, new `src/arltuner/vfmodel.py`
(pure: clamp, interpolation, VccIA prediction, turbo-order check), `src/arltuner/telemetry/mem.py` (cross-check fix), `src/arltuner/vfdump.py`
(measure ratio sets / reasons), `policies/advanced-2026-10-11.toml` (allow entries only if a read is missing — report it), `src/arltuner/web/server.py`
(OPS arg schema only if needed), tests under `tests/` for these. **Not** `web/`, `guard/` (G-115 owns the page).

## Requirements
- **Sim (F1/F2/F3):** P tables identical on all 8 cores (8,20,30,48,53,55,56,57), E 8,15,23,30,40,46; OC word P0 = `0x39`, others 0; 0x09 fused
  words = the table in platform-285k §4; 0x1C favored 0x3939 (P0, P3) / 0x3737 / E 0x2E30; 0x1AD `0x3737373737373939`-style buckets as measured
  (P 57,57,55,…; E 48,48,47,46,…, see tune.read readings), 0x1AE/0x651 counts. Sim VIDs ≈ the measured stock curve (tuning-results.md) and obey
  F4 (clamp) and interpolation; non-favored cores cannot exceed 55×; E 23× and P 52× not pinnable (land on neighbours).
- **tune.read:** add `fused` (12 guarded 0x09 reads) and `turbo` per the addendum; count them in the op estimate the page shows (124 → ~136).
- **tune.plan:** per unit/point `effective_v` (baseline curve VID + offset, then F4 clamp up to V(n−1)), `clamped_by`; `vccia` prediction (F6:
  max allowed VID over units at the relevant active-core counts + ring 1.201 V, + 0.105 V; label it a prediction); warnings for clamp, E 40× offset
  (F8), E target > 1.2 V (F8), positive offsets; `REFUSED TURBO_ORDER` when any table would increase with active-core count (F5). No baseline curve
  ⇒ effective_v null + warning "measure a stock baseline for predictions".
- **vf.measure (F3/F9):** quick set adds the unit's max turbo ratio(s) above the top point (E 47, 48); `unreachable` gets `reason`
  (`max_turbo` from sysfs `cpuinfo_max_freq`/per-core highest, `cpufreq_granularity` when the cap lands on a neighbour, `active_cores` for 56/57× on
  favored cores when other load is present). Investigate in sim why favored 57× could fail to settle; keep other units idle while measuring one.
- **mem.read:** the 0x13D98 decode must follow `memory-mchbar.md` exactly (component × factor × BCLK; today 3600 MHz) — fix the ×2 disagreement.

## Acceptance
Suite green; tests for each F-rule (clamp examples incl. the measured P0 57× case, turbo order refusal, VccIA set_by, E 40× warning, sim tables,
fused read ⇒ values of §4, cross-check agrees on the fixture). Report exact contract shapes added.
