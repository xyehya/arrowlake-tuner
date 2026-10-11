Distilled 2026-10-08 from linuxmon@b01f78a (~/AI/linuxmon); detail and raw evidence there.
# Platform facts: Core Ultra 9 285K / ASRock Z890 Steel Legend WiFi

Tags kept from source: [MEASURED host date] [DECOMPILED file] [DOC url] [COMMUNITY url] [USER] [UNVERIFIED]. Citations `linuxmon@b01f78a:<path>`.
Register/command layouts are NOT here (see registers.md, copied separately).

## 1. Machine and BIOS
| Item | Value | Evidence |
|---|---|---|
| CPU | Core Ultra 9 285K (Arrow Lake-S), 8P + 16E, no SMT, 24 threads, CPUID 0xC0662 | [MEASURED win 2026-09-30] `linuxmon@b01f78a:docs/HARDWARE.md` |
| Board / BIOS | ASRock Z890 Steel Legend WiFi, AMI BIOS 3.40 (2026-09-15) | same |
| BIOS gates | Undervolt Protection OFF, OC Lock OFF (why runtime mailbox writes work); Secure Boot OFF (Linux) | [USER 2026-09-30] `linuxmon@b01f78a:docs/HARDWARE.md` |
| BIOS OC (accepted state) | VccIA offset -100 mV, LLC Level 6, ring 40x with -16 mV on its 39x/40x points, DDR5-7200, NGU/D2D 32x, VccSA 1.185 V fixed, TVB voltage optimizations OFF, Intel default power profile | `linuxmon@b01f78a:docs/STATE.md` |
| BIOS P-core V/F offset | per-core mode programs ONE list onto P-core 0 only; cores 1-7 stock. All-core scope had no effect. Cleared by operator since 2026-10-02 | [MEASURED lnx 2026-10-02] `linuxmon@b01f78a:docs/research/lnx-first-read.md` |
| E-cores in BIOS | no voltage / V/F options | [USER] |
| Registers present | MSR 0x150 mailbox, 0x1AD/0x1AE/0x650/0x651 turbo tables, 0x198 VID, 0x620 ring ratio | [MEASURED lnx 2026-09-29] `linuxmon@b01f78a:docs/research/lnx-turbostat-plus-facts.md` |

Power limits: **PL1 250 W** (operator, 2026-10-08, D-024; PL2 250 W per `linuxmon@b01f78a:docs/research/win-first-apply.md` 2026-10-05). Older records said 295/295 W. Read PL at runtime (powercap), don't assume.

## 2. Topology
| Item | Value |
|---|---|
| Linux CPUs | P = 0-7; E = 8-23 [MEASURED lnx 2026-09-29] |
| E clusters (one DLVR each) | 8-11, 12-15, 16-19, 20-23 |
| Favored P-cores | CPU 0 and 3 (HWP highest 92 vs 88; 0x1C = 57x; others 55x) |
| XTU naming | "P-Core N" = Linux CPU N; "E-Core k" = Linux CPU 8+k [MEASURED win 2026-10-01] |
| Windows logical processors (mailbox masks) | NOT P-first: P = LP {0,1,10,11,12,13,22,23}; E = LP {2-9, 14-21}, clusters LP 2-5 / 6-9 / 14-17 / 18-21 = Linux 8-11 / 12-15 / 16-19 / 20-23. Map is order-preserving [MEASURED win 2026-10-01] `linuxmon@b01f78a:docs/research/win-xtu-live-trace.md` S-03d |
| Address scope | mailbox selects the core by thread affinity (cmd 0x11 P1=0 P2=0), never by P1/P2; XTU writes all four cores of an E cluster [DECOMPILED win-xtu-decompile-mailbox.md]; one Linux write per CPU, sibling coalescing not isolated [UNVERIFIED] |

## 3. V/F points (P2 index in cmd 0x10/0x11; P2 = 0 is the OC-ratio word)
| Domain | Fused point ratios (P2 = 1..n) | Beyond table | Evidence |
|---|---|---|---|
| P-core (all 8 identical) | 8, 20, 30, 48, 53, 55, 56, 57 (n = 8) | P2 = 9 -> rc 0x11 | [MEASURED lnx 2026-10-02] vf-dump of all cores |
| E-cluster | 8, 15, 23, 30, 40, 46 (n = 6) | P2 = 7 -> rc 0x11 | same |
| Ring | 8, 14, 20, 28, 34, 37, 38, 39 (n = 8) | P2 = 9 (40x) exists ONLY while ring OC ratio = 40x | [MEASURED lnx 2026-10-02] `linuxmon@b01f78a:docs/research/skatterbencher-285k.md` s8 |
- An OC ratio above the top fused ratio adds a point (ring proven; P 58x untested for point count) [MEASURED lnx 2026-10-02].
- **Community naming agrees** [COMMUNITY SkatterBencher 285K guide, "Advanced Voltage Offset" slides 39/40, operator-supplied 2026-10-11]: P V/F points 1–8 =
  8/20/30/48/53/55/56/57×, E points 1–6 = 8/15/23/30/40/46×, all factory-fused, offset only, no ratio control; **P point 9 / E point 7 = OC point**
  (user-adjustable base voltage, offset & direct, ratio control); points 10–15 unused. Ours: at stock P2 = 9 / 7 answer rc 0x11; the OC point's ratio
  and adaptive target are written through the `P2 = 0` word. Whether a P/E OC point is then *readable* as P2 = 9 / 7 is [UNVERIFIED] (ring: yes).
- Word: ratio [7:0], V target [19:8] /1024 V, mode [20] (0 adaptive), signed offset [31:21] /1024 V (raw -51 = -49.8 mV). Per-point offset write = `point_ratio | offset<<21`, V target 0 [MEASURED lnx 2026-10-02].
- Monotonic rule: hardware enforces V(n) >= V(n-1); a deeper offset is silently clamped up (P0 BIOS 57x -49.8 mV landed at -42.5 mV, clamped to 56x) [MEASURED lnx 2026-10-02].
- Between OC ratio and next fused point voltage interpolates linearly (E 45x: -7.4 of -10 mV) [MEASURED].
- 54x has no point: VID = midpoint of 53x/55x points (to +-2 mV) [MEASURED lnx 2026-10-03]. 52x: not pinnable via cpufreq (caps land on 51/53) [MEASURED lnx 2026-10-02].
- Non-favored P-cores top out at 55x in practice though they carry the 57x point. All-core P load runs 51-54x (power limited), i.e. the 48->53x blend [MEASURED lnx 2026-10-02].
- E 46x point offset also moves 47-48x (stock E OC word target 0 tracks point 6). Never offset E point 5 (40x): -20 mV there caused OCCT errors [MEASURED lnx 2026-10-02].

## 4. Fused point words (cmd 0x09 P1=0x11) = identity check and "Voltage Override" in XTU
`FUSED_POINT` in `linuxmon@b01f78a:src/linuxmon/msr/measured_baseline.py` [MEASURED win 2026-10-01 / lnx 2026-10-02]. Distinct per physical P-core, so it identifies which LP is which core.
| CPU | word | ratio | V (1/1024) | CPU | word | V |
|---|---|---|---|---|---|---|
| 0 | 0x5B439 | 57 | 1.4258 | 4 | 0x54439 | 1.3164 |
| 1 | 0x53839 | 57 | 1.3047 | 5 | 0x56C39 | 1.3555 |
| 2 | 0x57439 | 57 | 1.3633 | 6 | 0x56039 | 1.3438 |
| 3 | 0x59439 | 57 | 1.3945 | 7 | 0x58039 | 1.3750 |
| 8-11 | 0x4A02E | 46 | 1.1563 | 12-23 | 0x48C2E | 1.1367 |
Other baselines: 0x10 P2=0 = 0x39 on CPU 0, 0 elsewhere; 0x14 = 0x8 (PerCore Voltage Enable set); 0x26 = 3; 0x28 max-V raw P 0x681 (~1.625 V), E 0x56D (~1.355 V); 0x1C P favored 0x3939 / other 0x3737 / E 0x2E30; GT 0x10 = 0xF8600000 (-59.6 mV); 0x10 ring 0x28, NGU 0x20.
Stock VIDs at 48x/53x/55x differ per core (e.g. CPU 4: 1.0334/1.2262/1.3187 V; CPU 0 57x 1.4288 V stock) - table in `linuxmon@b01f78a:docs/research/lnx-first-read.md` (vf-dump --measure).

## 5. VccIA and voltage model
- Each P-core and E-cluster has a DLVR fed from VccIA (board "Vcore"). VID (0x198[47:32]/8192) ~ voltage delivered to the core; VccIA is always above the highest VID [MEASURED lnx 2026-10-01, USER analysis] `linuxmon@b01f78a:docs/research/skatterbencher-285k.md` s8. No delivered-voltage telemetry exists (VIDs are requests).
- VccIA ~ highest VID ALLOWED for the current active-core count + ~100-110 mV, not the active VIDs [MEASURED lnx 2026-10-03, hypothesis on 3+ points]. Board Vcore: 53x cap 1.312-1.328, 54x cap 1.344-1.360, 55x 8P or 295 W all-core 1.392 V (BIOS VccIA -75/-100, LLC 6/7).
- 55x points set VccIA for every multi-core load; 57x only for 1-2 active cores. Ring (40x ~1.201 V UncVID) is the floor: below ~53x allowed, Vcore ~1.30 V [MEASURED lnx 2026-10-03].
- Ring request counts toward VccIA; ring left at 1.2x V target once raised VccIA under all-core load (Vcore 1.408 -> 1.376 after ring change) [MEASURED lnx 2026-10-02].
- Voltage ceilings: default = lowest top-point VID +200 mV (P ~1.581 V, E ~1.270 V); hardware max via cmd 0x28 above. E-core WHEA reports above ~1.2 V [COMMUNITY skatterbencher.com/2025/02/14/285k-whea-when-e-core-voltage-over-1-2v/].
- NGU/NoC and SA share external VccSA (no DLVR); D2D on VnnAON [COMMUNITY] `linuxmon@b01f78a:docs/research/arl-voltage-topology.md`.

## 6. LLC (ASRock)
- Older ASRock boards: Level 1 = flattest, higher number droops more (opposite of ASUS) [COMMUNITY forum.asrock.com/topic7559.html]. NOT documented for Z890 [UNVERIFIED]. No level -> mOhm table exists. `linuxmon@b01f78a:docs/research/asrock-llc.md`.
- Measured 6 -> 7: ~8 mV more droop at ~67 A (8P 53x cap), below the 16 mV board-sensor step [MEASURED lnx 2026-10-03, weak]; consistent with "higher = droopier". Operator reverted to 6 because p-step7 was validated there (D-019).

## 7. Reboot persistence (mailbox state is volatile)
| Event | Result | Evidence |
|---|---|---|
| Cold boot / power off | everything stock: P points 08 14 1e 30 35 37 38 39; E 08 0f 17 1e 28 2e; OC words 0 (CPU 0 = 0x39); E 0x11 words cleared | [MEASURED lnx 2026-10-02, win 2026-10-02] |
| Warm reboot | per-point offsets KEPT on CPUs 1-7 and all E clusters; CPU 0 reset by BIOS (OC 0x39, points 0); E 0x11 words (ratio+V target) kept | [MEASURED win 2026-10-02/10-03; lnx 2026-10-01/02] `linuxmon@b01f78a:docs/research/lnx-first-read.md`, `linuxmon@b01f78a:docs/research/win-first-apply.md` |
| Warm reboot, E 0x1D / 0x650 / P0 0x11 word | restored by BIOS (0x1D back to 48, 0x650 back to 48x8, P0 0x5B439 -> 0x39; P1-7 point words are kept, see above) | [MEASURED lnx 2026-10-01] |
Earlier note "reboots clear point offsets" holds for cold boot only. Re-apply after EVERY boot; the planner steps from what it reads.

## 8. E ratio 49 and 0x650 evidence [MEASURED lnx 2026-10-01]
- XTU set E 49x + V target; after warm reboot the 0x11 word (0x0004A931, 1.165 V) persisted but E ran 48x because 0x1D and 0x650 were BIOS-restored.
- Linux apply: 0x11 + 0x1D=49 + 0x27=3 on 16 E-cores plus plain `wrmsr 0x650 = 0x3131313131313131` on CPU 0 accepted, read back identical on CPU 0 and 8: the E turbo table is runtime-writable and package-scoped. All 16 E-cores reached ~4900 MHz; 1-min stress-ng --verify passed; kernel log showed `energy_model` capacity 756 -> 768 (0x1D changes HWP/CPPC highest perf). Raising E needs 0x11 + 0x1D (+0x27) AND 0x650, every boot.
- Overnight unprivileged OCCT passed at that state. Later accepted state runs E at stock 48x (E only tuned via 46x offsets).

## 9. Turbo tables (plain MSRs, CPU 0, whole 64 bits RMW)
| MSR | Stock value [MEASURED lnx 2026-09-29, =2026-10-01] | Meaning |
|---|---|---|
| 0x1AD | 0x3737373737373939 | P ratio by active-core bucket 1..8, LSB first: 57,57,55,55,55,55,55,55 |
| 0x1AE | 0x0807060504030201 | P bucket core counts 1..8 (read-only by policy) |
| 0x650 | 0x3030303030303030 | E ratios 48 x8 |
| 0x651 | 0x100c080604030201 | E bucket counts 1,2,3,4,6,8,12,16 (read-only by policy) |
Pre-checks: 0xCE[28]=1, 0xCE[24]=1, 0x194[20]=0 (OC unlocked) [MEASURED lnx 2026-10-01]. XTU: NoBuckets for P on ARL-S (SupportsPCoreBucketing false) [DECOMPILED win-xtu-decompile-mailbox.md]. Operator once set 54x for 3-8 active (BIOS): no change in power/temp, reverted.

## 10. Proven on hardware vs not
| Control | Status |
|---|---|
| Per-point offsets P (1-8), E (1-6), ring point 8 | PROVEN, both OSes (p-step7) |
| Profile apply + word-for-word restore after boot | PROVEN, both OSes |
| E OC ratio via 0x11 + 0x1D + 0x650; E OC-point target V | PROVEN once (E 49x, 2026-10-01, 1-min stress + overnight OCCT) |
| P OC ratio, 0x1AD, P V-target raise | draft `linuxmon@b01f78a:docs/SPEC-gui.md` s5 says NOT written. CONFLICT: `linuxmon@b01f78a:docs/research/win-first-apply.md` records a Windows test 2026-10-06 (P0 58x, V target 1429.69 mV, 0x1D=0x393A, 0x1AD -> 0x373737373737393A) applied and read back equal on CPU 0 and 8; no stability outcome recorded and not in the accepted state. Treat as "written once, ungated" |
| Ring 9th point / NGU / SA / GT V/F | not written (ring offset point 8 written on lnx 2026-10-02) |

## 11. NGU / ring: skipped, why
- NGU 34x: every stable report uses VccSA 1.28-1.30 V; one sample failed at 1.25 V; operator cap VccSA <= 1.2 V (200S Boost limit) -> 34x unlikely; 33x only realistic test; expected gain a couple of ns, <= 1 % throughput [COMMUNITY skatterbencher #84/#101, igorslab] `linuxmon@b01f78a:docs/research/ngu-vccsa.md`. NGU 32x/D2D 32x kept.
- Ring: 40x kept; a ring undervolt saves only ring power when P 55x is allowed and "never passed a test in isolation here"; no community per-point ring offset data; ring <= max P/E ratio [COMMUNITY] `linuxmon@b01f78a:docs/research/ring-community.md`.

## 12. Memory baseline (DDR5-7200, NGU/D2D 32x, SA 1.185 V) [MEASURED lnx 2026-10-03] `linuxmon@b01f78a:docs/research/lnx-llama-bench.md`
- stress-ng --stream via perf uncore_imc: 8 P-cores 95 GB/s, 16 E 99, all 24 100 GB/s. AIDA64 read (win) 112 GB/s; theoretical 115.2 GB/s.
- Intel MLC 3.13 (unprivileged, -e -r): idle 63.7 ns default, 50.3 ns from P0, 77.3 ns from E8; peak reads 109.6 GB/s all cores, 111.2 GB/s with P-cores only; loaded-latency knee ~100-108 GB/s; L2->L2 HIT 26.4 ns, HITM 32.9 ns.
- RAM was 6800 in early notes; corrected to 7200 (operator, Geekbench sysinfo).
## 13. llama.cpp findings (RTX 5080 16 GB, P-cores 0-7 pinned) [MEASURED lnx 2026-10-03]
- Dense Qwen3.8-27B Q3: unified memory ON 85.6 t/s TG vs OFF 11.8 (7x; UM lets --fit put all layers on GPU). MoE Qwen3.6-35B-A3B Q8: UM OFF 42 t/s TG vs ON 13 (3.2x; UM ON pages experts over PCIe).
- Rule: UM ON for dense, OFF for MoE. MoE-noUM sits at ~33 GB/s (not bandwidth-bound; CPU<->GPU hand-off latency) -> where NGU latency could show. 8 P-cores reach ~95 % of package bandwidth.

## Runtime PL2 via powercap [MEASURED lnx 2026-10-08]
- Baseline `0x610` = `0x004287d000df87d0`: PL1 250 W en, clamp, tau 56.0 s · PL2 250 W en · **lock 0**. `intel-rapl-mmio:0` present but disabled
  (limits 0; peak_power 425 W) ⇒ the MSR path is the one in effect.
- `echo 295000000 > /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw` ⇒ `0x610` = `0x0042893800df87d0`: **PL2 295 W**, only bits
  [46:32] changed; PL1/tau/enables untouched; no kernel log lines. Revert: write `250000000`; a reboot restores the BIOS value `[UNVERIFIED]`.
- ⇒ PL1/PL2/tau need no mailbox on Linux while the lock bit is 0; the GUI can use powercap (root) and verify by reading `0x610`.
