"""Plan-side data model of the milestone-1 controls: targets, plan steps, predicted verdicts, pre-flight errors.

Pure data, no I/O. Facts: ``docs/knowledge/registers.md`` ("V/F data word", "XTU's write procedure", "Measured via XTU's level-4 log");
rules: ``docs/SAFETY.md`` ("Write-session protocol (T2)", "OC mailbox rule": every request, reads included, is a WRMSR); guard
semantics: ``docs/tasks/T-013-guard.md`` (every executed request counts toward the session limit, read-backs do not).

T-021: a per-core step list is XTU-shaped (``[MEASURED win 2026-10-01]``, ``docs/knowledge/registers.md`` "Measured via XTU's level-4 log on Windows" (S-04...S-07)): reads
``0x10``, ``0x1C``, ``0x09 P1=0x11``, ``0x26`` of the core, then the writes ``0x11`` (full word), ``0x1D`` (= its ratio) and ``0x27``
(= the ``0x26`` word).

T-022 / D-016: the ratio, adaptive V target and offset may be raised as well as lowered, bounded by the reviewed policy. A target that
asks for more than one step limit allows is answered with :class:`NeedsRamp` (the next allowed step as a profile); a ratio **and** V target
that move the same way take **two** ``0x11`` writes (voltage first when rising, frequency first when falling, registers.md "Ordering");
a profile with an OC-point voltage also reads ``0x28`` (the hardware maximum) of that CPU. Every write is read back by the guard: those
read-backs are WRMSRs too, and are counted in :attr:`ApplyPlan.required_operations`.

T-025 (per-V/F-point offsets): a profile's ``[[point]]`` entries add, per target CPU and point, one ``0x10 P2=point`` read and one ``0x11 P2=point``
write (the word of the read with only the offset field replaced; ``[MEASURED lnx 2026-10-02]`` point words are ``ratio | offset << 21``, V target 0).
No ``0x1C`` / ``0x09`` / ``0x26`` / ``0x28`` read and no ``0x1D`` / ``0x27`` write belongs to a point (those concern the OC point, ``P2 = 0``). Point
writes are ordered **before** the per-core (``[[core]]``) words (documented simplification: no OC-point / point interleaving by direction).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from arltuner.guard import MsrRequest, Request
from arltuner.mailbox import VfWord

if TYPE_CHECKING:  # pragma: no cover - typing only (report imports this module)
    from .report import ApplyReport

__all__ = [
    "READS_PER_CPU",
    "REVERT_NOTE",
    "SPLIT_EXTRA_WRITES",
    "VMAX_EXTRA_READS",
    "OFFSET_ONLY_WRITES",
    "WRITES_PER_CPU",
    "XTU_SHAPE_CAVEAT",
    "ApplyPlan",
    "ControlsError",
    "CoreReadings",
    "NeedsBootBudget",
    "NeedsHigherLimit",
    "NeedsRamp",
    "PlanError",
    "PlanRefused",
    "PlanStep",
    "PointTarget",
    "PredictedRefusal",
    "PreflightError",
    "StepKind",
    "Target",
    "Verdict",
]

#: Warning on every plan (docs/tasks/T-021; replaces the T-014 ratio caveat): the sequence reproduces XTU's Windows behaviour, but what
#: it does on Linux is not known. ASCII only.
XTU_SHAPE_CAVEAT = (
    "the 0x11 word and its 0x1D/0x27 follow-ups reproduce what XTU sent on Windows; OC words written this way have been read back as sent on Linux, "
    "but the semantics of 0x1D/0x27 stay UNVERIFIED. A V target of 0 is sent only on a word with no OC point "
    "(ratio at or below the core's top fused ratio): the hardware keeps no target next to an offset there. "
    "A zero ratio is never sent (its acceptance is UNVERIFIED)"
)

#: Line of every revert plan (docs/tasks/T-021, T-022, G-125): a reboot restores whatever the BIOS programmed ([MEASURED win 2026-10-01]: XTU persisted
#: nothing); a write puts back the word as read, which is exact except for an OC ratio above the top fused ratio.
REVERT_NOTE = (
    "a reboot restores the BIOS-programmed state; a write cannot clear an OC ratio above the top fused ratio (a zero word cannot be written; only a reboot clears it), "
    "while a word without one reverts exactly (G-124; the revert requests put back the previous XTU-shaped word, never one "
    "with a zero field, in the reverse of the order they were applied: after a raise the ratio first, then the voltage; a V/F point word is restored "
    "exactly as read)"
)

#: Mailbox requests per target CPU: reads 0x10, 0x1C, 0x09 P1=0x11, 0x26; writes 0x11, 0x1D, 0x27. Each write is also read back by the guard
#: (an extra WRMSR that the guard itself does not count, but the plan's budget does -- ``ApplyPlan.required_operations``).
READS_PER_CPU = 4
WRITES_PER_CPU = 3
#: G-122: a target that sets no OC ratio (offset / V target only) writes the 0x11 alone: a 0x1D / 0x27 would restate its ratio field, which is not a ratio the user set.
OFFSET_ONLY_WRITES = 1
#: A profile that sets an OC-point voltage also reads 0x28 (the hardware maximum voltage) of the CPU (D-016) ...
VMAX_EXTRA_READS = 1
#: ... and one that sets a ratio and an OC-point voltage may need a second 0x11 (voltage first when rising, frequency first when falling).
SPLIT_EXTRA_WRITES = 1

#: registers.md 0x14/0x15 data bit 3 = PerCore Voltage Enable (ARL client).
PERCORE_ENABLE_BIT = 0x8


class ControlsError(Exception):
    """Base class of every error raised by ``arltuner.controls``."""


class PlanError(ControlsError, ValueError):
    """A plan cannot be built (a requested value does not fit the V/F word, a baseline is missing, ...)."""


class PreflightError(ControlsError):
    """The plan was aborted by pre-flight, before any write. ``report`` is set when reads had already been executed."""

    report: ApplyReport | None = None


class NeedsRamp(PreflightError):
    """D-016: the profile asks for a bigger change than one operation may make (``Policy.max_ratio_step`` / ``max_v_step_mv`` /
    ``max_offset_step_mv``) but every absolute bound allows the final values. Nothing was sent. ``next_profile_text`` is the profile (TOML)
    of the next allowed step: save it, run it, read the result back, then ask again -- an overclock is reached by small, verified steps,
    each a separate human-attended session."""

    def __init__(self, next_profile_text: str) -> None:
        super().__init__(
            "the request is larger than one operation may change (per-operation step limits of the policy); nothing was sent. "
            "Next allowed step - save it as a profile and run that first:\n" + next_profile_text
        )
        self.next_profile_text = next_profile_text


class NeedsRounds(PreflightError):
    """G-148 (D-044, decision 5): a one-shot apply of a complete profile / the accepted state needs ``rounds`` guarded rounds (each: read, then write every word moved
    by at most one step limit, read back) because a word is further from its goal than the policy's per-operation step limit -- and the rounds do not fit
    (``ops_needed`` mailbox operations against ``ops_allowed``, or more than ``max_rounds``). ``worst`` names the unit and field that needs the most rounds.
    Raised before the first write: never a partial restore. Raise the step limit or the operation caps in the policy, or apply in separate sittings."""

    def __init__(self, rounds: int, worst: str, ops_needed: int, ops_allowed: int, max_rounds: int, where: str) -> None:
        why = (
            f"more than {max_rounds} rounds" if rounds > max_rounds else f"{ops_needed} mailbox operations (reads, writes, read-backs) but {ops_allowed} are allowed ({where})"
        )
        super().__init__(
            f"the change needs {rounds} guarded rounds within the per-operation step limits ({worst}), which is {why}; nothing was sent. "
            "Raise max_offset_step_mv / max_v_step_mv / max_ratio_step or the operation caps in the policy (/etc/arltuner/policy.toml), or bring the CPU closer first"
        )
        self.rounds = rounds
        self.worst = worst
        self.ops_needed = ops_needed
        self.ops_allowed = ops_allowed


class NeedsBootBudget(PreflightError):
    """The plan needs more mailbox operations than remain in this boot's budget (``policy.max_operations_per_boot`` minus the
    operations the audit log already records for this boot, from every process).

    Raised before anything is sent, so an ``apply`` can never stop half-way on ``BOOT_LIMIT`` with hardware partly changed
    (docs/SAFETY.md "Mailbox-session discipline", T-016). A reboot starts a new count; or the human raises the cap in a
    reviewed policy file. The requirement counts every WRMSR of the plan including the guard's read-backs (T-022).
    """

    def __init__(self, required: int, remaining: int, used: int, cap: int) -> None:
        super().__init__(
            f"plan needs {required} mailbox WRMSRs (reads, writes and the guard's read-backs) but only {remaining} remain in this boot's "
            f"budget ({used} already recorded, per-boot cap {cap}); reboot for a fresh count or raise max_operations_per_boot "
            "in a reviewed policy file (nothing was sent)"
        )
        self.required = required
        self.remaining = remaining
        self.used = used
        self.cap = cap


class NeedsHigherLimit(PreflightError):
    """The plan needs more mailbox operations than ``policy.max_operations_per_session`` allows.

    Every read and every write counts, and so does the guard's read-back of each write (every mailbox request is a WRMSR, SAFETY.md "OC
    mailbox rule"; T-022: the guard itself exempts read-backs from its counters, the plan's budget does not, so it is the stricter of the
    two). Raised before anything is sent.
    """

    def __init__(self, required: int, allowed: int) -> None:
        super().__init__(
            f"plan needs {required} mailbox WRMSRs (every read, every write and the guard's read-back of each write) but the policy "
            f"allows {allowed} per session; raise max_operations_per_session in a reviewed policy file (nothing was sent)"
        )
        self.required = required
        self.allowed = allowed


class PlanRefused(PreflightError):
    """Pre-flight predicts that the guard would refuse one or more planned requests; nothing was written."""

    def __init__(self, refusals: tuple[PredictedRefusal, ...], plan: ApplyPlan | None = None) -> None:
        listed = "; ".join(
            f"step {r.step_number} (cpu {r.cpu}, {'cmd ' + format(r.cmd, '#04x') if r.msr is None else 'msr ' + format(r.msr, '#x')}): {r.reason}: {r.detail}"
            for r in refusals
        )
        super().__init__(f"plan would be refused by the guard: {listed}")
        self.refusals = refusals
        self.plan = plan


class StepKind(str, Enum):
    """The kinds of step of the XTU write procedure (registers.md "XTU's write procedure"; ``[MEASURED win 2026-10-01]`` order)."""

    READ_PERCORE_ENABLE = "read_percore_enable"  # cmd 0x14, P1=0 P2=0
    READ_VF = "read_vf"  # cmd 0x10, P1=0 P2=0, baseline of one CPU
    READ_FAVORED = "read_favored"  # cmd 0x1C, P1=0 P2=0: [7:0] favored ratio, [15:8] fused
    READ_FUSED = "read_fused"  # cmd 0x09, P1=0x11 P2=0: the fused V/F point (ratio [7:0], voltage [19:8])
    READ_GRANULAR = "read_granular"  # cmd 0x26, P1=0 P2=0
    READ_VF_POINT = "read_vf_point"  # T-025: cmd 0x10, P1=0, P2=point (1..): baseline word of one V/F point of one CPU
    READ_MSR = "read_msr"  # T-023: plain rdmsr (0xCE, a turbo table); not a WRMSR, not counted in the budget
    READ_OC_CAP = "read_oc_cap"  # G-137 (D-039): cmd 0x01, P1=0 P2=0 on one CPU of a core kind: [7:0] fused max OC ratio, the CPU's own ratio limit
    READ_VMAX = "read_vmax"  # cmd 0x28, P1=0 P2=0: hardware maximum voltage (D-016; only when the profile sets an OC-point voltage)
    WRITE_PERCORE_ENABLE = "write_percore_enable"  # cmd 0x15 with old|8
    WRITE_VF = "write_vf"  # cmd 0x11, P1=0 P2=0, whole word
    WRITE_FAVORED = "write_favored"  # cmd 0x1D, data = the ratio of the 0x11 word
    WRITE_GRANULAR = "write_granular"  # cmd 0x27, data = the 0x26 word read
    WRITE_VF_POINT = "write_vf_point"  # T-025: cmd 0x11, P1=0, P2=point: the point's word with only the offset replaced; no 0x1D / 0x27
    WRITE_MSR = "write_msr"  # T-023: plain wrmsr of a turbo ratio table (0x650 E / 0x1AD P); counts as one operation, read back by rdmsr x2

    @property
    def is_write(self) -> bool:
        return self in (
            StepKind.WRITE_PERCORE_ENABLE,
            StepKind.WRITE_VF,
            StepKind.WRITE_VF_POINT,
            StepKind.WRITE_FAVORED,
            StepKind.WRITE_GRANULAR,
            StepKind.WRITE_MSR,
        )

    @property
    def is_msr(self) -> bool:
        """A plain-MSR step (T-023): goes through ``MsrRequest``; its read-back is an ``rdmsr``, not an extra WRMSR."""
        return self in (StepKind.READ_MSR, StepKind.WRITE_MSR)


@dataclass(frozen=True)
class Verdict:
    """Predicted guard verdict for one step. ``reason`` is ``"OK"`` or a ``arltuner.guard.Refusal`` value."""

    allowed: bool
    reason: str = "OK"
    detail: str = ""


OK_VERDICT = Verdict(True, "OK", "")


@dataclass(frozen=True)
class PredictedRefusal:
    """One step the guard is predicted to refuse (same predicate as the guard's: ``guard.CHECKS`` on the baseline)."""

    step_number: int
    cpu: int
    cmd: int
    reason: str
    detail: str
    msr: int | None = None  # T-023: set for a plain-MSR step (``cmd`` is 0 there)


@dataclass(frozen=True)
class CoreReadings:
    """The words read for one target CPU, the four needed to build XTU's word: ``vf`` (0x10 P1=0 P2=0, baseline; ``0x0`` on 23 of 24
    cores), ``favored`` (raw 0x1C), ``fused`` (raw 0x09 P1=0x11 P2=0: the fused V/F point) and ``granular`` (raw 0x26); plus ``vmax`` (raw
    0x28 P1=0 P2=0, the hardware maximum voltage; ``None`` when it was not read -- the guard then refuses any V target above the
    current one)."""

    vf: VfWord
    favored: int
    fused: int
    granular: int
    vmax: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.vf, VfWord):
            raise TypeError(f"vf must be a VfWord, got {type(self.vf).__name__}")
        for name in ("favored", "fused", "granular"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"{name} must be a u32, got {value!r}")
        if self.vmax is not None and (isinstance(self.vmax, bool) or not isinstance(self.vmax, int) or not 0 <= self.vmax <= 0xFFFFFFFF):
            raise ValueError(f"vmax must be a u32 or None, got {self.vmax!r}")


@dataclass(frozen=True)
class Target:
    """One CPU that will be written, with the profile values that apply to it (``None`` = leave alone)."""

    cpu: int
    label: str  # XTU name of this CPU's core, e.g. "P-Core 4" / "E-Core 12"
    kind: str
    index: int
    cluster: int | None
    ratio: int | None
    oc_voltage_mv: int | None
    offset_mv: int | None
    fused_top_ratio: int | None = None  # G-148: a complete profile's record of the unit's top fused ratio; the plan refuses the write when 0x09 reads another one

    def describe(self) -> str:
        parts = []
        if self.ratio is not None:
            parts.append(f"ratio {self.ratio}")
        if self.oc_voltage_mv is not None:
            parts.append(f"OC-point voltage {self.oc_voltage_mv} mV")
        if self.offset_mv is not None:
            parts.append(f"offset {self.offset_mv:+d} mV")
        return ", ".join(parts)


@dataclass(frozen=True)
class PointTarget:
    """One CPU whose V/F point ``point`` offset will be written (T-025). ``expect_ratio`` ``None`` = the point's ratio is not checked."""

    cpu: int
    label: str  # XTU name of this CPU's core, e.g. "E-Core 8"
    kind: str
    index: int
    cluster: int | None
    point: int
    offset_mv: int
    expect_ratio: int | None

    def describe(self) -> str:
        expect = "" if self.expect_ratio is None else f" (expects ratio {self.expect_ratio})"
        return f"point {self.point} offset {self.offset_mv:+d} mV{expect}"


@dataclass(frozen=True)
class PlanStep:
    """One mailbox request of the plan. ``data`` is ``None`` only for a 0x15 write whose word (old|0x8) is not known yet."""

    number: int  # 1-based, execution order
    kind: StepKind
    cpu: int
    cmd: int
    p1: int
    p2: int
    data: int | None
    purpose: str
    label: str  # "P-Core 4", "E-Core 12" or "package" (0x14/0x15 are package-wide)
    before: VfWord | None = None  # WRITE_VF / WRITE_VF_POINT: the baseline word read earlier
    after: VfWord | None = None  # WRITE_VF / WRITE_VF_POINT: the word that will be written
    conditional: bool = False  # 0x15 step that only happens if 0x14 bit 3 is clear (per-core state unknown at plan time)
    verdict: Verdict = field(default=OK_VERDICT)
    core_kind: str | None = None  # "P" / "E" of the target core (None for the package-wide 0x14/0x15)
    stage: str = ""  # WRITE_VF of a two-word change: "1/2 voltage first" / "1/2 frequency first" / "2/2 full word" (else "")
    msr: int | None = None  # T-023: READ_MSR / WRITE_MSR only (``cmd``/``p1``/``p2`` are 0 there); ``data`` is the u64 to write
    verify_cpu: int | None = None  # WRITE_MSR: the CPU of the other core kind the guard also reads back on
    msr_before: int | None = None  # WRITE_MSR: the table word read earlier (the baseline)

    def request(self) -> Request:
        """The guard ``Request`` for this (mailbox) step (``ValueError`` while ``data`` is still unknown, or for a plain-MSR step)."""
        if self.msr is not None:
            raise ValueError(f"step {self.number}: a plain-MSR step has no mailbox request (use guard_request)")
        if self.data is None:
            raise ValueError(f"step {self.number}: request data is not known yet")
        return Request(self.cpu, self.cmd, self.p1, self.p2, self.data, self.purpose)

    def guard_request(self) -> Request | MsrRequest:
        """The request to hand to ``WriteGuard.execute``: an ``MsrRequest`` for a plain-MSR step, else the mailbox ``Request``."""
        if self.msr is None:
            return self.request()
        if self.kind is StepKind.READ_MSR:
            return MsrRequest(self.cpu, self.msr, 0, self.purpose, "read")
        if self.data is None:
            raise ValueError(f"step {self.number}: request data is not known yet")
        return MsrRequest(self.cpu, self.msr, self.data, self.purpose, "write", self.verify_cpu)


@dataclass(frozen=True)
class ApplyPlan:
    """The ordered request sequence for one profile: all reads (0x14, then per target 0x10, 0x1C, 0x09 P1=0x11, 0x26 and -- with an
    OC-point voltage -- 0x28), then the writes (0x15 if needed, then per target 0x11 [twice when ratio and V target move the same way],
    0x1D, 0x27). ``steps`` includes every read and write; the guard's read-back of each write is not a plan step but is counted in
    ``required_operations``. ``assumed_bios_offset_mv`` / ``effective_offset_floor_mv`` (D-015) are the policy's, for display.
    ``next_profile_text`` (D-016) is set when a step limit stops the plan: the profile of the next allowed step, as TOML."""

    profile_name: str
    per_core_enabled: bool | None  # None = unknown (the 0x15 step is then conditional)
    targets: tuple[Target, ...]
    steps: tuple[PlanStep, ...]
    allowed_operations: int  # policy.max_operations_per_session
    min_dwell_s: float  # policy.min_dwell_s
    warnings: tuple[str, ...] = ()
    assumed_bios_offset_mv: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    effective_offset_floor_mv: int | None = None
    next_profile_text: str | None = None
    point_targets: tuple[PointTarget, ...] = ()  # T-025: the V/F point offsets of the profile (one per CPU and point)

    @property
    def guard_operations(self) -> int:
        """Operations the guard counts (every mailbox read and write, and every plain-MSR **write**: T-023 plain reads are not WRMSRs and
        are not counted); a dwell separates them."""
        return sum(1 for s in self.steps if s.kind is not StepKind.READ_MSR)

    @property
    def read_back_requests(self) -> int:
        """The guard reads every successful mailbox write back through the same transport: one extra WRMSR per write step. (A plain-MSR
        write is read back with ``rdmsr`` on two CPUs: not a WRMSR, not counted.)"""
        return sum(1 for s in self.steps if s.kind.is_write and not s.kind.is_msr)

    @property
    def required_operations(self) -> int:
        """Every mailbox WRMSR the plan causes: reads, writes **and the guard's read-backs** (T-022). The guard's own counters exempt
        the read-backs, so this budget is the stricter of the two; it is what is compared with the session / per-boot limits."""
        return self.guard_operations + self.read_back_requests

    @property
    def estimated_min_seconds(self) -> float:
        """``dwell x operations``: the guard wants one dwell after every operation before the next one (and SAFETY.md asks for
        a health check after the last). Read-backs follow their write without a dwell."""
        return self.min_dwell_s * self.guard_operations

    @property
    def needs_higher_limit(self) -> bool:
        return self.required_operations > self.allowed_operations

    @property
    def needs_ramp(self) -> bool:
        return self.next_profile_text is not None

    @property
    def refusals(self) -> tuple[PredictedRefusal, ...]:
        return tuple(
            PredictedRefusal(s.number, s.cpu, s.cmd, s.verdict.reason, s.verdict.detail)
            for s in self.steps
            if not s.verdict.allowed
        )

    @property
    def allowed(self) -> bool:
        """True when the whole plan fits the session limit, needs no ramp and no step is predicted to be refused."""
        return not self.needs_higher_limit and not self.needs_ramp and not self.refusals

    def require_allowed(self) -> None:
        """Raise ``NeedsHigherLimit`` (first), ``NeedsRamp`` (a step limit stops the plan; the answer is the next step) or ``PlanRefused`` if
        the plan is not allowed; otherwise return."""
        if self.needs_higher_limit:
            raise NeedsHigherLimit(self.required_operations, self.allowed_operations)
        if self.next_profile_text is not None:
            raise NeedsRamp(self.next_profile_text)
        refusals = self.refusals
        if refusals:
            raise PlanRefused(refusals, self)
