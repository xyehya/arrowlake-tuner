"""RAPL energy -> power. Primary source: powercap sysfs ``energy_uj`` (root-only since kernel 5.10 [DOC]); fallback: MSR 0x611.

Powercap layout under an injectable root: ``intel-rapl:0`` (package), ``intel-rapl:0:N`` subdomains with a ``name`` file
("core", "uncore", ...) [DOC Documentation/power/powercap/powercap.rst]. Counters wrap at ``max_energy_range_uj`` (sysfs) or 2^32
units (MSR 0x611 [31:0] [DOC SDM]); energy unit = 2^-ESU J with ESU = 0x606[12:8] (14 on this CPU [MEASURED lnx 2026-09-29]).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from arltuner.msr.port import MsrError

from .msrio import MSR_PKG_ENERGY_STATUS, MSR_RAPL_POWER_UNIT, TelemetryMsr, bits
from .readings import Reading, unavailable

DEFAULT_POWERCAP_ROOT = Path("/sys/class/powercap")


def _int(p: Path) -> int | None:
    try:
        return int(p.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


class _Delta:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._prev: tuple[float, int] | None = None

    def watts(self, counter: int, wrap: int, joules_per_count: float, src: str) -> Reading:
        now = self._clock()
        prev, self._prev = self._prev, (now, counter)
        if prev is None:
            return unavailable(src, "first sample (need two)", "W")
        t0, c0 = prev
        if now <= t0:
            return unavailable(src, "no time elapsed", "W")
        return Reading(((counter - c0) % wrap) * joules_per_count / (now - t0), "W", src)


class PowercapPower:
    """Power of one powercap zone (``zone`` = 'intel-rapl:0' package, or a subdomain such as 'intel-rapl:0:0')."""

    def __init__(self, zone: str = "intel-rapl:0", root: Path = DEFAULT_POWERCAP_ROOT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._dir = Path(root) / zone
        self._d = _Delta(clock)

    def name(self) -> str | None:
        try:
            return (self._dir / "name").read_text(encoding="ascii").strip()
        except OSError:
            return None

    def sample(self) -> Reading:
        src = f"powercap {self._dir.name}/energy_uj"
        e = _int(self._dir / "energy_uj")
        if e is None:
            return unavailable(src, "energy_uj missing or unreadable (root-only since 5.10)", "W")
        rng = _int(self._dir / "max_energy_range_uj")
        if not rng or rng <= 0:
            return unavailable(src, "max_energy_range_uj missing", "W")
        return self._d.watts(e, rng + 1, 1e-6, src)  # counter wraps back to 0 after max_energy_range_uj


class MsrPackagePower:
    """Package power from MSR 0x611 and the unit in 0x606[12:8]."""

    def __init__(self, msr: TelemetryMsr, cpu: int = 0, clock: Callable[[], float] = time.monotonic) -> None:
        self._msr, self._cpu, self._d = msr, cpu, _Delta(clock)

    def sample(self) -> Reading:
        src = "msr 0x611"
        try:
            esu = bits(self._msr.rdmsr(self._cpu, MSR_RAPL_POWER_UNIT), 12, 8)
            e = bits(self._msr.rdmsr(self._cpu, MSR_PKG_ENERGY_STATUS), 31, 0)
        except (MsrError, ValueError) as ex:
            return unavailable(src, str(ex), "W")
        return self._d.watts(e, 1 << 32, 2.0 ** -esu, src)
