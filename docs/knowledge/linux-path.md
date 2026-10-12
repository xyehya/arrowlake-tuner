Distilled 2026-10-08 from the project's earlier research phase; the raw notes and captures are not part of this repository.
# Linux execution path: /dev/cpu/N/msr, health bracket, restore, telemetry

Sources: earlier Linux runbooks and first-read notes (not part of this repository); `docs/SAFETY.md`, `tools/lnx/*`. Host: Fedora 44, kernel 7.2.x, Secure Boot off.

## 1. MSR access and privilege
| Fact | Value | Evidence |
|---|---|---|
| Device | `/dev/cpu/N/msr`, 24 nodes, `root:msr 0660`; `CONFIG_X86_MSR=y` built in (`modules.builtin`), so no modprobe | [MEASURED lnx 2026-10-01]; [DOC src.fedoraproject.org f44 kernel config] |
| Privilege | needs root (CAP_SYS_RAWIO). Group membership is NOT enough: open as the user fails EPERM although the node is group-rw and the user is in `msr` | [MEASURED lnx 2026-10-08] (from the project brief; not written in the 10-08 source docs - no raw capture located) |
| Reads | never taint; unimplemented MSR -> EIO (e.g. 0x690) | [DOC arch/x86/kernel/msr.c] |
| Writes | need `msr.allow_writes=on` (cmdline or `/sys/module/msr/parameters/allow_writes`; default is `default` = allowed with a notice); `off` -> EPERM. Every write taints `TAINT_CPU_OUT_OF_SPEC` (bit 2) even with allow_writes=on | [DOC lnx-interfaces.md s5]; [MEASURED lnx 2026-10-01] |
| Notices in dmesg | `msr: Write to unrecognized MSR 0x150 by <comm> (pid N), tainting CPU_OUT_OF_SPEC`, then rate-limited `filter_write: N callbacks suppressed` - EXPECTED, hide from health diff | [MEASURED lnx 2026-10-01] |
| Lockdown | Secure Boot ON => kernel lockdown blocks MSR writes (reads still work); check `/sys/kernel/security/lockdown`. Here Secure Boot is off | [DOC include/linux/security.h]; [USER 2026-09-30] |
| BIOS vetoes | OcSupport / OC Lock / Undervolt Protection (UVP); here OC Lock + UVP OFF. Silent-ignore mode exists (a RPL-HX ignored writes until a hidden switch) -> ALWAYS read back | [DOC FSP FspmUpd.h]; [COMMUNITY github tarilka0gg/system-lab] |
- Taint baseline here: 12288 (bits 12 `O` + 13 `E`, nvidia only) -> 12292 after the first MSR write of a boot (bit 2). Compare to the recorded baseline, never to zero. Pass `--allow-taint-bit 2`.
- kernel 7.2 bug: one `read(4 MiB + 1)` of `/proc/sys/kernel/tainted` fails ENOMEM; read in 64 KiB chunks (the guard refused fail-closed until fixed) [MEASURED lnx 2026-10-01].
- OCCT's Linux GUI writes MSR 0x150 at every launch when it has CAP_SYS_RAWIO (it was the source of the old taint). Run OCCT unprivileged; a root OCCT is a second mailbox client (observed: wrote 0x150 on 2026-10-02, did not change the words we read) [MEASURED lnx 2026-10-02]. One owner per shared controller: no HWiNFO / A-Tuning / EC clients during a sitting.

## 2. Mailbox sequence (per request)
Pin to the target CPU (`/dev/cpu/N/msr` runs on CPU N): read 0x150 until bit 63 = 0 (10 ms sleeps, cap ~10 s, abort WITHOUT writing on timeout) -> write -> read until bit 63 = 0 -> data [31:0], rc [39:32] -> explicit read-back. Serialise all users; never retry a failing write. Every request incl. reads is a WRMSR [DECOMPILED win-xtu-native-provider.md s5, s8]. Env flag `ARLTUNER_ALLOW_MAILBOX=1` is set by the human; CONFIRM token per command.
Incident 2026-08-26 (the operator's earlier tooling): a broad mailbox Get sweep, boot ended ~5.5 min later, Intel BE200 lost until AC removal; causality unproven. Rules inherited: one guarded command per sitting, >= 5 min watch, health bracket, cold-power plan, never a sweep (`docs/SAFETY.md`).

## 3. Health bracket (snapshot before / after)
Taint integer + set bits; PCI device count/set (35 here); loaded module set (187 here); new kernel-log lines (expected `msr` notices hidden; looks for MCE / "Hardware Error" / AER / PCI lines); `CLEAN` = gate. Script role: `tools/lnx/health_snapshot.sh LABEL [--since FILE]`. Wi-Fi roam lines are benign (Wi-Fi card class was lost in the 2026-08-26 incident). Never clear sticky thermal/limit bits.

## 4. Restore after every boot (D-020a, 2026-10-08)
- Role of `tools/lnx/restore_tuning.sh [--orchestrated] [--check]`: shows policy + profiles, ONE "Type CONFIRM" (exact; else nothing sent), `sudo -v`, health snapshot, apply each profile in the CURRENT list (E split3 -> p-boot-ramp -> p-step7), `vf-dump`, compare word-for-word with the expected-words table (`tuning-results.md` s1), health snapshot. `--check` = read-only. Aborts if OCCT runs as root. Stops at first failure.
- Guard answers per-command tokens via an orchestrator-confirm helper after the single CONFIRM; all guard checks unchanged (policy limits, read-back, compare). Never unattended (autostart is an open decision, D-021).
- Success: `RESTORED: all cores/clusters match the accepted state.` Cold-boot restore on 2026-10-07: 65 + 51 + 51 ops, taint 12288 -> 12292, no new kernel lines [MEASURED lnx]. 2026-10-08 restore: 167 ops [MEASURED lnx].
- Per-boot budget: policy `max_operations_per_boot = 600`, `max_operations_per_session = 260`, counted from the shared audit log; apply budgets the whole plan before sending anything. STATE.md rule: restore (~400 incl. the post-apply vf-dump; 167 of that is apply-only) + one P profile (78) fits; restore + two more does not -> reboot, never raise the cap.
- Boot-ramp profile: after a cold boot every offset is 0 and the guard allows <= 55 mV change per write (floor -100 mV), so deep targets (-97) are refused ("NEXT ALLOWED STEP"). `p-boot-ramp` = the target clamped to >= -50; then the target profile moves <= 47 mV (41 for p-step7). Warm reboot (CPUs 1-7/E kept, CPU 0 stock) also works because the planner steps from live words.
- Default guard (no policy): cross-process dwell 60 s, 12 ops/boot; sessions used reviewed policies with dwell 0 (operator 2026-10-02) or 5 s (D-018 E-49 sitting).

## 5. Audit log
`$XDG_STATE_HOME/arltuner/audit.jsonl` (default `~/.local/state/arltuner/audit.jsonl`; uninstalled/dev path, D-041; an installed machine uses `/var/lib/arltuner/audit.jsonl`, D-030). Under sudo the default resolves to root's home: ALWAYS pass `--audit ~/.local/state/arltuner/audit.jsonl`. `AUDIT_UNAVAILABLE` = nothing sent; recovery is a human act - never delete or truncate the log to pass a refusal (a crash-truncated last line is tolerated). Windows twin: `%LOCALAPPDATA%\arltuner\audit.jsonl`.

## 6. Interface facts (cpufreq / powercap / uncore) [DOC lnx-interfaces.md s2 unless tagged]
- cpufreq `scaling_max_freq` = soft per-core cap, never raises above fused HWP highest; used for stress ("P at 53x") and VID sampling. Mapping: P-cores exact (cap 4.8 GHz -> 48x) but 52x cannot be pinned (caps 5.10-5.20 -> 51x, 5.30 -> 53x); E-cores cap = (ratio+1) x 100 MHz, ratio-dependent quirk [MEASURED lnx 2026-10-02]; on kernel 7.2.9 only E 28/33/36-38/41-48 need the +1 cap, E 30/40x hit at ratio x 100 MHz (R-001 `power-profile-capping.md` section 6). Always restore caps.
- RAPL powercap `/sys/class/powercap/intel-rapl:0/constraint_{0,1}_power_limit_uw` changes PL1/PL2 at runtime [MEASURED lnx 2026-10-02]. ARL-S is rapl_defaults_core (no PL4 via 0x601).
- Uncore: `intel_uncore_frequency` sysfs via 0x620/0x621; ring only vs ring+NGU unknown [UNVERIFIED]. TCC offset 0x1A2 locked (bit 31). TPMI/intel-speed-select do not apply (server-only).
- Nothing in Linux exists for AVX offset, IccMax, BCLK, ring/NGU/D2D/SA V/F, per-core undervolt on ARL; no ASRock WMI driver. UnderVolter (UEFI) ARL claims unverified, README mentions fault injection: do not run. intel-undervolt: not compatible with Tiger Lake+.
- HWP: a `0x1D` change makes the kernel rescale E capacity (`energy_model: updating cpuN cpu_cap`) [MEASURED lnx 2026-10-01]. Stress tools used: stress-ng `--cpu-method all --verify --taskset`, stressapptest (memory), OCCT unprivileged, Linpack.

## 7. Telemetry sources known so far (P = platform-specific to this 285K/Z890; G = generic Intel/Linux)
| Value | Source | Privilege | Scope | Evidence |
|---|---|---|---|---|
| Per-CPU requested VID | MSR 0x198 `[47:32]`/8192 V; ratio `[15:8]` | root (msr node) | P (decode confirmed on this CPU; generic-ness [UNVERIFIED]) | [MEASURED lnx 2026-09-29]; VIDs are requests, not delivered voltage |
| CPU type / cluster map | CPUID leaf 0x1A; E cluster = 4 consecutive CPUs | none (cpuid) | G leaf, P numbering | [MEASURED lnx 2026-09-29] |
| Favored cores | HWP highest perf 92 vs 88 (CPU 0, 3); mailbox 0x1C | root | P | [MEASURED lnx 2026-09-29] |
| Turbo tables | MSR 0x1AD/0x1AE/0x650/0x651 | root | P (ARL layout) | [MEASURED lnx 2026-09-29] |
| PL1/PL2 + units | MSR 0x610, 0x606; powercap sysfs | root for MSR; sysfs not recorded | G | [MEASURED lnx 2026-09-29] |
| Ring ratio | MSR 0x620/0x621 | root | P | [MEASURED lnx 2026-09-29] |
| TCC / Tjmax | MSR 0x1A2 (105 C, offset 0, locked) | root | G | [MEASURED] |
| Design current | MSR 0x601 = 0xd48 = 425 A (NOT IccMax; IccMax 400 A is mailbox cmd 0x16) | root | P | [MEASURED]; XTU/IPF conflict C-2 (earlier access manifest) |
| HWP enable / C-state limit | MSR 0x770 = 1; 0xE2 = 0x74008008 (locked) | root | G | [MEASURED] |
| Limit reasons | MSR 0x6B0/0x6B1 raw, 0x64F live word | root | P (decode open) | [MEASURED]; 0x690 EIO |
| Per-core MHz / busy | APERF/MPERF (MSR) or turbostat | root | G | planned |
| Package/core power | RAPL energy 0x611 / powercap | not recorded | G | planned |
| Temps | coretemp / MSR | not recorded | G | planned |
| Vcore (VccIA) and board rails | Nuvoton Super-I/O via hwmon (the operator's `arl_telemetry`) | board-specific, sysfs/ioport; not recorded | P (ASRock Z890) | [MEASURED lnx 2026-10-01]; 16 mV sensor step |
| Uncore VID (ring) | operator telemetry "UncVID" | not recorded | P | [MEASURED lnx 2026-10-03] |
| DRAM R/W GB/s | perf `uncore_imc_free_running` (units MiB; x1.0486 = MB) | perf privileges not recorded | P (IMC PMU) | [MEASURED lnx 2026-10-03] |
| DDR5/MCHBAR registers | MMIO via PCI BAR (operator's Python readers); PL MMIO 0x59A0/59A4/59B0 | root | P; BAR identity open (C-9) | [UNVERIFIED] |
| GPU | nvidia-smi dmon (PCIe rx/tx, SM, power) | user | G (NVIDIA) | [MEASURED lnx 2026-10-03] |
Settled negatives: no ARL-S PMT schema; no delivered DLVR voltage telemetry; PL3 none. Sample 0.5-1 s; HWiNFO-style tools contending for Super-I/O: one owner per controller.
