# Mailbox recon summary: what XTU, IPF and ThrottleStop taught us

How Intel's tools drive the OC mailbox on Arrow Lake-S (XTU 10.0.1.188, ThrottleStop 9.7.3, measured on the reference 285K under Windows 11).
Register/command tables: `registers.md` (not repeated). The `[DECOMPILED …]` notes and the access manifest (C-n ids) are earlier research on the
reference machine and are not part of this repository; no vendor code or binaries are.

## 1. Call chain (XTU -> hardware) [DECOMPILED + MEASURED win 2026-10-01]
XTU managed code (OcMailboxAccess -> OcMailboxService, named mutex `Global\Access_Intel_OC_Mailbox`) -> IpfService JSON `{"MailboxAction":{"version":1,"cmd","param1","param2","ocmb_affinity_data":[{"group","affinity_mask","data"}]}}` via client API "Platform.SOC.Overclocking" -> native `XtuProvider.dll` (inside `XtuService.exe`) -> IPF host `WUDFHost.exe` (`ipfsrv.dll` + `ipf_umdf2.dll`) -> `\\.\ipf_lf` IOCTL 0x120004 -> `ipf_cpu.sys` -> pin thread to the target LP (KeSetSystemGroupAffinityThread) -> RDMSR 0x150 until bit 63 = 0 -> WRMSR `1<<63|P2<<48|P1<<40|cmd<<32|data` -> poll -> data [31:0], rc [39:32].
- ipf_cpu.sys: 10 ms sleeps, ~10 s cap, timeout before the write = no write; no retry; no lock in the OCMB path; cmd allow-list 0x01-0x09 and 0x10-0x29 (0x29 allowed though XTU never writes it); MSR allow-list (incl. 0x150, 0x198, 0x1AD, 0x1AE, 0x610, 0x620; list truncated in the note, see s5 there); the driver never interprets the completion byte [DECOMPILED win-xtu-native-provider.md s5].
- No MMIO/PCI path for the OC mailbox on this platform. IPF primitive 0x33E = OC mailbox (primitive ids 830/831 GET/SET_OC_MBOX in the public ESIF headers [DOC github.com/intel/dptf]).
- Live trace: `ipf_uf.exe`/`ipfsvc.exe` stay dormant; all 431 IOCTL 0x120004 calls return STATUS_PENDING, so requests are captured, responses are not. XTU start-up = 546 accesses, 534 reads, 0 writes (it does not re-apply on service restart) [MEASURED win 2026-10-01].
- Trick that worked: registry `HKU\.DEFAULT\Software\Intel\XTU\LoggingLevel` DWORD = 4 makes XtuCore log every mailbox transaction with Data and RC (default 3 logs only failures); reverted afterwards [MEASURED win 2026-10-01].
- Windows-only recon that worked: passive cdb breakpoint on `ntdll!NtDeviceIoControlFile` (resolve by `ntdll + RVA`); attach to `WUDFHost.exe`. UI automation cannot click elevated XTU (UIPI).

## 2. XTU behaviour that shapes the product
| Finding | Evidence |
|---|---|
| Per-core P and E writes are `cmd 0x11 P1=0 P2=0` with the V/F word; core chosen by (group, affinity mask), never by P1/P2/data | [DECOMPILED MailboxFieldResolver, PerCoreRatioControls]; [MEASURED win 2026-10-01] |
| Per-core scope switch = cmd 0x14/0x15 P1=0 bit 3 ("PerCore Voltage Enable"); XTU sets it around writes when it is clear (it was already set on the reference machine) | [DECOMPILED SetOCMBPerCoreMode]; [MEASURED win 2026-10-01] |
| Every Apply writes `0x11` (full word: ratio = `0x1C[7:0]`, V target = fused `0x09 P1=0x11` voltage, adaptive, offset), then `0x1D` (= ratio), reads `0x26`, writes `0x27` (= that value). It never writes a 0 ratio/V field, so XTU cannot restore a "V target 0" baseline word | [MEASURED win 2026-10-01] S-04/04d/06 |
| E-cores: XTU has no cluster concept but aggregates and writes all 4 cores of an E cluster in one Apply (all 0x11, then all 0x1D, then all 0x27). Whether hardware needs all four is open (C-4); this app always writes all four | [MEASURED win 2026-10-01]; [DECOMPILED] |
| The runtime offset field adds to a BIOS per-core V/F offset (a -15 mV runtime offset moved the VID by -14 mV on a core with a BIOS offset) | [MEASURED win 2026-10-01 HWiNFO] |
| XTU exposes only a whole-curve per-core offset: `SupportsVFCurve=false`, `SupportsPerPointVoltageOffset=false` on ARL-S, no VF Curve screen; the mailbox does encode points P2 = 1..15 and our product uses them | [DECOMPILED win-xtu-decompile-controls.md s7] |
| Completion codes: 0x01 OCLocked, 0x03 RatioExceedsLimits, 0x05 OCNotSupported, 0x0A RuntimeMemoryDisabled, 0x12 VoltageExceedsLimit, 0x13 UndervoltProtection; 0x11 (P2 past the table) is not in XTU's enum | [DECOMPILED MailboxCompletionCode]; 0x11 [MEASURED lnx 2026-10-02] |
| Orders cached writes voltage-first when performance rises, ratio-first when it falls; arms the ICCWDT watchdog (10 s timeout, ping 5 s) before a no-reboot apply; no hardware rollback; transport failures re-sent up to 100x at 500 ms, rc != 0 never retried | [DECOMPILED XtuTuning, ActivationOrdering] |
| Persistence is service-side (`PerformanceSettings.bin`), re-applied at service start unless the watchdog flagged a failure; hardware state does not survive reset (matches our reboot data) | [DECOMPILED]; [MEASURED lnx/win 2026-10-01/02] |
| Control IDs (285K): P ratio 0x74-0x7B, override 0x8C-0x93, offset 0xA4-0xAB, mode 0xE8-0xEF; E 0x5DC+i, 0x9C4+i, 0xDAC+i, 0x157C+i; P turbo 0x1D,0x1E,0x1F,0x20,0x2A,0x2B,0x60,0x61; ranges are generated from hardware reads at start, not hard-coded | [DECOMPILED ControlIdHelper] |
| Turbo tables go through IPF properties -> MSR 0x1AD/0x1AE/0x650/0x651 (whole 64-bit RMW on CPU 0; P = NoBuckets) | [DECOMPILED win-xtu-decompile-mailbox.md, MSR-tuning table]; confirmed by our plain-MSR write 2026-10-01 |
| "Voltage Override" in the XTU UI = the fused `0x09 P1=0x11` word voltage, not the `0x10` word | [MEASURED win 2026-10-01] |
Read at every XTU start: `0x01` (OC allowed bits per domain; P1=3 answers rc 5), `0x02` per-active-core fused max ratio, `0x16` IccMax, `0x04`, `0x05`
BCLK monitor, `0x24` TVB (reference values: `examples/reference-285k/notes/platform.md` §4).

## 3. ThrottleStop 9.7.3 (static) [DECOMPILED win-throttlestop-fivr.md]
- Same wire word; driver IOCTL map in `windows-path.md`. NO per-core voltage write: its "Per Core" is the favored-core ratio (`0x8000001D_000000RR`); voltage is per plane (P1 0..5) or V/F point (P2 1..11) and is NOT pinned to a core. Plane offset example: `0x80000011_F9A00000` (-50 mV).
- Fire-and-forget writes (no poll, no readback, no timeout); RMW only for ratio byte, AVX (0x1A/0x1B), residency bits (0x18/0x19). Startup probes include WRITES to cmd 0x1B (restored).
- Gating: CPUID model mask `0xC0660` (ARL-S) grouped with ADL/RPL; probes 0x1AE/0x650/0x651; OC lock = MSR 0x194 b20; 0xCE b24/b28; UVP = 0x195 b1; PL lock = 0x610 b63; ICCMax mask 0x7FF. Its driver works on ARL-S through our own code [MEASURED win 2026-10-02].

## 4. Open or unresolved (C-n from the access manifest)
| ID | Question | State |
|---|---|---|
| C-1 | cmd 0x04 layout (XTU vs ThrottleStop disagree; the reference sample `0x84002000` decodes with neither) | open |
| C-2 | PL4 via MSR 0x601 vs MMIO 0x59B0; 0x601 is a design current, not IccMax | open |
| C-3 | TCC offset field width in 0x1A2 (`[29:24]` SDM vs `[30:24]` IPF) | open |
| C-4 | does one E-core write move its whole cluster? | open; XTU and this app write all 4 |
| C-8/9 | PL1/PL2 MMIO vs MSR; MMIO base (MCHBAR vs processor-thermal BAR) | open (on the reference board MMIO RAPL is disabled and the MSR is in effect, `platform-arl-s.md` §12) |
| C-11 | `SET 0x15` rc 1 seen twice with OC Lock off | open |
Resolved: C-5 persistence (`platform-arl-s.md` §7); C-6 ratio via `0x11` + `0x1D` + `0x27` (+ turbo table, §8); C-7 core numbering (§2); C-10 doc staleness.
Only MEASURED/DECOMPILED/DOC facts may become constants; every DECOMPILED fact still needs a MEASURED read-back.
