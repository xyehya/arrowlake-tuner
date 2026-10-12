# G-137 — limits: policy up to the CPU's own maxima (D-039)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-137-limits-policy-only` · **Depends on:** G-134 merged

## Goal (read D-039 and D-032/D-038 in docs/DECISIONS.md)
1. `src/arltuner/guard/policy.py`: a policy value is validated against **hardware ceilings**, not code constants:
   - `ratio_max.<K>` ≤ the kind's cmd **0x01 [7:0] fused max OC ratio** read in the session (domain per registers.md 0x01; find which P1 the
     E-cores answer under, from registers.md / the simulator / `msr/measured_baseline.py`; if unknown, the P-core domain value with a
     `[UNVERIFIED]` comment). Unread ⇒ the value is accepted at load and enforced at request time as "no raise" (fail closed), like auto today.
   - `v_target_max_mv.<K>` ≤ cmd 0x28 max − 1 LSB of the kind (already read); `"auto"` unchanged.
   - `offset_ceiling_mv` ≤ 999, `effective_offset_floor_mv` ≥ −1000 (11-bit field / XTU range, registers.md line 29).
   - step ranges: ratio step 1…255 (encoding), V step and offset step 2…1000 mV.
   Keep the old `SANITY_*` names only if other code needs them, renamed/documented as **encoding bounds**, never as the effective cap.
   "auto" (D-038) keeps fused 0x1C + 3, now clamped by the 0x01 value instead of a constant.
2. Read **0x01** for P and E in the session where 0x28/0x1C are read (allowlist already has `oc_capability_read`; one transaction at a time).
   Expose per kind `hw_limits = {ratio_max_oc, v_target_max_mv, offset_mv: [-1000, 999]}` additively next to the live policy limits that G-133
   adds to the `tune.read`/`tune.last` result (coordinate by reading G-133's merged contract if merged, else add your own field
   `hw_limits` and document it in `docs/ARCHITECTURE.md`).
3. Every refusal message for a bound says the bound's **source**: `policy ratio_max P = 60 (/etc/arltuner/policy.toml; your CPU allows up to
   120× per cmd 0x01)` / `CPU limit (cmd 0x01)` / `field encoding`. CLI `plan` output: same wording.
4. `policies/default.toml` header: rewrite the limits paragraph — every number is a default, editable, with the hard ceilings named.
5. Simulator: give it plausible 0x01/0x28 answers (reuse measured_baseline values: 0x01 P1=0 → 0x778).
6. Tests: policy with ratio_max P 90 loads, is enforced ≤ 0x01 value, refused above it with source text; offsets −500 accepted when policy
   allows; unread 0x01 ⇒ no raise; messages name the source.

## Owns
`src/arltuner/guard/**`, `src/arltuner/helper/ops_tune.py` (session reads + `hw_limits` only), `src/arltuner/helper/sim*.py` /
`src/arltuner/msr/measured_baseline.py` (sim answers), `src/arltuner/cli.py` (limit wording only), `policies/default.toml`, `tests/**`
(not tests/web), `docs/ARCHITECTURE.md` (contract addendum). Not `web/*` (G-133).

## Acceptance
`python3 tools/run_tests.py` green.
