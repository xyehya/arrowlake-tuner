Distilled 2026-10-08 from linuxmon@b01f78a (~/AI/linuxmon); detail and raw evidence there.
# Tuning results: accepted state p-step7, gate, and what failed

Sources: `linuxmon@b01f78a:docs/STATE.md`, `linuxmon@b01f78a:docs/DECISIONS.md` (D-019), `linuxmon@b01f78a:docs/research/win-first-apply.md`, `linuxmon@b01f78a:docs/research/lnx-first-read.md`, `linuxmon@b01f78a:profiles/EXPECTED-words.txt`.

## 1. Accepted state: p-step7 + E split3, BIOS LLC 6 (D-019, 2026-10-04) [MEASURED win 2026-10-04, lnx 2026-10-07/08]
Offsets in mV from stock; 55x-57x on P were set as TARGET voltages (offset = target - measured stock VID, whole mV, hardware V >= target). Favored cores marked *.
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
| 0 (CPUs 8-11) | 0 (stock, never offset) | -35 |
| 1-3 (CPUs 12-23) | 0 | -25 |
Exact V/F words the chip must read back (OC word, then points 1..n; hex, no 0x) - `linuxmon@b01f78a:profiles/EXPECTED-words.txt` = `$Expected` in `linuxmon@b01f78a:tools/win/restore_tuning.ps1`:
```
cpu0  39 8 14 1E 30 F9A00035 FC600037 FCA00038 FA800039
cpu1  0 8 14 1E 30 F9A00035 FB600037 38 39        cpu5  0 8 14 1E 30 F9A00035 F6A00037 38 39
cpu2  0 8 14 1E 30 F9A00035 F5A00037 38 39        cpu6  0 8 14 1E 30 F9A00035 F8400037 38 39
cpu3  0 8 14 1E 30 F9A00035 FC600037 FC600038 FC000039   cpu7  0 8 14 1E 30 F9A00035 F4600037 38 39
cpu4  0 8 14 1E 30 F9A00035 FBC00037 38 39
cpu8  0 8 F 17 1E 28 FB80002E     cpu12/16/20  0 8 F 17 1E 28 FCC0002E
```
(P0 OC word 0x39 is written by the BIOS at every boot; the E rows are one CPU per cluster.)
Apply order after boot: E split3 (65 ops) -> p-boot-ramp (51) -> p-step7 (51) = 167 apply ops (the post-apply vf-dump adds ~110-130 reads), from stock or from a warm reboot.
BIOS assumed: LLC 6, VccIA -100 mV, ring 40x -16 mV, DDR5-7200, NGU/D2D 32x, VccSA 1.185 V, TVB voltage optimizations off. PL1 250 W (operator 2026-10-08, D-024; PL2 250 per the 10-05 notes); earlier records 295/295 W.

## 2. Acceptance gate (D-019)
OCCT Linpack 2021 (24 threads, 2048 MB) 60 min + OCCT CPU SSE variable 60 min, OCCT unprivileged, OCCT/XTU closed during apply, WHEA/MCE watched.
| OS | Date | Result |
|---|---|---|
| Windows | 2026-10-04 evening | both PASS, WHEA unchanged at 38 (no new events since 2026-09-30) [MEASURED win] |
| Linux | 2026-10-07/08 (boot 7aa29dbd) | both PASS (operator report; OCCT logs not captured); no MCE/hardware-error/btrfs csum lines in `journalctl -k -b -1` [MEASURED lnx] |
First state on this machine to pass Linpack. Judge changes by Vcore / watts / temps at fixed (capped) loads, not by all-core MHz (set by BIOS thermal/power limits).

## 3. What failed and why [MEASURED win 2026-10-04 unless noted]
Every failure was OCCT "CPU error detected" = silent wrong result, NO WHEA event, no MCE, no system error. OCCT does not name a core (it names "core 0" even when core 0 was stock).
| State | What it was | Result |
|---|---|---|
| p-step4 (accepted 2026-10-02/03 on both OSes) | 53x CPU4 -55, CPU5 -65 and other 53x/55x offsets, LLC 7 | passed SSE 10-15 min, CPU+RAM 20 min, clock cycler; later failed Linpack |
| p-fav57 | p-step4 + 57x CPU 0/3 -75 | Linpack errors from 50:05 at LLC 7; error after ~2 min at LLC 6 (same offsets) |
| p-step6 (B) | 53x ~1.181 V on P2/5/6/7 (-90/-90/-68/-78) | OCCT CPU error within minutes (SSE) |
| p-step6-half (A) | halfway of B (-72/-75/-57/-72) + p-fav57 | Linpack error at ~25 min |
| p-step8 | never applied | - |
| p-step7 | 53x -50 uniform; 55x capped by voltage; favored 56/57x by target V | PASS (section 2) |
Why p-step7: margin returned where heavy all-core AVX runs (P at 51-52x = the 48->53x blend, 48x stock) and on the favored boost points; 55x capped by voltage instead of the deepest offsets (P7 -97 -> -91).
Linpack time-to-error is noisy (same offsets: 50 min at LLC 7, ~2 min at LLC 6): trust only a full 60-min pass, never how long a fail took. LLC 7 vs 6 made little practical difference.

## 4. Earlier history (2026-10-02, Linux) [MEASURED lnx]
- Rounds 1-2: 53x/55x/56x/57x offsets on P, E 46x -10/-20 mV: stable; per-core stress-ng sweeps all PASS.
- Round 3 added E point 5 (40x) -20 mV: OCCT SSE errors "on core 0" (10838 errors). Bisect over runs 3-12 (P offsets, ring -50/-75, VccIA -100/-75/-50, PL 320, core 0 capped, E -20) ruled out everything but E 40x -20; run 12 (E 46x split, 40x stock) passed. Lesson: never offset E point 5; ring undervolt and VccIA were innocent.
- PL 320 W ran to 102 C; 295 W maxed ~92 C (PL can also be set at runtime via powercap).
- Run 14-18: selective P offsets, E split3 (46x -35/-25) accepted at 10-15 min OCCT SSE; E tuning stopped (no measurable gain vs split2).

## 5. Lessons
1. Weaker gates missed the errors: OCCT SSE 10-15 min, CPU+RAM 20 min, clock cycler, stress-ng --verify per-core sweeps, 1-thread tests all passed states that Linpack 2021 failed. Gate = 60-min Linpack 2021 + 60-min SSE. GUI quick screens must be labelled quick screens.
2. Silent corruption leaves no WHEA/MCE: do not treat a clean kernel/Event log as stability.
3. One variable at a time; a failing OCCT run names no core, so bisect by reverting groups of offsets (the guilty offset was on E, reported on "core 0").
4. Lowering the highest VID points (55x, 56x/57x) lowers VccIA package-wide; the 53x/48x points matter for all-core heat and clocks.
5. Voltages are targets, not offsets, at the top points (p-step7); monotonic clamp can silently absorb an offset (check read-back voltage, not just the word).
6. Per-boot budget: STATE.md rule: restore (~400 ops incl. the post-apply vf-dump) plus one complete P profile (78) fits in 600; restore plus two more profiles does not (reboot, never raise the cap). Counts differ by source: ~400 (STATE.md), ~480 (win-first-apply.md), 167 = apply-only (65+51+51); reads count toward the cap, read-backs do not.
7. Operator still parked: 58x P0 test outcome, newer Linpack build (Intel oneMKL Benchmarks Suite 2026.0 vs Linpack Xtreme 1.1.8) `linuxmon@b01f78a:docs/research/linpack-binaries.md`, E 48x/46x split via OC-point V target (idea only, never applied).

## Stock V/F curve, measured (first v3 `vf.measure` Quick) [MEASURED lnx 2026-10-11 18:17 UTC]
Cold boot, runtime offsets 0 (the 16:57 UTC word read showed bare ratios), BIOS LLC 6 / VccIA −100 mV unchanged; curve id `20261011T181718Z-y4c7`
(baseline, `/var/lib/arltuner/curves/`). Requested VID (V) at ratio under single-unit load; temps 28–80 °C. `unrea` = cap search could not reach the
ratio (non-favored P-cores top out at 55×), `misma` = never settled at the target.
| Unit | 8× | 20× | 30× | 48× | 53× | 55× | 56× | 57× |
|---|---|---|---|---|---|---|---|---|
| P0 ★ | 0.693 | 0.721 | 0.811 | 1.041 | 1.216 | 1.299 | 1.386 | mismatch |
| P1 | 0.691 | 0.723 | 0.806 | 1.041 | 1.221 | 1.306 | — | — |
| P2 | 0.691 | 0.721 | 0.821 | 1.073 | 1.271 | 1.366 | — | — |
| P3 ★ | 0.691 | 0.721 | 0.821 | 1.061 | 1.221 | 1.299 | 1.359 | mismatch |
| P4 | 0.688 | 0.713 | 0.804 | 1.033 | 1.226 | 1.319 | — | — |
| P5 | 0.690 | 0.721 | 0.811 | 1.054 | 1.261 | 1.359 | — | — |
| P6 | 0.691 | 0.721 | 0.805 | 1.033 | 1.249 | 1.346 | — | — |
| P7 | 0.690 | 0.719 | 0.811 | 1.041 | 1.271 | 1.376 | — | — |
E clusters (8× 15× 23× 30× 40× 46×): EC0 0.775 0.775 mismatch 0.775 0.970 1.157 · EC1 0.775 0.775 mismatch 0.775 0.970 1.137 · EC2/EC3 0.775 0.775
mismatch 0.775 0.963 1.137 (0.775 V = E-cluster VID floor up to 30×). Open: 57× never settles on P0/P3 under this method; E 23× never settles.

### Re-measure with G-114 sampling + `tune.read` [MEASURED lnx 2026-10-11 19:06 UTC, curve `20261011T190606Z-d53n`]
- P0 57× = **1.429 V** (5/11 at target) vs fused 0x09 1.4258 V; P3 56× 1.359 V (5/15), 57× missed (active cores); P5 55× and P7 55× (7/10) noisy.
- E 47× and 48× VID == 46× VID on every cluster (EC0 1.157, EC1–3 1.137 V): 47–48× run on point 6 (F2 confirmed). E cpuinfo_max_freq 4.9 GHz
  but 0x650 = 48×8 and 0x1C E = 48: 49× is not real (intel_pstate N→N−1 mapping).
- `tune.read` (112 mailbox reads + 0x09/turbo): fused 0x09 per unit = platform-285k §4 exactly; 0x1AD 57,57,55×6 (counts 1..8); 0x650 48×8
  (counts 1,2,3,4,6,8,12,16); favored 0x1C: P0/P3 57, others 55; P0 OC word 57× ("OC (point 9)"), all offsets 0.

### Dense stock curve [MEASURED lnx 2026-10-11 19:08 UTC, curve `20261011T190830Z-rj7b`, ~12 min, temps 34–80 °C]
- 44–47 ratios per P-core (8…55/56/57×), 32 per E-cluster (8…48×). **No unit is non-monotonic** (no ratio's VID below the previous by > 3 mV) — F4 holds.
- **54× = midpoint of 53×/55× within ±1.5 mV on every P-core** (e.g. P2 1.3187 vs 1.31875) — confirms interpolation between fused points.
- cpufreq caps cannot pin P 42/47/52× nor E 13/18/21/23/26/29× (land on neighbours) → `cpufreq_granularity`. P3 57× missed (active cores),
  P7 55× mismatch: background noise, retry pass (G-118) addresses it.
- 64 KiB line budget: the result omitted EC3 with `record.omitted_units = ["EC3"]`; `curves.get {units}` returned it.
