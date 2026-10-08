"""Read-only MSR access for telemetry. The only MSR door the sampling thread has.

``TelemetryMsr`` wraps any ``MsrPort`` (a fake in tests), exposes ``rdmsr`` only and refuses every address not in
``TELEMETRY_MSRS`` -- in particular 0x150 (the OC mailbox: every mailbox request is a WRMSR, docs/knowledge/registers.md).
The telemetry package must not import ``arltuner.mailbox`` / ``arltuner.guard`` / ``arltuner.msr.transport``
(enforced by tests/telemetry/test_isolation.py).

The set below must equal the proposed read rows in docs/knowledge/telemetry-sources.md; the orchestrator mirrors them into
``guard/policy.py`` ``PLAIN_MSR_READABLE`` at merge. Evidence tags: see that document.
"""
from __future__ import annotations

import errno

from arltuner.msr.port import MsrError, MsrPort, check_cpu, check_msr

MSR_MPERF = 0xE7  # [DOC Intel SDM vol 4]
MSR_APERF = 0xE8  # [DOC Intel SDM vol 4]
MSR_PLATFORM_INFO = 0xCE  # [15:8] = 37 on this CPU [MEASURED lnx 2026-09-29]
MSR_PERF_STATUS = 0x198  # [MEASURED lnx 2026-09-29]
MSR_THERM_STATUS = 0x19C  # [DOC Intel SDM vol 4]
MSR_TEMPERATURE_TARGET = 0x1A2  # [23:16] Tjmax 105 [MEASURED]
MSR_PACKAGE_THERM_STATUS = 0x1B1  # [DOC Intel SDM vol 4]
MSR_RAPL_POWER_UNIT = 0x606  # [MEASURED lnx 2026-09-29] 0.125 W, 2^-14 J, 2^-10 s
MSR_PKG_ENERGY_STATUS = 0x611  # [DOC Intel SDM vol 4]
MSR_CORE_PERF_LIMIT_REASONS = 0x64F  # live word, decode open [MEASURED raw]
MSR_PKG_PERF_LIMIT_REASONS = 0x6B0  # raw only, decode open [MEASURED raw]

TELEMETRY_MSRS = frozenset({
    MSR_MPERF, MSR_APERF, MSR_PLATFORM_INFO, MSR_PERF_STATUS, MSR_THERM_STATUS, MSR_TEMPERATURE_TARGET,
    MSR_PACKAGE_THERM_STATUS, MSR_RAPL_POWER_UNIT, MSR_PKG_ENERGY_STATUS, MSR_CORE_PERF_LIMIT_REASONS,
    MSR_PKG_PERF_LIMIT_REASONS,
})

MAILBOX_MSR = 0x150


class TelemetryMsr:
    """``rdmsr`` on allowlisted telemetry MSRs only. No ``wrmsr`` attribute exists."""

    __slots__ = ("_port",)

    def __init__(self, port: MsrPort) -> None:
        self._port = port

    def rdmsr(self, cpu: int, msr: int) -> int:
        check_cpu(cpu)
        check_msr(msr)
        if msr not in TELEMETRY_MSRS:
            raise MsrError(errno.EPERM, f"MSR {msr:#x} is not a telemetry read-allowlist entry")
        return self._port.rdmsr(cpu, msr)


def bits(value: int, hi: int, lo: int) -> int:
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)
