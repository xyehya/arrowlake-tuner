# Review of SPEC draft v1 (2026-10-08) — findings and disposition

Reviewer: independent Sonnet agent, read-only (files + harmless local checks). Spec v1 = `linuxmon@b01f78a:docs/SPEC-gui.md`.
Disposition: **A** = accepted into SPEC v2 · **P** = partly accepted (note) · **R** = rejected (reason) · **O** = open, operator decides.

| # | Sev | Finding (short) | Disp. | Where in SPEC v2 / note |
|---|---|---|---|---|
| 1 | blocker | CONFIRM can be echoed by any key holder; token must be bound to the exact plan | A | §4.3 plan-bound nonce |
| 2 | blocker | key in URL leaks (history, Referer, argv, /proc) | A | §4.2 bootstrap token → HttpOnly cookie, clean URL |
| 3 | blocker | local processes can reach 127.0.0.1; "root helper adds little since user is in msr group" | P | §4.2 hardening accepted. **Rejected part:** opening `/dev/cpu/0/msr` as the user fails `EPERM` despite `msr` group rw `[MEASURED lnx 2026-10-08]` (needs CAP_SYS_RAWIO) ⇒ the helper is the only write path |
| 4 | blocker | profile body chooses values; policy/audit/helper code user-writable ⇒ bypass; `~/.local` install = root escalation | A | §4.4 root-owned trust paths; decision **OD-1** |
| 5 | blocker | unattended autostart contradicts more standing rules than listed | A | §10 + D-021 draft lists every superseded rule |
| 6 | major | crash-loop guard: unclean-shutdown detection, volatile journal, partial apply ≠ stock | A | §10 marker file, revert plan, journal requirement |
| 7 | major | stress children must not run as root; enumerated params only | A | §9.1 |
| 8 | major | pipe protocol: framing, size cap, ids, heartbeat, cancellation | A | §4.5 |
| 9 | major | lockdown parse `[none]`; built-in msr; re-check at apply | A | §3 |
| 10 | major | `scaling_max_freq` is an upper bound under HWP; report achieved vs requested; pin governor/EPP | A | §9.2 |
| 11 | major | cap restore after crash needs a persisted journal; children die with helper | A | §9.2 |
| 12 | major | RAPL readability differs by distro; telemetry thread must not delay mailbox | A | §8 |
| 13 | major | perf uncore needs root; use one long-lived `perf stat -I`; event names unverified on other kernels | A | §8 |
| 14 | major | telemetry MSR reads (0x198, 0xE7/0xE8, 0x19C, 0x1B1) need allowlist entries | A | §8 |
| 15 | major | gates vs D-019: the app cannot run the acceptance gate; who attests | A | §5, §6.4 operator attestation record |
| 16 | major | GUI rewriting `EXPECTED-words.txt` bypasses autostart compare and drifts from Windows `$Expected` | A | §6.4 propose-only; root-owned accepted copy |
| 17 | major | one machine-wide lock shared with CLI; audit check not atomic | A | §4.6 (CLI already has `/run/lock/linuxmon-mailbox.lock`; helper uses the same) |
| 18 | major | two tabs / reload: single operation state machine | A | §4.6 |
| 19 | major | long applies: progress, abort between ops, browser disconnect | A | §4.6 |
| 20 | major | op-budget exhaustion handling; which policy the helper loads | A | §4.6, §7 |
| 21 | minor | polkit `auth_admin`, exec.path annotation; pkexec strips env (ALLOW flag) | A | §4.1; D-021 covers the flag |
| 22 | minor | helper EOF/SIGTERM/SIGHUP handling per state | A | §4.5 |
| 23 | minor | server compromise = helper vocabulary; test path traversal | A | §12 G1 security tests |
| 24 | minor | exact Host check incl. port; SSE over ThreadingHTTPServer | A | §4.2 |
| 25 | minor | Python 3.12 excludes Ubuntu 22.04 / Debian 12 | O | **OD-2** |
| 26 | minor | package names unverified outside Fedora; Ubuntu perf = linux-tools-*; nvidia-smi missing | A | §11 marked `[UNVERIFIED]` |
| 27 | minor | stressapptest: three classes incl. CRC-only; an undervolt copy-path error can mimic memory corruption | A | §9.3 "suggests", never "proves" |
| 28 | minor | stress-ng has no guaranteed SSE/AVX-only method; not all methods verify | A | §9.3 "vector-math-heavy" labels; NOT-a-gate banner |
| 29 | minor | never exec `~/Downloads` binaries as root; OCCT CLI unverified | P | Linpack Xtreme launched **as the user**, log shown, no auto pass/fail (§9.4); OCCT launcher dropped from v1 |
| 30 | minor | process-name checks weak; scan `/dev/cpu/*/msr` holders | A | §7 |
| 31 | minor | Super-I/O one-owner rule; leave board voltages out of v1 | P | **Kept**: Vcore via hwmon files (read-only, world-readable, already used on this board); labelled board-specific; no direct Super-I/O access |
| 32 | minor | platform match by fused data, not just CPUID; hypervisor ⇒ read-only | A | §3 |
| 33 | minor | target-V editing needs measured stock VID | A | §6.2 |
| 34 | minor | profile name validation, profile/config directories for system install | A | §6.4, §11 |
| 35 | minor | login autostart needs a polkit agent; root unit can't notify the session easily | A | §10 |
| 36 | minor | phase order: ownership/install in G1; G1 security test list; trim v1 | P | G1 owns install layout; trims per #29/#31 |
| 37 | minor | "forced unclean boot" test is itself a hardware event | A | §12 G6 simulated marker + documented human check |
