"""The only write path to the OC mailbox: policy, confirmation, lock, dwell, baseline, read-back, audit, revert.

Contract: ``docs/ARCHITECTURE.md`` Contract 5; spec and authority: ``docs/tasks/T-013-guard.md``; rules: ``docs/SAFETY.md``.
Every mailbox request -- reads included, because each is a WRMSR -- must go through ``WriteGuard.execute``.
"""
from __future__ import annotations

from .audit import AuditLog, AuditState, AuditUnreadable, read_all, read_state
from .guard import (
    ENV_FLAG,
    Confirmation,
    GuardRefused,
    MsrPortLike,
    MsrRequest,
    MsrResult,
    Refusal,
    Request,
    Result,
    Transport,
    WriteGuard,
    anchored_wall_clock,
    read_boot_id,
)
from .policy import (
    CHECKS,
    MSR_CHECKS,
    POINT_P2_MAX,
    AllowEntry,
    MsrAllowEntry,
    Policy,
    PolicyError,
    default_m1_policy,
    load_policy,
)

__all__ = [
    "CHECKS",
    "MSR_CHECKS",
    "POINT_P2_MAX",
    "ENV_FLAG",
    "AllowEntry",
    "AuditLog",
    "AuditState",
    "AuditUnreadable",
    "Confirmation",
    "GuardRefused",
    "MsrAllowEntry",
    "MsrPortLike",
    "MsrRequest",
    "MsrResult",
    "Policy",
    "PolicyError",
    "Refusal",
    "Request",
    "Result",
    "Transport",
    "WriteGuard",
    "anchored_wall_clock",
    "default_m1_policy",
    "load_policy",
    "read_all",
    "read_boot_id",
    "read_state",
]
