"""Read-only MSR access for telemetry. The only MSR door the sampling thread has.

``TelemetryMsr`` wraps any ``MsrPort`` (a fake in tests), exposes ``rdmsr`` only and refuses every address not in
``TELEMETRY_MSRS`` -- in particular 0x150 (the OC mailbox: every mailbox request is a WRMSR, docs/knowledge/registers.md).
The telemetry package must not import ``arltuner.mailbox`` / ``arltuner.guard`` / ``arltuner.msr.transport``
(enforced by tests/telemetry/test_isolation.py).

The set below must equal the telemetry read rows in docs/knowledge/telemetry-sources.md; the orchestrator mirrors them into
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
MSR_CORE_PERF_LIMIT_REASONS = 0x64F  # core limit word; decode telemetry/limits.py [MEASURED lnx 2026-10-13: 0x1DC21000]
MSR_GFX_PERF_LIMIT_REASONS = 0x6B0  # GRAPHICS limit word (SDM MSR_GRAPHICS_PERF_LIMIT_REASONS), not a package word [DOC SDM vol 4]

TELEMETRY_MSRS = frozenset({
    MSR_MPERF, MSR_APERF, MSR_PLATFORM_INFO, MSR_PERF_STATUS, MSR_THERM_STATUS, MSR_TEMPERATURE_TARGET,
    MSR_PACKAGE_THERM_STATUS, MSR_RAPL_POWER_UNIT, MSR_PKG_ENERGY_STATUS, MSR_CORE_PERF_LIMIT_REASONS,
    MSR_GFX_PERF_LIMIT_REASONS,
})

MAILBOX_MSR = 0x150

# G-118: the turbo ratio tables, READ by ``vf.measure`` to learn each unit's real max ratio (plain rdmsr; both are writable control MSRs elsewhere, so they are NOT in
# ``TELEMETRY_MSRS`` -- the sampler cannot read them -- but in this separate read-only set that only the measurement's reader opts into). D-026 rule: rdmsr only.
MSR_TURBO_RATIO_P = 0x1AD  # 8 buckets, bucket 0 = 1 active core; stock 57,57,55x6 [MEASURED lnx 2026-10-11]
MSR_TURBO_RATIO_E = 0x650  # 8 buckets; stock 48 x8 [MEASURED lnx 2026-10-11]
TURBO_TABLE_MSRS = TELEMETRY_MSRS | {MSR_TURBO_RATIO_P, MSR_TURBO_RATIO_E}


class TelemetryMsr:
    """``rdmsr`` on allowlisted telemetry MSRs only. No ``wrmsr`` attribute exists."""

    __slots__ = ("_port", "_allow")

    def __init__(self, port: MsrPort, allow: frozenset[int] = TELEMETRY_MSRS) -> None:
        self._port = port
        self._allow = allow

    def rdmsr(self, cpu: int, msr: int) -> int:
        check_cpu(cpu)
        check_msr(msr)
        if msr not in self._allow:
            raise MsrError(errno.EPERM, f"MSR {msr:#x} is not a telemetry read-allowlist entry")
        return self._port.rdmsr(cpu, msr)


def bits(value: int, hi: int, lo: int) -> int:
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)
