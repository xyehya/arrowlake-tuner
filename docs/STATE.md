# STATE — live handoff (rewrite at end of every session; ≤ 80 lines)

_Updated: 2026-10-12 · lnx · public-readiness pass done (operator goal: fine-tune every page, code / UI / security / git-history review, remove
private references, final polish). Suite **2685 tests OK** (`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py`)._

## Where we are
- **App:** local web GUI (Dashboard, Curves, Tune, Stress, Profiles, System, Audit) + root helper as a socket service (no polkit, D-043) + CLI; Linux. Windows: restore of the
  accepted state only (`tools/win/`, **never run on Windows from this repo**).
- **Public project (D-036):** everything is read live from the user's CPU (topology, fused points, favored cores, limits). Reference-machine
  numbers live only in `examples/reference-285k/`, `docs/knowledge/` (tagged) and the simulator model.
- **User guide:** `docs/guide/` (13 pages, documenter agent `.claude/agents/documenter.md`, memory `docs/guide/_documenter/NOTES.md`).
  README has a Screenshots section (`docs/img/`, captured read-only with `tools/screenshots.py`).
- **Remote:** `github.com/xyehya/arrowlake-tuner` (`origin`, only `main` pushed).

## This session (2026-10-12), all merged + installed + checked live
| Task | What |
|---|---|
| G-133/G-139 | Tune: All-Core/Per-Core, Whole Curve / Per V/F point Offsets, OC ratio before adaptive voltage (gated), limits live, no turbo text in cards, interpolated predictions, BUSY retry of read-only ops |
| G-134/G-137 | Limits: `"auto"` ratio = fused 0x1C[15:8] + 3; policy may go up to the CPU's own maxima (0x01 OC ratio, 0x28 V) and the field encoding; `hw_limits`; CLI reads 0x01 (D-038, D-039) |
| G-136 | README screenshots tool (read-only allowlist, CDP over pipe, read-scope session) |
| G-138 | Windows restore for any ARL-S: `fingerprint.json` at accept, LP map derived live |
| G-140 | Public scrub: no private-repo refs, D-041 dev paths (`~/.local/state/arltuner/`, `/run/lock/arltuner-mailbox.lock`) |
| G-141 | Code-review fixes (kind-wide plan ratio, paced extras, journals, turbo fold, encoding bounds, BIOS offset default 0, stale-read accept, `profiles.fingerprint`, audit 0600, `ops_done` total) |
| G-142 | Security fixes (D-042): random 127.x.y.z bind + read-only scope, restore under `-I`, socketpair + undumpable GUI, admin-only Windows driver dir with per-run copy re-hashed, caps, NaN |
| polish | Audit names/filter, System Tuning-limits card, E-cluster labels, applied chip ranges, fingerprint button, no internal ids in UI |

## Machine state (reference 285K)
- Policy `/etc/arltuner/policy.toml`: ratio_max **P 60 / E 50** (D-038; backup `policy.toml.bak-2026-10-12`), offsets ±200 mV.
- Warm reboot this morning; **restore run by the orchestrator 2026-10-12 19:0x** (`restore_tuning.sh --orchestrated`, new code under `-I`, live
  0x01 binding): 3 profiles applied (65 + 51 + 51 ops), all P-cores + EC0/EC3 match; **EC1 (OC word 48× + 1.137 V) and EC2 (OC word 46× +
  1.137 V) differ only in the OC word** — cannot be zeroed at runtime, a cold boot clears them. `fingerprint.json` recorded (tuning untouched).
- Tune controls exercised in Chrome on live data (OC ratio 58× on P0 → adaptive voltage enabled + prefilled 1.4248 V → Review 17 ops → discarded).
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
