# Favored P-cores: how they are chosen, and how to bin the other cores

Sources: desk research 2026-10-08 (Sonnet worker, web only; curated), plus our own data in `platform-285k.md` §2–4 and `tuning-results.md` §1.

## 1. How favored cores are designated
- Intel characterizes each core at sort/class test and **fuses** the result. Firmware/pcode publishes it per core as HWP_CAPABILITIES
  Highest_Performance and ACPI `_CPC` highest perf. Linux `intel_pstate` then calls `sched_set_itmt_core_prio()` (ITMT, Turbo Boost Max 3.0).
  The OS only consumes the ranking. [DOC https://blog.linuxplumbersconf.org/2016/ocw/system/presentations/3813/original/plumbers_fcore_v2.pdf]
  [DOC https://lkml.iu.edu/hypermail/linux/kernel/1610.2/04086.html]
- Patent US10922143B2: fuses "programmed at manufacturing time" mark per-core attributes. A favored core can run a higher frequency and/or a lower
  voltage. [DOC https://patents.google.com/patent/US10922143B2/en]
- Patent US12073227B2: per-core Vmin is fused in high-volume manufacturing, and pcode/BIOS ranks the cores (an efficiency criterion, not TBM3's).
  [DOC https://patents.google.com/patent/US12073227B2/en]
- We found no evidence that a board vendor can re-designate the favored cores. They expose only per-core ratio limits and V/F offsets.
  [UNVERIFIED]
- The ARL ranking criterion (Fmax at a V ceiling, VID, or Vmin) is not published anywhere we found. [UNVERIFIED]
- On our chip the favored cores are CPU 0 and CPU 3. Evidence: HWP highest perf 92 vs 88; `0x1C` fused max ratio 57x vs 55x. [MEASURED lnx 2026-09-29]

## 2. What our data says about the criterion
Stock VIDs (V) [MEASURED lnx 2026-10-02, `lnx-first-read.md` vf-dump --measure; CPU 0 without BIOS offsets]:

| CPU | 53x | 55x | fused 57x word | p-step7 55x headroom to ~1.27–1.29 V |
|---|---|---|---|---|
| 0* | 1.216 | 1.299 | 1.426 | −28 |
| 1 | 1.221 | 1.306 | 1.305 | −36 |
| 4 | 1.226 | 1.319 | 1.316 | −33 |
| 6 | 1.249 | – | 1.344 | −61 |
| 5 | 1.261 | 1.359 | 1.355 | −73 |
| 2 | 1.271 | 1.366 | 1.363 | −81 |
| 7 | – | 1.376 | 1.375 | −91 |
| 3* | – | – | 1.395 | −28 |

- **Favored ≠ simply the lowest VID at a common point.** CPU 1 and CPU 4 are within 20 mV of CPU 0 at 53x/55x and are not favored.
- **On non-favored cores the 57x fused word ≈ the 55x stock VID (within 2 mV).** The "57x point" is therefore a flat placeholder that no test
  characterized; those cores were never validated above 55x. On the favored cores the 55→57x slope is real (CPU 0: +127 mV). This refines the
  community lead that non-favored top points are "pre-defined from the favored cores" [COMMUNITY https://skatterbencher.com/2025/03/16/skatterbencher-84-core-ultra-9-285k-overclocked-to-5800-mhz/].
  On our dCode they are not copies: each sits at its own 55x VID. [MEASURED + inference]
- Working hypothesis: favored = the cores that passed a 57x Fmax test within the voltage ceiling, and VID is a by-product. [UNVERIFIED]
- **Fused VID spread is mostly guard band, not true Vmin.** In the p-step7 gate (all-core, 60 min) every core held 55x at ~1.27–1.29 V, so the
  cores with the worst fused VID had the most undervolt room [MEASURED win 2026-10-04 / lnx 2026-10-08]. Caveat: that test was all-core, and OCCT
  does not name the failing core, so per-core Vmin is still unmeasured.

## 3. Per-core bin metrics (what to measure)
| Metric | How | Indicates |
|---|---|---|
| M1 fused/stock VID per point | `vf-dump --measure` (read-only) | the factory's view, guard band included |
| M2 Vmin @ fixed ratio (53x, 55x) | 1T self-checking load pinned to one core; step that core's point offset down | real voltage margin (the main bin metric) |
| M3 Fmax @ fixed V (e.g. OC ratio 56/57x at 1.35 V target) | per-core OC ratio + V target, 1T load | frequency headroom; the TBM3-style criterion |
| M4 margin = M1 − M2 | derived | how much guard band the fuse carried |
| M5 ΔT and PP0 W at fixed ratio+VID | 1T at a pinned cap; `0x19C` per-core temp, RAPL PP0 | leakage/thermal position (TVB ~1.1 mV/°C [COMMUNITY]) |
| HWP highest / `0x1C` | read-only | the OS-visible ranking only |

## 4. Test protocol (Linux; writes are orchestrator-only, each session needs a go-ahead, per SAFETY.md)
1. Baseline: stock state for the tested point (or p-step7), health gate, `vf-dump --measure`.
2. Isolate: run the load with `taskset -c N` on one physical P-core. Leave the others idle and stock. Each P-core has its own DLVR, so one
   core's offset does not move the others. Fix the ratio with cpufreq `scaling_max_freq` (P caps exact at 48/53/55x; 52x not pinnable).
3. Loads must self-check, because ARL failures are silent wrong results with no WHEA or MCE: y-cruncher 1T, Prime95 small-FFT 1 worker,
   Linpack 1T residual check, `stress-ng --verify`. Rotate them, because single-thread instability shows up under different loads.
4. M2 sweep, per core: step that core's point offset −5 mV per write (guard limit ≤55 mV per write, floor −100). Run 10 min per step. At the
   first error, back off +10 mV and confirm with 60 min. Record Vmin, the load that failed, and the temperature.
5. M3 (optional, raises V/ratio, so needs a separate go-ahead and a ceiling ≤ the favored cores' fused 57x VID): set the OC ratio to 56x, then 57x,
   at a fixed target voltage. Pass/fail per core with the same loads.
6. M5: at a fixed cap and VID on each core, record steady-state core temp and PP0 power. Same fan curve, same ambient.
7. Rank by M2 at 55x (primary), then M3, then M5. Re-run M2 on the top 2 cores after a cold boot. Restore p-step7 and confirm readback.

## 5. Open questions
- Does a 58x OC ratio add a P point (ring did at 40x)? Does a non-favored core accept 56/57x at all?
- Where does HWiNFO's "Favored Cores List" come from? ASRock CPU Indicator formula?
- Aging: favored cores spend the most time at the highest VID. Intel says ARL is not affected by the RPL Vmin shift [COMMUNITY https://hothardware.com/news/arrow-lake-and-mobile-immune-crashing-issue]. Not confirmed independently.
