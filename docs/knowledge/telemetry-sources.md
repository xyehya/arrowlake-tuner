# Telemetry sources (SPEC §8) — one documented source per value

Code: `src/arltuner/telemetry/`. Host: lnx. Scope: P = platform-specific (this 285K / Z890), G = generic Intel/Linux.
Rule: only MEASURED / DECOMPILED / DOC facts are code constants; a missing or unreadable source yields `unavailable` + reason, never a number.
Registers cite `registers.md` ("Measured on this machine") and `linux-path.md` s7. Intel SDM = vol 3 (14.2 APERF/MPERF, 14.7 thermal) / vol 4 (MSR list).

| Value | Source | Privilege | Package | Scope | Evidence |
|---|---|---|---|---|---|
| Effective MHz per core | `tsc_MHz * dAPERF/dMPERF`, MSR 0xE8 / 0xE7 (64-bit, wrap handled) | root (msr node) | linux | G | [DOC Intel SDM vol 3 14.2] |
| Busy % per core | `100 * dMPERF / (tsc_MHz*1e6*dt)` | root | linux | G | [DOC SDM] |
| TSC MHz | 100 MHz * MSR 0xCE `[15:8]` (= 37 -> 3700 MHz here) | root | linux | P (ratio value) | [MEASURED lnx 2026-09-29] registers.md 0xCE = `0x08040838F1812500`; bus 100 MHz [DOC] |
| Requested VID + running ratio | MSR 0x198 `[47:32]`/8192 V; `[15:8]` ratio. A *request*, not delivered voltage | root | linux | P (decode confirmed on this CPU) | [MEASURED lnx 2026-09-29] |
| Temps (primary) | hwmon `coretemp`: `temp*_input` m°C, labels "Package id N"/"Core N" (E-core coverage on ARL: report only labels present) | none | linux | G | [DOC kernel hwmon/coretemp.rst]; ARL [MEASURED lnx 2026-10-11]: "Package id 0" + 24 "Core N" labels, N = sysfs `core_id` (0,4,..,44 / 8..15 / 32..39 ...), map via `topology/core_id` |
| Temps (fallback) | MSR 0x19C / 0x1B1: valid `[31]`, readout `[22:16]`; `Tjmax - readout`; Tjmax = 0x1A2 `[23:16]` (105) | root | linux | G | [DOC SDM vol 3 14.7]; 0x1A2 [MEASURED lnx 2026-09-29] |
| Package power (primary) | powercap `intel-rapl:0/energy_uj` delta, wrap at `max_energy_range_uj` | root since 5.10 (PLATYPUS) | linux | G | [DOC powercap.rst]; privilege [DOC] |
| Core power | powercap `intel-rapl:0:N` where `name == core` | root | linux | G (subdomain presence on ARL [UNVERIFIED]) | [DOC] |
| Package power (fallback) | MSR 0x611 `[31:0]` x 2^-ESU J, ESU = 0x606 `[12:8]` (14) | root | linux | G | 0x611 [DOC SDM]; 0x606 units [MEASURED lnx 2026-09-29] |
| Vcore | hwmon `nct6686` (kernel driver `nct6683`, ASRock NCT6686D), `in0` label **`VIN16`**, `in*_input` mV, 16 mV step = board VccIA/VCCCORE VR VOUT before the DLVRs (always above the highest VID). **Not** `in1`/`VIN0` (a CPU-0-activity-gated signal; never a metric, never Vcore). `in5`/`VIN6` = VccSA sense | none (sysfs) | linux | P (ASRock Z890) | [MEASURED lnx 2026-10-11]: idle 0.848 V (max VID 0.741), all-core stress-ng 1.456-1.488 V (max P VID 1.27-1.32, untuned); VIN6 1.184 V vs BIOS VccSA 1.185 V; mapping matches the operator's monitor notes (VIN16 = VCCCORE). No `Vcore` label exists on this chip |
| Limit reasons | MSR 0x6B0 raw word, **not decoded** | root | linux | P | raw read [MEASURED]; decode open (registers.md) |
| DRAM read/write GB/s | ONE long-lived `perf stat -a -I 1000 -x , -e uncore_imc_free_running/data_read/,.../data_write/`; stderr merged; MiB x 1.048576/1000 / interval | root (or perf_event_paranoid <= -1) | perf (linux-tools) | P (IMC PMU) | unit/scale [MEASURED lnx 2026-10-03]; CSV layout [DOC perf-stat(1)]; event spelling `uncore_imc_free_running/data_read/` (merged over `_0`/`_1`) [MEASURED lnx 2026-10-11, root perf 6.x: ~1 GB/s read idle] |
| GPU | nvidia-smi dmon — **not implemented in G-104** (optional, SPEC §8) | user | nvidia-utils | G | [MEASURED lnx 2026-10-03] |

Design: reads go through `TelemetryMsr` (rdmsr only; refuses anything outside `TELEMETRY_MSRS`, incl. 0x150; no `wrmsr` attribute).
The telemetry package imports nothing from mailbox/guard/msr.transport (AST test `tests/telemetry/test_isolation.py`). Sample thread 1 Hz default.
Idle core (MPERF flat): MHz unavailable, busy 0. First sample of any delta reader: unavailable ("need two").

## Proposed read-kind allowlist rows (orchestrator applies to registers.md + `PLAIN_MSR_READABLE`)
Already allowlisted: 0xCE, 0x198. Read kind = RDMSR only, any CPU, no mailbox involvement.

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
| 0x64F live limit word | raw, not decoded (listed in `TELEMETRY_MSRS`, no reader yet) | read | [MEASURED] raw (linux-path.md s7) |
| 0x6B0 PKG_PERF_LIMIT_REASONS | raw, not decoded | read | [MEASURED] raw (linux-path.md s7); 0x690 is EIO — never read |

Note: 0x64F is in `TELEMETRY_MSRS` for the planned throttle-reason tile; drop it from the proposal if the orchestrator prefers rows only for MSRs with a reader today.

## Recorded `perf stat` output (root, lnx 2026-10-11, idle desktop) — parser fixture
`sudo perf stat -a -I 1000 -x , -e uncore_imc_free_running/data_read/,uncore_imc_free_running/data_write/ -- sleep 2.5` [MEASURED lnx 2026-10-11]:
```
1.001185302,1041.93,MiB,uncore_imc_free_running/data_read/,2002449195,100.00,,
1.001185302,116.96,MiB,uncore_imc_free_running/data_write/,2002467557,100.00,,
2.002374512,909.23,MiB,uncore_imc_free_running/data_read/,2002319269,100.00,,
2.002374512,73.57,MiB,uncore_imc_free_running/data_write/,2002306047,100.00,,
```
One row per event per interval (instances `_0`/`_1` already summed by perf); `/sys/bus/event_source/devices/uncore_imc_free_running_{0,1}` exist.
