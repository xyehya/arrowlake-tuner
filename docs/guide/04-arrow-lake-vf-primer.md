# 04 Arrow Lake V/F primer

This page collects the hardware facts that tuning on Arrow Lake-S rests on: how the voltage/frequency (V/F) curve is built, what you can and cannot
change at runtime, how the CPU combines your changes, and what resets on reboot. It is for anyone about to write an offset or a ratio.
Read it before [06 Undervolting](06-undervolting.md) and [07 Overclocking](07-overclocking.md).

**How to read the tags.** Every hardware claim carries its evidence: `[MEASURED host date]` was read on a real machine, `[DECOMPILED file]` comes
from vendor code, `[DOC url]` from public documentation, `[COMMUNITY url]` from an overclocking community source, `[UNVERIFIED]` is a lead. The
register-level source for all encodings is [`../knowledge/registers.md`](../knowledge/registers.md).
**Example values.** Numbers marked *reference chip* were measured on one Core Ultra 9 285K on an ASRock Z890 Steel Legend WiFi (BIOS 3.40). Your
CPU's fused voltages differ per core and per sample, and the app reads yours live. Treat the numbers as an illustration of the shape, not as what you
will see.

## 1. How the tool talks to the CPU
The CPU exposes a voltage/frequency control interface, the **OC mailbox**, through model-specific register `0x150`. A request is a 64-bit value
`1<<63 | P2<<48 | P1<<40 | cmd<<32 | data`; the CPU clears bit 63 when it has answered, the status is in bits 39:32 and the result in bits 31:0
[DECOMPILED registers.md "OC mailbox"; matches kernel and published sources].

- **Every mailbox request, including a read, is a write (WRMSR) to MSR `0x150`.** There is no memory-mapped alternative [DECOMPILED registers.md]. This
  is why "Read ratios & voltages" in the Tune tab needs the same write permission as Apply.
- The core a request applies to is chosen by **which CPU executes it** (the tool pins the access to that CPU), not by a field in the request
  [DECOMPILED registers.md].
- The mailbox is volatile on the reference machine: a cold boot returns everything to stock (section 11).
- Turbo ratio tables are **not** in the mailbox; they are plain MSRs `0x1AD`, `0x1AE`, `0x650`, `0x651` (section 10).

## 2. Tuning units and regulators
Each **P-core** has its own on-die voltage regulator (DLVR) and therefore its own V/F curve. The **E-cores** are grouped in **clusters of four**
that share one regulator and one curve. All DLVRs are fed from one external rail, VccIA, which boards label "Vcore"
[MEASURED lnx 2026-10-01, platform-arl-s.md §5]. The cluster-of-four grouping (one regulator per cluster) is tagged [COMMUNITY SkatterBencher] in platform-arl-s.md §2 and registers.md "Voltage semantics" (the SPEC-v3 header calls it [MEASURED]); the cluster membership itself (four E-cores per cluster) is [MEASURED lnx 2026-09-29].

So a tuning unit is one P-core or one E-cluster. On the reference chip (8 P + 16 E, no SMT) that is 12 units: Linux CPUs 0-7 are P-cores, CPUs 8-23
are E-cores in clusters 8-11, 12-15, 16-19, 20-23 [MEASURED lnx 2026-09-29]. The app derives the unit list from your CPU's sysfs
(`/sys/devices/cpu_core`, `cpu_atom`) and never assumes it (D-036). To tune an E-cluster the tool writes every CPU of the cluster.

## 3. Fixed V/F points
A curve is a table of **points**, each a (ratio, voltage) pair. The CPU fuses the table at manufacture; **you cannot move the ratio of a fused point**.
For fused points the mailbox accepts a voltage **offset** only. The point count and ratios are the same for every core of a kind on the reference
chip and are read live by the app.

Example values from one chip (reference 285K; vf-dump of all cores [MEASURED lnx 2026-10-02]):

| Unit kind | Fused points (ratio, in order, mailbox `P2` = 1, 2, ...) | Request for the next index |
|---|---|---|
| P-core | 8, 20, 30, 48, 53, 55, 56, 57 (8 points) | `P2 = 9` answers return code `0x11` ("no such point") |
| E-cluster | 8, 15, 23, 30, 40, 46 (6 points) | `P2 = 7` answers `0x11` |

The same point ratios appear in an independent community write-up for the 285K (P points 1-8 = 8/20/30/48/53/55/56/57, E points 1-6 =
8/15/23/30/40/46, all factory-fused, offset only) [COMMUNITY SkatterBencher 285K guide, "Advanced Voltage Offset" slides 39/40, platform-arl-s.md §3].
Whether other 200S parts use the same ratios is [UNVERIFIED]; the tool reads the table from each CPU.

Facts about the points:

- **The mailbox does not expose the lower fused voltages.** Only the *top* fused point of each unit can be read, with command `0x09` (P1 = `0x11`)
  [MEASURED win 2026-10-01, registers.md]. The stock curve as the CPU applies it is obtained by **measuring** with all offsets at 0
  ([05](05-measuring-curves.md); SPEC-v3 F7).
- Example: top fused points on the reference chip, `0x09` words per P-core: P0 1.4258 V, P1 1.3047 V, P2 1.3633 V, P3 1.3945 V, P4 1.3164 V,
  P5 1.3555 V, P6 1.3438 V, P7 1.3750 V, all at 57x; E-cluster 0 1.1563 V and clusters 1-3 1.1367 V at 46x
  [MEASURED win 2026-10-01 / lnx 2026-10-02, platform-arl-s.md §4]. They differ per core: that is the per-core silicon spread.
- Stock requested voltage is a *request* (VID, MSR `0x198` bits 47:32 divided by 8192 V; ratio in bits 15:8), not a measurement of delivered
  voltage, and no delivered-voltage telemetry exists for the DLVRs [MEASURED lnx 2026-09-29 / 2026-10-01, registers.md, platform-arl-s.md §5].

## 4. The V/F word
The word for core-scoped commands `0x10` (read) and `0x11` (write) has these fields [DECOMPILED registers.md "V/F data word"]:

| Bits | Field | Encoding |
|---|---|---|
| 7:0 | ratio | integer multiplier (x 100 MHz bus clock) |
| 19:8 | voltage target | 12 bit, V = raw / 1024 (`0x48C` = 1.1367 V) |
| 20 | mode | 0 = adaptive, 1 = static (override); the tool only ever writes adaptive (D-016) |
| 31:21 | voltage offset | 11-bit signed two's complement, one step = 1/1024 V = 0.977 mV; -50 mV = raw -51 = `0x7CD` |

The `P2` request field selects what the word belongs to:

| `P2` | Meaning |
|---|---|
| 0 | the unit's **OC word**: ratio, adaptive target voltage and a *whole-curve* offset |
| 1 ... n | the V/F point of that index; a point write carries `point_ratio \| offset << 21` and a voltage target of 0 [MEASURED lnx 2026-10-02] |

The decode of the offset field was confirmed against a known value: a graphics-slice word read `0xF8600000` = -59.6 mV, matching the -0.060 V that
Intel XTU had applied [MEASURED win 2026-10-01]. Per-point offsets (`P2` = 1 ... n) are hidden in XTU on Arrow Lake-S but exist and were written on
both operating systems [MEASURED lnx 2026-10-02, platform-arl-s.md §10].

Example (reference chip): `0xF9A00035` = ratio `0x35` (53x), no target, offset -50 mV: a -50 mV offset at the 53x point of a P-core.

## 5. Two kinds of voltage change
| | Offset | Adaptive target |
|---|---|---|
| Where | any fused point (per point) or the whole curve | the **OC point** only |
| What it does | adds a fixed delta (mV) to the point's voltage | sets the voltage the OC point uses |
| Range in this tool | default -200 ... +200 mV, your policy (D-039); the field holds -1000 ... +999 mV | default up to the CPU's own maximum (mailbox command `0x28`); your policy may state a lower number |
| Needs an OC ratio | no | yes: the target is only active when a ratio above the top fused ratio is set |

Because the fused points take offsets only, an "undervolt" in this tool is always an offset. A **target voltage** exists only on the OC point
(the point above the top fused one) and is what you use to overclock. Static/override mode is out of scope (D-016).

**Whole-curve offset.** The offset field of the `P2 = 0` word shifts every point of the unit by the same amount. Measured: P0 with a whole-curve
-10 mV moved every point by -10 mV (8x: 0.688 V to 0.678 V ... 57x: 1.429 V to 1.419 V) [MEASURED lnx 2026-10-11, tuning-results.md §5; values:
examples/reference-285k/notes/tuning-history.md §5]. It is additive to per-point offsets [MEASURED lnx 2026-10-12].
**Without an OC point the hardware drops a written target voltage** (the word `0xFEC5B439` read back as `0xFEC00039`), so a whole-curve offset on
a unit with no OC ratio is written with target 0 (D-035) [MEASURED lnx 2026-10-11]. A target kept in such a word is not reliable [MEASURED lnx 2026-10-12].

## 6. The OC point
Setting an **OC ratio** above the top fused ratio of a unit adds one extra point: on the E-cluster the table gained `P2 = 7` (at the OC ratio),
"OC point 7" [MEASURED lnx 2026-10-11]. For P-cores the community numbering is "P point 9"; on the reference chip a P OC point was not shown to appear as
a readable point 9 [UNVERIFIED for P; the ring's ninth point is MEASURED, platform-arl-s.md §3].

Measured behaviour of an E OC point (EC1, ratio 48 above the fused top 46; example values from one chip) [MEASURED lnx 2026-10-11, examples/reference-285k/notes/tuning-history.md §5]:

| Adaptive target written | Result at the OC ratio |
|---|---|
| 1.1367 V (equal to point 6) | 48x = 1.1375 V: unchanged, neutral |
| 1.147 V (+10 mV) | 48x = 1.1475 V, 47x = 1.1425 V (interpolated), 46x unchanged |
| 1.117 V (below point 6) | 48x = 47x = 1.1375 V: clamped up to point 6 |

A **P-core** OC ratio raise (58x on P0) had been written once on Windows without a recorded stability outcome [MEASURED win 2026-10-06, treated as
"written once, ungated", platform-arl-s.md §10]. The ceiling the tool allows is a policy number you can edit, up to what the CPU's own limit (mailbox command `0x01`) reports ([02](02-install.md), [12 §4](12-safety-and-internals.md), D-038, D-039).

An OC ratio cannot be cleared to 0 at runtime; a cold boot clears it [MEASURED lnx 2026-10-11, tuning-results.md].

## 7. How the CPU combines points: interpolation and the monotonic clamp
- **Between two points the voltage interpolates linearly.** At 54x, a ratio without a point, the requested voltage is the midpoint of the 53x and 55x
  points to within about 1.5 mV on every P-core [MEASURED lnx 2026-10-03; dense curve MEASURED lnx 2026-10-11]. Between the OC ratio and the next
  fused point the voltage also interpolates (E 45x: -7.4 mV of a -10 mV offset) [MEASURED lnx, platform-arl-s.md §3].
- **Above the top point, ratios ride on it.** On E-clusters without an OC point, 47x and 48x request the same voltage as the 46x point; an offset on
  point 6 moves all three [MEASURED lnx 2026-10-11].
- **Monotonic clamp.** The hardware enforces V(n) >= V(n-1). A deeper offset on a point is silently clamped up to the previous point's voltage. Example:
  a BIOS offset of -49.8 mV on a P-core's 57x point landed at -42.5 mV, clamped to the 56x point [MEASURED lnx 2026-10-02]. The clamp applies to the OC point too (the 1.117 V case
  above). The word you wrote may read back unchanged while the *voltage* is not what you asked for; that is why the app predicts the effective curve and
  why you should compare a re-measured curve ([05](05-measuring-curves.md)), not only the read-back word.
- **Some points are inert on some cores.** On the reference chip's non-favored P-cores the 57x fused voltage is about equal to the core's
  55x voltage (within 2 mV): they are flat placeholders, and those cores top out at 55x in practice [MEASURED lnx 2026-10-02, favored-cores.md §3].

## 8. Favored cores
Intel characterises each core at manufacture and fuses a ranking. Firmware publishes it as each core's `HWP_CAPABILITIES` Highest_Performance and
ACPI `_CPC` highest performance; Linux `intel_pstate` consumes it through ITMT (Turbo Boost Max 3.0)
[DOC lkml.iu.edu/hypermail/linux/kernel/1610.2/04086.html; DOC blog.linuxplumbersconf.org plumbers_fcore_v2.pdf; favored-cores.md §1].
The criterion for the ranking is not published [UNVERIFIED]. We found no evidence that a board vendor can change it [UNVERIFIED].

- The tool detects favored cores live: the P-cores with the highest `acpi_cppc/highest_perf` (fallback `cpuinfo_max_freq`), none if all are equal,
  cross-checked with mailbox command `0x1C` after a read (D-036).
- Reference chip: favored P-cores are CPU 0 and CPU 3 (HWP highest performance 92 against 88; `0x1C` fused maximum 57x against 55x)
  [MEASURED lnx 2026-09-29]. Example values from one chip.
- Favored cores reach the top ratio only with few cores active (a 57x bucket for 1-2 active cores in the turbo table, section 10). Non-favored P-cores
  top out at 55x in practice even though they carry 56x and 57x points [MEASURED lnx 2026-10-02].
- Favored does not simply mean the lowest voltage: CPU 1 and CPU 4 sat within 20 mV of CPU 0 at 53x/55x and are not favored
  [MEASURED lnx 2026-10-02, favored-cores.md §3]. The fused voltage spread is mostly guard band, not true minimum voltage: in an all-core 60-minute test
  the cores with the worst fused VID had the most undervolt room [MEASURED win 2026-10-04 / lnx 2026-10-08].

## 9. VccIA and what lowering a unit does
All units draw from VccIA ("Vcore" on the board). VccIA is always above the highest VID any unit requests [MEASURED lnx 2026-10-01].
Working model on the reference chip: board Vcore is about the **highest voltage allowed** for the current number of active cores plus roughly
100-110 mV, not the voltages actually requested [MEASURED lnx 2026-10-03, hypothesis on three or more points]. Consequences measured there:

- Points around 55x set VccIA for every multi-core load; the 57x points only for one or two active cores [MEASURED lnx 2026-10-03].
- The ring (uncore) is a floor: at 40x its requested voltage kept board Vcore near 1.30 V below about 53x allowed [MEASURED lnx 2026-10-03].
- **Lowering a unit that does not set the maximum does not lower Vcore.** Lowering the highest-voltage points lowers VccIA for the whole package
  [tuning-results.md §3 lesson 2; SPEC-v3 F6]. Judge a change by Vcore, power and temperature at a fixed, capped load, not by all-core MHz (which the
  BIOS power and thermal limits set) [tuning-results.md §3].
- The BIOS VccIA offset and load-line calibration (LLC) also move it; they are BIOS settings the app does not change.
- Voltage ceilings: the default top is the top fused VID plus about 200 mV (reference P about 1.581 V, E about 1.270 V) [COMMUNITY]; the hardware
  maximum comes from mailbox command `0x28` (about 1.625 V P, 1.355 V E on the reference chip) [MEASURED win 2026-10-01]. The policy's `"auto"`
  ceiling reads this value from your CPU.

## 10. Turbo ratio tables
Four plain MSRs hold the turbo limits [registers.md "Not the mailbox" and "MSRs"]:

| MSR | Content | Writable by the tool |
|---|---|---|
| `0x1AD` | P-core ratio for 1, 2, ... 8 active-core buckets, 8 bytes, bucket 1 in the lowest byte | yes |
| `0x1AE` | the core count of each P bucket | read-only by policy |
| `0x650` | E-core ratios by bucket, same layout | yes |
| `0x651` | the core count of each E bucket | read-only by policy |

Example values from one chip (reference 285K, stock) [MEASURED lnx 2026-09-29, =2026-10-01]: `0x1AD` = `0x3737373737373939` = ratios 57, 57, 55, 55, 55, 55, 55, 55;
`0x1AE` = `0x0807060504030201` (P buckets for 1 to 8 cores); `0x650` = `0x3030303030303030` = E 48x in every bucket; `0x651` = `0x100c080604030201`
(E buckets for 1, 2, 3, 4, 6, 8, 12, 16 cores).

- Bucket ratios must not **increase** with more active cores. The tool refuses a plan that breaks the order (`TURBO_ORDER`); every byte must be
  non-zero and within the policy ceiling, and each write is judged against the table read in the same session (policy check `turbo_ratio_bounded`).
- The tables are written as a whole 64-bit value on CPU 0 and are package-wide: a write read back identical on other CPUs [MEASURED lnx 2026-10-01].
- A warm reboot restored the E table `0x650` on the reference chip [MEASURED lnx 2026-10-01]; the tool re-applies tables as part of a profile ([09](09-profiles-and-restore.md)).
- Raising an E-core ratio **above the table's current maximum needs three things together**: the E OC word (`0x11`), the favored-core word (`0x1D`)
  and the table (`0x650`). The tool writes them in one plan. Example: E 49x was set by `0x11` + `0x1D` = 49 + `0x650` = `0x3131313131313131` and
  every E-core then ran about 4900 MHz [MEASURED lnx 2026-10-01].
- The kernel's cpufreq layer also matters: after the raise `cpuinfo_max_freq` rose from 4.9 to 5.0 GHz but `scaling_max_freq` stayed at 4.9 GHz, so
  an uncapped load still ran 48x (intel_pstate maps N to N-1). The tool lifts `scaling_max_freq` itself when you raise an OC ratio and lowers it on
  revert [MEASURED lnx 2026-10-12, G-126].
- Writing only turbo tables (P 54x or E 47x in the last bucket) made an all-core stress run exactly those ratios, and the restore brought back
  55x / 48x [MEASURED lnx 2026-10-12].

## 11. What survives a reboot
Mailbox changes are volatile. Reference chip, per boot type [MEASURED lnx 2026-10-02, win 2026-10-02/10-03, platform-arl-s.md §7]:

| Event | Result |
|---|---|
| **Cold boot / power off** | everything returns to stock: point offsets, OC words and turbo tables |
| **Warm reboot** | per-point offsets on most cores and E clusters are **kept**; the BIOS resets some state (on the reference chip: CPU 0 (OC word and point offsets), the E-core favored word `0x1D` and the E turbo table `0x650`) |

So after a *warm* reboot the CPU may hold a mix of old and reset state. The tool never assumes a state: it reads the live words and plans from them.
Re-apply (or "Restore accepted") after every boot. A cold boot is the guaranteed undo. Nothing is applied automatically at boot.

## 12. Failure modes you have to design for
- **Silent errors.** Unstable undervolts on the reference chip showed up as stress-test "CPU error detected" (wrong results) with **no** machine-check or
  WHEA event and no system error [MEASURED win 2026-10-04, tuning-results.md §1]. A clean kernel log is therefore not evidence of stability. See
  [08](08-stability-testing.md).
- **Weak tests passed what a strong test failed.** On the reference chip OCCT SSE for 10-15 minutes, a 20-minute CPU+RAM run, clock cycling and
  per-core `stress-ng --verify` sweeps all passed states that a 60-minute OCCT Linpack 2021 run failed [MEASURED win 2026-10-04].
- **E-core low points.** Offsetting the E-cluster 40x point by -20 mV caused OCCT errors on the reference chip; bisecting ruled out the ring and VccIA
  settings [MEASURED lnx 2026-10-02, platform-arl-s.md §3]. The app warns on that point (`E40_OFFSET`, labelled a community hint).
- **E-core high voltage.** E-core WHEA reports are reported above about 1.2 V [COMMUNITY skatterbencher.com/2025/02/14/285k-whea-when-e-core-voltage-over-1-2v/].
- **A failing run does not name a core.** OCCT named "core 0" when core 0 was at stock and the offset on an E-cluster was the cause: bisect by reverting
  groups of offsets [MEASURED win 2026-10-04].
- **Time to failure is noisy.** The same offsets failed after 50 minutes in one run and about 2 minutes in another: trust only a full pass of your gate,
  never how long a failure took [MEASURED win 2026-10-04].
- **Raising voltage is not free.** The tool accepts positive offsets (up to +200 mV) and OC targets up to the CPU's own maximum. More voltage means
  more heat, power and wear. The tool flags positive offsets in the review (`POSITIVE_OFFSET`).

## 13. What the tool does not control
Not written by the tool: BIOS settings (VccIA offset, load-line calibration, ring/NGU/D2D ratios, VccSA, memory), static/override voltage, the GT
(graphics) and system-agent domains, and persistent firmware variables. The GUI does not tune the ring or NGU (SPEC §2: ring/NGU writes are out of v1); the command line can write a ring V/F point offset
(`point-offset --p1 2`, [10](10-cli-reference.md)). PL1/PL2 are not in the GUI either (SPEC §2); on the reference machine Linux powercap changed them at
runtime because the lock bit was 0 [MEASURED lnx 2026-10-08, platform-arl-s.md §12].

## 14. Key facts at a glance
| Fact | Tag and source |
|---|---|
| Every mailbox request, reads included, is a WRMSR to MSR `0x150` | [DECOMPILED registers.md] registers.md "OC mailbox" |
| P-core and E-cluster have fixed fused V/F points that accept offsets only (reference: P 8, E 6) | [MEASURED lnx 2026-10-02] platform-arl-s.md §3 |
| Adaptive target voltage is active on the OC point only; it needs an OC ratio above the top fused ratio | [MEASURED lnx 2026-10-11] tuning-results.md |
| `P2 = 0` offset = whole-curve offset; without an OC point a written target is dropped | [MEASURED lnx 2026-10-11] tuning-results.md |
| Hardware clamps V(n) >= V(n-1); deeper offsets are silently raised | [MEASURED lnx 2026-10-02] platform-arl-s.md §3 |
| Between points voltage interpolates (54x = midpoint of 53x/55x) | [MEASURED lnx 2026-10-03 / 2026-10-11] |
| Board Vcore tracks the highest allowed VID (+ about 0.1 V), not the active ones | [MEASURED lnx 2026-10-03], hypothesis on 3+ points |
| Cold boot clears all runtime changes; warm reboot keeps part | [MEASURED lnx/win 2026-10-02] platform-arl-s.md §7 |
| Unstable undervolts fail silently (no WHEA/MCE); only a long Linpack-class run caught them | [MEASURED win 2026-10-04] tuning-results.md §1 |
| UVP and OC Lock must be off or writes are refused | [DECOMPILED] registers.md return codes `0x13`, `0x01` |

Next: [05 Measuring curves](05-measuring-curves.md) · Previous: [03 First run](03-first-run.md)
