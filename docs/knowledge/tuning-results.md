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
