"""G-126 / G-147: ``scaling_max_freq`` follows ``cpuinfo_max_freq`` after a ``0x1D`` (favored ratio) changed it. One implementation for the helper's ``tune.apply`` and the CLI's
``apply`` (and so the restore scripts).

``cpuinfo_max_freq`` follows the HWP highest ratio the kernel sees (a ``0x1D`` moves it, ``[MEASURED lnx 2026-10-12]``), but ``scaling_max_freq`` keeps its old value, so a
raised ratio is never used and a lowered one stays above the new maximum. The rule, per CPU whose ratio changed: a cap that sat AT the old ``cpuinfo_max_freq`` (no user cap)
follows the new maximum (up or down); a cap the user set below the old maximum is **never raised**; a cap above a lowered maximum is lowered to it.

Pure decision (``follow_cap``) plus a runner over an injected cpufreq view (``read(cpu, attr) -> str | None`` / ``write(cpu, attr, value)``): no sysfs path is built here.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .model import StepKind
from .report import StepStatus

__all__ = ["CAP_ATTR", "MAX_ATTR", "POLL", "CpufreqView", "follow_cap", "lift", "oc_ratio_cpus", "snapshot"]

CAP_ATTR = "scaling_max_freq"
MAX_ATTR = "cpuinfo_max_freq"
#: ``cpuinfo_max_freq`` follows HWP highest a moment after the 0x1D: re-read the CPUs that have not moved this many rounds, this far apart (seconds). ``[UNVERIFIED]``: the kernel's lag.
POLL = (4, 0.25)


class CpufreqView(Protocol):
    def read(self, cpu: int, attr: str) -> str | None: ...

    def write(self, cpu: int, attr: str, value: str) -> None: ...


def oc_ratio_cpus(report: Any) -> tuple[list[int], list[int]]:
    """``(favored, vf)``: the CPUs whose 0x1D (OC ratio) an apply wrote successfully, and the others whose OC word (0x11) it wrote (the ratio is in its low byte
    too, so HWP highest might follow it): the first are expected to move, the second are only checked."""
    favored = {r.step.cpu for r in report.steps if r.step.kind is StepKind.WRITE_FAVORED and r.status is StepStatus.OK}
    vf = {r.step.cpu for r in report.steps if r.step.kind is StepKind.WRITE_VF and r.status is StepStatus.OK} - favored
    return sorted(favored), sorted(vf)


def _khz(value: str | None) -> int | None:
    return int(value) if value is not None and re.fullmatch(r"[0-9]{1,10}", value) else None


def snapshot(view: CpufreqView, cpus: Sequence[int]) -> dict[int, tuple[int | None, int | None]]:
    """``{cpu: (cpuinfo_max_freq, scaling_max_freq)}`` in kHz, read before the first write of an apply."""
    out: dict[int, tuple[int | None, int | None]] = {}
    for cpu in cpus:
        try:
            out[cpu] = (_khz(view.read(cpu, MAX_ATTR)), _khz(view.read(cpu, CAP_ATTR)))
        except OSError:
            out[cpu] = (None, None)
    return out


def follow_cap(old_max: int | None, new_max: int | None, cap: int | None) -> int | None:
    """The ``scaling_max_freq`` to write, or ``None`` to leave it: only when the maximum moved and the cap sat at the old maximum (no user cap) or lies above a lowered maximum.
    A cap below the old maximum is the user's and is never raised."""
    if old_max is None or new_max is None or cap is None or new_max == old_max or cap == new_max:
        return None
    if cap == old_max or (new_max < old_max and cap > new_max):
        return new_max
    return None


def lift(
    view: CpufreqView,
    before: Mapping[int, tuple[int | None, int | None]],
    favored: Sequence[int],
    checked: Sequence[int],
    warnings: list[str],
    *,
    owned: bool = False,
    poll: tuple[int, float] = POLL,
    wait: Callable[[float], Any] | None = None,
) -> list[dict[str, Any]]:
    """For each CPU whose OC ratio changed (``favored``: a ``0x1D`` was written, so ``cpuinfo_max_freq`` is expected to move and is waited for; ``checked``: only the
    ``0x11`` was written, listed only if its maximum moved): apply ``follow_cap``. ``owned``: a measure / stress journal owns the caps, nothing is written. Never raises: the
    hardware is already changed. Returns ``[{cpu, before_khz, after_khz}]`` (the cap before / after)."""
    cpus = sorted({*favored, *checked})
    if not cpus:
        return []
    if owned:
        warnings.append("a measure/stress cpufreq journal exists, so scaling_max_freq was not touched; fix cpufreq by hand (see the helper log) and check the frequency cap")
    now: dict[int, int | None] = {}
    rounds, interval = poll
    pending = list(cpus)
    for n in range(rounds):  # one shared wait: the kernel lags the 0x1D by a moment; only the CPUs a 0x1D was written to are waited for
        still = []
        for cpu in pending:
            try:
                now[cpu] = _khz(view.read(cpu, MAX_ATTR))
            except OSError:
                now[cpu] = None
            if cpu in favored and now[cpu] == before.get(cpu, (None, None))[0]:
                still.append(cpu)
        pending = still
        if not pending or n == rounds - 1:
            break
        if wait is not None:
            wait(interval)
    rows: list[dict[str, Any]] = []
    for cpu in cpus:
        old_max, old_cap = before.get(cpu, (None, None))
        new_max = now.get(cpu)
        moved = new_max is not None and old_max is not None and new_max != old_max
        if cpu not in favored and not moved:
            continue
        try:
            cap = _khz(view.read(cpu, CAP_ATTR))
            if old_cap is None:
                old_cap = cap
            after = cap
            target = None if owned else follow_cap(old_max, new_max, cap)
            if target is not None:
                view.write(cpu, CAP_ATTR, str(target))
                after = _khz(view.read(cpu, CAP_ATTR))  # read back: the kernel may clamp
            rows.append({"cpu": cpu, "before_khz": old_cap, "after_khz": after})
        except (OSError, ValueError) as exc:
            warnings.append(f"scaling_max_freq of cpu {cpu} could not be adjusted: {type(exc).__name__}: {exc}")
            rows.append({"cpu": cpu, "before_khz": old_cap, "after_khz": old_cap})
    return rows
