# Throttling indicators — registers, XTU-style mapping, read strategy, event log `host lnx`

Status: draft, research agent 2026-10-18, not yet MEASURED on lnx (except the sysfs / cpuinfo reads tagged MEASURED below; no MSR was read).

**Headline: the dashboard hex is the wrong register.** `desktop/app/ui/store.js:87-96` (`limitBadges`) reads `limit_reasons_raw`, which is the raw
**0x6B0 graphics** word (`src/arltuner/telemetry/sampler.py:101`, `telemetry/cpu.py:141-146`). It is rendered as hex by
`desktop/app/ui/views/dashboard.js:48-61` (tile) and `desktop/app/ui/components/monitor-strip.js:11-14,63-70` (strip badge). Live 0x6B0 is `0x01000000`
(sticky "Other" log, iGPU) `[MEASURED lnx 2026-10-18, telemetry-sources.md]`, so the tile shows a permanent amber `0x1000000` that says nothing about the cores.
The decoded **core** word (`limit_reasons` = 0x64F, `sampler.py:99`, `{raw,status,log,unknown_bits}`) is already in every snapshot and the UI ignores it.

## A. Registers

| MSR | Scope | Bits for throttle reporting (status RO / log sticky) | Who clears the log | On ARL-S | Linux exposure |
|---|---|---|---|---|---|
| 0x19C IA32_THERM_STATUS | per logical CPU `[DOC edk2 ArchitecturalMsr.h]`; P vs E-cluster sharing `[UNVERIFIED]` | 0/1 thermal status/log · 2/3 PROCHOT#/FORCEPR event/log · 4/5 critical temp · 6–9 thresholds 1/2 · **10/11 power-limit** (CPUID.06H:EAX[4] PLN) · **12/13 current-limit**, 14/15 cross-domain (CPUID.06H:EAX[7] HWP) · 22:16 readout · 31 valid `[DOC edk2 ArchitecturalMsr.h]` | log bits R/WC0 (write 0 = WRMSR). **Kernel owns them**: interrupt clear mask core = 1,3,5 (+7,9 TM2, +11 PLN, +13,15 HWP); `throttle_active_work` clears BIT(1) every 1 s while an event lasts `[DOC therm_throt.c]` | flags `pln pts hwp hwp_notify hfi dtherm tm2` present `[MEASURED lnx 2026-10-18 /proc/cpuinfo]` → bits 10–15 implemented | `thermal_throttle/core_throttle_{count,max_time_ms,total_time_ms}` |
| 0x1B1 IA32_PACKAGE_THERM_STATUS | package | 0/1 thermal · 2/3 PROCHOT · 4/5 critical · 6–9 thresholds · **10/11 power-limit** · 22:16 readout `[DOC edk2]` · **13 "PMAX log"** — IPF primitives `GET/SET_THERM_STATUS_PMAX_LOG` `[DECOMPILED linuxmon ipf-primitives.tsv:303-304]`, not architectural, ARL `[UNVERIFIED]` · 26 HFI update (kernel-handled) `[DOC therm_throt.c / msr-index.h]` | kernel pkg mask 1,3,5,7,9,11 (+26 HFI, +25 DPTI) `[DOC therm_throt.c]`; XTU writes 1/3/13 `[DECOMPILED ipf-primitives.tsv:299-304]` | PTS present `[MEASURED cpuinfo]` | `package_throttle_{count,max_time_ms,total_time_ms}` |
| 0x64F CORE (IA) PERF_LIMIT_REASONS | package (same word on P and E CPUs `[MEASURED lnx 2026-10-13]`) | status 15:0, log = status+16: 0 PROCHOT · 1 Thermal · 4 Residency-state regulation · 5 RATL · 6 VR therm alert · 7 VR TDC · 8 Other (electrical) · 10 PL1 · 11 PL2/PL3 · 12 Max turbo (multi-core) · 13 Turbo transition attenuation `[DOC edk2 SkylakeMsr.h]` | log "remains set until cleared by software writing 0" `[DOC edk2 SkylakeMsr.h]`. **Linux never touches it** (turbostat read-only; not in therm_throt.c). **XTU can clear it** (`SET_IA_CLIP_REASONS` 0x445 → 0x64F [29:0]) `[DECOMPILED ipf-primitives.tsv:355; win-xtu-decompile-mailbox.md:211]` | live; ARL semantics `[UNVERIFIED]` (only Skylake+ table exists) | none (turbostat: `adl_features` used for ARL has no `plr_msrs` → prints nothing `[DOC turbostat.c]`) |
| 0x6B0 GRAPHICS PERF_LIMIT_REASONS | package (iGPU) | 0,1,5,6,7,8,10,11 as 0x64F; 12 inefficient operation `[DOC edk2]` | XTU clears (`SET_GT_CLIP_REASONS` 0x446) `[DECOMPILED ipf-primitives.tsv:356]` | live `0x01000000` | none |
| 0x6B1 RING PERF_LIMIT_REASONS | package (ring) | 0,1,5,6,7,8,10,11 (no 4/12/13) `[DOC edk2 SkylakeMsr.h]` | software write 0 | in XTU's MSR allowlist as RO `[DECOMPILED win-xtu-native-provider.md:101]`; never read here | none |
| 0x690 | — | Haswell-era core word | — | **EIO** `[MEASURED lnx 2026-09-29]` — never read | turbostat's core bit names (Amps@8, Auto-HWP@5, CorePwr@9) are this layout `[DOC turbostat.c]` — do not apply to 0x64F |

Kernel facts `[DOC https://raw.githubusercontent.com/torvalds/linux/master/drivers/thermal/intel/therm_throt.c]`:
- `core_throttle_count` counts **0x19C bit 0** (kernel `THERM_STATUS_PROCHOT` = BIT(0) is the SDM *Thermal status*, not the PROCHOT# pin; its `THERM_STATUS_PROCHOT_LOG` = BIT(1) is the *thermal log*). Package likewise from 0x1B1 bit 0. Durations come from interrupt→poll end.
- Power-limit counters (`core/package_power_limit_count`) exist only with boot option `int_pln_enable` (default off): absent here — `cpu0/thermal_throttle/` has the 6 throttle files only, all `0` `[MEASURED lnx 2026-10-18 sysfs]`.
- So sysfs covers **thermal only**, root-free, with count/max/total time.

## B. Mapping to XTU's indicators

The decompiled XTU UI code that maps its labels to bits was **not found** in linuxmon (only the IPF paths: `Diagnostics.ClipReasons` = 0x64F [29:0], `Graphics…ClipReasons` = 0x6B0,
`Thermal.Status.Diagnostics.ProchotLog/PmaxLog/ThermalMonitorLog` = 0x1B1 bits 3/13/1, all read **and cleared** `[DECOMPILED win-xtu-decompile-mailbox.md:211, ipf-primitives.tsv]`).
XTU reads the log bits and has a clear path for them; whether it clears on every poll ("since my last poll") is `[UNVERIFIED]`. The mapping below is therefore **inference** from: Intel's definition of Current/EDP =
IccMax / VR current limit, Power = PL1/PL2 `[DOC https://www.intel.com/content/www/us/en/support/articles/000039154.html]`; ThrottleStop's "EDP OTHER" label
`[COMMUNITY https://techpowerup.com/forums/goto/post?id=5192378]`, whose bit (8, by SDM "Other (electrical)" + Haswell "EDP" history, registers.md) is `[UNVERIFIED]`; siomon's working three labels from 0x19C (`thermal` = bits 0|2|4, `power` = 10, `current` = 12)
`[CODE /home/yk/AI/siomon/src/sensors/intel_msr.rs:125-150; src/output/tui/cpu_view.rs:174-176]`.

| GUI indicator (group) | Primary (package, 1 read) | Corroborating | Meaning / note |
|---|---|---|---|
| **Power limit** | 0x64F bit 10 PL1, bit 11 PL2 | 0x19C bit 10 (per core), 0x1B1 bit 10 | sub-labels "PL1 (long)", "PL2 (short)". PL4/Pmax: 0x1B1 bit 13 log only `[UNVERIFIED]` — show as "Pmax (unverified)" |
| **Current / EDP** | 0x64F bit 8 Other (electrical), bit 7 VR TDC | 0x19C bit 12 current-limit (per core) | IccMax / VR current. Bit 8 is "electrical **or other**" — label "Current/EDP (electrical)" |
| **Thermal** | 0x64F bit 1 Thermal, bit 0 PROCHOT, bit 5 RATL | 0x19C bits 0/2/4, 0x1B1 bits 0/2/4; kernel counters | sub-labels "TjMax/TCC", "PROCHOT# (external)", "running-average thermal" |
| VR thermal (motherboard) | 0x64F bit 6 VR therm alert | — | separate chip; board VRM hot |
| Max turbo (informational) | 0x64F bit 12 | — | multi-core turbo-ratio table is the cap — **normal under load**, grey not amber |
| Turbo attenuation / residency (info) | 0x64F bits 13 / 4 | — | grey, in the detail popover only |
| Ring / iGPU | 0x6B1 / 0x6B0 | — | detail popover only; never the headline tile |
| Unknown | `unknown_bits` (e.g. 14/15; CoreFreq claims 14 = TVB `[COMMUNITY]`) | — | shown by number, never named |

Recommended GUI: one tile "Throttling" with three XTU chips **Power · Current/EDP · Thermal** (Yes = red/amber, No = green), a fourth chip "VR thermal",
a grey "Max-turbo cap" chip, and a popover listing every active status bit with its SDM name and the word it came from. Replace `limit_reasons_raw` with
`limit_reasons` (0x64F) in `store.js`; keep 0x6B0 in the popover only.

## C. Read strategy without writes

| Source | Semantics | Usable for | Problem |
|---|---|---|---|
| 0x64F status bits 15:0 | instantaneous | point-in-time Yes/No | a 1 s poll misses a PL2/EDP burst between samples |
| 0x64F log bits 31:16 | sticky since boot (Linux) / since XTU's last clear (dual boot) | "ever since boot" only | one 0→1 transition per boot; without clearing they carry no timing |
| 0x19C / 0x1B1 log bits | kernel-cleared | nothing | the kernel clears them (A); reading them races the kernel, writing them fights it |
| `thermal_throttle/*` sysfs | count + max/total ms, kernel-maintained | thermal events with durations, no root | thermal only; power counters off (`int_pln_enable`) |

**Recommendation: never write any of these registers.** Status bits are the point-in-time truth. To get XTU's "it happened" feel without a log-bit clear:
1. **Oversample 0x64F only** (one rdmsr on CPU 0) at ~10 Hz in the sampler thread and publish, per 1 s tick, the **fraction of samples** each status bit was set
   (`power: 0.3` = throttled ~30 % of the last second) plus `any` = OR over the window. Short bursts become visible; no WRMSR.
2. A **software "reset indicators"** button clears the app's own OR-accumulator (and the event-log view) — not the hardware.
3. Hardware log bits are shown as "since boot" in the popover, with "may have been cleared by Windows XTU" when the boot followed a Windows session.
4. Diff the kernel thermal counters each tick as an independent thermal witness.
The task's alternative (an explicit operator "clear hardware log" action) is not needed; if ever wanted it is a WRMSR of 0x64F/0x6B0/0x6B1 → its own
decision (D-NNN), audit entry, write-sitting rules (AGENTS.md), never 0x19C/0x1B1 (kernel-owned).

## D. Read path in this project

| Set | File | Has 0x19C | 0x1B1 | 0x64F | 0x6B0 | 0x6B1 |
|---|---|---|---|---|---|---|
| `TELEMETRY_MSRS` (what the helper's sampler uses: `helper/main.py:209` → `Sampler(TelemetryMsr(port))`) | `telemetry/msrio.py:29-33` | yes | yes | yes | yes | **no** |
| `PLAIN_MSR_READABLE` (guard path for ops, not telemetry) | `guard/policy.py:176` | no | no | no | no | no |

- Nothing must go into `PLAIN_MSR_READABLE` for this feature. Only **0x6B1** is missing from `TELEMETRY_MSRS` (+ a row in `telemetry-sources.md`); adding it widens an
  allowlist → orchestrator decision (D-026), not a worker edit. Doc conflict to fix: `msrio.py:8-10` says the set is mirrored into `PLAIN_MSR_READABLE` at merge;
  `registers.md:129-130` says separate/unchanged — the code agrees with registers.md.
- Today per 1 s tick: 3 rdmsr × 24 CPUs (APERF, MPERF, 0x198) = 72, plus 0x1A2, 0x64F, 0x6B0 ×2 (decoded + raw). Switch the UI to `limit_reasons` now (the bug); retire the `limit_reasons_raw` key and its read at the
  release boundary already declared (telemetry-sources.md, `cpu.py:142`).
  Each `/dev/cpu/N/msr` read is an IPI that wakes CPU N `[CODE siomon src/sensors/intel_msr.rs:16-22]`.
- Proposed: 0x64F at 10 Hz on CPU 0 (+9 reads/s, CPU 0 is awake anyway), 0x1B1 once per tick (power bit 10, package thermal), 0x19C per CPU once per tick only
  when the detail view is open (+24 IPIs/s; gives *which* core was power/current/thermal limited). 0x6B1 once per tick if adopted.

## E. Throttle event log

| Field | Source |
|---|---|
| `t_on`, `t_off` (wall + monotonic), `dur_ms` | sampler tick where the group's window-OR flipped |
| `group` (power / current / thermal / vr_thermal / max_turbo) and `bits` (SDM keys from `limits.CORE_64F`) | 0x64F status |
| `duty` (mean fraction while on) | the 10 Hz oversampling |
| `peak_temp_c`, `peak_pkg_w`, `peak_mhz`, `cpus` (if 0x19C detail on) | same snapshots during the event |
| `kernel_thermal_delta` | diff of `thermal_throttle/*_count` |
| `tuning_id` | accepted/applied state at onset (ties a throttle to an undervolt) |

- Debounce: open on first window with the bit, close after 2 consecutive clean windows; max-turbo events are logged at "info" or not at all (they fire on every load).
- Storage: a sibling JSONL next to the audit log — `$XDG_STATE_HOME/arltuner/throttle.jsonl` (dev, D-041) / `/var/lib/arltuner/throttle.jsonl` (installed, D-030) —
  not the audit log itself (audit = writes). Size-capped rotation. The GUI shows the last N events and a per-group "time throttled this session" counter.

## F. Open questions — exact live reads (orchestrator; all plain rdmsr, no mailbox)

| # | Read | Expect | Settles |
|---|---|---|---|
| 1 | 0x19C on CPUs 16–19 (one E cluster) and 0, 2 (P) at idle, then under E-only load | bits 31 = 1; readout 22:16 differs per CPU if per-core | per-core vs per-cluster scope on Skymont |
| 2 | 0x64F on CPU 0 under all-core load with PL1 capped below draw (powercap) | status bit 10 = 1 (`0x…0400`) and bit 12 | PL1 bit live on ARL |
| 3 | 0x19C bits 10/12 on a loaded P CPU in the same run | bit 10 = 1 with #2 | per-core power bit matches 0x64F |
| 4 | 0x1B1 on CPU 0, idle and loaded | bits 12–15 = 0 at idle; bit 13 behaviour unknown | XTU's "PMAX" bit exists on ARL |
| 5 | 0x6B1 on CPU 0 (after allowlisting) | `0x0000xxxx` with 0x64F-like log bits | ring word live |
| 6 | Same boot: read 0x64F and `cpu*/thermal_throttle/core_throttle_count` after a thermal event (the bit-17 log and the 0-counter seen so far are from **different boots**) | if bit 17 sets while the counter stays 0 → | what fires 0x64F "Thermal" without a TM1 event (TCC offset? RATL?) |
| 7 | 0x64F at 10 Hz for 60 s under a bursty load | bits 8/11 toggle within a second | oversampling is worth it |
