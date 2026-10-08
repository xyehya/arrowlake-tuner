"""The MSR port abstraction (Contract 4, docs/ARCHITECTURE.md): errors, the ``MsrPort`` protocol, argument checks.

No I/O here. A port reads/writes one 64-bit MSR on one Linux CPU; it knows nothing about the OC mailbox
(``docs/research/registers.md`` "OC mailbox -- request model": every mailbox request, reads included, is a WRMSR to
MSR 0x150 -- that framing lives in ``transport.py``). Policy (what may be written) is not here either: that is
``arltuner.guard`` (T-013).

Values are unsigned 64-bit ints. ``check_cpu`` / ``check_msr`` / ``check_value`` are shared by every port so that
out-of-range or non-int arguments fail with ``ValueError`` *before* any device is touched.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

MAX_CPU = 8191  # kernel NR_CPUS upper bound; a sanity limit, not a claim about this machine (which has 24 CPUs)
MAX_MSR = 0xFFFFFFFF  # MSR addresses are 32-bit; the /dev/cpu/N/msr file offset is the MSR number
MAX_VALUE = 0xFFFFFFFFFFFFFFFF  # MSR values are unsigned 64-bit


class MsrError(OSError):
    """An MSR access failed. Construct as ``MsrError(errno.EIO, "what happened")`` so ``.errno`` is set."""


class MailboxTimeout(Exception):
    """The OC mailbox stayed busy (MSR 0x150 bit 63) past the transport timeout.

    Message ``"busy before write"``: nothing was written. Message ``"busy after write"``: the request WAS written and
    its outcome is unknown (registers.md: XTU/IPF behaves the same -- no retry, no rollback).

    Structured form: ``phase`` is ``"before"`` or ``"after"`` (``None`` if constructed without it) and ``write_sent`` is
    true exactly when ``phase == "after"`` -- callers must not parse the message.
    """

    def __init__(self, message: str = "", *, phase: str | None = None) -> None:
        super().__init__(message)
        if phase not in (None, "before", "after"):
            raise ValueError(f"phase must be 'before', 'after' or None, got {phase!r}")
        self.phase = phase

    @property
    def write_sent(self) -> bool:
        """True when the request was written before the timeout (outcome unknown)."""
        return self.phase == "after"


@runtime_checkable
class MsrPort(Protocol):
    """Raw per-CPU MSR access. ``rdmsr``/``wrmsr`` raise ``MsrError``; a port with ``writable == False`` refuses
    every ``wrmsr`` with ``MsrError(EPERM)``. ``ValueError`` for out-of-range / non-int arguments."""

    writable: bool

    def rdmsr(self, cpu: int, msr: int) -> int: ...

    def wrmsr(self, cpu: int, msr: int, value: int) -> None: ...


def _check_int(name: str, value: object, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if not 0 <= value <= hi:
        raise ValueError(f"{name}={value} out of range 0..{hi:#x}")
    return int(value)


def check_cpu(cpu: object) -> int:
    """Return ``cpu`` as a plain int in ``0..MAX_CPU`` or raise ``ValueError``."""
    return _check_int("cpu", cpu, MAX_CPU)


def check_msr(msr: object) -> int:
    """Return ``msr`` as a plain int in ``0..0xFFFFFFFF`` or raise ``ValueError``."""
    return _check_int("msr", msr, MAX_MSR)


def check_value(value: object) -> int:
    """Return ``value`` as a plain int in ``0..2**64-1`` or raise ``ValueError``."""
    return _check_int("value", value, MAX_VALUE)
