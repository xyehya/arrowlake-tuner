# STATE — live handoff (rewrite at end of every session; ≤ 80 lines)

_Updated: 2026-10-08 · lnx · G0 bootstrap done (repo created from linuxmon@b01f78a). Resume: `docs/START-PROMPT.md`._

## Where we are
- **G0 done:** code renamed `arltuner`, 1261 offline tests OK, sim plan of p-step7 identical to linuxmon, accepted profiles + policy carried,
  Windows `$Expected` == `profiles/EXPECTED-words.txt` (12/12). Knowledge distilled into `docs/knowledge/`. Spec v2 `docs/SPEC.md` with the
  37-finding review folded in (`docs/review/2026-10-08-spec-v1-review.md`). G1 tasks written (`docs/tasks/INDEX.md`).
- **Not yet run from this repo:** the restore scripts on hardware (Linux `tools/lnx/restore_tuning.sh`, Windows `tools\win\restore_tuning.ps1`).
  Since G-101 `topology`/`msr` read `platforms/arl-s-285k.toml` at import (raise if missing/corrupt), so the next boot's restore is also
  the first real run of the platform loader — check it first.
- **Remote:** private GitHub `xyehya/arrowlake-tuner` (remote `origin`). linuxmon is the read-only archive (`docs/PROVENANCE.md`).

## Accepted tuning (reference machine) — p-step7 + E split3, BIOS LLC 6 (D-019)
`profiles/CURRENT`: `e-p6-split3` → `p-boot-ramp` → `p-step7`; words `profiles/EXPECTED-words.txt`. Gate passed on Windows (2026-10-04) and Linux
(2026-10-07/08): OCCT Linpack 2021 60 min + SSE 60 min, no MCE/WHEA. Table + history: `docs/knowledge/tuning-results.md`.
| Core | 48× | 53× | 55× → V | 56× → V | 57× → V | E-cluster | 46× |
|---|---|---|---|---|---|---|---|
| P0 ★ | 0 | −50 | −28 → 1.270 | −26 → 1.360 | −43 → 1.386 | 0 (CPUs 8–11) | −35 |
| P3 ★ | 0 | −50 | −28 → 1.270 | −28 → 1.331 | −31 → 1.365 | 1–3 (12–23) | −25 |
| P1 / P4 / P5 | 0 | −50 | −36 / −33 / −73 → 1.270 / 1.286 / 1.286 | 0 | 0 | | |
| P6 / P2 / P7 | 0 | −50 | −61 / −81 / −91 → 1.286 / 1.285 / 1.286 | 0 | 0 | | |
BIOS: **PL1 250 W** (operator 2026-10-08; PL2 250 per the 10-05/10-07 notes) · VccIA −100 mV · LLC 6 · ring 40× −16 mV · DDR5-7200 · NGU/D2D 32× · VccSA 1.185 V.

## After every boot (cold boot clears all offsets; warm keeps CPUs 1–7 + E, CPU 0 reset)
- **Linux:** `cd ~/AI/arrowlake-tuner && bash tools/lnx/restore_tuning.sh` → type `CONFIRM` once → "RESTORED: all cores/clusters match".
- **Windows:** elevated `powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1` (or `Restore-Tuning.cmd`) → CONFIRM once.
  Needs the repo on Windows (`git clone https://github.com/xyehya/arrowlake-tuner C:\Users\yk\AI\arrowlake-tuner`) and Python (`py -3`).
- Per-boot budget 600 mailbox ops, shared with the old linuxmon CLI via the same audit log (D-022).

## Open decisions (block the phase named) — SPEC §14
| # | Question | Proposed | Blocks |
|---|---|---|---|
| OD-1 | Root-owned install for helper/policy/audit/accepted state, also in development | **yes (2026-10-08, D-024)** | — |
| OD-2 | Python ≥ 3.12 | **yes (D-024)** | — |
| OD-3 | Autostart applies only a profile with a recorded gate | deferred: agree when G6 starts | G6 |
| OD-4 | Sign D-021 (unattended autostart) | deferred: agree when G6 starts | G6 |
| OD-5 | GitHub remote `xyehya/arrowlake-tuner`, private | **yes (D-024)** — remote `origin` | — |

## Next
1. G1 (`docs/tasks/INDEX.md`): G-101 and G-103/G-104 can start in parallel (Sonnet workers in worktrees, simulator/fake sysfs only); G-102 after G-101;
   G-105 last; G-106 (unblocked).
2. W: first Windows restore from this repo (check the rename end to end).

## How we work
Operator at the machine; orchestrator answers guard tokens only after a go-ahead. One variable at a time; acceptance gate = 60-min OCCT Linpack 2021
+ 60-min SSE; OCCT unprivileged; never raise policy limits without a yes. Design: decisions first, position taken, adversarial review, honest report.

## Tools
CLI `python3 tools/arltuner.py read | read-vf | vf-dump [--measure] | point-offset | apply | plan` (`--device` Linux, `--device-win` Windows) ·
`tools/lnx/`: restore_tuning.sh, reapply_current.sh, health_snapshot.sh, percore_stress.py, vfcurve.py, orchestrator_confirm.py · `tools/win/`:
restore_tuning.ps1, Restore-Tuning.cmd, reapply_current.ps1, ts_driver.py, preflight.ps1 · tests `python3 tools/run_tests.py` (1261).
