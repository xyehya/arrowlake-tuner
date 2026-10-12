"""Pure OC-mailbox codec (no I/O, no hardware): request/response words, the V/F data word, command/domain/RC enums.

Facts: ``docs/knowledge/registers.md``. Contracts: ``docs/ARCHITECTURE.md`` Contract 1 + 2.
"""
from __future__ import annotations

from .commands import WRITE_COMMANDS, Cmd, Domain, Rc, is_write, rc_name
from .vf import VfWord
from .words import MSR_OC_MAILBOX, Response, is_busy, pack, unpack

__all__ = [
    "MSR_OC_MAILBOX",
    "WRITE_COMMANDS",
    "Cmd",
    "Domain",
    "Rc",
    "Response",
    "VfWord",
    "is_busy",
    "is_write",
    "pack",
    "rc_name",
    "unpack",
]
