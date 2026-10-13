# G-147 — Restore handles a residual OC point (cannot be zeroed) and never lowers the favored ratio

- **Status:** ready
- **Host:** lnx
- **Safety tier:** T0 (worker, simulator + fake sysfs); the orchestrator verifies live (T2)
- **Branch/worktree:** `task/G-147-restore-residual-oc`
- **Depends on:** none

## Goal
After a warm reboot (or after write tests) an E-cluster or P-core can hold an **OC word** that the accepted state does not have (accepted OC word
`0`), e.g. EC1 `0x00048C30` = ratio 48 (> fused top 46) + adaptive target 1.137 V. The CPU refuses a zero OC ratio at runtime; only a cold boot
clears it (`docs/knowledge/platform-arl-s.md` §"OC point"). Today Restore accepted (helper op + `tools/lnx/restore_tuning.sh` + Windows twin)
leaves such a word alone and reports the unit as BAD/differ, while the unit runs a **different voltage** than the tested profile (EC1 at 48×:
1.137 V instead of the accepted 1.1125 V). Restore must instead write the **closest equivalent word** so the unit runs the accepted voltages, and
report the unit as "equivalent (residual OC point; cold boot clears)", not as a failure.

Second defect found in the same sitting: the planner sends `0x1D` (favored ratio) = the `0x11` word's ratio. Writing EC2 an OC word with ratio 46
lowered its favored ratio from 48 to 46 (`0x1C` read before: `0x2e30`), `cpuinfo_max_freq` 4.9 → 4.7 GHz, and `scaling_max_freq` stayed at 4.7 GHz
after the favored ratio was raised again (the CLI path does not lift it; the GUI does since G-126).

## Measured facts to rely on [MEASURED lnx 2026-10-13, reference 285K; see examples/reference-285k/notes/tuning-history.md §6]
| Write | Read-back | Effect |
|---|---|---|
| EC1 `0x11` ratio 48, target 1.100 V (below the accepted point 6 = 1.1125 V) | `0x00046630` | 48× measured **1.1125 V** = accepted (clamped up to point 6) |
| EC2 `0x11` ratio 46 (= fused top), target 0 (D-035) | `0x0000002E` | `0x1D` = 46 sent → favored 48 → 46, `cpuinfo_max_freq` 4.7 GHz |
| EC2 `0x11` ratio 48, target 1.100 V | `0x00046630` | favored 48 again, `cpuinfo_max_freq` 4.9 GHz, `scaling_max_freq` stayed 4.7 GHz until written; 48× then **1.1125 V** |
Earlier: an OC target below the top fused point is clamped up to it [MEASURED lnx 2026-10-11]; between the top fused point and an OC point the
voltage is linear [MEASURED lnx 2026-10-12].

## Read first (only these)
- `docs/knowledge/platform-arl-s.md` (OC point, target retention, clamp), `docs/knowledge/registers.md` (`0x10/0x11`, `0x1C/0x1D`, `0x26/0x27`)
- `src/arltuner/controls/planner.py`, `controls/executor.py`, `controls/model.py`, `controls/report.py`
- `src/arltuner/helper/ops_tune.py` (restore / accepted compare), `src/arltuner/profiles/accepted.py`
- `tools/lnx/restore_tuning.sh` (compare block), `tools/win/restore_tuning.ps1` (compare + apply), `src/arltuner/cli.py` (apply)
- D-035 and D-038/D-039 in `docs/DECISIONS.md`

## Owns
`src/arltuner/controls/`, `src/arltuner/helper/ops_tune.py`, `src/arltuner/cli.py`, `src/arltuner/msr/sim.py` (simulator model of the
residual OC point / clamp / favored cap), `tools/lnx/restore_tuning.sh`, `tools/win/restore_tuning.ps1`, `web/` (restore result + Dashboard
comparison wording only), tests for all of these, `docs/knowledge/platform-arl-s.md` (one general rule line).

## Deliverables
1. **Residual-OC neutralisation in the restore plan** (helper restore op, CLI apply of accepted profiles, both restore scripts). For each unit whose
   accepted OC word has no OC point (ratio 0 or ≤ fused top) but whose live OC word has ratio R > fused top: write `0x11` with ratio R (kept; it
   cannot be removed), the accepted whole-curve offset field, and an adaptive target **below the unit's accepted top fused point voltage** so the
   hardware clamps it up (choose a rule: e.g. the fused top point voltage from `0x09` minus 100 mV, floored by the encoding; document why). Never a
   zero ratio, never a target above the fused top point. Same for P-cores (residual P OC point above the fused top: same rule).
2. **Favored ratio never lowered as a side effect.** `0x1D` written = the favored ratio the plan intends: for a restore the accepted/before value
   (`0x1C[7:0]` read before), for an OC raise the new ratio; a write whose `0x11` ratio ≤ fused top must not send a `0x1D` below the `0x1C[7:0]`
   read before (or must not send `0x1D` at all — pick and justify against registers.md). Guard/plan test: the EC2 case above produces no
   favored-ratio decrease.
3. **cpufreq cap follows.** After any `0x1D` change, the CLI apply path and the restore scripts set `scaling_max_freq` of the unit's CPUs to the new
   `cpuinfo_max_freq` when it was at the old maximum (the GUI's G-126 logic; reuse it, do not duplicate). Never raise a cap the user lowered below
   the old maximum.
4. **Compare semantics** (restore result, `restore_tuning.sh --check`, Windows compare, Dashboard "accepted" comparison): a unit whose point words
   match and whose OC word is the neutralised residual (ratio > fused top, target ≤ fused top point V, offset field = accepted) is reported
   **EQUIVALENT** with the text "residual OC point (cleared by a cold boot); voltages as accepted". Only real differences are BAD. The script exit
   code treats EQUIVALENT as success.
5. **Simulator:** model "OC ratio cannot be written back to 0", "target below the top point clamps up", and "`0x1D` lowers `cpuinfo_max_freq`", so
   tests cover the whole flow (warm reboot with residual words → restore → EQUIVALENT, favored ratio unchanged, caps restored).
6. One general line in `docs/knowledge/platform-arl-s.md` (tagged) on neutralising a residual OC point; no chip numbers there.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green (new tests for 1–5).
- Simulated: accepted state with OC word 0 on EC1/EC2, live EC1 `0x48C30`, EC2 `0x48C2E` → restore plan writes EC1 ratio 48 + low target, EC2 a
  word that keeps favored 48; result EQUIVALENT for both; no `0x1D` decrease anywhere; `scaling_max_freq` back at the maximum.
- No zero ratio is ever sent; guard bounds unchanged (policy ceilings, D-038/D-039).

## Non-goals
No hardware access (orchestrator verifies live). No change to accept semantics (accepted = what the CPU holds). No policy widening.

## Report-back (≤ 300 words)
What changed per deliverable, the target rule chosen and why, test count, anything left for the live check.
