# Windows execution path: ThrottleStop.sys as a raw MSR port

Windows is the path for the PowerShell restore of the accepted state. XTU cannot write per-point offsets; ThrottleStop's own GUI has no per-core voltage
write and does not pin voltage writes to a core [DECOMPILED win-throttlestop-fivr.md s0], so only our code drives the mailbox through the driver.
Code: `tools/win/*`, `src/arltuner/msr/winport.py`. Procedure for users: `docs/guide/09-profiles-and-restore.md`.

## 1. The driver: carved from the operator's own exe, never shipped
| Item | Value |
|---|---|
| Source exe | ThrottleStop 9.7.3 `ThrottleStop.exe`, SHA-256 `52D86D3D0EC54D3B2F4FB7BD87DD44111EB770719AF03AED3071935A62312BA2` (zip `7B5E1A65...7098A`) [MEASURED win 2026-09-30] |
| Carved x64 driver | 50,216 B at exe offset 0x34ea20, SHA-256 `16F83F056177C4EC24C7E99D01CA9D9D6713BD0497EEEDB777A3FFEFA99C97F0`; Authenticode valid, TechPowerUp LLC (EV) |
| Carved x86 driver | 46,120 B at 0x3435f8, SHA-256 `7D39223E7EB902712AEAC77C90B9FA00A6E5E56A68D4C542BCC88556A57BA735` (unused) |
| Rule | the .sys is NEVER in the repo. `carve` refuses on exe-hash or driver-hash mismatch (use the original zip, don't force). Output: `%ProgramData%\arltuner\ThrottleStop.sys`, an admin-only directory (D-042) |
| Load | each run writes a new uniquely named copy, re-verifies directory ACL, reparse points and SHA-256, `sc create ThrottleStop type= kernel start= demand` + `sc start`; `\\.\ThrottleStop` must open. After the run the service and the copy are deleted (D-042) |
| Never run beside it | the ThrottleStop GUI, XTU, HWiNFO driver features or an elevated OCCT (all are mailbox clients) |
Config/security: Memory integrity (HVCI/VBS) must be OFF. With the Microsoft Vulnerable Driver Blocklist ON the driver loaded on Win11 26200/26300
[MEASURED win 2026-10-02], and again on a fresh Win11 26300 install with the blocklist ON (value 1) after DG_Readiness_Tool v3.6 `-Disable`
(Bitdefender's own Memory-integrity toggle did not take) [MEASURED win 2026-10-20]. First real `restore_tuning.ps1 -Orchestrated` run on that install:
identity 12/12, RESTORED (11 OK + cpu0 EQUIVALENT OC word 0x39), 317 mailbox ops, WHEA 0 [MEASURED win 2026-10-20].
**Hypervisor (WSL2) blocks the mailbox:** with only Virtual Machine Platform enabled (HVCI and Credential Guard off), Windows starts the hypervisor and the VBS base layer
(Win32_DeviceGuard VBS status 2, no services running); the driver still loads, but the first identity read (cmd 0x09 via MSR 0x150) returns fused word 0x0, so
the guard refuses before any write. Hyper-V logs NO event 12550 for it: the access is filtered silently [MEASURED win 2026-10-20]. ⇒ no runtime tuning while
the hypervisor runs. ThrottleStop.sys was abused by malware in 2025 and may be blocklisted later: errors 1275 / 577 / "blocked" -> route closed
(alternatives PawnIO, Intel IPF stack: unexplored). The device ACL is SYSTEM + Administrators only (SDDL `D:P(A;;GA;;;SY)(A;;GA;;;BA)`).

## 2. IOCTL interface [DECOMPILED win-throttlestop-fivr.md s1, driver ioctl.c]
| IOCTL | In / out | Notes |
|---|---|---|
| 0x80006448 RDMSR | u32 msr -> u64 | bare rdmsr on the CURRENT CPU; no filter. Reply must be 8 bytes or the port fails closed |
| 0x8000644C WRMSR | {u32 msr, u32 lo, u32 hi}, 12 B, no output | bare wrmsr; refuses only STAR/LSTAR/CSTAR (0xC0000081-83) and SYSENTER (0x174-176) |
| 0x80006430/34, 0x80006498/9C, 0x8000645C/60, 0x80006494, 0x800064A0/A4 | port I/O, phys read/write, map/unmap, open count, PCI cfg | exist; our port uses only RDMSR/WRMSR |
No CPU argument; no `__try` visible around rdmsr/wrmsr (an unimplemented MSR could fault the kernel) [UNVERIFIED]. Wire word for MSR 0x150 and the
busy/completion handling: `registers.md`. ThrottleStop itself writes the mailbox fire-and-forget; our port polls, reads back and never retries.

## 3. Safety checks built into the Windows port
- Affinity: the driver runs on whatever CPU the calling thread is on. Every access pins the thread to ONE logical processor (SetThreadGroupAffinity),
  verifies with GetCurrentProcessorNumberEx before AND after the IOCTL, then restores affinity (also after failure). Failure -> error, nothing sent.
- Topology and identity (G-138): Windows LP numbers differ from Linux CPU numbers (`platform-arl-s.md` §2). The restore derives the LP map live
  (CPU-set EfficiencyClass and L2 sharing prove P vs E and the E clusters) and checks it against `fingerprint.json` recorded at accept on Linux; the
  eight P-cores look identical to Windows, so their order is proven by the per-core fused word (cmd `0x09 P1=0x11`, distinct per core,
  `platform-arl-s.md` §4). Mismatch -> refused, nothing written.
- Health gate: new WHEA-Logger events in the System log during a session = fail (the Windows stand-in for taint/MCE).
- XtuService must be Stopped and set Manual (XTU's UI starts it; XTU may re-apply its own saved tuning at service start and is a second mailbox client).
  No OCCT / XtuUiLauncher / ThrottleStop GUI running.
- Limits are the policy's (D-032, D-039); the audit log lives in `%ProgramData%\arltuner\audit.jsonl` (D-042).
- While the driver is loaded any admin process has MSR and physical-memory access, hence it is removed after every run. Removing it undoes nothing;
  the revert is a cold boot.

## 4. Restore flow (after any boot)
- Scripts: `tools/win/restore_tuning.ps1` (`-Orchestrated`, `-Before` = extra pre-apply vf-dump, off by default) and `tools/win/Restore-Tuning.cmd`
  (self-elevating one-click launcher).
- Steps: checks (admin, XtuService stopped, no other mailbox client) -> one "Type CONFIRM" (case-sensitive; anything else = nothing sent; D-020) ->
  load the driver -> apply the one profile the accepted CURRENT names (D-044; a CURRENT that lists several is refused) -> vf-dump -> compare every word with the accepted EXPECTED words
  (EQUIVALENT for a residual OC point, G-147) -> remove the driver. Stops at the first failure, never retries.
- Works from a cold boot (all stock) and a warm reboot (part kept, `platform-arl-s.md` §7); the planner steps from what it reads.
- Preflight (`tools/win/preflight.ps1`, read-only): admin, Memory integrity, blocklist, XtuService, other mailbox clients, Python, driver state.

## 5. Troubleshooting
| Symptom | Meaning / action |
|---|---|
| install/`sc start` fails 1275, 577, "blocked" | Memory integrity or the blocklist rejects the driver. Memory integrity OFF + reboot; if still blocked the route is closed - report |
| `carve` refuses (SHA-256 mismatch) | not the analysed 9.7.3 exe; use the original zip; don't force |
| CreateFileW `\\.\ThrottleStop` access denied | shell not elevated |
| "cannot pin the thread" / "runs on LP N, not LP M" | pinning failed, nothing sent; retry once, then report |
| LP map / fingerprint mismatch | Windows topology differs from what was recorded at accept; report, don't override |
| "identity check failed on cpu N" | the fused word read on that LP is not P-core N's; nothing written; report all output |
| `DeviceIoControl ... returned 0 bytes, expected 8` | RDMSR reply size differs from the decompile; port fails closed |
| guard refusal | a policy limit; never raise limits without the operator's yes |
| WHEA gate fails | hardware error logged; stop, note the event, reboot to stock, report |
| "STOP: not elevated" / "XtuService is running" / "close first: ..." | restore preconditions |
| Crash or WHEA under load | check the BIOS settings the accepted state was validated on first; revert = reboot |
