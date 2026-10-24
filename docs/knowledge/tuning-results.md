# Tuning results: stability lessons and what the hardware did on write

General findings from tuning and write sittings on one Core Ultra 9 285K (Linux and Windows, 2026-10). The rules are stated generally; the reference
chip's offsets, words and voltages are in `examples/reference-285k/notes/tuning-history.md` (one sample, not a spec). Hardware rules derived from these
sittings are collected in `platform-arl-s.md` §3, §8.

## 1. Unstable undervolts fail silently
Every failure seen was OCCT "CPU error detected" = a wrong result, with **no WHEA event, no MCE, no system error** [MEASURED win 2026-10-04, lnx
2026-10-02]. A clean kernel/Event log is not evidence of stability. OCCT does not name the failing core (it reported "core 0" for an error caused by
an E-core offset), so bisect by reverting groups of offsets, one variable at a time.

## 2. The gate and what weaker gates missed
- The project's acceptance gate (D-019): OCCT Linpack 2021 (all threads, 2048 MB) 60 min + OCCT CPU SSE variable 60 min, OCCT unprivileged, no other
  mailbox client during apply, WHEA/MCE watched.
- OCCT SSE 10-15 min, CPU+RAM 20 min, clock cycler, per-core `stress-ng --verify` sweeps and 1-thread tests all passed states that Linpack 2021 later
  failed [MEASURED win 2026-10-04]. Short screens must be labelled as screens.
- Time-to-error is noisy (the same offsets failed after 50 min at one LLC level and ~2 min at another): trust only a full pass, never how long a fail
  took [MEASURED win 2026-10-04].

## 3. Tuning lessons
1. The heaviest all-core loads run between points, power-limited (the 48x-53x blend on the 285K), so the lower points matter for all-core stability,
   heat and clocks; the top points matter for light loads and set VccIA there (`platform-arl-s.md` §5).
2. Lowering the highest allowed VID points lowers VccIA package-wide; lowering a unit that does not set the maximum does not.
3. Setting the top points by target voltage (offset = target - measured stock VID) instead of a uniform offset gave margin back where it was needed;
   the cores with the worst fused VID had the most room (`favored-cores.md` §3).
4. The monotonic clamp can silently absorb an offset: check the re-measured voltage, not only the read-back word.
5. Judge a change by Vcore, power and temperature at a fixed (capped) load, not by all-core MHz, which the BIOS power/thermal limits set.

## 4. Measuring the stock curve
- A stock measured curve (offsets 0, cpufreq cap + pinned load + VID from `0x198`) reproduces the fused top point from cmd `0x09` within a few mV
  [MEASURED lnx 2026-10-11]; method and pitfalls in `power-profile-capping.md`.
- No unit was non-monotonic (no ratio's VID below the previous by > 3 mV); a ratio without a point sits at the interpolation of its neighbours
  [MEASURED lnx 2026-10-11].
- A favored core's top ratio settles only intermittently under a single pinned load (other active cores pull it to the next bucket); a retry pass helps.
- E-cores: VID is flat at a floor up to ~30x; ratios above the top E point run on its voltage. An E-core's `cpuinfo_max_freq` shows one bin more than
  its turbo table allows (intel_pstate rounding), so the E maximum comes from `0x650`, not from sysfs [MEASURED lnx 2026-10-11].

## 5. First write sittings: what the hardware did [MEASURED lnx 2026-10-11]
- An E OC ratio above the fused top (48 > 46) added a readable point `P2 = 7`; its adaptive target raised 48x, 47x interpolated, 46x unchanged; a
  target below point 6 was clamped up to point 6.
- `P2 = 0` offset on a P-core whose OC ratio equals the top fused ratio (no OC point): every point moved by the offset (whole-curve offset); the written
  V target was dropped from the read-back word.
- A per-point offset moved that point's VID as predicted, and the ratio between it and the next point by half.
- An OC ratio could not be written back to 0 (clears on reboot). Restoring the accepted state read back word for word.

## 6. Offset-only OC words [MEASURED lnx 2026-10-12]
- A whole-curve offset is additive to the per-point offsets.
- On a cold E word, an offset-only `0x11` (no `0x1D`/`0x27`) moved the whole cluster curve and added no OC point.
- **Target retention without an OC point is not reliable**: the same word kept its target in one apply and not in another. Rule adopted (G-124,
  D-035): a no-OC-point OC word is always written with V target 0.

## 7. Turbo-table writes and E OC 49× [MEASURED lnx 2026-10-12]
- Lowering the all-core bucket of `0x1AD` / `0x650` by one bin made a 30 s all-core load run one bin lower on P and E; restoring the words restored
  the ratios.
- E OC 49x worked with `0x11` + `0x1D` + `0x650` on the cluster; between the top fused point and the OC point the voltage is linear. HWP highest and
  `cpuinfo_max_freq` rose with `0x1D`, `scaling_max_freq` did not (uncapped load stayed at 48x until it was raised; the app raises and lowers it with the
  ratio since G-126).

## 2026-10-19 — compile stress (15 uncached llama.cpp builds, -j24, 6400 MT/s 36-38-38-80) [MEASURED lnx]
P state for all three runs: accepted P offsets + 53x <= 1.200 V (P2 -71, P5 -61, P7 -71) + 55x 1.270 V group (P2 -96, P4 -49, P5 -88, P6 -76, P7 -106),
P turbo 55x8 (`captures/lnx/profiles/p53-1200-p55-1270-*.toml`). Only the E state changed:
| TAG | E state | result |
|---|---|---|
| uv-p53p55 | 48x with no OC point (BIOS 0x1C 48 > fused 46: ran at the 46x voltage, EC0 1.157 V) | ICE in build 4 on cpu 8 (EC0), GCC 15 .cu |
| uv-p53p55-eacc | accepted: OC 48x EC0 1.123 / EC1-3 1.112 V, 46x -50/-40 mV | ICE in build 2 on cpu 13 (EC1), GCC 15 .cu |
| uv-p53p55-e46 | E turbo 46x8, point 6 offset 0 (stock 46x: EC0 1.1575 / EC1-3 1.1375 V) | **15/15, 0 ICE**, 111-114 s, MCE 0, btrfs clean, 0 SIGSEGV |
Reading: the P undervolt above passes this test; E at 48x failed at both voltages tried (two events, both GCC 15 at version.h:1). E 48x needs more voltage
than the accepted 1.11-1.12 V, or 48x is not worth it. Under load P ran 55x ~37% / 54x ~56%, rail 1.271 V, package ~245 W.

## 2026-10-19 22:2x — favored cores 56x -15 / 57x -75 mV (profile p53p55-fav5675, P turbo 57/57/55x6) [MEASURED lnx]
Hardware monotonic rule confirmed: with 56x at -15 mV the 57x point (-75 mV requested, word verified 0xF6600039) runs at the 56x voltage —
P0 57x VID 1.371 V (= 56x), P3 57x 1.344 V (= 56x); effective 57x undervolt ~-55 / ~-51 mV. stress-ng P0+P3 together, 120 s each, cpufreq cap 56x then
57x: both pass, MCE 0, 47-49 W, max 59 C, only turbo-table bit (0x1000) seen. Before the apply the BIOS-booted P3 held 56x -28.3 / 57x -31.3 mV
(BIOS per-core V/F setting, re-applied each boot). `captures/lnx/fav-cap-runs-*.txt`, `stress-fav-cap5[67]-*.txt`.

## 2026-10-19 23:xx — E-cluster search (3 all-core compile builds per step, auto-stop at first ICE; P = p53p55-fav5675, PL2 295 W) [MEASURED lnx]
| step | E state under load | result |
|---|---|---|
| 46x pt6 -40..-140 mV | 46x, EC0 1.118..1.018 / EC1-3 1.098..0.998 V | all 3/3 |
| 46x pt6 -150 / -160 mV | EC1-3 0.988 / 0.978 V | ICE cpu 21 (EC3), builds 3 / 1 |
| 49x OC 1.210 / 1.240 / 1.255 V (pt6 -120) | 49x at target | ICE cpu 23 / 23 / 21 (EC3) |
| 49x OC 1.270 V | 49x 1.270 V | 3/3 |
| 50x OC 1.300 V | **not 50x**: all-core load -> E 49x ~1.23 V (interpolated), P 48x | 3/3 but not a 50x result |
Every E failure was on EC3 (cpus 20-23): the weakest cluster sets the requirement. 46x edge -140 mV (working -120); 49x edge 1.270 V.
**All-core current limit:** with E at 50x (OC 1.285-1.300 V) a 24-core load sets 0x64F bit 8 ("Other (electrical)", Current/EDP group) + bit 12 at
~215 W (< PL2 295): E falls to 49x, P to 48x. E-only load runs 50x at 1.287 V with no limit bit. At E 49x 1.270 V, stress-ng 24 threads keeps E at 49x
but P drops to 46-54x (0x1100) — higher E voltage spends the shared current budget P would use. 50x all-core needs a higher IA current limit (IccMax,
BIOS or mailbox 0x16/0x17, not allowlisted) [inference: bit 8 = IccMax/EDP is UNVERIFIED on ARL].
- 2026-10-20 03:12: E 48x OC 1.175 V (pt6 -120), P state B, PL1 250 / PL2 295 / tau 112 s: 10-build run 9/10 — ICE in build 10 on cpu 23 (EC3), GCC 15
  version.h:1. A 3-build screen at the edge is not enough; 1.185 V (20 mV above the 1.165 V failure) is the 48x working value, unconfirmed at 10/15 builds.
  During the run: E 48x 100 %, P 54x ~85 % / 55x ~7 %, package ~231 W avg (243 max), 0x64F bit 8 intermittent.
- 2026-10-20 03:39: E 46x pt6 -120 (P state B, PL1 250/PL2 295): 12/15, ICE build 13 cpu 21 (EC3). Long runs move the edge; next -100 x15.
- 2026-10-20 04:08: E 46x pt6 -100 (P state B, PL1 250/PL2 295/tau 112 s): **15/15**, 111-114 s, MCE 0, 0 SIGSEGV. Known-good = -100 (good-p53p55-fav-e46m100.toml).
- 2026-10-20 06:29: E 47x OC 1.077 V all clusters (+P0 58x OC 1426 mV, turbo P 58,58,55x6 E 47x8): 10/10. 1.057/1.037 failed b4 of 10, 1.077/1.057 failed b3 (EC3).
- 2026-10-20 07:14: E 47x 1.077 V uniform under tuned hpc-compute-zram, llama.cpp b11748: ICE build 2 on cpu 8 (EC0), mmq-instance TU, MCE 0 — first traced EC0 failure; EC0 needs its +20 mV (fused V 20 mV above EC1-3). Back to e47-2 (EC0 1.097 / EC1-3 1.077).
- 2026-10-20 07:49: e47-2 (EC0 1.097 / EC1-3 1.077) 10/10 under hpc-compute-zram on b11748 (107-109 s, MCE 0). Accepted as good-p58-e47-2. hpc-compute-zram: HWP min=max, cpu_dma_latency 1 us -> ~257 W / 86 C during builds vs ~220-240 W / 60-66 C balanced [MEASURED lnx 2026-10-20].

## 2026-10-24 — corrected MCEs on CPU0 under P0 58x; OC dropped (D-051) [MEASURED lnx 2026-10-24]
- Boot 2026-10-22 12:36 → 2026-10-24 14:00 ran good-p58-e47-2 (P0 OC 58x @ 1426 mV): **23** corrected "Machine check events logged", first 46 min after the
  apply (13:22), incl. `CPU0 BANK0 CMCI storm detected` (06:23, subsided 06:24). Bank 0 on a P core = L1 data cache. No mcelog/rasdaemon then, so no
  status/address; ABRT reported each as a "kernel oops" and deleted it. Every other boot that week: 0 MCE — including a 24 h boot on the same profile (Oct 21-22).
- The machine's abrupt reboots that week are NOT this: all 7 since Oct 11 hit within 2.2 min of the hour (building generator / EDL switchover through the
  inverter); no panic, no kdump vmcore, no boot-time MCE.
- Operator dropped the P0 OC: profile `good-p57-e47` (= good-p58-e47-2 without the P0 OC point, P turbo 57,57,55x6), applied 14:21, all writes read back
  equal, MCE 0; accepted 11:30Z (previous saved as accepted-before-20261024-113040). rasdaemon 0.8.0 installed — a recurrence at 57x now keeps bank/status.
- segv-trace noise: `lscpu` SIGSEGV code=128 is its own VMware-backdoor probe (caught), run by ABRT for each oops — benign.
