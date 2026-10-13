# Linux execution path: /dev/cpu/N/msr, health bracket, restore, interfaces

Measured on Fedora 44, kernel 7.2.x (reference machine); kernel facts cite the source. Telemetry sources: `telemetry-sources.md`.

## 1. MSR access and privilege
| Fact | Value | Evidence |
|---|---|---|
| Device | `/dev/cpu/N/msr`, one node per CPU, `root:msr 0660`; Fedora builds `CONFIG_X86_MSR=y` in (`modules.builtin`), so no modprobe | [MEASURED lnx 2026-10-01]; [DOC src.fedoraproject.org f44 kernel config] |
| Privilege | needs root (CAP_SYS_RAWIO). Group membership is NOT enough: open as the user fails EPERM although the node is group-rw and the user is in `msr` | [MEASURED lnx 2026-10-08] |
| Reads | never taint; unimplemented MSR -> EIO (e.g. 0x690) | [DOC arch/x86/kernel/msr.c] |
| Writes | need `msr.allow_writes` not `off` (cmdline or `/sys/module/msr/parameters/allow_writes`; `default` = allowed with a notice); `off` -> EPERM. Every write taints `TAINT_CPU_OUT_OF_SPEC` (bit 2), even with `on` | [DOC arch/x86/kernel/msr.c]; [MEASURED lnx 2026-10-01] |
| Notices in dmesg | `msr: Write to unrecognized MSR 0x150 by <comm> (pid N), tainting CPU_OUT_OF_SPEC`, then rate-limited `filter_write: N callbacks suppressed` - expected, hidden from the health diff | [MEASURED lnx 2026-10-01] |
| Lockdown | Secure Boot ON => kernel lockdown blocks MSR writes (reads still work); check `/sys/kernel/security/lockdown` | [DOC include/linux/security.h] |
| BIOS vetoes | OcSupport / OC Lock / Undervolt Protection. A silent-ignore mode has been reported (an RPL-HX ignored writes until a hidden switch) -> ALWAYS read back | [DOC FSP FspmUpd.h]; [COMMUNITY github tarilka0gg/system-lab] |
- Taint: compare with the baseline recorded at boot, never with zero (out-of-tree modules such as NVIDIA set bits 12/13); allow bit 2 after the first
  write (`allow_taint_bits = [2]`).
- kernel 7.2: one `read(4 MiB + 1)` of `/proc/sys/kernel/tainted` fails ENOMEM; read in 64 KiB chunks [MEASURED lnx 2026-10-01].
- OCCT's Linux GUI writes MSR 0x150 at every launch when it has CAP_SYS_RAWIO: run OCCT unprivileged; a root OCCT is a second mailbox client
  [MEASURED lnx 2026-10-02]. One owner per shared controller: no HWiNFO / vendor tuning utility / EC client during a sitting.

## 2. Mailbox sequence (per request)
Pin to the target CPU (`/dev/cpu/N/msr` runs on CPU N): read 0x150 until bit 63 = 0 (10 ms sleeps, cap ~10 s, abort WITHOUT writing on timeout) ->
write -> read until bit 63 = 0 -> data [31:0], rc [39:32] -> explicit read-back. Serialise all users; never retry a failing write. Every request incl.
reads is a WRMSR [DECOMPILED win-xtu-native-provider.md s5, s8].
Incident 2026-08-26 (earlier tooling on the reference machine): a broad mailbox read sweep, the boot ended ~5.5 min later and the Wi-Fi card was lost
until AC removal; causality unproven. Rules inherited: one transaction at a time, never a sweep, health bracket (`docs/SAFETY.md`).

## 3. Health bracket (snapshot before / after)
Taint integer + set bits; PCI device set; loaded module set; new kernel-log lines (expected `msr` notices hidden; looks for MCE / "Hardware Error" /
AER / PCI lines); `CLEAN` = gate. Wi-Fi roam lines are benign. Never clear sticky thermal/limit bits.

## 4. Restore after every boot
Mailbox state is volatile (`platform-arl-s.md` §7): the accepted state is re-applied after every boot, with one CONFIRM (D-020a), planned from the live
words (cold or warm boot both work; only the words that differ are written, in guarded rounds within the policy's step limits, D-044) and compared word for word with the accepted state at the end (a residual OC point that runs the accepted voltage is EQUIVALENT, G-147). Never unattended (D-021 unsigned). Procedure:
`docs/guide/09-profiles-and-restore.md`.

## 5. Audit log
Installed: `/var/lib/arltuner/audit.jsonl` (D-030); uninstalled/dev: `$XDG_STATE_HOME/arltuner/audit.jsonl` (D-041). `AUDIT_UNAVAILABLE` = nothing sent;
never delete or truncate the log to pass a refusal (a crash-truncated last line is tolerated).

## 6. Interface facts (cpufreq / powercap / uncore) [DOC lnx-interfaces.md s2 unless tagged]
- cpufreq `scaling_max_freq` = soft per-core cap, never above HWP highest; used for stress and VID sampling. Pin `scaling_min_freq` = `scaling_max_freq`;
  a cap is quantised to 100 MHz bins and converted to HWP perf units, so some ratios are unreachable on hybrid parts (P and E sets differ):
  `power-profile-capping.md` §5-6 [MEASURED lnx 2026-10-12]. Always restore caps.
- RAPL powercap `/sys/class/powercap/intel-rapl:0/constraint_{0,1}_power_limit_uw` changes PL1/PL2 at runtime (`platform-arl-s.md` §12). ARL-S uses
  `rapl_defaults_core` (no PL4 via 0x601 in the kernel).
- Uncore: `intel_uncore_frequency` sysfs via 0x620/0x621; ring only vs ring+NGU unknown [UNVERIFIED]. TCC offset in 0x1A2 can be locked (bit 31).
  TPMI / intel-speed-select do not apply (server-only).
- Nothing in Linux exists for AVX offset, IccMax, BCLK, ring/NGU/D2D/SA V/F or per-core undervolt on ARL; no ASRock WMI driver. UnderVolter (UEFI)
  ARL claims unverified and its README mentions fault injection: do not run. intel-undervolt: not compatible with Tiger Lake and later.
- A `0x1D` change makes the kernel rescale capacity (`energy_model: updating cpuN cpu_cap`) [MEASURED lnx 2026-10-01].
