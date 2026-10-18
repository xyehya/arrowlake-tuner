#: D-049: operator value limits of older policy files. They still load (ignored, one warning line, ``Policy.ignored_keys``); the last two are tables, named by their sub-keys.
LEGACY_KEYS = (
    "effective_offset_floor_mv",
    "assumed_bios_offset_mv",
    "offset_ceiling_mv",
    "max_ratio_step",
    "max_v_step_mv",
    "max_offset_step_mv",
    "ratio_max",
    "v_target_max_mv",
    "power",
    "runtime",
)
_LEGACY_TABLES = frozenset({"power", "runtime"})
_TOP_KEYS = frozenset({"cpus", "max_operations_per_session", "max_operations_per_boot", "min_dwell_s", "allow", "msr_allow", "e_cpus", *LEGACY_KEYS})
_ENTRY_KEYS = frozenset({"name", "cmd", "p1", "p2", "kind", "check", "params"})
_ENTRY_REQUIRED = frozenset({"name", "cmd", "p1", "p2", "kind"})
_MSR_ENTRY_KEYS = frozenset({"name", "msr", "kind", "cpus", "check", "params"})
_MSR_ENTRY_REQUIRED = frozenset({"name", "msr", "kind"})
