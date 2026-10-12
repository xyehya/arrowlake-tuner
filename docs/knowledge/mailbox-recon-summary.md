Distilled 2026-10-08 from the project's earlier research phase; the raw notes and captures are not part of this repository.
# Mailbox recon summary: what XTU, IPF and ThrottleStop taught us

Register/command tables: registers.md (copied separately, not repeated).

Detail: the earlier research notes (XTU mailbox and controls analysis T-001/T-002, native provider T-005, ThrottleStop FIVR analysis T-006, access manifest T-017 with 82 access rows and 11 conflicts, live trace T-020, IPF primitives table of 412 rows, XTU inventory, recon methods) are not part of this repository. Raw decompiles and traces were never committed.

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
| Per-core scope switch = cmd 0x14/0x15 P1=0 bit 3 ("PerCore Voltage Enable"); already set on this box (0x14 = 0x8); XTU sets it around writes | [DECOMPILED SetOCMBPerCoreMode]; [MEASURED] |
| Every Apply writes `0x11` (full word: ratio = `0x1C[7:0]`, V target = fused `0x09 P1=0x11` voltage, adaptive, offset), then `0x1D` (= ratio), reads `0x26`, writes `0x27` (= that value). It never writes a 0 ratio/V field, so XTU cannot restore a "V target 0" baseline word | [MEASURED win 2026-10-01] S-04/04d/06 |
| E-cores: XTU has no cluster concept but aggregates and writes all 4 cores of an E tile in one Apply (16 cores in ~0.3 s: all 0x11, then all 0x1D, then all 0x27). Whether hardware needs all four (C-4): on Linux we always write all four | [MEASURED win 2026-10-01]; [DECOMPILED] |
| Offset field adds to the BIOS per-core V/F offset (P0 -15 mV runtime moved VID max 1.385 -> 1.371 V) | [MEASURED win 2026-10-01 HWiNFO] |
| XTU exposes only a whole-curve per-core offset: `SupportsVFCurve=false`, `SupportsPerPointVoltageOffset=false` on ARL-S, no VF Curve screen; the mailbox does encode points P2 = 1..15 and our product uses them | [DECOMPILED win-xtu-decompile-controls.md s7] |
| Completion codes: 0x01 OCLocked, 0x03 RatioExceedsLimits, 0x05 OCNotSupported, 0x0A RuntimeMemoryDisabled, 0x12 VoltageExceedsLimit, 0x13 UndervoltProtection; 0x11 (P2 past the table) is not in XTU's enum | [DECOMPILED MailboxCompletionCode]; 0x11 [MEASURED lnx 2026-10-02] |
| Orders cached writes voltage-first when performance rises, ratio-first when it falls; arms the ICCWDT watchdog (10 s timeout, ping 5 s) before a no-reboot apply; no hardware rollback; transport failures re-sent up to 100x at 500 ms, rc != 0 never retried | [DECOMPILED XtuTuning, ActivationOrdering] |
| Persistence is service-side (`PerformanceSettings.bin`), re-applied at service start unless the watchdog flagged a failure; hardware state does not survive reset (matches our reboot data) | [DECOMPILED]; [MEASURED lnx/win 2026-10-01/02] |
| Control IDs (285K): P ratio 0x74-0x7B, override 0x8C-0x93, offset 0xA4-0xAB, mode 0xE8-0xEF; E 0x5DC+i, 0x9C4+i, 0xDAC+i, 0x157C+i; P turbo 0x1D,0x1E,0x1F,0x20,0x2A,0x2B,0x60,0x61; ranges are generated from hardware reads at start, not hard-coded | [DECOMPILED ControlIdHelper] |
| Turbo tables go through IPF properties -> MSR 0x1AD/0x1AE/0x650/0x651 (whole 64-bit RMW on CPU 0; P = NoBuckets) | [DECOMPILED win-xtu-decompile-mailbox.md, MSR-tuning table]; confirmed by our plain-MSR write 2026-10-01 |
| "Voltage Override" in the XTU UI = the fused `0x09 P1=0x11` word voltage (P0 1.426 V), not the `0x10` word | [MEASURED win 2026-10-01] |
Values read at start: `0x01` (OC allowed bits per domain, P1=3 rc 5), `0x02` per-active-core fused max ratio (57,57,54,54), `0x16` IccMax 400 A, `0x04` raw 0x84002000 (layout unresolved, C-1), `0x05` BCLK monitor ~100000, `0x24` TVB reads (70 C cutoff).

## 3. ThrottleStop 9.7.3 (static) [DECOMPILED win-throttlestop-fivr.md]
- Same wire word; driver IOCTL map in `windows-path.md`. NO per-core voltage write: its "Per Core" is the favored-core ratio (`0x8000001D_000000RR`); voltage is per plane (P1 0..5) or V/F point (P2 1..11) and is NOT pinned to a core. Plane offset example: `0x80000011_F9A00000` (-50 mV).
- Fire-and-forget writes (no poll, no readback, no timeout); RMW only for ratio byte, AVX (0x1A/0x1B), residency bits (0x18/0x19). Startup probes include WRITES to cmd 0x1B (restored).
- Gating: CPUID model mask `0xC0660` (ARL-S) grouped with ADL/RPL; probes 0x1AE/0x650/0x651; OC lock = MSR 0x194 b20; 0xCE b24/b28; UVP = 0x195 b1; PL lock = 0x610 b63; ICCMax mask 0x7FF. Works on 285K/BIOS 3.40 through our own code, not through its GUI [MEASURED win 2026-10-02].

## 4. Open or unresolved (C-n from access-manifest)
| ID | Question | State |
|---|---|---|
| C-1 | cmd 0x04 layout (XTU vs ThrottleStop disagree; sample 0x84002000 decodes with neither) | open |
| C-2 | PL4 via MSR 0x601 vs MMIO 0x59B0; 0x601 = 425 A is design current, not IccMax | open |
| C-4 | one E write moves its cluster? | open; XTU writes all 4 |
| C-5 | persistence | RESOLVED: see platform-285k.md s7 |
| C-6 | ratio via 0x11 alone vs 0x1D | RESOLVED for XTU shape (writes 0x11+0x1D+0x27) |
| C-7 | core numbering | RESOLVED, `platform-285k.md` s2 |
| C-8/9 | PL1/PL2 MMIO vs MSR; MMIO base (MCHBAR vs AD03 BAR) | open (Linux read decides) |
| C-3, C-10, C-11 | TCC field width; doc staleness; `SET 0x15` rc 1 seen twice with OC lock off | open/moot |
Tags in the sources: only MEASURED/DECOMPILED/DOC facts may become constants; every DECOMPILED fact still needs a MEASURED read-back.
