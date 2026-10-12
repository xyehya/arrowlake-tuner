"""MSR access layer (Contract 4, docs/ARCHITECTURE.md): port contract, mailbox transport, simulator, /dev/cpu port.

Only ``devcpu`` / ``winport`` ever touch a real device, and only when a caller constructs ``DevCpuMsrPort`` / ``WinThrottleStopMsrPort``; importing this package
opens nothing. Every mailbox command is a WRMSR: the only caller of ``MailboxTransport.transact`` must be ``arltuner.guard``.
"""
from __future__ import annotations

from .devcpu import DevCpuMsrPort
from .port import MailboxTimeout, MsrError, MsrPort
from .sim import SimConfig, SimCpuState, SimMsrPort, SimRequest
from .transport import MailboxTransport
from .winport import IdentityError, WinThrottleStopMsrPort, identity_cpus, resolve_identity, verify_machine, windows_boot_id

__all__ = [
    "DevCpuMsrPort",
    "IdentityError",
    "MailboxTimeout",
    "MailboxTransport",
    "MsrError",
    "MsrPort",
    "SimConfig",
    "SimCpuState",
    "SimMsrPort",
    "SimRequest",
    "WinThrottleStopMsrPort",
    "identity_cpus",
    "resolve_identity",
    "verify_machine",
    "windows_boot_id",
]
