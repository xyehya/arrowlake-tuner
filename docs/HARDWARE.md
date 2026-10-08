# Hardware and host facts

> Carried over 2026-10-08 from `linuxmon@b01f78a` (`~/AI/linuxmon`). Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here live in that repo (see `docs/PROVENANCE.md`); "linuxmon" = this project's former name.

## Machine [MEASURED win 2026-09-30]
| Item | Value |
|---|---|
| CPU | Intel Core Ultra 9 285K (Arrow Lake-S), 24 cores / 24 threads (8P + 16E, no HT), CPUID `0xC0662` |
| Board | ASRock Z890 Steel Legend WiFi |
| BIOS | AMI 3.40 (dated 2026-09-15) |
| Disks | disk 0 Kingston 2 TB: ESP + 2 GB + 1.9 TB Linux-typed partitions; disk 1 Samsung 990 Pro 1 TB: Windows |
| BIOS limits (user-reported) | a **per-core V/F offset** for P-cores (operator 2026-10-01: −50 mV at the 57× point, effective ≈ −40 mV vs fused: VID 1.3864 V vs fused 1.4258 V on P-Core 0); no E-core V/F; no absolute voltages shown (earlier note said one shared all-core offset — corrected) |

## CPU numbering [MEASURED lnx 2026-09-29 via turbostat+ capture; XTU mapping inferred]
Linux CPUs **0–7 = P-cores, 8–23 = E-cores** (no SMT); E-clusters of 4 = **8–11, 12–15, 16–19, 20–23**; favored P-cores = **CPU 0 and 3**. XTU "P-Core N" = Linux CPU N; XTU "E-Core k" = Linux CPU 8+k. Details: `docs/research/lnx-turbostat-plus-facts.md`. **Windows logical-processor numbers (mailbox masks, XTU logs "Core N") are NOT P-first** [MEASURED win 2026-10-01]: P = LP {0,1,10,11,12,13,22,23}, E = LP {2–5, 6–9, 14–17, 18–21}; Linux CPU ↔ LP map (order-preserving, consistent with favored P = Linux 0,3 = LP 0,11): `docs/research/win-xtu-live-trace.md` S-03d.
Operator's Linux repos (Fedora): `~/AI/turbostat+` (read-only monitor, copied to `C:\Users\yk\AI\turbostat+` on win — **orchestrator-only**) and `~/AI/turbostat` (research repo with the OCMB decode table/trace evidence — **not yet copied**).

## Host `win` — Windows 11 Pro build 26200 [MEASURED win 2026-09-30]
- VBS/HVCI: off. Session was **not** elevated (Secure Boot query denied).
- Present: git 2.55, Python 3.14, Node, winget, VS Build Tools 2026, HWiNFO 8.52, Intel XTU 10.0.1.188,
  Intel IPF/DTT/KPE drivers (ipf_cpu, ipf_acpi, ipf_smbus, kpe_apo).
- Absent: WSL, .NET SDK (`dotnet`), `gh`, cargo/go, Sysinternals, WinDbg/WDK, ILSpy, Ghidra.
- Stale/registered kernel drivers (all stopped): OCCT `ORingDrv` (path in `%TEMP%\OCCT\...`), ASRock
  `AsrDrv202/204`, HWiNFO `HWiNFO_215/216` (in `%TEMP%`).
- XTU details: `docs/research/win-xtu-inventory.md`.

## Host `lnx` — Linux install
- Fedora 44, kernel 7.2 [user-reported 2026-09-30].
- [user-reported 2026-09-30] kernel 7.2, **`/dev/cpu/*/msr` exists**, Secure Boot off, OC unlocked, UVP disabled; **OCCT runs only when the operator opens it manually in Fedora** (never in the background).
- **Secure Boot: OFF; MSR writes are not blocked** [user-reported 2026-09-30] → no lockdown; raw writes need `msr.allow_writes=on` (Fedora builds the `msr` driver in) and will taint the kernel.
  `msr` module/`/dev/cpu/*/msr` presence and taint state: still to be confirmed by T-003 on Fedora.

## BIOS state (ASRock Z890 Steel Legend WiFi, BIOS 3.40) [user-reported 2026-09-30]
- **Undervolt Protection: OFF. OC Lock: OFF.** This is why XTU's runtime writes work (XTU shows a −0.060 V GT offset applied). Runtime mailbox writes are therefore expected to be accepted on Linux too (unverified there).
- Other BIOS OC settings (exposed only as one shared P-core V/F-point offset; no E-core V/F) as described in the Machine table.
- User-reported existing work: Python readers for MCHBAR/registers + a modified `turbostat` that reads per-core VID.
  It lives in a git repo on the Fedora disk; the human will push it to a private GitHub repo so `win` can clone it. URL: **TBD**.

## Approved tooling (record every approved install here)
- Installed by the human 2026-09-30 [MEASURED win, `winget list` / `dotnet tool list -g`]: ILSpy 11.1.0.9782 · Detect It Easy 3.21 · .NET SDK 10.0.401 +
  global tool `ilspycmd` 11.1.0.9782 · Process Monitor 4.11 · Process Explorer 17.14 · Temurin JDK 21.0.12.1. (Fresh shells needed for new PATH entries.)
- Already present: WinDbg 1.2606.22001.0 · 7-Zip · HWiNFO 8.52.
- 2026-10-01 (operator: "copy whatever you need"): 5 files copied from the installed WinDbg package (`...\WindowsApps\Microsoft.WinDbg_1.2606.22001.0_x64__8wekyb3d8bbwe\amd64\`: `cdb.exe dbgeng.dll dbghelp.dll dbgmodel.dll dbgcore.dll`, ≈13.6 MB, cdb 10.0.29617.1000) into git-ignored `tools/windbg-amd64/`, because processes outside the package cannot execute from `WindowsApps`. No download. Used only by `tools/win/ipf_trace/` (T-019); symbol path is always an empty local dir (no symbol server).
- Ghidra 12.1.4 (`ghidra_12.1.4_PUBLIC_20260921.zip`, 569,649,598 B expected) — human-downloaded into the repo root (git-ignored via `/*.zip`);
  verify size/digest, then extract to `tools/ghidra/` (git-ignored). Static analysis only.
- ThrottleStop 9.7.3 — user-provided, **static analysis only** (`captures/thirdparty/`, git-ignored, never executed by an agent).
  See `docs/research/win-throttlestop-9.7.3.md`.
