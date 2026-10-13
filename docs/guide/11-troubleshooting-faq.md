# 11 Troubleshooting and FAQ

This page lists symptoms, their usual causes and fixes, the mailbox return codes you may see in the Audit tab, and short answers to common questions. It is
for anyone whose tool is read-only, refuses a plan, or whose tuning misbehaves.

## 1. Quick triage
1. Open **System → Preflight**. Is the mode `write`? If not, fix the failing check first ([03 §3](03-first-run.md)).
2. Open **Audit** and look at the newest lines: `refused`, `error`, `trip`, or a non-zero `rc`.
3. If the machine crashed or behaves oddly: cold boot, then retry with a smaller change ([12 §11](12-safety-and-internals.md)).

## 2. The tool does not start or is read-only
| Symptom | Cause | Fix |
|---|---|---|
| Banner "Read-only mode. Mailbox requests are disabled, so Tune cannot read or apply. Measuring and the dashboard still work." | a preflight check of status `read-only` | read the reasons in the banner; see the rows below |
| `lockdown`: kernel lockdown is `[integrity]` or `[confidentiality]` | Secure Boot enabled | disable Secure Boot in the BIOS, or boot a kernel without lockdown ([01 §4](01-requirements.md)) |
| `msr_device`: `/dev/cpu/N/msr` missing | the `msr` driver is not loaded or built in | `sudo modprobe msr` (an explicit step you take; the tool never does it) |
| `msr_allow_writes`: `msr.allow_writes=off` | the kernel refuses MSR writes | boot with `msr.allow_writes=on` or write `on` to `/sys/module/msr/parameters/allow_writes` as root |
| `hypervisor` | running in a virtual machine | run on bare metal |
| `cpuid` / `topology` | not an Arrow Lake-S CPU or an unexpected core layout | not supported; nothing will be written |
| `helper_service` | the helper socket is missing, not a root-owned socket, or `/etc/arltuner/helper.toml` admits nobody | `sudo bash install.sh`; `sudo bash install.sh --add-user NAME` (see the table below) |
| `python` refuse | Python older than 3.12 | update Python |
| "helper: n/a", calls answer 503 | no helper: `/run/arltuner/helper.sock` does not exist or `--no-helper` | [02](02-install.md) |
| "Session ended. Open a fresh launcher link to continue." | the session cookie expired (30 min idle, 12 h total) or the GUI restarted | run `python3 tools/arltuner-gui.py --open` again |
| Install refuses: "an arltuner-helper process is running" | a helper the installer could not stop | `sudo systemctl stop arltuner-helper.socket arltuner-helper.service` (or SIGTERM a helper you started by hand), then re-run `install.sh` |
| Self-check failed after install | the installed tree is not trusted | `sudo bash uninstall.sh`, check the checkout is not modified and has no symlinks, install again |

### The GUI cannot connect to the helper
`python3 tools/arltuner-gui.py` prints `arltuner-gui: <message>` and exits with status 2. There is no password and no polkit; the messages name the fix:

| Message (shortened) | Cause | Fix |
|---|---|---|
| `warning: no helper: /run/arltuner/helper.sock does not exist (not installed? run: sudo bash install.sh). Starting without one` | nothing is installed (checkout run); the page loads but calls answer 503 | `sudo bash install.sh` |
| `the helper is not installed or not started: ... does not exist. Run: sudo bash install.sh (it enables arltuner-helper.socket)` | the socket vanished between check and connect, or systemd is not running the socket unit | `sudo bash install.sh`, or `sudo systemctl enable --now arltuner-helper.socket` |
| `nothing is listening on ...: start it with: sudo systemctl enable --now arltuner-helper.socket` | the socket node exists but no service owns it (for example after a manual helper stopped without systemd) | the command in the message; without systemd start the helper by hand ([02](02-install.md)) |
| `the helper refused this user: not in the allowlist (/etc/arltuner/helper.toml). Run: sudo bash install.sh --add-user NAME` | your user is not in `[access] users` | run the command with your user name; no re-login needed, just start the GUI again |
| `another client is connected to the helper (another arltuner GUI?). Close it, then start this one again.` | the helper serves one client at a time | Ctrl+C the other GUI (or wait for it to end), start again |
| `permission denied on the helper socket ...: its file mode is wrong (it should be root-owned, mode 0666 ...)` | the socket node was changed | `sudo bash install.sh` repairs it |
| `... is not a socket owned by root; not connecting to it` / `the process listening on ... is uid N, not root; not talking to it` | something other than the real helper owns the path; the GUI refuses to send it anything | find out what (`ls -l /run/arltuner`, `ss -xlp`), remove it, `sudo bash install.sh` |
| `the helper closed the connection right away (it may have refused to start). See: journalctl -u arltuner-helper -n 30` | the helper started and exited, for example a failed trust check or a bad `helper.toml` | run the `journalctl` command; `sudo bash install.sh` re-runs the self-check |
| `the helper did not answer within N s. See: journalctl -u arltuner-helper -n 30` | the helper hung | same |

Other things to know: the helper exits by itself after about 10 idle minutes and systemd starts it at the next connection, so that is normal; if the GUI is running while you reinstall or stop the helper, the page shows
"helper: n/a" and you start the GUI again; `helper.toml` with a syntax error admits **nobody** (fail closed), and the preflight check `helper_service` says why.

## 3. Refusals while planning or applying
| Message / code | Meaning | Fix |
|---|---|---|
| `REFUSED POLICY_RANGE` | a value is outside a limit. The message says whose: `policy ratio_max P = 60 (/etc/arltuner/policy.toml; ...)`, `CPU limit ... (cmd 0x01 ...)` or `field encoding` | lower the value; for a policy limit edit `/etc/arltuner/policy.toml` (your CPU's own limit and the field width cannot be edited) ([12 §4](12-safety-and-internals.md)) |
| `STEP_LIMIT` (a partial profile) | the profile asks for a larger single step than your policy's `max_*_step` allows; the message ends with the next allowed step | change fewer things, or apply the steps one after the other |
| `STEP_LIMIT` (Restore accepted or a complete profile) | the change is further from the CPU's words than one step limit, and the guarded rounds that would bridge it do not fit: more than 16 rounds, or more operations than the session cap or this boot's remaining budget. The message names the unit and field that needs the most rounds. Nothing was sent | raise `max_offset_step_mv` / `max_v_step_mv` / `max_ratio_step` or the operation caps in `/etc/arltuner/policy.toml`, or bring the CPU closer first ([09 §3](09-profiles-and-restore.md)) |
| `SESSION_LIMIT` / `BOOT_LIMIT` | the plan, with all its rounds, needs more mailbox operations than one session or the rest of this boot's budget allows. Nothing was sent | raise the cap in your policy, or apply less at once |
| `ACCEPTED_CHAIN` | `/etc/arltuner/accepted/CURRENT` still lists several profiles (an accepted state from before D-044). Restore refuses to replay a list | run `sudo python3 -I tools/arltuner.py accepted-migrate --dry-run`, then without `--dry-run`; the old directory is kept as `accepted.bak-<UTC time>/` ([09 §3](09-profiles-and-restore.md)) |
| `PROFILE_TOPOLOGY` | a complete profile does not fit this CPU: a unit is missing or named twice ("it was made on another CPU layout"), or a unit reads another top fused ratio than the profile recorded ("it belongs to another CPU"). Nothing was sent | use a profile made on this CPU; do not edit `fused_top_ratio` to get past it |
| `TURBO_ORDER` | a turbo bucket is higher than the bucket to its left | each bucket must be at or below the one before it ([07 §5](07-overclocking.md)) |
| `NO_READ` | nothing was read this boot. Also on **Save profile** and **Set as accepted**: a saved profile is the last read with your edits over it, so there must be a read ("read first") | **Read ratios & voltages** in Tune, then try again |
| "This plan expired" | plans expire after 10 minutes | close the dialog and press **Review changes** again |
| `NOTHING_TO_DO` / "Nothing would change" | the CPU already holds the planned words | no action needed |
| `NOT_APPLIED` (Set as accepted) | the saved profile is not what the CPU holds | apply it first ([09](09-profiles-and-restore.md)) |
| `STALE_READ` (Save profile, Set as accepted) | a mailbox or plain MSR write happened after the read it would record, so the read may no longer be what the CPU holds | **Read ratios & voltages** again, then save or accept |
| `NOT_REPRESENTABLE` (Save profile, Set as accepted) | a word cannot be written to a profile file exactly (profiles hold whole millivolts and adaptive voltage only), and the tool refuses rather than round | apply a value in whole millivolts and read again; static (override) voltage cannot be recorded |
| `EXISTS` (Save profile) | that profile name is taken (saved profiles are never overwritten) or is an accepted profile | pick another name |
| `NO_ACCEPTED_STATE` | **Restore accepted** with no accepted state | set one first |
| `NO_ENV_FLAG`, `BAD_CONFIRMATION` (CLI) | `ARLTUNER_ALLOW_MAILBOX=1` not exported, or the confirmation does not match | [10 §2](10-cli-reference.md) |
| `AUDIT_UNAVAILABLE` | the audit log cannot be read or written, so nothing is sent | inspect the file; never delete or truncate it to get past this ([12 §5](12-safety-and-internals.md)) |
| `LOCKED` | another mailbox transaction holds the lock | wait for it; close other tuning tools |
| `TRIPPED` | an earlier error, mismatch or health failure tripped the guard in this session | stop, check the audit log and `dmesg`, cold boot before more writes |
| `HEALTH_GATE` | the before/after health check found a change | read the detail; unexpected kernel messages mean stop |
| `NO_INVOKER` (Stress) | the connecting user is root (or unknown), and stress children are never run as root | run the GUI as your ordinary user (root can only connect if listed by name, and still cannot run stress) |
| `TOOL_MISSING` | `stress-ng` or `stressapptest` not installed | install it ([01 §5](01-requirements.md)) |
| `MEM_RANGE` | not enough free memory for the memory screen | pick a smaller size or Automatic |
| `STALE_JOURNAL`, `RESTORE_FAILED` | cpufreq limits from an earlier run could not be put back | check `scaling_max_freq` / `scaling_min_freq` in System before anything else; restarting the helper applies a stale journal |

## 4. Mailbox return codes (Audit tab, `rc` / `rc_name`)
From `registers.md` "Return codes" [DECOMPILED registers.md]:

| rc | Name | Typical cause |
|---|---|---|
| 0x00 | Pass | |
| 0x01 | OCLocked | BIOS **OC Lock** is on ([01 §2](01-requirements.md)) |
| 0x02 | UnsupportedDomain | that domain is not exposed on this CPU |
| 0x03 | RatioExceedsLimits | the ratio is above what the CPU allows |
| 0x04 | VoltageExceedsMax | the target is above the CPU's maximum |
| 0x05 | OCNotSupported | hardware overclocking disabled |
| 0x08 | IllegalCommandSelectedInParam1 | wrong domain/parameter |
| 0x10 | InvalidParam | |
| 0x11 | not in the enum (unknown) | **a V/F point index beyond the table**; expected when the dump walks to the end of a unit's points [MEASURED lnx 2026-10-02] |
| 0x12 | VoltageExceedsLimit | |
| 0x13 | UndervoltProtection | BIOS **Undervolt Protection** is on |
| 0x1F | UnrecognizedCommand | |

## 5. Tuning problems
| Symptom | Likely cause | What to do |
|---|---|---|
| Apply succeeds, read-back matches, but nothing changed in voltage | the monotonic clamp, or firmware ignoring writes | **Measure again** and compare ([04 §7](04-arrow-lake-vf-primer.md)); check UVP/OC Lock |
| Apply "succeeds" but the next read shows the old words | firmware silently ignoring writes (seen on other platforms) [COMMUNITY] | check UVP, OC Lock, `OcSupport`; trust the read-back, not the absence of an error |
| Board Vcore did not drop although I lowered a core | that unit does not set the highest voltage | lower the unit and point that does ([04 §9](04-arrow-lake-vf-primer.md)) |
| Crash, reboot or freeze under load | unstable tuning | cold boot (reverts all), then less negative offsets or lower ratio |
| Stress screen reports errors | unstable | treat as unstable; revert the last change ([08](08-stability-testing.md)) |
| Stress screen clean but a long test fails | the screen was too short | trust only the long gate |
| E-cluster ran 48x instead of an OC ratio of 49x | the kernel's cpufreq cap, not the hardware | after a favored-ratio change the tool makes `scaling_max_freq` follow `cpuinfo_max_freq` (G-126, G-147; in the GUI, in `arltuner apply --device` and so in the restore script), but only when the cap sat at the old maximum: a cap you set lower is never raised [MEASURED lnx 2026-10-12] |
| 56x / 57x point on a non-favored P-core is "not measured" | the core never runs that ratio (`max_turbo`) | not an error ([05 §4](05-measuring-curves.md)) |
| A ratio is "unreachable (`cap_granularity`)" | no cpufreq cap produces it | it is interpolated if both neighbours were measured |
| Measuring says "turbo is off" | Power Saver profile (`no_turbo` = 1) | switch to Balanced or Performance and measure again |
| Warm reboot left odd state | the BIOS reset only part of the tuning | Restore accepted again (it reads live words and writes only what differs) or cold boot |
| An E-cluster or P-core runs at the **stock** top-point voltage after write tests, although the accepted tuning has an offset there | a left-over OC point: the OC ratio and its adaptive voltage from the test are still in the unit, and cannot be written back to 0 at runtime | Restore accepted: it rewrites the OC word so the unit runs the accepted voltage and reports **EQUIVALENT**; a cold boot clears the left-over point ([09 §3](09-profiles-and-restore.md)) |
| The restore ended `RESTORED ... (1 equivalent: residual OC point ...)` | the unit still holds a left-over OC ratio, but its voltages are the accepted ones | nothing to fix; it is a success. A cold boot and a restore make it a plain OK |
| `cpuinfo_max_freq` of a cluster dropped (for example 4.9 to 4.7 GHz) after a write | a favored-ratio word (`0x1D`) lower than the unit's own maximum was written [MEASURED lnx 2026-10-13] | the tool no longer sends one as a side effect (a deliberate lower ratio still does). A cold boot restores the stock maximum; after a favored-ratio change `scaling_max_freq` follows only if it sat at the old maximum |
| After a cold boot everything is stock | expected | Restore accepted ([09](09-profiles-and-restore.md)) |
| "No accepted state yet" | you have not used Set as accepted | [09 §2](09-profiles-and-restore.md) |

## 6. FAQ
**Does anything survive a reboot?** No tool write does. A cold boot returns stock; a warm reboot may keep part of it ([04 §11](04-arrow-lake-vf-primer.md)).
Restore after every boot. Your BIOS settings do persist and stack with runtime writes.

**Why does *reading* need write permission?** Each mailbox request, reads included, is a WRMSR to MSR `0x150` ([04 §1](04-arrow-lake-vf-primer.md)).

**Is this safe?** It is a tool for advanced users. Writes are volatile, bounded by a policy, serialised, read back and logged, but a bad voltage can still crash
the machine or silently corrupt results, and over-voltage and heat can wear the CPU. You are responsible for what you apply. Only a long self-checking test
([08](08-stability-testing.md)) says a tuning is stable.

**Will it work on my CPU?** The code accepts the Arrow Lake-S family (family 6, model 198, stepping 2 listed). It was developed and verified on a Core Ultra 9 285K
only; other 200S parts should follow the same mailbox design but that is unverified [UNVERIFIED]. Everything shown is read live from your CPU; the reference numbers
in this guide are labelled examples.

**Does it work on laptop Arrow Lake parts or other vendors?** The platform check matches the desktop Arrow Lake-S CPUID (family 6, model 198); any other CPU makes the
app read-only. Whether a laptop part exposes the same mailbox is not tested here. AMD and older Intel are out of scope.

**Is there a password prompt?** No. The project uses no polkit. The GUI connects to a root helper service; the helper admits only the users listed in `/etc/arltuner/helper.toml`
(the installer adds the user who ran `sudo bash install.sh`; `sudo bash install.sh --add-user NAME` adds more). The trade-off is explained in [12 §7](12-safety-and-internals.md). You only need `sudo` to install,
to edit the policy and for the command-line restore script.

**Do I need to disable Secure Boot?** Usually yes: it normally turns on kernel lockdown, which blocks MSR writes. Without writes the dashboard and the Curves
measurement still work but Tune cannot read or apply.

**Does it apply tuning automatically at boot?** No. Unattended autostart is an unsigned proposal (D-021) and is not implemented.

**Does it work on Windows?** There is no Windows GUI. A PowerShell restore exists; it finds your cores through the `fingerprint.json` that **Set as accepted** records on Linux, so it is meant to work on any
Arrow Lake-S, for the machine the state was recorded on. It is untested on real Windows in its current form ([09 §4](09-profiles-and-restore.md)). If the Windows scripts say
"no fingerprint.json", press **Record CPU fingerprint for Windows** in Profiles (after a Tune read) and copy the accepted directory again.

**Can I use XTU, ThrottleStop or HWiNFO at the same time?** No. They are other mailbox clients. Close them before tuning.

**Why does OCCT show errors but no WHEA/MCE?** Unstable undervolts on the reference chip failed silently ([08 §1](08-stability-testing.md)).

**What are the "community hint" warnings?** E40 and WHEA warnings come from community or reference-chip reports, not from a rule of the hardware. They are
labelled that way; use them as hints.

**Can I raise the limits? I run extreme cooling.** Yes. Every limit is the numbers in `/etc/arltuner/policy.toml` (offset window, adaptive voltage ceiling, OC-ratio ceilings, step sizes). Edit them as root; a change applies from the
next read or plan. The hard ceilings are only what your CPU reports (mailbox command `0x01` for ratios, `0x28` for voltage) and the register field widths (offsets -1000 ... +999 mV). The defaults are conservative on
purpose, and going further is entirely at your own risk ([12 §4](12-safety-and-internals.md)).

**Is "EQUIVALENT" a failure?** No. It means the unit holds a left-over OC ratio that only a cold boot clears, and the restore set it so the unit runs the accepted
voltages. It counts as success; only `BAD` is a difference ([09 §3](09-profiles-and-restore.md)).

**Do I have to chain profiles or build an intermediate profile after a cold boot?** No. A profile is a complete tuning state that stands alone, and the accepted state is one
profile. Restore accepted applies it in one run and splits a large step into rounds itself; if that does not fit your policy it refuses with `STEP_LIMIT` before sending anything.
An older accepted state that is a list of profiles is converted once with `accepted-migrate` ([09 §3](09-profiles-and-restore.md)).

**What is a "partial (legacy)" profile?** One saved before profiles became complete. It holds only the rows that were changed and still loads and applies, over what the CPU
holds. **Set as accepted** on it records the complete state the CPU holds after you applied it, never the partial file.

**How do I clear a stuck state?** Cold boot. If a stale cpufreq journal remains, restarting the GUI applies it. The accepted state is changed only by
**Set as accepted** (and once by `accepted-migrate` if you had an older list of profiles).

**Where are my files?** Policy and accepted state `/etc/arltuner/`; curves, profiles, stress results, state and the audit log `/var/lib/arltuner/`; launcher
`$XDG_RUNTIME_DIR/arltuner/`; script captures `captures/lnx/` in your checkout.

**How do I report a problem?** Include the preflight report (System tab), the last Audit lines, your CPU model and BIOS version, and what you applied.

Previous: [10 CLI reference](10-cli-reference.md) · Next: [12 Safety and internals](12-safety-and-internals.md)
