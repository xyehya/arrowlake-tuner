# ROADMAP — phases (tasks are written just-in-time in `docs/tasks/`)

Spec: `docs/SPEC.md`. Phase 0 (the CLI, research, accepted tuning) was done in the project's earlier research phase.

| Phase | Host | Delivers | Exit criterion | Blocked by | State |
|---|---|---|---|---|---|
| **G0 Bootstrap** | lnx | new repo: code renamed `arltuner`, accepted profiles, knowledge distilled, spec v2, review | tests green (1261); sim plan identical to predecessor; restore scripts run | — | **done 2026-10-08** |
| **G1 Skeleton** | lnx | install layout (`install.sh`, root-owned paths), root helper + protocol, web server + security, preflight, platform file, read-only dashboard (telemetry + applied-vs-accepted) | read-only app on the reference machine; G1 security tests pass (SPEC §12) | OD-1, OD-2 | **done 2026-10-11** |
| **G2 Offsets** | lnx | V/F editor, profiles (diff/save-as), plan → nonce CONFIRM → apply → read-back → compare, restore, acceptance proposal (CURRENT + EXPECTED + `$Expected`) | the accepted state restored from the GUI after a cold boot, words equal | G1 | **delivered 2026-10-12 via SPEC-v3** (Tune: per-point/whole-curve offsets, plan→review→apply→read-back, Restore accepted verified on HW) |
| **G3 Stress** | lnx | freq caps with journal + achieved-vs-requested, stress-ng / stressapptest screens as the user, Linpack Xtreme launcher, result log | a 53×-capped screen run end to end; cap restored after a killed helper | G1 | **delivered 2026-10-12** (Stress tab: stress-ng/stressapptest as the user, ratio cap + restore, verified on HW) |
| **G4 E ratios** | lnx | E OC ratio + `0x1D` + `0x650` + E target V writable after the gate | E 49× re-applied supervised + D-019 gate recorded | G2, G3 | **delivered** (E OC point 48× + adaptive verified on HW; E > 48× untested) |
| **G5 P ratios** | lnx | P OC ratio, `0x1AD`, P target V (after a hand-added P ceiling) | each gate recorded (SPEC §5) | G2, G3 | **delivered in code** (P OC ratio/target, 0x1AD turbo-only plans; P 58× and turbo writes untested on HW in v3) |
| **G6 Autostart** | lnx | off / with CONFIRM / unattended; crash-loop guard; revert plan | simulated marker test + documented human hard-reset check | D-021 (OD-3, OD-4), G2 | — |
| **G7 Portability** | lnx | packaging + per-distro checks, docs; public-release privacy scrub | clean install on a second distro (VM: read-only paths) | G1–G3 | — |
| **W Windows** | win | keep `restore_tuning.ps1` / `Restore-Tuning.cmd` working after the rename; `$Expected` sync check | restore on Windows prints RESTORED from this repo | — | first run pending |

**v3 (2026-10-11/12):** SPEC-v3 (Measure → Store → Tune → Stress → Profiles) merged G-111…G-124; facts in `knowledge/tuning-results.md`.

**Done 2026-10-13 (D-044):** *G-147* restore neutralises a residual OC point (reported EQUIVALENT) and never lowers `0x1D` as a side effect; *G-148* profiles are
complete and independent, the accepted state is one profile, and Restore accepted is one shot (only the words that differ, guarded rounds within the step limits,
or an up-front `STEP_LIMIT` refusal); an older chain is converted once with `arltuner accepted-migrate`. No hand-made ramp profile is needed any more.

## Follow-ups from the security review (2026-10-12, D-042)
- **Restore from the installed, root-owned tree.** `tools/lnx/restore_tuning.sh` (and `reapply_current.sh`, now only an alias of it) still run the checkout's `tools/arltuner.py` as root (now
  `python3 -I`, so no user site / `PYTHON*` / cwd imports, but the checkout itself is still user-writable code run with `sudo`). The proper fix is to install the
  CLI under `/usr/lib/arltuner/` (root-owned, the tree `install.sh` already compiles) and have the scripts run that copy; the Windows scripts likewise
  (an installed copy under `%ProgramData%\arltuner`). Until then the operator reads the diff of `tools/` before a restore sitting after pulling.
- **Windows admin-only directory is untested on Windows.** Written on Linux (G-142): first run of `ts_driver.py init-dir/start`, `reapply_current.ps1 -Check` and
  `restore_tuning.ps1` is the bring-up test. The G-147 / G-148 changes to both scripts (one profile, residual rule, rounds) were written on Linux and checked statically only.

## Hard rules for every phase
Hardware writes: human + orchestrator only (`docs/SAFETY.md`); workers test against the simulator and fake sysfs only. Nothing in G1–G7 may
weaken the guard; new controls become writable only through their gate (SPEC §5).
