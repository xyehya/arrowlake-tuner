# Intel XTU 10 for Core Ultra (Arrow Lake) — UI and control reference

Host: lnx (web research) · collected 2026-10-18 · images in `xtu-img/` (local only, gitignored — third-party screenshots, never commit).

## Version facts
- Current: **XTU 10.0.1.188**, released 2026-08-04; supports Arrow Lake-S and Arrow Lake-HX (incl. 200S Plus: 270K Plus, 250K Plus) only.
  14th gen and older stay on 7.14.2.93. [DOC https://www.intel.com/content/www/us/en/download/17881/intel-extreme-tuning-utility-intel-xtu.html]
- 10.0.1.188 changes: XTU service starts after IPF via SCM dependency (fixes reboot crashes), installs without IPF but warns,
  "minimize on startup" to tray, telemetry artifact 3.1.207. Requires Intel IPF >= 2.2.10200.11; System Agent voltage monitor unavailable.
  [DOC https://downloadmirror.intel.com/873886/XTU-10.0.1.188-ReleaseNotes.pdf]
- 10.0 line (since 10.0.0.76, 2024-10-14) — the feature set that defines "new XTU for Core Ultra":
  [COMMUNITY https://wccftech.com/intel-extreme-tuning-utility-xtu-v10-tuning-overclocking-options-core-ultra-200s-cpus/,
   https://news.updatestar.com/post/intel-extreme-tuning-utility-10-0-1-188-released-deeper-arrow-lake-overclocking-controls]
  - legacy XTU driver replaced by Intel Innovation Platform Framework (IPF)
  - SA Fabric controls; Dual Reference Clock controls (SoC BCLK and compute BCLK separately)
  - **granular per-core ratios** (sub-integer ratio steps; known issue: may round up after reboot)
  - **Maximum Voltage** control
  - per-point real-time memory timings
  - **Core Parking** controls
  - **active-core and per-core ratios independent**; effective ratio = the lower of the two
  - "Default" removed from Voltage Override: the true value is always shown, even at boot
  - **OC TVB per core on E-cores** too
- Not on Arrow Lake: AI Assist (14900K/KF/KS only) [DOC https://www.intel.com/content/www/us/en/support/articles/000096983/processors.html];
  the VF Curve tab/scan is gone; individual VF point offsets remain in Advanced Tuning unless undervolt protection is on
  [DOC https://www.intel.com/content/www/us/en/support/articles/000093744/processors/processor-utilities-and-programs.html].
  Intel 200S Boost (a BIOS profile) greys out Advanced Tuning [DOC .../000090690/...].

## UI structure (from screenshots `intel-93744-2.png`, `xtu10-percore-crop.png`)
- **Left nav:** System Information · Speed Optimizer · Advanced Tuning · (VF Curve, removed) · Stress Test · Benchmarking · Profiles ·
  Profile Pairing.
- **Advanced Tuning category tabs:** All · Core · Cache · Graphics · Per Point Voltage Offset · XMP · Other.
- **Control idiom** (every control the same): label naming the operating point ("Core VF Point 3 Voltage Offset (at 36 x)"), an (i)
  tooltip, a slider, and an editable value box with unit ("0.000 V"). Steppers `< 55x >` on ratio fields. Changed values are pending
  until Apply.
- **Active Cores table:** rows "1 Active Core" .. "8 Active Cores", each = ratio (e.g. 52x) + TVB columns: Temperature (°C) /
  Ratio Offset (Disable / -N bins), and a second Temperature #2 / Ratio Offset #2 pair.
- **Performance Per-Core Tuning:** grid of tiles "P-Core 0..7", favored cores marked ★, each tile shows its ratio with `<` `>` steppers.
  Toggle Ratio / Monitors (tile shows live readings instead).
- **Selected Core panel** (click a tile): Ratio Multiplier · Voltage Mode (Adaptive / Override) · Voltage Override · Voltage Offset ·
  Intel Thermal Velocity Boost: Temperature, Ratio Offset, Temperature #2, Ratio Offset #2.
- **Efficient Per-Core Tuning:** same tile grid for E-Core 0..15 (green), same Selected Core panel.
- **Action bar:** Apply · Discard · Refresh (bottom). Save as profile.
- **Monitoring strip** (always visible, bottom): live graph with checkbox series (Package Temperature, CPU Utilization, ...), and a
  readout column (CPU Utilization, Processor Graphics frequency, Package power, max core frequency, throttling flags).

## XTU control catalogue relevant to us (Core / Cache / Other) [COMMUNITY + DOC above; exact names UNVERIFIED where not in a screenshot]
| Area | XTU control |
|---|---|
| Ratios | Active-core turbo ratio table (P, E); per-core ratio (granular); E-cluster ratio; Ring/Cache min-max ratio; NGU / D2D ratio |
| Voltage | per-domain Voltage Mode (Adaptive/Override), Override, Offset; per-VF-point offsets (at Nx); Maximum Voltage; ring offset |
| TVB | per active-core TVB temperature + ratio offset (x2); per-core OC TVB (P and E); TVB ratio clipping; TVB voltage optimizations |
| Power | Turbo Boost Power Max (PL1), Turbo Boost Short Power Max (PL2) + enable, Power Time Window (Tau), Processor Core IccMax |
| Clocks | Dual reference clock (SoC / compute BCLK), SA fabric |
| Other | Core Parking; Undervolt protection status; Memory (XMP, real-time timings) |
| Ops | Stress Test (CPU/memory/AVX), Benchmark, Profiles + Profile Pairing (profile bound to an app) |

## What to borrow (and what not)
- Borrow: tile grid of cores + one Selected Core inspector; every field labeled with its operating point and unit; pending-change state
  with one Apply/Discard bar; always-on monitoring strip; Ratio/Monitors toggle on the grid; favored-core stars.
- Improve on: XTU hides which values are fused vs changed and never shows the resulting voltage — we already compute target VIDs and
  readback; show "fused -> requested -> read back" per field. XTU's Apply has no preview; ours shows the ordered write list inline.
- Don't copy: memory/graphics/BCLK tabs we cannot write at runtime on Linux (show read-only or omit).
