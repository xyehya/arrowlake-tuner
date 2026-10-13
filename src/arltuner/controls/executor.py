"""Executor: run a profile through ``WriteGuard`` - reads first, pre-flight on the real baselines, then the writes in order.

Every mailbox request, reads included, is sent with ``WriteGuard.execute`` (``docs/SAFETY.md`` "OC mailbox rule": a mailbox
"read" is a WRMSR); this module never touches a transport or a port. It stops at the **first** guard refusal, non-zero
completion code, unverified read-back, transport exception or tripped guard: no retry, no auto-revert (SAFETY.md "Nothing in the
app is allowed to loop-retry a failing write"). The revert plan is reported, never executed.

G-148 (D-044, decision 5): with ``auto_ramp`` (a complete profile, the restore of the accepted state) a change larger than the policy's per-operation step
limits is split into **rounds** inside the one run, reusing the D-016 ramp (``ApplyPlan.next_profile_text``): read, write the next allowed step of every word,
read again (the guard measures each step from the word read in the session), ... until the plan of the profile itself is allowed. How many rounds the words
read need (``planner.ramp_rounds``) and what they cost is judged **before the first write** (``NeedsRounds``: nothing is sent), never half-way.

T-023: a profile's ``[turbo]`` tables add plain-MSR steps (``MsrRequest`` through the same guard): ``rdmsr`` of ``0xCE`` and the tables first (no dwell:
they are not WRMSRs), then the plain ``wrmsr`` of a table that only lowers buckets before the per-core words, one that raises a bucket after them.

Dwell (``docs/tasks/T-013-guard.md``: the guard refuses, never sleeps): this executor waits, through an injectable ``sleep``, until
``policy.min_dwell_s`` has elapsed since the previous operation before it sends the next one.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from arltuner.guard import Confirmation, GuardRefused, MsrResult, Policy, Refusal, WriteGuard
from arltuner.mailbox import VfWord, rc_name
from arltuner.profiles import Profile, ProfileError, load_profile
from arltuner.topology import Topology

from . import residual
from .model import PERCORE_ENABLE_BIT, ApplyPlan, CoreReadings, PlanError, PlanStep, PreflightError, StepKind
from .planner import _turbo_tables, build_plan, check_rounds, count_operations, oc_cap_reads_of, plan_warnings, preflight, ramp_rounds, read_steps, resolve_point_targets
from .report import ApplyReport, StepResult, StepStatus, StopKind, v_mv

__all__ = ["apply_profile"]

#: Added to every dwell wait so a sleep that returns marginally early cannot make the guard refuse DWELL half-way through.
_DWELL_MARGIN_S = 0.05


class _Pacer:
    """Waits (via ``sleep``) until the dwell since the previous executed operation has passed. Never loops."""

    def __init__(self, dwell_s: float, clock: Callable[[], float], sleep: Callable[[float], object]) -> None:
        self._dwell = dwell_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self, progress: Callable[[str], None] | None) -> None:
        if self._last is None or self._dwell <= 0:
            return
        remaining = self._dwell - (self._clock() - self._last)
        if remaining > 0:
            if progress is not None:
                progress(f"dwell: waiting {remaining:.1f} s before the next operation")
            self._sleep(remaining + _DWELL_MARGIN_S)

    def mark(self) -> None:
        self._last = self._clock()


class _Stop(Exception):
    """Internal: a step ended the run. Carries the step result and why."""

    def __init__(self, result: StepResult, kind: StopKind, reason: str) -> None:
        super().__init__(reason)
        self.result = result
        self.kind = kind
        self.reason = reason


def _check_cancel(cancelled: Callable[[], bool] | None, step: PlanStep) -> None:
    """G-112: the helper's cancel flag, looked at between two transactions only (never inside one). The CLI passes none."""
    if cancelled is not None and cancelled():
        detail = "the cancel flag was set; this step and the ones after it were not sent"
        raise _Stop(StepResult(step, StepStatus.CANCELLED, detail=detail), StopKind.CANCELLED, f"step {step.number}: cancelled between transactions; nothing was sent for it")


def _detail_of_exception(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    write_sent = getattr(exc, "write_sent", None)
    if write_sent is not None:  # MailboxTimeout: say whether the request reached the mailbox
        text += f" (phase={getattr(exc, 'phase', None)}, write_sent={write_sent})"
    return text


def _run_step(
    guard: WriteGuard,
    confirm: Confirmation,
    step: PlanStep,
    pacer: _Pacer,
    progress: Callable[[str], None] | None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[StepResult, int]:
    """Send one step through the guard. Returns ``(result, response word)``; raises ``_Stop`` if the run must end here."""
    req = step.guard_request()
    is_msr_read = step.kind is StepKind.READ_MSR  # a plain read is not a WRMSR: no dwell before it, none started by it
    _check_cancel(cancelled, step)
    if not is_msr_read:
        pacer.wait(progress)
        _check_cancel(cancelled, step)  # a cancel that cut the dwell wait short must not send the next request before the dwell has passed
    if progress is not None:
        what = f"msr {step.msr:#x}" if step.msr is not None else f"cmd {step.cmd:#04x}"
        progress(f"step {step.number}: cpu {step.cpu} {what} {step.purpose}")
    try:
        res = guard.execute(req, confirm)
    except GuardRefused as exc:
        kind = StopKind.TRIPPED if exc.reason is Refusal.TRIPPED else StopKind.REFUSED
        result = StepResult(step, StepStatus.REFUSED, detail=f"{exc.reason.value}: {exc.detail}")
        raise _Stop(result, kind, f"step {step.number}: the guard refused ({exc.reason.value}): {exc.detail}") from None
    except Exception as exc:  # the guard has tripped and re-raised; MailboxTimeout, MsrError, ...
        if not is_msr_read:
            pacer.mark()
        detail = _detail_of_exception(exc)
        result = StepResult(step, StepStatus.ERROR, detail=detail)
        raise _Stop(result, StopKind.TRANSPORT, f"step {step.number}: transport error: {detail}") from None
    if not is_msr_read:
        pacer.mark()
    if isinstance(res, MsrResult):
        return _msr_result(step, res, guard)
    resp = res.response
    name = rc_name(resp.rc)
    before: VfWord | None = None
    after: VfWord | None = None
    if step.kind in (StepKind.READ_VF, StepKind.READ_VF_POINT):
        after = VfWord.from_u32(resp.data)
    elif step.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT):
        before = step.before
        if res.readback is not None:
            after = VfWord.from_u32(res.readback.data)
    result = StepResult(
        step,
        StepStatus.OK,
        rc=resp.rc,
        rc_name=name,
        response_data=resp.data,
        readback_data=None if res.readback is None else res.readback.data,
        before=before,
        after=after,
        applied_v_mv=None if after is None or step.kind not in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT) else v_mv(after),
        applied_offset_mv=None if after is None or step.kind not in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT) else after.offset_mv,
        verified=res.verified,
    )
    if resp.rc != 0:
        result = _with_status(result, StepStatus.RC_ERROR, f"completion code {resp.rc} ({name})")
        raise _Stop(result, StopKind.RC, f"step {step.number}: mailbox completion code {resp.rc} ({name}) on cmd {step.cmd:#04x}, cpu {step.cpu}")
    if step.kind.is_write and res.verified is not True:
        result = _with_status(result, StepStatus.UNVERIFIED, "read-back does not equal the word written")
        rb = "missing" if res.readback is None else f"{res.readback.data:#010x} (rc {res.readback.rc})"
        raise _Stop(result, StopKind.UNVERIFIED, f"step {step.number}: read-back of cmd {step.cmd:#04x} on cpu {step.cpu} is {rb}, wrote {step.data:#010x}; the guard is tripped")
    if guard.tripped:
        raise _Stop(result, StopKind.TRIPPED, f"step {step.number}: the guard tripped after this step (e.g. health gate 'after'); stopping")
    return result, resp.data


def _msr_result(step: PlanStep, res: MsrResult, guard: WriteGuard) -> tuple[StepResult, int]:
    """The result of a plain-MSR step. A read returns the word; a write must be verified on both CPUs (the guard tripped otherwise)."""
    if step.kind is StepKind.READ_MSR:
        return StepResult(step, StepStatus.OK, response_data=res.value), res.value
    result = StepResult(
        step, StepStatus.OK, readback_data=res.value, verify_readback_data=res.verify_value, response_data=res.value, verified=res.verified
    )
    if res.verified is not True:
        result = _with_status(result, StepStatus.UNVERIFIED, "read-back does not equal the table written")
        raise _Stop(
            result,
            StopKind.UNVERIFIED,
            f"step {step.number}: read-back of msr {step.msr:#x} is {res.value:#018x} on cpu {step.cpu} and "
            f"{'missing' if res.verify_value is None else format(res.verify_value, '#018x')} on cpu {step.verify_cpu}, wrote {step.data:#018x}; "
            "the guard is tripped",
        )
    if guard.tripped:
        raise _Stop(result, StopKind.TRIPPED, f"step {step.number}: the guard tripped after this step (e.g. health gate 'after'); stopping")
    return result, res.value


def _with_status(result: StepResult, status: StepStatus, detail: str) -> StepResult:
    return replace(result, status=status, detail=detail)


def _report(
    profile: Profile,
    guard: WriteGuard,
    results: list[StepResult],
    plan: ApplyPlan | None,
    warnings: tuple[str, ...],
    stop: _Stop | None,
    policy: Policy,
    residual_lines: tuple[str, ...] = (),
) -> ApplyReport:
    if stop is not None:
        results = [*results, stop.result]
    return ApplyReport(
        profile_name=profile.name,
        plan=plan,
        steps=tuple(results),
        stop_kind=StopKind.NONE if stop is None else stop.kind,
        stopped_reason=None if stop is None else stop.reason,
        revert_plan=tuple(guard.revert_plan()),
        warnings=warnings,
        tripped=guard.tripped,
        assumed_bios_offset_mv=policy.assumed_bios_offset_mv,
        residual=residual_lines,
    )


def _demote(results: list[StepResult], first: int) -> int:
    """G-148: the results of an earlier round get negative step numbers (below ``first``), so a report keyed by step number (``tune.apply`` rows, ``zero_baseline_cpus``)
    only ever matches the steps of the last plan. Returns the next free negative number."""
    n = first
    for i, r in enumerate(results):
        if r.step.number > 0:
            n -= 1
            results[i] = replace(r, step=replace(r.step, number=n))
    return n


def apply_profile(
    profile: Profile,
    *,
    guard: WriteGuard,
    topology: Topology,
    confirm: Confirmation,
    policy: Policy,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] | None = None,
    plan_check: Callable[[ApplyPlan], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    neutralize_residual: bool = False,
    boot_ops_used: int = 0,
    auto_ramp: bool = False,
) -> ApplyReport:
    """Apply ``profile`` through ``guard`` and return what happened.

    T-025: a profile's ``[[point]]`` entries add ``0x10 P2=point`` reads (after ``0x14``, before the per-core reads) and, **before the per-core words**,
    one ``0x11 P2=point`` write per CPU and point (the word read with only its offset replaced; no ``0x1D`` / ``0x27``); the guard reads each back.

    1. **Pre-flight, nothing sent:** worst-case WRMSR count (reads, writes and the guard's read-backs) vs
       ``policy.max_operations_per_session`` (``NeedsHigherLimit``) and the per-boot budget (``NeedsBootBudget``), CPU set / allowlist of
       every read and write, and every profile value against the policy's explicit ceilings and offset window (``PlanRefused``).
    2. **Reads through the guard:** first (T-023) the plain ``rdmsr`` of ``0xCE`` and each ``[turbo]`` table (or of ``0x650``, if the policy allows it, when
       an E ratio is set without a ``[turbo] e`` table: the capped-raise warning), then ``0x14`` on the first target CPU, then ``0x10``, ``0x1C``, ``0x09 P1=0x11`` and ``0x26`` on every
       target CPU (and ``0x28``, the hardware maximum voltage, where the profile sets an OC-point voltage).
    3. **Plan from the actual reads**, then pre-flight again with the real words: the guard's own predicates
       (``guard.CHECKS``, same session facts) decide every write; a step too big for the policy's per-operation limits raises
       ``NeedsRamp`` (the next allowed step as a profile) -- or, with ``auto_ramp``, plans the rounds (module docstring; ``NeedsRounds`` when they do not fit) --,
       any other predicted refusal ``PlanRefused`` (both with the partial report attached as ``.report``) and **no write is sent**.
    4. **Writes in order:** ``0x15`` (``old|0x8``) if PerCore Voltage Enable is clear, then (T-023) a ``[turbo]`` table that only lowers buckets (plain
       ``wrmsr``, read back on two CPUs), then per target CPU ``0x11`` (XTU-shaped word; two
       words when ratio and V target move the same way: voltage first when rising, frequency first when falling), ``0x1D`` (= its ratio)
       and ``0x27`` (= the ``0x26`` word), then a ``[turbo]`` table that raises a bucket (so the frequency only rises once the voltage is in
       place); the guard reads every write back. Stops at the first refusal,
       non-zero rc, ``verified is False``, transport exception or tripped guard.

    ``policy`` must be the policy the guard was built with (the guard is authoritative; this copy only drives prediction and
    pacing). ``sleep`` / ``clock`` should be the ones the guard uses, so dwell waits are seen by the guard. ``progress`` receives
    one line per step just before it is sent.

    G-112 (helper ``tune.apply``): ``plan_check(plan)`` is called with the plan built from the actual reads, after ``require_allowed`` and before the first
    write; whatever it raises propagates and nothing was written (the helper uses it to refuse a plan whose assumed words changed). ``cancelled()`` is
    looked at before every step (both phases); once true the run stops with ``StopKind.CANCELLED`` -- a step in flight always completes. Both default to
    ``None`` (the CLI's behaviour is unchanged).

    G-147 (``neutralize_residual``, the restore of the accepted state): first, before any other request, ``0x10`` and ``0x09`` are read on one CPU of every unit ``profile``
    (the one accepted profile, D-044) sets no OC ratio for. A unit that holds an OC ratio above its top fused ratio (a residual OC point: it cannot be cleared at runtime)
    gets a row that keeps that ratio, the accepted offset and a V target below the accepted top fused point, so the hardware clamps it to the accepted voltage
    (``controls.residual``); no ``0x1D`` / ``0x27`` is sent for it. The scan reads are budgeted up front (``preflight(extra_ops=...)``, against ``boot_ops_used`` = the
    operations this boot has used so far). If the plan with those rows is refused by the guard's predicates (a step limit, the policy's ratio ceiling) they are dropped and
    the plan of ``profile`` alone runs, with a warning: a restore never fails because of the neutralisation.

    G-148: a ``profile.complete`` profile writes only the words that differ from the words read (``build_plan(only_changed=True)``).
    """
    only_changed = profile.complete
    scan = residual.scan_plan(profile, topology, policy) if neutralize_residual else ()
    scan_ops = len(scan)
    targets = preflight(profile, topology, policy, boot_ops_used=boot_ops_used, extra_ops=scan_ops)
    warnings = plan_warnings(targets, policy, resolve_point_targets(profile, topology))
    original = profile
    residual_cpus: frozenset[int] = frozenset()
    residual_lines: tuple[str, ...] = ()
    pacer = _Pacer(policy.min_dwell_s, clock, sleep)
    results: list[StepResult] = []

    try:
        if scan:
            scan_words: dict[int, dict[StepKind, int]] = {}
            for step in scan:
                result, word = _run_step(guard, confirm, step, pacer, progress, cancelled)
                results.append(result)
                scan_words.setdefault(step.cpu, {})[step.kind] = word
            found = residual.neutralise(
                profile, topology, policy,
                {c: w[StepKind.READ_VF] for c, w in scan_words.items()}, {c: w[StepKind.READ_FUSED] for c, w in scan_words.items()},
            )
            warnings = (*warnings, *found.notes)
            if found.units:
                try:
                    targets = preflight(found.profile, topology, policy, boot_ops_used=boot_ops_used, extra_ops=scan_ops)
                except PreflightError as exc:  # the extra rows do not fit (budget, ceilings): the plan of the profile alone runs
                    warnings = (*warnings, f"residual OC point not neutralised: {exc}")
                else:
                    profile, residual_cpus = found.profile, found.cpus
                    residual_lines = tuple(u.describe() for u in found.units)
                    warnings = (*plan_warnings(targets, policy, resolve_point_targets(profile, topology)), *found.notes)
        readings = _read_all(guard, confirm, targets, policy, profile, topology, pacer, progress, cancelled, results)
    except _Stop as stop:
        return _report(profile, guard, results, None, warnings, stop, policy, residual_lines)

    def plan_of(prof: Profile, rd: _Readings, residual_rows: frozenset[int]) -> ApplyPlan:
        return build_plan(
            prof, topology, rd.baseline,
            per_core_enabled=None if rd.misc_word is None else bool(rd.misc_word & PERCORE_ENABLE_BIT),
            policy=policy, misc_word=rd.misc_word, msr_words=rd.msr_words, point_words=rd.point_words,
            favored_words=getattr(guard, "favored_words", lambda: None)(), oc_cap_words=rd.oc_cap_words,
            residual_cpus=residual_rows, only_changed=only_changed,
        )

    plan = plan_of(profile, readings, residual_cpus)
    rounds = 1
    if not (auto_ramp and plan.next_profile_text is not None):
        try:
            plan.require_allowed()
        except PreflightError as exc:
            if profile is original:
                exc.report = _report(profile, guard, results, plan, warnings, None, policy, residual_lines)
                raise
            # G-147: the neutralisation rows are refused (e.g. a step limit); the plan of the profile alone is the restore
            warnings = (*warnings, f"residual OC point not neutralised: the plan with those rows was refused ({exc}); applying the profile alone")
            profile, residual_cpus, residual_lines = original, frozenset(), ()
            plan = plan_of(profile, readings, residual_cpus)
            if not (auto_ramp and plan.next_profile_text is not None):
                try:
                    plan.require_allowed()
                except PreflightError as exc2:
                    exc2.report = _report(profile, guard, results, plan, warnings, None, policy, residual_lines)
                    raise
    if auto_ramp and plan.next_profile_text is not None:  # G-148: rounds within the step limits, judged here, before the first write
        rounds, worst = ramp_rounds(profile, topology, readings.baseline, readings.point_words, policy, only_changed=only_changed)
        rounds = max(rounds, 2)  # the plan needs a step: at least one ramp round before the plan itself
        tables = len(_turbo_tables(profile))
        per_round = count_operations(targets, None, tables, len(resolve_point_targets(profile, topology)), oc_cap_reads_of(policy, targets, profile))
        try:
            check_rounds(rounds, worst, per_round, scan_ops, policy, boot_ops_used)
        except PreflightError as exc:
            exc.report = _report(profile, guard, results, plan, warnings, None, policy, residual_lines)
            raise
        warnings = (*warnings, f"the change is larger than the per-operation step limits ({worst}): it is applied in up to {rounds} guarded rounds in this run (G-148)")
    checked = False
    neg = min([0, *(r.step.number for r in results)])
    for round_no in range(1, rounds + 1):
        if plan.next_profile_text is None or round_no == rounds:
            try:
                plan.require_allowed()
            except PreflightError as exc:
                if round_no == 1:
                    exc.report = _report(profile, guard, results, plan, warnings, None, policy, residual_lines)
                    raise
                # cannot happen when ramp_rounds counted right; the earlier rounds did write: report it as a stop, never silently
                reason = f"round {round_no} of {rounds}: the profile's own plan is still not allowed after the ramp rounds ({exc}); the CPU holds an intermediate state"
                bad = next((s for s in plan.steps if not s.verdict.allowed), plan.steps[-1] if plan.steps else None)
                stop = _Stop(StepResult(bad, StepStatus.REFUSED, detail=f"{bad.verdict.reason if bad else 'REFUSED'}: {reason}"), StopKind.REFUSED, reason) if bad else None
                return _report(profile, guard, results, plan, warnings, stop, policy, residual_lines)
            step_plan = plan
        else:
            try:
                stage = load_profile(plan.next_profile_text)
            except ProfileError as exc:
                raise PlanError(f"the next ramp step is not a valid profile: {exc}") from exc
            step_plan = plan_of(stage, readings, residual_cpus)
            try:
                step_plan.require_allowed()
            except PreflightError as exc:
                if round_no == 1:
                    exc.report = _report(profile, guard, results, step_plan, warnings, None, policy, residual_lines)
                    raise
                reason = f"round {round_no} of {rounds}: the ramp step is not allowed ({exc}); the CPU holds an intermediate state"
                stop = _Stop(StepResult(step_plan.steps[-1], StepStatus.REFUSED, detail=f"REFUSED: {reason}"), StopKind.REFUSED, reason)
                return _report(profile, guard, results, step_plan, warnings, stop, policy, residual_lines)
        if not checked:
            checked = True
            if plan_check is not None:
                plan_check(step_plan)
        try:
            if progress is not None and rounds > 1:
                progress(f"round {round_no} of {rounds}: {'the profile' if step_plan is plan else 'the next allowed step'}")
            for step in step_plan.steps:
                if not step.kind.is_write:
                    continue
                result, _ = _run_step(guard, confirm, step, pacer, progress, cancelled)
                results.append(result)
            if step_plan is plan:
                return _report(profile, guard, results, plan, warnings, None, policy, residual_lines)
            neg = _demote(results, neg)
            readings = _read_all(guard, confirm, targets, policy, profile, topology, pacer, progress, cancelled, results)
        except _Stop as stop:
            return _report(profile, guard, results, step_plan, warnings, stop, policy, residual_lines)
        plan = plan_of(profile, readings, residual_cpus)
    return _report(profile, guard, results, plan, warnings, None, policy, residual_lines)  # not reached: the last round is the plan itself


class _Readings:
    """The words of one read pass (the executor's reads, in the plan's read order)."""

    def __init__(self) -> None:
        self.misc_word: int | None = None
        self.words: dict[int, dict[StepKind, int]] = {}
        self.point_words: dict[tuple[int, int], int] = {}  # T-025: (cpu, point) -> the 0x10 P2=point word
        self.msr_words: dict[int, int] = {}
        self.oc_cap_words: dict[int, int] = {}  # G-137 (D-039): cpu -> the 0x01 word read in this run

    @property
    def baseline(self) -> dict[int, CoreReadings]:
        return {
            cpu: CoreReadings(
                vf=VfWord.from_u32(w[StepKind.READ_VF]),
                favored=w[StepKind.READ_FAVORED],
                fused=w[StepKind.READ_FUSED],
                granular=w[StepKind.READ_GRANULAR],
                vmax=w.get(StepKind.READ_VMAX),
            )
            for cpu, w in self.words.items()
        }


def _read_all(
    guard: WriteGuard,
    confirm: Confirmation,
    targets: tuple,  # type: ignore[type-arg]
    policy: Policy,
    profile: Profile,
    topology: Topology,
    pacer: _Pacer,
    progress: Callable[[str], None] | None,
    cancelled: Callable[[], bool] | None,
    results: list[StepResult],
) -> _Readings:
    """Every read step of ``profile``'s plan through the guard (raises ``_Stop``); each successful read is the session baseline the guard measures the next write from."""
    rd = _Readings()
    for step in read_steps(targets, policy, profile, topology):
        result, word = _run_step(guard, confirm, step, pacer, progress, cancelled)
        results.append(result)
        if step.kind is StepKind.READ_MSR:
            assert step.msr is not None
            rd.msr_words[step.msr] = word
        elif step.kind is StepKind.READ_PERCORE_ENABLE:
            rd.misc_word = word
        elif step.kind is StepKind.READ_VF_POINT:
            rd.point_words[(step.cpu, step.p2)] = word
        elif step.kind is StepKind.READ_OC_CAP:  # G-137: the CPU's own ratio limit of a core kind, not a per-target reading
            rd.oc_cap_words[step.cpu] = word
        else:
            rd.words.setdefault(step.cpu, {})[step.kind] = word
    mailbox = bool(targets or resolve_point_targets(profile, topology))  # G-117: a turbo-table-only plan reads no 0x14
    assert rd.misc_word is not None or not mailbox
    return rd
