# G-148 — Profiles are complete and independent; the accepted state is one profile restored in one shot (D-044)

- **Status:** ready
- **Host:** lnx
- **Safety tier:** T0 (worker: simulator + fake sysfs only). The orchestrator runs the migration and the live restore after merge.
- **Branch/worktree:** `task/G-148-complete-profiles`
- **Depends on:** G-147 (merged)

## Goal (operator, 2026-10-13)
"profiles should represent discrete and independent tweakings and tunings and we shouldnt chain them and one profile can be restored in one shot."
Today the accepted state is a **chain** (`/etc/arltuner/accepted/CURRENT` lists several `profile =` lines applied in order and merged by
`_merge_profiles`), and saved profiles hold only the rows the user edited (a delta against whatever the CPU holds). Replace both with: **a
profile is a complete tuning state** (no implicit base), **the accepted state is exactly one profile**, and **Restore accepted = apply that one
profile**: read the CPU, write only the words that differ (guarded, read back), G-147 residual rule, OK / EQUIVALENT / BAD compare as the result.

## Decisions fixed by the orchestrator (do not re-decide; report if one is impossible)
1. **Complete profile** = for every unit of the CPU (each P-core, each E-cluster): the OC word fields (ratio, adaptive V target, whole-curve
   offset), the offset of **every** fused V/F point (explicit `0` included), and both turbo tables (`0x1AD`, `0x650`). Mark it in the file:
   `[meta] complete = true` (schema). A complete profile names its CPU (fused top ratio per unit or a fingerprint reference) so it is refused on a
   different topology. Applying a complete profile writes every word that differs from the live read and nothing else.
2. **The accepted profile and `EXPECTED-words.txt` hold the same information.** Pick one source and derive the other at accept time, so they
   cannot drift (recommended: the profile is derived from the verified read-back words + tables; EXPECTED-words stays as the compare file for the
   restore scripts / Windows). `CURRENT` holds exactly one `profile =` line; a second line is an error for new code.
3. **Saving** (`profiles.save`) stores a complete profile: this boot's last read merged with the editor's rows (refuse without a read of this boot
   — `NO_READ`, "read first"). **Legacy partial profiles** in `/var/lib/arltuner/profiles/` stay usable: listed as "partial (legacy)", "Review
   and apply" keeps working as a delta against live, and "Set as accepted" (which already requires the profile to be applied: `NOT_APPLIED`)
   records the **complete state from the read**, never the partial file.
4. **Migration of an existing multi-line chain** never comes from a read (the live CPU may hold neutralised residual OC words, G-147, which must
   not become the accepted state). It builds the single complete profile from the existing `EXPECTED-words.txt` plus the merged chain's turbo
   tables (or, when the chain has no turbo table, the tables marked "not part of the accepted state" — decide and document), keeps
   `fingerprint.json`, backs the old directory up to `/etc/arltuner/accepted.bak-<UTC>/`, and swaps atomically (reuse `profiles/accepted.py`'s
   `replace`). Provide it as a root CLI: `python3 tools/arltuner.py accepted-migrate [--dry-run]` (prints the profile it would write and the
   diff vs the chain's merge) — and have `install.sh` print a one-line hint when `CURRENT` has more than one line. **Never run it on the real
   `/etc/arltuner` (worker rule); tests use a temp dir.**
5. **One shot within the step limits.** Restore from a cold boot must not need a hand-made ramp profile (`p-boot-ramp` existed for a 55 mV step
   limit). The planner either splits a change into guarded intermediate steps automatically within the one restore, or refuses up front with
   `STEP_LIMIT` naming unit and field — never a partial silent restore. Pick one and justify (splitting preferred if it reuses the existing step
   machinery cleanly).
6. **Turbo tables** in a complete profile: the restore writes them only when they differ (the existing G-141 rule).

## Read first (only these)
- `docs/DECISIONS.md` D-035, D-038/D-039, D-044 (new), G-147 notes in `docs/tasks/G-147-restore-residual-oc.md`
- `src/arltuner/profiles/` (schema, accepted, contents, store, serialize, lastread, fingerprint)
- `src/arltuner/helper/ops_tune.py` (`_accepted_chain`, `_merge_profiles`, restore plan `StoredPlan.restore`, `profiles.*` ops, `accepted_match`),
  `src/arltuner/helper/ops_accept.py`, `src/arltuner/helper/ops.py` (`accepted_match`)
- `src/arltuner/controls/residual.py`, `controls/executor.py`, `controls/planner.py`, `src/arltuner/cli.py` (`apply`, `--accepted-before`, `compare-accepted`)
- `tools/lnx/restore_tuning.sh`, `tools/lnx/reapply_current.sh`, `tools/win/restore_tuning.ps1`, `tools/win/reapply_current.ps1`
- `web/app.js` (Profiles page: accepted chain list, `position/of/chain/cumulative`, Compare "As applied / This file only" toggle; Tune load)
- `docs/guide/09-profiles-and-restore.md`, `docs/guide/10-cli-reference.md`, `docs/SPEC.md`, `docs/ARCHITECTURE.md` (profile + accepted sections)

## Owns
`src/arltuner/profiles/`, `src/arltuner/helper/ops_tune.py`, `ops_accept.py`, `ops.py`, `src/arltuner/controls/`, `src/arltuner/cli.py`,
`src/arltuner/web/server.py` (profiles routes only), `src/arltuner/msr/sim.py` (if the simulator needs it), `web/`, `tools/lnx/restore_tuning.sh`,
`tools/lnx/reapply_current.sh` (delete, or make it a thin alias of restore), `tools/win/restore_tuning.ps1`, `tools/win/reapply_current.ps1`,
`tools/screenshots.py` (fixture names only), `install.sh` (the hint line only), `examples/reference-285k/` (convert its accepted example to one
complete profile if it ships a chain), tests for all of these, and the doc sections listed in Read first (guide 09/10, SPEC, ARCHITECTURE).
Do not edit `docs/DECISIONS.md` (the orchestrator wrote D-044) or `docs/STATE.md`.

## Deliverables
1. Schema: `complete` profiles (validation: every unit and every fused point present for the topology it is applied to; refuse otherwise),
   serializer, `contents`/summary say "complete" vs "partial (legacy)".
2. `profiles.save` = last read + edits → complete profile. `profiles.accept` → one complete profile + EXPECTED-words (+ fingerprint) derived from
   the same source. `CURRENT` single line.
3. Restore (helper op, CLI, `restore_tuning.sh`, `restore_tuning.ps1`) = apply the one accepted profile: diff vs live, write only differences,
   G-147 residual rule, compare result. `--accepted-before` and `_merge_profiles` go (keep merge only inside the migration). Old multi-line
   `CURRENT`: the helper refuses restore with a clear message pointing at `accepted-migrate` (no silent chain replay).
4. `accepted-migrate` CLI (decision 4), install.sh hint.
5. Step limits (decision 5).
6. Web: Profiles page without chain UI (no position/of/cumulative, no "As applied" toggle); badges complete / partial (legacy); Tune "Load into
   Tune" of a complete profile fills every unit.
7. Docs: guide 09 + 10, SPEC, ARCHITECTURE updated to the new model (no "chain" wording left, except the migration).
8. Tests: simulator flows — cold boot → restore one shot (with a small step-limit policy too); warm boot with residual OC words → EQUIVALENT;
   save complete; accept from a partial applied profile → complete; migration of a 3-profile chain built like the reference machine's
   (`e-p6-split3` + `p-boot-ramp` + `p-step7` + its EXPECTED-words) → one profile whose apply reproduces EXPECTED-words exactly; refusal on a
   multi-line CURRENT.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green.
- `grep -rn "_accepted_chain\|_merge_profiles\|accepted_before" src/` only inside the migration code.
- Windows scripts: syntax/static checks only (no `pwsh` here) — say so in the report.
- No policy or guard bound widened; no zero ratio ever sent (G-147 rules intact).

## Non-goals
No hardware. No change to the guard's checks. No new tuning features.

## Report-back (≤ 400 words)
**Open with your decisions on 2, 3-legacy, 4-turbo and 5** (one line each, with the reason), then: branch, commits, files changed per deliverable,
test count line, anything the orchestrator must do live (the migration command to run, what to check).
