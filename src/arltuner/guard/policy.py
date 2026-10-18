"""Write-guard policy: what the guard may ever send. Default deny; everything else is an explicit ``AllowEntry``.

Facts used: ``docs/knowledge/registers.md`` ("V/F data word", "Mailbox commands", "Return codes") and the operating
rules of ``docs/SAFETY.md`` ("Write-session protocol (T2)", "Mailbox-session discipline"). Status of the facts: ``decomp``
(XTU / ThrottleStop); nothing in the *default* policy has been MEASURED on this CPU, so the default policy is a
conservative proposal the human reviews and edits, not evidence.

A ``Policy`` is data plus a fixed registry of named predicates (``CHECKS``). Predicates are plain functions in this
file -- a policy file can only *name* one; there is no ``eval`` and no plug-in path. Unknown check names, unknown
TOML keys, contradictory entries and out-of-range values are all load errors (``PolicyError``): the guard must never
run under a policy it cannot fully interpret.

TOML schema (``load_policy``)::

    cpus = [0, 1, "8-23"]                 # ints or "lo-hi" strings
    max_operations_per_session = 1        # every executed mailbox request counts (reads too); read-back does not
    max_operations_per_boot = 12          # cap over ALL processes since boot, from the audit log (T-016; D-011)
    min_dwell_s = 60.0

    e_cpus = ["8-23"]                     # optional: CPUs that are E-cores (default: the live topology's; without one, the simulated reference part's 8-23); every other CPU is treated as P

    # D-049: there are NO operator value limits any more. The keys ``effective_offset_floor_mv`` ``assumed_bios_offset_mv`` ``offset_ceiling_mv`` ``max_ratio_step`` ``max_v_step_mv``
    # ``max_offset_step_mv`` ``ratio_max`` ``v_target_max_mv`` and the tables ``[power]`` / ``[runtime]`` of an older policy file still LOAD but are ignored (one warning line names
    # them; ``Policy.ignored_keys``). The 0x11 word is judged against the CPU's own limits only (cmd 0x01 [7:0] ratio, cmd 0x28 [11:0] - 1 LSB voltage, the 11-bit offset field).

    [[msr_allow]]                         # T-023: plain (non-mailbox) rdmsr/wrmsr; absent = every plain MSR access is denied
    name  = "turbo_e_write"               # unique
    msr   = 0x650                         # reads: 0x1AD 0x1AE 0x650 0x651 0xCE 0x194; writes: only 0x650 (E) and 0x1AD (P)
    kind  = "write"                       # "read" | "write"
    cpus  = [0]                           # optional subset of ``cpus`` (default: all of them)
    check = "turbo_ratio_bounded"         # writes MUST name it; reads MUST NOT

    [[allow]]
    name  = "voltage_freq_read"           # unique
    cmd   = 0x10                          # must be a named arltuner.mailbox.Cmd
    p1    = [0, 2, 7]
    p2    = [0]
    kind  = "read"                        # "write" iff cmd is in WRITE_COMMANDS
    # writes MUST name a check; reads MUST NOT:
    #   check  = "vf_bounded"
    #   params = { offset_raw_min = -102, offset_raw_max = 20 }   # optional extra tighteners of the offset window (raw, -1024..1023)

    # T-025: a per-V/F-point offset write is its OWN entry (0x11 with P1 0 and P2 in 1..15, never 0) with its own check; vf_bounded
    # entries must have P2 = [0] and a vf_point_offset_bounded entry must not include P2 0 (the OC point stays under vf_bounded):
    # [[allow]]
    # name = "vf_point_offset_write" ; cmd = 0x11 ; p1 = [0] ; p2 = [1, 2, 3, 4, 5, 6] ; kind = "write" ; check = "vf_point_offset_bounded"

Evidence (T-021, ``[MEASURED win 2026-10-01]``, ``docs/knowledge/registers.md`` "Measured via XTU's level-4 log on Windows" (S-03d, S-04...S-07)): XTU's per-core Apply is
``0x11`` (full word) -> ``0x1D`` (data = that word's ratio) -> ``0x26`` read -> ``0x27`` (data = the value read), and its ``0x11`` word
carries the core's non-zero baseline ratio (else ``0x1C[7:0]``) and the **fused** V target (``0x09 P1=0x11 P2=0`` ``[19:8]``): the
per-core ``0x10`` baseline is ``0x0`` on 23 of 24 cores, so it is not a usable reference by itself. The checks below therefore
also see the facts the guard has read *in this session* (``CheckContext``).

Scope (D-016, operator, 2026-10-01; supersedes D-009's undervolt-first rule): ratio, adaptive OC-point voltage and offset of P and E
cores may go **up or down**. D-049 (operator, 2026-10-18): the helper enforces only what the CPU / kernel itself enforces, read at runtime and failing closed when
unread -- the ratio limit ``0x01 [7:0]``, the voltage limit ``0x28 [11:0] - 1 LSB`` (field 0 = 2.0 V), the 11-bit offset field -- plus the PROCESS invariants (one mailbox
transaction at a time, the allowlists, baseline-first + read-back, the voltage-first / frequency-first ordering of ``registers.md`` "Ordering"). Everything else the operator
may set is a *warning* the Review shows (``controls.planner.plan_warnings``), never a refusal. Whether the hardware accepts a raise at all, the E-core write
scope and what ``0x1C`` returns after a ``0x1D`` are ``[UNVERIFIED]`` on this system.
"""
from __future__ import annotations

import logging
import math
import os
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from arltuner.mailbox import Cmd, VfWord, is_write
from arltuner.mailbox.encoding import (
    ENCODING_OFFSET_CEILING_MV,
    ENCODING_OFFSET_FLOOR_MV,
    ENCODING_RATIO_MAX,
    ENCODING_V_TARGET_MAX_MV,
)
from arltuner.topology.cpumap import KIND_E, KIND_P, Topology
from arltuner.topology.reference import arl_s_285k

__all__ = [
    "AUTO",
    "CPU_LIMIT_UNREAD",
    "DEEP_WHOLE_CURVE_MV",
    "LARGE_OFFSET_CHANGE_MV",
    "LARGE_POWER_CHANGE_W",
    "LARGE_RATIO_CHANGE",
    "LEGACY_KEYS",
    "CHECKS",
    "MSR_CHECKS",
    "POINT_P2_MAX",
    "MSR_TURBO_KIND",
    "ENCODING_PL_MAX_W",
    "ENCODING_TAU_MAX_S",
    "PLAIN_MSR_READABLE",
    "PLAIN_MSR_WRITABLE",
    "MsrAllowEntry",
    "MsrCheckContext",
    "MsrRequestLike",
    "build_msr_context",
    "turbo_bytes",
    "ENCODING_OFFSET_CEILING_MV",
    "ENCODING_OFFSET_FLOOR_MV",
    "ENCODING_RATIO_MAX",
    "ENCODING_V_TARGET_MAX_MV",
    "AllowEntry",
    "CheckContext",
    "Policy",
    "PolicyError",
    "RequestLike",
    "build_context",
    "default_m1_policy",
    "fused_top_ratio",
    "hw_ratio_max",
    "hw_v_max_mv",
    "hw_v_max_raw",
    "ratio_limit",
    "v_target_limit",
    "v_target_limit_mv",
    "load_policy",
]

#: D-049 (operator, 2026-10-18): **no operator value limit.** The only bounds are the CPU's own limits, read live (cmd ``0x01 [7:0]`` ratio, cmd ``0x28 [11:0] - 1 LSB`` voltage), and the field
#: encodings (``ENCODING_*``, re-exported from :mod:`arltuner.mailbox.encoding`, the one home of the field-encoding bounds; G-141): a value outside them cannot be written into the word at all.
#: An unread CPU limit is *unknown*: a write that needs it is refused with :data:`CPU_LIMIT_UNREAD` (fail closed), naming the read.
CPU_LIMIT_UNREAD = "CPU_LIMIT_UNREAD"

#: D-049 warning thresholds: constants of the Review (``controls.planner.plan_warnings``), NOT policy and never a refusal. A large single change is allowed and warned.
LARGE_OFFSET_CHANGE_MV = 50  # |offset delta| of one apply
LARGE_RATIO_CHANGE = 4  # |ratio delta| of one apply
LARGE_POWER_CHANGE_W = 50  # |PL1 / PL2 delta| of one apply
DEEP_WHOLE_CURVE_MV = -100  # a whole-curve offset deeper than this: the CPU's V/F monotonic clamp does not protect a whole-curve offset (cpu-rules-audit.md row 4a)


#: The simulated reference CPU, used ONLY where the caller asked for no topology on purpose (``default_m1_policy()`` / ``load_policy(text)`` of the simulator and the tests).
#: G-141 (D-036): a ``Policy`` built directly with no ``e_cpus`` is *unbound* (``e_cpus is None``) and ``core_kind`` refuses to guess a layout for it; every hardware path
#: binds the live topology (``Policy.with_topology`` / ``load_policy(..., topology)`` / ``default_m1_policy(topology)``).
_REFERENCE_TOPOLOGY = arl_s_285k()
_E_CPUS_REFERENCE = frozenset(_REFERENCE_TOPOLOGY.cpus_of_kind(KIND_E))

_U32_MAX = 0xFFFFFFFF
_CPU_MAX = 1023  # far above any real CPU number; rejects garbage, not a hardware fact


#: T-023 plain-MSR allowlist universe (``docs/knowledge/registers.md`` "Not the mailbox" + MSR table; facts ``[MEASURED lnx 2026-10-01]``):
#: only these may ever be read through the guard ...
#: 0x198 PERF_STATUS: running ratio [15:8], requested VID [47:32]/8192 (T-026 --measure). G-214: 0x606 RAPL_POWER_UNIT, 0x610 PKG_POWER_LIMIT (lock bit 63), 0x614 PKG_POWER_INFO,
#: read-only (docs/knowledge/registers.md "Power limits"); a write entry for any of them is a load error (``PLAIN_MSR_WRITABLE``).
PLAIN_MSR_READABLE = frozenset({0x1AD, 0x1AE, 0x650, 0x651, 0xCE, 0x194, 0x198, 0x606, 0x610, 0x614})
#: ... and only the two ratio tables may be written, each bounded by ``turbo_ratio_bounded``. The bucket-count MSRs ``0x1AE`` / ``0x651``
#: can never get a write entry (a policy that names one is a load error).
PLAIN_MSR_WRITABLE = frozenset({0x1AD, 0x650})
#: Core kind a ratio-table MSR belongs to: ``0x1AD`` = P-core table, ``0x650`` = E-core table. The kind of a *plain write* comes from the MSR,
#: never from the CPU it is sent on (XTU writes both on CPU 0, ``[DECOMPILED win-xtu-native-provider.md S6a]``).
MSR_TURBO_KIND: Mapping[int, str] = MappingProxyType({0x1AD: "P", 0x650: "E"})
_MSR_BUCKET_COUNT = frozenset({0x1AE, 0x651})
_TURBO_BUCKETS = 8


class PolicyError(ValueError):
    """The policy (object or TOML) is malformed, contradictory or uses something the guard does not implement."""


class RequestLike(Protocol):
    """Structural view of ``guard.Request`` (kept here so ``policy`` and ``audit`` do not import ``guard``)."""

    @property
    def cpu(self) -> int: ...
    @property
    def cmd(self) -> int: ...
    @property
    def p1(self) -> int: ...
    @property
    def p2(self) -> int: ...
    @property
    def data(self) -> int: ...
    @property
    def purpose(self) -> str: ...


class MsrRequestLike(Protocol):
    """Structural view of ``guard.MsrRequest`` (plain MSR access)."""

    @property
    def cpu(self) -> int: ...
    @property
    def msr(self) -> int: ...
    @property
    def value(self) -> int: ...
    @property
    def kind(self) -> str: ...
    @property
    def purpose(self) -> str: ...


def _is_int(value: object) -> bool:
    """Real ints only (``IntEnum`` members ok); ``bool`` and everything else are not."""
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------------------------------------------
# Predicates ("checks"). Signature: (request, baseline_word, params, context) -> (ok, detail). Any exception = refusal.
# ``baseline_word`` is the word read earlier in the session with the write's read twin (0x11->0x10, 0x15->0x14, 0x1D->0x1C,
# 0x27->0x26). ``context`` carries the other facts the guard has observed this session for the request's CPU; a check that needs a
# fact the context lacks refuses (fail closed). ``context`` defaults to ``None`` only so a caller that has none gets a refusal.
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckContext:
    """Facts about ``req.cpu`` that the guard (or the planner, mirroring it) has this session, plus the CPU's own limits for its core kind (D-049). ``None`` = not known.
    ``bound`` is the explicit "there is a context" flag (it replaces the old offset-floor sentinel): only :func:`build_context` sets it, and both ``0x11`` checks refuse a context
    that is not bound."""

    bound: bool = False
    favored_word: int | None = None  # 0x1C P1=0 P2=0 read this session: [7:0] favored ratio, [15:8] fused
    fused_word: int | None = None  # 0x09 P1=0x11 P2=0 read this session: the fused V/F point ([7:0] ratio, [19:8] voltage)
    vf_word_sent: int | None = None  # the last 0x11 word (P1=0 P2=0) executed with rc 0 for this CPU in this session
    max_voltage_word: int | None = None  # 0x28 P1=0 P2=0 read this session: [11:0] max voltage S2.10 (XTU uses raw - 1)
    hw_ratio_max: int | None = None  # the CPU's own ratio limit of this kind (cmd 0x01 [7:0]); None = unread (or ratio overclocking not supported: see ``ratio_text``)
    hw_v_max_raw: int | None = None  # the CPU's own voltage limit of this kind (cmd 0x28 [11:0] - 1 LSB, raw S2.10); None = unread
    top_fused_ratio: int | None = None  # G-120: the highest fused V/F point ratio of this CPU's kind; a P2 = 0 ratio at or below it has no OC point. None = not known
    ratio_text: str = field(default="", compare=False)  # where ``hw_ratio_max`` comes from, or why it is unknown (names the read), for refusal messages
    v_text: str = field(default="", compare=False)  # the same for ``hw_v_max_raw``


def fused_top_ratio(fused_word: int | None) -> int | None:
    """The highest fused V/F point ratio of a core, live: ``[7:0]`` of its ``0x09 P1=0x11 P2=0`` word (registers.md), or ``None`` when that word was not read
    (or reads 0). An OC ratio (the ``P2 = 0`` word) at or below it creates no OC point."""
    if not fused_word:
        return None
    return (fused_word & 0xFF) or None


def hw_ratio_max(oc_cap_words: Iterable[int | None]) -> int | None:
    """The CPU's own ratio limit of a core kind, live: ``[7:0]`` of the ``0x01 P1=0 P2=0`` words of that kind read in this session (registers.md 0x01: fused max
    OC ratio; ``[8]`` = ratio overclocking ok). The lowest if several differ (fail closed). ``None`` when no word was read, a word reads 0, or ``[8]`` is clear: the limit
    is unknown, so nothing may be raised. ``[UNVERIFIED]`` that an E CPU answers ``P1=0`` for the E domain (the decompile has no separate E domain, ``[DECOMPILED
    win-xtu-decompile-controls.md]``); ``[MEASURED win 2026-10-01]`` P reads ``0x778`` = 120x."""
    ratios = [w & 0xFF for w in oc_cap_words if w is not None and w & 0x100 and w & 0xFF]
    return min(ratios) if ratios else None


def hw_v_max_raw(max_voltage_word: int) -> int:
    """The highest V target (raw S2.10) the hardware allows from a ``0x28`` word: XTU uses ``[11:0] - 1`` and a field of 0 means 2.0 V = raw 2048 (registers.md "Mailbox commands" 0x28,
    ``[DECOMPILED]``; the values of this system, P ``0x681`` / E ``0x56D``, are ``[MEASURED win 2026-10-01]``). A word that WAS read as 0 is a 2.0 V limit, not "unread"."""
    raw = max_voltage_word & 0xFFF
    return 2048 if raw == 0 else raw - 1


def hw_v_max_mv(max_voltage_word: int | None) -> int | None:
    """The CPU's own V limit in whole mV, rounded down (``hw_v_max_raw``; field 0 -> 2000 mV, ``0x681`` -> 1625). ``None`` only when the word was not read."""
    return None if max_voltage_word is None else hw_v_max_raw(max_voltage_word) * 1000 // 1024


def _mv_to_raw_floor(mv: int) -> int:
    """A whole-mV V limit as raw S2.10, rounded down (never above the limit it stands for)."""
    return mv * 1024 // 1000


def ratio_limit(policy: Policy, kind: str, oc_cap_words: Iterable[int | None] = ()) -> tuple[int | None, str]:
    """``(the CPU's ratio limit of a core kind or None, source text)`` (D-049): the lowest of the ``0x01`` words of this session and the live limit bound to the policy (``Policy.with_hw_limits``).
    ``None`` = unknown -- unread (the text starts with ``CPU_LIMIT_UNREAD`` and names the read) or read but the CPU says ratio overclocking is not supported (``[8]`` clear / max 0)."""
    words = list(oc_cap_words)
    hw_values = [v for v in (hw_ratio_max(words), policy.hw_ratio_max[kind]) if v is not None]
    if hw_values:
        hw = min(hw_values)
        return hw, f"CPU limit {hw}x (cmd 0x01 [7:0] fused max OC ratio of a {kind} CPU, registers.md)"
    if any(w is not None for w in words):
        return None, f"the CPU reports ratio overclocking as not supported (cmd 0x01 of a {kind} CPU: bit 8 clear or max ratio 0)"
    return None, f"{CPU_LIMIT_UNREAD}: the CPU's own ratio limit (cmd 0x01 [7:0] of a {kind} CPU) was not read this session"


def v_target_limit(policy: Policy, kind: str, max_voltage_word: int | None) -> tuple[int | None, str]:
    """``(the CPU's V limit of a core kind as raw S2.10 or None, source text)`` (D-049): the lowest of the ``0x28`` word of this session (``hw_v_max_raw``, XTU's raw - 1 rule) and
    the live limit bound to the policy. ``None`` = unread (the text starts with ``CPU_LIMIT_UNREAD`` and names the read)."""
    candidates: list[int] = []
    if max_voltage_word is not None:
        candidates.append(hw_v_max_raw(max_voltage_word))
    bound = policy.hw_v_target_max_mv[kind]
    if bound is not None:
        candidates.append(_mv_to_raw_floor(bound))
    if candidates:
        raw = min(candidates)
        return raw, f"CPU limit {raw * 1000 // 1024} mV (cmd 0x28 [11:0] - 1 LSB of a {kind} CPU, XTU's rule, registers.md)"
    return None, f"{CPU_LIMIT_UNREAD}: the CPU's own voltage limit (cmd 0x28 [11:0] of a {kind} CPU) was not read this session"


def v_target_limit_mv(policy: Policy, kind: str, max_voltage_word: int | None) -> tuple[int | None, str]:
    """``v_target_limit`` in whole mV, rounded down (``None`` = unread)."""
    raw, text = v_target_limit(policy, kind, max_voltage_word)
    return (None if raw is None else raw * 1000 // 1024), text


def build_context(
    policy: Policy,
    cpu: int,
    *,
    favored_word: int | None,
    fused_word: int | None,
    vf_word_sent: int | None,
    max_voltage_word: int | None = None,
    oc_cap_words_of_kind: Iterable[int | None] = (),
) -> CheckContext:
    """The bound context for a request on ``cpu``: the session facts plus the CPU's own limits for its core kind (``oc_cap_words_of_kind``: the ``0x01`` words of the kind read in this
    session; the ``0x28`` word and the limits bound to the policy as well)."""
    kind = policy.core_kind(cpu)
    ratio_cap, ratio_text = ratio_limit(policy, kind, oc_cap_words_of_kind)
    v_max_raw, v_text = v_target_limit(policy, kind, max_voltage_word)
    return CheckContext(
        bound=True,
        top_fused_ratio=fused_top_ratio(fused_word),
        favored_word=favored_word,
        fused_word=fused_word,
        vf_word_sent=vf_word_sent,
        max_voltage_word=max_voltage_word,
        hw_ratio_max=ratio_cap,
        hw_v_max_raw=v_max_raw,
        ratio_text=ratio_text,
        v_text=v_text,
    )


Check = Callable[..., tuple[bool, str]]


@dataclass(frozen=True)
class MsrCheckContext:
    """What ``turbo_ratio_bounded`` needs besides the request and the baseline: the table's core kind (from the MSR, not the CPU) and the CPU's own ratio limit for that kind
    (``0x01 [7:0]``; ``None`` = unread, so no bucket may be raised)."""

    kind: str | None = None
    hw_ratio_max: int | None = None
    ratio_text: str = field(default="", compare=False)  # where ``hw_ratio_max`` comes from, or why it is unknown (names the read), for the refusal message


def build_msr_context(policy: Policy, msr: int, *, oc_cap_words: Mapping[int, int | None] | None = None) -> MsrCheckContext:
    """The context for a plain write of ``msr``: kind from the MSR (unknown MSR -> ``kind None``, which the check refuses) and the CPU's ratio limit of that kind from the ``0x01``
    words of this session (cpu -> word) and the limit bound to the policy."""
    kind = MSR_TURBO_KIND.get(msr)
    ratio_cap, ratio_text = None, ""
    if kind is not None:
        ratio_cap, ratio_text = ratio_limit(policy, kind, [w for c, w in (oc_cap_words or {}).items() if policy.core_kind(c) == kind])
    return MsrCheckContext(kind=kind, hw_ratio_max=ratio_cap, ratio_text=ratio_text)


def turbo_bytes(word: int) -> tuple[int, ...]:
    """The eight ratio buckets of a ``0x1AD`` / ``0x650`` word, bucket 0 (LSB = one active core) first."""
    return tuple((word >> (8 * k)) & 0xFF for k in range(_TURBO_BUCKETS))


def _check_turbo_ratio_bounded(
    req: MsrRequestLike, baseline: int, params: Mapping[str, Any], ctx: MsrCheckContext | None = None
) -> tuple[bool, str]:
    """Plain write of a turbo ratio table (``0x650`` E / ``0x1AD`` P; T-023, D-017), judged against the word read with ``rdmsr`` earlier in the
    session (``baseline``, required) by the CPU's own limit (D-049):

    1. the MSR is one of the two tables (the kind comes from the MSR) and ``value`` is a u64;
    2. every byte non-zero (a zero bucket is outside the field range ``TURBO_RATIO_RANGE``, never observed; XTU never sends one);
    3. every byte ``<=`` the CPU's ratio limit of that kind (cmd ``0x01 [7:0]``) when it is known; a byte above its baseline byte (a raise) **needs** that limit -- unread =
       ``CPU_LIMIT_UNREAD``, fail closed. A lowering never needs it.

    No step limit, no base-ratio floor, no monotonic rule: those are Review warnings (``controls.planner``: ``TURBO_NOT_MONOTONIC``, ``TURBO_RAISE``, below-base), not the CPU's.
    ``[UNVERIFIED]`` on this system: that the hardware accepts a raised table, and the scope of the write (package vs core, hence the
    guard's read-back on a second CPU). A caller without a context is refused.
    """
    if ctx is None or ctx.kind is None:
        return False, f"msr {getattr(req, 'msr', None)!r} is not a turbo ratio table (0x650 E / 0x1AD P) or no check context was given"
    value = req.value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        return False, f"value {value!r} is not a u64"
    new, old = turbo_bytes(value), turbo_bytes(baseline)
    for k, byte in enumerate(new):
        if byte == 0:
            return False, f"bucket {k} is 0 (a zero ratio bucket was never observed; XTU never sends one)"
    for k, (byte, was) in enumerate(zip(new, old)):
        if ctx.hw_ratio_max is not None:
            if byte > ctx.hw_ratio_max:
                return False, f"bucket {k}: ratio {byte} above the CPU limit {ctx.hw_ratio_max}x ({ctx.ratio_text})"
        elif byte > was:
            return False, f"bucket {k}: ratio {byte} raises the bucket from {was} but {ctx.ratio_text}"
    return True, "ok"


def _check_vf_bounded(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x11 (V/F write), D-049: judged against the word read with cmd 0x10 **and** the facts of this session, by the CPU's own limits only. Order of the tests (absolute bounds
    first, so a refusal names the root cause):

    1. a bound context (``CheckContext.bound``); mode adaptive in baseline and word (static / Power-Gate stay out of scope, D-016); ``ratio != 0`` and ``v_target_raw != 0`` (XTU never
       sends a zero field and the hardware's acceptance of one was never observed, ``[UNVERIFIED]``) -- except (G-120, D-035) a zero V target on a word with no OC point
       (``0 < ratio <= ctx.top_fused_ratio``: P 57, E 46), which is ``ratio | offset << 21`` as the hardware keeps it ``[MEASURED lnx 2026-10-11]``; that word skips the V reference /
       V ordering tests, every other bound applies;
    2. the ratio against the CPU's own limit (cmd ``0x01 [7:0]``, ``ctx.hw_ratio_max``): above it is refused; a **raise** (above the current ratio, ``max(baseline, 0x1C[7:0])``) with the
       limit unread is refused naming the read (``CPU_LIMIT_UNREAD``, fail closed);
    3. the predicted voltage against the CPU's own limit (cmd ``0x28 [11:0] - 1 LSB``, XTU's rule, field 0 = 2.0 V): the V target of the word (else the fused ``0x09`` voltage) plus a positive
       offset must not exceed it; a **raise** of that prediction over the baseline's with the limit unread is refused naming the read (``CPU_LIMIT_UNREAD``);
    4. the offset fits the 11-bit field (always true of a decoded word) and the entry's optional ``offset_raw_min`` / ``offset_raw_max`` tighteners (the allow entry, not a policy value);
    5. ordering (registers.md "Ordering", XTU: raise => voltage first, lower => frequency first): one word may not move the ratio and the V target the same way relative to the last
       word sent this session (else the baseline). Raise the V target first, then the ratio; lower the ratio first, then the V target.

    There is no offset window, no ceiling and no step limit any more: a big change is a Review warning (``controls.planner``), not a refusal. ``[MEASURED win 2026-10-01]``: XTU's word has
    ratio = the non-zero baseline ratio else ``0x1C[7:0]`` and V target = the fused voltage. ``[UNVERIFIED]`` on this system: that the hardware accepts a raise at all; that ``0x1C`` follows a ``0x1D``.
    """
    if ctx is None or not ctx.bound:
        return False, "no check context (the session facts and the CPU's limits are unknown); the bounds cannot be evaluated"
    if int(req.p2) != 0:  # T-025: a V/F point (P2 1..15) is judged by vf_point_offset_bounded only; this check's ratio/V logic is the OC point's
        return False, f"P2 {req.p2}: vf_bounded judges the OC point (P2 0) only; a V/F point offset needs a vf_point_offset_bounded entry"
    new = VfWord.from_u32(req.data)
    base = VfWord.from_u32(baseline)
    if base.static:
        return False, "baseline word is in static (override) mode; the bounds cannot be evaluated"
    if new.static:
        return False, "static (override) mode is not allowed (out of scope, D-016)"
    if new.ratio == 0:
        return False, "ratio 0 (XTU never sends a zero ratio; acceptance by the hardware is unverified)"
    # G-120 (D-035): with no OC point (ratio <= the top fused ratio) the hardware keeps no V target next to an offset ([MEASURED lnx 2026-10-11]: wrote
    # 0xFEC5B439, read 0xFEC00039), so the word is ratio | offset << 21 with target 0. Above the top fused ratio an OC point needs a real target.
    no_oc_point = new.v_target_raw == 0 and ctx.top_fused_ratio is not None and 0 < new.ratio <= ctx.top_fused_ratio
    if new.v_target_raw == 0 and not no_oc_point:
        return False, "v_target_raw 0 (XTU never sends a zero V target; only a word with no OC point, ratio <= the top fused ratio, may carry none)"
    favored_ratio = None if ctx.favored_word is None else ctx.favored_word & 0xFF
    fused_v = None if ctx.fused_word is None else (ctx.fused_word >> 8) & 0xFFF
    ratio_now = base.ratio or favored_ratio or 0
    v_now = base.v_target_raw or fused_v or 0
    if ratio_now == 0:
        return False, "no ratio reference: the baseline ratio is 0 and 0x1C[7:0] is 0 or was not read this session"
    if v_now == 0 and not no_oc_point:
        return False, "no voltage reference: the baseline V target is 0 and the fused 0x09 V target is 0 or was not read this session"
    ratio_noraise = max(base.ratio, favored_ratio or 0)
    if ctx.hw_ratio_max is not None:
        if new.ratio > ctx.hw_ratio_max:
            return False, f"ratio {new.ratio} above the CPU limit {ctx.hw_ratio_max}x ({ctx.ratio_text}; baseline {base.ratio}, 0x1C[7:0] {favored_ratio})"
    elif new.ratio > ratio_noraise:
        return False, f"ratio {new.ratio} raises the ratio (now {ratio_noraise}) but {ctx.ratio_text}"
    # predicted voltage: the V target of the word (else the fused top-point voltage) plus a positive offset; the baseline's own prediction is the "no raise" reference
    v_ref = new.v_target_raw or (fused_v or 0)
    if v_ref == 0 and new.offset_raw > 0:
        return False, "no voltage reference: a positive offset on a word with no OC point needs the fused 0x09 voltage, which was not read this session"
    predicted = v_ref + max(new.offset_raw, 0)
    predicted_now = max(base.v_target_raw, fused_v or 0) + max(base.offset_raw, 0)
    what = f"V target {new.v_target_raw:#x}" + (f" + offset {new.offset_raw}" if new.offset_raw > 0 else "") if new.v_target_raw else f"fused voltage {v_ref:#x} + offset {new.offset_raw}"
    if ctx.hw_v_max_raw is not None:
        if predicted > ctx.hw_v_max_raw:
            return False, f"predicted voltage {predicted:#x} raw ({what}) above the CPU limit {ctx.hw_v_max_raw:#x} ({ctx.v_text}; baseline {base.v_target_raw:#x}, fused {fused_v})"
    elif predicted > predicted_now:
        return False, f"predicted voltage {predicted:#x} raw ({what}) raises the voltage (now {predicted_now:#x}) but {ctx.v_text}"
    low, high = params.get("offset_raw_min", -1024), params.get("offset_raw_max", 1023)
    if not low <= new.offset_raw <= high:
        return False, f"offset_raw {new.offset_raw} outside {low}..{high} (the 11-bit offset field is -1024..1023; entry params {dict(params)})"
    if base.ratio == 0 and ctx.top_fused_ratio is not None and 0 < new.ratio <= ctx.top_fused_ratio:
        # G-122: a cold word (ratio field 0) written at or below the top fused ratio has no OC point: its ratio field is not a ratio change. The reference is then
        # capped at the top fused ratio (E: 0x1C[7:0] reads 48, the E turbo, but the word without an OC point is 46), so 0 -> 46 / 57 is no change of ratio.
        ratio_now = min(ratio_now, ctx.top_fused_ratio)
    ratio_before, v_before = ratio_now, v_now
    if ctx.vf_word_sent is not None:
        sent = VfWord.from_u32(ctx.vf_word_sent)
        ratio_before, v_before = sent.ratio, sent.v_target_raw
    d_ratio, d_v = new.ratio - ratio_before, 0 if no_oc_point else new.v_target_raw - v_before
    if d_ratio > 0 and d_v > 0:
        return False, "the word raises the ratio and the V target together: raise the V target first (one 0x11), the ratio in the next"
    if d_ratio < 0 and d_v < 0:
        return False, "the word lowers the ratio and the V target together: lower the ratio first (one 0x11), the V target in the next"
    return True, "ok"


#: T-025: ``0x11`` / ``0x10`` with ``P2`` = a V/F point (registers.md "Mailbox commands": P2 1..15; the OC point is the ``P2 = 0`` word, not a
#: point index: ``0x10 P2=7`` -> rc ``0x11`` on E CPU 8, ``[MEASURED lnx 2026-10-02]``, docs/knowledge/platform-arl-s.md section 3).
POINT_P2_MAX = 15
#: T-028: domains whose V/F points a point-offset write may address: P1 0 = core, P1 2 = ring (package-level, addressed through any CPU;
#: ``0x10 P1=2 P2=1..8`` = ring points 8,14,20,28,34,37,38,39x, ``[MEASURED lnx 2026-10-02]``, docs/knowledge/platform-arl-s.md section 3). P1 1 / 7 stay refused.
POINT_P1_ALLOWED = frozenset({0, 2})


def _check_vf_point_offset_bounded(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x11 with ``P1`` 0 (core) or 2 (ring, T-028) and ``P2`` = a V/F point 1..15 (T-025): a **per-point offset** write, distinct from the whole-curve offset
    (``P2 = 0``, ``vf_bounded``). ``baseline`` is the ``0x10`` word of the **same CPU and the same P2** read this session (the guard keys its
    baselines by cpu/p1/p2, so another point's or the OC point's word can never stand in for it). Tests, fail closed (D-049):

    1. a bound context (``CheckContext.bound``); the request addresses P1 0 or 2 (the allow entry must list it) and P2 1..15 (P2 0 is the OC point and belongs to ``vf_bounded``'s
       ratio / V limits: refused here);
    2. the word equals the baseline in ratio ``[7:0]``, V target ``[19:8]`` and the mode bit ``[20]`` **exactly** -- only the offset field
       ``[31:21]`` may change (``[MEASURED lnx 2026-10-02]`` E CPU 8: point 6 reads ``0x0000002E`` = 46x, V target 0; OCCT wrote point words as
       ``ratio | offset << 21`` with V target 0, ``docs/knowledge/registers.md``). A baseline with ratio 0 or the static bit set is refused: it
       is not a known fused point, so there is nothing to keep;
    3. the offset fits the 11-bit field (always true of a decoded word) and the entry's optional ``offset_raw_min`` / ``offset_raw_max`` tighteners.

    No offset window and no step limit: the voltages of the fused points are not readable, so a point offset cannot be predicted against the cmd ``0x28`` limit -- the CPU's own
    limit and V/F clamp apply, and a big or positive offset is a Review warning (``controls.planner``). The hardware's V(n) >= V(n-1) rule across points clamps a deeper offset up silently
    ``[MEASURED lnx 2026-10-02]``; it is not checked here (the read-back compares words, not voltages). A caller without a bound context is refused."""
    if ctx is None or not ctx.bound:
        return False, "no check context (the session facts are unknown); the bounds cannot be evaluated"
    if int(req.p1) not in POINT_P1_ALLOWED or not 1 <= int(req.p2) <= POINT_P2_MAX:
        return False, f"P1 {req.p1} / P2 {req.p2}: a point offset write needs P1 0 or 2 and P2 1..{POINT_P2_MAX} (P2 0 is the OC point: vf_bounded)"
    new = VfWord.from_u32(req.data)
    base = VfWord.from_u32(baseline)
    if base.ratio == 0:
        return False, "the point's baseline word has ratio 0: not a fused point, nothing to keep"
    if base.static:
        return False, "the point's baseline word is in static (override) mode; the bounds cannot be evaluated"
    if new.ratio != base.ratio:
        return False, f"ratio {new.ratio} differs from the point's baseline ratio {base.ratio}: only the offset may change"
    if new.v_target_raw != base.v_target_raw:
        return False, f"V target raw {new.v_target_raw:#x} differs from the point's baseline {base.v_target_raw:#x}: only the offset may change"
    if new.static != base.static:
        return False, "the mode bit differs from the point's baseline: only the offset may change"
    low, high = params.get("offset_raw_min", -1024), params.get("offset_raw_max", 1023)
    if not low <= new.offset_raw <= high:
        return False, f"offset_raw {new.offset_raw} outside {low}..{high} (the 11-bit offset field is -1024..1023; entry params {dict(params)})"
    return True, "ok"

_PERCORE_ENABLE_BIT = 0x8  # registers.md 0x14/0x15 data: bit 3 = PerCore Voltage Enable (ARL client)


def _check_percore_enable_only(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x15 (MiscGlobalConfig write): the new word must be exactly ``baseline | 0x8`` -- bit 3 set, nothing else moves.

    registers.md "XTU's write procedure" (1): ``write cmd 0x15 with old|8`` when PerCore Voltage Enable is off.
    """
    expected = baseline | _PERCORE_ENABLE_BIT
    if req.data != expected:
        return False, f"data {req.data:#010x} is not baseline|0x8 ({expected:#010x}); only bit 3 may change"
    return True, "ok"


def _check_favored_ratio_follows_vf(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x1D (favored-core write): ``data`` must be the ratio of the 0x11 word executed for the same CPU earlier in this session
    (XTU: ``0x1D`` data = the ratio of the word it just wrote, ``[MEASURED win 2026-10-01]`` S-04/S-06) and non-zero (a zero ratio was
    never accepted/observed). The ceiling on that ratio is the ``0x11`` check's business; this check follows the word actually executed,
    not the baseline (D-016: it keeps working when the ratio may be raised). ``baseline`` (the 0x1C word) only has to exist: it is the
    baseline-first rule and the read-back reference. ``0x1C[15:8]`` is not used: on E cores it is 46 while XTU wrote the ``[7:0]`` value
    48 (``0x2E30``)."""
    if ctx is None or ctx.vf_word_sent is None:
        return False, "no 0x11 word was executed for this CPU earlier in this session"
    ratio = VfWord.from_u32(ctx.vf_word_sent).ratio
    if req.data != ratio:
        return False, f"data {req.data:#x} is not the ratio {ratio} of the 0x11 word executed for this CPU"
    if req.data == 0:
        return False, "ratio 0 (XTU never sends a zero ratio; acceptance by the hardware is unverified)"
    return True, "ok"


def _check_granular_equals_read(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x27 (granular-ratio write): ``data`` must equal, exactly, the 0x26 word read for the same CPU in this session
    (``baseline``): XTU writes back the value it just read (``[MEASURED win 2026-10-01]`` S-04: ``GET 0x26`` = 3 -> ``SET 0x27`` = 3)."""
    if req.data != baseline:
        return False, f"data {req.data:#x} is not the 0x26 word {baseline:#x} read for this CPU in this session"
    return True, "ok"


#: The fixed registry. A policy file can only name one of these.
CHECKS: Mapping[str, Check] = MappingProxyType(
    {
        "vf_bounded": _check_vf_bounded,
        "vf_point_offset_bounded": _check_vf_point_offset_bounded,
        "percore_enable_only": _check_percore_enable_only,
        "favored_ratio_follows_vf": _check_favored_ratio_follows_vf,
        "granular_equals_read": _check_granular_equals_read,
    }
)
#: The registry of plain-MSR write predicates (``MsrAllowEntry``), kept apart from ``CHECKS`` (their request / context types differ): a mailbox
#: ``AllowEntry`` cannot name one of these, a ``MsrAllowEntry`` can name only these. Signature: (``MsrRequest``, baseline u64, params, ``MsrCheckContext``).
MSR_CHECKS: Mapping[str, Check] = MappingProxyType({"turbo_ratio_bounded": _check_turbo_ratio_bounded})


def _offset_raw_validator(name: str) -> Callable[[object], None]:
    """``params.offset_raw_min`` / ``offset_raw_max``: any value of the 11-bit signed offset field (-1024..1023). D-016: the offset window
    is a pair of bounds of the allow entry, so neither side is restricted to the undervolt half."""

    def validate(value: object) -> None:
        if not _is_int(value) or not -1024 <= value <= 1023:  # type: ignore[operator]
            raise PolicyError(f"params.{name} must be an int in -1024..1023, got {value!r}")

    return validate


#: Allowed ``params`` keys per check, each with its validator.
_CHECK_PARAMS: Mapping[str, Mapping[str, Callable[[object], None]]] = MappingProxyType(
    {
        "vf_bounded": MappingProxyType(
            {"offset_raw_min": _offset_raw_validator("offset_raw_min"), "offset_raw_max": _offset_raw_validator("offset_raw_max")}
        ),
        "vf_point_offset_bounded": MappingProxyType(
            {"offset_raw_min": _offset_raw_validator("offset_raw_min"), "offset_raw_max": _offset_raw_validator("offset_raw_max")}
        ),
        "percore_enable_only": MappingProxyType({}),
        "favored_ratio_follows_vf": MappingProxyType({}),
        "granular_equals_read": MappingProxyType({}),
    }
)


# --------------------------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------------------------
def _int_frozenset(what: str, values: object, lo: int, hi: int) -> frozenset[int]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Iterable):
        raise PolicyError(f"{what} must be a collection of ints, got {type(values).__name__}")
    out: set[int] = set()
    for v in values:
        if not _is_int(v):
            raise PolicyError(f"{what}: {v!r} is not an int")
        if not lo <= v <= hi:
            raise PolicyError(f"{what}: {v} out of range {lo}..{hi}")
        out.add(int(v))
    if not out:
        raise PolicyError(f"{what} must not be empty")
    return frozenset(out)


def _int_in(what: str, value: object, lo: int, hi: int, why: str = "") -> None:
    if not _is_int(value) or not lo <= value <= hi:  # type: ignore[operator]
        raise PolicyError(f"{what} must be an int in {lo}..{hi}, got {value!r}" + (f" (field encoding: {why})" if why else ""))


def _kind_ceilings(what: str, table: object, bounds: Mapping[str, tuple[int, int]], why: str = "") -> Mapping[str, int | None]:
    """Normalise a per-core-kind ceiling table: keys within ``{"P", "E"}`` (a missing kind is ``None`` = the no-raise value), values ``None``
    or an int inside the kind's ``(lo, hi)`` sanity bounds."""
    if not isinstance(table, Mapping) or not set(table) <= set(bounds):
        raise PolicyError(f"{what} must be a table with the keys {sorted(bounds)}, got {table!r}")
    out: dict[str, int | None] = {}
    for kind, (lo, hi) in sorted(bounds.items()):
        value = table.get(kind)
        if value is not None:
            _int_in(f"{what}[{kind!r}]", value, lo, hi, why)
            value = int(value)
        out[kind] = value
    return MappingProxyType(out)


@dataclass(frozen=True)
class AllowEntry:
    """One allowlisted request shape: ``cmd`` with ``p1`` in a set and ``p2`` in a set, plus (for writes) a named check.

    ``kind`` must agree with the codec: ``"write"`` exactly when ``cmd`` is in ``WRITE_COMMANDS``. A write entry must
    name a check (a write with no predicate is a load error); a read entry must not. ``params`` are only those the
    named check declares (``vf_bounded``: ``offset_raw_min`` / ``offset_raw_max``, optional extra tighteners of the offset window).
    """

    name: str
    cmd: int
    p1: frozenset[int]
    p2: frozenset[int]
    kind: Literal["read", "write"]
    check: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise PolicyError("allow entry needs a non-empty name")
        name = self.name
        if not _is_int(self.cmd) or not 0 <= self.cmd <= 0xFF:
            raise PolicyError(f"{name}: cmd must be an int in 0..255, got {self.cmd!r}")
        try:
            Cmd(self.cmd)
        except ValueError:
            raise PolicyError(f"{name}: cmd {self.cmd:#04x} is not a named mailbox command (arltuner.mailbox.Cmd)") from None
        object.__setattr__(self, "cmd", int(self.cmd))
        object.__setattr__(self, "p1", _int_frozenset(f"{name}: p1", self.p1, 0, 0xFF))
        object.__setattr__(self, "p2", _int_frozenset(f"{name}: p2", self.p2, 0, 0xFF))
        if self.kind not in ("read", "write"):
            raise PolicyError(f"{name}: kind must be 'read' or 'write', got {self.kind!r}")
        if (self.kind == "write") != is_write(self.cmd):
            raise PolicyError(f"{name}: kind {self.kind!r} contradicts the codec for cmd {self.cmd:#04x}")
        if not isinstance(self.params, Mapping):
            raise PolicyError(f"{name}: params must be a table")
        if self.kind == "write":
            if self.check is None:
                raise PolicyError(f"{name}: a write entry must name a check")
        elif self.check is not None or self.params:
            raise PolicyError(f"{name}: a read entry cannot have a check or params")
        if self.check is not None:
            if not isinstance(self.check, str) or self.check not in CHECKS:
                raise PolicyError(f"{name}: unknown check {self.check!r} (known: {sorted(CHECKS)})")
            allowed = _CHECK_PARAMS[self.check]
            for key, value in self.params.items():
                if key not in allowed:
                    raise PolicyError(f"{name}: check {self.check!r} has no parameter {key!r}")
                allowed[key](value)
            # T-025: the two 0x11 checks judge different words, so each entry may only cover the addresses its check understands.
            if self.check == "vf_point_offset_bounded" and (
                self.cmd != int(Cmd.VOLTAGE_FREQ_WRITE) or not self.p1 <= POINT_P1_ALLOWED or not self.p2 <= frozenset(range(1, POINT_P2_MAX + 1))
            ):
                raise PolicyError(f"{name}: vf_point_offset_bounded needs cmd 0x11, p1 within [0, 2] and p2 within 1..{POINT_P2_MAX} (P2 0 is the OC point)")
            if self.check == "vf_bounded" and self.p2 != frozenset({0}):
                raise PolicyError(f"{name}: vf_bounded judges the OC point only: p2 must be [0] (a V/F point needs its own vf_point_offset_bounded entry)")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def matches(self, cmd: int, p1: int, p2: int) -> bool:
        return self.cmd == cmd and p1 in self.p1 and p2 in self.p2


@dataclass(frozen=True)
class MsrAllowEntry:
    """One allowlisted **plain MSR** access (T-023): ``msr`` with ``kind`` ``"read"`` or ``"write"`` on ``cpus`` (``None`` = every CPU of the
    policy). Reads: any of ``PLAIN_MSR_READABLE`` and no check. Writes: only ``PLAIN_MSR_WRITABLE`` (``0x650`` / ``0x1AD``), and they MUST name
    ``turbo_ratio_bounded``. The bucket-count MSRs ``0x1AE`` / ``0x651`` can never be written (load error); every other MSR is unknown."""

    name: str
    msr: int
    kind: Literal["read", "write"]
    cpus: frozenset[int] | None = None
    check: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise PolicyError("msr_allow entry needs a non-empty name")
        name = self.name
        if not _is_int(self.msr) or not 0 <= self.msr <= 0xFFFFFFFF:
            raise PolicyError(f"{name}: msr must be an int in 0..0xFFFFFFFF, got {self.msr!r}")
        object.__setattr__(self, "msr", int(self.msr))
        if self.kind not in ("read", "write"):
            raise PolicyError(f"{name}: kind must be 'read' or 'write', got {self.kind!r}")
        if self.kind == "write":
            if self.msr in _MSR_BUCKET_COUNT:
                raise PolicyError(f"{name}: MSR {self.msr:#x} holds the bucket core counts and can never have a write entry")
            if self.msr not in PLAIN_MSR_WRITABLE:
                raise PolicyError(f"{name}: MSR {self.msr:#x} cannot be written through the guard (writable: {sorted(map(hex, PLAIN_MSR_WRITABLE))})")
        elif self.msr not in PLAIN_MSR_READABLE:
            raise PolicyError(f"{name}: MSR {self.msr:#x} is not a known plain MSR (readable: {sorted(map(hex, PLAIN_MSR_READABLE))})")
        if self.cpus is not None:
            object.__setattr__(self, "cpus", _int_frozenset(f"{name}: cpus", self.cpus, 0, _CPU_MAX))
        if not isinstance(self.params, Mapping):
            raise PolicyError(f"{name}: params must be a table")
        if self.kind == "write":
            if self.check is None:
                raise PolicyError(f"{name}: a write entry must name a check")
            if not isinstance(self.check, str) or self.check not in MSR_CHECKS:
                raise PolicyError(f"{name}: unknown MSR check {self.check!r} (known: {sorted(MSR_CHECKS)})")
            for key in self.params:
                raise PolicyError(f"{name}: check {self.check!r} has no parameter {key!r}")
        elif self.check is not None or self.params:
            raise PolicyError(f"{name}: a read entry cannot have a check or params")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def matches(self, msr: int, kind: str) -> bool:
        return self.msr == msr and self.kind == kind

    def covers_cpu(self, cpu: int) -> bool:
        return self.cpus is None or cpu in self.cpus


#: G-214 / D-049: the field encodings of the package power limits (the only PL bound is the ``0x610`` field width; ``constraint_0_max_power_uw`` is not a PL1 ceiling).
#: ``0x610`` PL fields are 15 bits in 1/8 W units (``[DOC SDM]``); the PL1 time window is ``2^Y * (1 + Z/4)`` time units with Y 5 bits (``[DOC SDM]``, time unit 2^-10 s ``[MEASURED lnx 2026-09-29]``).
ENCODING_PL_MAX_W = 4095
ENCODING_TAU_MAX_S = (1 << 31) * 1.75 / 1024


@dataclass(frozen=True)
class Policy:
    """The whole policy: PROCESS settings only (D-049). ``max_operations_per_session`` counts every executed mailbox request (reads included, because
    every request is a WRMSR -- SAFETY.md "OC mailbox rule"); the post-write read-back does not count. The default of 1
    means *one* transaction until the human raises it (a read-then-write session needs at least 2).

    ``e_cpus`` (T-021): which CPUs are E-cores (any other CPU counts as P).

    ``max_operations_per_boot`` (T-016) is the same count taken over **every process** since the current boot, read from the
    audit log (``attempt`` records with the current ``boot_id``): the CLI is one process per command, so a per-session limit
    alone cannot stop back-to-back invocations -- the sequence behind the 2026-08-26 incident (SAFETY.md
    "Mailbox-session discipline", D-011). ``min_dwell_s`` likewise also applies across processes (from the audit log).

    D-049: there is no operator value limit. ``hw_ratio_max`` / ``hw_v_target_max_mv`` carry the CPU's own limits per core kind (``0x01 [7:0]``, ``0x28 [11:0] - 1 LSB`` in whole mV) when the
    helper bound them from this boot's ``tune.read`` (``with_hw_limits``); ``None`` = not read, and a write that needs the limit is refused (``CPU_LIMIT_UNREAD``). ``ignored_keys``
    names the keys of an older policy file that were ignored (``load_policy``); ``source`` names the policy file (for messages). The attributes of the removed operator limits
    (``ratio_max``, ``offset_ceiling_mv``, ``max_*_step*``, ``power``, ``runtime`` ...) remain as read-only properties with neutral values so a reader outside this package keeps working."""

    cpus: frozenset[int]
    max_operations_per_session: int = 1
    min_dwell_s: float = 60.0
    allow: tuple[AllowEntry, ...] = ()
    max_operations_per_boot: int = 12
    e_cpus: frozenset[int] | None = None  # None = not bound to a topology: core_kind() raises instead of assuming a particular layout
    msr_allow: tuple[MsrAllowEntry, ...] = ()  # T-023: plain MSR allowlist; empty (the default) = every plain MSR access is denied
    hw_ratio_max: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})  # the CPU's own ratio limit per kind (0x01 [7:0]) read by the helper; None = unread
    hw_v_target_max_mv: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})  # the CPU's own V limit per kind (0x28 - 1 LSB), whole mV; None = unread
    source: str | None = field(default=None, compare=False)  # the policy file this policy was loaded from (named in every refusal that quotes it)
    ignored_keys: tuple[str, ...] = field(default=(), compare=False)  # D-049: keys of an older policy file that load but no longer apply

    @property
    def where(self) -> str:
        """How refusals name the policy file."""
        return self.source or "the policy"

    # --- D-049: neutral read-only stand-ins for the removed operator limits (readers outside guard/controls-limits code: report, residual, executor, ops_power, ops_runtime) ---
    @property
    def ratio_max(self) -> Mapping[str, int | None]:
        return MappingProxyType({"P": None, "E": None})

    @property
    def v_target_max_mv(self) -> Mapping[str, int | None]:
        return MappingProxyType({"P": None, "E": None})

    @property
    def assumed_bios_offset_mv(self) -> Mapping[str, int]:
        return MappingProxyType({"P": 0, "E": 0})

    @property
    def effective_offset_floor_mv(self) -> int:
        return ENCODING_OFFSET_FLOOR_MV

    @property
    def offset_ceiling_mv(self) -> int:
        return ENCODING_OFFSET_CEILING_MV

    @property
    def max_ratio_step(self) -> int:
        return ENCODING_RATIO_MAX

    @property
    def max_v_step_mv(self) -> int:
        return 1000

    @property
    def max_offset_step_mv(self) -> int:
        return 2000

    @property
    def power(self) -> None:
        return None

    @property
    def runtime(self) -> None:
        return None

    def with_hw_limits(self, ratio_max_oc: Mapping[str, int | None] | None = None, v_target_max_mv: Mapping[str, int | None] | None = None) -> Policy:
        """The same policy bound to the CPU's own limits read live: ``0x01 [7:0]`` per kind and ``0x28 - 1 LSB`` in mV per kind. Kinds not given keep their value; keys that
        are not a core kind (``"ring"``) are not a policy matter and are dropped."""
        return replace(
            self,
            hw_ratio_max={**self.hw_ratio_max, **{k: v for k, v in (ratio_max_oc or {}).items() if k in (KIND_P, KIND_E)}},
            hw_v_target_max_mv={**self.hw_v_target_max_mv, **{k: v for k, v in (v_target_max_mv or {}).items() if k in (KIND_P, KIND_E)}},
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "cpus", _int_frozenset("cpus", self.cpus, 0, _CPU_MAX))
        object.__setattr__(self, "hw_ratio_max", _kind_ceilings("hw_ratio_max", self.hw_ratio_max, {k: (1, ENCODING_RATIO_MAX) for k in (KIND_P, KIND_E)}, "ratio field [7:0]"))
        object.__setattr__(
            self,
            "hw_v_target_max_mv",
            _kind_ceilings("hw_v_target_max_mv", self.hw_v_target_max_mv, {k: (0, ENCODING_V_TARGET_MAX_MV) for k in (KIND_P, KIND_E)}, "0x28 field [11:0]"),
        )
        if self.e_cpus is not None:
            if isinstance(self.e_cpus, (str, bytes, Mapping)) or not isinstance(self.e_cpus, Iterable):
                raise PolicyError(f"e_cpus must be a collection of ints, got {type(self.e_cpus).__name__}")
            e_cpus = list(self.e_cpus)
            for v in e_cpus:
                if not _is_int(v) or not 0 <= v <= _CPU_MAX:
                    raise PolicyError(f"e_cpus: {v!r} is not an int in 0..{_CPU_MAX}")
            object.__setattr__(self, "e_cpus", frozenset(int(v) for v in e_cpus))
        if not _is_int(self.max_operations_per_session) or self.max_operations_per_session < 1:
            raise PolicyError(f"max_operations_per_session must be an int >= 1, got {self.max_operations_per_session!r}")
        if not _is_int(self.max_operations_per_boot) or self.max_operations_per_boot < 1:
            raise PolicyError(f"max_operations_per_boot must be an int >= 1, got {self.max_operations_per_boot!r}")
        dwell = self.min_dwell_s
        if isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or not math.isfinite(dwell) or dwell < 0:
            raise PolicyError(f"min_dwell_s must be a finite number >= 0, got {dwell!r}")
        object.__setattr__(self, "min_dwell_s", float(dwell))
        if isinstance(self.allow, (str, bytes)) or not isinstance(self.allow, Iterable):
            raise PolicyError("allow must be a sequence of AllowEntry")
        entries = tuple(self.allow)
        for entry in entries:
            if not isinstance(entry, AllowEntry):
                raise PolicyError(f"allow contains a non-AllowEntry: {entry!r}")
        names = [e.name for e in entries]
        if len(set(names)) != len(names):
            raise PolicyError(f"duplicate allow entry names: {sorted(n for n in set(names) if names.count(n) > 1)}")
        for i, a in enumerate(entries):
            for b in entries[i + 1 :]:
                if a.cmd == b.cmd and a.p1 & b.p1 and a.p2 & b.p2:
                    raise PolicyError(f"allow entries {a.name!r} and {b.name!r} overlap (ambiguous check)")
        object.__setattr__(self, "allow", entries)
        if isinstance(self.msr_allow, (str, bytes)) or not isinstance(self.msr_allow, Iterable):
            raise PolicyError("msr_allow must be a sequence of MsrAllowEntry")
        msr_entries = tuple(self.msr_allow)
        for m in msr_entries:
            if not isinstance(m, MsrAllowEntry):
                raise PolicyError(f"msr_allow contains a non-MsrAllowEntry: {m!r}")
            if m.cpus is not None and not m.cpus <= self.cpus:
                raise PolicyError(f"msr_allow entry {m.name!r} names CPUs outside the policy's cpus: {sorted(m.cpus - self.cpus)}")
        msr_names = [m.name for m in msr_entries]
        if len(set(msr_names)) != len(msr_names):
            raise PolicyError(f"duplicate msr_allow entry names: {sorted(n for n in set(msr_names) if msr_names.count(n) > 1)}")
        for i, a in enumerate(msr_entries):
            for b in msr_entries[i + 1 :]:
                if a.msr == b.msr and a.kind == b.kind:
                    raise PolicyError(f"msr_allow entries {a.name!r} and {b.name!r} overlap (one entry per MSR and kind)")
        object.__setattr__(self, "msr_allow", msr_entries)
        object.__setattr__(self, "ignored_keys", tuple(self.ignored_keys))

    def cpu_allowed(self, cpu: object) -> bool:
        return _is_int(cpu) and cpu in self.cpus

    def with_topology(self, topology: Topology) -> Policy:
        """The same policy over a live topology (D-036): ``cpus`` and ``e_cpus`` are exactly that topology's CPUs and E CPUs. Hardware paths bind the topology
        they derived from sysfs; an unbound policy carries the simulated reference CPU's sets."""
        return replace(self, cpus=frozenset(c.cpu for c in topology.cores), e_cpus=frozenset(topology.cpus_of_kind(KIND_E)))

    def core_kind(self, cpu: int) -> str:
        """``"E"`` for a CPU listed in ``e_cpus``, else ``"P"``. ``PolicyError`` for a policy not bound to a topology."""
        if self.e_cpus is None:
            raise PolicyError("this policy is not bound to a CPU topology (e_cpus unset): bind it with Policy.with_topology / load_policy(..., topology) before asking a CPU's kind")
        return "E" if cpu in self.e_cpus else "P"

    def match_msr(self, msr: object, kind: object) -> MsrAllowEntry | None:
        """The ``msr_allow`` entry for (``msr``, ``kind``), or ``None`` (default deny). Non-int / unknown input never matches. The caller still
        asks ``covers_cpu`` and ``cpu_allowed``."""
        if not _is_int(msr) or kind not in ("read", "write"):
            return None
        for entry in self.msr_allow:
            if entry.matches(int(msr), str(kind)):  # type: ignore[call-overload]
                return entry
        return None

    def match(self, cmd: object, p1: object, p2: object) -> AllowEntry | None:
        """The single allow entry covering ``(cmd, p1, p2)``, or ``None`` (default deny). Non-int input never matches."""
        if not (_is_int(cmd) and _is_int(p1) and _is_int(p2)):
            return None
        for entry in self.allow:
            if entry.matches(int(cmd), int(p1), int(p2)):  # type: ignore[call-overload]
                return entry
        return None


# --------------------------------------------------------------------------------------------------------------

# Default M1 policy
# --------------------------------------------------------------------------------------------------------------
def default_m1_policy(topology: Topology | None = None) -> Policy:
    """The shipped milestone-1 policy. D-049: the PROCESS settings and the allowlists only -- the 0x11 word is judged against the CPU's own limits (cmd 0x01 ratio, cmd 0x28 voltage,
    the offset field), read live and failing closed when unread; ``docs/knowledge/registers.md`` "Voltage semantics".

    * the CPUs of ``topology`` (the live one on hardware; without one, the simulated reference CPU); 1 operation per session; at most 12 operations per
      boot over all processes (``max_operations_per_boot``, T-016: counted from the audit log, reads included, read-back
      excluded; SAFETY.md "Mailbox-session discipline", D-011); 60 s dwell, also enforced across processes.
    * Reads (registers.md "Mailbox commands", P1/P2 columns): ``0x01`` p1 0..7 p2 0; ``0x02`` p1 0..7 p2 0; ``0x09`` p1 0x11
      (domain floors) with p2 = domain {0,2,7}; ``0x1C`` and ``0x14`` p1 0 p2 0; ``0x28`` p1 = domain {0,2,7} p2 0; ``0x10`` p1 {0,2,7} p2 0.
    * Writes: ``0x15`` p1 0 p2 0 with ``percore_enable_only``; ``0x11`` p1 0 p2 0 with ``vf_bounded`` (no entry params), and -- the XTU follow-ups of a per-core
      Apply, ``[MEASURED win 2026-10-01]`` -- ``0x1D`` p1 0 p2 0 with ``favored_ratio_follows_vf`` and ``0x27`` p1 0 p2 0 with
      ``granular_equals_read``; reads ``0x26`` p1 0 p2 0 (with ``0x1C``/``0x09``/``0x28`` above). Everything else, including every other
      write command, is denied.
    * T-023 plain MSRs: ``rdmsr`` of ``0x1AD 0x1AE 0x650 0x651 0xCE 0x194 0x198`` and (G-214, read-only RAPL registers) ``0x606 0x610 0x614`` (``msr_allow``); **no plain write** (a turbo table write needs a
      reviewed policy file with a ``turbo_ratio_bounded`` entry, D-017).

    ``[UNVERIFIED]`` as a whole: entries join a real allowlist only with a DECISIONS.md entry and a MEASURED read.
    """
    domains = frozenset({0, 2, 7})  # core, ring, NGU: the domains M1 cares about (registers.md "Domain P1 wire values")
    zero = frozenset({0})
    topo = topology if topology is not None else _REFERENCE_TOPOLOGY
    return Policy(
        cpus=frozenset(c.cpu for c in topo.cores),
        e_cpus=frozenset(topo.cpus_of_kind(KIND_E)),
        max_operations_per_session=1,
        max_operations_per_boot=12,
        min_dwell_s=60.0,
        allow=(
            AllowEntry("oc_capability_read", Cmd.OC_CAPABILITY_READ, frozenset(range(8)), zero, "read"),
            AllowEntry("per_core_capability_read", Cmd.PER_CORE_CAPABILITY_READ, frozenset(range(8)), zero, "read"),
            AllowEntry("voltage_floor_read", Cmd.VOLTAGE_FLOOR_READ, frozenset({0x11}), domains, "read"),
            AllowEntry("favored_core_read", Cmd.FAVORED_CORE_READ, zero, zero, "read"),
            AllowEntry("granular_ratio_read", Cmd.GRANULAR_RATIO_READ, zero, zero, "read"),
            AllowEntry("max_voltage_limit_read", Cmd.MAXIMUM_VOLTAGE_LIMIT_READ, domains, zero, "read"),
            AllowEntry("misc_global_config_read", Cmd.MISC_GLOBAL_CONFIG_READ, zero, zero, "read"),
            AllowEntry("voltage_freq_read", Cmd.VOLTAGE_FREQ_READ, domains, zero, "read"),
            AllowEntry(
                "misc_global_config_write", Cmd.MISC_GLOBAL_CONFIG_WRITE, zero, zero, "write", "percore_enable_only"
            ),
            AllowEntry("voltage_freq_write", Cmd.VOLTAGE_FREQ_WRITE, zero, zero, "write", "vf_bounded"),
            AllowEntry("favored_core_write", Cmd.FAVORED_CORE_WRITE, zero, zero, "write", "favored_ratio_follows_vf"),
            AllowEntry("granular_ratio_write", Cmd.GRANULAR_RATIO_WRITE, zero, zero, "write", "granular_equals_read"),
        ),
        # T-023: plain READS of the turbo-table MSRs and PLATFORM_INFO / FLEX_RATIO, no plain write: the shipped policy stays inert.
        msr_allow=tuple(MsrAllowEntry(f"msr_{msr:#x}_read", msr, "read") for msr in sorted(PLAIN_MSR_READABLE)),
    )


# --------------------------------------------------------------------------------------------------------------
# TOML loading (strict)
# --------------------------------------------------------------------------------------------------------------
#: D-049: operator value limits of older policy files. They still load (ignored, one warning line, ``Policy.ignored_keys``); the last two are tables, named by their sub-keys.
LEGACY_KEYS = (
    "effective_offset_floor_mv",
    "assumed_bios_offset_mv",
    "offset_ceiling_mv",
    "max_ratio_step",
    "max_v_step_mv",
    "max_offset_step_mv",
    "ratio_max",
    "v_target_max_mv",
    "power",
    "runtime",
)
_LEGACY_TABLES = frozenset({"power", "runtime"})
_TOP_KEYS = frozenset({"cpus", "max_operations_per_session", "max_operations_per_boot", "min_dwell_s", "allow", "msr_allow", "e_cpus", *LEGACY_KEYS})
_ENTRY_KEYS = frozenset({"name", "cmd", "p1", "p2", "kind", "check", "params"})
_ENTRY_REQUIRED = frozenset({"name", "cmd", "p1", "p2", "kind"})
_MSR_ENTRY_KEYS = frozenset({"name", "msr", "kind", "cpus", "check", "params"})
_MSR_ENTRY_REQUIRED = frozenset({"name", "msr", "kind"})


def _parse_int_list(what: str, value: object) -> list[int]:
    """TOML list of ints and/or ``"lo-hi"`` strings -> flat list of ints."""
    if not isinstance(value, list):
        raise PolicyError(f"{what} must be a list")
    out: list[int] = []
    for item in value:
        if _is_int(item):
            out.append(int(item))
        elif isinstance(item, str):
            lo_s, sep, hi_s = item.partition("-")
            try:
                lo, hi = int(lo_s.strip(), 0), int(hi_s.strip(), 0)
            except ValueError:
                raise PolicyError(f"{what}: {item!r} is not an int or 'lo-hi'") from None
            if not sep or lo > hi or hi - lo > 4096:
                raise PolicyError(f"{what}: bad range {item!r}")
            out.extend(range(lo, hi + 1))
        else:
            raise PolicyError(f"{what}: {item!r} is not an int or 'lo-hi' string")
    return out


def _read_source(source: str | os.PathLike[str]) -> str:
    """``PathLike`` -> file contents. ``str`` -> the TOML text itself, except a single-line string naming an existing file."""
    if isinstance(source, str) and ("\n" in source or not os.path.isfile(source)):
        return source
    if not isinstance(source, (str, os.PathLike)):
        raise PolicyError(f"load_policy needs a path or TOML text, got {type(source).__name__}")
    try:
        return Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"cannot read policy file {source!s}: {exc}") from exc


AUTO = "auto"
_LOG = logging.getLogger("arltuner.policy")
_WARNED: set[tuple[str | None, tuple[str, ...]]] = set()  # (file, ignored keys) already warned about in this process


def load_policy(source: str | os.PathLike[str], topology: Topology | None = None) -> Policy:
    """Load a policy from TOML text or a file path. Strict: unknown keys, unknown checks, bad types -> ``PolicyError``.

    ``topology`` (D-036): the live topology. ``cpus = "auto"`` / ``e_cpus = "auto"`` are derived from it and are a ``PolicyError`` without one (never a default); a
    policy without an ``e_cpus`` key takes the topology's E CPUs when one is given.

    D-049: the operator value limits of older policy files (``LEGACY_KEYS``: the offset window, the step limits, ``ratio_max``, ``v_target_max_mv`` and the ``[power]`` /
    ``[runtime]`` tables) still load -- the installed ``/etc/arltuner/policy.toml`` has them -- but are IGNORED: one warning line (``logging``, logger ``arltuner.policy``) names
    them and ``Policy.ignored_keys`` carries the names. Their values are not even validated."""
    text = _read_source(source)
    named = str(source) if isinstance(source, os.PathLike) or (isinstance(source, str) and "\n" not in source and os.path.isfile(source)) else None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"policy is not valid TOML: {exc}") from exc
    unknown = set(doc) - _TOP_KEYS
    if unknown:
        raise PolicyError(f"unknown top-level key(s): {sorted(unknown)}")
    if "cpus" not in doc:
        raise PolicyError("policy must define cpus (no default: an absent CPU set is not 'all CPUs')")

    def needs_topology(what: str) -> Topology:
        if topology is None:
            raise PolicyError(f'{what} = "auto" is derived from the live topology, which was not supplied')
        return topology

    if doc["cpus"] == AUTO:
        kwargs: dict[str, Any] = {"cpus": frozenset(c.cpu for c in needs_topology("cpus").cores)}
    else:
        kwargs = {"cpus": frozenset(_parse_int_list("cpus", doc["cpus"]))}
    if named is not None:
        kwargs["source"] = named
    kwargs["e_cpus"] = frozenset(topology.cpus_of_kind(KIND_E)) if topology is not None else _E_CPUS_REFERENCE  # no topology = the simulator / tests, on purpose (G-141)
    if "max_operations_per_session" in doc:
        kwargs["max_operations_per_session"] = doc["max_operations_per_session"]
    if "max_operations_per_boot" in doc:
        kwargs["max_operations_per_boot"] = doc["max_operations_per_boot"]
    if "min_dwell_s" in doc:
        kwargs["min_dwell_s"] = doc["min_dwell_s"]
    if "e_cpus" in doc:
        if doc["e_cpus"] == AUTO:
            kwargs["e_cpus"] = frozenset(needs_topology("e_cpus").cpus_of_kind(KIND_E))
        else:
            kwargs["e_cpus"] = frozenset(_parse_int_list("e_cpus", doc["e_cpus"]))
    ignored: list[str] = []
    for key in LEGACY_KEYS:
        if key not in doc:
            continue
        value = doc[key]
        if key in _LEGACY_TABLES and isinstance(value, dict) and value:
            ignored.extend(f"{key}.{sub}" for sub in sorted(value))
        else:
            ignored.append(key)
    if ignored:
        kwargs["ignored_keys"] = tuple(ignored)
        seen = (named, tuple(ignored))
        if named is None or seen not in _WARNED:  # the helper loads the file on every op: one warning line per file and process, not one per load
            if named is not None:
                _WARNED.add(seen)
            _LOG.warning(
                "policy %s: ignoring %s -- D-049: the helper enforces only the CPU's own limits (cmd 0x01 ratio, cmd 0x28 voltage, the offset field, MSR 0x610, cpufreq, uncore); "
                "these operator value limits no longer apply, remove them from the file",
                named or "(text)",
                ", ".join(ignored),
            )
    raw_entries = doc.get("allow", [])
    if not isinstance(raw_entries, list) or not all(isinstance(e, dict) for e in raw_entries):
        raise PolicyError("allow must be an array of tables ([[allow]])")
    entries = []
    for i, raw in enumerate(raw_entries):
        label = f"allow[{i}]" + (f" ({raw['name']})" if isinstance(raw.get("name"), str) else "")
        bad = set(raw) - _ENTRY_KEYS
        if bad:
            raise PolicyError(f"{label}: unknown key(s) {sorted(bad)}")
        missing = _ENTRY_REQUIRED - set(raw)
        if missing:
            raise PolicyError(f"{label}: missing key(s) {sorted(missing)}")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise PolicyError(f"{label}: params must be a table")
        entries.append(
            AllowEntry(
                name=raw["name"],
                cmd=raw["cmd"],
                p1=frozenset(_parse_int_list(f"{label} p1", raw["p1"])),
                p2=frozenset(_parse_int_list(f"{label} p2", raw["p2"])),
                kind=raw["kind"],
                check=raw.get("check"),
                params=params,
            )
        )
    kwargs["allow"] = tuple(entries)
    raw_msr = doc.get("msr_allow", [])
    if not isinstance(raw_msr, list) or not all(isinstance(e, dict) for e in raw_msr):
        raise PolicyError("msr_allow must be an array of tables ([[msr_allow]])")
    msr_entries = []
    for i, raw in enumerate(raw_msr):
        label = f"msr_allow[{i}]" + (f" ({raw['name']})" if isinstance(raw.get("name"), str) else "")
        bad = set(raw) - _MSR_ENTRY_KEYS
        if bad:
            raise PolicyError(f"{label}: unknown key(s) {sorted(bad)}")
        missing = _MSR_ENTRY_REQUIRED - set(raw)
        if missing:
            raise PolicyError(f"{label}: missing key(s) {sorted(missing)}")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise PolicyError(f"{label}: params must be a table")
        msr_entries.append(
            MsrAllowEntry(
                name=raw["name"],
                msr=raw["msr"],
                kind=raw["kind"],
                cpus=None if "cpus" not in raw else frozenset(_parse_int_list(f"{label} cpus", raw["cpus"])),
                check=raw.get("check"),
                params=params,
            )
        )
    kwargs["msr_allow"] = tuple(msr_entries)
    return Policy(**kwargs)
