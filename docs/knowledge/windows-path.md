Distilled 2026-10-08 from the project's earlier research phase; the raw notes and captures are not part of this repository.
# Windows execution path: ThrottleStop.sys as a raw MSR port

Sources: earlier Windows runbooks and first-apply notes (not part of this repository); `tools/win/*`, `src/arltuner/msr/winport.py`.
Windows is the path for the PowerShell restore (and recon). XTU cannot do per-point offsets; ThrottleStop's own GUI has no per-core voltage write and does not pin voltage writes to a core [DECOMPILED win-throttlestop-fivr.md s0], so only our code drives the mailbox through the driver.

## 1. The driver: carved from the operator's own exe, never shipped
| Item | Value |
|---|---|
| Source exe | ThrottleStop 9.7.3 `ThrottleStop.exe`, SHA-256 `52D86D3D0EC54D3B2F4FB7BD87DD44111EB770719AF03AED3071935A62312BA2` (zip `7B5E1A65...7098A`) [MEASURED win 2026-09-30] |
| Carved x64 driver | 50,216 B at exe offset 0x34ea20, SHA-256 `16F83F056177C4EC24C7E99D01CA9D9D6713BD0497EEEDB777A3FFEFA99C97F0`; Authenticode valid, TechPowerUp LLC (EV) |
| Carved x86 driver | 46,120 B at 0x3435f8, SHA-256 `7D39223E7EB902712AEAC77C90B9FA00A6E5E56A68D4C542BCC88556A57BA735` (unused) |
| Rule | the .sys is NEVER in the repo. `carve` refuses on exe-hash or driver-hash mismatch (use the original zip, don't force). Output: `%LOCALAPPDATA%\arltuner\ThrottleStop.sys` (D-041) |
| Install | `sc create ThrottleStop type= kernel start= demand` + `sc start` (idempotent); `\\.\ThrottleStop` must open. Service is demand-start: `sc start ThrottleStop` after every boot |
| Never run | the ThrottleStop GUI, XTU, HWiNFO driver features or OCCT elevated beside it (all race on the mailbox) |
Config/security: Memory integrity (HVCI/VBS) must be OFF. Microsoft Vulnerable Driver Blocklist ON: driver loaded fine on Win11 26200/26300 [MEASURED win 2026-10-02] (earlier first-apply notes). An earlier runbook note said the blocklist must be OFF (written before the test) and a handoff note said to try with it ON first; the measurement wins. ThrottleStop.sys was abused by malware in 2025 and may get blocklisted later: errors 1275 / 577 / "blocked" -> route closed (alternatives PawnIO, Intel IPF stack unexplored).
Session was not elevated at first recon (`docs/HARDWARE.md`); the device ACL is SYSTEM + Administrators only (SDDL `D:P(A;;GA;;;SY)(A;;GA;;;BA)`).

## 2. IOCTL interface [DECOMPILED win-throttlestop-fivr.md s1, driver ioctl.c]
| IOCTL | In / out | Notes |
|---|---|---|
| 0x80006448 RDMSR | u32 msr -> u64 | bare rdmsr on the CURRENT CPU; no filter. Reply must be 8 bytes or the port fails closed |
| 0x8000644C WRMSR | {u32 msr, u32 lo, u32 hi}, 12 B, no output | bare wrmsr; refuses only STAR/LSTAR/CSTAR (0xC0000081-83) and SYSENTER (0x174-176) |
| 0x80006430/34, 0x80006498/9C, 0x8000645C/60, 0x80006494, 0x800064A0/A4 | port I/O, phys read/write, map/unmap, open count, PCI cfg | exist; our port uses only RDMSR/WRMSR |
No CPU argument; no `__try` visible around rdmsr/wrmsr (an unimplemented MSR could fault the kernel) [UNVERIFIED]. Wire word for MSR 0x150: `1<<63 | P2<<48 | P1<<40 | cmd<<32 | data`; busy bit 63 polled before and after; completion [39:32] (see registers.md). Mailbox traffic on Windows is fire-and-forget in ThrottleStop itself; ours polls, reads back and never retries.

## 3. Safety checks built into the Windows port
- Affinity: the driver runs on whatever CPU the calling thread is on. Every access pins the thread to ONE logical processor (SetThreadGroupAffinity), verifies with GetCurrentProcessorNumberEx before AND after the IOCTL, then restores affinity (also after failure). Failure -> error, nothing sent ("cannot pin the thread" / "thread runs on LP N, not LP M").
- Topology check at port construction: Windows CPU-set EfficiencyClass and L2 sharing per LP must match the measured map (P = LP 0,1,10,11,12,13,22,23; E clusters LP 2-5, 6-9, 14-17, 18-21). Proves P vs E and the E clusters, NOT the order of the eight P-cores (they look identical to Windows) -> hence the identity check.
- Identity check: before any apply / point-offset / vf-dump read the fused word (cmd 0x09 P1=0x11, P2=0) of P-cores 0-7 and require equality with 0x5B439 0x53839 0x57439 0x59439 0x54439 0x56C39 0x56039 0x58039 (all distinct; `platform-285k.md` s4). Mismatch -> refused, nothing written ("identity check failed on cpu N"). Passed in every session [MEASURED win 2026-10-02].
- Health gate: new WHEA-Logger events in the System log during a session = fail (Windows stand-in for taint/MCE). Baseline WHEA count was 38 (newest 2026-09-30) and unchanged through all 2026-10-0x sessions [MEASURED win].
- XtuService must be Stopped (and set Manual: `Set-Service XtuService -StartupType Manual`); XTU's UI starts it. XTU may re-apply its own saved tuning at service start and is a second mailbox client. No OCCT / XtuUiLauncher / PerfTune / ThrottleStop GUI running.
- Guard policy limits (reviewed TOML; the GUI/tool never widens them): <= 55 mV offset change per write, floor -100 mV, 260 ops/session, 600 ops/boot, dwell. Cumulative cross-process state lives in the audit log `%LOCALAPPDATA%\arltuner\audit.jsonl`.
- Driver is STOPPED after apply (`sc stop ThrottleStop`): applied offsets stay until reboot, but while loaded any admin process gets MSR and physical-memory access. Stopping does not undo anything; revert = cold boot (or warm reboot for CPU 0 only).

## 4. Restore flow (after ANY boot)
- Reference scripts `tools/win/restore_tuning.ps1` (`-Orchestrated` = orchestrator answers prompts after a go-ahead; `-Before` = extra pre-apply vf-dump, ~132 ops, default off) and `tools/win/Restore-Tuning.cmd` (one-click: self-elevates via UAC, runs the .ps1 interactively, `pause`; Desktop shortcut "Restore CPU tuning").
- Steps: checks (admin, XtuService stopped, no OCCT/XTU/ThrottleStop processes) -> single "Type CONFIRM" (case-sensitive, anything else = nothing sent; D-020) -> `sc start ThrottleStop` -> apply each profile in the CURRENT list (E split3, p-boot-ramp, p-step7) -> vf-dump -> compare every word with the inline expected-words table (= `tuning-results.md` s1) -> `sc stop ThrottleStop`. Stops at first failure, never retries.
- Success line: `RESTORED: all 12 cores/clusters match the accepted state.` Captures: `captures\win\restore-<ts>-*.txt`.
- Works from cold boot (all stock) and warm reboot (CPUs 1-7 + E kept, CPU 0 reset by the BIOS); the planner steps from what it reads. A cold boot is the only full reset.
- p-boot-ramp exists because from stock the 55 mV/write limit refuses deep targets (e.g. -97); the ramp clamps offsets to >= -50, then p-step7 moves <= 41 mV [MEASURED win 2026-10-04].
- Budget lesson: an interrupted run + rerun exhausted the 600 ops/boot on 2026-10-05 (final compare refused although all 57 writes read back equal) -> `-Before` became opt-in so two restores fit.
- First live restore of p-step7 on Windows: cold boot 2026-10-05, RESTORED. A P0 58x test (2026-10-06) leaves CPU 0 flagged BAD (OC word != 0x39) until reboot or until accepted into EXPECTED.
- Preflight (`tools/win/preflight.ps1`, read-only): admin, Memory integrity, blocklist, XtuService, other mailbox clients, Python, driver state; read-only.

## 5. Troubleshooting [HANDOFF-windows.md s5]
| Symptom | Meaning / action |
|---|---|
| install/`sc start` fails 1275, 577, "blocked" | Memory integrity or blocklist rejects the driver. Memory integrity OFF + reboot; if still blocked the route is closed - report |
| `carve` refuses (SHA-256 mismatch) | not the analysed 9.7.3 exe; use original zip; don't force |
| CreateFileW `\\.\ThrottleStop` access denied | shell not elevated |
| "cannot pin the thread" / "runs on LP N, not LP M" | pinning failed, nothing sent; retry once, then report |
| "LP map ... does not match" (before driver opens) | Windows topology differs from measured map; report, don't override |
| "identity check failed on cpu N" | fused word read on that LP is not P-core N's; nothing written; report all output |
| `DeviceIoControl ... returned 0 bytes, expected 8` | RDMSR reply size differs from the decompile; port fails closed; check before changing the ioctl wrapper |
| guard refusal dwell / cap / step / NEXT ALLOWED STEP | policy limit; usually a profile applied out of order (use restore script); never raise limits without the operator's yes |
| WHEA gate fails | hardware error logged; stop, note the event, reboot to stock, report |
| "STOP: not elevated" / "XtuService is running" / "close first: ..." | restore_tuning.ps1 preconditions |
| CONFIRM prompt not visible | transcript missed native output; the token is printed just before the prompt; check the audit log |
| Crash or WHEA under load | check BIOS settings first (LLC 6, VccIA -100); revert = reboot |

## 6. Windows-specific measurements
- Windows at LLC 7 matched Linux (p-step4 state): OCCT SSE steady 295 W, Vcore 1.34 V (Linux 1.344) [MEASURED win 2026-10-03]. Geekbench 7.0.0 at the p-step3-era state: 3132 single / 29779 multi (https://browser.geekbench.com/v7/cpu/529116), RAM DDR5-7200.
- HWiNFO sensors-only is tolerated during apply (closed for the first apply); OCCT elevated writes the mailbox (never during tuning).
- Windows logical-processor numbering vs Linux: see `platform-285k.md` s2; the code speaks Linux numbering and maps internally.
