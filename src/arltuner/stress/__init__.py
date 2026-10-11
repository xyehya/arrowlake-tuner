"""Stress / validation backend pieces (G-116, contract 9 addendum): command builders, output parsers, kernel-log watch, result store. Pure stdlib; no
process is started here and nothing touches hardware (the helper op ``arltuner.helper.ops_stress`` owns the run)."""
from .cmd import CHILD_ENV, CPU_METHODS, DURATION_RANGE, MEMORY_MB_MIN, STRESS_NG, STRESSAPPTEST, cpu_command, cpu_list, memory_command
from .klog import BAD_KLOG, JournalLog, KernelLogWatch, KmsgLog, NullLog, count_bad
from .parse import MAX_ERROR_LINES, StressappScanner, StressNgScanner, Verdict, clean_line
from .store import LIST_LIMIT, MAX_RECORDS, StressStore, summary_row

__all__ = [
    "BAD_KLOG", "CHILD_ENV", "CPU_METHODS", "DURATION_RANGE", "JournalLog", "KernelLogWatch", "KmsgLog", "LIST_LIMIT", "MAX_ERROR_LINES", "MAX_RECORDS",
    "MEMORY_MB_MIN", "NullLog", "STRESSAPPTEST", "STRESS_NG", "StressNgScanner", "StressStore", "StressappScanner", "Verdict", "clean_line", "count_bad",
    "cpu_command", "cpu_list", "memory_command", "summary_row",
]
