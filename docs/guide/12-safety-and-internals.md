# 12 Safety and internals

This page explains how the tool protects the machine and your data: the write guard, the policy file, the audit log, the health check, the root helper
and the local web server. It is for anyone who wants to know what happens between pressing **Apply** and a word reaching the CPU, or who wants to change
the policy. Sources: `docs/SAFETY.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `src/arltuner/guard/`, `policies/default.toml`.

## 1. Architecture in one picture
```
browser (127.0.0.1) --HTTP/SSE--> arltuner-gui.py (your user: web server)
                                       |  JSON lines over a pipe
                                       v
                     root helper: pkexec /usr/lib/arltuner/arltuner-helper --device
                                       |
                              plan -> guard -> mailbox transport -> /dev/cpu/N/msr
                                       |            |
                                  audit log     health gate (before/after)
```
- The **web server** runs as you. It serves the page, checks the session, and forwards only an allowlisted set of operations with checked arguments
  to the helper. It never touches hardware.
- The **root helper** runs as root, started by `pkexec` through a polkit action that names exactly `/usr/lib/arltuner/arltuner-helper`. As root it accepts no
  command-line arguments but `--device`; its paths are constants (`/etc/arltuner/policy.toml`, `/etc/arltuner/helper.toml`, `/var/lib/arltuner/audit.jsonl`,
  `/etc/arltuner/accepted`); every file it opens is checked (regular, one link, owned by root, no group/other write, symlinks refused). It refuses to run from a
  checkout (SPEC §4.4, D-029).
- The **guard** is the only code allowed to send a mailbox request.

## 2. The write guard
Every mailbox request, a read included, passes these checks in order, and the first failure refuses it with **nothing sent** (`guard.py`):

| Check | Refusal | Meaning |
|---|---|---|
| guard tripped earlier | `TRIPPED` | after any transport error, read-back mismatch, failing health gate or unreleasable lock, everything is refused until a new session |
| environment flag | `NO_ENV_FLAG` | `ARLTUNER_ALLOW_MAILBOX=1` must be set by the human or the helper |
| confirmation | `BAD_CONFIRMATION` | the plan-bound token / typed confirmation did not match |
| CPU allowed | `CPU_NOT_ALLOWED` | the CPU is not in the policy's P/E set |
| allowlist | `NOT_ALLOWLISTED` | the command, `P1`, `P2` and kind (read/write) must be listed in the policy; default deny |
| baseline | `BASELINE_MISSING` | a write needs an earlier read of the same register in the session |
| bounds | `CHECK_FAILED` | the entry's check (for example `vf_bounded`, `vf_point_offset_bounded`, `turbo_ratio_bounded`) says the value is out of range |
| session / boot caps | `SESSION_LIMIT`, `BOOT_LIMIT` | operation counters (indicators by default, D-032) |
| audit log readable | `AUDIT_UNAVAILABLE` | limits that cannot be evaluated are not waived |
| dwell | `DWELL` | minimum time since the last attempt by any process (0 by default) |
| lock | `LOCKED` | one mailbox transaction at a time, machine-wide |
| health gate | `HEALTH_GATE` | the before/after health check |

After a **write**, the guard reads the word back through the same path and compares the whole word. A mismatch or an unreadable read-back trips the guard.
The guard never sleeps and never retries a failing write.

**Never work around a refusal.** A refusal names the cause; fix the cause (for example the BIOS gate, the policy value, or a missing read), not the guard.

## 3. One transaction at a time
The mailbox discipline comes from an incident on the project author's machine, recorded in `docs/SAFETY.md` ("2026-08-26 incident"): a broad sweep of mailbox
*get* requests was followed about 5.5 minutes later by an abrupt end of the boot, and on the next warm boot a PCI device (a Wi-Fi card) was missing until the AC power
was physically removed. Causality is unproven; the mailbox burst was the only identified low-level change [MEASURED on the operator's other repository].
The rule that follows is kept regardless of any limit setting:

- **One transaction at a time**, serialised by a lock file (`/run/arltuner/mailbox.lock`), never a sweep, loop or "enumerate everything".
- Every request is a pinned access to one CPU: poll MSR `0x150` until the busy bit (63) is clear (hard timeout, **abort without writing** on timeout), write, poll
  until the busy bit clears, read the status (bits 39:32; 0 = OK) and data (bits 31:0), then verify by an explicit read-back. No retry.
- Another tool using the mailbox at the same time is a second client: close HWiNFO, A-Tuning, a root OCCT.
- After a hang or unexplained device loss, power the machine fully off and remove AC power for a while before you trust it again; a driver reload is not enough
  (the incident's recovery).

The Tune page's read is about 124-136 requests, and an apply is tens of operations per unit (13 for one P-core and 53 for an E-cluster raise in the T-022
planning budget [SAFETY.md "Operation budget"]). They are sent inside a budget-checked plan, paced by the guard and bracketed by health checks, not as free-running sweeps (D-018).

## 4. The policy file
`/etc/arltuner/policy.toml` is root-owned, seeded from `policies/default.toml` and never overwritten by the installer ([02](02-install.md)). The helper trust-checks
it on every start. It holds limits and the allowlist:

| Key | Meaning | Default |
|---|---|---|
| `cpus`, `e_cpus` | CPUs the guard may address | `"auto"` (from the live topology) |
| `max_operations_per_session`, `max_operations_per_boot` | operation counters | 100000 (indicators) |
| `min_dwell_s` | seconds between attempts, any process | 0.0 |
| `max_ratio_step`, `max_v_step_mv`, `max_offset_step_mv` | largest change per write | 8, 200, 200 |
| `offset_ceiling_mv`, `effective_offset_floor_mv` | offset range | +200, -200 |
| `assumed_bios_offset_mv` | BIOS offset assumed for the effective-offset budget | 0 for P and E |
| `[ratio_max]` `P`, `E` | ratio ceiling | `"auto"` (fused `0x1C` value; + 3 per D-038 once G-134 is in) |
| `[v_target_max_mv]` `P`, `E` | adaptive target ceiling, mV | `"auto"` (`0x28` hardware maximum minus one step) |
| `[[allow]]` | allowed mailbox request: `name`, `cmd`, `p1`, `p2`, `kind` (`read`/`write`), optional `check` | the commands in `policies/default.toml` |
| `[[msr_allow]]` | allowed plain MSR access: `msr`, `kind`, optional `cpus`, `check` | `0xCE 0x650 0x1AD 0x1AE 0x651 0x198` reads, `0x650` and `0x1AD` writes on CPU 0 |

A policy file **replaces the whole policy**: it must list every allowed request. The shipped allowlist, by command, allows writes only for: the per-core switch
(`0x15`, only setting the per-core-voltage bit), the V/F OC word `0x11` (`P2` = 0) and per-point words (`P2` 1..8), the favored-ratio word `0x1D`, the granular
ratio word `0x27`, a ring V/F point (`P1` = 2, CLI only), and the two turbo tables. Everything else is read-only or not listed.

Editing it: change a number, keep the file valid TOML, then restart the GUI (the helper reads it at start). If the file is wrong the helper refuses to start
with `POLICY_INVALID`. **Never widen the policy beyond what you can justify**; D-032 already opens offsets to +-200 mV and ratios to the CPU's own ceilings,
because the project treats you as an advanced user who accepts the risk. `"auto"` is read from your CPU each session; a number you write replaces it.
To go back: `sudo bash install.sh --reseed-config`.

## 5. The audit log
`/var/lib/arltuner/audit.jsonl` (root, 0600) is an append-only JSON-lines file. Each line has `ts` (UTC), `seq`, `event` and, when applicable, `cpu`, `cmd`/`p1`/`p2`
(hex), `req_data`, `purpose`, `operator`, `rc`, `rc_name`, `data`, `reason`, `detail`, `baseline`. Events: `refused`, `attempt`, `msr_read`, `result`, `readback`,
`error`, `trip`. Attempts carry the `boot_id`; the per-boot count is the number of `attempt` records for the current boot. The confirmation token is never logged.
The GUI operator is recorded as `gui:uid<N>`. The **Audit** tab shows the tail (last 256 KiB).

- A read-back is logged as `readback`, never `attempt`, so it does not count against the counters.
- If the log cannot be read, the guard refuses everything (`AUDIT_UNAVAILABLE`). Recovery is a human act: inspect the file, remove only a malformed line (a
  crash-truncated *last* line is tolerated), and **never delete or truncate the log to get past a refusal**. That would erase the history the limits rely on
  [SAFETY.md]. `uninstall.sh --purge` needs `--yes-reset-budget` for the same reason.
- The log keeps the legacy name `linuxmon` on the old path on purpose, so the per-boot counter is shared with the predecessor CLI.

## 6. Health check
Around each mailbox transaction the guard compares two snapshots (`health/gate.py`): the kernel taint word and its set bits, the PCI device set, the loaded
module set, and new kernel-log lines (the expected `msr` notices are hidden). The verdict is **CLEAN or not**; a changed PCI or module set, or a new taint bit
outside the allowed ones, fails the gate and trips the guard. It compares against the snapshot taken before *this* transaction, never against zero, because the
boot's taint word may already carry bits from earlier tools. Taint bit 2 (`CPU_OUT_OF_SPEC`) is expected on the first MSR write of a boot and is allowed through
`allow_taint_bits = [2]` in `/etc/arltuner/helper.toml` [MEASURED lnx 2026-10-07, linux-path.md §1]. Clearing a sticky taint bit is never attempted.
On Windows the equivalent check is new WHEA events.

## 7. Web server hardening (summary)
The server listens on `127.0.0.1` on a random port. Access starts with a **one-time token** in a private launcher file; the token becomes a session cookie that
expires after 30 minutes idle or 12 hours. Requests need an exact `Host` and a same-origin check (`Sec-Fetch-Site`);
only allowlisted operations with schema-checked arguments are forwarded. Applying a plan needs the plan-bound single-use nonce that expires after 10 minutes.
The page uses a strict content-security policy and no CDN; the GUI is plain HTML/CSS/JS shipped in the repository (D-012).

Residual risks the project accepted (D-028): cookies are not port-isolated on `127.0.0.1`, so another local service on that host name could receive the session
cookie (mitigated by expiry, exact Host/Origin checks and polkit at helper start, not eliminated); a local process could hold all connection slots (a local
denial of service only); rate limiting does not slow guessing but the secrets are 256-bit. Do not run the GUI on a machine with untrusted local users.

## 8. Software and downloads
The tool installs nothing by itself and downloads nothing. Its only third-party tools are the optional ones you install ([01 §5](01-requirements.md)).
The Windows restore needs a driver carved from **your own** `ThrottleStop.exe`; it is never committed to the repository, and the service is stopped after use.
Third-party tuning programs (OCCT, XTU, ThrottleStop, HWiNFO) are never launched by the tool.

## 9. What the tool will not do
- Write persistent settings: BIOS/NVRAM variables, firmware, anything that survives a cold boot. Everything it writes is volatile (T2 in `SAFETY.md`).
- Use static/override voltage mode (D-016) or touch the ring/NGU/system-agent/graphics V/F from the GUI.
- Apply anything at boot without you. Unattended autostart is an undecided, unsigned proposal (D-021) and is not implemented.
- Raise a policy limit on its own, or retry a failing write.

## 10. If something goes wrong
| Symptom | Do this |
|---|---|
| Freeze or reboot while tuning | cold boot (hold power until off, wait, start). The CPU is stock. Start from the last good state with a smaller change |
| Machine will not boot after tuning | all tool writes are volatile; power off, remove AC for a minute, boot. If your BIOS holds offsets, clear CMOS per your board manual |
| A device is missing after a hang (for example Wi-Fi) | full power-off with AC removed ([the 2026-08-26 incident](#3-one-transaction-at-a-time)); a driver reload is not enough |
| `TRIPPED` / health `changed` | stop; read the Audit tab and `dmesg`; cold boot before more writes |
| Unexpected machine-check lines | stop tuning; revert; test at stock |

More: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) and `docs/SAFETY.md`.

Previous: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) · Up: [guide index](README.md)
