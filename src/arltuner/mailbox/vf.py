"""The V/F data word carried by mailbox cmd 0x10 (read) / 0x11 (write): pure encode/decode, no I/O.

Source of every fact here: ``docs/knowledge/registers.md``, section "V/F data word -- cmd 0x10 (read) / 0x11 (write),
P1=0 P2=0, core-scoped". Status tag: ``decomp`` (XTU + ThrottleStop 9.7.3), corroborated by ``trace`` words from
this system. Not yet MEASURED by a readback, so nothing here is a write allowlist entry.

Layout of the 32-bit word::

    [7:0]    ratio          integer multiplier
    [19:8]   v_target_raw   12 bit, volts = raw/1024
    [20]     static         0 = adaptive, 1 = static/override
    [31:21]  offset_raw     11 bit signed two's complement, volts = raw/1024

mV -> offset_raw uses ThrottleStop/XTU rounding: ``(|mV|*1024 + 512) // 1000`` on the magnitude, sign preserved.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

_RATIO_MAX = 0xFF
_V_RAW_MAX = 0xFFF
_OFFSET_MIN = -1024
_OFFSET_MAX = 1023
_U32_MAX = 0xFFFFFFFF


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_field(name: str, value: object, lo: int, hi: int) -> None:
    if not _is_int(value):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    assert isinstance(value, int)
    if not lo <= value <= hi:
        raise ValueError(f"{name}={value} out of range {lo}..{hi}")


@dataclass(frozen=True)
class VfWord:
    """One decoded V/F word. Every field is range-checked on construction (``ValueError`` on violation)."""

    ratio: int  # [7:0]   0..255
    v_target_raw: int  # [19:8]  0..4095, volts = raw/1024
    static: bool  # [20]    False = adaptive (M1 never sets True)
    offset_raw: int  # [31:21] signed 11-bit, -1024..1023, volts = raw/1024

    def __post_init__(self) -> None:
        _check_field("ratio", self.ratio, 0, _RATIO_MAX)
        _check_field("v_target_raw", self.v_target_raw, 0, _V_RAW_MAX)
        if not isinstance(self.static, bool):
            raise ValueError(f"static must be a bool, got {type(self.static).__name__}")
        _check_field("offset_raw", self.offset_raw, _OFFSET_MIN, _OFFSET_MAX)

    @classmethod
    def from_u32(cls, w: int) -> "VfWord":
        """Decode a 32-bit V/F word. Total over 0..2**32-1 (every bit pattern is a valid word)."""
        if not _is_int(w):
            raise ValueError(f"w must be an int, got {type(w).__name__}")
        if not 0 <= w <= _U32_MAX:
            raise ValueError(f"w={w} out of range 0..0xFFFFFFFF")
        offset = (w >> 21) & 0x7FF
        if offset & 0x400:  # sign-extend the 11-bit two's-complement field
            offset -= 0x800
        return cls(
            ratio=w & 0xFF,
            v_target_raw=(w >> 8) & 0xFFF,
            static=bool((w >> 20) & 1),
            offset_raw=offset,
        )

    def to_u32(self) -> int:
        """Encode to the 32-bit word; inverse of :meth:`from_u32`."""
        return (
            self.ratio
            | (self.v_target_raw << 8)
            | (int(self.static) << 20)
            | ((self.offset_raw & 0x7FF) << 21)
        )

    @property
    def v_target_v(self) -> float:
        """Voltage target in volts (``v_target_raw / 1024``)."""
        return self.v_target_raw / 1024

    @property
    def offset_mv(self) -> float:
        """Voltage offset in millivolts (``offset_raw * 1000 / 1024``, as XTU displays it)."""
        return self.offset_raw * 1000 / 1024

    @staticmethod
    def v_to_raw(volts: float) -> int:
        """Volts -> ``v_target_raw``: ``round(volts * 1024)``. Non-finite input raises ``ValueError``."""
        if isinstance(volts, bool) or not isinstance(volts, (int, float)):
            raise ValueError(f"volts must be a number, got {type(volts).__name__}")
        if not math.isfinite(volts):
            raise ValueError(f"volts must be finite, got {volts!r}")
        return round(volts * 1024)

    @staticmethod
    def mv_to_offset_raw(mv: float) -> int:
        """Millivolts -> signed ``offset_raw`` with XTU/ThrottleStop rounding.

        ``(|mv|*1024 + 512) // 1000`` on the magnitude (round half up), sign preserved, computed exactly.
        Result must lie in -1024..1023, else ``ValueError`` (so -1000..+999 mV are accepted, +1000 and -1001 are not).
        """
        if isinstance(mv, bool) or not isinstance(mv, (int, float)):
            raise ValueError(f"mv must be a number, got {type(mv).__name__}")
        if not math.isfinite(mv):
            raise ValueError(f"mv must be finite, got {mv!r}")
        magnitude = int((abs(Fraction(mv)) * 1024 + 512) // 1000)
        raw = -magnitude if mv < 0 else magnitude
        if not _OFFSET_MIN <= raw <= _OFFSET_MAX:
            raise ValueError(f"mv={mv} -> offset_raw={raw} outside {_OFFSET_MIN}..{_OFFSET_MAX}")
        return raw

    def with_(self, **changes: Any) -> "VfWord":
        """Copy with some fields replaced (``dataclasses.replace``); the new word is validated like any other."""
        return dataclasses.replace(self, **changes)
