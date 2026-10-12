# 12 Safety and internals

This page explains how the tool protects the machine and your data: the write guard, the policy file, the audit log, the health check, the root helper
and the local web server. It is for anyone who wants to know what happens between pressing **Apply** and a word reaching the CPU, or who wants to change
the policy. Sources: `docs/SAFETY.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `src/arltuner/guard/`, `policies/default.toml`.

## 1. Architecture in one picture
```
browser (127.x.y.z, random) --HTTP/SSE--> arltuner-gui.py (your user: web server)
                                       |  JSON lines over a Unix socket (/run/arltuner/helper.sock)
                                       v
                     root helper: arltuner-helper.service (socket-activated; allowlist check per connection)
                                       |
                              plan -> guard -> mailbox transport -> /dev/cpu/N/msr
                                       |            |
                                  audit log     health gate (before/after)
```
- The **web server** runs as you. It serves the page, checks the session, and forwards only an allowlisted set of operations with checked arguments
  to the helper. It never touches hardware. It connects to the helper's socket (after checking that the node is a socket owned by root and, with `SO_PEERCRED` on its own end, that the
  listener really is root, so a look-alike gets no request). It marks itself non-dumpable (`PR_SET_DUMPABLE 0`: other processes of your user cannot ptrace it or read its memory/environment) (G-142). With the
  development option `--helper CMD` the channel is a `socketpair` to a child process instead.
- The **root helper** runs as root as a **systemd socket-activated service** (D-043): `arltuner-helper.socket` listens on `/run/arltuner/helper.sock` (mode 0666) and starts
  `arltuner-helper.service` (`/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket`) on the first connection. Under another init system you run that command yourself. There is **no polkit and no
  password**. At every connection the helper reads the connecting process's user id with `SO_PEERCRED` and admits it only if the name is in `[access] users` (or the user is in `[access] group`) of
  `/etc/arltuner/helper.toml`; a missing, empty, unreadable or invalid list admits nobody; root only if listed by name. One client at a time (a second gets `CLIENT_BUSY`); when it leaves, telemetry stops and an operation in flight finishes
  its read-back or restore; the service exits after `idle_exit_s` (default 600 s) with no client and no operation. As root it accepts no command-line arguments but `--device --socket`; its paths are constants (`/etc/arltuner/policy.toml`, `/etc/arltuner/helper.toml`, `/var/lib/arltuner/audit.jsonl`,
  `/etc/arltuner/accepted`); every file it opens is checked (regular, one link, owned by root, no group/other write, symlinks refused). It refuses to run from a
  checkout (SPEC §4.4, D-029).
- The **guard** is the only code allowed to send a mailbox request.

## 2. The write guard
Every mailbox request, a read included, passes these checks in order, and the first failure refuses it with **nothing sent** (`guard.py`):

| Check | Refusal | Meaning |
|---|---|---|
| guard tripped earlier | `TRIPPED` | after any transport error, read-back mismatch, failing health gate or unreleasable lock, everything is refused until a new session |
| environment flag | `NO_ENV_FLAG` | `ARLTUNER_ALLOW_MAILBOX=1` must be set: exported by you on the CLI path; the helper supplies it itself for an admitted connection (the allowlist replaced the old password, SPEC §4.1, D-043) |
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
The mailbox discipline comes from an incident on the project author's machine with earlier monitoring tooling, recorded in `docs/SAFETY.md` ("2026-08-26 incident"): a broad sweep of mailbox
*get* requests was followed about 5.5 minutes later by an abrupt end of the boot, and on the next warm boot a PCI device (a Wi-Fi card) was missing until the AC power
was physically removed. Causality is unproven; the mailbox burst was the only identified low-level change [observed once by the project author, causality unproven].
The rule that follows is kept regardless of any limit setting:

- **One transaction at a time**, serialised by a lock file (`/run/arltuner/mailbox.lock`), never a sweep, loop or "enumerate everything".
- Every request is a pinned access to one CPU: poll MSR `0x150` until the busy bit (63) is clear (hard timeout, **abort without writing** on timeout), write, poll
  until the busy bit clears, read the status (bits 39:32; 0 = OK) and data (bits 31:0), then verify by an explicit read-back. No retry.
- Another tool using the mailbox at the same time is a second client: close HWiNFO, A-Tuning, a root OCCT.
- After a hang or unexplained device loss, power the machine fully off and remove AC power for a while before you trust it again; a driver reload is not enough
  (the incident's recovery).

The Tune page's read is about 124-136 requests, and an apply is tens of operations per unit (13 for one P-core and 53 for an E-cluster raise in the T-022
planning budget [SAFETY.md "Operation budget"]). They are sent inside a budget-checked plan, paced by the guard and bracketed by health checks, not as free-running sweeps (D-018).

## 4. Limits: the policy file is yours
**Every limit is your policy, not the program's** (D-039). `/etc/arltuner/policy.toml` is root-owned, seeded once from `policies/default.toml`, and never overwritten by the
installer ([02](02-install.md)). The numbers in it are **conservative defaults you can change**; the program hard-codes none of them. Because some overclockers run
liquid nitrogen or other extreme cooling on very good silicon, you may raise any default, **up to what your CPU itself reports or the register field can hold**. Nothing else
stops you, and nothing stops you from destroying the chip: that is why the defaults are conservative and why you are asked to change one number at a time.

The limits sit in two layers:

| Layer | Who sets it | What it is |
|---|---|---|
| **Your policy** (`/etc/arltuner/policy.toml`) | you | the numbers below. Edit as root; a change applies from the next read or plan (no restart). `sudo bash install.sh --reseed-config` restores the defaults |
| **Hard ceilings** | your CPU and the register encodings, not editable | the CPU's own limits, read live in the session; the field widths |

| Limit | Default in `policy.toml` | Hard ceiling and where it comes from |
|---|---|---|
| Voltage offset (`offset_ceiling_mv`, `effective_offset_floor_mv`) | +200 ... -200 mV | the 11-bit signed field: -1000 ... +999 mV (XTU's range) |
| Adaptive target voltage (`[v_target_max_mv]` P, E) | `"auto"` = the CPU's own maximum from mailbox command `0x28` minus one step; or a number in mV | the same `0x28` value, read live; the 12-bit field (0.5 ... 3.999 V). `0x28` not read = no raise |
| OC ratio (`[ratio_max]` P, E) | `"auto"` = the fused ratio the CPU reports in command `0x1C` bits 15:8 (highest over the cores of that kind read this session) **+ 3** bins (P 57 to 60, E 46 to 49 on the reference chip); or a number | the CPU's OC-ratio limit from mailbox command `0x01` bits 7:0 for that core kind, read live; the field holds 1 ... 255. Not read = no raise. (The reference chip's `0x01` reports 120x for P, so in practice your policy is the limit [MEASURED win 2026-10-01, registers.md]) |
| Per-write steps (`max_ratio_step`, `max_v_step_mv`, `max_offset_step_mv`) | 8, 200 mV, 200 mV | ratio 1 ... 255; voltage and offset 2 ... 1000 mV |
| Assumed BIOS offset (`assumed_bios_offset_mv`) | 0 for P and E | none (it only budgets the effective offset) |
| Turbo-table buckets | within the ratio ceiling above and the per-write step | 1 ... 255 |
| Operation counters (`max_operations_per_session`, `..._per_boot`) and `min_dwell_s` | 100000 each, dwell 0 s | none: indicators |

Every refusal names where its limit comes from, for example `policy ratio_max P = 60 (/etc/arltuner/policy.toml; your CPU allows up to 120x per cmd 0x01)`,
`CPU limit 84x (cmd 0x01 ...)` or `field encoding`. **System → Tuning limits** shows your policy next to what your CPU allows, for each limit, after a read; the Tune page tooltips say the
same ("from your policy, editable"). The Tune dropdowns offer only values inside the limits in force.

What is **not** a limit and stays on whatever you edit: the allowlist (default deny), adaptive mode only (no static/override voltage, D-016), one mailbox transaction at a time,
the health check, the audit log, the plan nonce and the read-back of every write.

### The other keys
| Key | Meaning | Default |
|---|---|---|
| `cpus`, `e_cpus` | CPUs the guard may address | `"auto"` (from the live topology) |
| `[[allow]]` | allowed mailbox request: `name`, `cmd`, `p1`, `p2`, `kind` (`read`/`write`), optional `check` | the commands in `policies/default.toml` |
| `[[msr_allow]]` | allowed plain MSR access: `msr`, `kind`, optional `cpus`, `check` | reads of `0xCE 0x650 0x1AD 0x1AE 0x651 0x198`, writes of `0x650` and `0x1AD` on CPU 0 |

A policy file **replaces the whole policy**: it must list every allowed request, including the read of command `0x01` (without an `oc_capability_read` entry a ratio raise cannot be planned).
The shipped allowlist allows writes only for: the per-core switch (`0x15`, only setting the per-core-voltage bit), the V/F OC word `0x11` (`P2` = 0) and per-point words (`P2` 1..8), the
favored-ratio word `0x1D`, the granular ratio word `0x27`, a ring V/F point (`P1` = 2, command line only) and the two turbo tables. Everything else is read-only or not listed.

Editing: change a number, keep the file valid TOML. A broken file makes the operation refuse with `POLICY_INVALID`. To go back: `sudo bash install.sh --reseed-config`.
Raising a limit is your decision; the project's own advice is to move one number at a time and to test each step ([08](08-stability-testing.md)).

## 5. The audit log
`/var/lib/arltuner/audit.jsonl` (root, 0600) is an append-only JSON-lines file. Each line has `ts` (UTC), `seq`, `event` and, when applicable, `cpu`, `cmd`/`p1`/`p2`
(hex), `req_data`, `purpose`, `operator`, `rc`, `rc_name`, `data`, `reason`, `detail`, `baseline`. Events: `refused`, `attempt`, `msr_read`, `result`, `readback`,
`error`, `trip`. Attempts carry the `boot_id`; the per-boot count is the number of `attempt` records for the current boot. The confirmation token is never logged.
The GUI operator is recorded as `gui:uid<N>`. The **Audit** tab shows the tail (last 256 KiB).

- A read-back is logged as `readback`, never `attempt`, so it does not count against the counters.
- If the log cannot be read, the guard refuses everything (`AUDIT_UNAVAILABLE`). Recovery is a human act: inspect the file, remove only a malformed line (a
  crash-truncated *last* line is tolerated), and **never delete or truncate the log to get past a refusal**. That would erase the history the limits rely on
  [SAFETY.md]. `uninstall.sh --purge` needs `--yes-reset-budget` for the same reason.
- Before an install, the command line writes its own log at `$XDG_STATE_HOME/arltuner/audit.jsonl` (default `~/.local/state/arltuner/audit.jsonl`; Windows `%LOCALAPPDATA%\arltuner\`, and
  `%ProgramData%\arltuner\audit.jsonl` for the Windows restore scripts). `install.sh` seeds `/var/lib/arltuner/audit.jsonl` once from it. The **Audit** tab names each request in words and has a filter
  (**All** / **Writes & refusals**).

## 6. Health check
Around each mailbox transaction the guard compares two snapshots (`health/gate.py`): the kernel taint word and its set bits, the PCI device set, the loaded
module set, and new kernel-log lines (the expected `msr` notices are hidden). The verdict is **CLEAN or not**; a changed PCI or module set, or a new taint bit
outside the allowed ones, fails the gate and trips the guard. It compares against the snapshot taken before *this* transaction, never against zero, because the
boot's taint word may already carry bits from earlier tools. Taint bit 2 (`CPU_OUT_OF_SPEC`) is expected on the first MSR write of a boot and is allowed through
`allow_taint_bits = [2]` in `/etc/arltuner/helper.toml` [MEASURED lnx 2026-10-07, linux-path.md §1]. Clearing a sticky taint bit is never attempted.
On Windows the equivalent check is new WHEA events.

**Known limitation (security review, D-042):** the gate sees only what happens inside its window, a before and an after snapshot around one transaction. An instability that shows
later, under load, is not seen by it. It is caught by stress tests and bounded by "revert = cold boot"; a clean health result is evidence about that window, not proof that a setting is
stable (`docs/SAFETY.md`). The audit log has no size cap and is never truncated by the tools.

## 7. Who can drive the helper: the access model (D-043)
The project removed polkit on purpose (fewer dependencies, the same experience on every distribution, no password prompt on each start). That **changes the security model**, and you should
know exactly how:

| Before (D-029/D-042) | Now (D-043) |
|---|---|
| Starting the helper needed the administrator password once per GUI start (polkit). A process of yours without the password could not get root's MSR access | **Any process of an allowed user can connect to the helper and drive it without a prompt.** The socket is connectable by everyone (0666), but the helper refuses every user id that is not in the allowlist |

What the allowlist means in practice: if you put a user in `/etc/arltuner/helper.toml`, anything running as that user (a browser exploit, a script, a malicious package you ran) can ask the helper
to read or write the CPU, within the limits below, until the user is removed from the list. Do not allow accounts you do not control, and do not leave untrusted software running as an allowed user.
To remove access, delete the name from `[access] users` (a root edit; effective at the next connection). Root cannot be added by the installer.

**What still protects you** (none of it depends on a password):

- **The allowlist itself**: a kernel-supplied user id (`SO_PEERCRED`, not forgeable by an unprivileged process), checked at every connection, fail closed; the file is root-owned and trust-checked.
- **The guard and your policy limits** on every request: allowlist of commands, offset/ratio/voltage limits (hard ceilings: your CPU's own limits), one mailbox transaction at a time, read-back of every write, the
  health check, and the audit log that records each request with the user (`gui:uid<N>`) ([§2](#2-the-write-guard), [§4](#4-limits-the-policy-file-is-yours)).
- **One client at a time** and **idle exit**: a second client is refused; with no client the helper exits after about 10 minutes.
- **Nothing is applied on its own**: the helper acts only on requests; no tuning is applied at boot (autostart is an unsigned proposal, D-021). Everything it writes is volatile: a cold boot reverts it.
- **The GUI side**: the web server checks that the listener is root; its sessions have a fixed scope, and a **read** session can only read ([§8](#8-web-server-hardening-summary)). These web checks protect the browser path;
  a process that speaks the helper protocol directly bypasses the web layer and is bounded by the helper's own checks above.
- The helper's own hardening: root-owned code tree and trusted file opens, no argument but `--device --socket` as root, unit options `NoNewPrivileges`, `RestrictSUIDSGID`, `LockPersonality`.

The trade is deliberate: a prompt that appears on every start was judged more friction than protection on a single-user workstation. On a **multi-user machine**, allow only yourself, or do not install.

## 8. Web server hardening (summary)
- **Random loopback address (G-142, D-042).** The server binds `127.x.y.z` with a random port, a fresh address each run (it falls back to `127.0.0.1`, with a warning on the terminal, only
  if that bind fails). Browsers keep cookies per host name, not per port, so every run has its own cookie jar: another local service cannot receive your session cookie. This replaces
  the earlier accepted risk of D-028 #1, and it matters because the plan nonce is returned to the browser (D-033): whoever holds the cookie could otherwise fetch it.
- **One-time token and session.** Access starts with a **one-time token** in a launcher file only you can read (directory 0700, file 0600); it becomes a session cookie that expires after 30 minutes idle or 12 hours
  in total. Requests need an exact `Host` and a same-origin check (`Sec-Fetch-Site`); a duplicate session cookie is refused (401).
- **Session scope.** The scope is fixed when the session starts: `full` (the launcher) or `read`. A **read** session may call only an explicit list of read-only operations (preflight,
  audit tail, curves list/get, profiles list/get, `tune.last`, memory read, stress list, telemetry), and its event stream carries only heartbeat, state and telemetry. Nothing that
  measures, stresses, saves, accepts, deletes or cancels is reachable. `tools/screenshots.py` uses a read session. An unknown or garbled scope is refused.
- **Allowlisted operations only**, with schema-checked arguments; NaN and Infinity are refused; at most 500 saved profiles. Applying a plan needs the plan-bound single-use nonce that expires after 10 minutes.
- The page uses a strict content-security policy and no CDN; the GUI is plain HTML/CSS/JS shipped in the repository (D-012).

Residual risks the project still accepts (D-028 minus the cookie item): a local process could hold all connection slots (a local denial of service only); rate limiting does not slow
guessing, but the secrets are 256-bit. Do not run the tool on a machine with untrusted local users or allow accounts you do not control ([§7](#7-who-can-drive-the-helper-the-access-model-d-043)). The restore scripts run Python under `-I` from the checkout; moving restore onto the installed,
root-owned tree is a planned follow-up (ROADMAP).

## 9. Software and downloads
The tool installs nothing by itself and downloads nothing. Its only third-party tools are the optional ones you install ([01 §5](01-requirements.md)).
The Windows restore needs a driver carved from **your own** `ThrottleStop.exe`; it is never committed to the repository. It lives in the admin-only `%ProgramData%\arltuner`, each run loads a new
hash-checked copy, and the service and the copy are deleted after the run ([09 §4](09-profiles-and-restore.md)).
Third-party tuning programs (OCCT, XTU, ThrottleStop, HWiNFO) are never launched by the tool.

## 10. What the tool will not do
- Write persistent settings: BIOS/NVRAM variables, firmware, anything that survives a cold boot. Everything it writes is volatile (T2 in `SAFETY.md`).
- Use static/override voltage mode (D-016) or touch the ring/NGU/system-agent/graphics V/F from the GUI.
- Apply anything at boot without you. Unattended autostart is an undecided, unsigned proposal (D-021) and is not implemented.
- Raise a policy limit on its own, or retry a failing write.

## 11. If something goes wrong
| Symptom | Do this |
|---|---|
| Freeze or reboot while tuning | cold boot (hold power until off, wait, start). The CPU is stock. Start from the last good state with a smaller change |
| Machine will not boot after tuning | all tool writes are volatile; power off, remove AC for a minute, boot. If your BIOS holds offsets, clear CMOS per your board manual |
| A device is missing after a hang (for example Wi-Fi) | full power-off with AC removed ([the 2026-08-26 incident](#3-one-transaction-at-a-time)); a driver reload is not enough |
| `TRIPPED` / health `changed` | stop; read the Audit tab and `dmesg`; cold boot before more writes |
| Unexpected machine-check lines | stop tuning; revert; test at stock |

More: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) and `docs/SAFETY.md`.

Previous: [11 Troubleshooting and FAQ](11-troubleshooting-faq.md) · Up: [guide index](README.md)
