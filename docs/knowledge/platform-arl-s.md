# Platform facts: Arrow Lake-S (Core Ultra 200S)

Hardware behaviour of the V/F points, voltages, turbo tables and reboot persistence. Register/command encodings are in `registers.md` (not repeated).
**Scope:** every `[MEASURED]` fact here was measured on one Core Ultra 9 285K (ASRock Z890, BIOS 3.40); the structure holds for Arrow Lake-S, the
numbers do not. Ratios marked "285K" are that SKU's values (other SKUs have other point ratios and maxima: read them live). One chip's full data:
`examples/reference-285k/notes/platform.md`.

## 1. Prerequisites for runtime writes
| Fact | Evidence |
|---|---|
| Runtime OC-mailbox writes are honoured only with the BIOS gates open: OC Lock off, Undervolt Protection off (FSP `OcLock`, `UnderVoltProtection`; flags in `registers.md`) | [DOC FSP FspmUpd.h]; [MEASURED win 2026-10-01, lnx 2026-10-01] writes accepted with both off |
| Present on ARL-S: MSR 0x150 (OC mailbox), 0x1AD/0x1AE/0x650/0x651 (turbo tables), 0x198 (VID), 0x620/0x621 (uncore ratio) | [MEASURED lnx 2026-09-29] |
| Power limits are set by the BIOS: read them at runtime (powercap / `0x610`), never assume | §12 |

## 2. Topology
| Item | Fact |
|---|---|
| CPUs | no SMT (one CPU per core); P CPUs = sysfs `devices/cpu_core/cpus`, E CPUs = `devices/cpu_atom/cpus`; CPUID leaf 0x1A gives the core type [MEASURED lnx 2026-09-29] |
| E clusters | four E-cores per cluster (`topology/cluster_cpus_list`) [MEASURED lnx 2026-09-29]; one DLVR (one voltage) per cluster [COMMUNITY SkatterBencher]; whether one core's `0x11` write moves its whole cluster is open (C-4, `mailbox-recon-summary.md` §4): XTU and this app write all four |
| Mailbox addressing | the core is selected by the CPU that executes the WRMSR (thread affinity / `/dev/cpu/N/msr`), never by P1/P2 [DECOMPILED win-xtu-decompile-mailbox.md]; [MEASURED lnx 2026-10-02] |
| Windows numbering | Windows logical-processor numbers (mailbox affinity masks, XTU logs) are NOT P-first and differ from Linux CPU numbers; within each kind the order is preserved [MEASURED win 2026-10-01, one board]. XTU's UI "P-Core N" / "E-Core k" follow the Linux order. The Windows restore derives the map live (G-138, `windows-path.md` §3) |
| Favored cores | `favored-cores.md` |

## 3. V/F points (P2 index in cmd 0x10/0x11; P2 = 0 is the OC-ratio word)
- **Structure** [MEASURED lnx 2026-10-02; COMMUNITY SkatterBencher 285K guide, "Advanced Voltage Offset" slides 39/40]: every P-core has the same fused
  point table, every E-cluster another; ring (P1 = 2) has its own. Fused points take **offsets only** (no ratio, no absolute voltage) [COMMUNITY];
  per-point offset writes are proven [MEASURED]. P2 past the
  table answers rc `0x11`. Points 10-15 are unused. 285K: P points 1-8 = 8/20/30/48/53/55/56/57x, E points 1-6 = 8/15/23/30/40/46x, ring 1-8 =
  8/14/20/28/34/37/38/39x. Read the tables live (`tune.read` walks P2 until rc `0x11`).
- **OC point** = the `P2 = 0` word: ratio + adaptive target. An OC ratio above the top fused ratio adds a point after the table (SkatterBencher: P
  point 9, E point 7): proven on the ring (40x became P2 = 9) and on E (48x became P2 = 7, readable) [MEASURED lnx 2026-10-02, 2026-10-11]. Whether a P
  OC point reads back as P2 = 9 is [UNVERIFIED].
- **Per-point offset write** = `point_ratio | offset << 21`, V target 0 [MEASURED lnx 2026-10-02].
- **Whole-curve offset:** the offset field `[31:21]` of the `P2 = 0` word shifts every point of that core/cluster, additive to the per-point offsets
  [MEASURED lnx 2026-10-11, 2026-10-12].
- **Target without an OC point:** with OC ratio <= top fused ratio a written V target is not reliably retained (dropped on one write, kept on another);
  write such words with target 0 (D-035, G-124) [MEASURED lnx 2026-10-11/12].
- **OC target below the top fused point** is clamped up to it [MEASURED lnx 2026-10-11].
- **An OC ratio cannot be written back to 0** at runtime; a cold boot clears it [MEASURED lnx 2026-10-11].
- **E-cluster clock and cpufreq caps:** the four E-cores of a cluster run one ratio: a `scaling_max_freq` cap on one E-core alone did not hold it below its
  neighbours' (one core capped at 4.7 GHz ran 48x), capping all four did (46x); on the 285K an E cap of (ratio + 1) x 100 MHz holds that ratio [MEASURED lnx 2026-10-14].
- **Neutralising a residual OC point:** rewrite the ratio the unit holds with the intended offset and an adaptive target below the top fused point voltage (plus its point offset); the clamp above makes the unit run the intended voltages, and no `0x1D` is sent, because a `0x1D` that restates a ratio below `0x1C[7:0]` lowers the unit's maximum (`cpuinfo_max_freq`) and `scaling_max_freq` stays at the old value [MEASURED lnx 2026-10-13].
- **Monotonic clamp:** the hardware enforces V(n) >= V(n-1); a deeper offset is silently clamped up (example: a -49.8 mV BIOS offset on a P 57x point
  landed at -42.5 mV, held by the 56x point) [MEASURED lnx 2026-10-02]. Check the re-measured voltage, not only the read-back word.
- **Interpolation:** a ratio between two points runs at the linear interpolation of their voltages (54x = midpoint of 53x/55x within +-2 mV on every
  P-core; E 45x got -7.4 mV of a -10 mV offset on 46x) [MEASURED lnx 2026-10-02/03/11].
- **Ratios above the top point** (turbo bins without an OC point) run on the top point's voltage, so its offset moves them too (E 47-48x follow the 46x
  point) [MEASURED lnx 2026-10-11].
- **Reachability:** non-favored P-cores carry the upper points but run no higher than the all-core turbo bucket allows; favored cores reach the top ratio
  only with 1-2 active cores [MEASURED lnx 2026-10-02, 2026-10-11]. An all-core P load is power-limited and runs between points (51-54x on the 285K).
- **E 40x point (one chip):** a -20 mV offset on the 285K's E point 5 (40x) produced OCCT errors that a bisect pinned to that offset [MEASURED lnx
  2026-10-02]. The app warns on it (`E40_OFFSET`) as a hint from one chip, not an architectural limit.

## 4. Fused top point (cmd 0x09 P1=0x11 P2=0)
Returns each core's fused top V/F point (ratio `[7:0]`, voltage `[19:8]`); distinct per physical core, so it identifies a core; it is what XTU shows as
"Voltage Override" [MEASURED win 2026-10-01 / lnx 2026-10-02]. Lower fused points are not exposed by any known command: a stock measured curve (offsets
0) is the fused table as applied. Example (285K): P-cores 1.305-1.426 V at 57x, E-clusters 1.137/1.156 V at 46x (full table in the notes).

## 5. VccIA and voltage model
- Each P-core and each E-cluster has a DLVR fed from VccIA (board "Vcore"). The VID in `0x198[47:32]`/8192 is the requested voltage; VccIA is always
  above the highest VID. No delivered (post-DLVR) voltage telemetry exists [MEASURED lnx 2026-10-01].
- VccIA ~ the highest VID *allowed* for the current active-core count (P points, E points, ring request) + ~100-110 mV, not the VIDs actually active
  [MEASURED lnx 2026-10-03, one chip, hypothesis fitted on 3+ points]. So the top points set VccIA for 1-2 active cores, the all-core points for
  multi-core loads, and lowering a unit that does not set the maximum does not lower Vcore.
- Default voltage ceiling = top fused VID + 200 mV; hardware maximum from cmd 0x28 [COMMUNITY]. E-cores report WHEA above ~1.2 V [COMMUNITY
  skatterbencher.com/2025/02/14/285k-whea-when-e-core-voltage-over-1-2v/].
- NGU/NoC and SA share the external VccSA rail (no DLVR); D2D is on VnnAON [COMMUNITY SkatterBencher].

## 6. Load-line calibration
Board-specific; no level -> mOhm table is published. On older ASRock boards Level 1 is the flattest and a higher number droops more (opposite of ASUS)
[COMMUNITY forum.asrock.com/topic7559.html]; not documented for Z890 [UNVERIFIED]; one weak measurement on the reference board agrees (notes §7).

## 7. Reboot persistence (mailbox state is volatile)
| Event | Result |
|---|---|
| Cold boot / power off | everything back to stock: point offsets 0, OC words 0 (except what the BIOS writes itself), E `0x11` words cleared [MEASURED lnx 2026-10-02, win 2026-10-02] |
| Warm reboot | per-point offsets and E `0x11` words (ratio + V target) are KEPT; whatever the BIOS programs at boot is rewritten: `0x1D`, `0x650`, and the core(s) the BIOS per-core V/F offset targets [MEASURED win 2026-10-02/03; lnx 2026-10-01/02, one board] |
Re-apply after every boot; plan from what you read, not from what you wrote last boot.

## 8. Raising an E-core ratio
- Needs the `0x11` word (ratio + target, + `0x27` granular) **and** `0x1D` = ratio **and** the E turbo table `0x650`, on every core of the cluster,
  every boot (a warm reboot restores `0x1D`/`0x650`, so the kept `0x11` word alone leaves the cores at the old ratio) [MEASURED lnx 2026-10-01].
- `0x650` is runtime-writable and package-scoped (written on CPU 0, read back equal on every CPU) [MEASURED lnx 2026-10-01].
- `0x1D` changes the HWP/CPPC highest performance: the kernel rescales capacity (`energy_model ... cpu_cap`) and raises `cpuinfo_max_freq`, but
  intel_pstate does not raise `scaling_max_freq`, so an uncapped load stays one bin lower until `scaling_max_freq` is raised (the app does it, G-126)
  [MEASURED lnx 2026-10-01, 2026-10-12]. How fast `cpuinfo_max_freq` follows is [UNVERIFIED].
- `0x1C[15:8]` (fused) is not a ratio cap on E cores: hardware accepted an E ratio above it [MEASURED win 2026-10-01, lnx 2026-10-12].

## 9. Turbo tables (plain MSRs, CPU 0, whole 64 bits RMW)
| MSR | Meaning |
|---|---|
| 0x1AD | P ratio per active-core bucket, byte 0 = bucket 1 (LSB first) |
| 0x1AE | P bucket core counts (read-only by policy) |
| 0x650 | E ratio per bucket |
| 0x651 | E bucket core counts (285K: 1,2,3,4,6,8,12,16; read-only by policy) |
Pre-checks: `0xCE[28]` = 1 (programmable ratio limits), `0xCE[24]` = 1, `0x194[20]` = 0 (OC unlocked) [MEASURED lnx 2026-10-01]. A written table takes
effect at once (lowering the 8-core / 16-core bucket made an all-core load run one bin lower) [MEASURED lnx 2026-10-12]. Bucket ratios must not
increase with the active-core count (SPEC-v3 F5). XTU: no P bucketing on ARL-S (`SupportsPCoreBucketing` false) [DECOMPILED win-xtu-decompile-mailbox.md].

## 10. Proven on hardware vs not (reference 285K)
| Control | Status |
|---|---|
| Per-point offsets P, E, ring | PROVEN on both OSes (read back, VID moved as predicted) [MEASURED 2026-10-02...12] |
| Whole-curve offset (`P2 = 0` `[31:21]`) | PROVEN on Linux [MEASURED lnx 2026-10-11/12] |
| Profile apply + word-for-word restore after boot | PROVEN on both OSes |
| E OC ratio via `0x11` + `0x1D` + `0x650`; E OC-point target V | PROVEN (E 48x/49x, short stress + one overnight OCCT) [MEASURED lnx 2026-10-01, 2026-10-12] |
| Turbo-table ratio bytes `0x1AD` / `0x650` | PROVEN (written, read back, ratio changed) [MEASURED lnx 2026-10-12] |
| P OC ratio above the top fused ratio, P V-target raise | written once on Windows (58x, 2026-10-06) and read back; no stability result; untested on Linux |
| Ring OC point / NGU / SA / GT V/F | not written (ring per-point offset only) |

## 11. Ring / NGU (community)
NGU 34x needs VccSA ~1.28-1.30 V in every stable report; expected gain a couple of ns, <= 1 % throughput [COMMUNITY skatterbencher #84/#101,
igorslab]. Ring ratio <= the max P/E ratio; no community per-point ring offset data [COMMUNITY].

## 12. Runtime power limits via powercap [MEASURED lnx 2026-10-08]
- `/sys/class/powercap/intel-rapl:0/constraint_{0,1}_power_limit_uw` change PL1/PL2 at runtime while the `0x610` lock bit (63) is 0; a PL2 write changed
  only `0x610[46:32]` (PL1, tau and enables untouched), no kernel log lines. No mailbox is needed for PL1/PL2/tau on Linux; verify by reading `0x610`.
- `intel-rapl-mmio:0` can be present but disabled (limits 0), in which case the MSR path is the one in effect. A reboot restores the BIOS value
  [UNVERIFIED].
