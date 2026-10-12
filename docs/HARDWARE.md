# Hardware and host facts

> Carried over 2026-10-08 from the project's earlier research phase. Paths `docs/knowledge/*`, `docs/tasks/T-0xx` and `captures/*` cited here name notes of that phase, which are not part of this repository.

## Machine [MEASURED win 2026-09-30]
| Item | Value |
|---|---|
| CPU | Intel Core Ultra 9 285K (Arrow Lake-S), 24 cores / 24 threads (8P + 16E, no HT), CPUID `0xC0662` |
| Board | ASRock Z890 Steel Legend WiFi |
| BIOS | AMI 3.40 (dated 2026-09-15) |
| Disks | disk 0 Kingston 2 TB: ESP + 2 GB + 1.9 TB Linux-typed partitions; disk 1 Samsung 990 Pro 1 TB: Windows |
| BIOS limits (user-reported) | a **per-core V/F offset** for P-cores (operator 2026-10-01: −50 mV at the 57× point, effective ≈ −40 mV vs fused: VID 1.3864 V vs fused 1.4258 V on P-Core 0); no E-core V/F; no absolute voltages shown (earlier note said one shared all-core offset — corrected) |

## CPU numbering [MEASURED lnx 2026-09-29 via a read-only monitoring capture; XTU mapping inferred]
Linux CPUs **0–7 = P-cores, 8–23 = E-cores** (no SMT); E-clusters of 4 = **8–11, 12–15, 16–19, 20–23**; favored P-cores = **CPU 0 and 3**. XTU "P-Core N" = Linux CPU N; XTU "E-Core k" = Linux CPU 8+k. **Windows logical-processor numbers (mailbox masks, XTU logs "Core N") are NOT P-first** [MEASURED win 2026-10-01]: P = LP {0,1,10,11,12,13,22,23}, E = LP {2–5, 6–9, 14–17, 18–21}; Linux CPU ↔ LP map (order-preserving, consistent with favored P = Linux 0,3 = LP 0,11): `docs/knowledge/registers.md` "Measured via XTU's level-4 log on Windows".

## Host `win` — Windows 11 Pro build 26200 [MEASURED win 2026-09-30]
- VBS/HVCI: off. Session was **not** elevated (Secure Boot query denied).
- Present: git 2.55, Python 3.14, Node, winget, VS Build Tools 2026, HWiNFO 8.52, Intel XTU 10.0.1.188,
  Intel IPF/DTT/KPE drivers (ipf_cpu, ipf_acpi, ipf_smbus, kpe_apo).
- Absent: WSL, .NET SDK (`dotnet`), `gh`, cargo/go, Sysinternals.
- Stale/registered kernel drivers (all stopped): OCCT `ORingDrv` (path in `%TEMP%\OCCT\...`), ASRock
  `AsrDrv202/204`, HWiNFO `HWiNFO_215/216` (in `%TEMP%`).
- XTU details: `docs/knowledge/registers.md`.

## Host `lnx` — Linux install
- Fedora 44, kernel 7.2 [user-reported 2026-09-30].
- [user-reported 2026-09-30] kernel 7.2, **`/dev/cpu/*/msr` exists**, Secure Boot off, OC unlocked, UVP disabled; **OCCT runs only when the operator opens it manually in Fedora** (never in the background).
- **Secure Boot: OFF; MSR writes are not blocked** [user-reported 2026-09-30] → no lockdown; raw writes need `msr.allow_writes=on` (Fedora builds the `msr` driver in) and will taint the kernel.
  `msr` module/`/dev/cpu/*/msr` presence and taint state: still to be confirmed by T-003 on Fedora.

## BIOS state (ASRock Z890 Steel Legend WiFi, BIOS 3.40) [user-reported 2026-09-30]
- **Undervolt Protection: OFF. OC Lock: OFF.** This is why XTU's runtime writes work (XTU shows a −0.060 V GT offset applied). Runtime mailbox writes are therefore expected to be accepted on Linux too (unverified there).
- Other BIOS OC settings (exposed only as one shared P-core V/F-point offset; no E-core V/F) as described in the Machine table.
- User-reported existing work: Python readers for MCHBAR/registers + a modified `turbostat` that reads per-core VID (the operator's own tooling; not part of this repository).

## Approved tooling
- ThrottleStop 9.7.3 — user-provided; its signed driver is carved at runtime from the operator's own exe (`tools/win/ts_driver.py`), never committed, and stopped after applying.
- Vendor tools were studied statically for interoperability only; no vendor code or binaries are part of this repository.
