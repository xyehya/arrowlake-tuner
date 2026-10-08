# PROVENANCE — where the evidence lives

This repo started 2026-10-08 from **`linuxmon@b01f78a`** (`~/AI/linuxmon`, private GitHub `xyehya/linuxmon`), fresh history (D-022).
`linuxmon` is the **read-only archive**: research notes, decompiles, traces, captures, experimental profiles, task history (T-001…T-028).

| Cited here as | Lives in linuxmon@b01f78a |
|---|---|
| `docs/research/*.md` (e.g. `lnx-first-read.md`, `win-first-apply.md`, `win-xtu-*.md`, `access-manifest.md`, `asrock-llc.md`) | `docs/research/` — index `docs/research/README.md` |
| `docs/tasks/T-0xx-*.md` | `docs/tasks/` — index `docs/tasks/INDEX.md` |
| `captures/lnx/*`, `captures/win/*` | `captures/` (small text logs tracked; raw dumps were never committed) |
| `docs/HANDOFF-windows.md`, `HANDOFF-fedora.md`, `OPERATING_MODEL.md`, start prompts | `docs/` |
| experimental profiles `p-step2…p-step8`, `p-round3*`, `p-fav57`, `p-step6-half`, `p-step7-p0-58`, `p0-58-1430`, `tune-2026-10-02`, `test-p0-pt5-*` | `profiles/` |
| policies `session-2026-10-06-p0-58.toml` | `policies/` |
| recon tools `tools/win/ipf_trace/`, `tools/tracker/`, llama/MLC bench scripts, `tools/lnx/boot_to_windows.sh` | `tools/` |

Carried over (renamed `linuxmon` → `arltuner` where it is code): `src/`, `tests/` (minus the IPF-trace tests), `tools/run_tests.py`,
`tools/arltuner.py`, `tools/lnx/{restore_tuning.sh,reapply_current.sh,orchestrator_confirm.py,health_snapshot.sh,percore_stress.py,vfcurve.py,preflight.sh,read_plain_msrs.py}`,
`tools/win/{restore_tuning.ps1,Restore-Tuning.cmd,reapply_current.ps1,preflight.ps1,ts_driver.py}`, accepted profiles (`CURRENT`, `EXPECTED-words.txt`,
`e-p6-split3`, `p-boot-ramp`, `p-step7`, `stock-all`, `e49-xtu-restore`), policies (`session-2026-10-02-points`, `session-2026-10-01-e49`,
`example-oc-ramp`), `docs/{SAFETY,ARCHITECTURE,DECISIONS,HARDWARE,RUNBOOK-lnx,RUNBOOK-win}.md`, `docs/knowledge/registers.md` (verbatim).
Distilled: `docs/knowledge/{platform-285k,tuning-results,linux-path,windows-path,mailbox-recon-summary}.md`.
Verification at the move: 1261 tests OK (1355 − 94 IPF-trace tests); `plan profiles/p-step7.toml --sim` output identical to linuxmon's;
`EXPECTED-words.txt` == `$Expected` in `restore_tuning.ps1` (12/12).
