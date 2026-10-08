# ROADMAP — phases (tasks are written just-in-time in `docs/tasks/`)

Spec: `docs/SPEC.md`. Phase 0 (the CLI, research, accepted tuning) was done in the predecessor repo `linuxmon` (`docs/PROVENANCE.md`).

| Phase | Host | Delivers | Exit criterion | Blocked by | State |
|---|---|---|---|---|---|
| **G0 Bootstrap** | lnx | new repo: code renamed `arltuner`, accepted profiles, knowledge distilled, spec v2, review | tests green (1261); sim plan identical to predecessor; restore scripts run | — | **done 2026-10-08** |
| **G1 Skeleton** | lnx | install layout (`install.sh`, root-owned paths), root helper + protocol, web server + security, preflight, platform file, read-only dashboard (telemetry + applied-vs-accepted) | read-only app on the reference machine; G1 security tests pass (SPEC §12) | OD-1, OD-2 | next |
| **G2 Offsets** | lnx | V/F editor, profiles (diff/save-as), plan → nonce CONFIRM → apply → read-back → compare, restore, acceptance proposal (CURRENT + EXPECTED + `$Expected`) | p-step7 restored from the GUI after a cold boot, words equal | G1 | — |
| **G3 Stress** | lnx | freq caps with journal + achieved-vs-requested, stress-ng / stressapptest screens as the user, Linpack Xtreme launcher, result log | a 53×-capped screen run end to end; cap restored after a killed helper | G1 | — |
| **G4 E ratios** | lnx | E OC ratio + `0x1D` + `0x650` + E target V writable after the gate | E 49× re-applied supervised + D-019 gate recorded | G2, G3 | — |
| **G5 P ratios** | lnx | P OC ratio, `0x1AD`, P target V (after a hand-added P ceiling) | each gate recorded (SPEC §5) | G2, G3 | — |
| **G6 Autostart** | lnx | off / with CONFIRM / unattended; crash-loop guard; revert plan | simulated marker test + documented human hard-reset check | D-021 (OD-3, OD-4), G2 | — |
| **G7 Portability** | lnx | packaging + per-distro checks, docs; public-release privacy scrub | clean install on a second distro (VM: read-only paths) | G1–G3 | — |
| **W Windows** | win | keep `restore_tuning.ps1` / `Restore-Tuning.cmd` working after the rename; `$Expected` sync check | restore on Windows prints RESTORED from this repo | — | first run pending |

## Hard rules for every phase
Hardware writes: human + orchestrator only (`docs/SAFETY.md`); workers test against the simulator and fake sysfs only. Nothing in G1–G7 may
weaken the guard; new controls become writable only through their gate (SPEC §5).
