"""OC-mailbox request/response words: pure pack/unpack, no I/O.

Source of every fact here: ``docs/knowledge/registers.md``, section "OC mailbox -- request model (from XTU)".
Status tag: ``decomp`` (wire packing decoded from XTU's ``ipf_cpu.sys`` and ThrottleStop 9.7.3; matches
FSP/V0LTpwn/kernel ``doc``). Not yet MEASURED on this CPU, so nothing here is a write allowlist entry.

Wire format (WRMSR to MSR 0x150; a mailbox "read" is also a WRMSR)::

    raw = 1<<63 | p2<<48 | p1<<40 | cmd<<32 | data

Completion: poll RDMSR 0x150 until bit 63 clears; completion code = bits [39:32], result data = bits [31:0].
"""
from __future__ import annotations

from dataclasses import dataclass

MSR_OC_MAILBOX = 0x150  # registers.md "OC mailbox -- request model", "MSRs" table (0x150)

_BUSY_SHIFT = 63
_U8_MAX = 0xFF
_U32_MAX = 0xFFFFFFFF
_U64_MAX = 0xFFFFFFFFFFFFFFFF


def _is_int(value: object) -> bool:
    """True for real ints (IntEnum members included), False for bool and everything else."""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_range(name: str, value: object, hi: int) -> int:
    if not _is_int(value):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    assert isinstance(value, int)
    if not 0 <= value <= hi:
        raise ValueError(f"{name}={value} out of range 0..{hi:#x}")
    return value


def pack(cmd: int, p1: int, p2: int, data: int) -> int:
    """Build the 64-bit MSR 0x150 request word ``(1<<63)|(p2<<48)|(p1<<40)|(cmd<<32)|data``.

    ``cmd``, ``p1``, ``p2`` must be ints in 0..255 and ``data`` an int in 0..2**32-1; anything else
    (negative, too large, bool, float, ...) raises ``ValueError`` and nothing is built.
    """
    cmd = _check_range("cmd", cmd, _U8_MAX)
    p1 = _check_range("p1", p1, _U8_MAX)
    p2 = _check_range("p2", p2, _U8_MAX)
    data = _check_range("data", data, _U32_MAX)
    return (1 << _BUSY_SHIFT) | (p2 << 48) | (p1 << 40) | (cmd << 32) | data


def is_busy(raw: int) -> bool:
    """True when bit 63 (the run/busy bit) of a raw MSR 0x150 value is set."""
    raw = _check_range("raw", raw, _U64_MAX)
    return bool((raw >> _BUSY_SHIFT) & 1)


@dataclass(frozen=True)
class Response:
    """Decoded completion: ``rc`` = bits [39:32] (0 = Pass), ``data`` = bits [31:0]. Bit 63 is never part of either."""

    rc: int
    data: int


def unpack(raw: int) -> Response:
    """Split a raw MSR 0x150 value into ``Response(rc=(raw>>32)&0xFF, data=raw&0xFFFFFFFF)``."""
    raw = _check_range("raw", raw, _U64_MAX)
    return Response(rc=(raw >> 32) & _U8_MAX, data=raw & _U32_MAX)
