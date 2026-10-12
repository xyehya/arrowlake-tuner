"""Results and text output of the controls: ``StepResult`` / ``ApplyReport`` and the plain-text renderers.

Pure formatting, no I/O, ASCII only (the CLI still makes its output streams tolerant of other characters). Facts:
``docs/knowledge/registers.md`` ("V/F data word": volts = raw/1024, offset LSB = 1/1024 V, so integer mV snaps to the nearest 1/1024 V;
"Return codes"); rules: ``docs/SAFETY.md`` ("Write-session protocol" step 5 "Revert path", "Linux specifics": every MSR write taints the
kernel). T-021 / D-015: offsets are shown as *runtime* and *effective* (assumed BIOS per-core offset + runtime; the runtime offset adds
to the BIOS one, ``[MEASURED win 2026-10-01]``).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from arltuner.guard import MsrRequest, Policy, Request
from arltuner.guard.policy import fused_top_ratio, hw_v_max_raw, turbo_bytes
from arltuner.mailbox import Cmd, VfWord
from arltuner.profiles import TurboTables

from .model import REVERT_NOTE, ApplyPlan, PlanStep, PointTarget, StepKind, Target
from .planner import count_operations, oc_cap_kinds

__all__ = [
    "ApplyReport",
    "StepResult",
    "StepStatus",
    "StopKind",
    "describe_word",
    "effective_offset_text",
    "format_plan",
    "format_preview",
    "format_revert_plan",
    "v_mv",
]


class StepStatus(str, Enum):
    OK = "ok"  # executed, rc 0 (and, for a write, read back equal)
    REFUSED = "refused"  # the guard refused; nothing was sent
    RC_ERROR = "rc_error"  # executed, non-zero completion code
    UNVERIFIED = "unverified"  # write accepted (rc 0) but the read-back differs (or is missing)
    ERROR = "error"  # the transport raised (MailboxTimeout, MsrError, ...)
    CANCELLED = "cancelled"  # G-112: the cancel flag was set before this step; nothing was sent for it


class StopKind(str, Enum):
    """Why an apply stopped. The CLI maps ``REFUSED`` to exit 2 and every other non-``NONE`` kind to exit 3."""

    NONE = "none"
    REFUSED = "refused"
    RC = "rc"
    UNVERIFIED = "unverified"
    TRANSPORT = "transport"
    TRIPPED = "tripped"  # the guard is tripped (refused TRIPPED, or tripped after the step: read-back / health gate)
    CANCELLED = "cancelled"  # G-112: the helper's cancel flag was set between two steps (the CLI never sets it)


def v_mv(word: VfWord) -> float:
    """Voltage target of ``word`` in mV (``raw * 1000 / 1024``)."""
    return word.v_target_raw * 1000 / 1024


def effective_offset_text(runtime_mv: float, kind: str | None, bios: Mapping[str, int]) -> str:
    """``runtime offset -5.86 mV, effective -55.86 mV (assumed BIOS offset -50 mV)``: the effective offset is the BIOS offset the
    policy assumes for the core kind plus the runtime offset (D-015). Without a known kind / BIOS value only the runtime offset."""
    if kind is None or kind not in bios:
        return f"runtime offset {runtime_mv:+.2f} mV"
    base = bios[kind]
    return f"runtime offset {runtime_mv:+.2f} mV, effective {base + runtime_mv:+.2f} mV (assumed BIOS offset {base:+d} mV)"


def describe_word(word: VfWord) -> str:
    mode = "static" if word.static else "adaptive"
    return (
        f"ratio {word.ratio}, V target {v_mv(word):.2f} mV (raw {word.v_target_raw:#05x}), {mode}, "
        f"offset {word.offset_mv:+.2f} mV (raw {word.offset_raw})"
    )


@dataclass(frozen=True)
class StepResult:
    """What happened to one step. For a V/F write, ``before`` is the baseline word and ``after`` the word read back (the
    *actually applied* word, so ``applied_v_mv`` / ``applied_offset_mv`` show the 1/1024 V snapping of integer mV); for a
    0x10 read, ``after`` is the word read. ``rc`` / ``rc_name`` are the mailbox completion code (``None`` if nothing ran)."""

    step: PlanStep
    status: StepStatus
    rc: int | None = None
    rc_name: str | None = None
    response_data: int | None = None
    readback_data: int | None = None
    before: VfWord | None = None
    after: VfWord | None = None
    applied_v_mv: float | None = None
    applied_offset_mv: float | None = None
    verified: bool | None = None
    detail: str = ""
    verify_readback_data: int | None = None  # T-023 WRITE_MSR: the read-back on the verify CPU (``readback_data`` is the one on the written CPU)


@dataclass(frozen=True)
class ApplyReport:
    """Result of ``apply_profile``. ``stopped_reason`` is ``None`` only when every step ran, rc 0, every write verified and
    the guard is not tripped. ``revert_plan`` is ``guard.revert_plan()``: inverse requests, **never executed** by this code."""

    profile_name: str
    plan: ApplyPlan | None
    steps: tuple[StepResult, ...]
    stop_kind: StopKind = StopKind.NONE
    stopped_reason: str | None = None
    revert_plan: tuple[Request | MsrRequest, ...] = ()
    warnings: tuple[str, ...] = ()
    tripped: bool = False
    assumed_bios_offset_mv: Mapping[str, int] = field(default_factory=dict)  # the policy's D-015 values, for the effective offsets

    @property
    def zero_baseline_cpus(self) -> tuple[int, ...]:
        """CPUs whose 0x10 baseline word read back with a zero ratio, or a zero V target next to an OC ratio (above the top fused ratio): no write restores such
        a word. G-124: a zero V target at or below the top fused ratio is the word the hardware keeps without an OC point (e.g. the cold ``0x39``) and is restorable."""

        fused = {r.step.cpu: r.response_data for r in self.steps if r.step.kind is StepKind.READ_FUSED and r.response_data}  # live 0x09 words of this session

        def unreachable(step: PlanStep, word: VfWord) -> bool:
            if word.ratio == 0:
                return True
            top = fused_top_ratio(fused.get(step.cpu))
            return word.v_target_raw == 0 and top is not None and word.ratio > top

        return tuple(r.step.cpu for r in self.steps if r.step.kind is StepKind.READ_VF and r.after is not None and unreachable(r.step, r.after))

    @property
    def ok(self) -> bool:
        return self.stopped_reason is None and self.stop_kind is StopKind.NONE

    @property
    def operations(self) -> int:
        """Mailbox operations that reached the transport (refused steps excluded, read-backs not counted; plain-MSR steps are not mailbox
        operations and are not counted here -- ``plain_msr_operations``)."""
        return sum(1 for r in self.steps if r.status not in (StepStatus.REFUSED, StepStatus.CANCELLED) and not r.step.kind.is_msr)

    @property
    def plain_msr_operations(self) -> int:
        """T-023: plain ``rdmsr`` / ``wrmsr`` steps of the turbo tables that reached the port (refused steps excluded)."""
        return sum(1 for r in self.steps if r.status not in (StepStatus.REFUSED, StepStatus.CANCELLED) and r.step.kind.is_msr)

    @property
    def msr_writes(self) -> int:
        """G-119: plain ``wrmsr`` steps (the turbo tables) that reached the port. The guard counts each as one operation; ``operations`` stays
        mailbox-only (many figures pin it), so the helper adds this to ``ops_used`` and reports it apart as ``msr_writes``."""
        return sum(1 for r in self.steps if r.step.kind is StepKind.WRITE_MSR and r.status not in (StepStatus.REFUSED, StepStatus.CANCELLED))

    def format_report(self) -> str:
        """Plain-text report: per-step results, applied values, stop reason, revert plan, warnings."""
        return _format_report(self)


# --------------------------------------------------------------------------------------------------------------
# Plan / preview
# --------------------------------------------------------------------------------------------------------------
_SHORT = {
    StepKind.READ_PERCORE_ENABLE: "read PerCore Voltage Enable state (package-wide)",
    StepKind.WRITE_PERCORE_ENABLE: "write PerCore Voltage Enable (old|0x8)",
}
_SHORT_OF_CORE = {
    StepKind.READ_VF: "read V/F baseline of",
    StepKind.READ_FAVORED: "read favored-core word of",
    StepKind.READ_FUSED: "read fused V/F point of",
    StepKind.READ_GRANULAR: "read granular-ratio word of",
    StepKind.READ_OC_CAP: "read the CPU's own ratio limit (0x01) of",
    StepKind.READ_VMAX: "read hardware maximum voltage (0x28) of",
    StepKind.WRITE_VF: "write V/F word of",
    StepKind.READ_VF_POINT: "read V/F point baseline of",
    StepKind.WRITE_VF_POINT: "write V/F point offset of",
    StepKind.WRITE_FAVORED: "write favored-core word of",
    StepKind.WRITE_GRANULAR: "write granular-ratio word of",
}


def _short(step: PlanStep) -> str:
    if step.kind is StepKind.READ_MSR:
        return f"read {step.label} (plain rdmsr, not a mailbox request)"
    if step.kind is StepKind.WRITE_MSR:
        return f"write {step.label} (plain wrmsr, read back on cpu {step.cpu} and cpu {step.verify_cpu})"
    if step.kind in _SHORT:
        return _SHORT[step.kind]
    return f"{_SHORT_OF_CORE[step.kind]} {step.label}"


def _table_text(word: int) -> str:
    return "/".join(str(b) for b in turbo_bytes(word))


def _step_summary(step: PlanStep, bios: Mapping[str, int] | None = None) -> str:
    if step.kind is StepKind.WRITE_MSR and step.data is not None and step.msr_before is not None:
        return (
            f"write {step.label}, buckets (1..8 active cores) {_table_text(step.msr_before)} -> {_table_text(step.data)}, "
            f"data {step.data:#018x}; read back on cpu {step.cpu} and on cpu {step.verify_cpu} (the other core kind)"
        )
    if step.kind is StepKind.WRITE_PERCORE_ENABLE:
        data = "(0x14 word) | 0x8" if step.data is None else f"{step.data:#010x}"
        cond = " (only if 0x14 bit 3 is clear)" if step.conditional else ""
        return f"enable PerCore Voltage Enable, data {data}{cond}"
    if step.kind is StepKind.WRITE_FAVORED and step.data is not None:
        return f"write favored-core word of {step.label}: data {step.data:#x} (= the ratio of its 0x11 word)"
    if step.kind is StepKind.WRITE_GRANULAR and step.data is not None:
        return f"write granular-ratio word of {step.label}: data {step.data:#x} (= the 0x26 word read)"
    before, after = step.before, step.after
    if step.kind is StepKind.WRITE_VF_POINT and before is not None and after is not None:
        text = (
            f"write V/F point {step.p2} offset of {step.label.split(' point ')[0]}: ratio {before.ratio} (kept), V target raw {before.v_target_raw:#x} (kept), "
            f"offset {before.offset_mv:+.2f} -> {after.offset_mv:+.2f} mV (raw {before.offset_raw} -> {after.offset_raw}), data {step.data:#010x}"
        )
        return f"{text}; {effective_offset_text(after.offset_mv, step.core_kind, bios or {})}"
    if step.kind is not StepKind.WRITE_VF or before is None or after is None:
        return _short(step)
    part = f" [{step.stage}]" if step.stage else ""
    text = (
        f"write V/F word of {step.label}{part}: ratio {before.ratio} -> {after.ratio}, "
        f"V target {v_mv(before):.2f} -> {v_mv(after):.2f} mV, offset {before.offset_mv:+.2f} -> {after.offset_mv:+.2f} mV"
        f" (raw {before.offset_raw} -> {after.offset_raw}), data {step.data:#010x}"
    )
    return f"{text}; {effective_offset_text(after.offset_mv, step.core_kind, bios or {})}"


def _verdict_text(step: PlanStep) -> str:
    v = step.verdict
    return "allowed" if v.allowed else f"REFUSED {v.reason}: {v.detail}"


def format_plan(plan: ApplyPlan) -> str:
    """The dry-run text: targets, every step with its predicted verdict, operation count vs the policy limit, estimated time."""
    lines = [f"Plan for profile {plan.profile_name!r} ({len({t.cpu for t in (*plan.targets, *plan.point_targets)})} CPU(s))"]
    for t in plan.targets:
        lines.append(f"  {t.label} (cpu {t.cpu}): {t.describe()}")
    for pt in plan.point_targets:
        lines.append(f"  {pt.label} (cpu {pt.cpu}): {pt.describe()}")
    lines.append("Steps (every request is a WRMSR to MSR 0x150, reads included; reads first, then writes):")
    if any(s.kind.is_msr for s in plan.steps):
        lines.append("  (a step with 'msr' is a plain rdmsr / wrmsr of a turbo ratio table, not a mailbox request: its reads are not WRMSRs, its writes are counted)")
    for s in plan.steps:
        what = f"msr {s.msr:#05x}" if s.msr is not None else f"cmd 0x{s.cmd:02X}"
        lines.append(f"  {s.number:>3}. cpu {s.cpu:>2} {what} {_step_summary(s, plan.assumed_bios_offset_mv)} -> {_verdict_text(s)}")
    over = "  ** NEEDS A HIGHER LIMIT **" if plan.needs_higher_limit else ""
    lines.append(
        f"Operations required: {plan.required_operations} mailbox WRMSRs = {plan.guard_operations} guard operations (every read and write; "
        f"plain-MSR reads are not counted, plain-MSR writes are) + {plan.read_back_requests} read-backs (one per write; the guard does not count them, "
        f"this budget does); "
        f"policy allows {plan.allowed_operations} per session{over}"
    )
    lines.append(
        f"Estimated minimum time: {plan.estimated_min_seconds:g} s (dwell {plan.min_dwell_s:g} s x {plan.guard_operations} guard operations)"
    )
    refusals = plan.refusals
    if plan.allowed:
        lines.append("Predicted verdict: ALLOWED (the guard stays authoritative)")
    else:
        why = []
        if plan.needs_ramp:
            why.append("needs a smaller step")
        if plan.needs_higher_limit:
            why.append("needs higher limit")
        if refusals:
            why.append(f"{len(refusals)} step(s) predicted refused")
        lines.append(f"Predicted verdict: NOT ALLOWED ({'; '.join(why)})")
    if plan.next_profile_text is not None:
        lines.append(
            "NEXT ALLOWED STEP: the request is larger than one operation may change (per-operation step limits of the policy). "
            "Save this as a profile and run it first; then read the result back, check stability and ask again:"
        )
        lines.extend("    " + line for line in plan.next_profile_text.rstrip("\n").split("\n"))
    for w in plan.warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)


def format_preview(
    profile_name: str,
    targets: Sequence[Target],
    policy: Policy,
    turbo: TurboTables | None = None,
    points: Sequence[PointTarget] = (),
) -> str:
    """What ``apply`` will do when the baseline is not known yet (real hardware: reads need the confirmation first). ``turbo`` (T-023): the
    profile's ``[turbo]`` tables. ``points`` (T-025): the profile's V/F point offset targets (one per CPU and point)."""
    n = len(targets)
    n_cpus = len({t.cpu for t in (*targets, *points)})
    tables = [] if turbo is None else [(k, getattr(turbo, k)) for k in ("e", "p") if getattr(turbo, k) is not None]
    worst = count_operations(tuple(targets), None, len(tables), len(points), len(oc_cap_kinds(policy, targets, [k.upper() for k, _ in tables])))
    lines = [f"Plan for profile {profile_name!r}: {n_cpus} CPU(s); baseline words are read through the guard after confirmation"]
    for t in targets:
        lines.append(f"  {t.label} (cpu {t.cpu}): {t.describe()}")
    for pt in points:
        lines.append(f"  {pt.label} (cpu {pt.cpu}): {pt.describe()}")
    if points:
        lines.append(
            "  V/F points: per CPU and point read 0x10 P2=<point>, then write 0x11 P2=<point> (the word read with only the offset replaced; no 0x1D / 0x27); "
            "point writes come before the per-core words; the hardware's V(n) >= V(n-1) rule is not checked here;"
        )
    for key, ratios in tables:
        lines.append(
            f"  plain MSR {'0x650 (E' if key == 'e' else '0x1AD (P'} turbo table): rdmsr 0xCE and the table first, then wrmsr {'/'.join(map(str, ratios))} "
            "(raising a bucket: after all per-core words; only lowering: before them), read back on two CPUs of different core kinds;"
        )
    if points or targets:
        lines.append(f"  read 0x14 on cpu {points[0].cpu if points else targets[0].cpu}; write 0x15 (old|0x8) only if bit 3 is clear;")
    if targets:
        lines.append(f"  read 0x10, 0x1C, 0x09 P1=0x11 and 0x26 on each of the {n} CPU(s) (and 0x28, the hardware maximum voltage, where an OC-point voltage is set);")
        lines.append("  then, per CPU, write 0x11 (the word XTU builds: ratio and V target filled in from 0x1C / 0x09, only the requested fields")
        lines.append("    changed; a ratio and V target moving the same way take two words - voltage first when rising, frequency first when falling),")
        lines.append("    0x1D (= its ratio) and 0x27 (= the 0x26 word); a change larger than the policy's step limits is refused with the next allowed step")
    bios = policy.assumed_bios_offset_mv
    lines.append(
        f"  effective offset = assumed BIOS offset (P {bios['P']:+d} mV, E {bios['E']:+d} mV) + runtime offset; "
        f"floor {policy.effective_offset_floor_mv} mV; runtime offset ceiling {policy.offset_ceiling_mv:+d} mV"
    )
    lines.append(
        f"Operations required: at most {worst} mailbox WRMSRs (every read, every write and the guard's read-back of each write); "
        f"policy allows {policy.max_operations_per_session}; dwell {policy.min_dwell_s:g} s after each operation"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------------
# Apply report
# --------------------------------------------------------------------------------------------------------------
def _read_word_text(kind: StepKind, data: int) -> str:
    if kind is StepKind.READ_VF_POINT:
        return f"word {data:#010x}: point {describe_word(VfWord.from_u32(data))}"
    if kind is StepKind.READ_FAVORED:
        return f"word {data:#06x}: favored ratio {data & 0xFF}, fused ratio {(data >> 8) & 0xFF}"
    if kind is StepKind.READ_FUSED:
        return f"word {data:#010x}: fused point {describe_word(VfWord.from_u32(data))}"
    if kind is StepKind.READ_OC_CAP:
        return f"word {data:#x}: CPU limit (fused max OC ratio) {data & 0xFF}x, ratio overclocking {'ok' if data & 0x100 else 'NOT ok'}"
    if kind is StepKind.READ_VMAX:
        return f"word {data:#x}: hardware maximum voltage raw {data & 0xFFF} -> V target limit raw {hw_v_max_raw(data)} (XTU raw-1 rule)"
    return f"word {data:#x}"


def _msr_word_text(msr: int | None, word: int | None) -> str:
    if word is None:
        return "no value"
    if msr == 0xCE:
        return f"word {word:#018x}: base ratio {(word >> 8) & 0xFF}"
    return f"word {word:#018x}: buckets (1..8 active cores) {_table_text(word)}"


def _result_line(r: StepResult) -> str:
    s = r.step
    head = f"{s.number:>3}. cpu {s.cpu:>2} " + (f"msr {s.msr:#05x}" if s.msr is not None else f"cmd 0x{s.cmd:02X}") + f" {_short(s)}"
    if r.status is StepStatus.REFUSED:
        return f"{head} -> REFUSED by the guard, nothing sent: {r.detail}"
    if r.status is StepStatus.ERROR:
        return f"{head} -> TRANSPORT ERROR: {r.detail}"
    if r.status is StepStatus.CANCELLED:
        return f"{head} -> CANCELLED before it was sent: {r.detail}"
    if s.kind is StepKind.READ_MSR:
        return f"{head} -> {_msr_word_text(s.msr, r.response_data)}"
    if s.kind is StepKind.WRITE_MSR:
        state = "read-back equal on both CPUs" if r.verified else "read-back DIFFERS" if r.verified is False else "read-back not done"
        extra = "" if r.readback_data is None else f", read-back cpu {s.cpu} {r.readback_data:#018x}"
        extra += "" if r.verify_readback_data is None else f", read-back cpu {s.verify_cpu} {r.verify_readback_data:#018x}"
        return f"{head} -> {state}{extra}"
    parts = [f"rc {r.rc} ({r.rc_name})"]
    if s.kind is StepKind.READ_PERCORE_ENABLE and r.response_data is not None:
        parts.append(f"word {r.response_data:#010x}, PerCore Voltage Enable {'set' if r.response_data & 0x8 else 'clear'}")
    elif s.kind is StepKind.READ_VF and r.after is not None:
        parts.append(describe_word(r.after))
    elif s.kind in (StepKind.READ_FAVORED, StepKind.READ_FUSED, StepKind.READ_GRANULAR, StepKind.READ_VMAX, StepKind.READ_VF_POINT, StepKind.READ_OC_CAP) and r.response_data is not None:
        parts.append(_read_word_text(s.kind, r.response_data))
    elif s.kind.is_write:
        parts.append(f"read-back {'equal' if r.verified else 'DIFFERS' if r.verified is False else 'not done'}")
        if r.readback_data is not None:
            parts.append(f"read-back word {r.readback_data:#010x}")
    return f"{head} -> " + ", ".join(parts)


_REVERT_MEANING = {
    int(Cmd.VOLTAGE_FREQ_WRITE): "restores the word in effect before the write (a zero ratio / V target is filled in as XTU does)",
    int(Cmd.FAVORED_CORE_WRITE): "favored-core word = the ratio of that CPU's revert 0x11 word",
    int(Cmd.GRANULAR_RATIO_WRITE): "granular-ratio word as read before",
}


def format_revert_plan(revert_plan: Sequence[Request | MsrRequest], zero_baseline_cpus: Sequence[int] = ()) -> list[str]:
    """Text lines for ``guard.revert_plan()`` output (per CPU, in the order to send them). Nothing here is ever executed.
    ``zero_baseline_cpus``: CPUs whose baseline word cannot be written back (named in the note, when known)."""
    lines = ["Revert plan (NOT executed; each is a new guarded request after the dwell, or reboot/power-cycle):"]
    for i, req in enumerate(revert_plan, 1):
        if isinstance(req, MsrRequest):
            lines.append(
                f"  {i}. cpu {req.cpu} plain wrmsr {req.msr:#x} value {req.value:#018x} (buckets {_table_text(req.value)}; the table as read before; "
                f"read back on cpu {req.verify_cpu} too)"
            )
            continue
        meaning = _REVERT_MEANING.get(req.cmd, "restores the baseline word")
        if req.cmd == int(Cmd.VOLTAGE_FREQ_WRITE) and req.p2 != 0:  # T-025
            meaning = f"restores the V/F point {req.p2} word as read (ratio and V target kept exactly; nothing filled in)"
        lines.append(f"  {i}. cpu {req.cpu} cmd 0x{req.cmd:02X} p1 {req.p1} p2 {req.p2} data {req.data:#010x} ({meaning})")
    lines.append(f"  Note: {REVERT_NOTE}.")
    if zero_baseline_cpus:
        cpus = ", ".join(str(c) for c in sorted(set(zero_baseline_cpus)))
        lines.append(f"  CPU(s) {cpus} read a baseline 0x10 word that no write restores (a zero ratio, or a zero V target next to an OC ratio): only a reboot restores it.")
    lines.append("  Volatility of these settings is unproven (SAFETY.md): if in doubt, reboot; a write taints the kernel (CPU_OUT_OF_SPEC).")
    return lines


def _format_report(rep: ApplyReport) -> str:
    lines = [f"Apply report for profile {rep.profile_name!r}"]
    lines.append(f"Result: {'OK' if rep.ok else 'STOPPED (' + rep.stop_kind.value + ')'}; {rep.operations} mailbox operation(s) reached the transport")
    if rep.stopped_reason is not None:
        lines.append(f"Stopped: {rep.stopped_reason}")
    lines.append("Steps:")
    for r in rep.steps:
        lines.append("  " + _result_line(r))
    written = [r for r in rep.steps if r.step.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT) and r.after is not None]
    if written:
        lines.append("Values as read back (integer mV snaps to 1/1024 V):")
        for r in written:
            assert r.after is not None and r.before is not None
            state = "verified" if r.verified else "NOT verified"
            lines.append(
                f"  {r.step.label} (cpu {r.step.cpu}): {describe_word(r.after)}; was: {describe_word(r.before)}; {state}; "
                f"{effective_offset_text(r.after.offset_mv, r.step.core_kind, rep.assumed_bios_offset_mv)}"
            )
    if rep.revert_plan:
        lines.extend(format_revert_plan(rep.revert_plan, rep.zero_baseline_cpus))
    if rep.tripped:
        lines.append("The guard is tripped: no further request will be sent in this session.")
    for w in rep.warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)
