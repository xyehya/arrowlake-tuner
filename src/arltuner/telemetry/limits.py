"""Decode of the frequency-limit-reason words 0x64F (core) and 0x6B0 (graphics). Pure functions on an int; no MSR access here.

Bit tables, sources and evidence: docs/knowledge/registers.md "Limit reasons". Only bits documented in the Intel SDM Vol 4 (read via the
EDK2 transcription) are named; every other set bit is reported by number in ``unknown_bits`` and never named (only DOC/MEASURED facts become
constants). Layout of both words: bits 15:0 are the live status ("frequency is currently reduced because ..."), bits 31:16 are the sticky
log of the same condition (log bit = status bit + 16). The SDM makes the log bits clear-on-write-0; this module never writes anything.
0x6B0 is the GRAPHICS word, not a package word (Linux msr-index.h MSR_GFX_PERF_LIMIT_REASONS, SDM 0x6B0 graphics).
"""
from __future__ import annotations

from dataclasses import dataclass

LOG_SHIFT = 16

# status bit -> (key, label). Keys are stable identifiers for the JSON/UI; labels are the SDM names.
CORE_64F: dict[int, tuple[str, str]] = {
    0: ("prochot", "PROCHOT"),
    1: ("thermal", "Thermal"),
    4: ("residency_state_regulation", "Residency state regulation"),
    5: ("ratl", "Running average thermal limit"),
    6: ("vr_therm_alert", "VR thermal alert"),
    7: ("vr_tdc", "VR thermal design current"),
    8: ("other", "Other (electrical or other)"),
    10: ("pl1", "Package power limit PL1"),
    11: ("pl2", "Package power limit PL2"),
    12: ("max_turbo_limit", "Max turbo limit (multi-core)"),
    13: ("turbo_transition_attenuation", "Turbo transition attenuation"),
}

GFX_6B0: dict[int, tuple[str, str]] = {
    0: ("prochot", "PROCHOT"),
    1: ("thermal", "Thermal"),
    5: ("ratl", "Running average thermal limit"),
    6: ("vr_therm_alert", "VR thermal alert"),
    7: ("vr_tdc", "VR thermal design current"),
    8: ("other", "Other"),
    10: ("pl1", "Package power limit PL1"),
    11: ("pl2", "Package power limit PL2"),
    12: ("inefficient_operation", "Inefficient operation"),
}


@dataclass(frozen=True)
class LimitReasons:
    """Decoded word. ``status``/``log`` hold keys in ascending bit order; ``unknown_bits`` are set bit numbers the table does not name."""

    raw: int
    status: tuple[str, ...]
    log: tuple[str, ...]
    unknown_bits: tuple[int, ...]

    def to_json(self) -> dict:
        return {"raw": self.raw, "status": list(self.status), "log": list(self.log), "unknown_bits": list(self.unknown_bits)}


def _decode(word: int, table: dict[int, tuple[str, str]]) -> LimitReasons:
    if not isinstance(word, int) or isinstance(word, bool) or not 0 <= word < 1 << 64:
        raise ValueError("limit-reason word must be an unsigned 64-bit integer")
    status = tuple(table[b][0] for b in sorted(table) if word >> b & 1)
    log = tuple(table[b][0] for b in sorted(table) if word >> (b + LOG_SHIFT) & 1)
    named = set(table) | {b + LOG_SHIFT for b in table}
    unknown = tuple(b for b in range(64) if word >> b & 1 and b not in named)
    return LimitReasons(word, status, log, unknown)


def decode_core_64f(word: int) -> LimitReasons:
    """MSR 0x64F (SDM MSR_CORE_PERF_LIMIT_REASONS, Skylake-and-later client table). Package scope."""
    return _decode(word, CORE_64F)


def decode_graphics_6b0(word: int) -> LimitReasons:
    """MSR 0x6B0 (SDM MSR_GRAPHICS_PERF_LIMIT_REASONS): limits on the integrated graphics frequency, not the cores."""
    return _decode(word, GFX_6B0)

