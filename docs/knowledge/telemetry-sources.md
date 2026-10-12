# Telemetry sources (SPEC §8) — one documented source per value

Code: `src/arltuner/telemetry/`. Host: lnx. Scope: P = platform-specific (Arrow Lake-S, or the board named), G = generic Intel/Linux.
Rule: only MEASURED / DECOMPILED / DOC facts are code constants; a missing or unreadable source yields `unavailable` + reason, never a number.
Registers cite `registers.md` ("Measured on the reference machine"). Intel SDM = vol 3 (14.2 APERF/MPERF, 14.7 thermal) / vol 4 (MSR list).
Measured values below are from the reference 285K / ASRock Z890 (`examples/reference-285k/notes/platform.md`).

| Value | Source | Privilege | Package | Scope | Evidence |
|---|---|---|---|---|---|
| Effective MHz per core | `tsc_MHz * dAPERF/dMPERF`, MSR 0xE8 / 0xE7 (64-bit, wrap handled) | root (msr node) | linux | G | [DOC Intel SDM vol 3 14.2] |
| Busy % per core | `100 * dMPERF / (tsc_MHz*1e6*dt)` | root | linux | G | [DOC SDM] |
| TSC MHz | 100 MHz * MSR 0xCE `[15:8]` (285K: 37 -> 3700 MHz) | root | linux | P (ratio value) | [MEASURED lnx 2026-09-29]; bus 100 MHz [DOC] |
| Requested VID + running ratio | MSR 0x198 `[47:32]`/8192 V; `[15:8]` ratio. A *request*, not delivered voltage | root | linux | P (decode confirmed on this CPU) | [MEASURED lnx 2026-09-29] |
| Temps (primary) | hwmon `coretemp`: `temp*_input` m°C, labels "Package id N"/"Core N" (report only labels present) | none | linux | G | [DOC kernel hwmon/coretemp.rst]; ARL [MEASURED lnx 2026-10-11]: one "Core N" label per P and E core, N = sysfs `topology/core_id` (not the CPU number) |
| Temps (fallback) | MSR 0x19C / 0x1B1: valid `[31]`, readout `[22:16]`; `Tjmax - readout`; Tjmax = 0x1A2 `[23:16]` (105) | root | linux | G | [DOC SDM vol 3 14.7]; 0x1A2 [MEASURED lnx 2026-09-29] |
| Package power (primary) | powercap `intel-rapl:0/energy_uj` delta, wrap at `max_energy_range_uj` | root since 5.10 (PLATYPUS) | linux | G | [DOC powercap.rst]; privilege [DOC] |
| Core power | powercap `intel-rapl:0:N` where `name == core` | root | linux | G (subdomain presence on ARL [UNVERIFIED]) | [DOC] |
| Package power (fallback) | MSR 0x611 `[31:0]` x 2^-ESU J, ESU = 0x606 `[12:8]` (14) | root | linux | G | 0x611 [DOC SDM]; 0x606 units [MEASURED lnx 2026-09-29] |
| Vcore | hwmon `nct6686` (kernel driver `nct6683`, ASRock NCT6686D), `in0` label **`VIN16`**, `in*_input` mV, 16 mV step = board VccIA/VCCCORE VR VOUT before the DLVRs (always above the highest VID). **Not** `in1`/`VIN0` (a CPU-0-activity-gated signal; never a metric, never Vcore). `in5`/`VIN6` = VccSA sense | none (sysfs) | linux | P (ASRock Z890 Steel Legend WiFi; other boards need their own row, keyed by DMI board name, D-036) | [MEASURED lnx 2026-10-11]: VIN16 tracks load above the max VID, VIN6 matches the BIOS VccSA setting; the Super-I/O labels are generic (`VINn`), no `Vcore` label exists |
| Limit reasons | MSR 0x6B0 raw word, **not decoded** | root | linux | P | raw read [MEASURED]; decode open (registers.md) |
| DRAM read/write GB/s | ONE long-lived `perf stat -a -I 1000 -x , -e uncore_imc_free_running/data_read/,.../data_write/`; stderr merged; MiB x 1.048576/1000 / interval | root (or perf_event_paranoid <= -1) | perf (linux-tools) | P (IMC PMU) | unit/scale [MEASURED lnx 2026-10-03]; CSV layout [DOC perf-stat(1)]; event spelling `uncore_imc_free_running/data_read/` (merged over `_0`/`_1`) [MEASURED lnx 2026-10-11, root perf 6.x: ~1 GB/s read idle] |
| GPU | nvidia-smi dmon — **not implemented in G-104** (optional, SPEC §8) | user | nvidia-utils | G | [MEASURED lnx 2026-10-03] |

Design: reads go through `TelemetryMsr` (rdmsr only; refuses anything outside `TELEMETRY_MSRS`, incl. 0x150; no `wrmsr` attribute).
The telemetry package imports nothing from mailbox/guard/msr.transport (AST test `tests/telemetry/test_isolation.py`). Sample thread 1 Hz default.
Idle core (MPERF flat): MHz unavailable, busy 0. First sample of any delta reader: unavailable ("need two").

## Telemetry read rows (applied: `TELEMETRY_MSRS`, D-026; mirrored in registers.md "Telemetry read allowlist")
Read kind = RDMSR only, any CPU, no mailbox involvement.

| MSR | Bits used | Kind | Evidence |
|---|---|---|---|
| 0xE7 MPERF | `[63:0]` | read | [DOC SDM vol 4] |
| 0xE8 APERF | `[63:0]` | read | [DOC SDM vol 4] |
| 0xCE PLATFORM_INFO (already) | `[15:8]` base ratio | read | [MEASURED lnx 2026-09-29] |
| 0x198 PERF_STATUS (already) | `[47:32]` VID, `[15:8]` ratio | read | [MEASURED lnx 2026-09-29] |
| 0x19C IA32_THERM_STATUS | `[31]` valid, `[22:16]` readout | read | [DOC SDM vol 4]; ARL behaviour [UNVERIFIED] until first read |
| 0x1A2 TEMPERATURE_TARGET | `[23:16]` Tjmax | read | [MEASURED lnx 2026-09-29] `0x80691400` |
| 0x1B1 IA32_PACKAGE_THERM_STATUS | `[31]` valid, `[22:16]` readout | read | [DOC SDM vol 4]; ARL [UNVERIFIED] |
| 0x606 RAPL_POWER_UNIT | `[12:8]` ESU (`[3:0]` PU, `[19:16]` TU not used) | read | [MEASURED lnx 2026-09-29] |
| 0x611 PKG_ENERGY_STATUS | `[31:0]` | read | [DOC SDM vol 4] |
| 0x64F live limit word | raw, not decoded (listed in `TELEMETRY_MSRS` for a planned throttle-reason tile, no reader yet) | read | [MEASURED lnx 2026-09-29] raw |
| 0x6B0 PKG_PERF_LIMIT_REASONS | raw, not decoded | read | [MEASURED lnx 2026-09-29] raw; 0x690 is EIO — never read |

## Other sources (not sampled by the app)
| Value | Source | Evidence |
|---|---|---|
| CPU type / cluster map | CPUID leaf 0x1A; sysfs `cpu_core` / `cpu_atom`, `cluster_cpus_list` (`platform-arl-s.md` §2) | [MEASURED lnx 2026-09-29] |
| Favored cores | `acpi_cppc/highest_perf`, mailbox `0x1C` (`favored-cores.md` §2) | [MEASURED lnx 2026-09-29] |
| Turbo tables, ring ratio, PL1/PL2 | MSR 0x1AD/0x1AE/0x650/0x651, 0x620/0x621, 0x610 + 0x606 (registers.md); powercap sysfs | [MEASURED lnx 2026-09-29] |
| Design current | MSR 0x601 (not IccMax; IccMax = mailbox cmd 0x16); PL4 via 0x601 vs MMIO 0x59B0 open (C-2) | [MEASURED] |
| Ring voltage request | "UncVID" (read by the operator's own monitor; source register not recorded here) | [MEASURED lnx 2026-10-03] |
| DDR5 frequency / timings | MCHBAR, `memory-mchbar.md` | [DOC Intel 835538] |
| GPU | `nvidia-smi dmon` (PCIe rx/tx, SM, power) | [MEASURED lnx 2026-10-03] |
Settled negatives: no ARL-S PMT telemetry schema; no delivered (post-DLVR) voltage telemetry; no PL3. Tools contending for the Super-I/O (HWiNFO-style):
one owner per controller.

## Recorded `perf stat` output (root, lnx 2026-10-11, idle desktop) — parser fixture
Units are MiB (x 1.048576 = MB) [MEASURED lnx 2026-10-03].
`sudo perf stat -a -I 1000 -x , -e uncore_imc_free_running/data_read/,uncore_imc_free_running/data_write/ -- sleep 2.5` [MEASURED lnx 2026-10-11]:
```
1.001185302,1041.93,MiB,uncore_imc_free_running/data_read/,2002449195,100.00,,
1.001185302,116.96,MiB,uncore_imc_free_running/data_write/,2002467557,100.00,,
2.002374512,909.23,MiB,uncore_imc_free_running/data_read/,2002319269,100.00,,
2.002374512,73.57,MiB,uncore_imc_free_running/data_write/,2002306047,100.00,,
```
One row per event per interval (instances `_0`/`_1` already summed by perf); `/sys/bus/event_source/devices/uncore_imc_free_running_{0,1}` exist.
