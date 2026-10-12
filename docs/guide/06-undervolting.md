# 06 Undervolting

This page is the workflow for lowering voltage with the **Tune** tab: what an undervolt is on Arrow Lake, how to apply one step by step, what the
guard and the hardware do to your values, and what was learned on the reference chip. It is for someone who has measured a stock curve
([05](05-measuring-curves.md)) and read the [primer](04-arrow-lake-vf-primer.md).

**Danger.** Too low a voltage causes crashes, or worse, silent wrong results with no error message. Change one thing at a time, in small steps, and
stress-test each step ([08](08-stability-testing.md)). A cold boot reverts everything the tool wrote.

## 1. What an undervolt is here
On Arrow Lake the fused V/F points accept a voltage **offset** only ([04 §5](04-arrow-lake-vf-primer.md)). An undervolt is therefore a negative
offset on a point, or on the whole curve. There are two ways to enter it:

| Control | Effect | Use it for |
|---|---|---|
| **Whole Curve Offset** | one offset applied to every point of the unit | a first, coarse step; also lowers the points you never run |
| **Per V/F point Offsets** | an offset per fused point (P-core: 8 points; E-cluster: 6 on the reference chip) | shaping the curve where it matters (all-core clocks, the top points) |

The default range is -200 ... +200 mV in 5 mV steps; values snap to the hardware's 1/1024 V (0.977 mV) grid and the exact millivolts are
shown. **That range is your policy, not a built-in limit** (D-039): it is `offset_ceiling_mv` / `effective_offset_floor_mv` in `/etc/arltuner/policy.toml`, editable as root up to the
register field's own range of -1000 ... +999 mV. The page reads the limits live from the helper and says under each control "from your policy (...) - editable; your CPU allows ..."; **System → Tuning
limits** lists them all ([12 §4](12-safety-and-internals.md)). A BIOS per-core offset, if you left one set, adds to what you write.

**Scope.** **All-Core** shows two rows ("All P-cores", "All E-clusters") and writes the same values to every unit of that kind. **Per-Core** shows one
row per unit (every P-core, every E-cluster) so each can differ. Silicon varies per core: on the reference chip the 55x voltage differs by 77 mV
between P0 and P7 [MEASURED lnx 2026-10-11], so the right offset differs too.

## 2. Before you start
- [ ] BIOS: Undervolt Protection and OC Lock off ([01](01-requirements.md)); no BIOS per-core offsets you have forgotten about.
- [ ] A **stock baseline** curve is stored ([05](05-measuring-curves.md)). Without it Tune warns `no baseline` and cannot predict voltages.
- [ ] You know your gate: the test you will call "stable" (the project's is 60 minutes of OCCT Linpack plus 60 minutes of OCCT SSE; [08](08-stability-testing.md)).
- [ ] No other tool is using the mailbox (OCCT only unprivileged, HWiNFO closed).
- [ ] You are at the machine. If it hangs, power-cycle it; the tuning is gone after a cold boot.

## 3. Step by step
1. **Read.** In Tune press **Read ratios & voltages**. The tool reads the CPU's words (about 140 mailbox reads on the 12-unit reference chip: one per
   V/F point of each unit, plus the fused-point reads, the favored-ratio reads, and the CPU's own limits `0x01` and `0x28`; the page computes the estimate from your unit list and shows
   the exact count when the read ends). Each request is a WRMSR, so the card shows how many operations this boot has used. The page keeps the last read of this boot and shows when it was made; **Show raw words** reveals the hex words.
   The **applied** chip says `stock`, `words` or `unknown` for the state the CPU is in.
2. **Choose a unit and one point.** Start small: one core, one point, -10 to -20 mV. Pick **Per-Core**, pick the unit, switch its offset control to
   **Per V/F point Offsets**, and set one point. The preview chart overlays the predicted curve on your baseline, marked as a prediction.
   Points that are fused but above what a core runs (for example 56x/57x on a non-favored P-core) are tagged "above 55x limit" (the number is that core's own limit; the tooltip has the full sentence) and get no predicted voltage.
3. **Review changes.** The dialog shows, per unit and point, before and after (millivolts and volts), the number of operations, and warnings. Read them:

   | Warning | Meaning |
   |---|---|
   | `clamped` | the monotonic clamp will raise this point to the previous point's voltage; the effective change is smaller than you typed ([04 §7](04-arrow-lake-vf-primer.md)) |
   | `positive offset` | you are raising voltage |
   | `no baseline` | no stock baseline curve; predictions are limited |
   | `E 40x offset (community hint)` | a negative offset on an E-cluster's 40x point; community and reference-chip reports of errors there [MEASURED lnx 2026-10-02] |
   | `WHEA risk (community hint)` | an E-core voltage above about 1.2 V [COMMUNITY] |
   | `turbo raise` | the plan changes a turbo table upwards |

   The dialog also shows the **predicted VccIA** and which unit sets it ("Highest predicted VID" card). Lowering a unit that does not set the maximum
   does not lower board Vcore ([04 §9](04-arrow-lake-vf-primer.md)).
4. **Apply.** Press **Apply** (it is enabled only when something would change and the plan has not expired; a plan expires after 10 minutes). The
   confirmation code is bound to this exact plan, single-use and checked by the helper. The tool sends the writes one transaction at a time and reads
   every word back. The result table lists each word expected against actual with a match flag.
5. **Verify.** Check the read-back. A word that matches does not prove the voltage: if the clamp applied, the voltage differs from your request.
   Use **Measure again** (the dialog offers it, with the changed units preselected, Quick) and compare to the baseline with **Δ vs baseline**.
6. **Stress.** Use **Validate with a stress screen** (opens Stress with the changed units selected) for a quick check, then your real gate.
7. **Repeat** with one more point or a slightly deeper value. Only after a full gate pass do you keep it ([09](09-profiles-and-restore.md)).

**Undo.** **Reset offsets to 0** sets every offset to 0 mV through the same review-and-apply flow; it leaves the OC ratio, the OC target voltage and the
turbo tables alone. **Discard changes** drops unapplied choices. A **cold boot** removes everything.

## 4. What the guard and the hardware do to your numbers
| Mechanism | Effect |
|---|---|
| Policy range | an offset outside -200 ... +200 mV is refused (`POLICY_RANGE`), naming the field and limit |
| Step limits | per-write steps are at the code maxima by default (offset 200 mV); a custom policy can reduce them, and a plan that needs more steps is refused (`STEP_LIMIT`) |
| Monotonic clamp | the hardware raises a too-deep point to the previous point's voltage ([04 §7](04-arrow-lake-vf-primer.md)) |
| Whole-curve offset on a unit with no OC point | written with a zero target voltage, because the hardware drops a target there (D-035) [MEASURED lnx 2026-10-11] |
| Interpolation | ratios between points follow the neighbours: lowering the 55x point also lowers 54x [MEASURED lnx 2026-10-11] |
| Everything above the top point | ratios above the last point ride on it (E 47x and 48x follow the 46x point) [MEASURED lnx 2026-10-11] |
| Read-back | every written word is read back; a mismatch stops the plan |

## 5. Strategy facts from the reference chip
All numbers here are **example values from one Core Ultra 9 285K**. They show how a tuning was built and what failed, not what your chip accepts.

- **Passing state** (called p-step7, accepted 2026-10-04 on Windows and 2026-10-07/08 on Linux) [MEASURED win 2026-10-04, lnx 2026-10-07/08,
  examples/reference-285k/notes/tuning-history.md §1-2]: 48x left at stock on all P-cores; 53x at -50 mV on all P-cores; 55x set by a *target voltage* per core (about 1.27-1.29 V; the
  offsets ranged from -28 to -91 mV, deepest on the cores with the highest stock voltage); the favored cores' 56x/57x set by target (P0 1.360/1.385 V,
  P3 1.330/1.365 V); E-clusters 46x point at -35 mV (cluster 0) and -25 mV (clusters 1-3), the 40x point left at stock. The gate was 60 minutes OCCT Linpack
  2021 plus 60 minutes OCCT SSE, no MCE/WHEA.
- **What failed first.** An earlier accepted state (p-step4: 53x about -55 to -65 mV on some cores, other 53x/55x offsets) passed shorter tests (SSE 10-15
  minutes, CPU+RAM 20 minutes, clock cycling, per-core `stress-ng --verify`) but later failed OCCT Linpack with "CPU error detected" and no WHEA, no MCE.
  Deeper variants (53x about -90 mV on four cores) failed within minutes [MEASURED win 2026-10-04, examples/reference-285k/notes/tuning-history.md §3]. Only the long gate separated
  the states.
- **Where margin returned.** Backing off the deepest offsets where all-core AVX loads run (P-cores at 51-52x = the blend between the 48x and 53x
  points) and capping 55x by voltage instead of offset fixed it [MEASURED win 2026-10-04].
- **Why it works.** The factory voltage spread across cores is mostly guard band, so the cores with the worst stock VID had the most room
  [MEASURED win 2026-10-04 / lnx 2026-10-08, favored-cores.md §3].
- **E-cores.** Only the 46x point was offset on the reference chip; an offset on the 40x point produced errors [MEASURED lnx 2026-10-02].
- **Heat and power.** Judge a change by Vcore, watts and temperature at a fixed capped load, not by all-core MHz, which the BIOS power and thermal limits
  decide [tuning-results.md §3].

## 6. Common mistakes
- Typing several changes at once: if the test fails you cannot tell which one caused it. One variable at a time.
- Trusting a short pass, or a clean `dmesg`. Unstable undervolts here left no WHEA/MCE ([08](08-stability-testing.md)).
- Tuning the unit that is not the problem: lowering a low-voltage point does not lower Vcore if another unit sets the maximum.
- Forgetting the BIOS: a stale BIOS per-core offset or a changed load-line calibration moves your numbers.
- Assuming the offsets will survive a reboot. After a cold boot the CPU is stock; after a warm reboot it may be a mix ([04 §11](04-arrow-lake-vf-primer.md)).

Next: [07 Overclocking](07-overclocking.md) · Previous: [05 Measuring curves](05-measuring-curves.md)
