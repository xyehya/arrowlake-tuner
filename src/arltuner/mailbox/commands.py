"""Mailbox command ids, domain wire values and completion codes: pure enums, no I/O.

Source of every fact here: ``docs/research/registers.md``, sections "Mailbox commands", "Domain P1 wire values"
(inside "Mailbox commands") and "Return codes (``MailboxCompletionCode``)". Status tag: ``decomp`` (XTU code, ThrottleStop
9.7.3). Nothing here is on any write allowlist; an entry joins the allowlist only via DECISIONS.md plus a MEASURED read.
"""
from __future__ import annotations

from enum import IntEnum


class Cmd(IntEnum):
    """Mailbox command ids (MSR 0x150 bits [39:32]). Read and write ids; write ids are listed in ``WRITE_COMMANDS``.

    The first block is the registers.md "Mailbox commands" table. The second block is the commands that
    registers.md lists under "Unused on ARL-S" (0x07, 0x12/0x13, 0x1E/0x1F, 0x20/0x21); their names and the
    read/write polarity of each pair follow the odd-id-writes pattern and are ``[UNVERIFIED]``, kept so that every id in
    ``WRITE_COMMANDS`` is a named command.
    """

    # registers.md "Mailbox commands" table
    OC_CAPABILITY_READ = 0x01
    PER_CORE_CAPABILITY_READ = 0x02
    DDR_INFO_READ = 0x03
    ICC_MAX_VR_READ = 0x04
    REFERENCE_CLK_MONITOR_READ = 0x05
    VOLTAGE_FLOOR_READ = 0x09
    VOLTAGE_FREQ_READ = 0x10
    VOLTAGE_FREQ_WRITE = 0x11
    MISC_GLOBAL_CONFIG_READ = 0x14
    MISC_GLOBAL_CONFIG_WRITE = 0x15
    ICC_MAX_READ = 0x16
    ICC_MAX_WRITE = 0x17
    RESIDENCY_STATE_REGULATION_READ = 0x18
    RESIDENCY_STATE_REGULATION_WRITE = 0x19
    AVX_OFFSET_CONTROL_READ = 0x1A
    AVX_OFFSET_CONTROL_WRITE = 0x1B
    FAVORED_CORE_READ = 0x1C
    FAVORED_CORE_WRITE = 0x1D
    BCLK_READ = 0x22
    BCLK_WRITE = 0x23
    TVB_READ = 0x24
    TVB_WRITE = 0x25
    GRANULAR_RATIO_READ = 0x26
    GRANULAR_RATIO_WRITE = 0x27
    MAXIMUM_VOLTAGE_LIMIT_READ = 0x28
    # registers.md "Unused on ARL-S" (names/polarity [UNVERIFIED])
    FUSED_PER_CORE_RATIO_VOLTAGE_READ = 0x07
    SVID_READ = 0x12
    SVID_WRITE = 0x13
    REALTIME_MEMORY_READ = 0x1E
    REALTIME_MEMORY_WRITE = 0x1F
    AVX_SCALE_READ = 0x20
    AVX_SCALE_WRITE = 0x21


class Domain(IntEnum):
    """Mailbox P1 domain wire values (registers.md "Domain P1 wire values"; XTU's internal enum values differ)."""

    CORE = 0  # core / IA
    GT = 1  # GT slice
    RING = 2  # ring / cache (CLR)
    GT_UNSLICE = 3  # GT unslice
    SA = 4  # system agent (present but not exposed by XTU on ARL)
    MEMSS = 6  # memory subsystem (seen in OCCT/HWiNFO traces on this system, registers.md "Third confirmation")
    NGU = 7  # NGU / "SA Fabric"


class Rc(IntEnum):
    """Completion codes (MSR 0x150 bits [39:32]); names are XTU's ``MailboxCompletionCode``. 0x11 is not in the enum."""

    Pass = 0x00
    OCLocked = 0x01
    UnsupportedDomain = 0x02
    RatioExceedsLimits = 0x03  # XTU converts to success for ratio-only per-core writes
    VoltageExceedsMax = 0x04
    OCNotSupported = 0x05  # cmd 0x01 P1=0 => "hardware OC locked"
    FIVRError = 0x06
    IccMaxInvalid = 0x07
    IllegalCommandSelectedInParam1 = 0x08
    InvalidAVXLevel = 0x09
    RuntimeMemoryDisabled = 0x0A
    InvalidMemControllerId = 0x0B
    InvalidParam = 0x10
    VoltageExceedsLimit = 0x12
    UndervoltProtection = 0x13  # XTU ignores it for P-core ratio-limit writes
    UnrecognizedCommand = 0x1F
    UnsupportedCmd = 0xFE  # XTU-synthetic, never produced by hardware
    MailboxFailure = 0xFF  # provider-synthetic, never produced by hardware


WRITE_COMMANDS: frozenset[int] = frozenset(
    {
        Cmd.VOLTAGE_FREQ_WRITE,  # 0x11
        Cmd.SVID_WRITE,  # 0x13
        Cmd.MISC_GLOBAL_CONFIG_WRITE,  # 0x15
        Cmd.ICC_MAX_WRITE,  # 0x17
        Cmd.RESIDENCY_STATE_REGULATION_WRITE,  # 0x19
        Cmd.AVX_OFFSET_CONTROL_WRITE,  # 0x1B
        Cmd.FAVORED_CORE_WRITE,  # 0x1D
        Cmd.REALTIME_MEMORY_WRITE,  # 0x1F
        Cmd.AVX_SCALE_WRITE,  # 0x21
        Cmd.BCLK_WRITE,  # 0x23
        Cmd.TVB_WRITE,  # 0x25
        Cmd.GRANULAR_RATIO_WRITE,  # 0x27
    }
)


def is_write(cmd: int) -> bool:
    """True if ``cmd`` is a state-changing command (member of ``WRITE_COMMANDS``).

    state-changing command; **every** mailbox request is still a WRMSR -- a False result here does NOT mean
    the request is safe to send without the guard.

    ``cmd`` must be an int in 0..255 (``ValueError`` otherwise); ids not named in ``Cmd`` return False.
    """
    if isinstance(cmd, bool) or not isinstance(cmd, int):
        raise ValueError(f"cmd must be an int, got {type(cmd).__name__}")
    if not 0 <= cmd <= 0xFF:
        raise ValueError(f"cmd={cmd} out of range 0..0xff")
    return cmd in WRITE_COMMANDS


def rc_name(code: int) -> str:
    """Name of completion code ``code`` (e.g. 3 -> ``"RatioExceedsLimits"``); undefined codes give ``"UNKNOWN_0x11"``."""
    if isinstance(code, bool) or not isinstance(code, int):
        raise ValueError(f"code must be an int, got {type(code).__name__}")
    if not 0 <= code <= 0xFF:
        raise ValueError(f"code={code} out of range 0..0xff")
    try:
        return Rc(code).name
    except ValueError:
        return f"UNKNOWN_0x{code:02X}"
