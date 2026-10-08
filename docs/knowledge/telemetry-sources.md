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
| Temps (primary) | hwmon `coretemp`: `temp*_input` m°C, labels "Package id N"/"Core N" (E-core coverage on ARL: report only labels present) | none | linux | G | [DOC kernel hwmon/coretemp.rst]; ARL labels [UNVERIFIED] |
| Temps (fallback) | MSR 0x19C / 0x1B1: valid `[31]`, readout `[22:16]`; `Tjmax - readout`; Tjmax = 0x1A2 `[23:16]` (105) | root | linux | G | [DOC SDM vol 3 14.7]; 0x1A2 [MEASURED lnx 2026-09-29] |
| Package power (primary) | powercap `intel-rapl:0/energy_uj` delta, wrap at `max_energy_range_uj` | root since 5.10 (PLATYPUS) | linux | G | [DOC powercap.rst]; privilege [DOC] |
| Core power | powercap `intel-rapl:0:N` where `name == core` | root | linux | G (subdomain presence on ARL [UNVERIFIED]) | [DOC] |
| Package power (fallback) | MSR 0x611 `[31:0]` x 2^-ESU J, ESU = 0x606 `[12:8]` (14) | root | linux | G | 0x611 [DOC SDM]; 0x606 units [MEASURED lnx 2026-09-29] |
| Vcore | hwmon board Super-I/O (Nuvoton nct67xx), `in*_label == "Vcore"`, `in*_input` mV; 16 mV step | none (sysfs) | linux | P (ASRock Z890) | chip+step [MEASURED lnx 2026-10-01] (linux-path.md s7); hwmon name/label [UNVERIFIED] -> unavailable if absent |
| Limit reasons | MSR 0x6B0 raw word, **not decoded** | root | linux | P | raw read [MEASURED]; decode open (registers.md) |
| DRAM read/write GB/s | ONE long-lived `perf stat -a -I 1000 -x , -e uncore_imc_free_running/data_read/,.../data_write/`; stderr merged; MiB x 1.048576/1000 / interval | root (or perf_event_paranoid <= -1) | perf (linux-tools) | P (IMC PMU) | unit/scale [MEASURED lnx 2026-10-03]; CSV layout [DOC perf-stat(1)]; event spelling [UNVERIFIED] |
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
