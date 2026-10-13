# 10 CLI reference

This page lists the command-line tools: the `arltuner` CLI, the GUI launcher, the helper script and the scripts in `tools/`. It is for scripting,
for working without the GUI, and for developers. Every command and flag below is taken from the argument parsers and script headers.

**The GUI is the main interface.** The CLI is the direct path to the same guarded write code. A mailbox command on real hardware sends WRMSRs
to MSR `0x150`: use it as carefully as the GUI.

## 1. Overview
| Command | Purpose | Touches hardware? |
|---|---|---|
| `python3 tools/arltuner-gui.py` | start the web GUI; it connects to the root helper service, no password ([03](03-first-run.md)) | via the helper service |
| `python3 tools/arltuner.py topology` | print the core map from sysfs | reads sysfs only |
| `python3 tools/arltuner.py plan` | dry-run a profile against the simulator | no |
| `python3 tools/arltuner.py apply --device` | apply a profile through the guard | yes, writes |
| `python3 tools/arltuner.py read-vf` / `read` / `vf-dump` / `point-offset` | guarded mailbox reads / one offset write | yes (reads are WRMSRs) |
| `bash tools/lnx/restore_tuning.sh` | restore the accepted state ([09](09-profiles-and-restore.md)) | yes, writes |
| `sudo python3 -I tools/arltuner.py accepted-migrate` | turn an older accepted list of profiles into one complete profile ([09 §3](09-profiles-and-restore.md)) | no (files only) |
| `python3 tools/arltuner.py compare-accepted --readback F --expected F` | compare a read-back with the accepted words (OK / EQUIVALENT / BAD) | no (reads two files) |
| `python3 tools/run_tests.py` | the offline test suite | no |

## 2. `tools/arltuner.py` (the `arltuner` CLI)
```
python3 tools/arltuner.py <command> [options]        # same as: python -m arltuner (with ./src on the path)
```
Commands: `topology`, `plan`, `apply`, `read-vf`, `read`, `vf-dump`, `point-offset`, `compare-accepted`, `accepted-migrate`.

### Safety model of every hardware command
- **Environment flag.** Mailbox traffic needs `ARLTUNER_ALLOW_MAILBOX=1` exported by *you*. The program never sets it. Without it the command refuses
  and sends nothing.
- **Typed confirmation.** The CLI prints a random token and requires you to type `CONFIRM <token>` exactly. There is no option to pass the token as an
  argument.
- **Guard, policy, audit, health.** Every request goes through the write guard: allowlist, policy limits, one transaction at a time (a lock file),
  a health bracket before and after, read-back, and an audit-log line.
- **Modes.** `--sim` runs against a software simulator of the reference 285K (no hardware; the default for the read commands); `--device` uses
  `/dev/cpu/N/msr` (Linux, needs root); `--device-win` is Windows 11 as Administrator through ThrottleStop's driver
  ([09 §4](09-profiles-and-restore.md); needs the accepted state's `fingerprint.json`, see `--fingerprint`). `apply` requires exactly one of the three.
- Without `--policy` the CLI uses a **built-in conservative policy** (no raise, small steps). To use your own limits pass
  `--policy /etc/arltuner/policy.toml`, as the restore scripts do. **Every limit is the policy file's number** (D-039): ratio and voltage ceilings, the offset window and the step
  limits are yours to edit, up to what your CPU reports (mailbox commands `0x01` for the ratio and `0x28` for the voltage, read live in the session) and the register field
  encodings (offset -1000 ... +999 mV). A refusal names where its limit comes from ([12 §4](12-safety-and-internals.md)). A ratio raise costs one extra guarded `0x01` read
  per core kind, which the plan counts.

### Common options (`apply`, `read-vf`, `read`, `vf-dump`, `point-offset`)
| Option | Meaning |
|---|---|
| `--sim` / `--device` / `--device-win` | backend (see above) |
| `--policy FILE` | policy TOML: its numbers are your limits (see [12 §4](12-safety-and-internals.md)) |
| `--audit FILE` | audit log. Default on an installed machine: `/var/lib/arltuner/audit.jsonl`; else `$XDG_STATE_HOME/arltuner/audit.jsonl` (default `~/.local/state/arltuner/audit.jsonl`; Windows `%LOCALAPPDATA%\arltuner\audit.jsonl`) |
| `--lock FILE` | lock file. Default on an installed machine: `/run/arltuner/mailbox.lock`; else `/run/lock/arltuner-mailbox.lock`, else the temp dir |
| `--operator NAME` | name recorded in the audit log (default `$USER`) |
| `--fingerprint FILE` | `--device-win` only: the accepted state's `fingerprint.json` (default `%LOCALAPPDATA%\arltuner\accepted\fingerprint.json`; the Windows scripts pass the one in `%ProgramData%\arltuner\accepted`) |
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
**`apply PROFILE.toml (--sim | --device | --device-win) [--neutralize-residual] [common options]`**: plan, then apply through the guard after the typed confirmation.
The whole plan is budgeted against the policy before anything is sent. A **partial** profile (no `complete = true`) that asks for a step beyond the limits is refused
with the next allowed step. A **complete** profile (D-044, the kind the app saves and accepts) is applied in one run: only the words that differ from what the CPU
holds are written, a left-over OC ratio is neutralised (`--neutralize-residual` is implied), and a change beyond the step limits is split into rounds within
the run; whether the rounds fit is decided before the first write (refused with `STEP_LIMIT` otherwise, nothing sent). A complete profile made on another CPU (a unit missing, or
another top fused ratio) is refused. After a `0x1D` write, `apply --device` on Linux lets `scaling_max_freq` follow the new `cpuinfo_max_freq` (a cap you lowered is never raised;
[09 §3](09-profiles-and-restore.md)). `--neutralize-residual` does the residual-OC-point rule for a partial profile too (the restore scripts pass it).

**`compare-accepted --readback FILE --expected FILE [--fingerprint FILE]`**: no hardware. Compares a `vf-dump --json` read-back with `EXPECTED-words.txt`:
identical words are OK, a left-over OC ratio that runs the accepted voltages is EQUIVALENT (a success), anything else is BAD. Without `--fingerprint` (its fused `0x09` words
tell a left-over OC point from an OC point the accepted state has) only identical words match. Exit 0 when no unit is BAD, 2 otherwise, 1 when an input cannot be read.
The restore scripts use it ([09 §3](09-profiles-and-restore.md)).

**`accepted-migrate [--dir DIR] [--dry-run] [--sim]`**: no hardware, run as root. Turns an accepted state that still lists several profiles in `CURRENT` (written
before D-044) into one complete profile built from its `EXPECTED-words.txt` (never from a read), keeps `EXPECTED-words.txt` and `fingerprint.json`, copies the old
directory to `accepted.bak-<UTC time>/` next to it and swaps the new one in atomically. `--dry-run` prints the profile and how it differs from the old list and
changes nothing. `--dir` defaults to `/etc/arltuner/accepted`; `--sim` uses the simulated reference layout instead of this machine's (tests). A state that names
one profile already is left alone ("nothing to migrate"). Exit 0 when migrated or nothing to migrate, 2 when refused (nothing changed), 1 for a usage error. It takes none of the
common options and sends nothing to the CPU. The option `--accepted-before` of earlier versions no longer exists (the accepted state is one profile).

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
Starts the web server and connects it to the root helper service on `/run/arltuner/helper.sock` (installed by `install.sh`; no password, no polkit; a refusal is printed with its fix and the exit status is 2). Options `--open`, `--no-helper`, `--helper CMD ...`, `--launcher PATH`,
`--web-root DIR`: see [03](03-first-run.md). Running it again while it is running prints a fresh one-time link.

## 4. `tools/arltuner-helper.py`
`python3 tools/arltuner-helper.py --sim [--policy F --audit F --lock F]` runs the helper against the simulator from a checkout (JSON lines on stdin/stdout;
developers only). `--socket` serves one client at a time on a Unix socket instead (non-root, simulator: `--socket-path FILE --helper-toml FILE`). `--device` **refuses to start from a checkout** (helper code is never
run from a user-writable tree); the installed copy is the `ExecStart` of the socket-activated `arltuner-helper.service`, or you run `sudo /usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket`
yourself without systemd ([02](02-install.md)). Its protocol is contract 8 and 9 and the G-144 addendum of `docs/ARCHITECTURE.md`.

## 4a. `tools/screenshots.py`
Captures the README images of the web page with headless Chrome (standard library only). `python3 tools/screenshots.py --sim` starts the GUI against the simulator (no hardware);
`--launcher PATH` logs in to a GUI you started, with a **read-only** session (scope `read`, [12 §8](12-safety-and-internals.md)); `--out DIR`, `--only a,b`. It only
displays what the page shows and never presses Read, Review, Apply or Measure. Developers only.

## 5. Scripts in `tools/lnx/`
| Script | What | Hardware access |
|---|---|---|
| `restore_tuning.sh [--check] [--from DIR] [--policy FILE] [--orchestrated]` | restore the accepted state (one profile, one run) and compare word for word; EQUIVALENT counts as success; refuses a `CURRENT` that lists several profiles with the `accepted-migrate` command ([09](09-profiles-and-restore.md)) | writes via the guard |
| `reapply_current.sh [...]` | another name for `restore_tuning.sh`; same options | writes via the guard |
| `preflight.sh` | read-only state record (kernel, taint word, msr parameters, lockdown, Secure Boot state, other tuning tools, audit-log size, recent kernel lines); writes `captures/lnx/preflight-<UTC>.txt` | none (some lines need root and say so) |
| `health_snapshot.sh LABEL [--since FILE]` | before/after record: taint, PCI count, module count, kernel-log length; with `--since` prints new kernel lines minus the expected `msr` notices | none |
| `read_plain_msrs.py [--cpus LIST] [--msrs LIST]` (sudo) | plain `rdmsr` of the allowlisted non-mailbox MSRs (`0x1AD 0x1AE 0x650 0x651 0xCE 0x194 0x198`); refuses any other MSR | read-only |
| `vfcurve.py --cpus 8-11 --ratios 40,46,48 [--out FILE]` (sudo) | measure the requested VID at fixed ratios on one group (E-cluster or one P-core) | read-only MSRs; writes cpufreq caps |
| `percore_stress.py [--cpus] [--from-ratio] [--to-ratio] [--no-uncapped] [--seconds] [--out]` (sudo) | per-core sweep ([08 §5](08-stability-testing.md)) | read-only MSRs; writes cpufreq caps |
| `orchestrator_confirm.py CMD ...` | answers the CLI's `CONFIRM <token>` prompt for one command | whatever the wrapped command does |

`orchestrator_confirm.py` exists for the project's own automated sessions: it defeats the "a human types the confirmation" step. **Do not use it unless
you understand that you are removing that safety step**, and never schedule it or run it unattended (cron, systemd, boot hooks); its own header forbids it.

## 6. Scripts in `tools/win/`
| Script | What |
|---|---|
| `restore_tuning.ps1 [-From DIR] [-Policy FILE] [-Before] [-Orchestrated]` | restore the accepted state (one profile, one run) and compare word for word; EQUIVALENT counts as success. Refuses a `CURRENT` that lists several profiles ([09 §3](09-profiles-and-restore.md)) |
| `Restore-Tuning.cmd` | double-click launcher: elevates (UAC), runs `restore_tuning.ps1` |
| `reapply_current.ps1 [-Check] [-From DIR] [-Policy FILE]` | apply the one profile in `CURRENT`, one confirmation, no compare; `-Check` = read-only dump |
| `preflight.ps1` | read-only checks (elevation, Memory integrity, driver blocklist, XTU service, other mailbox clients, Python, driver state) |
| `ts_driver.py init-dir \| carve --exe PATH \| start \| install \| status \| remove \| check-dir DIR` | prepare the admin-only directory, carve the driver from your own ThrottleStop exe, manage the per-run service ([09 §4](09-profiles-and-restore.md)) |

Windows is a restore-only path and is **untested on real Windows with the G-138/G-142 changes**: the first run is the bring-up test.

## 7. Tests
```
python3 tools/run_tests.py
```
Runs the whole offline suite with `unittest` against the simulator and fake sysfs. No hardware, no root. The browser tests are opt-in
(`ARLTUNER_BROWSER_TESTS=1`, per the G-133 task); they need Playwright and Chrome, which the project does not install, and are not required for normal use.

Next: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) · Previous: [09 Profiles and restore](09-profiles-and-restore.md)
