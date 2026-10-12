# Registers, MSRs, mailbox commands — single source of truth

> Carried over 2026-10-08 from the project's earlier research phase. Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here name notes of that phase, which are not part of this repository; the facts are kept with their evidence tags.

Status legend: `decomp` = read from Intel's own XTU code (worker-produced in T-001/T-002, orchestrator spot-checked; usable as a constant only after a MEASURED readback on this CPU) ·
`doc` = primary public source · `lead` = COMMUNITY/UNVERIFIED (never a constant) · `seen` = observed on this machine · `measured` = read on this machine by the operator's own read-only monitoring tool (capture 2026-09-29) · `trace` = recorded from OCCT/HWiNFO mailbox traces on this machine (corroboration).
**Nothing here is on any write allowlist.** An entry joins the allowlist only via a DECISIONS.md entry plus a MEASURED read on this CPU.
Evidence trails: `win-xtu-decompile-mailbox.md` (T-001), `win-xtu-decompile-controls.md` (T-002), `win-xtu-inventory.md`, `win-xtu-ui-catalogue.md`, `lnx-interfaces.md`.

## OC mailbox — request model (from XTU)
XTU builds a request tuple `(cmd u8, P1 u8, P2 u8, data u32, scope)` and hands it to native `XtuProvider.dll` as an IPF JSON `MailboxAction` on `Platform.SOC.Overclocking`. `decomp`
- **Scope:** package (no core) or core-scoped: logical core `L` (group `L/64`, affinity mask bit `L%64`). **The core is chosen only by the scope, not by P1/P2.** P and E cores use the same field ids and the same request (P1=0, P2=0).
  There is no E-cluster/module address in XTU. "Logical core index" = position in IPF `Platform.SOC.Info.Core`; mapping to Linux CPU numbers **unknown** (T-003/T-005).
- **Wire packing — decoded independently from XTU's IPF driver `ipf_cpu.sys` (T-005) and from ThrottleStop 9.7.3 (T-006), both `decomp`; matches FSP/V0LTpwn/kernel `doc`. Not yet MEASURED on this CPU.**
  WRMSR **MSR 0x150** with the 64-bit value `1<<63 | P2<<48 | P1<<40 | cmd<<32 | data` (i.e. `hi = 0x80000000 | P2<<16 | P1<<8 | cmd`, `lo = data`). Completion: poll RDMSR 0x150 until bit 63 clears; status = `hi & 0x1F` (0 = OK); result data in `lo`.
  ThrottleStop's driver has **no CPU selection**: RDMSR/WRMSR run on the calling thread's CPU, so the caller pins (`SetThreadAffinityMask`). Its write sites mostly fire-and-forget (no poll, no timeout, no readback) — our code must poll with a timeout and read back.
  Example (legacy plane word, CPU Core, −50 mV, adaptive): `0x80000011_F9A00000`. Driver IOCTLs `0x80006448` (RDMSR) / `0x8000644C` (WRMSR) are ThrottleStop-specific.
  **XTU/IPF sequence (T-005):** `ipf_cpu.sys` pins the request's `(group, mask)` CPU (`KeSetSystemGroupAffinityThread`; package scope = whichever CPU), reads MSR 0x150 until bit 63 = 0 (10 ms sleeps, ≈10 s cap; timeout ⇒ **abort without writing**), WRMSR, polls again;
  response data `[31:0]`, completion code `[39:32]`; no retry, no lock below XTU's own mutex, no rollback; the driver accepts only cmds 0x01–0x09 and 0x10–0x29. There is **no MMIO/PCI path** for the OC mailbox: every request, *including reads*, is a WRMSR to 0x150
  (hardware-write tier). Example: `SET 0x11 P1=0 P2=0 data=D` → `0x8000001100000000 | D` (`D=0x0004A02D` → `0x80000011_0004A02D`). "Logical core L" = Windows logical processor L (group L/64, bit L%64); Linux CPU numbering vs this — **measure in T-003**.
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
Checks against XTU's UI `seen`: E-Core 12 override 1.137 V = 0x48C; P0 1.426 V ≈ 0x5B4; P4 1.316 V ≈ 0x544; logged `SET 0x11 0x0004A02D` = ratio 45, V target 1.156 V, offset 0, adaptive.
**Third confirmation `trace` (OCCT/HWiNFO mailbox traces on this machine):** `0x03400030` = point 4 / 48×, `0x03400039` = point 8 / 57× with offset raw 26 (+25.39 mV) and **V-target field 0** (an offset-only write, like ThrottleStop's V/F-point writes that send "offset bits only"; corrected by T-010 — bits [19:8] of `0x03400030` are 0, not 0x400) — **per-V/F-point offsets exist and were written (P2 = point index)**;
ring (domain 2) 40× target `0x4C8` = 1195.31 mV; NGU (domain 7) 34× `0x4BD` = 1184.57 mV; `cmd 0x1C` high byte = fused max ratio (E 46×, P 55×/57×); `cmd 0x16` IccMax 425 A before / 400 A after a CMOS clear (GT VR 45 A); `cmd 0x04` raw `0x84002000`; domain 6 = memory subsystem (ratios 0,32,84,96).
**XTU's write procedure** (a description of behaviour, not a recipe): (1) package scope: read cmd 0x14 (P1=0,P2=0); if bit 3 ("PerCore Voltage Enable") is 0, write cmd 0x15 with `old|8`; restore afterwards. (2) core scope: read 0x10 → patch only the wanted bit-slices →
write whole word with 0x11 (read-modify-write; for P2≠0 the read word is masked `&0xFFFFFF00`). (3) a ratio write also writes cmd 0x1D (P1=0,P2=0) with data = ratio (favored-core word, no RMW). (4) cores whose merged words are equal share one action. (5) re-read all per-core fields.
**No external oracle** exists for XTU's "PerCore Voltage Enable" path: ThrottleStop 9.7.3 never touches cmd 0x15 bit 3 and has no per-core *voltage* write (its "Per Core" = favored-core ratio via 0x1C/0x1D pinned per CPU).
Ordering: raising performance → voltage first, lowering → frequency first; markers `0xE0000002/4` bracket a batch. No hardware rollback on failure (watchdog + persistence only). `decomp` (T-002 §Apply flow)

## Voltage semantics (ARL-S) — see `arl-voltage-topology.md`
Adaptive voltage `[19:8]` (mode `[20]`=0) sets the **OC-ratio point** only (P-core point 9, E-core point 7); fused lower points accept **offsets only** `[USER]` `[COMMUNITY]`; `[31:21]` offset with P2=0 = whole-curve offset `[UNVERIFIED]`;
per-point offsets = P2 1..15 (hidden by XTU on ARL-S; probe). E-core DLVR is per **cluster of 4**. Default voltage ceiling = top fused VID + 200 mV (285K: P 1.581 V, E 1.270 V; absolute max 1.681 / 1.370 V) `[COMMUNITY]`.

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
**Limits in the tool (G-137, D-039):** ratio ceiling = policy value clamped by `0x01 P1=0 [7:0]` of a CPU of that kind (needs `[8]` set; P reads `0x778` = 120x `[MEASURED win 2026-10-01]`; XTU's decompile names no separate E domain for `0x01`, field ids 19-21 = "IA" for all core CPUs `[DECOMPILED win-xtu-decompile-controls.md]`, so the E value is read with P1=0 pinned to an E CPU, whether it differs from P is `[UNVERIFIED]`); V target ceiling = policy value clamped by `0x28 P1=0 [11:0] - 1 LSB` of a CPU of that kind (P 0x681 -> 1625 mV, E 0x56D -> 1355 mV `[MEASURED win 2026-10-01]`); offset -1000..+999 mV (field encoding). `0x1C` answers on E CPUs too (E `0x2E30` = [7:0] 48, [15:8] fused 46 `[MEASURED win 2026-10-01]`, `lnx 2026-10-11`): the `"auto"` ratio ceiling uses the **fused byte `0x1C[15:8]`** only (highest over the CPUs of the kind, + 3): P 0x3939 / 0x3737 -> 57 / 55 so P 57 + 3 = 60, E 0x2E30 -> 46 so E 46 + 3 = 49. `[7:0]` is the favored/current ratio (E 48, follows the BIOS turbo setting and a `0x1D` write), so it would ratchet; `[15:8]` is the fused value on both kinds in `msr/measured_baseline.FAVORED_WORD` `[MEASURED win 2026-10-01]`.
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
Explains the XTU log (`win-xtu-inventory.md`): `SET 0x11 … RC 3` = ratio 45 above the limit; `SET 0x15 data 8 RC 1` = per-core switch refused (OCLocked; now off in BIOS); `GET 0x01 P1=3 RC 5` = GT-unslice not overclockable (probed at every start).

## Not the mailbox
Turbo ratio limits (P active-core table, E ranges) go through IPF properties `Performance.TurboRatio/TrlRange/EfficientCoreTurboRatio/EfficientCoreTrlRange` → MSR 0x1AD/0x1AE/0x650/0x651 (whole 64-bit, written on CPU 0) `decomp` (T-005 §6a; stale "unknown" wording fixed 2026-10-01, access-manifest C-10). Power limits, TCC, uncore ratio: IPF/MSR/MMIO (below).
ARL-S constants in XTU `decomp`: 8 P-cores (max 32 E capacity, 16 present), 8 turbo buckets, 2 favored cores, per-core ratios/voltage/favored/granular = yes; V/F-curve, per-point offset, FIVR, AVX2 scale = no.

## MSRs
| MSR | Use | Source | Status / note |
|---|---|---|---|
| 0x150 | OC mailbox (wire format above; no MMIO path — every request incl. reads is a WRMSR) | V0LTpwn; kernel; XTU/IPF + ThrottleStop `decomp` | doc; runtime writes gated by BIOS (OcSupport/OcLock/UVP — here OFF) |
| 0x1AD | turbo ratio limit: 8 bytes = active-core buckets 1..8 | turbostat | doc; XTU/IPF reads and writes the whole 64 bits of 0x1AD/0x1AE/0x650/0x651 (writes on CPU 0) `decomp`; still measure by read |
| 0x650 | secondary turbo ratio limit (E-cores) | turbostat | doc; **resolved `measured`:** 0x1AE/0x651 are readable on ARL-S (P bucket core counts 1..8; E bucket core counts 1,2,3,4,6,8,12,16) — the earlier "ARL doesn't use 0x1AE" note was wrong |
| 0x770 / 0x771 / 0x774 | HWP enable / capabilities / request | `intel_pstate` | doc; lower-only |
| 0x620 / 0x621 | uncore ratio limit ([6:0] max, [14:8] min) / status | `intel_uncore_frequency` | doc; ring vs NGU unknown. XTU/IPF: 0x620 [6:0] max / [14:8] min, 0x621 [6:0] current ratio `decomp`; XTU also sets `UncoreMax/MinRatio` when the ring ratio changes |
| 0x610 | PKG_POWER_LIMIT (PL1/PL2; bit 63 lock) | kernel RAPL | doc; XTU/IPF prefers MMIO 0x59A0/0x59A4 with this MSR as fallback `decomp` |
| 0x601 | PL4 | kernel RAPL | doc; ARL-S = `rapl_defaults_core` → no PL4 via MSR per kernel, but IPF DSP reads PL4 from MSR 0x601 `[14:0]` `decomp` (PL4 path 0x601 vs MMIO 0x59B0 unresolved) |
| 0x1A2 | TEMPERATURE_TARGET; TCC offset [29:24]; lock bit 31 | `intel_tcc.c` | doc; IPF DSP uses `[30:24]` `decomp` — field width unresolved |
| 0xE2 | PKG_CST_CONFIG_CONTROL; CFG lock bit 15 | coreboot | doc |
| 0x10A | bit 23 = OC status (IPF DSP) | XTU/IPF `decomp` | read-only candidate |
| 0xCE | PLATFORM_INFO: bit 24 = OC available (ThrottleStop shows "Not Available" when 0), bit 28 = programmable turbo ratio limits | ThrottleStop `decomp` (+Intel SDM) | **read-only candidate** for T-003 |
| 0x194 | bit 20 = **OC lock**, [19:17] OC bins (IPF `decomp`) (ThrottleStop skips all FIVR/TRL writes and shows "Locked") | ThrottleStop `decomp` | **read-only candidate** — verifies the BIOS "OC Lock off" |
| 0x195 | bit 1 = **Undervolt Protection** indicator | ThrottleStop `decomp` | **read-only candidate** — verifies the BIOS "UVP off" |
| 0x1AE / 0x651 | probed (with 0x650) by ThrottleStop on ARL; if they answer it enables turbo-ratio write paths (writes 0x1AD/0x1AE/0x650/0x651). MEASURED 2026-09-29: readable; 0x1AE = P bucket core counts 1..8, 0x651 = E bucket core counts 1,2,3,4,6,8,12,16 | `measured` | resolved |
| 0x1FC | **bit 19 = energy-efficiency turbo** (IPF DSP `decomp`, replaces the earlier unsourced "bit 20" claim); bits 0/1/23 seen in ThrottleStop (meaning not recorded) — bit 20 "OC lock" claim remains unsourced | | do not use |

## Measured on this machine (read-only capture `2026-09-29-bios340`, BIOS 3.40) `measured`
0x1AD = `0x3737373737373939` → P ratios by bucket 57,57,55,55,55,55,55,55 (LSB first; matches XTU) · 0x1AE = `0x0807060504030201` · 0x650 = `0x3030303030303030` (E 48×8) · 0x651 = `0x100c080604030201` ·
0x610 = `0x0042893800e38938` → PL1 295 W/224 s, PL2 295 W · 0x606 units 0.125 W, 2^-14 J, 2^-10 s · 0x614 TDP 125 W · **0x601 = 0xd48 → 425 A (PL4/TDC design current, not IccMax)** · 0x620 = `0x828` (ring 8…40×), 0x621 cur 40× ·
0x1A2 = `0x80691400` (Tjmax 105, TCC offset 0, bit 31 set) · 0xE2 = `0x74008008` · 0x770 = 1 · **0x198: requested VID = `[47:32]`/8192 V; ratio = `[15:8]`**.
**CPU map:** Linux CPUs 0–7 = P, 8–23 = E (clusters 8–11, 12–15, 16–19, 20–23); favored P = CPU 0, 3; XTU UI P-Core N = Linux CPU N (XTU E-Core k = CPU 8+k inferred); **the Windows logical-processor number used in mailbox masks/XTU logs differs: P = LP {0,1,10,11,12,13,22,23}, E = LP {2–5,6–9,14–17,18–21} (measured, see the XTU level-4 section below)** (earlier inference: E-cluster VIDs 1.1575 V for CPUs 8–11 and 1.1375 V for 12–23 equal XTU's logged `0x4A0`/`0x48C` words).

## Measured via XTU's level-4 log on Windows (S-03d, 2026-10-01) `measured win` — full table `win-xtu-live-trace.md`
Baseline (BIOS 3.40, OC lock off, UVP off, no XTU tuning applied): `0x14` = `0x8`; **`0x10 P1=0 P2=0` per core = `0x39` on LP 0, `0x0` on all other 23 LPs** (no per-core voltage target/offset set); `0x10 P1=1` (GT slice) = `0xF8600000` = **−59.6 mV offset** (matches the GT −0.060 V applied in XTU: offset decode confirmed); ring `0x28` (40×), NGU `0x20` (32×); `0x16` P1=0/1 = 400 A / 50 A; `0x04` = `0x84002000` (unexplained by both layouts); `0x1C` favored: LP 0 and LP 11 = 57.
**`0x09 P1=17 P2=0` returns the fused V/F point per core** (P `0x5B439` … 1.426 V / 1.316 V …, E `0x4A02E`/`0x48C2E` = 1.156 V / 1.137 V): these — not the `0x10` word — are the "Voltage Override" values the XTU UI shows (the earlier "Checks against XTU's UI" line above is therefore about `0x09`).
**`0x1C[15:8]` is not a ratio cap on E cores** (reads 46 = fused, while XTU wrote ratio 48 = `0x1C[7:0]` and the hardware accepted it).
**Raise ordering and `0x28` (design rule, XTU's own ordering `[DECOMPILED]`):** raise => voltage first then frequency; lower => frequency first; `0x28` bounds the V target (XTU uses `raw-1`: E `0x56D` => ~1.355 V, P `0x681` => ~1.625 V on this CPU). Whether the hardware accepts a ratio/voltage *raise* is still **unverified** (XTU only wrote stock-equivalent words in S-04...S-07).
**Logical-core numbering of mailbox affinity masks (Windows LP) ≠ Linux CPU number:** P = LP {0,1,10,11,12,13,22,23}, E = LP {2–5, 6–9, 14–17, 18–21}; best-supported order-preserving map to Linux in the live-trace note (the Linux side must address `/dev/cpu/N/msr` with the **Linux** CPU id).

**First real per-core write sequence (XTU, S-04/S-05, Windows, `measured win`, LP 0, −5 mV then back):** `GET 0x14`→`GET 0x10`→`SET 0x11` (full word incl. V target = fused voltage, e.g. `0xFF65B439`)→`SET 0x1D` (= ratio)→`GET 0x26`→`SET 0x27` (= value read), all RC 0; read-back identical to the written word; revert word `0x0005B439` ≠ original `0x00000039` (V target stays set). Details and caveats: `win-xtu-live-trace.md` S-04/S-05. Not yet on any allowlist.

### Telemetry read allowlist (G-104, D-026) — `rdmsr` only, code `telemetry/msrio.py` `TELEMETRY_MSRS`; bits/decoding in `telemetry-sources.md`
0xE7 MPERF, 0xE8 APERF `[DOC SDM]` · 0xCE PLATFORM_INFO [15:8] `measured` · 0x198 PERF_STATUS [47:32] V, [15:8] ratio `measured` ·
0x19C / 0x1B1 THERM_STATUS [31] valid, [22:16] readout `[DOC SDM]`, ARL `[UNVERIFIED]` · 0x1A2 [23:16] Tjmax `measured` · 0x606 [12:8] energy unit
`measured` · 0x611 PKG_ENERGY [31:0] `[DOC SDM]` · 0x64F, 0x6B0 limit reasons: raw only, decode open. Separate from the guard's `PLAIN_MSR_READABLE`
(unchanged). 0x150 (mailbox) refused by `TelemetryMsr`.

## MMIO
MCHBAR 0x59A0 / 0x59A4 / 0x59B0 = PL1 / PL2 / PL4 (INT340X RAPL `ARL_S_THERMAL`, `processor_thermal_rapl.c`) `doc`, PL4 on ARL-S untested. IPF DSP `decomp`: also 0x59A8/0x59AC (PP0/PP1), 0x5938/0x5930/0x593C; **base = mapped BAR of the processor-thermal PCI device**; PCU mailbox 0x5810/0x5814/0x5818 (not the OC mailbox); ref-clock 0x5F60/0x5F64. Per-core HWP request `0x774` `[15:8]`/`[23:16]` fields are used per CPU ·
MCHBAR 0x5DA0 / 0x5DA4 = BIOS mailbox (0x37 core-ratio ext; SAGV B2P 0x22), *not* MSR 0x150 `doc` (FSP; Intel EDC 13th-gen datasheet).

## BIOS/FSP gating flags (boot-time; decide whether runtime writes are honoured)
`OcSupport` (default off) · `OcLock` (default 1) · `UnderVoltProtection` (default 1) · `TurboPowerLimitLock` · `PowerLimit3Lock` · `PowerLimit4Lock` · `TccOffsetLock` · `ConfigTdpLock` · `LockPTMregs` · `PmgCstCfgCtrlLock`.
**This machine (user-reported): UVP off, OC Lock off, Secure Boot off.** Silicon per-core/cluster FSP fields: see `win-recon-methods.md`.

**Linux measurements `measured lnx 2026-10-01`** (`lnx-first-read.md`): `0x10` CPU 4 = `0x0`, CPU 0 = `0x39`, CPU 8 = `0x4A931` (XTU word persisted over a warm reboot), CPUs 12–23 `0x49A31`; `0x1C` CPU 8 = `0x2E30` → after `0x1D`=49 `0x2E31`; `0x0CE` = `0x08040838F1812500` ([28]=1 programmable TRL, [24]=1, [15:8]=37); `0x194` = `0xE0000` ([20]=0 OC unlocked); **`0x650` is runtime-writable and package-scoped** (`wrmsr` on CPU 0 = `0x3131313131313131`, read back identical on CPUs 0/8/20/23); `0x1D` writes change the HWP highest performance the kernel sees (`energy_model` capacity 756 → 768).
