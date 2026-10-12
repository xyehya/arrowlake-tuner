# Memory: live DDR5 frequency and timings from MCHBAR (lnx, read-only)

Host: lnx. Scope: P (Arrow Lake-S; host bridge `8086:7d1a` on the reference 285K). The method follows Intel's public register map **Core Ultra 200S/200HX Series 2, document 835538 rev 001** [DOC Intel 835538]. These are the
*trained, in-use* values (what the memory controller runs), not SPD/XMP values. Read-only: PCI config read + exact-width `/dev/mem` reads; **no
PCI or MMIO write, ever; no MRC/PMA mailbox request**.

## Discovery
1. Host bridge `/sys/bus/pci/devices/0000:00:00.0`: `vendor:device` must be `8086:7d1a`, else unavailable ("unknown host bridge"). Whether other
   ARL-S dies (e.g. 6P+8E parts) use the same device id is [UNVERIFIED]; add ids only from a measurement or Intel's datasheet.
2. MCHBAR register = 8 bytes LE at PCI config offset **`0x48`** (`os.pread(config_fd, 8, 0x48)`, root). Bit 0 = enable (0 ⇒ unavailable
   "MCHBAR disabled"); base = `reg & ~0x7FFF`; base 0 ⇒ unavailable. Window size `0x20000`.
3. Reads: `os.open("/dev/mem", O_RDONLY|O_SYNC)`, `os.pread(fd, width, base + offset)` for a **compiled whitelist** of (offset, width) only; a
   short read ⇒ unavailable. Works on Fedora kernel 7.2 with `CONFIG_STRICT_DEVMEM=y` + `CONFIG_IO_STRICT_DEVMEM=y` (MCHBAR is MMIO, not claimed RAM)
   [MEASURED lnx 2026-10-11]. `/dev/mem` absent / EPERM ⇒ unavailable with the errno.

## Timing instances (MCHBAR-relative bases)
`mc0_channel0 0x0E000`, `mc0_channel1 0x0E800`, `mc0_broadcast 0x0F000`, `mc1_channel0 0x1E000`, `mc1_channel1 0x1E800`, `mc1_broadcast 0x1F000`.
Broadcast instances **read channel 0 of their controller** (835538 MC chapter) — aliases, not extra data. Per-instance whitelist (relative offset,
width): `(0x000,8) (0x00C,4) (0x010,4) (0x014,4) (0x018,4) (0x070,4) (0x088,8) (0x138,8) (0x4A0,8)`. Use the first instance that passes the
plausibility check; report how many instances agree (on the reference machine all 6 agree).

## Field layout (per instance; little-endian words)
| Register (rel) | Name | Fields |
|---|---|---|
| `+0x000` u64 | TC_PRE | tRP `[7:0]`, tRPab `[17:10]`, tRTP `[26:20]`, tWRPRE `[42:33]`, tRAS `[53:45]` |
| `+0x00C` u32 | TC_RDRD | SG `[6:0]`, DG `[14:8]`, SD `[23:16]`, DD `[31:24]` |
| `+0x010` u32 | TC_RDWR | SG `[7:0]`, DG `[15:8]`, SD `[23:16]`, DD `[31:24]` |
| `+0x014` u32 | TC_WRRD | SG `[8:0]`, DG `[17:9]`, SD `[24:18]`, DD `[31:25]` |
| `+0x018` u32 | TC_WRWR | SG `[6:0]`, DG `[14:8]`, SD `[22:16]`, DD `[31:24]` |
| `+0x070` u32 | TC_CAS | tCL `[22:16]`, tCWL `[31:24]` |
| `+0x088` u64 | SC_GS_CFG | gear `[31]` (1 ⇒ Gear 4, 0 ⇒ Gear 2), CMD stretch `[3]` (1 ⇒ 2N "2T", 0 ⇒ 1N) |
| `+0x138` u64 | TC_ACT | tFAW `[8:0]`, tRRD_L `[14:9]`, tRRD_S `[21:15]`, tRCD `[29:22]`, tRCDW `[39:32]` |
| `+0x4A0` u64 | TC_RFTP | tREFI `[17:0]`, tRFC `[30:18]`, tRFCpb `[50:40]`, tREFSBRD `[58:51]` |
Derived (DDR5 BL16): `tWR = tWRPRE − tCWL − 8`, `tWTP = tCWL + 8 + tWR`, `tRC = tRAS + tRP`.
Plausibility (reject an instance otherwise): `4 ≤ tCL ≤ 72`, `8 ≤ tRCD ≤ 59`, `10 ≤ tRP ≤ 76`, `28 ≤ tRAS ≤ 136`, `16 ≤ tFAW ≤ 88`, `tRFC > 0`.

## Frequency
- **MemSS trained QCLK** `MCHBAR+0x13D10` u32: ratio `[7:0]` × 33.333 MHz (100/3), Gear bit `[8]`. Data rate MT/s = 2 × QCLK MHz. [DOC 835538]
- Cross-check `MCHBAR+0x13D98` u32 (empirical): `((w>>12)&0xFF) × (4 if (w>>24)&1 else 2) × BCLK` MHz; `0xFFFFFFFF` = invalid. Report agreement.
- BCLK: `MCHBAR+0x5F60` u64, CPU BCLK kHz `[63:32]`, SoC BCLK kHz `[31:0]` [DOC 835538].
- Max theoretical bandwidth `MCHBAR+0x13D08` `[28:9]` MB/s (configured ceiling, not traffic).

## DIMMs (no MCHBAR)
- SMBIOS type 17 (size, part number, manufacturer, configured speed/voltage, rank): `dmidecode -t 17` (root) or parse
  `/sys/firmware/dmi/entries/17-*/raw` (root, stdlib, preferred: no external binary).
- DIMM temperatures: hwmon `spd5118` devices (`temp1_input`, m°C) (present with DDR5 DIMMs on the reference machine) [MEASURED lnx 2026-10-11].

## Fixture
The reference machine's live values (DDR5-7200, Gear 2, 36-44-44-96 ...), which the tests encode into synthetic words with the layout above:
`examples/reference-285k/notes/platform.md` §8 [MEASURED lnx 2026-10-11].
