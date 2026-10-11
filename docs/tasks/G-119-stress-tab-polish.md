# G-119 — Stress tab + polish (one-click Apply, chart spans, small backend gaps)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-119-stress-tab-polish` · **Depends on:** G-116, G-117 merged; G-118 in flight

## Goal
Finish the XTU-style loop in the page: **Tune → Stress (validate) → keep**.
1. **Stress tab** (contract 9 "G-116" addendum): CPU screen (method integer / vector-math / cache), memory screen (stressapptest MB), units
   picker (mini core map), optional ratio cap, duration presets (1/5/15/60 min + custom 30–3600 s), Start/Stop, live view (elapsed ring, errors,
   per-unit achieved MHz, temp/power from telemetry), result card (stopped_by, errors + error lines, cap_valid, MCE count, health) and a history
   table from `stress.list`. Persistent banner: **"Quick screen — NOT an acceptance gate (D-019: Linpack 2021 60 min + SSE 60 min)."** No "pass"
   wording (SPEC §9.3): say "no errors detected in N min". After a Tune apply the page offers "Validate with a stress screen".
2. **One-click Apply (D-033 d):** the review dialog's Apply button sends `typed: "CONFIRM " + nonce` itself; no typing. Keep: diff, warnings,
   ops, nonce single-use, expiry, the Apply button disabled while the plan is loading/stale.
3. **Charts:** minimum y-spans so 1 °C / 1 W noise doesn't look like a cliff (temps ≥ 10 °C, power ≥ 20 W, MHz ≥ 500, V ≥ 0.05 V, GB/s ≥ 2).
4. **Curves:** use `fused_point` (G-118: int 1-based or "oc") when present to mark fused points (filled) vs in-between ratios (small), and
   `tuning_state.kind == "stock"` ⇒ "stock = fused table as applied" badge; defensive when absent.
5. **Backend small gaps:** `profiles/schema.py` accepts an empty-offsets profile for turbo-only intents (remove the `object.__setattr__` bypass in
   `ops_tune.py`); executor counts plain-MSR writes in `ops_used` (report it as `msr_writes` separately from mailbox `ops_used`).

## Read first
`docs/SPEC.md` §9 · `docs/SPEC-v3.md` §1, §4 · `docs/ARCHITECTURE.md` contract 9 + addenda (incl. "G-116") · `web/*` · `tests/web/*` ·
`src/arltuner/helper/ops_stress.py` (result shapes) · `src/arltuner/profiles/schema.py` · `src/arltuner/controls/executor.py` · `src/arltuner/helper/ops_tune.py`.

## Owns
`web/*`, `tests/web/*`, `src/arltuner/profiles/schema.py`, `src/arltuner/controls/executor.py` + `report.py` (counting only), `src/arltuner/helper/ops_tune.py`
(bypass removal + `msr_writes`), their tests. **Not** `ops_measure.py`, `curves/`, `vfdump.py` (G-118).

## Acceptance
Suite green; screenshots (fake helper / sim GUI) of Stress idle, running, result, history; Tune review dialog with one-click Apply; charts with
small noise; saved as `.../scratchpad/s-*.png`.
