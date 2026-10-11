"""Command lines of the stress children (G-116, contract 9 addendum). Arguments come from enumerated values and integers in ranges only; nothing is a shell
string and nothing the caller typed reaches argv except through these builders.

``stress-ng`` flags: [DOC stress-ng(1)] ``--temp-path DIR``, ``--cpu N``, ``--cache N``, ``--taskset LIST``, ``--cpu-method``, ``--verify``, ``--metrics-brief``, ``--timeout Ns``.
``stressapptest`` flags: [DOC stressapptest --help] ``-s`` seconds, ``-M`` MB of memory, ``-W`` more CPU-stressful memory copy.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

__all__ = [
    "CHILD_ENV", "CPU_METHODS", "DURATION_RANGE", "MEMORY_MB_MIN", "STRESSAPPTEST", "STRESS_NG", "cpu_command", "cpu_list", "memory_command",
]

STRESS_NG = "/usr/bin/stress-ng"
STRESSAPPTEST = "/usr/bin/stressapptest"
CHILD_ENV = {"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"}  # fixed, nothing inherited (the helper's own environment never reaches a child)
DURATION_RANGE = (30, 3600)
MEMORY_MB_MIN = 256
#: method name (contract) -> (stressor flag, ``--cpu-method`` value or None). SPEC section 9.3: not called SSE / AVX until the executed instructions are verified.
CPU_METHODS: dict[str, tuple[str, str | None]] = {
    "integer": ("--cpu", "int64"),
    "vector-math": ("--cpu", "matrixprod"),
    "cache": ("--cache", None),
}


def cpu_list(cpus: Iterable[int]) -> str:
    """``[0, 1, 2, 16, 17]`` -> ``"0-2,16-17"`` (sorted, distinct, non-negative integers only)."""
    values = sorted(set(cpus))
    if not values or any(isinstance(c, bool) or not isinstance(c, int) or not 0 <= c < 1024 for c in values):
        raise ValueError("a CPU list needs at least one integer 0..1023")
    parts: list[str] = []
    start = prev = values[0]
    for c in values[1:]:
        if c == prev + 1:
            prev = c
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = c
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _check_duration(duration_s: object) -> int:
    lo, hi = DURATION_RANGE
    if isinstance(duration_s, bool) or not isinstance(duration_s, int) or not lo <= duration_s <= hi:
        raise ValueError(f"duration_s must be an integer {lo}..{hi}")
    return duration_s


def _check_temp_path(temp_path: str) -> str:
    if not isinstance(temp_path, str) or not temp_path.startswith("/") or "\0" in temp_path or temp_path == "/":
        raise ValueError("temp_path must be an absolute directory path (not /)")
    return temp_path


def cpu_command(exe: str, method: str, cpus: Sequence[int], duration_s: int, temp_path: str | None = None) -> list[str]:
    """``stress-ng --cpu N --taskset LIST --cpu-method <mapped> --verify --metrics-brief --timeout Ts [--temp-path DIR]`` (``cache``: ``--cache N`` and no
    method). ``--temp-path`` is the child's private directory (G-121): stress-ng refuses to run when its temp path is not readable and writeable."""
    if method not in CPU_METHODS:
        raise ValueError(f"method must be one of {', '.join(CPU_METHODS)}")
    flag, value = CPU_METHODS[method]
    lst = cpu_list(cpus)
    cmd = [exe, flag, str(len(set(cpus))), "--taskset", lst]
    if value is not None:
        cmd += ["--cpu-method", value]
    cmd += ["--verify", "--metrics-brief", "--timeout", f"{_check_duration(duration_s)}s"]
    if temp_path is not None:
        cmd += ["--temp-path", _check_temp_path(temp_path)]
    return cmd


def memory_command(exe: str, duration_s: int, memory_mb: int) -> list[str]:
    """``stressapptest -s T -M MB -W``."""
    if isinstance(memory_mb, bool) or not isinstance(memory_mb, int) or memory_mb < MEMORY_MB_MIN:
        raise ValueError(f"memory_mb must be an integer >= {MEMORY_MB_MIN}")
    return [exe, "-s", str(_check_duration(duration_s)), "-M", str(memory_mb), "-W"]
