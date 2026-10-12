# 05 Measuring curves

This page explains the **Curves** tab: how the tool measures the voltage your CPU requests at each frequency, how to store and compare curves, and what
the "not measured" reasons mean. It is for anyone about to tune. **Measure your stock curve before the first write.**

## 1. What is measured, and what is not
For each tuning unit (a P-core, or an E-cluster) and each ratio, the tool caps the unit at that ratio, loads it, and records the **requested VID**:
MSR `0x198` bits 47:32 divided by 8192 V, together with the running ratio in bits 15:8 [MEASURED lnx 2026-09-29, registers.md]. The result is the
real volts-against-frequency curve *as currently tuned*, offsets included (SPEC-v3 §3).

- It is a **request**, not the voltage the regulator delivers. No delivered-voltage telemetry exists [MEASURED lnx 2026-10-01, platform-285k.md §5].
- Measuring sends **no mailbox traffic and writes no MSR.** The only writes are cpufreq sysfs limits, journaled first and restored when the run ends,
  is cancelled, fails, or at the next helper start if the helper died mid-run (`ops_measure.py`).
- The measured curve is a tuning input: the Tune page predicts the effect of an offset on top of your **baseline** curve.

## 2. Run a measurement
In the **Curves** tab, card **Measure V/F curves**:

| Control | Meaning |
|---|---|
| **Resolution: Quick** | the unit's V/F point ratios (plus the unit's top turbo ratio if it is above the top point). The page estimates the time; the tooltip says about 2 minutes for all units |
| **Resolution: Dense** | every ratio from 8 to the unit's top. Tooltip: about 10 minutes for all units; one run on the reference chip took about 12 minutes [MEASURED lnx 2026-10-11] |
| **Name** | optional, up to 64 characters, e.g. `stock` |
| **Units** | chips for each unit; pick any subset (a changed unit only is quick) |
| **Measure** / **Cancel** | start; cancel at any time, partial results are kept (stored as incomplete) |

How a run works (helper `vf.measure`, SPEC-v3 §3, G-132):

1. For every CPU of the unit the tool records `scaling_min_freq` and `scaling_max_freq` and journals them.
2. For each ratio it **pins** the unit: `scaling_min_freq` = `scaling_max_freq` = ratio x 100 MHz on every CPU of the unit (P-core: one CPU; E-cluster:
   its four CPUs), writing max first when raising and min first when lowering (the kernel refuses min above max). Governor, power profile and energy
   preference are never changed [MEASURED lnx 2026-10-12, power-profile-capping.md].
3. It loads all CPUs of the unit with pinned busy loops, waits 0.3 s, takes 10 samples 0.1 s apart and keeps those taken at the target ratio. At least
   half must match, else the point is not `ok`.
4. If the ratio is not reached it retries with the next cap bin (ratio + 1) x 100 MHz, and for a point nothing reaches it runs a bounded cap search
   (about 10 s per point at most).
5. When the run ends, min and max are restored exactly.

Run it when the machine is idle and cool-ish: a measurement loads the unit hard, and temperatures were 28-80 C during the reference runs. Don't run it
while a stress screen or another tuning tool is active.

### Power profile and turbo
The V/F request at a given ratio is **the same in the Performance and Balanced power profiles** [MEASURED lnx 2026-10-12, power-profile-capping.md §3]. The
**Power Saver** profile switches turbo off and holds a loaded core at a low ratio (27x P / 19x E on the reference chip), so nothing above base can be
measured there. The page shows a note when it sees `intel_pstate/no_turbo` = 1 and the points above base are marked `turbo_off`. Switch to Balanced or
Performance (for example in GNOME Settings) and measure again. The tool does not change the profile itself.

## 3. Reading a curve
Views (segmented control **Chart / Table / Library**; the **Curve** selector picks which stored curve is shown):

- **Chart**: x = frequency (MHz, with ratio ticks), y = volts. One line per unit. Presets: **All**, **P-cores**, **E-clusters**, **Favored ★** (the
  favored P-cores detected on your CPU). Legend chips toggle single units. Hovering shows a crosshair with every drawn unit's voltage. **Compare with**
  overlays a second stored curve as dashed lines. On the baseline curve the fused top point of each unit (mailbox command `0x09`) is drawn as a marker
  for cross-checking.
- **Table**: rows are units, columns are ratios, cells are volts. The toggle **Volts / Δ vs baseline (mV)** shows the difference to the baseline curve;
  negative means lower voltage.
- **Library**: every stored curve with name, date, resolution and tuning state; buttons **View**, **Compare**, **Rename**, **Export** (JSON download),
  **Delete**, and **Set baseline** / **Clear baseline**.

### The stock baseline
Mark **one** curve as the **stock baseline**: measure with all offsets at 0 (right after a cold boot, or after **Reset offsets to 0** in Tune), name it
`stock`, and press **Set baseline** in the Library. A curve measured at offsets 0 *is* the fused V/F table as the CPU applies it, because the mailbox
exposes only the top fused point of each unit (SPEC-v3 F7; [04 §3](04-arrow-lake-vf-primer.md)). Tune predicts voltages on
the baseline, and **Δ vs baseline** compares later curves against it. Without a baseline Tune warns `no baseline` and predicts less.

Each stored record keeps: id, name, creation time, boot id, resolution, kernel, the **tuning state** in force (`stock`, `words` from a read this boot, or
`unknown`), min/max temperature during the run, and per unit the points `{ratio, mhz, vid_v (mean), vid_min, vid_max, kept, total, status}`. Records live
in `/var/lib/arltuner/curves/<id>.json` (root-owned, mode 0600; the directory is 0700).

## 4. Point status and "why not measured"
A point is `ok`, `mismatch` (the target ratio was hit now and then but too rarely), `interpolated`, or `unreachable` with a reason.

| Reason | Meaning |
|---|---|
| `max_turbo` | the ratio is above this unit's highest ratio (for example a 56x or 57x point on a non-favored P-core that tops out at 55x). The point exists in the CPU's table; it never runs here. Not an error |
| `cap_granularity` | no cpufreq cap produces this ratio; the caps land on its neighbours. The kernel converts the cap to hardware performance units, and some ratios are never produced (reference chip: E 13, 18, 21, 23, 26, 29, 31, 34, 39x; P 42, 47, 52x) [MEASURED lnx 2026-10-12, power-profile-capping.md §6]. Older records call it `cpufreq_granularity` |
| `active_cores` | reachable only with few cores active, and the measurement loads the whole unit (a favored core's top points) |
| `turbo_off` | `intel_pstate/no_turbo` is 1 (Power Saver); ratios above base are not attempted |
| `user_cap` | your own `scaling_max_freq` is below the ratio, so it was not attempted. Remove the cap and measure again |

**`interpolated`.** For a V/F point that failed with `cap_granularity` or `active_cores` and has a measured point on both sides, the tool estimates the
voltage by a straight line between the nearest measured V/F points (the chart draws it as a hollow marker and the caption says "interpolated (hollow markers): ..."). This rests on the measured behaviour that the
CPU itself interpolates between points: 54x measured at the midpoint of 53x and 55x on every P-core [MEASURED lnx 2026-10-11]. An interpolated value is
never shown as measured; points above the unit's reach are not interpolated.

Example (reference chip, E-cluster 0, 23x unreachable by any cap): stored as `interpolated` at 0.775 V between its neighbours [MEASURED lnx 2026-10-12,
STATE.md].

## 5. Example: a stock curve from one chip
Quick curve, cold boot, all offsets 0, requested VID (V) at ratio, single-unit load. **Example values from one chip** (reference 285K)
[MEASURED lnx 2026-10-11 18:17 UTC, tuning-results.md]; `-` = the point is above what that core runs:

| Unit | 8x | 20x | 30x | 48x | 53x | 55x | 56x | 57x |
|---|---|---|---|---|---|---|---|---|
| P0 (favored) | 0.693 | 0.721 | 0.811 | 1.041 | 1.216 | 1.299 | 1.386 | mismatch |
| P1 | 0.691 | 0.723 | 0.806 | 1.041 | 1.221 | 1.306 | - | - |
| P2 | 0.691 | 0.721 | 0.821 | 1.073 | 1.271 | 1.366 | - | - |
| P7 | 0.690 | 0.719 | 0.811 | 1.041 | 1.271 | 1.376 | - | - |

E-clusters (8x, 15x, 23x, 30x, 40x, 46x): cluster 0 0.775, 0.775, mismatch, 0.775, 0.970, 1.157 V; clusters 1-3 end at 1.137 V (0.775 V is the E voltage
floor up to 30x). What to notice, and what holds for any chip: the voltage at the same ratio differs between cores (55x: 1.299 V on P0, 1.376 V on P7, a 77 mV spread); the curve is
monotonic (a dense run found no unit whose voltage fell by more than 3 mV from one ratio to the next [MEASURED lnx 2026-10-11]); and a favored core's
voltage climbs steeply from 55x to 57x (P0: +127 mV) while a non-favored core's 57x point is a flat copy of its 55x [MEASURED lnx 2026-10-02].

## 6. After a tuning change
After you apply offsets, **Measure again** (Quick, only the changed units; the Tune page preselects them). Compare to the baseline with **Δ vs
baseline**. This shows the *effective* voltage change including the monotonic clamp and interpolation ([04](04-arrow-lake-vf-primer.md) §7). A
measured curve at offsets 0 is not the same as one measured after a change; the record's tuning state tells you which it is.

Next: [06 Undervolting](06-undervolting.md) · Previous: [04 Arrow Lake V/F primer](04-arrow-lake-vf-primer.md)
