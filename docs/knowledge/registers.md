# Registers, MSRs, mailbox commands — single source of truth

Encodings for Arrow Lake-S. Code cites the section names below; keep them. Measured values are from one Core Ultra 9 285K (the "reference
machine"); its full readings are in `examples/reference-285k/notes/platform.md` (one sample, not a spec).

Status legend: `decomp` = read from Intel's XTU code (usable as a constant only after a MEASURED read-back) · `doc` = primary public source ·
`lead` = COMMUNITY/UNVERIFIED (never a constant) · `seen` = observed in XTU's UI on the reference machine · `measured` = read on the reference machine ·
`trace` = recorded from OCCT/HWiNFO mailbox traces on the reference machine (corroboration).
Write allowlist: a register joins the guard's allowlist only via a DECISIONS.md entry plus a MEASURED read (the guard's tables in `src/arltuner/guard/policy.py`).
`[DECOMPILED …]` file names (`win-xtu-decompile-mailbox.md`, `win-xtu-decompile-controls.md`, `win-xtu-inventory.md`, `win-xtu-live-trace.md`,
`win-throttlestop-fivr.md`, `lnx-interfaces.md`) name notes of the earlier research on the reference machine; they are not part of this repository.

## OC mailbox — request model (from XTU)
XTU builds a request tuple `(cmd u8, P1 u8, P2 u8, data u32, scope)` and hands it to native `XtuProvider.dll` as an IPF JSON `MailboxAction` on `Platform.SOC.Overclocking`. `decomp`
- **Scope:** package (no core) or core-scoped: logical core `L` (group `L/64`, affinity mask bit `L%64`). **The core is chosen only by the scope, not by P1/P2.** P and E cores use the same field ids and the same request (P1=0, P2=0).
  There is no E-cluster/module address in XTU. "Logical core index" = position in IPF `Platform.SOC.Info.Core` = the Windows logical processor, which is not the Linux CPU number (`platform-arl-s.md` §2).
- **Wire packing — decoded independently from XTU's IPF driver `ipf_cpu.sys` (T-005) and from ThrottleStop 9.7.3 (T-006), both `decomp`; matches FSP/V0LTpwn/kernel `doc`; round-trips MEASURED on Linux and Windows (2026-10-01/02).**
  WRMSR **MSR 0x150** with the 64-bit value `1<<63 | P2<<48 | P1<<40 | cmd<<32 | data` (i.e. `hi = 0x80000000 | P2<<16 | P1<<8 | cmd`, `lo = data`). Completion: poll RDMSR 0x150 until bit 63 clears; status = `hi & 0x1F` (0 = OK); result data in `lo`.
  ThrottleStop's driver has **no CPU selection**: RDMSR/WRMSR run on the calling thread's CPU, so the caller pins (`SetThreadAffinityMask`). Its write sites mostly fire-and-forget (no poll, no timeout, no readback) — our code must poll with a timeout and read back.
  Example (legacy plane word, CPU Core, −50 mV, adaptive): `0x80000011_F9A00000`. Driver IOCTLs `0x80006448` (RDMSR) / `0x8000644C` (WRMSR) are ThrottleStop-specific.
  **XTU/IPF sequence (T-005):** `ipf_cpu.sys` pins the request's `(group, mask)` CPU (`KeSetSystemGroupAffinityThread`; package scope = whichever CPU), reads MSR 0x150 until bit 63 = 0 (10 ms sleeps, ≈10 s cap; timeout ⇒ **abort without writing**), WRMSR, polls again;
  response data `[31:0]`, completion code `[39:32]`; no retry, no lock below XTU's own mutex, no rollback; the driver accepts only cmds 0x01–0x09 and 0x10–0x29. There is **no MMIO/PCI path** for the OC mailbox: every request, *including reads*, is a WRMSR to 0x150
  (hardware-write tier). Example: `SET 0x11 P1=0 P2=0 data=D` → `0x8000001100000000 | D` (`D=0x0004A02D` → `0x80000011_0004A02D`). "Logical core L" = Windows logical processor L (group L/64, bit L%64).
- Cross-process mutex `Global\Access_Intel_OC_Mailbox` in XTU (other Intel tools share the mailbox). ICC watchdog (device `ACPI\INTC109F`) is armed before a no-reboot apply (10 s default). `decomp`

## V/F data word — cmd 0x10 (read) / 0x11 (write), P1=0 P2=0, core-scoped  `decomp`
| Bits | Field | Encoding |
|---|---|---|
| [7:0] | ratio | integer multiplier (×BCLK); ARL-S is *granular*: integer part here + bins in cmd 0x26/0x27 `[15:8]` (`g - (w mod g)`, g from 0x26 `[2:0]`) |
| [19:8] | voltage target (override) | 12 bit, S2.10: V = raw/1024 (0x48C → 1.1367 V, 0x4A0 → 1.1563 V) |
| [20] | mode | 0 = adaptive, 1 = static/override |
| [31:21] | voltage offset | 11 bit signed two's complement, LSB = 1/1024 V (XTU shows raw·1000/1024 mV); XTU allows −1000…+999 mV (raw 0x400…0x3FF); −50 mV = 0x7CD |
ThrottleStop uses the same word (`decomp`): display ratio = `w&0xFF`, target = `((w>>8)&0xFFF)/1024` V, mode = `w&0x100000` (Static), offset sign-extended 11 bit ×1000/1024 mV; mV→raw = `(mV·1024+512)/1000` (round half up on magnitude), −50 mV → raw 51 → `0xF9A00000`.
Its offset slider spans ±128 / ±256 / −1024…+1023 raw (≈ ±125 mV, ±250 mV, −1000…+999 mV).
Checks against XTU's UI `seen`: its "Voltage Override" values decode with this layout (e.g. 1.137 V = 0x48C); they are the cmd `0x09` words, not `0x10` (section "Measured via XTU's level-4 log").
**Third confirmation `trace` (OCCT/HWiNFO mailbox traces on the reference machine):** `0x03400030` = point 4 / 48×, `0x03400039` = point 8 / 57× with offset raw 26 (+25.39 mV) and **V-target field 0** (an offset-only write, like ThrottleStop's V/F-point writes that send "offset bits only"; corrected by T-010 — bits [19:8] of `0x03400030` are 0, not 0x400) — **per-V/F-point offsets exist and were written (P2 = point index)**;
ring (domain 2) 40× target `0x4C8` = 1195.31 mV; NGU (domain 7) 34× `0x4BD` = 1184.57 mV; `cmd 0x1C` high byte = fused max ratio (285K: E 46×, P 55×/57×); `cmd 0x16` data = IccMax in 0.25 A; domain 6 = memory subsystem.
**XTU's write procedure** (a description of behaviour, not a recipe): (1) package scope: read cmd 0x14 (P1=0,P2=0); if bit 3 ("PerCore Voltage Enable") is 0, write cmd 0x15 with `old|8`; restore afterwards. (2) core scope: read 0x10 → patch only the wanted bit-slices →
write whole word with 0x11 (read-modify-write; for P2≠0 the read word is masked `&0xFFFFFF00`). (3) a ratio write also writes cmd 0x1D (P1=0,P2=0) with data = ratio (favored-core word, no RMW). (4) cores whose merged words are equal share one action. (5) re-read all per-core fields.
**No external oracle** exists for XTU's "PerCore Voltage Enable" path: ThrottleStop 9.7.3 never touches cmd 0x15 bit 3 and has no per-core *voltage* write (its "Per Core" = favored-core ratio via 0x1C/0x1D pinned per CPU).
Ordering: raising performance → voltage first, lowering → frequency first; markers `0xE0000002/4` bracket a batch. No hardware rollback on failure (watchdog + persistence only). `decomp` (T-002 §Apply flow)

## Voltage semantics (ARL-S) — behaviour in `platform-arl-s.md` §3
Adaptive voltage `[19:8]` (mode `[20]`=0) sets the **OC-ratio point** only (P-core point 9, E-core point 7, added when the OC ratio exceeds the top fused ratio); fused points accept **offsets only** (no ratio or target) `[COMMUNITY SkatterBencher]`; per-point offset writes work `[MEASURED lnx 2026-10-02]`; `[31:21]` offset with P2=0 = whole-curve offset `[MEASURED lnx 2026-10-11]`;
per-point offsets = P2 1..n (n = table length, read live; hidden by XTU on ARL-S) `[MEASURED lnx 2026-10-02]`. E-core voltage is per **cluster of 4** `[COMMUNITY]`. Default voltage ceiling = top fused VID + 200 mV (285K: P 1.581 V, E 1.270 V; absolute max 1.681 / 1.370 V) `[COMMUNITY]`.

## Mailbox commands (rd/wr; P1 = domain or index; P2)  `decomp` unless noted
| Cmd | Name | P1 | P2 | Data |
|---|---|---|---|---|
| 0x01 | OCCapabilityRead | domain | 0 | [7:0] fused max OC ratio · [8] ratio-OC ok · [9] voltage-override ok · [10] voltage-offset ok |
| 0x02 | PerCoreCapabilityRead | word `n/4` (n=active cores−1) | 0 (1 = E-core 1C turbo) | byte `n%4` = fused max OC ratio for n+1 active cores |
| 0x03 | DdrInfoRead | SAGV point 0..3 | 0 | [7:0] point ratio · [19:16] gear code · P1=0 [13:10] channels |
| 0x04 | ICCMaxVRRead | 0 | 0 | [11:8] core IccMax VR addr · [12] SVID en · [16:13] GT VR addr · [17] GT SVID en. **Conflict:** ThrottleStop decodes 5×5-bit fields from bit 0 → resolve with one MEASURED read |
| 0x05 | ReferenceClkMonitorRead | 0 int SoC · 1 ext SoC · 2 int CPU · 3 ext CPU | 0 | raw monitor (unit unknown); >0 on "internal" ⇒ that BCLK domain is settable |
| 0x09 | VoltageFloorRead | 0x11 (dom floors) / 0x21 (AVX scale) | domain | same fields as 0x10 (floors); used for UVP lower bounds |
| **0x10/0x11** | VoltageFreqRead/Write | domain (below) | 0 = whole domain/core; 1..15 = V/F point (hidden on ARL-S) | see V/F data word |
| **0x14/0x15** | MiscGlobalConfigRead/Write | 0 | 0 | [3] **PerCore Voltage Enable** (ARL client); [0],[1] server FIVR |
| 0x16/0x17 | ICCMaxRead/Write | VR addr (from 0x04) | 0 | [10:0] U9.2 (0.25 A/LSB, raw 4..2046) · [31] unlimited |
| 0x18/0x19 | ResidencyStateRegulation | 0 | 0 | [1] ring-to-core offset disable · [2] TVB **disable** · [4] further disable bit (ThrottleStop: bits 1, 2, 4 are all *disable* bits — resolves the polarity question) |
| 0x1A/0x1B | AVXOffsetControl | 0 | 0 | [9:5] AVX2 negative core-ratio offset 0..31 (pre-ARL: [14:10] AVX-512 offset) |
| 0x1C/0x1D | FavoredCoreRead/Write | 0 | 0 | rd [7:0] favored ratio, [15:8] fused; wr = whole word = ratio (no RMW) |
| 0x22/0x23 | Bclk read/write | 0 SoC · 2 CPU | 0 | [19:0] BCLK, 0.001 scale; range 70…200 MHz; only if domain "internal" |
| 0x24/0x25 | TVBRead/Write | 1/17 = per active-core bucket set1/2; 0/16 = per CCP set1/2 | bucket 0..7 or CCP(tile) | [7:0] temp cutoff · [15:8] ratio offset |
| 0x26/0x27 | GranularRatio | 0 (core-scoped) | 0 | rd [2:0] granular index; r/w [15:8] ratio bins |
| 0x28 | MaximumVoltageLimitRead | domain | 0 | [11:0] max voltage S2.10 (XTU uses raw−1; 0 ⇒ 2.0 V) |
**Limits in the tool (G-137, D-039):** ratio ceiling = policy value clamped by `0x01 P1=0 [7:0]` of a CPU of that kind (needs `[8]` set; the reference P-cores read `0x778` = 120x `[MEASURED win 2026-10-01]`; XTU's decompile names no separate E domain for `0x01`, field ids 19-21 = "IA" for all core CPUs `[DECOMPILED win-xtu-decompile-controls.md]`, so the E value is read with P1=0 pinned to an E CPU, whether it differs from P is `[UNVERIFIED]`); V target ceiling = policy value clamped by `0x28 P1=0 [11:0] - 1 LSB` of a CPU of that kind (P 0x681 -> 1625 mV, E 0x56D -> 1355 mV on the reference chip `[MEASURED win 2026-10-01]`); offset -1000..+999 mV (field encoding). `0x1C` answers on E CPUs too (E `0x2E30` = [7:0] 48, [15:8] fused 46 `[MEASURED win 2026-10-01]`, `lnx 2026-10-11`): the `"auto"` ratio ceiling uses the **fused byte `0x1C[15:8]`** only (highest over the CPUs of the kind, + 3): P 0x3939 / 0x3737 -> 57 / 55 so P 57 + 3 = 60, E 0x2E30 -> 46 so E 46 + 3 = 49. `[7:0]` is the favored/current ratio (E 48, follows the BIOS turbo setting and a `0x1D` write), so it would ratchet; `[15:8]` is the fused value on both kinds in `msr/measured_baseline.FAVORED_WORD` `[MEASURED win 2026-10-01]`.
Unused on ARL-S: 0x07 (fused per-core ratio/voltage), 0x12/0x13 (SVID, server), 0x20/0x21 (AVX scale), 0x1E/0x1F (real-time memory; RC 0x0A seen).
**Domain P1 wire values:** 0 core/IA · 1 GT slice · 2 ring/cache (CLR) · 3 GT unslice · 4 SA (present but **not exposed by XTU on ARL**) · 7 NGU / "SA Fabric". (XTU's internal `MailboxDomain` enum values differ from the wire.)
ThrottleStop plane indices (P1, `decomp`): 0 CPU Core · 1 GPU · 2 CPU (P) Cache · 3 iGPU Unslice · 4 System Agent · 5 Analog I/O (labelled "CPU E Cache" on hybrid — lead for ARL); it never uses 7.
**Limits XTU applies:** ring `[MaxEfficiencyRatio … cap(0x01 P1=2)]`; NGU `[6 … cap(0x01 P1=7)]` (+ BIOS-mailbox SAGV ratio bits [15:8], 0 = dynamic); voltage override 0.5–2.0 V (cap from 0x28); offset −1000…+999 mV; UVP raises lower bounds from cmd 0x09;
per-core ratio range `[MaxNonTurboRatio … fused max OC ratio]`. E-core cache/VccIO/VccU capability ids exist in code but are absent from this build's field table.

## Return codes (`MailboxCompletionCode`)  `decomp`
Hardware status = bits [39:32] (full byte, IPF driver; the codec's `Response.rc`); ThrottleStop masks `& 0x1F`; 0xFE/0xFF are XTU/provider-synthetic, never hardware. Ids 0x07, 0x12/0x13, 0x1E/0x1F, 0x20/0x21 are "unused on ARL-S": `Cmd` names/polarity for them are inferred (odd id = write) `[UNVERIFIED]`.
0x00 Pass · 0x01 OCLocked · 0x02 UnsupportedDomain · **0x03 RatioExceedsLimits** (XTU converts to success for ratio-only per-core writes) · 0x04 VoltageExceedsMax · **0x05 OCNotSupported** (cmd 1 P1=0 ⇒ "hardware OC locked") ·
0x06 FIVRError · 0x07 IccMaxInvalid · 0x08 IllegalCommandSelectedInParam1 · 0x09 InvalidAVXLevel · 0x0A RuntimeMemoryDisabled · 0x0B InvalidMemControllerId · 0x10 InvalidParam · **0x11 not in enum (unknown)** ·
0x12 VoltageExceedsLimit · **0x13 UndervoltProtection** (XTU ignores it for P-core ratio-limit writes) · 0x1F UnrecognizedCommand · 0xFE UnsupportedCmd (XTU-synthetic) · 0xFF MailboxFailure (provider).
Examples from XTU's log on the reference machine (`win-xtu-inventory.md`): `SET 0x11 … RC 3` = ratio above the limit; `SET 0x15 data 8 RC 1` = per-core switch refused (OCLocked; also seen twice with OC Lock off, C-11 in `mailbox-recon-summary.md` §4, open); `GET 0x01 P1=3 RC 5` = GT unslice not overclockable (probed at every start).

## Not the mailbox
Turbo ratio limits (P active-core table, E ranges) go through IPF properties `Performance.TurboRatio/TrlRange/EfficientCoreTurboRatio/EfficientCoreTrlRange` → MSR 0x1AD/0x1AE/0x650/0x651 (whole 64-bit, written on CPU 0) `decomp` (T-005 §6a; stale "unknown" wording fixed 2026-10-01, access-manifest C-10). Power limits, TCC, uncore ratio: IPF/MSR/MMIO (below).
ARL-S constants in XTU `decomp`: 8 P-cores (max 32 E capacity, 16 present), 8 turbo buckets, 2 favored cores, per-core ratios/voltage/favored/granular = yes; V/F-curve, per-point offset, FIVR, AVX2 scale = no.

## MSRs
| MSR | Use | Source | Status / note |
|---|---|---|---|
| 0x150 | OC mailbox (wire format above; no MMIO path — every request incl. reads is a WRMSR) | V0LTpwn; kernel; XTU/IPF + ThrottleStop `decomp` | doc + measured; runtime writes gated by BIOS (OcSupport/OcLock/UVP) |
| 0x1AD | turbo ratio limit: 8 bytes = active-core buckets 1..8 | turbostat | doc; XTU/IPF reads and writes the whole 64 bits of 0x1AD/0x1AE/0x650/0x651 (writes on CPU 0) `decomp`; still measure by read |
| 0x650 | secondary turbo ratio limit (E-cores) | turbostat | doc; **resolved `measured`:** 0x1AE/0x651 are readable on ARL-S (P bucket core counts 1..8; E bucket core counts 1,2,3,4,6,8,12,16) — the earlier "ARL doesn't use 0x1AE" note was wrong |
| 0x770 / 0x771 / 0x774 | HWP enable / capabilities / request | `intel_pstate` | doc; lower-only |
| 0x620 / 0x621 | uncore ratio limit ([6:0] max, [14:8] min) / status | `intel_uncore_frequency` | doc; ring vs NGU unknown. XTU/IPF: 0x620 [6:0] max / [14:8] min, 0x621 [6:0] current ratio `decomp`; XTU also sets `UncoreMax/MinRatio` when the ring ratio changes |
| 0x610 | PKG_POWER_LIMIT (PL1/PL2; bit 63 lock) | kernel RAPL | doc; XTU/IPF prefers MMIO 0x59A0/0x59A4 with this MSR as fallback `decomp` |
| 0x601 | PL4 | kernel RAPL | doc; ARL-S = `rapl_defaults_core` → no PL4 via MSR per kernel, but IPF DSP reads PL4 from MSR 0x601 `[14:0]` `decomp` (PL4 path 0x601 vs MMIO 0x59B0 unresolved) |
| 0x1A2 | TEMPERATURE_TARGET; TCC offset [29:24]; lock bit 31 | `intel_tcc.c` | doc; IPF DSP uses `[30:24]` `decomp` — field width unresolved |
| 0xE2 | PKG_CST_CONFIG_CONTROL; CFG lock bit 15 | coreboot | doc |
| 0x10A | bit 23 = OC status (IPF DSP) | XTU/IPF `decomp` | read-only candidate |
| 0xCE | PLATFORM_INFO: bit 24 = OC available (ThrottleStop shows "Not Available" when 0), bit 28 = programmable turbo ratio limits, [15:8] base ratio | ThrottleStop `decomp` (+Intel SDM) | read; measured ([28] = 1, [24] = 1 with OC unlocked) |
| 0x194 | bit 20 = **OC lock**, [19:17] OC bins (IPF `decomp`) (ThrottleStop skips all FIVR/TRL writes and shows "Locked") | ThrottleStop `decomp` | read; measured ([20] = 0 with BIOS OC Lock off) |
| 0x195 | bit 1 = **Undervolt Protection** indicator | ThrottleStop `decomp` | read-only candidate; never read |
| 0x1AE / 0x651 | probed (with 0x650) by ThrottleStop on ARL; if they answer it enables turbo-ratio write paths (writes 0x1AD/0x1AE/0x650/0x651). MEASURED 2026-09-29: readable; 0x1AE = P bucket core counts 1..8, 0x651 = E bucket core counts 1,2,3,4,6,8,12,16 | `measured` | resolved |
| 0x1FC | **bit 19 = energy-efficiency turbo** (IPF DSP `decomp`, replaces the earlier unsourced "bit 20" claim); bits 0/1/23 seen in ThrottleStop (meaning not recorded) — bit 20 "OC lock" claim remains unsourced | | do not use |

## Measured on the reference machine (decodes) `measured`
Values: `examples/reference-285k/notes/platform.md` §4-5. What the readings established:
- `0x1AD` / `0x650`: one ratio byte per bucket, byte 0 = bucket 1; `0x1AE` / `0x651`: bucket core counts in the same byte order (285K: P 1..8, E
  1,2,3,4,6,8,12,16). `0x1AE`/`0x651` are readable on ARL-S.
- `0x610` decodes as the SDM layout (PL1 `[14:0]`, PL2 `[46:32]`, lock `[63]`) with `0x606` units (0.125 W, 2^-14 J, 2^-10 s on the 285K).
- `0x601` reads a design current (425 A on the 285K = PL4/TDC design current, **not** IccMax; IccMax comes from cmd `0x16`).
- `0x620` = ring min/max ratio, `0x621` = current ring ratio. `0x1A2` = Tjmax `[23:16]` (105 C), TCC offset, lock bit 31.
- **`0x198`: requested VID = `[47:32]`/8192 V; ratio = `[15:8]`.**
- CPU map and Windows LP numbering: `platform-arl-s.md` §2.

## Measured via XTU's level-4 log on Windows (S-03d…S-07, 2026-10-01) `measured win`
Raw tables: baseline words `src/arltuner/msr/measured_baseline.py`, write sequences `tests/controls/test_measured_apply.py`, values in notes §4.
- At stock (OC lock off, UVP off, no XTU tuning) `0x10 P1=0 P2=0` is `0x0` on every core except where the BIOS wrote an OC word; `0x14` = `0x8`
  (PerCore Voltage Enable already set). GT `0x10 P1=1` = `0xF8600000` = −59.6 mV matched the GT −0.060 V applied in XTU: **offset decode confirmed**.
- **`0x09 P1=17 P2=0` returns the fused V/F top point per core** (ratio `[7:0]`, voltage `[19:8]`): these — not the `0x10` word — are the "Voltage
  Override" values the XTU UI shows.
- **`0x1C[15:8]` is not a ratio cap on E cores** (reads the fused 46 on the 285K while XTU wrote 48 = `0x1C[7:0]` and the hardware accepted it).
- **Raise ordering and `0x28` (design rule, XTU's own ordering `[DECOMPILED]`):** raise => voltage first then frequency; lower => frequency first;
  `0x28` bounds the V target (XTU uses `raw-1`).
- **Logical-core numbering of mailbox affinity masks (Windows LP) ≠ Linux CPU number**; the Linux side addresses `/dev/cpu/N/msr` with the Linux CPU id.

**First real per-core write sequence (XTU, S-04/S-05, `measured win`):** `GET 0x14`→`GET 0x10`→`SET 0x11` (full word incl. V target = fused voltage)→`SET 0x1D` (= ratio)→`GET 0x26`→`SET 0x27` (= value read), all RC 0; read-back identical to the written word; XTU's revert word keeps the V target (`0x0005B439`, not the original `0x00000039`).

### Telemetry read allowlist (G-104, D-026) — `rdmsr` only, code `telemetry/msrio.py` `TELEMETRY_MSRS`; bits/decoding in `telemetry-sources.md`
0xE7 MPERF, 0xE8 APERF `[DOC SDM]` · 0xCE PLATFORM_INFO [15:8] `measured` · 0x198 PERF_STATUS [47:32] V, [15:8] ratio `measured` ·
0x19C / 0x1B1 THERM_STATUS [31] valid, [22:16] readout `[DOC SDM]`, ARL `[UNVERIFIED]` · 0x1A2 [23:16] Tjmax `measured` · 0x606 [12:8] energy unit
`measured` · 0x611 PKG_ENERGY [31:0] `[DOC SDM]` · 0x64F core / 0x6B0 **graphics** limit reasons: bit tables in "Limit reasons" below (decoder
`telemetry/limits.py`). Separate from the guard's `PLAIN_MSR_READABLE` (unchanged). 0x150 (mailbox) refused by `TelemetryMsr`.

### Limit reasons — 0x64F (core) and 0x6B0 (graphics) (G-215) `host lnx`, decoder `src/arltuner/telemetry/limits.py`
**Names (resolves a repo mislabel).** Linux `arch/x86/include/asm/msr-index.h`: `MSR_PERF_LIMIT_REASONS = 0x64F` (under "Hardware P state interface"),
`MSR_CORE_PERF_LIMIT_REASONS = 0x690`, `MSR_GFX_PERF_LIMIT_REASONS = 0x6B0`, `MSR_RING_PERF_LIMIT_REASONS = 0x6B1`
`[DOC https://raw.githubusercontent.com/torvalds/linux/master/arch/x86/include/asm/msr-index.h]`. The SDM names for the Skylake-and-later client
parts are `MSR_CORE_PERF_LIMIT_REASONS` = **0x64F**, `MSR_GRAPHICS_PERF_LIMIT_REASONS` = **0x6B0**, `MSR_RING_PERF_LIMIT_REASONS` = **0x6B1**, all "Package"
scope (SDM Vol 4, May 2018, as transcribed in EDK2 `MdePkg/Include/Register/Intel/Msr/SkylakeMsr.h`, `MSR_SKYLAKE_*_PERF_LIMIT_REASONS`)
`[DOC https://raw.githubusercontent.com/tianocore/edk2/master/MdePkg/Include/Register/Intel/Msr/SkylakeMsr.h]`. So **0x6B0 is the iGPU word, not "PKG"**;
the `PKG_PERF_LIMIT_REASONS` label in `msrio.py` / `telemetry-sources.md` / the "Limit reasons (raw 0x6B0)" tile is wrong (not edited by G-215; orchestrator to rename).
Linux's 0x690 is the Haswell-era address of the core word; it is EIO on this CPU `[MEASURED lnx 2026-09-29]` (telemetry-sources.md) — never read.
turbostat decodes the Haswell 0x690/0x6B0/0x6B1 trio (`print_perf_limit()`, gated by `plr_msrs` per platform; the entries seen were Haswell-class, ARL not
checked) and its core-word bit names differ from the Skylake 0x64F table — **do not** mix them.

**Layout (both words).** Bits 15:0 = live status ("frequency is reduced below the OS request because ..."); bits 31:16 = log of the same condition,
log bit = status bit + 16, set when the status asserted since last cleared, cleared by software writing 0 (R/W); bits 63:32 reserved. Decode reads only.

| Status bit | 0x64F core (Skylake+ SDM) | 0x6B0 graphics (SDM) |
|---|---|---|
| 0 | PROCHOT (external) | PROCHOT |
| 1 | Thermal event | Thermal |
| 4 | Residency state regulation | reserved |
| 5 | Running average thermal limit (RATL) | RATL |
| 6 | VR thermal alert | VR thermal alert |
| 7 | VR thermal design current (TDC) | VR TDC |
| 8 | Other (electrical or other constraints) | Other |
| 10 | Package/platform PL1 | PL1 |
| 11 | Package/platform PL2/PL3 | PL2 |
| 12 | Max turbo limit (multi-core turbo limits) | Inefficient operation |
| 13 | Turbo transition attenuation | reserved |
| 2, 3, 9, 14, 15 | reserved | 2, 3, 4, 9, 13, 14, 15 reserved |

Evidence: every row above is `[DOC SDM Vol 4 via edk2 SkylakeMsr.h]` (URL above); log bits identical positions +16. 0x6B1 (ring) has the same set as
0x6B0 without bit 12 — documented there, **not decoded**: 0x6B1 is not in `TELEMETRY_MSRS`.
**Arrow Lake specifics are `[UNVERIFIED]`:** the SDM tables above are the Skylake-and-later generation; no ARL-specific SDM table was found. What is
consistent on this CPU: `0x64F = 0x1DC21000` on CPUs 0,1,2,4,7,8,16 `[MEASURED lnx 2026-10-13 captures/lnx/perf-limit-reasons-20261013T155847Z.txt]` —
identical on P and E CPUs (package scope), every set bit (12, 17, 22, 23, 24, 26, 27, 28) lands on a documented position, none on a reserved one.
That proves scope and no-reserved-bits at that instant, not that the semantics are unchanged on ARL.

**Not decoded in code (named only here).** `[COMMUNITY https://github.com/cyring/CoreFreq/discussions/333]` a user reports 64F bit 14 as Thermal Velocity
Boost (via HWiNFO); the SDM calls bits 14–15 reserved → `[UNVERIFIED]`, reported by the decoder as `unknown_bits`. Haswell-era Intel-forum posts reading bit 14/15 as "max n-core turbo" concern the old 0x690 layout `[COMMUNITY https://community.intel.com/t5/Software-Tuning-Performance/MSR-CORE-PERF-LIMIT-REASONS-Set/m-p/1085194]`.
CoreFreq notes bit 24 "Other" was "Electrical Design Point" on Haswell. **Interpretation:** a live `max_turbo_limit` (bit 12) means the multi-core turbo
ratio limit is the cap — normal under load, not a fault. Log bits accumulate until cleared; this app never clears them.

## MMIO
MCHBAR 0x59A0 / 0x59A4 / 0x59B0 = PL1 / PL2 / PL4 (INT340X RAPL `ARL_S_THERMAL`, `processor_thermal_rapl.c`) `doc`, PL4 on ARL-S untested. IPF DSP `decomp`: also 0x59A8/0x59AC (PP0/PP1), 0x5938/0x5930/0x593C; **base = mapped BAR of the processor-thermal PCI device**; PCU mailbox 0x5810/0x5814/0x5818 (not the OC mailbox); ref-clock 0x5F60/0x5F64. Per-core HWP request `0x774` `[15:8]`/`[23:16]` fields are used per CPU ·
MCHBAR 0x5DA0 / 0x5DA4 = BIOS mailbox (0x37 core-ratio ext; SAGV B2P 0x22), *not* MSR 0x150 `doc` (FSP; Intel EDC 13th-gen datasheet).

## BIOS/FSP gating flags (boot-time; decide whether runtime writes are honoured)
`OcSupport` (default off) · `OcLock` (default 1) · `UnderVoltProtection` (default 1) · `TurboPowerLimitLock` · `PowerLimit3Lock` · `PowerLimit4Lock` · `TccOffsetLock` · `ConfigTdpLock` · `LockPTMregs` · `PmgCstCfgCtrlLock`.
Runtime mailbox writes need OC Lock off and UVP off (`platform-arl-s.md` §1); read `0x194[20]` / `0x195[1]` rather than trusting the BIOS menu.

**Linux measurements `measured lnx 2026-10-01`**: the `0x10` word persists over a warm reboot; `0x1C` answers on E CPUs and `[7:0]` follows a `0x1D` write; **`0x650` is runtime-writable and package-scoped** (written on CPU 0, read back identical on P and E CPUs); `0x1D` writes change the HWP highest performance the kernel sees (`energy_model` capacity rescaled). Values in notes §4.
