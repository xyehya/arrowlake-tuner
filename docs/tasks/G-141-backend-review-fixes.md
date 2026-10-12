# G-141 — backend fixes from the code review (2026-10-12)

- **Status:** in progress · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-141-backend-review-fixes`

Each item was verified by a read-only reviewer on the simulator (SIM) or by reading. Fix, test (a regression test per item), keep diffs focused.
1. **Plan vs guard ratio context** (`controls/planner.py:~869,~899`): `build_plan` builds each write's context from the target CPU's own 0x1C
   word; `_ratio_limit` / the guard use the max over the kind. With `ratio_max = "auto"` a non-favored core is offered 58–60 by the page and
   `tune.plan`, then `tune.apply` is refused (nonce burnt). Fix the planner to use the kind-wide words (`favored_words` + baseline rows,
   filtered by kind), like the guard.
2. **`_read_extras` pacing** (`helper/ops_tune.py:~533-610`): mailbox reads back-to-back; with `min_dwell_s > 0` every extras group fails
   (DWELL) ⇒ auto ratio = no raise, `hw_limits` empty. Pace like `op_vf_dump`/executor. Word the notes at ~574/~605 by `exc.reason`, not "no
   allow entry".
3. **Measure journal overwrite** (`helper/ops_measure.py:~828` `_write_journal`): recover a stale journal first, refuse (STALE_JOURNAL, like
   `ops_stress.py:607`) if it still exists; never record capped values as "original".
4. **Applied turbo tables not folded** (`ops_tune.py:~1919` `_merge_applied_words`): fold the `WRITE_MSR` read-backs into `st.tables`,
   `st.turbo` and the stored last read, so tune.last / the next plan / predictions see the applied table.
5. **`favored_words` not restored after a helper restart** (`_restore_last` ~808-842 → `st.favored_words`), so plan-time checks match apply.
   Also: `_last_restored` set before the restore finishes (race with a concurrent `tune.plan`) — fix the ordering.
6. **Hidden ceilings (D-039)**: `ops_tune.py:111, ~946-963` (ratio/turbo ≤ 100, V target ≤ 2.0 V) and `profiles/schema.py:74-75,85`
   (ratio ≤ 100, OC V ≤ 2000 mV) → the encoding bounds (ratio 255, V 3999 mV, turbo 1..255). One shared constants module (dedupe
   `_RATIO_RANGE`/`RATIO_RANGE`). Errors name the source.
7. **BIOS-offset default** (`guard/policy.py:128` `DEFAULT_ASSUMED_BIOS_OFFSET_MV = {P: -50}` = the operator's old BIOS value): default 0 for
   both kinds; `policies/default.toml` states `assumed_bios_offset_mv = { P = 0, E = 0 }` explicitly with a one-line comment. Also D-036
   residue: `_E_CPUS_DEFAULT` (285K layout) for an unbound policy and `BudgetMeter._budget_locked` calling `default_m1_policy()` without a
   topology (`helper/ops.py:~287`) — bind to the live topology or fail visibly.
8. **Accept from a stale read** (`helper/ops_accept.py:~160,~197`): `profiles.accept {from:"read"}` must refuse when the audit log shows a
   0x11 / MSR write after the read's timestamp ("read again first"), and must include the turbo tables of the read (not `None`). Trace and
   fix the `profiles.accept` vs `tune.apply` concurrency (accept must not run while an apply holds the device; refuse BUSY).
9. **New op `profiles.fingerprint`** (operator safety, G-138 follow-up): writes `fingerprint.json` into the EXISTING accepted dir from the
   current fused 0x09 words of this boot's read, without touching `CURRENT`, profiles or `EXPECTED-words.txt`. Refuses if there is no accepted
   state or the read lacks a unit's 0x09 word. Add it to the web server's op allowlist (`src/arltuner/web/server.py` OPS — one line; G-142
   owns that file otherwise, keep it to that line) and document in ARCHITECTURE. The page button is added by the orchestrator.
10. **Audit log mode 0600** everywhere it can be created: `helper/ops.py:~625`, `helper/ops_tune.py:~420`, `guard/audit.py:~139` (open with
    0o600 + fchmod), matching `install.sh` and the self-check.
11. **Stale text**: `install.sh:~264`, `uninstall.sh:10,31` say "cap 600" → the op counter is an indicator (100000 default).
12. **Dev `--sim` apply after restart** refuses with DWELL "in the future" (sim clock restarts, audit keeps wall time): make the simulator's
    clock start at wall time (or the guard compare on the same clock). Test apply-after-restart.
13. Small: `winport.py:~483` swallowed affinity-restore failure (log/raise); `ops_tune.py:~2124` dead `created = None`; boot_id unknown treated
    as zero used (`ops_tune.py:~1941-1958, ~257, ~400-410`) → treat as unknown, not zero.

## Owns
`src/arltuner/**` except `src/arltuner/web/**` (one OPS line allowed), `policies/default.toml`, `install.sh`/`uninstall.sh` text lines of item 11
only, `tests/**` (not tests/web), `docs/ARCHITECTURE.md`. NOT `web/*`, `tools/*` (G-142), not `docs/guide`.

## Acceptance
`python3 tools/run_tests.py` green.
