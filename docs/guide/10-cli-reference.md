# 10 CLI reference

This page lists the command-line tools: the `arltuner` CLI, the GUI launcher, the helper script and the scripts in `tools/`. It is for scripting,
for working without the GUI, and for developers. Every command and flag below is taken from the argument parsers and script headers.

**The GUI is the main interface.** The CLI is the operator's direct path to the same guarded write code. A mailbox command on real hardware sends WRMSRs
to MSR `0x150`: use it as carefully as the GUI.

## 1. Overview
| Command | Purpose | Touches hardware? |
|---|---|---|
| `python3 tools/arltuner-gui.py` | start the web GUI and root helper ([03](03-first-run.md)) | via the helper, after polkit |
| `python3 tools/arltuner.py topology` | print the core map from sysfs | reads sysfs only |
| `python3 tools/arltuner.py plan` | dry-run a profile against the simulator | no |
| `python3 tools/arltuner.py apply --device` | apply a profile through the guard | yes, writes |
| `python3 tools/arltuner.py read-vf` / `read` / `vf-dump` / `point-offset` | guarded mailbox reads / one offset write | yes (reads are WRMSRs) |
| `bash tools/lnx/restore_tuning.sh` | restore the accepted state ([09](09-profiles-and-restore.md)) | yes, writes |
| `python3 tools/run_tests.py` | the offline test suite | no |

## 2. `tools/arltuner.py` (the `arltuner` CLI)
```
python3 tools/arltuner.py <command> [options]        # same as: python -m arltuner (with ./src on the path)
```
Commands: `topology`, `plan`, `apply`, `read-vf`, `read`, `vf-dump`, `point-offset`.

### Safety model of every hardware command
- **Environment flag.** Mailbox traffic needs `ARLTUNER_ALLOW_MAILBOX=1` exported by *you*. The program never sets it. Without it the command refuses
  and sends nothing.
- **Typed confirmation.** The CLI prints a random token and requires you to type `CONFIRM <token>` exactly. There is no option to pass the token as an
  argument.
- **Guard, policy, audit, health.** Every request goes through the write guard: allowlist, policy limits, one transaction at a time (a lock file),
  a health bracket before and after, read-back, and an audit-log line.
- **Modes.** `--sim` runs against a software simulator of the reference 285K (no hardware; the default for the read commands); `--device` uses
  `/dev/cpu/N/msr` (Linux, needs root); `--device-win` is Windows 11 as Administrator through ThrottleStop's driver
  ([09 §4](09-profiles-and-restore.md); reference-chip only). `apply` requires exactly one of the three.
- Without `--policy` the CLI uses a **built-in conservative policy** (no raise, small steps). To use your installed limits pass
  `--policy /etc/arltuner/policy.toml`, as the restore scripts do.

### Common options (`apply`, `read-vf`, `read`, `vf-dump`, `point-offset`)
| Option | Meaning |
|---|---|
| `--sim` / `--device` / `--device-win` | backend (see above) |
| `--policy FILE` | reviewed policy TOML (see [12](12-safety-and-internals.md)) |
| `--audit FILE` | audit log. Default on an installed machine: `/var/lib/arltuner/audit.jsonl`; else `~/.local/state/linuxmon/audit.jsonl` (Windows `%LOCALAPPDATA%\linuxmon\audit.jsonl`) |
| `--lock FILE` | lock file. Default on an installed machine: `/run/arltuner/mailbox.lock`; else `/run/lock/linuxmon-mailbox.lock`, else the temp dir |
| `--operator NAME` | name recorded in the audit log (default `$USER`) |
| `--allow-taint-bit N` | kernel taint bit the health gate may ignore; repeatable. Use `--allow-taint-bit 2` on Linux (the first MSR write of a boot sets bit 2, expected) |

"Installed machine" means `/etc/arltuner/policy.toml` exists and passes the helper's trust check (D-030). If it exists but is untrusted, the CLI refuses
rather than guess between the installed and legacy paths. Under `sudo`, pass `HOME` through (the restore script does) or give `--audit` explicitly.

### Commands
**`topology [--json]`**: print the core map derived from sysfs (XTU name, Linux CPU, cluster, favored). Needs no privilege and no flags.

**`plan PROFILE.toml --sim [--policy FILE]`**: dry-run a profile against the simulator's state. `--sim` is required. Sends nothing. Prints the planned steps
and whether the policy allows them. Example for developers:
```
python3 tools/arltuner.py plan examples/reference-285k/profiles/p-step7.toml --sim --policy policies/default.toml
```
**`apply PROFILE.toml (--sim | --device | --device-win) [common options]`**: plan, then apply through the guard after the typed confirmation. The whole plan is
budgeted against the policy before anything is sent. Refuses on a step beyond the limits and tells you the next allowed step.

**`read-vf --cpu N [backend] [common]`**: ONE guarded read of command `0x10` (the V/F word) on one CPU.

**`read --cpu N --cmd C [--p1 N] [--p2 N] [backend] [common]`**: ONE guarded read of any allowlisted read command. `C` is decimal or `0x` hex (for
example `0x1C`). Only entries of kind `read` in the policy allowlist are accepted.

**`point-offset --cpu N [--p1 0|2] --p2 N --offset-mv X [--expect-ratio R] [backend] [common]`**: ONE guarded session: read V/F point `P2` (command `0x10`),
then write only its offset field (command `0x11`). `--p1 0` is a core (default), `--p1 2` is the ring (package-level; use `--cpu 0`). `--expect-ratio R`
refuses before any write unless the point's ratio field reads R. `X` is millivolts (rounded to 1/1024 V).

**`vf-dump [--cpus LIST] [--all-e] [--measure] [--json FILE] [backend] [common]`**: ONE guarded session that reads the V/F OC word and every point of the
P-cores and E-clusters. `--cpus 0-7,8,12` selects CPUs; the default is every P-core plus one CPU per E-cluster; `--all-e` dumps all E CPUs.
`--measure` also measures the requested VID of every point ratio per unit by capping cpufreq `scaling_max_freq` (restored afterwards), running `stress-ng`,
and reading MSR `0x198` through the guard; it needs `--device` and a policy `msr_allow` entry for `0x198`. (The GUI's Curves measurement is a separate
implementation that pins min and max and does not use `stress-ng`; see [05](05-measuring-curves.md).) `--json FILE` writes the same data as JSON.

### Exit codes
| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | usage or parse error (bad argument, profile or policy error, cannot open the audit log) |
| 2 | refused: environment flag missing, wrong confirmation, policy/topology refusal, plan not allowed, needs a higher limit |
| 3 | hardware error, timeout, non-zero completion code, unverified read-back or a tripped guard |

### A typical read-only session (Linux)
```
sudo env ARLTUNER_ALLOW_MAILBOX=1 USER="$USER" HOME="$HOME" \
  python3 tools/arltuner.py read --device --cpu 0 --cmd 0x1C \
  --policy /etc/arltuner/policy.toml --allow-taint-bit 2
```
It prints a token; type `CONFIRM <token>`. One request is sent, read back and logged. Do not loop it: the project's safety rule is **one transaction at a
time, never a sweep** ([12](12-safety-and-internals.md)).

## 3. `tools/arltuner-gui.py`
Starts the web server and, when installed, the root helper through `pkexec`. Options `--open`, `--no-helper`, `--helper CMD ...`, `--launcher PATH`,
`--web-root DIR`: see [03](03-first-run.md). Running it again while it is running prints a fresh one-time link.

## 4. `tools/arltuner-helper.py`
`python3 tools/arltuner-helper.py --sim [--policy F --audit F --lock F]` runs the helper against the simulator from a checkout (JSON lines on stdin/stdout;
developers only). `--device` **refuses to start from a checkout** (helper code is never run from a user-writable tree); the installed copy is started by
`pkexec` ([02](02-install.md)). Its protocol is contract 8 and 9 of `docs/ARCHITECTURE.md`.

## 5. Scripts in `tools/lnx/`
| Script | What | Hardware access |
|---|---|---|
| `restore_tuning.sh [--check] [--from DIR] [--policy FILE] [--orchestrated]` | restore the accepted state and compare word for word ([09](09-profiles-and-restore.md)) | writes via the guard |
| `reapply_current.sh [--check] [--from DIR] [--policy FILE]` | apply the profiles in `CURRENT`, one confirmation each | writes via the guard |
| `preflight.sh` | read-only state record (kernel, taint word, msr parameters, lockdown, Secure Boot state, other tuning tools, audit-log size, recent kernel lines); writes `captures/lnx/preflight-<UTC>.txt` | none (some lines need root and say so) |
| `health_snapshot.sh LABEL [--since FILE]` | before/after record: taint, PCI count, module count, kernel-log length; with `--since` prints new kernel lines minus the expected `msr` notices | none |
| `read_plain_msrs.py [--cpus LIST] [--msrs LIST]` (sudo) | plain `rdmsr` of the allowlisted non-mailbox MSRs (`0x1AD 0x1AE 0x650 0x651 0xCE 0x194 0x198`); refuses any other MSR | read-only |
| `vfcurve.py --cpus 8-11 --ratios 40,46,48 [--out FILE]` (sudo) | measure the requested VID at fixed ratios on one group (E-cluster or one P-core) | read-only MSRs; writes cpufreq caps |
| `percore_stress.py [--cpus] [--from-ratio] [--to-ratio] [--no-uncapped] [--seconds] [--out]` (sudo) | per-core sweep ([08 §5](08-stability-testing.md)) | read-only MSRs; writes cpufreq caps |
| `orchestrator_confirm.py CMD ...` | answers the CLI's `CONFIRM <token>` prompt for one command | whatever the wrapped command does |

`orchestrator_confirm.py` exists for the project's own automated sessions: it defeats the "a human types the confirmation" step. **Do not use it unless
you understand that you are removing that safety step**, and never schedule it or run it unattended (cron, systemd, boot hooks); its own header forbids it.

## 6. Scripts in `tools/win/`
`restore_tuning.ps1`, `Restore-Tuning.cmd`, `reapply_current.ps1`, `preflight.ps1`, `ts_driver.py`: see [09 §4](09-profiles-and-restore.md). Windows is a
restore-only path and is not tested from this repository.

## 7. Tests
```
python3 tools/run_tests.py
```
Runs the whole offline suite with `unittest` against the simulator and fake sysfs. No hardware, no root. The browser tests are opt-in
(`ARLTUNER_BROWSER_TESTS=1`, per the G-133 task); they need a browser and are not required for normal use.

Next: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) · Previous: [09 Profiles and restore](09-profiles-and-restore.md)
