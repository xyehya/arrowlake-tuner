"""The OC-mailbox transaction: poll idle -> WRMSR request -> poll done -> decode. Deliberately dumb.

Sequence and limits follow what Intel's own driver does (``docs/knowledge/registers.md`` "OC mailbox -- request model",
"XTU/IPF sequence (T-005)": read MSR 0x150 until bit 63 = 0 in 10 ms steps with a ~10 s cap, timeout => abort WITHOUT
writing, WRMSR, poll again, no retry, no rollback). Every mailbox request, reads included, is a WRMSR, so the ONLY
caller of ``MailboxTransport.transact`` must be ``arltuner.guard`` (docs/ARCHITECTURE.md). This module contains no
allowlist, no confirmation and no policy on purpose.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from arltuner.mailbox import MSR_OC_MAILBOX, Response, is_busy, pack, unpack

from .port import MailboxTimeout, MsrPort, check_cpu


def _check_seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}={value!r} must be finite and >= 0")
    return float(value)


class MailboxTransport:
    """One mailbox transaction at a time on one CPU's MSR 0x150 (registers.md "OC mailbox -- request model").

    ``sleep`` / ``clock`` are injectable so tests run instantly: the timeout is measured as ``clock()`` elapsed since
    the start of each polling phase, and ``sleep(poll_sleep_s)`` separates two reads.
    """

    def __init__(
        self,
        port: MsrPort,
        *,
        poll_sleep_s: float = 0.01,
        timeout_s: float = 10.0,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._poll_sleep_s = _check_seconds("poll_sleep_s", poll_sleep_s)
        self._timeout_s = _check_seconds("timeout_s", timeout_s)
        self._sleep = sleep
        self._clock = clock

    def _wait_idle(self, cpu: int, phase: str) -> int:
        """Read MSR 0x150 until bit 63 is clear; return that raw value. At least one read is always made."""
        start = self._clock()
        while True:
            raw = self._port.rdmsr(cpu, MSR_OC_MAILBOX)
            if not is_busy(raw):
                return raw
            if self._clock() - start >= self._timeout_s:
                raise MailboxTimeout(f"busy {phase} write", phase=phase)
            self._sleep(self._poll_sleep_s)

    def transact(self, cpu: int, cmd: int, p1: int, p2: int, data: int = 0) -> Response:
        """Run one mailbox request on ``cpu`` and return its decoded completion.

        1. poll ``rdmsr(cpu, 0x150)`` until bit 63 is clear; on timeout raise ``MailboxTimeout("busy before write")``
           with NO ``wrmsr`` having happened;
        2. ``wrmsr(cpu, 0x150, pack(cmd, p1, p2, data))``;
        3. poll until bit 63 is clear; on timeout raise ``MailboxTimeout("busy after write")`` (the write was sent);
        4. return ``unpack`` of the last value read.

        No retry and no loop around failures; ``MsrError`` from the port propagates. Invalid arguments raise
        ``ValueError`` before any MSR access.
        """
        cpu = check_cpu(cpu)
        word = pack(cmd, p1, p2, data)  # validates cmd/p1/p2/data before anything is read or written
        self._wait_idle(cpu, "before")
        self._port.wrmsr(cpu, MSR_OC_MAILBOX, word)
        return unpack(self._wait_idle(cpu, "after"))
