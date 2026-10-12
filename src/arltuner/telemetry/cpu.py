"""Per-CPU MSR telemetry: APERF/MPERF effective frequency and busy, requested VID, MSR digital thermal sensors.

Decodes (registers.md / telemetry-sources.md):
  * effective MHz = tsc_mhz * dAPERF / dMPERF ; busy % = 100 * dMPERF / (tsc_mhz * 1e6 * dt)   [DOC Intel SDM vol 3 14.2]
    tsc_mhz = 100 MHz * PLATFORM_INFO[15:8].
  * 0x198 PERF_STATUS: ratio [15:8], requested VID [47:32] / 8192 V (a request, not delivered voltage) [MEASURED lnx 2026-09-29]
  * 0x19C / 0x1B1: valid [31], digital readout [22:16] (degrees below Tjmax), Tjmax = 0x1A2[23:16] [DOC SDM; 0x1A2 MEASURED]
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from arltuner.msr.port import MsrError

from .msrio import (MSR_APERF, MSR_MPERF, MSR_PACKAGE_THERM_STATUS, MSR_PERF_STATUS, MSR_PLATFORM_INFO,
                    MSR_TEMPERATURE_TARGET, MSR_THERM_STATUS, TelemetryMsr, bits)
from .readings import Reading, unavailable

BUS_MHZ = 100  # [DOC] ARL BCLK 100 MHz; tsc ratio from PLATFORM_INFO
_M64 = (1 << 64) - 1


def tsc_mhz(msr: TelemetryMsr, cpu: int = 0) -> Reading:
    try:
        ratio = bits(msr.rdmsr(cpu, MSR_PLATFORM_INFO), 15, 8)
    except (MsrError, ValueError) as e:
        return unavailable("msr 0xCE", str(e), "MHz")
    if ratio == 0:
        return unavailable("msr 0xCE", "base ratio reads 0", "MHz")
    return Reading(ratio * BUS_MHZ, "MHz", "msr 0xCE[15:8]")


class FreqSampler:
    """Keeps the previous APERF/MPERF per CPU; each ``sample`` returns (mhz, busy_pct) readings per CPU."""

    def __init__(self, msr: TelemetryMsr, tsc_mhz_value: float, clock: Callable[[], float] = time.monotonic) -> None:
        if not tsc_mhz_value or tsc_mhz_value <= 0:
            raise ValueError("tsc_mhz must be positive")
        self._msr, self._tsc, self._clock = msr, float(tsc_mhz_value), clock
        self._prev: dict[int, tuple[float, int, int]] = {}

    def sample(self, cpus: Iterable[int]) -> dict[int, tuple[Reading, Reading]]:
        out: dict[int, tuple[Reading, Reading]] = {}
        for cpu in cpus:
            src = f"msr 0xE8/0xE7 cpu{cpu}"
            try:
                now = self._clock()
                a = self._msr.rdmsr(cpu, MSR_APERF)
                m = self._msr.rdmsr(cpu, MSR_MPERF)
            except (MsrError, ValueError) as e:
                self._prev.pop(cpu, None)
                out[cpu] = (unavailable(src, str(e), "MHz"), unavailable(src, str(e), "%"))
                continue
            prev = self._prev.get(cpu)
            self._prev[cpu] = (now, a, m)
            if prev is None:
                out[cpu] = (unavailable(src, "first sample (need two)", "MHz"),
                            unavailable(src, "first sample (need two)", "%"))
                continue
            t0, a0, m0 = prev
            da, dm, dt = (a - a0) & _M64, (m - m0) & _M64, now - t0
            if dt <= 0:
                out[cpu] = (unavailable(src, "no time elapsed", "MHz"), unavailable(src, "no time elapsed", "%"))
            elif dm == 0:
                # core idle the whole interval (MPERF only counts in C0): frequency is undefined, busy is 0
                out[cpu] = (unavailable(src, "idle (MPERF did not advance)", "MHz"), Reading(0.0, "%", src))
            else:
                busy = min(100.0, 100.0 * dm / (self._tsc * 1e6 * dt))
                out[cpu] = (Reading(self._tsc * da / dm, "MHz", src), Reading(busy, "%", src))
        return out


def read_vid(msr: TelemetryMsr, cpu: int) -> tuple[Reading, Reading]:
    """(requested VID in V, running ratio) from 0x198; unavailable when the MSR cannot be read."""
    src = f"msr 0x198 cpu{cpu}"
    try:
        w = msr.rdmsr(cpu, MSR_PERF_STATUS)
    except (MsrError, ValueError) as e:
        return unavailable(src, str(e), "V"), unavailable(src, str(e), "x")
    return Reading(bits(w, 47, 32) / 8192.0, "V", src), Reading(bits(w, 15, 8), "x", src)


def read_tjmax(msr: TelemetryMsr, cpu: int = 0) -> Reading:
    try:
        tj = bits(msr.rdmsr(cpu, MSR_TEMPERATURE_TARGET), 23, 16)
    except (MsrError, ValueError) as e:
        return unavailable("msr 0x1A2", str(e), "C")
    return Reading(tj, "C", "msr 0x1A2[23:16]") if tj else unavailable("msr 0x1A2", "Tjmax reads 0", "C")


def _therm(msr: TelemetryMsr, cpu: int, addr: int, tjmax: Reading) -> Reading:
    src = f"msr {addr:#x} cpu{cpu}"
    if not tjmax.available:
        return unavailable(src, f"Tjmax unavailable: {tjmax.reason}", "C")
    try:
        w = msr.rdmsr(cpu, addr)
    except (MsrError, ValueError) as e:
        return unavailable(src, str(e), "C")
    if not bits(w, 31, 31):
        return unavailable(src, "reading-valid bit clear", "C")
    return Reading(tjmax.value - bits(w, 22, 16), "C", src)


def read_core_temp_msr(msr: TelemetryMsr, cpu: int, tjmax: Reading) -> Reading:
    return _therm(msr, cpu, MSR_THERM_STATUS, tjmax)


def read_package_temp_msr(msr: TelemetryMsr, cpu: int, tjmax: Reading) -> Reading:
    return _therm(msr, cpu, MSR_PACKAGE_THERM_STATUS, tjmax)


def read_limit_reasons_raw(msr: TelemetryMsr, cpu: int = 0) -> Reading:
    """Raw 0x6B0 word, NOT decoded (decode open in registers.md)."""
    from .msrio import MSR_PKG_PERF_LIMIT_REASONS
    src = "msr 0x6B0 (raw)"
    try:
        return Reading(msr.rdmsr(cpu, MSR_PKG_PERF_LIMIT_REASONS), "raw", src)
    except (MsrError, ValueError) as e:
        return unavailable(src, str(e), "raw")
