"""MSR access layer (Contract 4, docs/ARCHITECTURE.md): port contract, mailbox transport, simulator, /dev/cpu port.

Only ``devcpu`` / ``winport`` ever touch a real device, and only when a caller constructs ``DevCpuMsrPort`` / ``WinThrottleStopMsrPort``; importing this package
opens nothing. Every mailbox command is a WRMSR: the only caller of ``MailboxTransport.transact`` must be ``arltuner.guard``.
"""
from __future__ import annotations

from .devcpu import DevCpuMsrPort
from .measured_baseline import FUSED_POINT
from .port import MailboxTimeout, MsrError, MsrPort
from .sim import SimConfig, SimCpuState, SimMsrPort, SimRequest
from .transport import MailboxTransport
from .winport import WinThrottleStopMsrPort, verify_machine, windows_boot_id

__all__ = [
    "FUSED_POINT",
    "DevCpuMsrPort",
    "MailboxTimeout",
    "MailboxTransport",
    "MsrError",
    "MsrPort",
    "SimConfig",
    "SimCpuState",
    "SimMsrPort",
    "SimRequest",
    "WinThrottleStopMsrPort",
    "verify_machine",
    "windows_boot_id",
]
