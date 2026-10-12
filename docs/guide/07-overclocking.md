# 07 Overclocking

This page covers raising frequency in the **Tune** tab: OC ratios, the adaptive voltage that goes with them, and the turbo ratio tables. It is for
someone who has done [undervolting](06-undervolting.md) first and understands the [OC point](04-arrow-lake-vf-primer.md) and the monotonic clamp.

**Danger.** Raising ratio and voltage increases heat, power draw and wear, and can destroy data. The tool accepts values up to the limits in your policy;
those are *ceilings that stop typos*, not safe values. Raise in the smallest steps, stress-test every step ([08](08-stability-testing.md)), and watch temperatures.

**Labels.** This page uses the names introduced by task G-133 (OC ratio before adaptive voltage; **All-Core** / **Per-Core**). If your build predates it,
the Tune page still shows the older layout and the ratio dropdown may offer only the unit's current top ratio; the facts below are unchanged.

## 1. The model: ratio first, then voltage
Per unit (a P-core or an E-cluster), Tune offers three kinds of change, in this order:

1. **Voltage offset**: whole curve or per V/F point ([06](06-undervolting.md)).
2. **OC ratio**: a ratio above what the unit runs at stock.
3. **Adaptive voltage (OC point)**: the voltage target of the OC point.

The adaptive voltage belongs to the **OC point only** and exists only when an OC ratio above the unit's top fused ratio is set; the lower points take
offsets only ([04 §5-6](04-arrow-lake-vf-primer.md)). So with no OC ratio configured, the adaptive-voltage control is disabled with the reason "set an OC
ratio first". Per the G-133 specification the OC box reads:

| OC ratio set | Box text | Adaptive voltage |
|---|---|---|
| none, or not above what the unit runs at stock | "No OC ratio configured" | disabled |
| P-core only: above its stock maximum but not above its top fused point | "OC ratio r x, uses fused point n (offset only)" | disabled |
| above the top fused point | "OC point n, r x, adaptive V" | enabled |

"Stock maximum" is the unit's fused ratio from mailbox command `0x1C` (read live; on the reference chip favored P-cores 57x, other P-cores 55x, E 46x for
the OC logic). The dropdown starts with "No OC ratio" (selectable only while the unit has none) and then offers stock maximum + 1 up to the policy's
ratio ceiling. **An OC ratio cannot be cleared at runtime**; a cold boot clears it [MEASURED lnx 2026-10-11, tuning-results.md].

If the policy's ratio ceiling is not above the unit's stock maximum, the ratio dropdown is disabled with the hint that OC ratios above N x are not
allowed by `ratio_max` in `/etc/arltuner/policy.toml`.

## 2. Limits that apply
| Limit | Value | Source |
|---|---|---|
| Voltage offset | -200 ... +200 mV | D-032 |
| Adaptive target ceiling | the CPU's own maximum from mailbox command `0x28` (minus one step), read in the session | D-032; `policies/default.toml` |
| Ratio ceiling (`ratio_max`, per kind) | policy `"auto"` = the fused maximum from `0x1C` **plus 3** (D-038, 2026-10-12), clamped to the code limit P 62 / E 60. Builds before task G-134 use the plain fused maximum. A number in `policy.toml` overrides it | D-038; `src/arltuner/guard/policy.py` `fused_ratio_cap` |
| Ratio change per write | at most 8 | D-032 |
| Turbo tables | non-increasing with active cores; each byte non-zero and within the ratio ceiling | `turbo_ratio_bounded` check |
| Operation counters | indicators only | D-032 |

D-038 was made because with the older rule a P-core's ceiling equalled its fused top, so no P OC ratio was possible at all. On the reference machine the
operator set `ratio_max` to P 60 / E 50 in `/etc/arltuner/policy.toml` (D-038). Read your actual limits on the Tune page; do not assume them.

## 3. Step by step: an OC ratio on one unit
1. Complete the undervolting checklist in [06 §2](06-undervolting.md): baseline curve stored, gate chosen, nothing else on the mailbox.
2. **Read ratios & voltages**, choose **Per-Core**, find the unit.
3. Pick an OC ratio **one step above** the unit's stock maximum. If it is above the top fused ratio the adaptive voltage control enables; the tool prefills
   it with the unit's fused top voltage (read with `0x09`) so you can see what will be written. It never sends a zero target with an OC point
   (D-035; the prefill is a G-133 feature, so an older build shows an empty target).
4. Leave the adaptive voltage at the prefilled value for the first test (the OC point then runs at the same voltage as the point below it). Raise it
   in 5 mV steps only if the test fails and temperatures allow. A target below the top fused voltage is clamped up to it by the hardware ([04 §6](04-arrow-lake-vf-primer.md)).
5. **Review changes.** Check the operations count, the warnings (`WHEA risk`, `turbo raise`, `positive offset`) and the predicted VccIA.
6. **Apply**, check the read-back, then **Validate with a stress screen** with the unit capped to its new ratio.
7. Measure the new curve (**Measure again**), then run your gate.

What the tool writes for a ratio raise: the voltage first, then the frequency (XTU's own ordering for a raise [DECOMPILED win-xtu-decompile-mailbox.md]);
the guard refuses one request that moves both the same way. For an E-cluster raise, one plan writes the cluster's OC word on its four CPUs, the
favored-ratio word `0x1D`, the granular-ratio word `0x27`, and the E turbo table `0x650`, and then lifts cpufreq's `scaling_max_freq` so
the kernel actually uses the new ratio (G-126). Review shows all of them.

Cost (order of magnitude): the reference E-cluster raise was planned as 53 mailbox WRMSRs and one P-core as 13 [T-022, SAFETY.md]; restoring the whole
accepted state on the reference machine took 172 write operations plus reads [MEASURED lnx 2026-10-11].

## 4. What was measured on the reference chip (example values from one chip)
All from a Core Ultra 9 285K; the point is the behaviour, not the numbers.

| Test | Result |
|---|---|
| E-cluster OC ratio 48x (above the fused top 46x) with adaptive 1.1367 V | 48x = 1.1375 V, neutral; the table gained "OC point 7" [MEASURED lnx 2026-10-11] |
| Same, adaptive +10 mV | 48x = 1.1475 V, 47x = 1.1425 V (interpolated) [MEASURED lnx 2026-10-11] |
| Adaptive below point 6 (1.117 V) | 48x = 47x = 1.1375 V: clamped up [MEASURED lnx 2026-10-11] |
| E-cluster OC ratio 49x at 1.165 V, E table raised to 49x, `0x1D` = 49 | 49x = 1.1675 V, 48x = 1.1475 V, 47x = 1.130 V; `cpuinfo_max_freq` rose to 5.0 GHz; with `scaling_max_freq` lifted a stress run held 49x (4901 MHz), 0 errors, 43 C. Reverted afterwards [MEASURED lnx 2026-10-12] |
| Same, kernel default after the raise | `scaling_max_freq` stayed 4.9 GHz, so an uncapped load ran 48x (intel_pstate maps N to N-1); fixed by G-126 [MEASURED lnx 2026-10-12] |
| E 49x earlier (Windows XTU, then Linux) | all 16 E-cores reached about 4900 MHz, a 1-minute `stress-ng --verify` passed, overnight OCCT passed [MEASURED lnx 2026-10-01] |
| P-core OC ratio 58x on P0 (V target 1.4297 V) | written once on Windows and read back; no stability result recorded; not part of any accepted state [MEASURED win 2026-10-06, platform-285k.md §10] |

Nothing about a P-core OC ratio above the top fused point has been validated for stability on the reference chip. Whether a non-favored P-core accepts
56x/57x as real ratios is open (favored-cores.md §5, [UNVERIFIED]).

## 5. Turbo ratio tables
The **Turbo ratio tables** card shows the eight buckets for P-cores and for E-cores: the ratio allowed with 1, 2, ... active cores. The core counts of
the buckets are read from `0x1AE` / `0x651` and are not editable. What can be edited is the ratio of each bucket ([04 §10](04-arrow-lake-vf-primer.md)).

Rules enforced:

- Ratios may only fall as more cores are active. A value higher than the bucket to its left is not offered, and lowering a bucket brings the ones on
  its right down with it. A plan that breaks the order is refused with `TURBO_ORDER`.
- The ratio is bounded by the policy ceiling (see section 2) and by `max_ratio_step` from the table read in the session.
- The table is written as a whole 64-bit value on CPU 0; the tool reads it back on CPU 0 and on one E-core.
- A change that raises a bucket shows the `turbo raise` warning.

A turbo table is not an OC point: on the reference chip E-cores run 48x because the E table `0x650` says 48x, not because of an OC point
[MEASURED lnx 2026-10-11, SPEC-v3 F2]. Lowering a bucket lowers the all-core clocks without touching voltage; this is a quick way to test a ratio cap. Example:
setting only the P table's 8-core bucket to 54x and the E table's 16-core bucket to 47x made an all-core stress run exactly 54x / 47x, and restoring the
tables brought back 55x / 48x [MEASURED lnx 2026-10-12].

Per-core favored ratios (`0x1C`) sit on top of the table: the favored word is written together with an OC ratio so the hardware's "highest performance"
follows. Observed side effect: the kernel rescaled the CPU capacity (`energy_model: updating cpuN cpu_cap`) when `0x1D` changed [MEASURED lnx 2026-10-01].

## 6. Limits you hit that are not the tool's
- **Power and thermal.** All-core clocks on the reference chip were set by the BIOS power limits, not by the voltage: P-cores ran 51-54x under all-core
  load, limited by power [MEASURED lnx 2026-10-02, platform-285k.md §3]. A higher ratio helps only where the load is not power-limited.
  A PL of 320 W on the same machine reached 102 C; 295 W peaked near 92 C [MEASURED lnx 2026-10-02]. The tool does not change power limits.
- **Board.** Load-line calibration, the VccIA offset and cooling decide how much voltage droop you see; they are BIOS settings.
- **Voltage ceiling.** E-core WHEA reports are reported above about 1.2 V [COMMUNITY skatterbencher.com/2025/02/14/285k-whea-when-e-core-voltage-over-1-2v/];
  the tool warns on E targets above that.
- **cpufreq.** A user-set `scaling_max_freq` below the new ratio wins. The tool lifts it for an OC raise but respects caps you set yourself (G-126).

Next: [08 Stability testing](08-stability-testing.md) · Previous: [06 Undervolting](06-undervolting.md)
