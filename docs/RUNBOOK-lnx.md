# RUNBOOK — hardware sittings on Fedora (`lnx`)

> Carried over 2026-10-08 from the project's earlier research phase. Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here name notes of that phase, which are not part of this repository.

How the orchestrator (or the operator) runs arltuner against the real CPU, with the exact commands used on 2026-10-01/02. Rules behind it:
`AGENTS.md`, `docs/SAFETY.md`, D-011/D-017/D-018. Results of every sitting go to `docs/research/lnx-first-read.md` (or a new `lnx-*.md`).
All paths are relative to `~/AI/arrowlake-tuner`. `A=--audit ~/.local/state/arltuner/audit.jsonl` (always pass it: under sudo the default would
be root's home, and the per-boot cap and cross-process dwell are counted from this one log).

## 0. Before any sitting
- Operator at the machine, explicit go-ahead in chat (the operator gave a standing one for orchestrated sittings on 2026-10-01).
- No other mailbox client: OCCT only **unprivileged** (it writes MSR 0x150 at launch when it has CAP_SYS_RAWIO); `arl_telemetry`/turbostat are read-only and fine.
- `bash tools/lnx/health_snapshot.sh before-X` (taint, PCI count, modules, kernel-log length).
- First MSR write of a boot sets taint bit 2 (`CPU_OUT_OF_SPEC`): pass `--allow-taint-bit 2` always.

## 1. Tools (`tools/lnx/`)
| Tool | What | Hardware access |
|---|---|---|
| `restore_tuning.sh [--orchestrated] [--check]` | **after every boot:** apply `profiles/CURRENT` + verify every word against `profiles/EXPECTED-words.txt` (Linux twin of `tools/win/restore_tuning.ps1`) | writes point offsets via the guard |
| `reapply_current.sh [--check]` | re-apply the tuning state listed in `profiles/CURRENT` after a boot (one CONFIRM per profile); `--check` dumps the point words read-only | writes point offsets via the guard |
| `preflight.sh` | state record before a first sitting | none |
| `health_snapshot.sh LABEL [--since FILE]` | before/after: taint, PCI, modules, new kernel lines (expected msr notices hidden) | none |
| `read_plain_msrs.py [--cpus] [--msrs]` | plain rdmsr of 0x1AD/0x1AE/0x650/0x651/0xCE/0x194/0x198 with decodes; refuses anything else | read-only, no taint |
| `percore_stress.py [--cpus 0-7] [--from-ratio 52] [--seconds 60]` | per-core stability sweep: stress-ng --verify pinned to one core, uncapped then capped at every ratio from 52× to the core max; VID/ratio/temp each second; stops at the first failure (stress-ng or new MCE/hardware-error kernel lines) | read-only MSRs; writes cpufreq caps, restores them |
| `vfcurve.py --cpus 8-11 --ratios 40,46,48` | quick measured V/F curve of one group: cpufreq cap (exact, then +1 if the core settles below) + stress-ng load + VID from 0x198, only samples at the target ratio kept; `arltuner vf-dump --measure` does the same for every core | read-only MSRs; writes cpufreq caps, restores them |
| `rehearse_seeded.py PROFILE --policy P --word CPUS=HEX …` | full `apply` on a simulator seeded with the words just read on the device | none |
| `orchestrator_confirm.py <arltuner cmd>` | answers the CONFIRM prompt once — **orchestrator only, after a go-ahead, never unattended** | whatever the wrapped command does |

## 2. Recipes
**One mailbox read** (any allowlisted read; point reads need `policies/session-2026-10-02-points.toml`):
```
sudo env ARLTUNER_ALLOW_MAILBOX=1 USER=$USER python3 tools/lnx/orchestrator_confirm.py \
  python3 tools/arltuner.py read --device --cpu 8 --cmd 0x10 [--p2 6] --policy policies/session-2026-10-02-points.toml --allow-taint-bit 2 $A
```
`read-vf --device --cpu N` is the `0x10 P2=0` special case. Several reads: loop **and stop at the first non-zero exit** (`|| break`).

**Apply a profile** (always: read the current words → rehearse seeded → apply → measure):
```
python3 tools/lnx/rehearse_seeded.py profiles/X.toml --policy policies/S.toml --word 8-11=0x… --word 12-23=0x…
sudo env ARLTUNER_ALLOW_MAILBOX=1 USER=$USER python3 tools/lnx/orchestrator_confirm.py \
  python3 tools/arltuner.py apply profiles/X.toml --device --policy policies/S.toml --allow-taint-bit 2 $A > captures/lnx/X-apply.txt 2>&1
sudo python3 tools/lnx/vfcurve.py --cpus 8-11 --ratios 40,46,48 --out captures/lnx/vfcurve-X.txt
bash tools/lnx/health_snapshot.sh after-X --since captures/lnx/health-before-X.txt
```

## 3. Profiles and policies in use
| File | What |
|---|---|
| `profiles/e49-xtu-restore.toml` | all E 49×, OC-point V 1.165 V (cluster 1) / 1.145 V (2–4), E turbo table 49 ×8 (D-017) |
| `profiles/e-c1-p6-minus10.toml`, `profiles/e-c234-p6-minus10.toml` | per-point offset −10 mV on E point 6 (46×) → 46–48× −10 mV, points 1–5 untouched (T-025) |
| `policies/session-2026-10-01-e49.toml` | E ratio ≤ 49, E V ≤ 1165 mV, 0x650 write, caps 220/240, dwell 5 s |
| `policies/session-2026-10-02-points.toml` | as above + `0x10 P2 1..9` reads + `0x11 P2 1..6` point-offset writes, dwell 0 (operator 2026-10-02) |

## 4. Machine facts that recipes depend on (details in `lnx-first-read.md`, `registers.md`)
- E V/F curve per cluster: 6 fused points + OC point; point 5 = 40×, point 6 = 46× on every cluster; `P2 = 7` → rc `0x11`; the OC point (48×
  stock) is the `P2 = 0` word and, at stock (V target 0), tracks point 6 → a point-6 offset moves 46–48×. Monotonicity V(n) ≥ V(n−1): measure the
  margin with `vfcurve.py` before lowering a point (stock margin V6 − V5 ≈ 170–190 mV).
- Warm reboot keeps E `0x11` words, restores `0x1D`, `0x650`, P words. Cold boot restores everything. Point offsets across reboots: not measured.
- `0x650` is runtime-writable and package-scoped; raising E above 48× needs `0x11` + `0x1D` + `0x650`.

## 5. To fold into the arltuner CLI (the final tool) — candidates
`arltuner status` (plain MSRs + one guarded read per cluster/core, decoded) · `arltuner vf-curve` (vfcurve.py) · `arltuner msr-read` (read_plain_msrs.py
through the guard's msr_allow) · a non-interactive confirmation mode for an approved, operator-signed boot profile (re-apply at boot; needs a
decision — today confirmation is human-only by design) · seeded rehearsal as `apply --rehearse-from-device` (read the real words, simulate, then ask).
