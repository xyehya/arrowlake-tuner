"""G-216: the offline-CPU guard. Core parking is held out of this app, but an operator (or a power daemon) can still take a CPU offline: ``cpu<N>/online`` reads 0 and the live topology
(``cpu_core/cpus``, ``cpu_atom/cpus``) loses it, which renumbers the P indices and can leave the E clusters unequal. Every op that numbers things by that topology (``tune.apply``,
``vf.measure``, ``stress.start``, ``runtime.plan`` / ``runtime.apply`` -- a runtime *revert* excepted: it restores per-CPU files by number, skips an offline CPU and says so) refuses, naming the CPUs, while one reads 0. "Cannot tell" (no wiring, an unreadable file) is not a refusal: only a
definite 0 is. Reads only."""
from __future__ import annotations

import re
from typing import Any

from .protocol import REFUSED, OpError

__all__ = ["offline_cpus", "refuse_if_cpu_offline"]

_CPU_DIR = re.compile(r"cpu(\d+)")


def offline_cpus(helper: Any) -> list[int]:
    """The CPUs whose ``online`` file reads 0 (sorted); ``[]`` when none or when the runtime sysfs cannot be consulted."""
    from .ops_runtime import _wiring  # lazy: ops_runtime imports ops_tune, which imports this module

    try:
        files = _wiring(helper).files
        cpus = sorted(int(m.group(1)) for name in files.listdir("") if (m := _CPU_DIR.fullmatch(name)))
        return [cpu for cpu in cpus if files.read_int(f"cpu{cpu}/online") == 0]
    except Exception:
        return []


def refuse_if_cpu_offline(helper: Any, op: str) -> None:
    """Raise ``REFUSED`` (``check = CPU_OFFLINE``) when a CPU is offline; nothing has been done by ``op`` yet."""
    offline = offline_cpus(helper)
    if offline:
        names = ", ".join(f"cpu {c}" for c in offline)
        raise OpError(
            REFUSED,
            f"{names} {'is' if len(offline) == 1 else 'are'} offline (cpu<N>/online reads 0), not by this app: the CPU topology the offsets, caps and units are numbered by has changed. "
            f"Bring {'it' if len(offline) == 1 else 'them'} back online (echo 1 > /sys/devices/system/cpu/cpuN/online) or reboot, then {op} again. Nothing was written.",
            {"check": "CPU_OFFLINE", "cpus": offline},
        )
