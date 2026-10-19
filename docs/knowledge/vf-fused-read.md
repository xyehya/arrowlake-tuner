# Reading the fused V/F curve (host: lnx/win research, 2026-10-19)

Question: can the per-point factory-fused voltages be READ from the CPU (as "Shamino's tool" / the ASUS BIOS show), instead of measuring VID under
pinned ratios (vf.measure)? Four research passes (deepseek-flash, 2026-10-19) + the linuxmon XTU decompile. Transcripts:
`~/.hermes/cache/delegation/live/deleg_eaebd562/task-{0..3}.log` (not in the repo).

## Answer
- The curve exists from reset: pCode/PCU consumes the per-core fused V/F points (P: 8 fused + OC point; E-cluster and ring their own tables).
  Physical mechanism `[UNVERIFIED]` (no public Intel doc).
- The documented/known OS interface does NOT return per-point fused voltages: cmd 0x10 P2=n gives ratio + offset, V-target 0 on fused points
  `[MEASURED lnx 2026-10-02]`; only cmd 0x09 P1=0x11 P2=0 returns a fused (top-point) voltage per core `[MEASURED win 2026-10-01]`.
- Intel documents no fused-curve read ("Overclocking is not architectural I/F" — Intel kernel maintainer `[COMMUNITY https://lwn.net/ml/linux-kernel/22617e57e541e460fac09db04fdb370f8e96e8ef.camel@linux.intel.com]`).
  FSP config carries VF offsets / ratios / count only, never fused voltages `[DOC intel/FSP TigerLake integration guide; CODE coreboot 54083]`.
- ASUS Z890 BIOS shows Frequency / VID / Offset per point (e.g. a 285K: 8x 0.725, 48x 1.051, 55x 1.196, 57x 1.313 V) `[COMMUNITY skatterbencher arl_pcore_vfc.png]`;
  the mid points moved 3-15 mV between two visits => a temperature-conditioned VID, not a raw fuse dump `[COMMUNITY same]`. Its transport is unknown.
- "Shamino's tool" = ASUS internal **OC Tool / `Tool.exe` in `OCpak`** (Peter "Shamino" Tan), via the ROG forum "ArrowLake Resources" thread (Dropbox
  `OCPak_1506.zip`); driver AsIO3.sys (MSR + I/O port), runtime-only changes, per P-core / E-cluster / ring point offsets, a "fused curve" view, a
  "proposed curve" generator `[COMMUNITY rog-forum, hardwareluxx p.450/492, overclock.net Z690 guide]`. The "Shamino work tool" mention is in
  Skatterbencher's Arrow Lake ring article, not #84 `[DOC https://skatterbencher.com/2024/10/24/arrow-lake-ring-overclocking/]`. How it reads the
  fused curve is not public `[UNVERIFIED]`. Unsigned internal binary: not to be downloaded for this project without the operator's yes.
- XTU on ARL-S shows no VF-curve page (`SupportsVFCurve=false`, `SupportsPerCoreFusedValues=false`); where it exists, its "Characterized" series is a
  scan (cap, stress, record VID) — the same technique as vf.measure `[DECOMPILED linuxmon win-xtu-decompile-controls.md:152,225-227; -mailbox.md:161]`.
- No open-source / Linux tool prints fused per-point voltages (HWiNFO, ThrottleStop, UnderVolter, intel-undervolt, CoreFreq: offsets or live VID only)
  `[CODE github wesmar/UnderVolter src/VfCurve.c; cyring/CoreFreq]`.

## Candidate reads (tested 2026-10-19, see "Result of the reads")
`[DECOMPILED linuxmon win-xtu-decompile-mailbox.md:100,101,173]`:
| request | meaning per XTU code | status |
|---|---|---|
| cmd 0x09 P1=0x11 P2=`n<<4` (core), `(n<<4)\|2` (ring) | "VoltageFloorRead" per V/F point n: `[19:8]` V-target floor, `[31:21]` offset floor | never sent on ARL-S; NOT in the allowlist |
| cmd 0x07 P1=0 P2=0 | "FusedRatioAndVoltageRead": fused P0 (top) ratio `[7:0]` + voltage `[19:8]` per core | unused on ARL-S; NOT in the allowlist |
| cmd 0x09 P1=0x11 P2=2 / 7 | ring / NGU domain floor | allowlisted, not yet measured |
Caveat: a "floor" is the UVP minimum for a V target, which on ARL-S appears to equal the fused top point; whether a per-point floor equals the fused
point voltage is exactly what a read would show (compare with vf.measure: P0 8x 0.689 / 48x 1.041 / 55x 1.271 / 57x 1.386 V `[MEASURED lnx 2026-10-19]`).
Each is a WRMSR 0x150 (read class): one transaction at a time, operator go-ahead, and the first two need an allowlist entry (policy widening = the
operator's yes). Expected benign failure: an RC (0x10 invalid param / 0x11 no such point), state untouched.

## Result of the reads `[MEASURED lnx 2026-10-19]` (captures/lnx/fused-vf-20261019.txt; installed CLI, one transaction each, cpu 0)
| request | raw word | decoded |
|---|---|---|
| 0x09 P1=0x11 P2=0 | 0x0005b439 | 57x, 1.4258 V (fused top; unchanged from 2026-10-01) |
| 0x09 P1=0x11 P2=0x10 (P point 1) | 0x00000008 | 8x, V 0 |
| 0x09 P1=0x11 P2=0x80 (P point 8) | 0x00000039 | 57x, V 0 |
| 0x09 P1=0x11 P2=0x12 (ring point 1) | 0x00000008 | 8x, V 0 |
| 0x07 P1=0 P2=0 | — | completion code 31 UnrecognizedCommand |
The per-point floor form exists and is addressed exactly as the XTU code says (each point answers its own ratio), but it carries **no voltage**;
cmd 0x07 is not implemented on ARL-S. Conclusion: no OS-visible per-point fused-voltage read via the OC mailbox on this CPU; the voltage is only in the
P2=0 (top point) word. vf.measure stays the method. MCE 0, taint 12292 (same as earlier sittings). The temporary allowlist entries were removed again
(policy restored from policy.toml.bak-fused-20261019T090226).

## Why VID under a pinned ratio != the fused point
Requested VID = base VID(ratio) + offset + AC_LL*I + AVX guardband - TVB voltage optimisation, read back after DC_LL; 0x198[47:32] scale 1/8192 V is a
convention, not in the SDM `[COMMUNITY https://skatterbencher.com/asus-v-f-point-offset/; DOC SDM vol 4]`. vf.measure is a good approximation (its 57x
point matches the 0x09 top word within a few mV), not a fuse dump.
