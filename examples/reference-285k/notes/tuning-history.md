# Reference 285K: tuning history and write sittings

> **One sample, not a spec.** The offsets, target voltages and failures below belong to ONE Core Ultra 9 285K on ONE ASRock Z890 Steel Legend
> WiFi (BIOS settings in `platform.md` §1). Another chip can crash or silently corrupt data at the same values. The general lessons drawn from
> this history are in `docs/knowledge/tuning-results.md`; the hardware rules in `docs/knowledge/platform-arl-s.md`.

## 1. Accepted state: p-step7 + E split3, BIOS LLC 6 (D-019, 2026-10-04) [MEASURED win 2026-10-04, lnx 2026-10-07/08]
Offsets in mV from stock; 55x-57x on P were set as TARGET voltages (offset = target - measured stock VID, whole mV). Favored cores marked *.
| Core | 48x | 53x | 55x -> V | 56x -> V | 57x -> V |
|---|---|---|---|---|---|
| P0* | 0 | -50 | -28 -> 1.270 | -26 -> 1.360 | -43 -> 1.386 |
| P3* | 0 | -50 | -28 -> 1.270 | -28 -> 1.331 | -31 -> 1.365 |
| P1 | 0 | -50 | -36 -> 1.270 | 0 | 0 |
| P4 | 0 | -50 | -33 -> 1.286 | 0 | 0 |
| P5 | 0 | -50 | -73 -> 1.286 | 0 | 0 |
| P6 | 0 | -50 | -61 -> 1.286 | 0 | 0 |
| P2 | 0 | -50 | -81 -> 1.285 | 0 | 0 |
| P7 | 0 | -50 | -91 -> 1.286 | 0 | 0 |

| E cluster | 40x (point 5) | 46x (point 6, also 47-48x) |
|---|---|---|
| 0 (CPUs 8-11) | 0 (never offset) | -35 |
| 1-3 (CPUs 12-23) | 0 | -25 |
Words the chip must read back (OC word, then points 1..n; hex) = `../profiles/EXPECTED-words.txt` = `$Expected` in `tools/win/restore_tuning.ps1`:
```
cpu0  39 8 14 1E 30 F9A00035 FC600037 FCA00038 FA800039
cpu1  0 8 14 1E 30 F9A00035 FB600037 38 39        cpu5  0 8 14 1E 30 F9A00035 F6A00037 38 39
cpu2  0 8 14 1E 30 F9A00035 F5A00037 38 39        cpu6  0 8 14 1E 30 F9A00035 F8400037 38 39
cpu3  0 8 14 1E 30 F9A00035 FC600037 FC600038 FC000039   cpu7  0 8 14 1E 30 F9A00035 F4600037 38 39
cpu4  0 8 14 1E 30 F9A00035 FBC00037 38 39
cpu8  0 8 F 17 1E 28 FB80002E     cpu12/16/20  0 8 F 17 1E 28 FCC0002E
```
Apply order after boot: E split3 (65 ops) -> p-boot-ramp (51) -> p-step7 (51). The boot ramp existed because the guard then limited a write to 55 mV
(floor -100 mV); D-032 removed those limits.

## 2. Acceptance gate results (gate definition: `docs/knowledge/tuning-results.md` §2)
| OS | Date | Result |
|---|---|---|
| Windows | 2026-10-04 evening | Linpack 2021 60 min + SSE variable 60 min both PASS, WHEA unchanged at 38 [MEASURED win] |
| Linux | 2026-10-07/08 | both PASS (operator report; OCCT logs not captured); no MCE / hardware-error lines in `journalctl -k -b -1` [MEASURED lnx] |
First state on this machine to pass Linpack.

## 3. What failed [MEASURED win 2026-10-04 unless noted]
| State | What it was | Result |
|---|---|---|
| p-step4 (accepted 2026-10-02/03) | 53x CPU4 -55, CPU5 -65 and other 53x/55x offsets, LLC 7 | passed SSE 10-15 min, CPU+RAM 20 min, clock cycler; later failed Linpack |
| p-fav57 | p-step4 + 57x CPU 0/3 -75 | Linpack errors from 50:05 at LLC 7; after ~2 min at LLC 6 |
| p-step6 (B) | 53x ~1.181 V on P2/5/6/7 (-90/-90/-68/-78) | OCCT CPU error within minutes (SSE) |
| p-step6-half (A) | halfway of B (-72/-75/-57/-72) + p-fav57 | Linpack error at ~25 min |
| p-step7 | 53x -50 uniform; 55x capped by voltage; favored 56/57x by target V | PASS |
Why p-step7 passed: margin returned where heavy all-core AVX runs (P at 51-52x = the 48->53x blend) and on the favored boost points; 55x capped by
voltage instead of the deepest offsets (P7 -97 -> -91).

Per-core view (stock 53x/55x VIDs from `platform.md` §6; headroom = 55x offset in p-step7): CPU 0* -28, 1 -36, 4 -33, 6 -61, 5 -73, 2 -81, 7 -91,
3* -28 mV. Every core held 55x at ~1.27-1.29 V in the all-core gate, so the cores with the worst fused VID had the most room.

## 4. Earlier history (2026-10-02, Linux) [MEASURED lnx]
- Rounds 1-2: 53x/55x/56x/57x offsets on P, E 46x -10/-20 mV: stable; per-core stress-ng sweeps all PASS.
- Round 3 added E point 5 (40x) -20 mV: OCCT SSE errors reported on "core 0" (10838 errors). Bisect over runs 3-12 (P offsets, ring -50/-75, VccIA
  -100/-75/-50, PL 320, core 0 capped, E -20) ruled out everything but E 40x -20; run 12 (E 46x split, 40x stock) passed.
- Runs 14-18: selective P offsets, E split3 (46x -35/-25) accepted at 10-15 min OCCT SSE; E tuning stopped (no measurable gain vs split2).
- E 49x via XTU + Linux (2026-10-01): XTU set E 49x + V target; after a warm reboot the `0x11` word (`0x0004A931`, 1.165 V) persisted but E ran 48x
  (`0x1D`, `0x650` BIOS-restored). Linux apply `0x11` + `0x1D` = 49 + `0x27` = 3 on 16 E-cores plus `wrmsr 0x650 = 0x3131313131313131`: all E-cores
  ~4900 MHz, 1-min stress-ng --verify passed, overnight unprivileged OCCT passed. Not kept (accepted state runs E at 48x).
- P0 58x test (Windows 2026-10-06): OC ratio 58, V target 1429.69 mV, `0x1D` = `0x393A`, `0x1AD` -> `0x373737373737393A`; read back equal on CPU 0 and 8;
  no stability outcome recorded; never accepted.
- Geekbench 7.0.0 at the p-step3-era state: 3132 single / 29779 multi (https://browser.geekbench.com/v7/cpu/529116).

## 5. Write sittings from the app's Tune path (2026-10-11/12, D-034) [MEASURED lnx; no MCE/hardware-error lines throughout]
| Test | Words (read-back) | Measured VID effect |
|---|---|---|
| EC1 OC point: ratio 48 (> fused top 46), adaptive 1.1367 V | `0x00048C30` on CPUs 12-15 | 48x = 1.1375 V; table gains P2 = 7 = 48x |
| EC1 OC adaptive +10 mV (1.147 V) | `0x00049730` | 48x 1.1475, 47x 1.1425 (interpolated), 46x 1.1375 |
| EC1 OC adaptive 1.117 V (< point 6) | `0x00047830` | 48x = 47x = 1.1375: clamped up to point 6 |
| P0 whole-curve -10 mV (P2 = 0 `[31:21]`) | wrote `0xFEC5B439`, read `0xFEC00039` (target dropped: OC ratio 57 = top fused) | every point -10 mV (8x 0.688 -> 0.678 ... 57x 1.429 -> 1.419) |
| P0 whole-curve back to 0 (`0x0005B439`) | `0x0005B439` | - |
| P7 point 6 (55x) -19.5 mV | `0xFD800037` | 55x 1.376 -> 1.359; 54x 1.3137 = midpoint of 53/55 (predicted 1.315) |
| Restore accepted (172 ops, 57 read-back rows) | all match | CPU 0 `0x5B439` vs accepted `0x39` (XTU-shaped, same V); CPU 12 kept the EC1 OC point (cannot be written to 0; clears on reboot) |
- Offset-only OC words (2026-10-12 ~21:00 UTC): P0 whole-curve -10 mV = `0xFEC00039`, additive to the accepted point offsets (57x 1.386 -> 1.376 V,
  53x 1.166 -> 1.159 V). EC2 whole-curve -10 mV on a cold E word: only `0x11` sent, word `0xFEC0002E`; 46x 1.1375 - 25 - 10 = 1.1025 V, 40x 0.9525 V;
  no OC point added. Target retention: `0x0005B439` was kept at 19:5x (that apply also sent `0x1D`/`0x27`) but at 21:05 (`0x11` only) the CPU kept
  `0x00000039`.
- Page-driven write (2026-10-12 21:44 UTC, the GUI's own controls): P7 55x -90.8 -> -94.7 mV wrote `0xF3E00037`, read back equal; restore read back 57 words.
- Turbo tables (2026-10-12 ~23:10 UTC): `0x1AD` -> `0x3637373737373939`, `0x650` -> `0x2F30303030303030` => 30 s all-core stress ran P 54x / E 47x; restored.
- EC1 OC 49x @ 1.165 V: `0x0004A931` on CPUs 12-15, `0x650` = `0x3131313131313131`, `0x1D` = 49. Measured 49x 1.1675 V, 48x 1.1475, 47x 1.130 (linear
  between point 6 = 1.1125 V with -25 mV and the OC point). HWP highest CPU 12 0x44 -> 0x45, `cpuinfo_max_freq` 4.9 -> 5.0 GHz; with `scaling_max_freq`
  raised: stress 49x (4901 MHz), 0 errors, 43 C. Reverted.
- Residual OC words neutralised (2026-10-13 07:1x UTC, same boot, orchestrator): EC1 `0x11` ratio 48 + target 1.100 V -> read `0x00046630`, 48x
  measured 1.1125 V (= accepted -25 mV curve; target clamped up to point 6). EC2 first written ratio 46 + target 0 (`0x0000002E`): the planner's `0x1D`
  = 46 lowered its favored ratio (`0x1C` before `0x2e30`), `cpuinfo_max_freq` 4.9 -> 4.7 GHz; rewritten ratio 48 + 1.100 V (`0x00046630`): favored
  48, 4.9 GHz, `scaling_max_freq` had to be written back to 4.9 GHz by hand; 48x then 1.1125 V. EC0 48x 1.1226 V. MCE 0. Fix in the app: G-147.
- Short stress screens with the accepted tuning (2026-10-11): all 24 CPUs integer 60 s at P 55x / E 48x, 0 errors, 67 C, 170 W; P0-P3 capped 50x vector 30 s,
  70 C, 85 W; stressapptest 30 s, 0 errors.

## 6. Parked by the operator
58x P0 stability; a newer Linpack build (Intel oneMKL Benchmarks Suite 2026.0 vs Linpack Xtreme 1.1.8); E 48x/46x split through the OC-point V target
(never applied).
