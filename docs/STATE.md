# STATE — live handoff (rewrite at end of every session; ≤ 80 lines)

_Updated: 2026-10-12 · lnx · **v3 complete on the reference machine**: G-111…G-126 merged (suite 2277 OK), installed, verified on hardware end to end. User guide: `docs/USER-GUIDE.md`._

## Where we are
- **G0 done.** **G1 merged:** G-101 platform file + preflight, G-102 root helper (read-only ops; 2 Sonnet review rounds), G-103 web server
  (2 review rounds), G-104 telemetry readers. Suite **1810 OK** (an occasional `ConnectionResetError` line is slow-client test log noise).
- **All G1 tasks merged:** G-105 dashboard (layout unverified in a real browser; calls `telemetry.start` on every SSE open), G-106 install +
  polkit + D-030 paths (2 review rounds), G-107 helper budget heartbeat + accepted compare (1 review round), G-108 plain uninstall keeps tmpfiles.d + /run/arltuner.
- **Decisions this session:** D-026 telemetry own read allowlist · D-027 web↔helper (202 + SSE result, Sec-Fetch-Site) · D-028 web residual risks
  · D-029 root helper hardening (no argv as root) · D-030 path migration only when installed · D-031 install + operator CLI path.
- **Installed 2026-10-11** (`sudo bash install.sh`, operator go; self-check all 17 passed; audit seeded from the legacy log, 24935 lines).
  From now on CLI/restore use `/run/arltuner/mailbox.lock` + `/var/lib/arltuner/audit.jsonl` (D-030). **No mailbox traffic in the install.**
- **Before 2026-10-11 hardware not touched by this repo.** Restore scripts not yet run from this repo. Since G-101, `topology`/`msr` read
  `platforms/arl-s-285k.toml` at import ⇒ the next boot's restore also exercises the platform loader (check it first).
- **Sequencing (D-030):** before install, CLI keeps legacy lock/audit paths; after the install go-ahead (T1), the *following* restore is the first
  run on `/run/arltuner/mailbox.lock` + `/var/lib/arltuner/audit.jsonl`. The helper refuses to run from a checkout ⇒ first real helper run needs
  the install. Each is a separate operator go-ahead.
- **Remote:** private GitHub `xyehya/arrowlake-tuner` (`origin`). linuxmon = read-only archive (`docs/PROVENANCE.md`).

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
1. **Done 2026-10-11/12 [MEASURED]** (`knowledge/tuning-results.md`): reads (preflight, memory MCHBAR, tune.read with fused/turbo, Quick + Dense
   curves), writes via the Tune path (OC point ⇒ P2=7 on E, adaptive follows/clamps, P2=0 whole-curve offset P and E (offset-only = 0x11 only),
   per-point, Restore accepted 172 ops), stress screens (CPU/memory/capped). Machine on the accepted tuning; 11/12 match, CPU 12 = EC1 neutral OC
   point 48× from the stage-1 test (clears at cold boot).
2. Also verified 2026-10-12: turbo-table writes (P 54× / E 47× all-core, restored), E OC 49× with automatic cpufreq lift (G-126). Open, low
   priority: `write_percore_enable` predicted as worst case; ring/NGU not in scope (SPEC §2); P OC ratio 58× is above the D-032 cap (P 57); light theme dropped; restore scripts still use their checkout
   policy (session-2026-10-02) — move them onto the helper later. Testing helper without polkit: `--helper sudo -n PKEXEC_UID=1000 …`.
3. **Pending operator step:** final build through the real polkit path (`python3 tools/arltuner-gui.py --open`, password) + one Stress screen
   from the tab, check the child runs as the user (`ps -eo user,args | grep stress`). PKEXEC_UID is set by pkexec [DOC pkexec(1)]; all hardware
   checks so far used `sudo -n PKEXEC_UID=1000`. Prompt was left open 2026-10-12.
4. W: first Windows restore from this repo. Workers `sonnet`/`high`. D-034: orchestrator autonomous.

## How we work
Operator at the machine; orchestrator answers guard tokens only after a go-ahead. One variable at a time; acceptance gate = 60-min OCCT Linpack 2021
+ 60-min SSE; OCCT unprivileged; never raise policy limits without a yes. Design: decisions first, position taken, adversarial review, honest report.

## Tools
CLI `python3 tools/arltuner.py read | read-vf | vf-dump [--measure] | point-offset | apply | plan` (`--device` Linux, `--device-win` Windows) ·
`tools/lnx/`: restore_tuning.sh, reapply_current.sh, health_snapshot.sh, percore_stress.py, vfcurve.py, orchestrator_confirm.py · `tools/win/`:
restore_tuning.ps1, Restore-Tuning.cmd, reapply_current.ps1, ts_driver.py, preflight.ps1 · tests `python3 tools/run_tests.py` (1810).
