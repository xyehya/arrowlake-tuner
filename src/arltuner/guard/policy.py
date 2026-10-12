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
    effective_offset_floor_mv = -100      # optional (D-015): floor of BIOS offset + runtime offset
    assumed_bios_offset_mv = { P = -50, E = 0 }   # optional (D-015): the operator's BIOS per-core V/F offset by core kind

    # D-016 bounds of the 0x11 word (all optional; an absent ceiling means "no raise": see ``vf_bounded``):
    offset_ceiling_mv = 0                 # runtime offset may not exceed this (default 0: positive offsets refused)
    max_ratio_step = 1                    # |ratio - current| per 0x11 word
    max_v_step_mv = 20                    # |V target - current| per 0x11 word (mV)
    max_offset_step_mv = 25               # |offset - current| per 0x11 word (mV)
    [ratio_max]                           # per core kind; absent kind = the current ratio (no raise); "auto" = fused 0x1C + 3
    E = 50                                # effective ceiling = this, clamped by the CPU's own cmd 0x01 [7:0] (D-039); 0x01 unread = no raise
    [v_target_max_mv]                     # per core kind (mV); absent kind = the current/fused V target (no raise)
    E = 1250                              # effective ceiling = this, clamped by the CPU's own cmd 0x28 maximum - 1 LSB (D-039)

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

Evidence (T-021, ``[MEASURED win 2026-10-01]``, ``docs/research/win-xtu-live-trace.md`` S-03d, S-04...S-07): XTU's per-core Apply is
``0x11`` (full word) -> ``0x1D`` (data = that word's ratio) -> ``0x26`` read -> ``0x27`` (data = the value read), and its ``0x11`` word
carries the core's non-zero baseline ratio (else ``0x1C[7:0]``) and the **fused** V target (``0x09 P1=0x11 P2=0`` ``[19:8]``): the
per-core ``0x10`` baseline is ``0x0`` on 23 of 24 cores, so it is not a usable reference by itself. The checks below therefore
also see the facts the guard has read *in this session* (``CheckContext``).

Scope (D-016, operator, 2026-10-01; supersedes D-009's undervolt-first rule): ratio, adaptive OC-point voltage and offset of P and E
cores may go **up or down**. Safety is expressed as explicit, reviewed **bounds**, never as a direction: per-kind ratio and V-target
ceilings (default = the no-raise values, so the shipped policy cannot overclock; an overclock only ever comes from numbers a human wrote
in a policy file), the D-015 offset window, the hardware maximum voltage of ``0x28``, per-operation step limits and the
voltage-first / frequency-first ordering of ``registers.md`` ("Ordering"). Whether the hardware accepts a raise at all, the E-core write
scope and what ``0x1C`` returns after a ``0x1D`` are ``[UNVERIFIED]`` on this system.
"""
from __future__ import annotations

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
    V_TARGET_MIN_MV as _V_TARGET_MIN_MV,
)
from arltuner.topology.cpumap import KIND_E, KIND_P, Topology
from arltuner.topology.reference import arl_s_285k

__all__ = [
    "AUTO",
    "CHECKS",
    "MSR_CHECKS",
    "POINT_P2_MAX",
    "MSR_TURBO_KIND",
    "PLAIN_MSR_READABLE",
    "PLAIN_MSR_WRITABLE",
    "MsrAllowEntry",
    "MsrCheckContext",
    "MsrRequestLike",
    "build_msr_context",
    "turbo_bytes",
    "DEFAULT_ASSUMED_BIOS_OFFSET_MV",
    "DEFAULT_EFFECTIVE_OFFSET_FLOOR_MV",
    "DEFAULT_MAX_OFFSET_STEP_MV",
    "DEFAULT_MAX_RATIO_STEP",
    "DEFAULT_MAX_V_STEP_MV",
    "DEFAULT_OFFSET_CEILING_MV",
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

#: D-015 (docs/DECISIONS.md): the runtime offset ADDS to the BIOS per-core V/F offset ``[MEASURED win 2026-10-01]``. The BIOS offset is not readable through the
#: mailbox; a policy may state one per core kind (``assumed_bios_offset_mv``) and the runtime floor of a kind is
#: ``effective_offset_floor_mv - assumed_bios_offset_mv[kind]``. G-141: the default assumes **no** BIOS offset for either kind (0), so the runtime floor of a policy that
#: states nothing is ``effective_offset_floor_mv`` itself for P and E. (It used to be P -50 mV, one operator's old BIOS value, which cut 50 mV off the window on every other machine.)
DEFAULT_ASSUMED_BIOS_OFFSET_MV: Mapping[str, int] = MappingProxyType({"P": 0, "E": 0})
DEFAULT_EFFECTIVE_OFFSET_FLOOR_MV = -100

#: D-016 defaults of the per-operation step limits and the offset ceiling: the orchestrator's conservative choice (``[UNVERIFIED]`` as
#: engineering values, not hardware facts). One ratio bin, 20 mV of V target and 25 mV of offset per 0x11 word; positive runtime offsets
#: stay refused until a reviewed policy raises ``offset_ceiling_mv``.
DEFAULT_MAX_RATIO_STEP = 1
DEFAULT_MAX_V_STEP_MV = 20
DEFAULT_MAX_OFFSET_STEP_MV = 25
DEFAULT_OFFSET_CEILING_MV = 0

#: D-039 (operator yes, 2026-10-12): **no number in this file is a ceiling a policy cannot pass.** The effective limit of a ratio is the policy value clamped by
#: the CPU's own ``0x01 [7:0]`` fused max OC ratio, of a V target the policy value clamped by ``0x28 - 1 LSB``; both are read live in the session (an unread limit
#: = no raise, fail closed). What remains here are the **field encodings** (the widest value the word can carry, registers.md "V/F data word") and the XTU range
#: of the offset: a policy value outside them cannot be written into the word at all.
# ENCODING_* (imported above from arltuner.mailbox.encoding, the one home of the field-encoding bounds; G-141) are re-exported here unchanged.
#: D-038 (operator yes, 2026-10-12): the ``"auto"`` ratio ceiling is the 0x1C[15:8] fused max of the kind plus this many bins (P 57 -> 60, E 46 -> 49), so an OC ratio
#: above the top fused point is reachable without a hand-written ``ratio_max``; clamped by the CPU's own ``0x01`` limit (D-039), never by a constant.
AUTO_OC_HEADROOM = 3
_RATIO_STEP_RANGE = (1, ENCODING_RATIO_MAX)
_V_STEP_MV_RANGE = (2, 1000)  # >= 2 mV so that every step window holds at least one whole-mV value (1 mV = 1.024 raw)
_OFFSET_STEP_MV_RANGE = (2, 1000)

#: The simulated reference CPU, used ONLY where the caller asked for no topology on purpose (``default_m1_policy()`` / ``load_policy(text)`` of the simulator and the tests).
#: G-141 (D-036): a ``Policy`` built directly with no ``e_cpus`` is *unbound* (``e_cpus is None``) and ``core_kind`` refuses to guess a layout for it; every hardware path
#: binds the live topology (``Policy.with_topology`` / ``load_policy(..., topology)`` / ``default_m1_policy(topology)``).
_REFERENCE_TOPOLOGY = arl_s_285k()
_E_CPUS_REFERENCE = frozenset(_REFERENCE_TOPOLOGY.cpus_of_kind(KIND_E))

_U32_MAX = 0xFFFFFFFF
_CPU_MAX = 1023  # far above any real CPU number; rejects garbage, not a hardware fact


#: T-023 plain-MSR allowlist universe (``docs/knowledge/registers.md`` "Not the mailbox" + MSR table; facts ``[MEASURED lnx 2026-10-01]``):
#: only these may ever be read through the guard ...
PLAIN_MSR_READABLE = frozenset({0x1AD, 0x1AE, 0x650, 0x651, 0xCE, 0x194, 0x198})  # 0x198 PERF_STATUS: running ratio [15:8], requested VID [47:32]/8192 (T-026 --measure)
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
_DEFAULT_MAX_V_STEP_RAW = VfWord.v_to_raw(DEFAULT_MAX_V_STEP_MV / 1000)
_DEFAULT_MAX_OFFSET_STEP_RAW = VfWord.mv_to_offset_raw(DEFAULT_MAX_OFFSET_STEP_MV)
_NO_STEP_LIMIT = (255, 4095, 2047)  # (ratio, V target raw, offset raw): larger than any field difference; see ``unlimited_steps``


@dataclass(frozen=True)
class CheckContext:
    """Facts about ``req.cpu`` that the guard (or the planner, mirroring it) has this session, plus the policy's resolved D-016 limits for
    its core kind. ``None`` = not known (for the facts) / "the no-raise value" (for ``ratio_max`` / ``v_target_max_raw``). The limit
    fields default to the shipped policy's values, so a caller that sets none gets the strictest behaviour."""

    offset_floor_raw: int | None = None  # runtime floor of the V/F offset field for this CPU's kind (D-015, <= 0)
    favored_word: int | None = None  # 0x1C P1=0 P2=0 read this session: [7:0] favored ratio, [15:8] fused
    fused_word: int | None = None  # 0x09 P1=0x11 P2=0 read this session: the fused V/F point ([7:0] ratio, [19:8] voltage)
    vf_word_sent: int | None = None  # the last 0x11 word (P1=0 P2=0) executed with rc 0 for this CPU in this session
    max_voltage_word: int | None = None  # 0x28 P1=0 P2=0 read this session: [11:0] max voltage S2.10 (XTU uses raw - 1)
    ratio_max: int | None = None  # D-016 policy ceiling of the ratio for this CPU's kind; None = no raise (current ratio)
    v_target_max_raw: int | None = None  # D-016 policy ceiling of the V target (raw); None = no raise (current / fused V target)
    offset_ceiling_raw: int = 0  # D-016: runtime offset ceiling (raw, >= 0)
    max_ratio_step: int = DEFAULT_MAX_RATIO_STEP  # D-016 per-operation step limits, in word units (ratio bins / raw S2.10 / raw offset)
    max_v_step_raw: int = _DEFAULT_MAX_V_STEP_RAW
    max_offset_step_raw: int = _DEFAULT_MAX_OFFSET_STEP_RAW
    top_fused_ratio: int | None = None  # G-120: the highest fused V/F point ratio of this CPU's kind; a P2 = 0 ratio at or below it has no OC point. None = not known
    ratio_text: str = field(default="", compare=False)  # D-039: where ``ratio_max`` comes from (policy file / CPU limit cmd 0x01 / why it is the no-raise value), for refusal messages
    v_text: str = field(default="", compare=False)  # the same for ``v_target_max_raw`` (policy file / CPU limit cmd 0x28)
    policy_where: str = field(default="the policy", compare=False)  # the policy file the limits come from, for the offset window's message

    def unlimited_steps(self) -> CheckContext:
        """The same context with the step limits lifted: the planner uses it to tell "refused only because the step is too big" (a ramp
        is the answer) from "refused by an absolute bound" (no ramp can help)."""
        ratio, v_raw, offset_raw = _NO_STEP_LIMIT
        return replace(self, max_ratio_step=ratio, max_v_step_raw=v_raw, max_offset_step_raw=offset_raw)


def fused_top_ratio(fused_word: int | None) -> int | None:
    """The highest fused V/F point ratio of a core, live: ``[7:0]`` of its ``0x09 P1=0x11 P2=0`` word (registers.md), or ``None`` when that word was not read
    (or reads 0). An OC ratio (the ``P2 = 0`` word) at or below it creates no OC point."""
    if not fused_word:
        return None
    return (fused_word & 0xFF) or None


def hw_ratio_max(oc_cap_words: Iterable[int | None]) -> int | None:
    """The CPU's own ratio limit of a core kind, live (D-039): ``[7:0]`` of the ``0x01 P1=0 P2=0`` words of that kind read in this session (registers.md 0x01: fused max
    OC ratio; ``[8]`` = ratio overclocking ok). The lowest if several differ (fail closed). ``None`` when no word was read, a word reads 0, or ``[8]`` is clear: the limit
    is unknown, so nothing may be raised. ``[UNVERIFIED]`` that an E CPU answers ``P1=0`` for the E domain (the decompile has no separate E domain, ``[DECOMPILED
    win-xtu-decompile-controls.md]``); ``[MEASURED win 2026-10-01]`` P reads ``0x778`` = 120x."""
    ratios = [w & 0xFF for w in oc_cap_words if w is not None and w & 0x100 and w & 0xFF]
    return min(ratios) if ratios else None


def hw_v_max_mv(max_voltage_word: int | None) -> int | None:
    """The CPU's own V target limit in whole mV (D-039): ``0x28 [11:0] - 1 LSB`` (``hw_v_max_raw``), rounded down. ``None`` when the word was not read or its field is 0."""
    if max_voltage_word is None or (max_voltage_word & 0xFFF) == 0:
        return None
    return hw_v_max_raw(max_voltage_word) * 1000 // 1024 or None  # a limit that rounds to 0 mV is no usable limit


def fused_ratio_cap(kind: str, favored_words: Iterable[int | None], hw_max: int | None = None) -> int | None:
    """The ``"auto"`` ratio ceiling of a core kind (D-038): the highest **fused** ratio ``0x1C[15:8]`` over the ``0x1C`` words of that kind read in this session plus
    ``AUTO_OC_HEADROOM`` bins (P 57 -> 60, E 46 -> 49), clamped by ``hw_max`` (the CPU's own ``0x01`` limit, D-039) when given. ``[7:0]`` is deliberately not used: it is
    the favored / current ratio, it follows the BIOS turbo setting and a ``0x1D`` write (E reads 48 while its fused ratio is 46), so a cap from it would ratchet
    ``[MEASURED win 2026-10-01]`` (``measured_baseline.FAVORED_WORD``: P ``0x3939`` / ``0x3737`` = 57 / 55, E ``0x2E30`` = fused 46). ``None`` (the no-raise value)
    when no ``0x1C`` of the kind was read. Never a sysfs value: ``cpuinfo_max_freq`` follows runtime OC writes, so a cap derived from it would rise with every raise."""
    best = 0
    for word in favored_words:
        if word:
            best = max(best, (word >> 8) & 0xFF)
    if not best:
        return None
    cap = best + AUTO_OC_HEADROOM
    return cap if hw_max is None else min(cap, hw_max)


def ratio_limit(policy: Policy, kind: str, favored_words: Iterable[int | None], oc_cap_words: Iterable[int | None] = ()) -> tuple[int | None, str]:
    """``(effective ratio ceiling or None, source text)`` of a core kind (D-039). The ceiling is the policy value (a number, or ``"auto"`` = fused ``0x1C`` + 3)
    clamped by the CPU's own ``0x01`` limit -- the ``oc_cap_words`` of this session and the live limit bound to the policy (``Policy.with_hw_limits``). ``None`` =
    no raise: the policy sets none, or the CPU's limit was not read (fail closed). The text names where the number comes from; every refusal of a ratio quotes it."""
    where = policy.where
    oc_cap_words = list(oc_cap_words)
    hw_values = [v for v in (hw_ratio_max(oc_cap_words), policy.hw_ratio_max[kind]) if v is not None]
    hw = min(hw_values) if hw_values else None
    unsupported = hw is None and any(w is not None for w in oc_cap_words)  # a 0x01 word was read, but it says no ratio overclocking (bit 8 clear) or max 0
    unread = "the CPU reports ratio overclocking as not supported (cmd 0x01: bit 8 clear or max ratio 0)" if unsupported else "the CPU's own limit (cmd 0x01) was not read this session"
    if kind in policy.ratio_auto:
        label = f'policy ratio_max {kind} = "auto" ({where})'
        base = fused_ratio_cap(kind, favored_words)
        if base is None:
            return None, f"no-raise value: {label} needs the fused ratio of cmd 0x1C, which was not read this session"
        fused = base - AUTO_OC_HEADROOM
        if hw is None:
            return None, f"no-raise value: {label} = fused {fused} + {AUTO_OC_HEADROOM}, but {unread}"
        if base > hw:
            return hw, f"CPU limit {hw}x (cmd 0x01 [7:0] fused max OC ratio; {label} = fused {fused} + {AUTO_OC_HEADROOM} = {base} is above it)"
        return base, f"{label} = fused {fused} + {AUTO_OC_HEADROOM} = {base}; your CPU allows up to {hw}x per cmd 0x01"
    number = policy.ratio_max[kind]
    if number is None:
        return None, f"no-raise value: policy ratio_max {kind} is not set ({where})"
    if hw is None:
        return None, f"no-raise value: policy ratio_max {kind} = {number} ({where}), but {unread}"
    if number > hw:
        return hw, f"CPU limit {hw}x (cmd 0x01 [7:0] fused max OC ratio; policy ratio_max {kind} = {number} ({where}) is above it)"
    return number, f"policy ratio_max {kind} = {number} ({where}; your CPU allows up to {hw}x per cmd 0x01)"


def v_target_limit_mv(policy: Policy, kind: str, max_voltage_word: int | None) -> tuple[int | None, str]:
    """``(effective V target ceiling in whole mV or None, source text)`` of a core kind (D-039): the policy value (a number, or ``"auto"``) clamped by the CPU's own
    ``0x28`` maximum minus one LSB (the word of this session and/or the live limit bound to the policy). ``"auto"`` with ``0x28`` unread = no raise (``None``). A policy
    number with ``0x28`` unread stays the number: ``vf_bounded`` then refuses any V target above the no-raise value (a raise needs the read)."""
    where = policy.where
    hw_values = [v for v in (hw_v_max_mv(max_voltage_word), policy.hw_v_target_max_mv[kind]) if v is not None]
    hw_mv = min(hw_values) if hw_values else None
    if kind in policy.v_target_auto:
        label = f'policy v_target_max_mv {kind} = "auto" ({where})'
        if hw_mv is None:
            return None, f"no-raise value: {label} is the CPU's own limit (cmd 0x28), which was not read this session"
        return hw_mv, f"{label} = CPU limit {hw_mv} mV (cmd 0x28 max - 1 LSB)"
    number = policy.v_target_max_mv[kind]
    if number is None:
        return None, f"no-raise value: policy v_target_max_mv {kind} is not set ({where})"
    if hw_mv is None:
        return number, f"policy v_target_max_mv {kind} = {number} mV ({where}); the CPU's own limit (cmd 0x28) was not read this session"
    if number > hw_mv:
        return hw_mv, f"CPU limit {hw_mv} mV (cmd 0x28 max - 1 LSB; policy v_target_max_mv {kind} = {number} mV ({where}) is above it)"
    return number, f"policy v_target_max_mv {kind} = {number} mV ({where}; your CPU allows up to {hw_mv} mV per cmd 0x28)"


def v_target_limit(policy: Policy, kind: str, max_voltage_word: int | None) -> tuple[int | None, str]:
    """``v_target_limit_mv`` with the ceiling as raw S2.10 (``None`` = no raise)."""
    mv, text = v_target_limit_mv(policy, kind, max_voltage_word)
    return (None if mv is None else VfWord.v_to_raw(mv / 1000)), text


def build_context(
    policy: Policy,
    cpu: int,
    *,
    favored_word: int | None,
    fused_word: int | None,
    vf_word_sent: int | None,
    max_voltage_word: int | None = None,
    favored_words_of_kind: Iterable[int | None] | None = None,
    oc_cap_words_of_kind: Iterable[int | None] = (),
) -> CheckContext:
    """The context for a request on ``cpu``: the policy's D-015 runtime floor and D-016 limits for its core kind plus the session facts. The ratio and V target
    ceilings are the policy values clamped by the CPU's own limits (``oc_cap_words_of_kind``: the ``0x01`` words of the kind read in this session, D-039)."""
    kind = policy.core_kind(cpu)
    favored = favored_words_of_kind if favored_words_of_kind is not None else [favored_word]  # all CPUs of the kind that were read; else this CPU's own word
    ratio_cap, ratio_text = ratio_limit(policy, kind, favored, oc_cap_words_of_kind)
    v_max_raw, v_text = v_target_limit(policy, kind, max_voltage_word)
    return CheckContext(
        top_fused_ratio=fused_top_ratio(fused_word),
        offset_floor_raw=policy.offset_floor_raw(cpu),
        favored_word=favored_word,
        fused_word=fused_word,
        vf_word_sent=vf_word_sent,
        max_voltage_word=max_voltage_word,
        ratio_max=ratio_cap,
        v_target_max_raw=v_max_raw,
        offset_ceiling_raw=VfWord.mv_to_offset_raw(policy.offset_ceiling_mv),
        max_ratio_step=policy.max_ratio_step,
        max_v_step_raw=VfWord.v_to_raw(policy.max_v_step_mv / 1000),
        max_offset_step_raw=(policy.max_offset_step_mv * 1024 + 512) // 1000,  # a step is a difference, not a field value: 1000 mV = 1024 raw (VfWord.mv_to_offset_raw rejects it)
        ratio_text=ratio_text,
        v_text=v_text,
        policy_where=policy.where,
    )


Check = Callable[..., tuple[bool, str]]


@dataclass(frozen=True)
class MsrCheckContext:
    """What ``turbo_ratio_bounded`` needs besides the request and the baseline: the table's core kind (from the MSR, not the CPU), the policy's
    ratio ceiling for that kind (``None`` = no raise: each byte at most its baseline byte), the per-operation ratio step and ``0xCE[15:8]``
    (the base ratio) if ``0xCE`` was read in this session (``None`` = not read)."""

    kind: str | None = None
    ratio_max: int | None = None
    max_ratio_step: int = DEFAULT_MAX_RATIO_STEP
    base_ratio: int | None = None
    ratio_text: str = field(default="", compare=False)  # D-039: where ``ratio_max`` comes from, for the refusal message


def build_msr_context(
    policy: Policy,
    msr: int,
    *,
    platform_info: int | None,
    favored_words: Mapping[int, int | None] | None = None,
    oc_cap_words: Mapping[int, int | None] | None = None,
) -> MsrCheckContext:
    """The context for a plain write of ``msr``: kind from the MSR (unknown MSR -> ``kind None``, which the check refuses), the policy's
    ceiling and step for that kind, and ``0xCE[15:8]`` from ``platform_info`` (the ``0xCE`` word read this session, or ``None``)."""
    kind = MSR_TURBO_KIND.get(msr)
    ratio_cap, ratio_text = None, ""
    if kind is not None:  # the 0x1C / 0x01 words of this kind read in this session (cpu -> word); the CPU's own limit clamps the policy value (D-039)
        ratio_cap, ratio_text = ratio_limit(
            policy,
            kind,
            [w for c, w in (favored_words or {}).items() if policy.core_kind(c) == kind],
            [w for c, w in (oc_cap_words or {}).items() if policy.core_kind(c) == kind],
        )
    return MsrCheckContext(
        kind=kind,
        ratio_max=ratio_cap,
        max_ratio_step=policy.max_ratio_step,
        base_ratio=None if platform_info is None else (platform_info >> 8) & 0xFF,
        ratio_text=ratio_text,
    )


def turbo_bytes(word: int) -> tuple[int, ...]:
    """The eight ratio buckets of a ``0x1AD`` / ``0x650`` word, bucket 0 (LSB = one active core) first."""
    return tuple((word >> (8 * k)) & 0xFF for k in range(_TURBO_BUCKETS))


def _check_turbo_ratio_bounded(
    req: MsrRequestLike, baseline: int, params: Mapping[str, Any], ctx: MsrCheckContext | None = None
) -> tuple[bool, str]:
    """Plain write of a turbo ratio table (``0x650`` E / ``0x1AD`` P; T-023, D-017), judged against the word read with ``rdmsr`` earlier in the
    session (``baseline``, required) by **bounds**, never by direction:

    1. the MSR is one of the two tables (the kind comes from the MSR) and ``value`` is a u64;
    2. every byte non-zero (a zero bucket was never observed; XTU never sends one) and the buckets non-increasing from bucket 0 to 7
       (more active cores never turbo higher);
    3. every byte ``<=`` the policy's ``ratio_max`` of that kind, **else ``<=`` its baseline byte** (no ceiling set = no raise, per byte; the
       same no-raise default as ``vf_bounded``);
    4. ``|byte - baseline byte| <= max_ratio_step`` per bucket;
    5. every byte ``>= 0xCE[15:8]`` (the base ratio) if ``0xCE`` was read this session, else ``>= min(baseline bytes) - max_ratio_step``.

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
    for k in range(_TURBO_BUCKETS - 1):
        if new[k] < new[k + 1]:
            return False, f"bucket {k} ({new[k]}) is below bucket {k + 1} ({new[k + 1]}): the table must not increase with more active cores"
    for k, byte in enumerate(new):
        cap = old[k] if ctx.ratio_max is None else ctx.ratio_max
        if byte > cap:
            which = ctx.ratio_text or (f"baseline byte (no-raise value, kind {ctx.kind})" if ctx.ratio_max is None else f"policy ratio_max[{ctx.kind}]")
            return False, f"bucket {k}: ratio {byte} above {cap} ({which}{'; the cap is the baseline byte' if ctx.ratio_max is None else ''})"
    for k, (byte, was) in enumerate(zip(new, old)):
        if abs(byte - was) > ctx.max_ratio_step:
            return False, f"bucket {k}: step {byte - was:+d} from {was} exceeds the per-operation limit {ctx.max_ratio_step}"
    floor = ctx.base_ratio if ctx.base_ratio is not None else min(old) - ctx.max_ratio_step
    source = "0xCE[15:8] base ratio" if ctx.base_ratio is not None else "baseline minimum minus the step limit (0xCE was not read)"
    for k, byte in enumerate(new):
        if byte < floor:
            return False, f"bucket {k}: ratio {byte} below {floor} ({source})"
    return True, "ok"


def hw_v_max_raw(max_voltage_word: int) -> int:
    """The highest V target (raw S2.10) the hardware allows from a ``0x28`` word: XTU uses ``[11:0] - 1``, and a field of 0 means 2.0 V
    (registers.md "Mailbox commands", ``[DECOMPILED]``; the values of this system, P ``0x681`` / E ``0x56D``, are ``[MEASURED win
    2026-10-01]``)."""
    raw = max_voltage_word & 0xFFF
    return 2048 if raw == 0 else raw - 1


def _check_vf_bounded(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x11 (V/F write), D-016: judged against the word read with cmd 0x10 **and** the facts of this session, by **bounds**, never by
    direction. Order of the tests (absolute bounds first, so a refusal names the root cause):

    1. mode adaptive in baseline and word (static / Power-Gate stay out of scope); ``ratio != 0`` and ``v_target_raw != 0`` (XTU never sends
       a zero field and the hardware's acceptance of one was never observed, ``[UNVERIFIED]``) -- except (G-120, D-035) a zero V target on a word with
       no OC point (``0 < ratio <= ctx.top_fused_ratio``: P 57, E 46), which is ``ratio | offset << 21`` as the hardware keeps it
       ``[MEASURED lnx 2026-10-11]``; that word skips the V reference / V step / V ordering tests, every other bound applies;
    2. ``ratio <= ratio ceiling`` and ``v_target_raw <= V target ceiling``: the policy's per-kind number, **else the no-raise value**
       (``max`` of the non-zero baseline and ``0x1C[7:0]`` / fused ``0x09`` voltage), so the shipped policy refuses every raise;
    3. the hardware maximum: ``v_target_raw <= hw_v_max_raw(0x28 word)`` whenever the session read ``0x28``; a V target above the no-raise
       value **needs** that read (fail closed);
    4. the offset window ``max(effective floor - assumed BIOS offset, offset_raw_min) <= offset_raw <= min(offset ceiling, offset_raw_max)``
       (D-015; the window includes positive offsets only when a policy raised the ceiling);
    5. per-operation step limits against the current state: ``|ratio - ratio_now|``, ``|V target - V target now|`` and ``|offset - offset
       now|``, where *now* is the baseline word with a zero ratio / V target filled in from ``0x1C[7:0]`` / the fused voltage;
    6. ordering (registers.md "Ordering", XTU: raise => voltage first, lower => frequency first): one word may not move the ratio and the
       V target the same way relative to the last word sent this session (else the baseline). Raise the V target first, then the ratio;
       lower the ratio first, then the V target.

    The step limits are measured from the session baseline, so a session cannot drift further than one step from it by repeating writes. A
    ceiling with no reference at all (baseline 0 and no ``0x1C`` / ``0x09`` read) refuses. A caller without a ``CheckContext`` is refused.
    ``[MEASURED win 2026-10-01]``: XTU's word has ratio = the non-zero baseline ratio else ``0x1C[7:0]`` and V target = the fused voltage.
    ``[UNVERIFIED]`` on this system: that the hardware accepts a raise at all; that ``0x1C`` follows a ``0x1D``.
    """
    if ctx is None or ctx.offset_floor_raw is None:
        return False, "no check context (the CPU's runtime offset floor is unknown); the bounds cannot be evaluated"
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
    v_noraise = max(base.v_target_raw, fused_v or 0)
    ratio_cap = ratio_noraise if ctx.ratio_max is None else ctx.ratio_max
    if new.ratio > ratio_cap:
        which = ctx.ratio_text or ("no-raise value (current ratio, 0x1C[7:0])" if ctx.ratio_max is None else "policy ratio_max")
        return False, f"ratio {new.ratio} above {ratio_cap} ({which}; baseline {base.ratio}, 0x1C[7:0] {favored_ratio})"
    v_cap = v_noraise if ctx.v_target_max_raw is None else ctx.v_target_max_raw
    if new.v_target_raw > v_cap:
        which = ctx.v_text or ("no-raise value (current / fused V target)" if ctx.v_target_max_raw is None else "policy v_target_max_mv")
        return False, f"v_target_raw {new.v_target_raw:#x} above {v_cap:#x} ({which}; baseline {base.v_target_raw:#x}, fused {fused_v})"
    if ctx.max_voltage_word is None:
        if new.v_target_raw > v_noraise:
            return False, f"v_target_raw {new.v_target_raw:#x} raises the V target but 0x28 (the hardware maximum) was not read this session"
    elif new.v_target_raw > hw_v_max_raw(ctx.max_voltage_word):
        return False, (
            f"v_target_raw {new.v_target_raw:#x} above the CPU limit {hw_v_max_raw(ctx.max_voltage_word):#x} "
            f"(cmd 0x28 word {ctx.max_voltage_word:#x}, XTU raw-1 rule)"
        )
    low = max(ctx.offset_floor_raw, params.get("offset_raw_min", -1024))
    high = min(ctx.offset_ceiling_raw, params.get("offset_raw_max", 1023))
    if not low <= new.offset_raw <= high:
        return False, (
            f"offset_raw {new.offset_raw} outside {low}..{high} (runtime floor {ctx.offset_floor_raw} after the assumed BIOS offset, D-015; "
            f"runtime ceiling {ctx.offset_ceiling_raw}; both are policy values, {ctx.policy_where}, within the field encoding -1024..1023; entry params {dict(params)})"
        )
    if base.ratio == 0 and ctx.top_fused_ratio is not None and 0 < new.ratio <= ctx.top_fused_ratio:
        # G-122: a cold word (ratio field 0) written at or below the top fused ratio has no OC point: its ratio field is not a ratio change. The reference is then
        # capped at the top fused ratio (E: 0x1C[7:0] reads 48, the E turbo, but the word without an OC point is 46), so 0 -> 46 / 57 is no step. A real raise past
        # the cap, or any ratio change above it, still measures from 0x1C[7:0] as before.
        ratio_now = min(ratio_now, ctx.top_fused_ratio)
    if abs(new.ratio - ratio_now) > ctx.max_ratio_step:
        return False, f"ratio step {new.ratio - ratio_now:+d} from {ratio_now} exceeds the per-operation limit {ctx.max_ratio_step}"
    if not no_oc_point and abs(new.v_target_raw - v_now) > ctx.max_v_step_raw:
        return False, f"V target step {new.v_target_raw - v_now:+d} raw from {v_now:#x} exceeds the per-operation limit {ctx.max_v_step_raw} raw"
    if abs(new.offset_raw - base.offset_raw) > ctx.max_offset_step_raw:
        return False, (
            f"offset step {new.offset_raw - base.offset_raw:+d} raw from {base.offset_raw} exceeds the per-operation limit "
            f"{ctx.max_offset_step_raw} raw"
        )
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
#: point index: ``0x10 P2=7`` -> rc ``0x11`` on E CPU 8, ``[MEASURED lnx 2026-10-02]``, docs/research/lnx-first-read.md).
POINT_P2_MAX = 15
#: T-028: domains whose V/F points a point-offset write may address: P1 0 = core, P1 2 = ring (package-level, addressed through any CPU;
#: ``0x10 P1=2 P2=1..8`` = ring points 8,14,20,28,34,37,38,39x, ``[MEASURED lnx 2026-10-02]``, docs/research/skatterbencher-285k.md). P1 1 / 7 stay refused.
POINT_P1_ALLOWED = frozenset({0, 2})


def _check_vf_point_offset_bounded(
    req: RequestLike, baseline: int, params: Mapping[str, Any], ctx: CheckContext | None = None
) -> tuple[bool, str]:
    """cmd 0x11 with ``P1`` 0 (core) or 2 (ring, T-028) and ``P2`` = a V/F point 1..15 (T-025): a **per-point offset** write, distinct from the whole-curve offset
    (``P2 = 0``, ``vf_bounded``). ``baseline`` is the ``0x10`` word of the **same CPU and the same P2** read this session (the guard keys its
    baselines by cpu/p1/p2, so another point's or the OC point's word can never stand in for it). Tests, fail closed:

    1. the request addresses P1 0 or 2 (the allow entry must list it) and P2 1..15 (P2 0 is the OC point and belongs to ``vf_bounded``'s ratio / V ceilings: refused here);
    2. the word equals the baseline in ratio ``[7:0]``, V target ``[19:8]`` and the mode bit ``[20]`` **exactly** -- only the offset field
       ``[31:21]`` may change (``[MEASURED lnx 2026-10-02]`` E CPU 8: point 6 reads ``0x0000002E`` = 46x, V target 0; OCCT wrote point words as
       ``ratio | offset << 21`` with V target 0, ``docs/knowledge/registers.md``). A baseline with ratio 0 or the static bit set is refused: it
       is not a known fused point, so there is nothing to keep;
    3. the offset window of the whole-curve offset: ``max(effective floor - assumed BIOS offset, offset_raw_min) <= offset_raw <=
       min(offset_ceiling, offset_raw_max)`` (D-015 / D-016, same policy numbers);
    4. ``|offset - baseline offset| <= max_offset_step_mv``.

    The hardware's V(n) >= V(n-1) rule across points is **not** checked here (the voltages of fused points are not readable)
    ``[COMMUNITY https://skatterbencher.com/2024/10/24/arrow-lake-e-core-overclocking/]``. A caller without a ``CheckContext`` is refused."""
    if ctx is None or ctx.offset_floor_raw is None:
        return False, "no check context (the CPU's runtime offset floor is unknown); the bounds cannot be evaluated"
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
    low = max(ctx.offset_floor_raw, params.get("offset_raw_min", -1024))
    high = min(ctx.offset_ceiling_raw, params.get("offset_raw_max", 1023))
    if not low <= new.offset_raw <= high:
        return False, (
            f"offset_raw {new.offset_raw} outside {low}..{high} (runtime floor {ctx.offset_floor_raw} after the assumed BIOS offset, D-015; "
            f"runtime ceiling {ctx.offset_ceiling_raw}; both are policy values, {ctx.policy_where}, within the field encoding -1024..1023; entry params {dict(params)})"
        )
    if abs(new.offset_raw - base.offset_raw) > ctx.max_offset_step_raw:
        return False, (
            f"offset step {new.offset_raw - base.offset_raw:+d} raw from {base.offset_raw} exceeds the per-operation limit "
            f"{ctx.max_offset_step_raw} raw"
        )
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
    is a pair of bounds, so neither side is restricted to the undervolt half (the Policy-level floor / ceiling are the primary bounds)."""

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


@dataclass(frozen=True)
class Policy:
    """The whole policy. ``max_operations_per_session`` counts every executed mailbox request (reads included, because
    every request is a WRMSR -- SAFETY.md "OC mailbox rule"); the post-write read-back does not count. The default of 1
    means *one* transaction until the human raises it (a read-then-write session needs at least 2).

    ``assumed_bios_offset_mv`` / ``effective_offset_floor_mv`` / ``e_cpus`` (T-021, D-015): the BIOS per-core V/F offset by core kind
    (``"P"``/``"E"``; the operator's stated value, not readable through the mailbox), the floor of *BIOS + runtime* offset, and which
    CPUs are E-cores (any other CPU counts as P -- the stricter floor). ``offset_floor_raw(cpu)`` is the resulting floor of the runtime
    offset field (``<= 0``): -100 mV for P and E by default (no BIOS offset is assumed). The runtime offset adds to the BIOS offset ``[MEASURED win 2026-10-01]``.

    ``max_operations_per_boot`` (T-016) is the same count taken over **every process** since the current boot, read from the
    audit log (``attempt`` records with the current ``boot_id``): the CLI is one process per command, so a per-session limit
    alone cannot stop back-to-back invocations -- the sweep-like traffic behind the 2026-08-26 incident (SAFETY.md
    "Mailbox-session discipline", D-011). ``min_dwell_s`` likewise also applies across processes (from the audit log).

    D-016 bounds of the ``0x11`` word (T-022; read by the ``vf_bounded`` check through ``build_context``): ``ratio_max`` / ``v_target_max_mv``
    map a core kind (``"P"`` / ``"E"``) to a ceiling or ``None``; **``None`` (the default) means the no-raise value**, so a policy that
    sets nothing cannot overclock -- an overclock only ever comes from a number in a reviewed policy. D-039: a set value is bounded only by the
    **field encodings** at load (``ENCODING_*``); the effective limit is the value clamped by the CPU's own (``0x01`` ratio, ``0x28`` V target, read live; unread
    = no raise). ``hw_ratio_max`` / ``hw_v_target_max_mv`` carry those live CPU limits when the helper bound them (``with_hw_limits``); ``source`` names the
    policy file (for messages). ``offset_ceiling_mv`` (default 0) is the highest *runtime*
    offset; ``max_ratio_step`` / ``max_v_step_mv`` / ``max_offset_step_mv`` limit one ``0x11`` word's change from the current state."""

    cpus: frozenset[int]
    max_operations_per_session: int = 1
    min_dwell_s: float = 60.0
    allow: tuple[AllowEntry, ...] = ()
    max_operations_per_boot: int = 12
    assumed_bios_offset_mv: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_ASSUMED_BIOS_OFFSET_MV))
    effective_offset_floor_mv: int = DEFAULT_EFFECTIVE_OFFSET_FLOOR_MV
    e_cpus: frozenset[int] | None = None  # None = not bound to a topology: core_kind() raises instead of assuming a particular layout
    ratio_max: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})
    v_target_max_mv: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})
    v_target_auto: frozenset[str] = frozenset()  # kinds whose V target ceiling is "auto": the CPU's own 0x28 maximum minus one LSB, read in the session
    ratio_auto: frozenset[str] = frozenset()  # kinds whose ratio ceiling is "auto": the fused value the CPU reports in 0x1C, read in the session (never a sysfs value)
    offset_ceiling_mv: int = DEFAULT_OFFSET_CEILING_MV
    max_ratio_step: int = DEFAULT_MAX_RATIO_STEP
    max_v_step_mv: int = DEFAULT_MAX_V_STEP_MV
    max_offset_step_mv: int = DEFAULT_MAX_OFFSET_STEP_MV
    msr_allow: tuple[MsrAllowEntry, ...] = ()  # T-023: plain MSR allowlist; empty (the default) = every plain MSR access is denied
    hw_ratio_max: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})  # D-039: the CPU's own ratio limit per kind (0x01 [7:0]) read by the helper; None = unread
    hw_v_target_max_mv: Mapping[str, int | None] = field(default_factory=lambda: {"P": None, "E": None})  # D-039: the CPU's own V target limit per kind (0x28 - 1 LSB), whole mV
    source: str | None = field(default=None, compare=False)  # the policy file this policy was loaded from (named in every refusal that quotes a policy limit)

    @property
    def where(self) -> str:
        """How refusals name the policy file."""
        return self.source or "the policy"

    def with_hw_limits(self, ratio_max_oc: Mapping[str, int | None] | None = None, v_target_max_mv: Mapping[str, int | None] | None = None) -> Policy:
        """The same policy bound to the CPU's own limits read live (D-039): ``0x01 [7:0]`` per kind and ``0x28 - 1 LSB`` in mV per kind. Kinds not given keep their value."""
        return replace(
            self,
            hw_ratio_max={**self.hw_ratio_max, **(ratio_max_oc or {})},
            hw_v_target_max_mv={**self.hw_v_target_max_mv, **(v_target_max_mv or {})},
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "cpus", _int_frozenset("cpus", self.cpus, 0, _CPU_MAX))
        object.__setattr__(
            self, "ratio_max", _kind_ceilings("ratio_max", self.ratio_max, {k: (1, ENCODING_RATIO_MAX) for k in (KIND_P, KIND_E)}, "ratio field [7:0]")
        )
        object.__setattr__(
            self,
            "v_target_max_mv",
            _kind_ceilings(
                "v_target_max_mv", self.v_target_max_mv, {k: (_V_TARGET_MIN_MV, ENCODING_V_TARGET_MAX_MV) for k in (KIND_P, KIND_E)}, "voltage field [19:8], S2.10, 12 bits"
            ),
        )
        object.__setattr__(self, "hw_ratio_max", _kind_ceilings("hw_ratio_max", self.hw_ratio_max, {k: (1, ENCODING_RATIO_MAX) for k in (KIND_P, KIND_E)}, "ratio field [7:0]"))
        object.__setattr__(
            self,
            "hw_v_target_max_mv",
            _kind_ceilings("hw_v_target_max_mv", self.hw_v_target_max_mv, {k: (1, ENCODING_V_TARGET_MAX_MV) for k in (KIND_P, KIND_E)}, "0x28 field [11:0]"),
        )
        auto = frozenset(self.v_target_auto)
        if not auto <= {KIND_P, KIND_E}:
            raise PolicyError(f"v_target_auto must be a subset of ['E', 'P'], got {sorted(auto)}")
        object.__setattr__(self, "v_target_auto", auto)
        auto_r = frozenset(self.ratio_auto)
        if not auto_r <= {KIND_P, KIND_E}:
            raise PolicyError(f"ratio_auto must be a subset of ['E', 'P'], got {sorted(auto_r)}")
        object.__setattr__(self, "ratio_auto", auto_r)
        _int_in("offset_ceiling_mv", self.offset_ceiling_mv, 0, ENCODING_OFFSET_CEILING_MV, "offset field [31:21] is 11-bit signed: XTU range -1000..+999 mV")
        _int_in("max_ratio_step", self.max_ratio_step, *_RATIO_STEP_RANGE, "ratio field [7:0]")
        _int_in("max_v_step_mv", self.max_v_step_mv, *_V_STEP_MV_RANGE, "V target field and the 2 mV minimum window")
        _int_in("max_offset_step_mv", self.max_offset_step_mv, *_OFFSET_STEP_MV_RANGE, "offset field range")
        bios = self.assumed_bios_offset_mv
        if not isinstance(bios, Mapping) or set(bios) != {"P", "E"}:
            raise PolicyError(f"assumed_bios_offset_mv must have exactly the keys 'P' and 'E', got {bios!r}")
        for kind, value in bios.items():
            if not _is_int(value) or not -1000 <= value <= 1000:
                raise PolicyError(f"assumed_bios_offset_mv[{kind!r}] must be an int in -1000..1000 mV, got {value!r}")
        object.__setattr__(self, "assumed_bios_offset_mv", MappingProxyType({k: int(v) for k, v in bios.items()}))
        if not _is_int(self.effective_offset_floor_mv) or not -1000 <= self.effective_offset_floor_mv <= 0:
            raise PolicyError(f"effective_offset_floor_mv must be an int in -1000..0 mV, got {self.effective_offset_floor_mv!r} (field encoding: XTU range -1000..+999 mV)")
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

    def cpu_allowed(self, cpu: object) -> bool:
        return _is_int(cpu) and cpu in self.cpus

    def with_topology(self, topology: Topology) -> Policy:
        """The same policy over a live topology (D-036): ``cpus`` and ``e_cpus`` are exactly that topology's CPUs and E CPUs. Hardware paths bind the topology
        they derived from sysfs; an unbound policy carries the simulated reference CPU's sets."""
        return replace(self, cpus=frozenset(c.cpu for c in topology.cores), e_cpus=frozenset(topology.cpus_of_kind(KIND_E)))

    def core_kind(self, cpu: int) -> str:
        """``"E"`` for a CPU listed in ``e_cpus``, else ``"P"`` (the kind with the stricter default floor). ``PolicyError`` for a policy not bound to a topology."""
        if self.e_cpus is None:
            raise PolicyError("this policy is not bound to a CPU topology (e_cpus unset): bind it with Policy.with_topology / load_policy(..., topology) before asking a CPU's kind")
        return "E" if cpu in self.e_cpus else "P"

    def offset_floor_raw(self, cpu: int) -> int:
        """D-015: the lowest runtime offset (raw, 1/1024 V) allowed on ``cpu`` so that BIOS + runtime stays at or above
        ``effective_offset_floor_mv``: ``effective_offset_floor_mv - assumed_bios_offset_mv[kind]`` in mV, converted with XTU's rounding
        (``(|mV| * 1024 + 512) // 1000``, sign kept) and clamped to ``<= 0`` (an offset of 0 is always within the floor; the matching upper
        bound is ``offset_ceiling_mv``, D-016). -100 mV -> -102 for P and E by default."""
        floor_mv = self.effective_offset_floor_mv - self.assumed_bios_offset_mv[self.core_kind(cpu)]
        if floor_mv >= 0:
            return 0
        return -((-floor_mv * 1024 + 512) // 1000)

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
    """Conservative milestone-1 policy. D-016 / T-022: safety is by **bounds**, and every ceiling is left at its no-raise default (``ratio_max``
    / ``v_target_max_mv`` unset, ``offset_ceiling_mv`` 0, steps 1 ratio / 20 mV / 25 mV), so this policy still refuses every raise and every
    positive offset while allowing undervolting offsets inside the D-015 window; ``arl-voltage-topology.md`` "Implications for milestone 1".

    * the CPUs of ``topology`` (the live one on hardware; without one, the simulated reference CPU); 1 operation per session; at most 12 operations per
      boot over all processes (``max_operations_per_boot``, T-016: counted from the audit log, reads included, read-back
      excluded; SAFETY.md "Mailbox-session discipline", D-011); 60 s dwell, also enforced across processes.
    * Reads (registers.md "Mailbox commands", P1/P2 columns): ``0x01`` p1 0..7 p2 0; ``0x02`` p1 0..7 p2 0; ``0x09`` p1 0x11
      (domain floors) with p2 = domain {0,2,7}; ``0x1C`` and ``0x14`` p1 0 p2 0; ``0x28`` p1 = domain {0,2,7} p2 0; ``0x10`` p1 {0,2,7} p2 0.
    * Writes: ``0x15`` p1 0 p2 0 with ``percore_enable_only``; ``0x11`` p1 0 p2 0 with ``vf_bounded`` (no entry params: the D-015
      runtime floor, -100 mV for both kinds, is the offset floor), and -- the XTU follow-ups of a per-core
      Apply, ``[MEASURED win 2026-10-01]`` -- ``0x1D`` p1 0 p2 0 with ``favored_ratio_follows_vf`` and ``0x27`` p1 0 p2 0 with
      ``granular_equals_read``; reads ``0x26`` p1 0 p2 0 (with ``0x1C``/``0x09``/``0x28`` above). Everything else, including every other
      write command, is denied.
    * T-023 plain MSRs: ``rdmsr`` of ``0x1AD 0x1AE 0x650 0x651 0xCE 0x194`` (``msr_allow``); **no plain write** (a turbo table write needs a
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
_TOP_KEYS = frozenset(
    {
        "cpus",
        "max_operations_per_session",
        "max_operations_per_boot",
        "min_dwell_s",
        "allow",
        "msr_allow",
        "e_cpus",
        "effective_offset_floor_mv",
        "assumed_bios_offset_mv",
        "ratio_max",
        "v_target_max_mv",
        "offset_ceiling_mv",
        "max_ratio_step",
        "max_v_step_mv",
        "max_offset_step_mv",
    }
)
_KIND_TABLE_KEYS = ("ratio_max", "v_target_max_mv")
_SCALAR_KEYS = ("offset_ceiling_mv", "max_ratio_step", "max_v_step_mv", "max_offset_step_mv")
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


def load_policy(source: str | os.PathLike[str], topology: Topology | None = None) -> Policy:
    """Load a policy from TOML text or a file path. Strict: unknown keys, unknown checks, bad types -> ``PolicyError``.

    ``topology`` (D-036): the live topology. ``cpus = "auto"`` / ``e_cpus = "auto"`` are derived from it and are a ``PolicyError`` without one (never a default); a
    policy without an ``e_cpus`` key takes the topology's E CPUs when one is given. ``ratio_max.<K> = "auto"`` and ``v_target_max_mv.<K> = "auto"`` need no topology
    and are not numbers at load time: they are CPU data of the session, resolved per request (``build_context``). Ratio: the highest fused byte ``0x1C[15:8]`` of
    that kind's CPUs read in the session plus 3; V target: the CPU's own ``0x28`` maximum minus one LSB; both clamped by the CPU's own limit (ratio: ``0x01 [7:0]``,
    D-039), and no raise when that word was not read. Numbers are bounded at load only by the field encodings (``ENCODING_*``); the CPU's limits apply per request. Never a sysfs value (``cpuinfo_max_freq`` follows OC writes: a ceiling from it would ratchet). Numeric values keep working unchanged."""
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
    if "effective_offset_floor_mv" in doc:
        kwargs["effective_offset_floor_mv"] = doc["effective_offset_floor_mv"]
    if "assumed_bios_offset_mv" in doc:
        table = doc["assumed_bios_offset_mv"]
        if not isinstance(table, dict) or not set(table) <= {"P", "E"}:
            raise PolicyError("assumed_bios_offset_mv must be a table with the keys 'P' and/or 'E'")
        kwargs["assumed_bios_offset_mv"] = {**DEFAULT_ASSUMED_BIOS_OFFSET_MV, **table}  # a missing kind keeps its default
    auto_v: set[str] = set()
    auto_r: set[str] = set()
    for key in _KIND_TABLE_KEYS:  # D-016 ceilings: a missing kind stays None = the no-raise value; values are range-checked by Policy
        if key in doc:
            table = doc[key]
            if not isinstance(table, dict) or not set(table) <= {"P", "E"}:
                raise PolicyError(f"{key} must be a table with the keys 'P' and/or 'E'")
            resolved = dict(table)
            for kind, value in table.items():
                if value == AUTO:
                    resolved[kind] = None
                    (auto_r if key == "ratio_max" else auto_v).add(kind)
            kwargs[key] = resolved
    if auto_v:
        kwargs["v_target_auto"] = frozenset(auto_v)
    if auto_r:
        kwargs["ratio_auto"] = frozenset(auto_r)
    for key in _SCALAR_KEYS:
        if key in doc:
            kwargs[key] = doc[key]
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
