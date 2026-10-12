"""Read-only health bracket (taint integer, PCI device set, module set) and a ``WriteGuard``-compatible health gate.

Spec: ``docs/tasks/T-015-health-gate.md``. Rules: ``docs/SAFETY.md`` "Mailbox-session discipline"; origin: the health
bracket of earlier research on the reference machine (``docs/SAFETY.md``). Reads only; the single write is ``save_snapshot`` creating a new file.
"""
from __future__ import annotations

from .gate import HealthDelta, diff, make_health_gate
from .whea import make_whea_health_gate
from .snapshot import HealthError, HealthSnapshot, decode_bits, load_snapshot, save_snapshot, take_snapshot

__all__ = [
    "HealthDelta",
    "HealthError",
    "HealthSnapshot",
    "decode_bits",
    "diff",
    "load_snapshot",
    "make_health_gate",
    "make_whea_health_gate",
    "save_snapshot",
    "take_snapshot",
]
