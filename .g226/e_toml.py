P = "policies/default.toml"
s = open(P).read()

a = s.index("# policies/default.toml")
b = s.index("cpus = \"auto\"")
header = '''# policies/default.toml -- the shipped ROOT POLICY (D-049: CPU-enforced limits only; D-036: generic, derived live from the CPU).
# install.sh seeds /etc/arltuner/policy.toml from this file ONLY when that file is absent (--reseed-config replaces an existing one). It names no
# machine: the CPU set is "auto" and every limit that depends on the CPU is read from the CPU in the session.
#
# THERE ARE NO OPERATOR VALUE LIMITS HERE (D-049). The program refuses a value only when the CPU / the kernel itself cannot take it, read live in the session
# (nothing read = nothing may be raised, CPU_LIMIT_UNREAD names the read):
#   ratio        cmd 0x01 [7:0] fused max OC ratio of the core kind (P read on a P CPU, E on an E CPU, ring with P1 = 2)
#   voltage      cmd 0x28 [11:0] - 1 LSB (XTU's rule; field 0 = 2.0 V) of the domain; the predicted voltage (V target or fused voltage + a positive offset) may not exceed it
#   offset       the 11-bit field: -1024 .. +1023 raw = -1000 .. +999 mV
#   power        MSR 0x610: the lock bit 63 (a locked limit is not written) and the 15-bit PL fields in the units of MSR 0x606
#   caps         cpufreq cpuinfo_min_freq .. cpuinfo_max_freq, and not below scaling_min_freq
#   uncore       intel_uncore_frequency initial_min_freq_khz .. initial_max_freq_khz, min <= max
# A large single change, a positive or deep whole-curve offset, a non-monotonic or raised turbo table, PL2 below PL1 are WARNINGS of the Review, never refusals.
# Older policy files may still carry max_ratio_step, max_v_step_mv, max_offset_step_mv, offset_ceiling_mv, effective_offset_floor_mv, assumed_bios_offset_mv, [ratio_max],
# [v_target_max_mv], [power] and [runtime]: they still load, are IGNORED, and one warning line names them. Remove them.
#
# What is KEPT (safety mechanisms, not limits): one mailbox transaction at a time (2026-08-26 incident: sweeps, not budgets), the allowlist below
# (default deny), the health gate, the audit log, the plan-bound nonce of tune.apply, the read-back of every write (a mismatch trips the guard).
# Static/override voltage stays out (D-016: mode must be adaptive).
# Defaults in this file:
#   CPUs         "auto": the P and E CPUs of the live topology (P = devices/cpu_core, E = devices/cpu_atom)
#   op caps      indicators only: 100000 per session and per boot; no dwell between operations

'''
s = s[:a] + header + s[b:]

a = s.index("# Step limits (below) also decide")
b = s.index("# --- allowlist")
s = s[:a] + "\n" + s[b:]

a = s.index("# (every byte non-zero, non-increasing bucket 0..7")
b = s.index("[[msr_allow]]\nname = \"msr_0xce_read\"")
s = s[:a] + "# (every byte non-zero and at most the CPU's own ratio limit cmd 0x01 of its kind; a raise needs that limit read this session). ---\n" + s[b:]
s = s.replace("# --- plain-MSR access (T-023, D-017): the turbo ratio tables. Writes: 0x650 (E) and 0x1AD (P) on CPU 0 only, under turbo_ratio_bounded\n", "# --- plain-MSR access (T-023, D-017): the turbo ratio tables. Writes: 0x650 (E) and 0x1AD (P) on CPU 0 only, under turbo_ratio_bounded\n")

a = s.index("# --- package power limits (G-214, D-039): THE BOUNDS BELOW")
s = s[:a].rstrip("\n") + "\n"
s += '''
# --- package power limits and runtime controls (G-214, G-216; D-049): no keys. PL1 / PL2 / tau go through the powercap files and are refused only for a set lock bit (MSR 0x610 bit 63)
# or a value the register cannot hold; per-CPU caps and the uncore limits are bounded by the kernel's own files (cpuinfo_min/max_freq, initial_min/max_freq_khz). A revert to the values
# this boot started with (power.plan / runtime.plan {revert: true}) restores the recorded state. ---
'''
open(P, "w").write(s)
print("ok")
