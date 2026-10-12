# 11 Troubleshooting and FAQ

This page lists symptoms, their usual causes and fixes, the mailbox return codes you may see in the Audit tab, and short answers to common questions. It is
for anyone whose tool is read-only, refuses a plan, or whose tuning misbehaves.

## 1. Quick triage
1. Open **System → Preflight**. Is the mode `write`? If not, fix the failing check first ([03 §3](03-first-run.md)).
2. Open **Audit** and look at the newest lines: `refused`, `error`, `trip`, or a non-zero `rc`.
3. If the machine crashed or behaves oddly: cold boot, then retry with a smaller change ([12 §10](12-safety-and-internals.md)).

## 2. The tool does not start or is read-only
| Symptom | Cause | Fix |
|---|---|---|
| Banner "Read-only mode. Mailbox requests are disabled, so Tune cannot read or apply. Measuring and the dashboard still work." | a preflight check of status `read-only` | read the reasons in the banner; see the rows below |
| `lockdown`: kernel lockdown is `[integrity]` or `[confidentiality]` | Secure Boot enabled | disable Secure Boot in the BIOS, or boot a kernel without lockdown ([01 §4](01-requirements.md)) |
| `msr_device`: `/dev/cpu/N/msr` missing | the `msr` driver is not loaded or built in | `sudo modprobe msr` (an explicit operator step) |
| `msr_allow_writes`: `msr.allow_writes=off` | the kernel refuses MSR writes | boot with `msr.allow_writes=on` or write `on` to `/sys/module/msr/parameters/allow_writes` as root |
| `hypervisor` | running in a virtual machine | run on bare metal |
| `cpuid` / `topology` | not an Arrow Lake-S CPU or an unexpected core layout | not supported; nothing will be written |
| `pkexec`, `polkit`, `polkit_agent` | polkit missing, or no authentication agent in the session | install polkit; use a normal desktop login |
| `python` refuse | Python older than 3.12 | update Python |
| no password prompt, "helper: n/a" | helper not installed or `--no-helper` | [02](02-install.md) |
| "Session ended. Open a fresh launcher link to continue." | the session cookie expired (30 min idle, 12 h total) or the GUI restarted | run `python3 tools/arltuner-gui.py --open` again |
| Install refuses: "an arltuner-helper process is running" | the GUI is still running | Ctrl+C in its terminal, then re-run `install.sh` |
| Self-check failed after install | the installed tree is not trusted | `sudo bash uninstall.sh`, check the checkout is not modified and has no symlinks, install again |

## 3. Refusals while planning or applying
| Message / code | Meaning | Fix |
|---|---|---|
| `REFUSED POLICY_RANGE` | a value is outside the policy range (offset, voltage target or ratio) | lower the value, or change the policy ([12 §4](12-safety-and-internals.md)) |
| `STEP_LIMIT` | the plan needs a larger single step than the policy allows | change fewer things, or in smaller steps |
| `TURBO_ORDER` | a turbo bucket is higher than the bucket to its left | each bucket must be at or below the one before it ([07 §5](07-overclocking.md)) |
| `NO_READ` | nothing was read this boot | **Read ratios & voltages** in Tune |
| "This plan expired" | plans expire after 10 minutes | close the dialog and press **Review changes** again |
| `NOTHING_TO_DO` / "Nothing would change" | the CPU already holds the planned words | no action needed |
| `NOT_APPLIED` (Set as accepted) | the saved profile is not what the CPU holds | apply it first ([09](09-profiles-and-restore.md)) |
| `NO_ACCEPTED_STATE` | **Restore accepted** with no accepted state | set one first |
| `NO_ENV_FLAG`, `BAD_CONFIRMATION` (CLI) | `ARLTUNER_ALLOW_MAILBOX=1` not exported, or the confirmation does not match | [10 §2](10-cli-reference.md) |
| `AUDIT_UNAVAILABLE` | the audit log cannot be read or written, so nothing is sent | inspect the file; never delete or truncate it to get past this ([12 §5](12-safety-and-internals.md)) |
| `LOCKED` | another mailbox transaction holds the lock | wait for it; close other tuning tools |
| `TRIPPED` | an earlier error, mismatch or health failure tripped the guard in this session | stop, check the audit log and `dmesg`, cold boot before more writes |
| `HEALTH_GATE` | the before/after health check found a change | read the detail; unexpected kernel messages mean stop |
| `NO_INVOKER` (Stress) | the helper does not know which user to run the load as | start the GUI from the launcher, not as root |
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
| E-cluster ran 48x instead of an OC ratio of 49x | the kernel's cpufreq cap, not the hardware | the tool lifts `scaling_max_freq` for an OC raise (G-126); if you set your own cap lower it wins [MEASURED lnx 2026-10-12] |
| 56x / 57x point on a non-favored P-core is "not measured" | the core never runs that ratio (`max_turbo`) | not an error ([05 §4](05-measuring-curves.md)) |
| A ratio is "unreachable (`cap_granularity`)" | no cpufreq cap produces it | it is interpolated if both neighbours were measured |
| Measuring says "turbo is off" | Power Saver profile (`no_turbo` = 1) | switch to Balanced or Performance and measure again |
| Warm reboot left odd state | the BIOS reset only part of the tuning | Restore accepted again (it reads live words) or cold boot |
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

**Do I need to disable Secure Boot?** Usually yes: it normally turns on kernel lockdown, which blocks MSR writes. Without writes the dashboard and the Curves
measurement still work but Tune cannot read or apply.

**Does it apply tuning automatically at boot?** No. Unattended autostart is an unsigned proposal (D-021) and is not implemented.

**Does it work on Windows?** There is no Windows GUI. A PowerShell restore exists, is tied to the reference chip, and is untested from this repository
([09 §4](09-profiles-and-restore.md)).

**Can I use XTU, ThrottleStop or HWiNFO at the same time?** No. They are other mailbox clients. Close them before tuning.

**Why does OCCT show errors but no WHEA/MCE?** Unstable undervolts on the reference chip failed silently ([08 §1](08-stability-testing.md)).

**What are the "community hint" warnings?** E40 and WHEA warnings come from community or reference-chip reports, not from a rule of the hardware. They are
labelled that way; use them as hints.

**How do I clear a stuck state?** Cold boot. If a stale cpufreq journal remains, restarting the GUI applies it. The accepted state is changed only by
**Set as accepted**.

**Where are my files?** Policy and accepted state `/etc/arltuner/`; curves, profiles, stress results, state and the audit log `/var/lib/arltuner/`; launcher
`$XDG_RUNTIME_DIR/arltuner/`; script captures `captures/lnx/` in your checkout.

**How do I report a problem?** Include the preflight report (System tab), the last Audit lines, your CPU model and BIOS version, and what you applied.

Previous: [10 CLI reference](10-cli-reference.md) · Next: [12 Safety and internals](12-safety-and-internals.md)
