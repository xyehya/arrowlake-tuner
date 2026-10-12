# Reference 285K: chip, board and stock values

> **One sample, not a spec.** Everything here was read on ONE Core Ultra 9 285K on ONE ASRock Z890 Steel Legend WiFi (BIOS 3.40) in
> September-October 2026. Fused words, VIDs, point voltages and limits differ per chip; never use these numbers as defaults or constants for
> another CPU. The general rules these values illustrate are in `docs/knowledge/` (registers.md, platform-arl-s.md, favored-cores.md).

Tags: `[MEASURED host date]` = read on this machine; `[USER]` = reported by the operator.

## 1. Machine and BIOS
| Item | Value | Evidence |
|---|---|---|
| CPU | Core Ultra 9 285K (Arrow Lake-S), 8P + 16E, no SMT, 24 threads, CPUID 0xC0662 | [MEASURED win 2026-09-30] |
| Board / BIOS | ASRock Z890 Steel Legend WiFi, AMI BIOS 3.40 (2026-09-15) | same |
| BIOS gates | Undervolt Protection OFF, OC Lock OFF (why runtime mailbox writes work); Secure Boot OFF | [USER 2026-09-30] |
| BIOS OC (state the accepted tuning was validated on) | VccIA offset -100 mV, LLC Level 6, ring 40x with -16 mV on its 39x/40x points, DDR5-7200, NGU/D2D 32x, VccSA 1.185 V fixed, TVB voltage optimizations OFF, Intel default power profile | [USER] |
| BIOS P-core V/F offset | the per-core mode programs ONE list onto P-core 0 only; cores 1-7 stay stock; all-core scope had no effect. Cleared since 2026-10-02 | [MEASURED lnx 2026-10-02] |
| E-cores in BIOS | no voltage / V/F options | [USER] |
| Power limits | PL1 = PL2 = 250 W (D-024; `0x610` = `0x004287d000df87d0`: PL1 250 W, clamp, tau 56 s, PL2 250 W, lock 0) [MEASURED lnx 2026-10-08]. Earlier records said 295/295 W (`0x610` = `0x0042893800e38938` on 2026-09-29) | |
| Linux host | Fedora 44, kernel 7.2.x; `intel-rapl-mmio:0` present but disabled (limits 0, peak_power 425 W) | [MEASURED lnx 2026-10-08] |
| Health baseline | taint 12288 (bits 12 `O` + 13 `E`, NVIDIA module) -> 12292 after the first MSR write of a boot; 35 PCI devices; 187 modules | [MEASURED lnx 2026-10-01/07] |
| Windows host | Windows 11 26200/26300; WHEA-Logger baseline 38 events (newest 2026-09-30), unchanged through all 2026-10 sessions | [MEASURED win] |

## 2. Topology and numbering
| Item | Value |
|---|---|
| Linux CPUs | P = 0-7; E = 8-23; E clusters 8-11, 12-15, 16-19, 20-23 [MEASURED lnx 2026-09-29] |
| Favored P-cores | CPU 0 and 3: `acpi_cppc/highest_perf` 92 vs 88; `0x1C` = `0x3939` (57x) vs `0x3737` (55x); E `0x2E30` (fused byte 46) |
| XTU naming | "P-Core N" = Linux CPU N; "E-Core k" = Linux CPU 8+k [MEASURED win 2026-10-01] |
| Windows logical processors | P = LP {0,1,10,11,12,13,22,23}; E = LP {2-9, 14-21}; clusters LP 2-5 / 6-9 / 14-17 / 18-21 = Linux 8-11 / 12-15 / 16-19 / 20-23; order-preserving within each kind [MEASURED win 2026-10-01] |
| Identity words (fused `0x09 P1=0x11` of P-cores 0-7, all distinct) | `0x5B439 0x53839 0x57439 0x59439 0x54439 0x56C39 0x56039 0x58039` (the Windows port used them as a fixed identity check until G-138) |
| sysfs `coretemp` labels | "Package id 0" + 24 "Core N", N = `core_id` (0,4,..,44 / 8..15 / 32..39 ...) [MEASURED lnx 2026-10-11] |

## 3. Point tables of this SKU [MEASURED lnx 2026-10-02]
| Domain | Fused point ratios (P2 = 1..n) | Beyond the table |
|---|---|---|
| P-core (all 8 identical) | 8, 20, 30, 48, 53, 55, 56, 57 (n = 8) | P2 = 9 -> rc 0x11 at stock |
| E-cluster | 8, 15, 23, 30, 40, 46 (n = 6) | P2 = 7 -> rc 0x11 at stock; P2 = 7 = 48x after an E OC ratio 48 [MEASURED lnx 2026-10-11] |
| Ring | 8, 14, 20, 28, 34, 37, 38, 39 (n = 8) | P2 = 9 (40x) exists only while the ring OC ratio is 40x |
Cold-boot point words: P `08 14 1E 30 35 37 38 39`; E `08 0F 17 1E 28 2E`; OC words 0 except CPU 0 = `0x39` (written by the BIOS at every boot).
cpufreq caps on this chip (kernel 7.2.9): P 42/47/52x and E 13/18/21/23/26/29/31/34/39x unreachable (`docs/knowledge/power-profile-capping.md` §6).

## 4. Fused top points (cmd `0x09 P1=0x11 P2=0`) and other stock words [MEASURED win 2026-10-01 / lnx 2026-10-02]
`FUSED_POINT` in `src/arltuner/msr/measured_baseline.py` (a simulator fixture).
| CPU | word | ratio | V | CPU | word | V |
|---|---|---|---|---|---|---|
| 0 | 0x5B439 | 57 | 1.4258 | 4 | 0x54439 | 1.3164 |
| 1 | 0x53839 | 57 | 1.3047 | 5 | 0x56C39 | 1.3555 |
| 2 | 0x57439 | 57 | 1.3633 | 6 | 0x56039 | 1.3438 |
| 3 | 0x59439 | 57 | 1.3945 | 7 | 0x58039 | 1.3750 |
| 8-11 | 0x4A02E | 46 | 1.1563 | 12-23 | 0x48C2E | 1.1367 |
- XTU UI "Voltage Override" shows these (E-Core 12 1.137 V = 0x48C; P0 1.426 V; P4 1.316 V); XTU log `SET 0x11 0x0004A02D` = ratio 45, 1.156 V, offset 0.
- `0x10 P2=0` per core: `0x39` on CPU 0, `0x0` elsewhere; `0x14` = `0x8`; `0x26` = 3; `0x28` P `0x681` (~1.625 V), E `0x56D` (~1.355 V);
  `0x01 P1=0` on a P CPU = `0x778` (fused max OC ratio 120x); `0x02` per-active-core fused max 57,57,54,54.
- GT `0x10 P1=1` = `0xF8600000` (-59.6 mV, an XTU-applied GT offset); ring `0x10 P1=2` = 40x, NGU `P1=7` = 32x; `0x16` IccMax core 400 A / GT 50 A
  (core 425 A and GT 45 A before a CMOS clear in OCCT/HWiNFO traces); `0x04` = `0x84002000` (decodes with neither known layout); `0x05` BCLK
  monitor ~100000; `0x24` TVB cutoff 70 C; `0x1C` LP 0 / LP 11 = 57.
- OCCT/HWiNFO trace words: ring 40x target `0x4C8` = 1195.31 mV; NGU 34x `0x4BD` = 1184.57 mV; domain 6 ratios 0, 32, 84, 96.
- XTU level-4 trace (Windows, S-03d...S-07): first per-core write on LP 0 = `GET 0x14` -> `GET 0x10` -> `SET 0x11 0xFF65B439` (-5 mV, V target =
  fused) -> `SET 0x1D` -> `GET 0x26` -> `SET 0x27`, all RC 0; revert `0x0005B439` (not the original `0x39`). These tables are transcribed in
  `src/arltuner/msr/measured_baseline.py` and `tests/fixtures/win_reference.py`.
- Linux 2026-10-01 reads: `0x10` CPU 4 `0x0`, CPU 0 `0x39`, CPU 8 `0x4A931` (XTU's E 49x word, persisted over a warm reboot), CPUs 12-23 `0x49A31`;
  `0x1C` CPU 8 `0x2E30` -> `0x2E31` after `0x1D` = 49.

## 5. Plain MSRs at stock (BIOS 3.40) [MEASURED lnx 2026-09-29, same on 2026-10-01]
| MSR | Value | Decode |
|---|---|---|
| 0x1AD | `0x3737373737373939` | P bucket ratios 57,57,55,55,55,55,55,55 |
| 0x1AE | `0x0807060504030201` | P bucket counts 1..8 |
| 0x650 | `0x3030303030303030` | E 48 x8 |
| 0x651 | `0x100c080604030201` | E bucket counts 1,2,3,4,6,8,12,16 |
| 0x606 | | units 0.125 W, 2^-14 J, 2^-10 s; 0x614 TDP 125 W |
| 0x601 | `0xd48` | 425 A (PL4/TDC design current, not IccMax) |
| 0x620 / 0x621 | `0x828` / 40 | ring 8...40x / current 40x |
| 0x1A2 | `0x80691400` | Tjmax 105 C, TCC offset 0, bit 31 set |
| 0xE2 | `0x74008008` | locked |
| 0xCE | `0x08040838F1812500` | [28] = 1, [24] = 1, [15:8] = 37 (TSC 3700 MHz) |
| 0x194 | `0xE0000` | [20] = 0 (OC unlocked) |
| 0x770 | 1 | HWP enabled |

## 6. Stock V/F curve (requested VID, V) [MEASURED lnx 2026-10-11 18:17 UTC, curve `20261011T181718Z-y4c7`]
Cold boot, runtime offsets 0, BIOS LLC 6 / VccIA -100 mV; single-unit load, temps 28-80 C; `mismatch` = never settled at the target ratio.
| Unit | 8x | 20x | 30x | 48x | 53x | 55x | 56x | 57x |
|---|---|---|---|---|---|---|---|---|
| P0 (favored) | 0.693 | 0.721 | 0.811 | 1.041 | 1.216 | 1.299 | 1.386 | mismatch |
| P1 | 0.691 | 0.723 | 0.806 | 1.041 | 1.221 | 1.306 | - | - |
| P2 | 0.691 | 0.721 | 0.821 | 1.073 | 1.271 | 1.366 | - | - |
| P3 (favored) | 0.691 | 0.721 | 0.821 | 1.061 | 1.221 | 1.299 | 1.359 | mismatch |
| P4 | 0.688 | 0.713 | 0.804 | 1.033 | 1.226 | 1.319 | - | - |
| P5 | 0.690 | 0.721 | 0.811 | 1.054 | 1.261 | 1.359 | - | - |
| P6 | 0.691 | 0.721 | 0.805 | 1.033 | 1.249 | 1.346 | - | - |
| P7 | 0.690 | 0.719 | 0.811 | 1.041 | 1.271 | 1.376 | - | - |
E clusters (8x 15x 23x 30x 40x 46x): EC0 0.775 0.775 mismatch 0.775 0.970 1.157 · EC1 0.775 0.775 mismatch 0.775 0.970 1.137 · EC2/EC3 0.775 0.775
mismatch 0.775 0.963 1.137 (0.775 V = the E VID floor up to 30x). Re-measure 19:06 UTC (`20261011T190606Z-d53n`): P0 57x = 1.429 V (fused 1.4258 V);
P3 56x 1.359 V. Dense curve 19:08 UTC (`20261011T190830Z-rj7b`): 44-47 ratios per P-core, 32 per E-cluster; 54x within +-1.5 mV of the 53/55x midpoint
on every P-core (P2 1.3187 vs 1.31875 V).
Earlier stock VIDs, CPU 0 with a BIOS offset at the time [MEASURED lnx 2026-10-02]: CPU 4 48/53/55x 1.0334/1.2262/1.3187 V; CPU 0 57x 1.4288 V.

## 7. Voltage rails and board sensors
- Board Vcore (VccIA, NCT6686D `in0`/`VIN16`, 16 mV step): idle 0.848 V (max VID 0.741 V); all-core stress-ng 1.456-1.488 V untuned (max P VID
  1.27-1.32 V); `VIN6` (VccSA) 1.184 V vs BIOS 1.185 V [MEASURED lnx 2026-10-11].
- Vcore at P caps (BIOS VccIA -75/-100, LLC 6/7): 53x 1.312-1.328 V, 54x 1.344-1.360 V, 55x on 8P or 295 W all-core 1.392 V; ring 40x UncVID ~1.201 V;
  below ~53x allowed, Vcore ~1.30 V; ring 1.2x V target once raised Vcore under all-core load (1.408 -> 1.376 V after the ring change)
  [MEASURED lnx 2026-10-02/03].
- LLC (ASRock): Level 6 -> 7 gave ~8 mV more droop at ~67 A (8P 53x cap), below the 16 mV sensor step [MEASURED lnx 2026-10-03, weak]; consistent with
  the older-ASRock-board lead "higher number = more droop" [COMMUNITY forum.asrock.com/topic7559.html]. Reverted to 6 (D-019).
- PL 320 W ran to 102 C; 295 W peaked ~92 C [MEASURED lnx 2026-10-02]. Windows at LLC 7 matched Linux: OCCT SSE 295 W, Vcore 1.34 V (Linux 1.344)
  [MEASURED win 2026-10-03].

## 8. Memory [MEASURED lnx 2026-10-03 / 2026-10-11, BIOS DDR5-7200, NGU/D2D 32x, VccSA 1.185 V]
- MCHBAR fixture (the values `tests/telemetry/test_mem.py` and `tests/web/fake_v3.py` encode): register base `0xFEDC0000`; 6/6 timing instances plausible
  and equal. QCLK ratio 108 -> 3600.0 MHz, DDR5-7200, Gear 2, CMD 2N; 0x13D98 cross-check agrees (3600 MHz, component 18 x 2 x BCLK). Primary
  36-44-44-96, tRC 140, tRFC 574, tFAW 32, tCWL 34, tRTP 23, tWR 110 (tWRPRE 152), tRRD_L 12, tRRD_S 8, tREFI 32767, tRCDW 44, tRPab 44, tRFCpb 467.
  RDRD SG/DG/SD/DD 18/8/16/16 · WRWR 72/8/18/18 · RDWR 23/23/23/23 · WRRD 68/48/13/13. BCLK CPU 100.000 / SoC 100.004 MHz. Max theoretical bandwidth
  115200 MB/s. `spd5118` DIMM sensors present (hwmon5, hwmon6). Raw words were not captured.
- Bandwidth (stress-ng --stream, perf `uncore_imc`): 8 P-cores 95 GB/s, 16 E 99, all 24 100 GB/s; AIDA64 read (Windows) 112 GB/s. Intel MLC 3.13
  (`-e -r`): idle latency 63.7 ns (50.3 ns from P0, 77.3 ns from E8); peak reads 109.6 GB/s all cores, 111.2 GB/s P only; loaded-latency knee ~100-108
  GB/s; L2->L2 HIT 26.4 ns, HITM 32.9 ns. Idle desktop IMC traffic ~1 GB/s read.

## 9. Ring / NGU choices on this machine
NGU and D2D kept at 32x and ring at 40x: NGU 34x needs VccSA 1.28-1.30 V in every stable community report, above the operator's VccSA cap of 1.2 V;
expected gain a couple of ns (<= 1 % throughput) [COMMUNITY skatterbencher #84/#101, igorslab]. A ring undervolt was never tested in isolation here.
Setting P 54x for 3-8 active cores in the BIOS changed neither power nor temperature (reverted).
