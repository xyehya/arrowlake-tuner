# 07 Overclocking

This page covers raising frequency in the **Tune** tab: OC ratios, the adaptive voltage that goes with them, and the turbo ratio tables. It is for
someone who has done [undervolting](06-undervolting.md) first and understands the [OC point](04-arrow-lake-vf-primer.md) and the monotonic clamp.

**Danger.** Raising ratio and voltage increases heat, power draw and wear, and can destroy data. The tool accepts values up to the limits in your policy;
those are *ceilings that stop typos*, not safe values. Raise in the smallest steps, stress-test every step ([08](08-stability-testing.md)), and watch temperatures.

## 1. The model: ratio first, then voltage
Two terms, used the same way everywhere on the Tune page (G-150):

| Term | Meaning |
|---|---|
| **OC ratio** | any ratio a unit runs *above its fused top ratio*, however it got there. It can be set at boot (on the reference chip the BIOS sets the E-cores to 48x while their fused curve ends at 46x) or by an OC point |
| **OC point** | the OC ratio plus an adaptive voltage, written through the `P2 = 0` word. It is the only way a ratio above the fused top gets a voltage **of its own**. Without one, those ratios run on the top fused point's voltage |

Per unit (a P-core or an E-cluster), Tune offers these kinds of change, in this order:

1. **Voltage offset**: whole curve or per V/F point ([06](06-undervolting.md)).
2. **OC point ratio**: a ratio above what the unit runs at stock.
3. **Adaptive voltage**: the voltage target of the OC point, also shown as a **48x offset** (see below; the label uses the unit's live OC ratio).

The adaptive voltage belongs to the **OC point only** and exists only when an OC ratio above the unit's top fused ratio is set by an OC point; the lower points take
offsets only ([04 §5-6](04-arrow-lake-vf-primer.md)). So without an OC point, the adaptive-voltage control is disabled with the reason "set an OC
ratio first". The OC box reads:

| State of the unit | Box text | Adaptive voltage |
|---|---|---|
| runs at or below its stock maximum | "No OC ratio configured" | disabled |
| runs above its fused top ratio, no OC point (set at boot) | "48× (OC ratio set at boot) · no OC point" (the ratio is the live one) | disabled; use the offset control below |
| P-core only: above its stock maximum but not above its top fused point | "OC ratio r× · uses fused point n (offset only)" | disabled |
| an OC point exists | "OC point n · r× · adaptive V" | enabled |

Each unit's header line summarises the state: the offsets range, the OC ratio, and "runs up to N×" (from the turbo table read from the CPU; the tooltip says where). Examples:

| Header | Meaning |
|---|---|
| `OC ratio: 48× (fused top 46×) · no OC point · runs up to 48×` | a boot-time OC ratio with no OC point of its own |
| `OC ratio: 48× · OC point 7` | an OC point exists |
| `OC ratio: none` | nothing above the fused top |

"Stock maximum" is the unit's fused ratio from mailbox command `0x1C` (read live; on the reference chip favored P-cores 57x, other P-cores 55x, E 46x for
the OC logic). The ratio dropdown starts with **No OC point** (selectable only while the unit has none) and then offers stock maximum + 1 up to the policy's
ratio ceiling. A ratio above the fused top but below what the unit runs now (E 47x while the max is 48x) is labelled "· lowers the max from 48×": choosing it writes
a favored-ratio word that lowers the unit's maximum (measured, see section 3). **An OC point cannot be cleared at runtime**; a cold boot clears it [MEASURED lnx 2026-10-11, tuning-results.md].

If the policy's ratio ceiling is not above the unit's stock maximum, the ratio dropdown is disabled with the hint that OC ratios above N x are not
allowed by `ratio_max` in `/etc/arltuner/policy.toml`.

### The "48x offset" control
When a unit runs above its fused top, the OC box has a dropdown labelled **48x offset** (the number is the unit's live OC ratio). It is the OC ratio's voltage written
as an offset from the voltage of the fused top point (read with `0x09`), the number XTU shows. The absolute **target** dropdown stays; both edit the same draft.

| Item | Behaviour |
|---|---|
| First option | "same as point 6 (-35 mV) · no OC point": the OC ratio keeps running on the top fused point's voltage, with that point's current (edited) offset. With no OC point read, picking it changes nothing |
| Higher options | offsets above point 6's, up to your policy's target-voltage ceiling. Picking one creates the OC point at the unit's current OC ratio with the **exact** target (fused top voltage + offset); each unit of a group uses its own fused voltage |
| Lower offsets | **not offered**: the CPU raises a target below the top fused point to that point's voltage [MEASURED lnx 2026-10-11, 2026-10-13]. To go lower, lower point 6; 46-48x share its voltage |
| Disabled | when your policy has no `v_target_max_mv`, or its `ratio_max` is below the OC ratio. The tooltip names the reason |

Warnings in the review area:

- "... 48× is raised to point 6's voltage by the CPU": the target you drafted is under point 6's voltage after your point 6 edit.
- "An OC point stays until a cold boot; Restore accepted then reports it EQUIVALENT": you are creating an OC point on a unit that has none ([09 §3](09-profiles-and-restore.md)).

On a unit with no OC point, the top fused point's label also names the ratios it carries: **Point 6 · 46× (also 47-48×)**. Its offset sets 46-48x. The note under the E-cluster panel says the same:
the ratios above the top point run on its voltage, and for a voltage of their own you set the 48x offset.

## 2. Limits that apply
**Every limit here is your policy, not a built-in rule** (D-039). The values in the second column are *defaults* in `/etc/arltuner/policy.toml`; you can edit them as root, up to the
hard ceilings in the third column, which are your CPU's own limits (read live) and the register field widths. Overclockers who run liquid nitrogen or other extreme cooling on
very good silicon can therefore go beyond the defaults, at their own risk. Nothing is hard-coded below the CPU's own limits.

| Limit | Default (editable in `policy.toml`) | Hard ceiling (not editable) |
|---|---|---|
| Voltage offset | -200 ... +200 mV | the 11-bit field: -1000 ... +999 mV |
| Adaptive target voltage | `"auto"`: the CPU's own maximum from mailbox command `0x28` minus one step | that same CPU value, read live; the 12-bit field (0.5 ... 3.999 V). Not read = no raise |
| OC ratio (`ratio_max` P, E) | `"auto"`: the fused ratio from `0x1C` bits 15:8 **plus 3** (D-038: P 57 to 60, E 46 to 49), or a number you write | the CPU's OC-ratio limit from mailbox command `0x01` bits 7:0 for that core kind, read live; the field holds 1 ... 255. Not read = no raise |
| Ratio change per write | 8 | 255 |
| Turbo tables | non-increasing with active cores; each byte within the ratio ceiling | 1 ... 255 per bucket |
| Operation counters | indicators only (100000) | none |

The reference chip reports 120x in `0x01` for its P-cores, so there your policy, not the CPU, is the effective ceiling [MEASURED win 2026-10-01, registers.md]. The old "auto" rule
(ceiling = the fused top) allowed no P OC ratio at all; D-038 added the 3-bin headroom, D-039 made every number editable. On the reference machine the project author also set `ratio_max`
to P 60 / E 50 in `/etc/arltuner/policy.toml`. **Read your actual limits on the Tune page or in System → Tuning limits (policy next to what your CPU allows); do not assume them.** A refusal
names where its limit came from ([12 §4](12-safety-and-internals.md)). To allow more, edit the number, for example `E = 52` under `[ratio_max]`; it applies from the next read or plan.

## 3. Step by step: an OC ratio on one unit
1. Complete the undervolting checklist in [06 §2](06-undervolting.md): baseline curve stored, gate chosen, nothing else on the mailbox.
2. **Read ratios & voltages**, choose **Per-Core**, find the unit.
3. Pick an OC ratio **one step above** the unit's stock maximum. If it is above the top fused ratio the adaptive voltage control enables; the tool prefills
   it with the unit's fused top voltage (read with `0x09`) so you can see what will be written. (For a ratio the unit already runs, such as a boot-time 48x on an E-cluster,
   use the **48x offset** control instead; see the how-to after this list.) It never sends a zero target with an OC point
   (D-035).
4. Leave the adaptive voltage at the prefilled value for the first test (the OC point then runs at the same voltage as the point below it). Raise it
   in 5 mV steps only if the test fails and temperatures allow. A target below the top fused voltage is clamped up to it by the hardware ([04 §6](04-arrow-lake-vf-primer.md)).
5. **Review changes.** Check the operations count, the warnings (`WHEA risk`, `turbo raise`, `positive offset`) and the predicted VccIA.
6. **Apply**, check the read-back, then **Validate with a stress screen** with the unit capped to its new ratio.
7. Measure the new curve (**Measure again**), then run your gate.

What the tool writes for a ratio raise: the voltage first, then the frequency (XTU's own ordering for a raise [DECOMPILED registers.md]);
the guard refuses one request that moves both the same way. For an E-cluster raise, one plan writes the cluster's OC word on its four CPUs, the
favored-ratio word `0x1D`, the granular-ratio word `0x27`, and the E turbo table `0x650`, and then lets cpufreq's `scaling_max_freq` follow the new
maximum so the kernel actually uses the new ratio (G-126, G-147). The rule: a cap that sat at the old `cpuinfo_max_freq` follows it (up or down); a cap you set below the old
maximum is never raised. It runs in the GUI and in `arltuner apply --device` (so in the restore script), on Linux only. Review shows all of them.

Cost (order of magnitude): the reference E-cluster raise was planned as 53 mailbox WRMSRs and one P-core as 13 [T-022, SAFETY.md]. Restore accepted writes only the
words that differ, so its size depends on the CPU's state: a live restore of a state the CPU already held was 281 reads and 0 writes on the reference machine
(orchestrator's run, docs/STATE.md 2026-10-13) ([09 §3](09-profiles-and-restore.md)).

**After an OC test.** An OC ratio cannot be written back to 0 at runtime; only a cold boot removes it ([04 §6](04-arrow-lake-vf-primer.md)). If you turn an OC test off
without a cold boot, the unit keeps the OC ratio and the adaptive voltage you tested with. **Restore accepted** then rewrites that word so the unit runs the accepted voltages
and reports it as "equivalent" ([09 §3](09-profiles-and-restore.md)). The tool also never sends a favored-ratio word (`0x1D`) that only restates a ratio below the unit's
current favored maximum, because that lowers the maximum: on the reference chip `0x1D` = 46 took an E-cluster from 48x / 4.9 GHz to 46x / 4.7 GHz [MEASURED lnx 2026-10-13].

### How-to: keep the OC ratio, lower the top point
Case: an E-cluster runs 48x (set at boot, no OC point) and its fused curve ends at point 6 = 46x. You want 46x lower, but 48x unchanged. With no OC point, 46-48x
share point 6's voltage, so lowering point 6 alone lowers 48x too. An OC point gives 48x a voltage of its own.

1. **Read ratios & voltages**. Note point 6's offset (the "same as point 6 (...)" option of the 48x offset shows it). That is the 48x voltage you want to keep.
2. Lower **point 6** to the new offset.
3. In the **48x offset** dropdown pick the old point-6 offset. It is offered now, because the list starts above point 6's *edited* offset. The 48x target becomes the fused top voltage plus that offset.
4. **Review changes**. Check the 48x target and that no "raised to point 6's voltage" warning shows, then **Apply**. Read the result, then stress-test 46x, 47x and 48x ([08](08-stability-testing.md)).

What the tool does with it: it creates the OC point, and the OC point stays until a cold boot (the warning says so; Restore accepted then reports it EQUIVALENT, [09 §3](09-profiles-and-restore.md)).
You do not have to split the change. Every plan (GUI apply, CLI apply, Restore accepted) is written in a transition-safe order per unit (G-151): no ratio's voltage dips below
the lower of its voltage before and after the plan while the words are written. Writes that raise a voltage go first, so the OC word that creates the OC point is written
before the point offsets that lower the curve under it, and 48x never runs on the lowered point-6 voltage. A unit for which no such order exists is refused by name
(`write_order`) and nothing is written for it; the message suggests splitting the change into two profiles, raising voltages first (`controls/order.py`).
After the OC point exists, the CPU shows its table slot (on the reference E-clusters: point 7, ratio 48, offset 0); that slot is part of the OC point, see [09 §3](09-profiles-and-restore.md).

Result on the reference chip (example values from one Core Ultra 9 285K; yours will differ), 46x lowered by 15 mV, 48x kept [MEASURED lnx 2026-10-14, examples/reference-285k/notes/tuning-history.md §5]:

| E-cluster | Point 6 offset | 46x VID | 47x VID | 48x VID |
|---|---|---|---|---|
| EC0 | -35.2 -> -49.8 mV | 1.1226 -> 1.1075 V | 1.1150 V (midpoint) | 1.1250 V |
| EC1-3 | -25.4 -> -40.0 mV | 1.1125 -> 1.0975 V | 1.1050 V (midpoint) | 1.1125 V |

47x lands on the midpoint of 46x and 48x (the CPU interpolates between points). EC0's 48x came out 2.4 mV above the old value: the target is rounded up to the next 1/1024 V step.
This state was applied and screened (all 16 E-cores at 46x, 47x and 48x, a per-core sweep, 0 failures, no machine-check events); it was not yet accepted when this was written.

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
| P-core OC ratio 58x on P0 (V target 1.4297 V) | written once on Windows and read back; no stability result recorded; not part of any accepted state [MEASURED win 2026-10-06, platform-arl-s.md §10] |

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
  load, limited by power [MEASURED lnx 2026-10-02, platform-arl-s.md §3]. A higher ratio helps only where the load is not power-limited.
  A PL of 320 W on the same machine reached 102 C; 295 W peaked near 92 C [MEASURED lnx 2026-10-02]. The tool does not change power limits.
- **Board.** Load-line calibration, the VccIA offset and cooling decide how much voltage droop you see; they are BIOS settings.
- **Voltage ceiling.** E-core WHEA reports are reported above about 1.2 V [COMMUNITY skatterbencher.com/2025/02/14/285k-whea-when-e-core-voltage-over-1-2v/];
  the tool warns on E targets above that.
- **E-cluster caps.** The four E-cores of a cluster run one ratio. When you test an E-cluster at a fixed ratio with cpufreq caps, cap **all four** cores (the Stress tab does this for you, [08](08-stability-testing.md)): a cap on one core alone did not hold it below its neighbours' (one core capped at 4.7 GHz ran 48x); capping all four did (46x). On the reference chip a cap of (ratio + 1) x 100 MHz holds that ratio [MEASURED lnx 2026-10-14, platform-arl-s.md §3].
- **cpufreq.** A user-set `scaling_max_freq` below the new ratio wins. The tool makes the cap follow a changed maximum only when the cap sat at the old maximum, and never raises a cap you set yourself (G-126, G-147).

Next: [08 Stability testing](08-stability-testing.md) · Previous: [06 Undervolting](06-undervolting.md)
