# Hardware safety protocol

> Carried over 2026-10-08 from the project's earlier research phase. Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here name notes of that phase, which are not part of this repository.

Applies to every host. Failure modes to design against: system hang/crash, silent instability (data corruption),
persistent settings that survive reboot, kernel taint, and hardware stress from over-voltage or over-current.

## Who may do what
- **Reads** (allowlisted MSR/MMIO/mailbox GETs, sysfs, turbostat): any agent on `lnx`; workers read-only.
- **Writes** (wrmsr, OC-mailbox SET, MMIO/PCI-config writes, BIOS setup vars, XTU Apply, driver load/install):
  **human or orchestrator only**, and only with a go-ahead in chat for that specific write session.
- Workers never write, even if a tool would let them. If a task seems to need a write, stop and ask the orchestrator.

## Risk tiers (assign one to every task)
| Tier | Meaning | Rule |
|---|---|---|
| T0 | Pure read / offline analysis / docs | Free |
| T1 | Read of registers not yet verified for this CPU | Only via allowlist entry with source; log the first read |
| T2 | Volatile write (believed to reset on reboot/power cycle) | Full protocol below, orchestrator/human only |
| T3 | Persistent write (BIOS NVRAM/setup var, firmware, anything surviving reboot) | Prohibited unless a DECISIONS.md entry authorises it for a named variable |

**OC mailbox rule (T-005/T-006):** every OC-mailbox request — including a "read" (e.g. `cmd 0x10`) — is a **WRMSR to MSR 0x150** (there is no MMIO/PCI path). So *all* mailbox traffic is T2, human + orchestrator only, never a worker, never part of a baseline/T-003 read pass.
Required access sequence for our code (as the vendor drivers do, but stricter): pin to the target CPU → read MSR 0x150 and require bit 63 = 0 (poll with sleeps and a hard timeout; on timeout **abort without writing**) → WRMSR → poll until bit 63 clears (timeout ⇒ error) →
completion code = bits [39:32] (0 = OK), data = [31:0] → verify by an explicit read-back. Serialise all mailbox users; never run two requests concurrently on different CPUs; never retry in a loop.

Volatility is an assumption until measured: the first write of any kind is treated as T3-until-proven-T2
(prove by reboot after a small, conservative value).

## Write-session protocol (T2)
1. **Baseline:** read the target and neighbours; record values + timestamp in `captures/`. Confirm BIOS profile
   saved and CMOS-clear path known (human confirms).
2. **Allowlist:** the exact register/command, domain/core, and value range come from `docs/knowledge/registers.md`
   with a [MEASURED]/[DECOMPILED]/[DOC] tag. Nothing else is written.
3. **Bounds:** apply conservative limits first (small steps; ceilings and floors come from the reviewed policy file, not from the direction of the change — D-016; shipped defaults equal the current fused/baseline values, so any raise is a number the operator set).
   Increase in small steps only after readback + a stability check.
4. **Apply → readback:** write once, read back, compare; abort and revert on mismatch or unexpected RC.
5. **Revert path:** the inverse write (or reboot) is prepared before applying. Nothing is auto-applied at boot
   until the human enables it deliberately.
6. **Record:** result + evidence tag into the relevant research note; update STATE.md.

## Implementation requirements for write-capable code (future)
- A single write module; everything else imports it. It refuses unless the human sets an explicit env flag/CLI
  confirmation, checks the allowlist, enforces bounds, logs every write, and verifies by readback.
- Dry-run/simulator backend for tests, so workers can test control logic without hardware.
- Nothing in the app is allowed to loop-retry a failing write.

## Linux specifics (verify on the actual install before relying)
- Fedora 44: `CONFIG_X86_MSR=y` (built in): enable writes with `msr.allow_writes=on` (cmdline or `/sys/module/msr/parameters/allow_writes`).
  With **Secure Boot on, kernel lockdown blocks MSR writes** (reads still work) — check `cat /sys/kernel/security/lockdown` first.
- Every MSR **write** taints the kernel (`TAINT_CPU_OUT_OF_SPEC`), even with `allow_writes=on`; reads never taint and unimplemented
  MSRs return EIO. Expected for our writes — record it in the session log. If a tool tainted the kernel, look for its writes in `dmesg`.
- Still no blind sweeps (noise, MMIO/PCI side effects): only allowlisted reads.
- Source: `docs/research/lnx-interfaces.md` §5 (verify on the actual install before relying).

## Mailbox-session discipline (lesson of the 2026-08-26 incident, from the operator's earlier monitoring tooling)
_2026-10-11 (D-032): numeric limits (caps, dwell, steps, floor) are the operator's advanced-user policy; the one-at-a-time rule, health
bracket, audit and guard below still apply._
A broad OC-mailbox *Get* sweep (`WRMSR 0x150`) was followed ~5.5 min later by an abrupt boot end (no panic/MCE/AER/watchdog/thermal trip) and, on the next warm boot, a lost PCI device (Intel BE200) until **AC power was physically removed**.
Causality unproven; the mailbox burst was the only identified low-level mutation. Therefore:
- **One transaction at a time. Never a sweep, loop or "enumerate everything".** After each transaction: dwell, then check kernel/RAS/PCI health before the next.
- **Pre-flight (every session):** stop HWiNFO, OCCT (its Linux GUI writes MSR 0x150 at every launch), A-Tuning, any EC/OC-mailbox client; capture the exact raw before-state (raw words, not UI labels); define the single transaction, its expected completion and its rollback;
  confirm a **cold-power recovery plan** (AC removal; a driver reload is not enough); the human is present.
- **Health bracket:** `health_snapshot` before and after (taint integer + set bits, PCI device set, module set, watched kernel-log lines); acceptance = `CLEAN` delta. The taint baseline already carries `CPU_OUT_OF_SPEC` from earlier OCCT runs — compare against the recorded baseline word, never zero.
- Never clear sticky thermal/limit bits; one owner per shared controller.
- The first mailbox transaction ever sent from arltuner is a single core-pinned `cmd 0x10` read on one CPU (spec: T-007, after T-003), followed by a dwell and a health delta — not a batch.
- **Limits survive process boundaries (T-016):** the guard reads the audit log (`/var/lib/arltuner/audit.jsonl` when installed, else `$XDG_STATE_HOME/arltuner/audit.jsonl`, D-041) before every operation: a **cross-process dwell** (`min_dwell_s`, default 60 s since the last attempt by *any* process; a future-dated last attempt is refused) and a **per-boot cap** (`max_operations_per_boot`, default 12; reads count, read-backs don't). `apply` budgets the whole plan against the remaining per-boot operations **before sending anything**, so it cannot stop half-way on `BOOT_LIMIT`.
- **`AUDIT_UNAVAILABLE` means the log cannot be evaluated, so nothing is sent.** Recovery is a human act: inspect the file, remove only a malformed line (a crash-truncated *last* line is tolerated; one that later gets a record appended becomes a malformed middle line); **never delete or truncate the log to get past a refusal** — that would erase the history the limits rely on. Reboot = new per-boot count, but the dwell still applies.

## Windows-side rules (`win`)
- Observing XTU is T0/T1. Pressing Apply in XTU while capturing is a human action (T2) — the orchestrator only
  prepares the capture and lets the human perform the apply.
- Do not load extra kernel drivers (IRPMon, RWEverything, etc.) without stating name/source/hash and getting a yes;
  prefer user-mode/observation methods first.
- Agents never launch ThrottleStop, XTU, HWiNFO or OCCT. Those tools carry MSR/MMIO-capable drivers; launching is a human
  action (T2). ThrottleStop/XTU/HWiNFO driver features must not run concurrently (mailbox races).
- Third-party binaries live under `captures/thirdparty/` (git-ignored). Analyse statically (hash, PE parse, strings).

## Operation budget of one apply (T-021, 2026-10-01)
A per-core apply is **2 + 7n operations** (`0x14`; per CPU `0x10, 0x1C, 0x09, 0x26, 0x11, 0x1D, 0x27`; plus `0x15` when bit 3 is clear), each a WRMSR to MSR 0x150: **one P core = 9 operations (≈ 9 minutes at the 60 s dwell, ≈ 11 mailbox WRMSRs including guard read-backs)**; two P cores or any E cluster (4 CPUs) exceed the default per-boot cap of 12, so the operator must raise `max_operations_per_session` / `max_operations_per_boot` in a reviewed policy for that session. The guard's `0x1C`/`0x26` read-backs are extra and not in XTU's sequence.

T-022 numbers (the plan budget counts read-backs): **one P core = 13 mailbox WRMSRs; an E-cluster raise = 53**; the default per-boot cap of 12 covers not even one P core, so every real session needs a reviewed policy with higher `max_operations_per_session` / `max_operations_per_boot`. Raises go in small verified steps (ratio +1, V +20 mV per step by default), one human-attended session per step, with a stability check between steps.
