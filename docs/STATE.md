# STATE — live handoff (rewrite at end of every session; ≤ 80 lines)

_Updated: 2026-10-14 · lnx · G-150..G-153 merged + installed; E 46x/48x split applied live and screened (not accepted yet). Suite **2941 tests OK**
(`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py`)._

## Where we are
- **App:** local web GUI (Dashboard, Curves, Tune, Stress, Profiles, System, Audit) + root helper as a socket service (no polkit, D-043) + CLI; Linux. Windows: restore of the
  accepted state only (`tools/win/`, **never run on Windows from this repo**).
- **Public project (D-036):** everything is read live from the user's CPU (topology, fused points, favored cores, limits). Reference-machine
  numbers live only in `examples/reference-285k/`, `docs/knowledge/` (tagged) and the simulator model.
- **User guide:** `docs/guide/` (13 pages, documenter agent `.claude/agents/documenter.md`, memory `docs/guide/_documenter/NOTES.md`).
  README has a Screenshots section (`docs/img/`, captured read-only with `tools/screenshots.py`).
- **Remote:** `github.com/xyehya/arrowlake-tuner` (`origin`, only `main` pushed).

## 2026-10-12 (merged + installed + checked live)
G-133/G-139 Tune model · G-134/G-137 live limits (D-038/D-039) · G-136 screenshots · G-138 Windows fingerprint · G-140 public scrub (D-041) ·
G-141 review fixes · G-142 security (D-042) · polish.

## Machine state (reference 285K)
- Policy `/etc/arltuner/policy.toml`: ratio_max **P 60 / E 50** (D-038; backup `policy.toml.bak-2026-10-12`), offsets ±200 mV.
- Boot 2026-10-13 19:03 (cold, BIOS TVB Voltage Optimizations ON — inert). Accepted = `read-20261013-145635`.
- **Live now differs from accepted on the E-clusters (2026-10-14, operator's test):** OC point 48x (EC0 1.1230 V, EC1-3 1.1123 V = the old 48x
  voltage) + point 6 EC0 -49.8 / EC1-3 -40.0 mV (was -35.2 / -25.4). P-cores = accepted. The OC points stay until a cold boot; Restore accepted
  would put point 6 back and report E as EQUIVALENT.
- Headless GUI restarted 2026-10-14 (serves G-150); port in `~/.local/share/arltuner-dev/port` (assets are loaded at server start: restart after web/ merges).
- **No polkit (G-144, D-043), live-verified 2026-10-12:** `arltuner-helper.socket` (systemd, `/run/arltuner/helper.sock` 0666) starts the root
  helper on connect; SO_PEERCRED allowlist `[access] users = ["yk"]` in `/etc/arltuner/helper.toml`; second client and root refused; stress child runs
  as `yk`; audit operator `gui:uid1000`. The temporary polkit rule was removed (nothing uses polkit any more).
- MCE count 0 all session.

## 2026-10-13
- EC1/EC2 residual OC words neutralised live (48x, target 1.100 V -> clamps to the accepted 46x point; all E clusters measured at the accepted
  48x voltage, EC0 1.1226 / EC1-3 1.1125 V). **G-147**: restore does this itself and reports EQUIVALENT; no 0x1D decrease; cpufreq cap follows.
- **G-148 (D-044)**: profiles are complete and independent; accepted = ONE profile restored in one shot (step splitting inside the apply).
  Live: chain migrated with `accepted-migrate` (backup `/etc/arltuner/accepted.bak-20261013T141400Z`); restore = 281 reads, 0 writes, RESTORED.
- Cold boot 2026-10-13 17:46: all 12 units stock (EC1/EC2 OC words cleared). GUI Restore accepted in one shot: 37 writes, all 36 words read
  back as written, 353 ops, MCE 0. Re-read -> "Set the current state as accepted" -> `read-20261013-145635` (complete, 12 units, turbo tables
  e 48x8 / p 57,57,55x6); EXPECTED-words identical to the tested state. Fingerprint kept.

## Done (2026-10-13): BIOS "TVB Voltage Optimizations" — no measurable effect (one variable)
- VO on + cold boot: `0x18` still `0x1E` (TVB-disable bit 2 set), `0x24` unchanged (70 C / -0,-1 bin; set 2 empty). Stock curve `fikh` vs VO-off
  stock (`d53n`, `y4c7`): 0 mV at 48/53/55/57x on every unit; tuned curve `yfjh` vs VO-off tuned `q9x7`: within +-2 mV at every point. A cool
  light-load sweep (44-58 C, P0-P2) requested the same VIDs (P0 53x 1.1686, 55x 1.2712, 57x 1.3864). Stopped there (operator: no added value).
  Reading: VO acts only with TVB itself enabled [UNVERIFIED]. With TVB disabled the 70 C down-bin is not applied either (P0/P1 55x at 80 C).
## 2026-10-14
- Operator: "48x is an OC ratio" and no offset control for it. **G-150** merged: OC ratio (any ratio above the fused top) vs OC point (own
  voltage) named apart; header "OC ratio: 48x (fused top 46x) · no OC point"; Point 6 "(also 47-48x)"; a "48x offset" dropdown (offset from the
  fused 0x09 voltage, exact 1/1024 V targets, floor = point 6: the CPU raises lower targets); "No OC point"; 47x says it lowers the max.
- **E 46x/48x split** applied (two plans: OC point first, then point 6). VID 46x -15 mV (1.1075 / 1.0975 V), 47x midpoint, 48x same (EC0 +2.4 mV).
  Screen PASS: all-E 46x 5 min / 47x 3 / 48x 2, bursty 46x, per-core sweep 44-48x, 46x single-thread per core with the cluster capped; MCE 0.
  `captures/lnx/esplit-tests-20261014.txt`. E clusters share one ratio: cap all four cores (`platform-arl-s.md`).
- **G-151** merged + installed: every apply/restore writes per unit in a transition-safe order (raising first; refused by name `write_order` when
  impossible); the OC point's slot (E point 7) never written as a fused point, a missing slot before the OC word is not a separate failure. So a
  cold-boot restore of the split writes the OC word before point 6. **G-152** merged + installed: Stress caps whole E clusters at the kernel's map
  (E 46x = 4.7 GHz), "cap held at Nx" / "ran Mx (cap Nx)". Gate 2933 OK. `install.sh` 21/21 self-checks.
- **G-153** merged + installed: the Review list shows the apply's real order (live: EC1 OC triple before point 6); the GUI reconnects to a
  restarted helper (live: `systemctl restart arltuner-helper` -> next call OK). Guide updated for G-150..G-152. Gate 2941 OK.
- **Next:** operator's own longer E test at 46x (caps on all 16 E-cores, 4.7 GHz); then Tune -> Read -> "Set the current state as accepted".

## Operator to-do
1. The pre-D-041 dev audit log (old directory name) is no longer read; Windows: re-carve the driver (`ts_driver.py init-dir`, `carve`).

## Next / open
- Windows restore path entirely untested on Windows (scripts, ACL checks, identity read, `vf-dump --json`); driver blocklist: `windows-path.md` [MEASURED] says it loaded with the blocklist on, `preflight.ps1` flags it FIX — decide after the first Windows run.
- Restore scripts still run from the checkout (under `-I`); move them onto the installed tree (ROADMAP).
- Health gate checks right after each op only (D-042 (6), SAFETY.md).
- Open question from 2026-10-12: does a non-favored P-core run 56/57× with an OC ratio (needs a write sitting).
- G6 autostart (OD-3/OD-4), G7 portability per ROADMAP.

## How we work
Orchestrator + Sonnet workers in worktrees (`docs/ORCHESTRATION.md`); workers never touch hardware (one-off exceptions D-037, D-040).
Before every merge: `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green. Decisions in `docs/DECISIONS.md` (latest D-044).

## Tools
CLI `python3 tools/arltuner.py read | read-vf | vf-dump | plan | apply` (`--device`, `--device-win`) · GUI `python3 tools/arltuner-gui.py --open` ·
`tools/lnx/restore_tuning.sh` · `tools/screenshots.py` · `tools/win/` (restore, ts_driver) · tests `python3 tools/run_tests.py`.
