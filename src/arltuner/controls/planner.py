"""Pure planner: profile + topology + words read from the core -> ordered, pre-checked mailbox request sequence. No I/O.

XTU's per-core Apply, as **measured** on this machine (``[MEASURED win 2026-10-01]``, ``docs/research/win-xtu-live-trace.md`` S-03d,
S-04...S-07; registers.md "Measured via XTU's level-4 log"), is reproduced here:

1. read cmd ``0x14`` (P1=0, P2=0) once, on the first target CPU;
2. if bit 3 (PerCore Voltage Enable) is clear, write cmd ``0x15`` with ``old | 0x8`` (XTU skips it when the bit is set; ``0x14`` is
   ``0x8`` on this machine);
3. per target CPU read ``0x10`` (the baseline, ``0x0`` on 23 of 24 cores), ``0x1C`` (favored word), ``0x09 P1=0x11 P2=0`` (the fused V/F
   point) and ``0x26``; then write ``0x11`` (P1=0, P2=0; the full word), ``0x1D`` (data = that word's ratio) and ``0x27`` (data = the
   ``0x26`` word read).

The ``0x11`` word is built as XTU builds it: **ratio** = the core's non-zero baseline ratio, else ``0x1C[7:0]``; **V target** = the core's
non-zero baseline V target, else the fused voltage (``0x09`` ``[19:8]``: the XTU UI "Voltage Override", ``[MEASURED]``); adaptive mode; only
the requested fields changed. XTU never sends a ratio of 0 or a V target of 0, and neither does this planner (it would plan a word the guard
refuses). All reads are ordered before all writes (including the ``0x15``), so pre-flight judges every write against the real words
before the first write is sent; XTU's own order interleaves them (``0x26`` is read between ``0x1D`` and ``0x27``) and, for the 16
E-cores, batches ``0x11`` x16, ``0x1D`` x16, ``0x27`` x16 -- whether the hardware needs XTU's order is ``[UNVERIFIED]``. An E-core entry
applies to all four CPUs of its cluster (XTU wrote every core of an E tile and never one alone; whether one write is cluster-wide is
``[UNVERIFIED]``, C-4), de-duplicated by cluster; a single-CPU E write is not planned.

T-022 / D-016 (the operator's scope: ratio, adaptive OC-point voltage and offsets of P and E, raised *or* lowered, bounded by a reviewed
policy; static / Power-Gate stay out of scope):

* the profile fields may exceed the baseline; the guard's ``vf_bounded`` check decides by bounds (this module mirrors it);
* a target whose change exceeds a per-operation step limit (``Policy.max_ratio_step`` / ``max_v_step_mv`` / ``max_offset_step_mv``) while every
  absolute bound allows the final values makes the plan carry the **next allowed step** as a profile (``ApplyPlan.next_profile_text``;
  ``require_allowed`` raises ``NeedsRamp``) -- an overclock is reached by small, read-back-verified steps, each its own session;
* a ratio **and** V target that move the same way take two ``0x11`` words: raising = **voltage first** (word 1: the new V target with the
  old ratio; word 2: the full word), lowering = **frequency first** (word 1: the new ratio with the old V target) -- XTU's rule
  (registers.md "Ordering", ``[DECOMPILED]``); the guard refuses a single word that moves both the same way;
* a profile with an OC-point voltage also reads ``0x28`` (the hardware maximum, XTU's ``raw - 1``) of that CPU: the guard refuses a V target
  above the current one without that read, and any above the hardware maximum;
* the budget counts every WRMSR: reads, writes **and the guard's read-back of each write** (``count_operations``). ``[UNVERIFIED]`` on this
  machine: that the hardware accepts a raise at all (XTU has only written stock-equivalent values), the E-core write scope, and what
  ``0x1C`` returns after a ``0x1D``.

T-023 / D-017 (turbo ratio tables, plain MSRs ``0x650`` E / ``0x1AD`` P): a profile's ``[turbo]`` table adds plain ``rdmsr`` steps (``0xCE`` and the
table, on the first P CPU -- XTU writes both tables on CPU 0, ``[DECOMPILED win-xtu-native-provider.md S6a]``) **before** the mailbox reads and one
plain ``wrmsr`` step per table, read back by the guard on the write CPU and on a CPU of the other core kind (``verify_cpu``: the scope of the
write is ``[UNVERIFIED]``). Ordering, so the effective frequency only rises once the voltage is in place: a table that **raises** any bucket is
written **last** (after every per-core word), one that only **lowers** buckets is written **first** (after the ``0x15`` enable, before the per-core
words); a table that both raises and lowers buckets is refused (split it). The plain writes count in the budget (one operation each; plain reads and
the read-back ``rdmsr`` s are not WRMSRs). An E ratio above the current ``0x650`` maximum with no ``[turbo] e`` table is warned about (the raise will
be capped; no table is invented): that read of ``0x650`` is planned only when the policy allows it.

T-025 (per-V/F-point offsets, ``[[point]]``): per target CPU and point the plan reads ``0x10 P2=point`` (after ``0x14``, before the per-core reads) and
writes ``0x11 P2=point`` -- the word read with only its offset field replaced (``[MEASURED lnx 2026-10-02]``: the top fused point of an E CPU reads
``0x0000002E`` = 46x, V target 0; OCCT wrote ``ratio | offset << 21``). The ``0x15`` enable rule is the same as for a ``[[core]]``; no ``0x1C`` / ``0x09`` /
``0x26`` / ``0x28`` read and no ``0x1D`` / ``0x27`` write belongs to a point. A point's ``expect_ratio`` that differs from the ratio field read refuses
the write step (predicted ``CHECK_FAILED``, nothing is sent). Ordering when a profile has several kinds of change (a documented simplification, no
interleaving by direction): ``0x15``, a lowering-only turbo table, **all point writes, then all per-core words**, then a raising turbo table; the budget
counts every point read, write and read-back. The hardware's V(n) >= V(n-1) rule across points is not checked here (fused-point voltages are not readable).

Pre-flight mirrors the guard: ``Policy.cpu_allowed``, ``Policy.match`` and the entry's named predicate from ``guard.CHECKS``, with the same
session facts (``guard.build_context``: the ``0x1C`` / ``0x09`` / ``0x28`` words, the planned ``0x11`` word, the D-015 / D-016 bounds), so a
predicted refusal is exactly what the guard would answer (the guard stays authoritative).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from arltuner.guard import CHECKS, MSR_CHECKS, AllowEntry, MsrAllowEntry, MsrRequest, Policy, Refusal, Request
from arltuner.guard.policy import MSR_TURBO_KIND, CheckContext, build_context, build_msr_context, turbo_bytes
from arltuner.mailbox import Cmd, VfWord
from arltuner.profiles import PLATFORM, Profile
from arltuner.topology import Topology

from .model import (
    OK_VERDICT,
    PERCORE_ENABLE_BIT,
    READS_PER_CPU,
    SPLIT_EXTRA_WRITES,
    VMAX_EXTRA_READS,
    WRITES_PER_CPU,
    XTU_SHAPE_CAVEAT,
    ApplyPlan,
    CoreReadings,
    NeedsBootBudget,
    NeedsHigherLimit,
    PlanError,
    PlanRefused,
    PlanStep,
    PointTarget,
    PredictedRefusal,
    StepKind,
    Target,
    Verdict,
)

__all__ = [
    "PLATFORM_INFO_MSR",
    "RAISE_CAVEAT",
    "TURBO_MSR",
    "build_plan",
    "count_operations",
    "pack_turbo",
    "plan_warnings",
    "predict",
    "predict_msr",
    "preflight",
    "read_steps",
    "resolve_point_targets",
    "resolve_targets",
    "turbo_cpus",
    "xtu_word",
]

#: T-023: the plain MSRs of the turbo tables by profile key (``[turbo] e`` / ``p``) and ``MSR_PLATFORM_INFO`` (``0xCE[15:8]`` = base ratio).
TURBO_MSR = {"e": 0x650, "p": 0x1AD}
PLATFORM_INFO_MSR = 0xCE
_TURBO_LABEL = {0x650: "E turbo table (0x650)", 0x1AD: "P turbo table (0x1AD)"}

_FUSED_P1 = 0x11  # cmd 0x09: P1 0x11 = domain floors / fused points, P2 = domain (0 = core) -- registers.md "Mailbox commands"

_CLUSTER_WARNING = (
    "E-core entries apply to all 4 CPUs of their cluster (XTU writes every E-core); whether one write is "
    "cluster-wide is UNVERIFIED on this machine"
)

#: Warning on a plan that raises the ratio, the V target or the offset (D-016). ASCII only.
RAISE_CAVEAT = (
    "this plan RAISES a ratio, V target or offset: the numbers come from the reviewed policy (D-016), but that the hardware accepts a raise at "
    "all is UNVERIFIED (XTU has only written stock-equivalent values on this machine); take a stability check between steps; a reboot "
    "restores the BIOS-programmed state"
)


#: Warning on a plan that raises a turbo table bucket (D-017). ASCII only.
TURBO_RAISE_CAVEAT = (
    "this plan RAISES a turbo ratio table bucket (plain wrmsr of 0x650 / 0x1AD, bounded by the policy's ratio_max): that the hardware accepts it and "
    "the write scope (package or core) are UNVERIFIED -- the guard reads the table back on a CPU of the other core kind and trips on a mismatch; "
    "a reboot restores the BIOS-programmed tables"
)


def _bytes_text(word: int) -> str:
    return "/".join(str(b) for b in turbo_bytes(word))


def _capped_raise_warnings(
    targets: tuple[Target, ...], currents: Mapping[int, VfWord], e_table: int | None, profile: Profile
) -> list[str]:
    """D-017: an E ratio above the highest bucket of the current ``0x650`` table, with no ``[turbo] e`` table in the profile, takes effect only up to
    that bucket (``[MEASURED lnx 2026-10-01]``: ratio 49 in the ``0x11`` word, ``0x650`` = 48 x8, the cores ran 48x). Warn; never invent a table."""
    if e_table is None:
        return []
    cap = max(turbo_bytes(e_table))
    out: list[str] = []
    seen: set[int] = set()
    for t in targets:
        if t.kind != "E" or t.ratio is None or t.ratio <= cap or t.ratio <= currents[t.cpu].ratio:
            continue
        if t.cluster is not None:
            if t.cluster in seen:
                continue
            seen.add(t.cluster)
        out.append(
            f"{t.label}: ratio {t.ratio} is above the E turbo table maximum {cap} (MSR 0x650 = {e_table:#018x}); the raise will be capped at {cap} "
            "unless the profile also carries a [turbo] e table (none is invented)"
        )
    return out


def resolve_targets(profile: Profile, topology: Topology) -> tuple[Target, ...]:
    """Expand the profile into the CPUs to write (profile order). An E entry yields its whole cluster once; entries of one
    cluster carry identical values (schema rule), so a repeated cluster is skipped. ``ProfileError`` if an entry is not in
    ``topology``."""
    targets: list[Target] = []
    seen_clusters: set[int] = set()
    seen_cpus: set[int] = set()
    for setting in profile.expand(topology):
        if setting.cluster is not None:
            if setting.cluster in seen_clusters:
                continue
            seen_clusters.add(setting.cluster)
        for cpu in setting.cpus:
            if cpu in seen_cpus:
                raise PlanError(f"CPU {cpu} is targeted twice by the profile")
            seen_cpus.add(cpu)
            core = topology.by_cpu(cpu)
            targets.append(
                Target(
                    cpu=cpu,
                    label=topology.xtu_name(core),
                    kind=core.kind,
                    index=core.index,
                    cluster=core.cluster,
                    ratio=setting.ratio,
                    oc_voltage_mv=setting.oc_voltage_mv,
                    offset_mv=setting.offset_mv,
                )
            )
    if not targets and not profile.points:
        raise PlanError("the profile has no targets")
    return tuple(targets)


def resolve_point_targets(profile: Profile, topology: Topology) -> tuple[PointTarget, ...]:
    """T-025: the CPUs and fused V/F points of the profile's ``[[point]]`` entries (profile order; an E entry yields the whole cluster once per point,
    like ``resolve_targets``). ``ProfileError`` if an entry is not in ``topology``; an empty tuple when the profile has none."""
    out: list[PointTarget] = []
    seen: set[tuple[int, int]] = set()
    for setting in profile.expand_points(topology):
        for cpu in setting.cpus:
            if (cpu, setting.point) in seen:  # an E cluster named by several entries: the schema made them identical
                continue
            seen.add((cpu, setting.point))
            core = topology.by_cpu(cpu)
            out.append(
                PointTarget(cpu, topology.xtu_name(core), core.kind, core.index, core.cluster, setting.point, setting.offset_mv, setting.expect_ratio)
            )
    return tuple(out)


def _reads_of(target: Target) -> int:
    """Reads of one target: the four of XTU's sequence, plus 0x28 when the profile sets an OC-point voltage."""
    return READS_PER_CPU + (VMAX_EXTRA_READS if target.oc_voltage_mv is not None else 0)


def _max_writes_of(target: Target) -> int:
    """Writes of one target, worst case: 0x11, 0x1D, 0x27, and a second 0x11 when ratio and V target could move the same way (both set)."""
    both = target.ratio is not None and target.oc_voltage_mv is not None
    return WRITES_PER_CPU + (SPLIT_EXTRA_WRITES if both else 0)


def count_operations(targets: int | Sequence[Target], per_core_enabled: bool | None, msr_writes: int = 0, n_points: int = 0) -> int:
    """Mailbox WRMSRs of a plan (worst case for the targets given): the 0x14 read, per target its reads (0x10, 0x1C, 0x09, 0x26, and 0x28 with an
    OC-point voltage) and writes (0x11, 0x1D, 0x27, and a second 0x11 when ratio and OC-point voltage are both set), plus the 0x15 write unless
    PerCore Voltage Enable is known to be set -- and **one read-back per write** (the guard reads every write back through the same transport;
    T-022 counts them, the guard's own counters do not). ``None`` (unknown) counts the 0x15 write: the conservative worst case.

    ``msr_writes`` (T-023): plain-MSR table writes of the plan; each is one more guard operation (a WRMSR); plain reads and the ``rdmsr`` read-backs
    are not WRMSRs and are not counted.

    ``n_points`` (T-025): CPU x point offset targets of the profile; each is one ``0x10 P2`` read, one ``0x11 P2`` write and its read-back (3 WRMSRs). With
    ``n_points > 0`` the core ``targets`` may be empty (a point-only profile).

    ``targets`` is a number of plain (offset-only) targets or the ``Target`` objects themselves."""
    if isinstance(n_points, bool) or not isinstance(n_points, int) or n_points < 0:
        raise ValueError(f"n_points must be an int >= 0, got {n_points!r}")
    if isinstance(targets, int) and not isinstance(targets, bool):
        if targets < 1:
            raise ValueError(f"n_targets must be an int >= 1, got {targets!r}")
        reads = 1 + READS_PER_CPU * targets
        writes = WRITES_PER_CPU * targets
    else:
        try:
            listed = tuple(targets)
        except TypeError:
            listed = ()
        if (not listed and n_points == 0) or not all(isinstance(t, Target) for t in listed):
            raise ValueError(f"count_operations needs a number of targets >= 1 or Target objects, got {targets!r}")
        reads = 1 + sum(_reads_of(t) for t in listed)
        writes = sum(_max_writes_of(t) for t in listed)
    reads += n_points
    writes += n_points + (0 if per_core_enabled is True else 1)
    return reads + writes + writes + msr_writes


def _address_verdict(policy: Policy, req: Request) -> tuple[AllowEntry | None, Verdict]:
    """The guard's first two content checks that need no baseline: CPU set, then allowlist (default deny)."""
    if not policy.cpu_allowed(req.cpu):
        return None, Verdict(False, Refusal.CPU_NOT_ALLOWED.value, f"cpu {req.cpu} is not in the policy CPU set")
    entry = policy.match(req.cmd, req.p1, req.p2)
    if entry is None:
        return None, Verdict(
            False,
            Refusal.NOT_ALLOWLISTED.value,
            f"cmd/p1/p2 {req.cmd:#04x}/{req.p1}/{req.p2} matches no allow entry (default deny)",
        )
    return entry, OK_VERDICT


def predict(policy: Policy, req: Request, baseline_word: int | None, ctx: CheckContext | None = None) -> Verdict:
    """What the guard will answer for ``req`` given the word read earlier with its read twin (``baseline_word``; reads need none) and
    the session facts ``ctx``: CPU set, then allowlist, then - for a write - the entry's named predicate. Session limit / dwell / lock
    / gates are not predictable here."""
    entry, verdict = _address_verdict(policy, req)
    if entry is None:
        return verdict
    if entry.kind == "write":
        check = CHECKS.get(entry.check) if entry.check is not None else None
        if check is None or baseline_word is None:
            return Verdict(False, Refusal.CHECK_FAILED.value, f"write entry {entry.name!r} has no evaluable check")
        try:
            ok, detail = check(req, baseline_word, entry.params, ctx)
        except Exception as exc:  # the guard treats a raising predicate as a refusal; so do we
            ok, detail = False, f"check {entry.check!r} raised {type(exc).__name__}: {exc}"
        if ok is not True:
            return Verdict(False, Refusal.CHECK_FAILED.value, f"{entry.check}: {detail}")
    return OK_VERDICT


def predict_msr(policy: Policy, req: MsrRequest, baseline_word: int | None, platform_info: int | None = None) -> Verdict:
    """What the guard will answer for the plain-MSR ``req`` (the plain twin of :func:`predict`): CPU set, ``msr_allow`` entry (and its CPUs), then
    for a write the verify CPU (other core kind, covered by a read entry) and ``turbo_ratio_bounded`` on the word read earlier (``baseline_word``)
    with ``platform_info`` = the ``0xCE`` word read this session. Limits / dwell / lock / gates are not predictable here."""
    if not policy.cpu_allowed(req.cpu):
        return Verdict(False, Refusal.CPU_NOT_ALLOWED.value, f"cpu {req.cpu} is not in the policy CPU set")
    entry: MsrAllowEntry | None = policy.match_msr(req.msr, req.kind)
    if entry is None:
        return Verdict(False, Refusal.NOT_ALLOWLISTED.value, f"plain msr/kind {req.msr:#x}/{req.kind} matches no msr_allow entry (default deny)")
    if not entry.covers_cpu(req.cpu):
        return Verdict(False, Refusal.CPU_NOT_ALLOWED.value, f"cpu {req.cpu} is not covered by msr_allow entry {entry.name!r}")
    if entry.kind == "read":
        return OK_VERDICT
    verify = req.verify_cpu
    if verify is None or verify == req.cpu or not policy.cpu_allowed(verify) or policy.core_kind(verify) == policy.core_kind(req.cpu):
        return Verdict(False, Refusal.CHECK_FAILED.value, f"verify_cpu {verify!r} must be an allowed CPU of the other core kind than cpu {req.cpu}")
    reader = policy.match_msr(req.msr, "read")
    if reader is None or not reader.covers_cpu(req.cpu) or not reader.covers_cpu(verify):
        return Verdict(False, Refusal.CHECK_FAILED.value, f"the read-back needs a msr_allow read entry for {req.msr:#x} covering cpu {req.cpu} and {verify}")
    check = MSR_CHECKS.get(entry.check) if entry.check is not None else None
    if check is None or baseline_word is None:
        return Verdict(False, Refusal.CHECK_FAILED.value, f"write entry {entry.name!r} has no evaluable check")
    try:
        ok, detail = check(req, baseline_word, entry.params, build_msr_context(policy, req.msr, platform_info=platform_info))
    except Exception as exc:
        ok, detail = False, f"check {entry.check!r} raised {type(exc).__name__}: {exc}"
    if ok is not True:
        return Verdict(False, Refusal.CHECK_FAILED.value, f"{entry.check}: {detail}")
    return OK_VERDICT


def pack_turbo(buckets: Sequence[int]) -> int:
    """The u64 of a ratio table: bucket 0 in the least significant byte."""
    return sum((int(b) & 0xFF) << (8 * k) for k, b in enumerate(buckets))


def turbo_cpus(topology: Topology) -> tuple[int, int]:
    """``(write cpu, verify cpu)`` of the plain table writes: the lowest P CPU (XTU writes both tables on CPU 0) and the lowest E CPU (a CPU of the
    other core kind, so the guard's read-back also shows whether the write is package-wide)."""
    p_cpus = sorted(c.cpu for c in topology.cores if c.kind == "P")
    e_cpus = sorted(c.cpu for c in topology.cores if c.kind == "E")
    if not p_cpus or not e_cpus:
        raise PlanError("the turbo tables need a P and an E CPU in the topology (write on one, read back on the other kind)")
    return p_cpus[0], e_cpus[0]


def _turbo_tables(profile: Profile | None) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """``(msr, ratios)`` of the profile's ``[turbo]`` tables, E first."""
    if profile is None or profile.turbo is None:
        return ()
    out = []
    for key in ("e", "p"):
        ratios = getattr(profile.turbo, key)
        if ratios is not None:
            out.append((TURBO_MSR[key], tuple(ratios)))
    return tuple(out)


def _msr_read_wants(profile: Profile | None, targets: tuple[Target, ...], topology: Topology | None, policy: Policy) -> list[int]:
    """The plain MSRs the plan reads (on the write CPU) before the mailbox reads, in order: ``0xCE`` and each ``[turbo]`` table; and ``0x650`` alone
    (if the policy allows that read) when an E target sets a ratio but the profile has no ``[turbo] e`` table -- the baseline of the "this raise
    will be capped" warning."""
    if profile is None or topology is None:
        return []
    tables = [msr for msr, _ in _turbo_tables(profile)]
    if tables:
        return [PLATFORM_INFO_MSR, *tables]
    if any(t.kind == "E" and t.ratio is not None for t in targets):
        write_cpu, _ = turbo_cpus(topology)
        if predict_msr(policy, MsrRequest(write_cpu, TURBO_MSR["e"], 0, "preflight", "read"), None).allowed:
            return [TURBO_MSR["e"]]
    return []


#: (step kind, cmd, P1, text) of the reads of one target CPU, in order (all P2 = 0).
_CPU_READS = (
    (StepKind.READ_VF, int(Cmd.VOLTAGE_FREQ_READ), 0, "V/F baseline (0x10)"),
    (StepKind.READ_FAVORED, int(Cmd.FAVORED_CORE_READ), 0, "favored-core word (0x1C)"),
    (StepKind.READ_FUSED, int(Cmd.VOLTAGE_FLOOR_READ), _FUSED_P1, "fused V/F point (0x09 P1=0x11)"),
    (StepKind.READ_GRANULAR, int(Cmd.GRANULAR_RATIO_READ), 0, "granular-ratio word (0x26)"),
)
_VMAX_READ = (StepKind.READ_VMAX, int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), 0, "hardware maximum voltage (0x28)")


def read_steps(
    targets: tuple[Target, ...], policy: Policy, profile: Profile | None = None, topology: Topology | None = None
) -> tuple[PlanStep, ...]:
    """The read phase (T-025: ``0x10 P2=point`` of each ``[[point]]`` target follows the ``0x14`` read): with a ``profile`` and ``topology`` (T-023) first the plain ``rdmsr`` steps (``0xCE`` and the ``[turbo]`` tables, or ``0x650``
    for the capped-raise warning) on the first P CPU; then 0x14 on the first target CPU, then per target 0x10, 0x1C, 0x09 P1=0x11 and 0x26 (P2=0 for
    all), and 0x28 when the target sets an OC-point voltage (D-016: the hardware maximum bounds a raised V target)."""
    steps: list[PlanStep] = []
    points = resolve_point_targets(profile, topology) if profile is not None and topology is not None else ()
    wants = _msr_read_wants(profile, targets, topology, policy)
    if wants:
        assert topology is not None
        write_cpu, _ = turbo_cpus(topology)
        for msr in wants:
            what = "PLATFORM_INFO (0xCE)" if msr == PLATFORM_INFO_MSR else _TURBO_LABEL[msr]
            mreq = MsrRequest(write_cpu, msr, 0, f"apply: read {what} (plain rdmsr) before any write", "read")
            steps.append(
                PlanStep(
                    len(steps) + 1, StepKind.READ_MSR, write_cpu, 0, 0, 0, 0, mreq.purpose, what, verdict=predict_msr(policy, mreq, None), msr=msr
                )
            )
    number = len(steps) + 1
    first_cpu = points[0].cpu if points else targets[0].cpu  # 0x14 is package-wide: read it on the first CPU the plan touches
    req = Request(first_cpu, int(Cmd.MISC_GLOBAL_CONFIG_READ), 0, 0, 0, "apply: read PerCore Voltage Enable state (0x14) before any write")
    steps.append(
        PlanStep(number, StepKind.READ_PERCORE_ENABLE, req.cpu, req.cmd, 0, 0, 0, req.purpose, "package", verdict=predict(policy, req, None))
    )
    for pt in points:  # T-025: the baseline word of each V/F point (0x10 P2=point), then the per-core reads below
        number += 1
        req = Request(pt.cpu, int(Cmd.VOLTAGE_FREQ_READ), 0, pt.point, 0, f"apply: read V/F point {pt.point} baseline (0x10 P2={pt.point}) of {pt.label}")
        steps.append(
            PlanStep(
                number, StepKind.READ_VF_POINT, pt.cpu, req.cmd, 0, pt.point, 0, req.purpose, f"{pt.label} point {pt.point}",
                verdict=predict(policy, req, None), core_kind=pt.kind,
            )
        )
    for t in targets:
        for kind, cmd, p1, text in (*_CPU_READS, *((_VMAX_READ,) if t.oc_voltage_mv is not None else ())):
            number += 1
            req = Request(t.cpu, cmd, p1, 0, 0, f"apply: read {text} of {t.label}")
            steps.append(PlanStep(number, kind, t.cpu, cmd, p1, 0, 0, req.purpose, t.label, verdict=predict(policy, req, None), core_kind=t.kind))
    return tuple(steps)


#: Warning on every plan with a ``[[point]]`` (T-025). ASCII only.
POINT_CAVEAT = (
    "per-point offsets change one V/F point only (0x11 with P2 = the point; the OC point is the P2 = 0 word): the hardware enforces V(n) >= V(n-1) across "
    "the points of a cluster, arltuner does not check it (the voltages of the fused points are not readable) [COMMUNITY skatterbencher.com]; whether "
    "the hardware accepts a point write at all on Linux is UNVERIFIED (OCCT wrote such words on this machine); a reboot restores the BIOS-programmed state"
)


def plan_warnings(
    targets: tuple[Target, ...], policy: Policy | None = None, point_targets: tuple[PointTarget, ...] = ()
) -> tuple[str, ...]:
    """Warnings that apply to a plan for ``targets`` (and, T-025, ``point_targets``): the XTU-shape caveat (not for a point-only profile) (0x1D/0x27 and zero-field acceptance are UNVERIFIED on
    Linux); the cluster caveat for E cores; and, given the ``policy`` and an offset in the profile, the D-015 note that the effective
    offset is the (operator-stated) BIOS offset plus the runtime offset."""
    warnings: list[str] = [XTU_SHAPE_CAVEAT] if targets else []  # T-025: a point-only profile sends no 0x1D / 0x27 and no OC-point word
    if any(t.cluster is not None for t in (*targets, *point_targets)):
        warnings.append(_CLUSTER_WARNING)
    if point_targets:
        warnings.append(POINT_CAVEAT)
    if policy is not None and (point_targets or any(t.offset_mv is not None for t in targets)):
        bios = policy.assumed_bios_offset_mv
        warnings.append(
            f"effective offset = assumed BIOS offset (P {bios['P']:+d} mV, E {bios['E']:+d} mV: operator-stated, not readable through the "
            f"mailbox) + runtime offset (the runtime offset adds to the BIOS one, [MEASURED win 2026-10-01]); the policy floors the "
            f"effective offset at {policy.effective_offset_floor_mv} mV (D-015)"
        )
    return tuple(warnings)


def xtu_word(base: VfWord, favored_word: int, fused_word: int) -> VfWord:
    """The word XTU writes for a core before any change ``[MEASURED win 2026-10-01]``: the baseline word with ratio = its non-zero ratio,
    else ``0x1C[7:0]``, and V target = its non-zero V target, else the fused ``0x09`` voltage ``[19:8]``; mode and offset are the baseline's.
    (A baseline of ``0x0`` therefore becomes ``ratio | fused V << 8``, e.g. CPU 1: ``0x00053837``.) This is also the *current state* the
    step limits are measured from. D-016: a V target the core already carries is kept -- it is no longer reset to the fused voltage, which
    would silently undo an earlier step of a ramp (``[MEASURED]`` XTU only ever met baselines without a V target)."""
    ratio = base.ratio or (favored_word & 0xFF)
    v_target = base.v_target_raw or ((fused_word >> 8) & 0xFFF)
    return base.with_(ratio=ratio, v_target_raw=v_target)


def _goal_word(target: Target, current: VfWord) -> VfWord:
    """``current`` with the profile's fields applied (``static`` is never in the changes)."""
    try:
        changes: dict[str, int] = {}
        if target.ratio is not None:
            changes["ratio"] = target.ratio
        if target.oc_voltage_mv is not None:
            changes["v_target_raw"] = VfWord.v_to_raw(target.oc_voltage_mv / 1000)
        if target.offset_mv is not None:
            changes["offset_raw"] = VfWord.mv_to_offset_raw(target.offset_mv)
        return current.with_(**changes)
    except ValueError as exc:
        raise PlanError(f"{target.label}: requested value does not fit the V/F word: {exc}") from exc


def _vf_words(current: VfWord, goal: VfWord) -> tuple[tuple[VfWord, str], ...]:
    """The 0x11 words to send, in order, with their stage label. Ratio and V target moving the same way take two words (registers.md
    "Ordering"): rising -> V target first (the old ratio stays), falling -> ratio first (the old V target stays); the offset moves in the
    second word. Otherwise one word."""
    d_ratio = goal.ratio - current.ratio
    d_v = goal.v_target_raw - current.v_target_raw
    if d_ratio > 0 and d_v > 0:
        return ((current.with_(v_target_raw=goal.v_target_raw), "1/2 voltage first"), (goal, "2/2 full word"))
    if d_ratio < 0 and d_v < 0:
        return ((current.with_(ratio=goal.ratio), "1/2 frequency first"), (goal, "2/2 full word"))
    return ((goal, ""),)


def _step_toward(current: int, goal: int, limit: int) -> int:
    return current + max(-limit, min(limit, goal - current))


def _mv_toward(goal_raw: int, current_raw: int, to_raw: Callable[[int], int]) -> int:
    """The whole-mV profile value whose word value is nearest ``goal_raw`` **without passing it** (seen from ``current_raw``): a profile can
    only say whole mV, one mV is 1.024 raw, so rounding the other way could exceed the step limit by one raw unit."""
    mv = round(goal_raw * 1000 / 1024)
    if goal_raw >= current_raw:
        while to_raw(mv) > goal_raw:
            mv -= 1
        while to_raw(mv + 1) <= goal_raw:
            mv += 1
    else:
        while to_raw(mv) < goal_raw:
            mv += 1
        while to_raw(mv - 1) >= goal_raw:
            mv -= 1
    return mv


def _v_raw_of_mv(mv: int) -> int:
    return VfWord.v_to_raw(mv / 1000)


@dataclass(frozen=True)
class _Ramp:
    """The next allowed step of one target: the clamped word and the profile values that produce it."""

    word: VfWord
    ratio: int | None
    oc_voltage_mv: int | None
    offset_mv: int | None


def _next_ramp(target: Target, current: VfWord, goal: VfWord, ctx: CheckContext) -> _Ramp:
    """The goal moved by at most the step limits from ``current``; fields the profile did not set are left alone. A field already within its
    limit keeps the exact requested value."""
    ratio = _step_toward(current.ratio, goal.ratio, ctx.max_ratio_step)
    v_raw = _step_toward(current.v_target_raw, goal.v_target_raw, ctx.max_v_step_raw)
    offset_raw = _step_toward(current.offset_raw, goal.offset_raw, ctx.max_offset_step_raw)
    word = current.with_(ratio=ratio, v_target_raw=v_raw, offset_raw=offset_raw)
    return _Ramp(
        word,
        None if target.ratio is None else ratio,
        None
        if target.oc_voltage_mv is None
        else (target.oc_voltage_mv if v_raw == goal.v_target_raw else _mv_toward(v_raw, current.v_target_raw, _v_raw_of_mv)),
        None
        if target.offset_mv is None
        else (target.offset_mv if offset_raw == goal.offset_raw else _mv_toward(offset_raw, current.offset_raw, VfWord.mv_to_offset_raw)),
    )


def _next_profile_text(
    profile_name: str,
    targets: tuple[Target, ...],
    ramps: Mapping[int, _Ramp],
    point_targets: tuple[PointTarget, ...] = (),
    point_ramps: Mapping[tuple[int, int], int] | None = None,
) -> str:
    """TOML text of the next step: one ``[[core]]`` per target (an E cluster once), the ramped values for the targets that needed one, the
    requested values for the others; T-025: likewise one ``[[point]]`` per (core, point) with the ramped ``offset_mv`` (``point_ramps``:
    (cpu, point) -> mV). It is a valid profile (``load_profile``)."""
    point_ramps = {} if point_ramps is None else point_ramps
    lines = ["[meta]", f"name = {json.dumps(f'{profile_name} (next step)')}", f'platform = "{PLATFORM}"']
    seen_clusters: set[int] = set()
    for t in targets:
        if t.cluster is not None:
            if t.cluster in seen_clusters:
                continue
            seen_clusters.add(t.cluster)
        ramp = ramps.get(t.cpu)
        if t.cluster is not None and ramp is None:  # an E cluster is one profile entry: take the first CPU of it that needs a step
            ramp = next((ramps[c.cpu] for c in targets if c.cluster == t.cluster and c.cpu in ramps), None)
        ratio, volts, offset = (t.ratio, t.oc_voltage_mv, t.offset_mv) if ramp is None else (ramp.ratio, ramp.oc_voltage_mv, ramp.offset_mv)
        lines += ["", "[[core]]", f'kind = "{t.kind}"', f"index = {t.index}"]
        for key, value in (("ratio", ratio), ("oc_voltage_mv", volts), ("offset_mv", offset)):
            if value is not None:
                lines.append(f"{key} = {value}")
    seen_points: set[tuple[int | None, int]] = set()
    for pt in point_targets:
        if pt.cluster is not None:
            if (pt.cluster, pt.point) in seen_points:
                continue
            seen_points.add((pt.cluster, pt.point))
        mv = point_ramps.get((pt.cpu, pt.point))
        if mv is None and pt.cluster is not None:  # an E cluster is one entry: take the first CPU of it that needs a step
            mv = next((point_ramps[(c.cpu, c.point)] for c in point_targets if c.cluster == pt.cluster and c.point == pt.point and (c.cpu, c.point) in point_ramps), None)
        lines += ["", "[[point]]", f'kind = "{pt.kind}"', f"index = {pt.index}", f"point = {pt.point}", f"offset_mv = {pt.offset_mv if mv is None else mv}"]
        if pt.expect_ratio is not None:
            lines.append(f"expect_ratio = {pt.expect_ratio}")
    return "\n".join(lines) + "\n"


def _is_step_only(policy: Policy, req: Request, baseline: int, ctx: CheckContext) -> bool:
    """True when ``req`` is refused by the step limits alone: it would be allowed with the step limits lifted."""
    return predict(policy, req, baseline, ctx.unlimited_steps()).allowed


def build_plan(
    profile: Profile,
    topology: Topology,
    baseline: Mapping[int, CoreReadings],
    *,
    per_core_enabled: bool | None,
    policy: Policy,
    misc_word: int | None = None,
    msr_words: Mapping[int, int] | None = None,
    point_words: Mapping[tuple[int, int], int] | None = None,
) -> ApplyPlan:
    """Build the full plan from the profile and the **actual** words read. Pure: no I/O, nothing is sent.

    ``msr_words`` (T-023): the plain MSR words read on the write CPU (``0xCE``, ``0x650``, ``0x1AD``); every ``[turbo]`` table of the profile needs
    its MSR here (else ``PlanError``). The turbo writes are ordered by direction (module docstring).

    ``point_words`` (T-025): the ``0x10 P2=point`` word read for every ``[[point]]`` target, keyed ``(cpu, point)`` (else ``PlanError``).

    ``baseline`` maps every target CPU to its ``CoreReadings`` (0x10, 0x1C, 0x09, 0x26 and, for a profile with an OC-point voltage, 0x28).
    ``per_core_enabled``: ``True`` -> no 0x15 step, ``False`` -> a 0x15 step, ``None`` -> unknown: a conditional 0x15 step is included (worst
    case). ``misc_word`` (optional) is the 0x14 word read earlier; it fixes the 0x15 data (``old | 0x8``) and must agree with
    ``per_core_enabled``. Every step carries a predicted guard ``Verdict``; the plan never raises for a predicted refusal (see
    ``ApplyPlan.require_allowed``). A step-limit refusal also yields ``ApplyPlan.next_profile_text`` (D-016).
    """
    if per_core_enabled is not None and not isinstance(per_core_enabled, bool):
        raise TypeError(f"per_core_enabled must be bool or None, got {type(per_core_enabled).__name__}")
    if misc_word is not None:
        if isinstance(misc_word, bool) or not isinstance(misc_word, int) or not 0 <= misc_word <= 0xFFFFFFFF:
            raise ValueError(f"misc_word must be a u32, got {misc_word!r}")
        derived = bool(misc_word & PERCORE_ENABLE_BIT)
        if per_core_enabled is None:
            per_core_enabled = derived
        elif per_core_enabled != derived:
            raise ValueError(f"per_core_enabled={per_core_enabled} contradicts misc_word {misc_word:#010x} (bit 3 = {int(derived)})")
    targets = resolve_targets(profile, topology)
    point_targets = resolve_point_targets(profile, topology)
    point_words = {} if point_words is None else dict(point_words)
    missing_points = [(t.cpu, t.point) for t in point_targets if (t.cpu, t.point) not in point_words]
    if missing_points:
        raise PlanError(f"no baseline reading of the V/F point word(s) (cpu, point) {missing_points}")
    missing = [t.cpu for t in targets if t.cpu not in baseline]
    if missing:
        raise PlanError(f"no baseline readings for CPU(s) {missing}")
    for cpu in (t.cpu for t in targets):
        if not isinstance(baseline[cpu], CoreReadings):
            raise PlanError(f"baseline for CPU {cpu} is not a CoreReadings")

    msr_words = {} if msr_words is None else dict(msr_words)
    tables = _turbo_tables(profile)
    write_cpu, verify_cpu = turbo_cpus(topology) if (tables or msr_words) else (0, 0)
    for msr, _ in tables:
        if msr not in msr_words:
            raise PlanError(f"no baseline reading of MSR {msr:#x} for the profile's turbo table")
    steps = list(read_steps(targets, policy, profile, topology))
    number = len(steps)
    first_cpu = point_targets[0].cpu if point_targets else targets[0].cpu
    if per_core_enabled is not True:
        number += 1
        old_word = 0 if misc_word is None else misc_word
        # percore_enable_only is satisfied by construction (new data == baseline | 0x8); evaluate it against the known word, else 0.
        probe = Request(first_cpu, int(Cmd.MISC_GLOBAL_CONFIG_WRITE), 0, 0, old_word | PERCORE_ENABLE_BIT, "apply: enable PerCore Voltage Enable (0x15, old|0x8)")
        steps.append(
            PlanStep(
                number,
                StepKind.WRITE_PERCORE_ENABLE,
                first_cpu,
                probe.cmd,
                0,
                0,
                None if misc_word is None else probe.data,
                probe.purpose,
                "package",
                conditional=per_core_enabled is None,
                verdict=predict(policy, probe, old_word),
            )
        )
    turbo_notes: list[str] = []
    lowering: list[PlanStep] = []  # built now, numbered when they are appended (a table that only lowers goes first, one that raises goes last)
    raising: list[PlanStep] = []
    for msr, ratios in tables:
        old = msr_words[msr]
        new = pack_turbo(ratios)
        if new == old:
            turbo_notes.append(f"the profile's {_TURBO_LABEL[msr]} equals the current table ({old:#018x}): no write planned")
            continue
        up = any(n > o for n, o in zip(turbo_bytes(new), turbo_bytes(old)))
        down = any(n < o for n, o in zip(turbo_bytes(new), turbo_bytes(old)))
        purpose = f"apply: write {_TURBO_LABEL[msr]} (plain wrmsr, cpu {write_cpu}): {_bytes_text(old)} -> {_bytes_text(new)}"
        mreq = MsrRequest(write_cpu, msr, new, purpose, "write", verify_cpu)
        verdict = predict_msr(policy, mreq, old, msr_words.get(PLATFORM_INFO_MSR))
        if up and down and verdict.allowed:
            verdict = Verdict(
                False,
                Refusal.CHECK_FAILED.value,
                "turbo_ratio_bounded (planner): the table both raises and lowers buckets; the order of the writes differs (raise: after the per-core "
                "words, lower: before them), so split it into two profiles",
            )
        step = PlanStep(
            0, StepKind.WRITE_MSR, write_cpu, 0, 0, 0, new, purpose, _TURBO_LABEL[msr], verdict=verdict, core_kind=MSR_TURBO_KIND[msr],
            msr=msr, verify_cpu=verify_cpu, msr_before=old,
        )
        (raising if up else lowering).append(step)

    def append_turbo(group: list[PlanStep]) -> None:
        nonlocal number
        for st in group:
            number += 1
            steps.append(replace(st, number=number))

    append_turbo(lowering)
    ramps: dict[int, _Ramp] = {}
    point_ramps: dict[tuple[int, int], int] = {}
    step_only: set[int] = set()  # numbers of the 0x11 steps refused ONLY because a step limit is exceeded
    currents: dict[int, VfWord] = {}
    raises = False
    for pt in point_targets:  # T-025: V/F point offsets first, then the per-core words
        word = point_words[(pt.cpu, pt.point)]
        base = VfWord.from_u32(word)
        try:
            goal_raw = VfWord.mv_to_offset_raw(pt.offset_mv)
            new = base.with_(offset_raw=goal_raw)
        except ValueError as exc:
            raise PlanError(f"{pt.label} point {pt.point}: requested offset does not fit the V/F word: {exc}") from exc
        raises = raises or goal_raw > base.offset_raw
        ctx = build_context(policy, pt.cpu, favored_word=None, fused_word=None, vf_word_sent=None)
        purpose = f"apply: write V/F point {pt.point} offset (0x11 P2={pt.point}) of {pt.label}: {pt.describe()}"
        req = Request(pt.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, pt.point, new.to_u32(), purpose)
        verdict = predict(policy, req, word, ctx)
        mismatch = pt.expect_ratio is not None and base.ratio != pt.expect_ratio
        if mismatch and verdict.reason in (OK_VERDICT.reason, Refusal.CHECK_FAILED.value):
            verdict = Verdict(
                False,
                Refusal.CHECK_FAILED.value,
                f"expect_ratio (planner): the ratio field read for {pt.label} point {pt.point} is {base.ratio} (word {word:#010x}), the profile expects "
                f"{pt.expect_ratio}; nothing is sent",
            )
        elif not verdict.allowed and verdict.reason == Refusal.CHECK_FAILED.value and predict(policy, req, word, ctx.unlimited_steps()).allowed:
            offset_raw = _step_toward(base.offset_raw, goal_raw, ctx.max_offset_step_raw)
            point_ramps[(pt.cpu, pt.point)] = pt.offset_mv if offset_raw == goal_raw else _mv_toward(offset_raw, base.offset_raw, VfWord.mv_to_offset_raw)
            step_only.add(number + 1)
        number += 1
        steps.append(
            PlanStep(
                number, StepKind.WRITE_VF_POINT, pt.cpu, req.cmd, 0, pt.point, req.data, purpose, f"{pt.label} point {pt.point}",
                before=base, after=new, verdict=verdict, core_kind=pt.kind,
            )
        )
    for t in targets:
        readings = baseline[t.cpu]
        current = xtu_word(readings.vf, readings.favored, readings.fused)
        currents[t.cpu] = current
        goal = _goal_word(t, current)
        raises = raises or goal.ratio > current.ratio or goal.v_target_raw > current.v_target_raw or goal.offset_raw > current.offset_raw
        facts = dict(favored_word=readings.favored, fused_word=readings.fused, max_voltage_word=readings.vmax)
        words = _vf_words(current, goal)
        sent: int | None = None
        for word, stage in words:
            ctx = build_context(policy, t.cpu, vf_word_sent=sent, **facts)
            part = f" [{stage}]" if stage else ""
            purpose = f"apply: write V/F word (0x11) of {t.label}{part}: {t.describe()}"
            req = Request(t.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, 0, word.to_u32(), purpose)
            verdict = predict(policy, req, readings.vf.to_u32(), ctx)
            if not verdict.allowed and verdict.reason == Refusal.CHECK_FAILED.value and _is_step_only(policy, req, readings.vf.to_u32(), ctx):
                ramps[t.cpu] = _next_ramp(t, current, goal, ctx)
                step_only.add(number + 1)
            number += 1
            steps.append(
                PlanStep(
                    number,
                    StepKind.WRITE_VF,
                    t.cpu,
                    req.cmd,
                    0,
                    0,
                    req.data,
                    purpose,
                    t.label,
                    before=readings.vf,
                    after=word,
                    verdict=verdict,
                    core_kind=t.kind,
                    stage=stage,
                )
            )
            sent = word.to_u32()
        ctx = build_context(policy, t.cpu, vf_word_sent=goal.to_u32(), **facts)
        purpose = f"apply: write favored-core word (0x1D) of {t.label} = ratio {goal.ratio} of the 0x11 word"
        req = Request(t.cpu, int(Cmd.FAVORED_CORE_WRITE), 0, 0, goal.ratio, purpose)
        number += 1
        steps.append(
            PlanStep(
                number,
                StepKind.WRITE_FAVORED,
                t.cpu,
                req.cmd,
                0,
                0,
                req.data,
                purpose,
                t.label,
                verdict=predict(policy, req, readings.favored, ctx),
                core_kind=t.kind,
            )
        )
        purpose = f"apply: write granular-ratio word (0x27) of {t.label} = the 0x26 word read ({readings.granular:#x})"
        req = Request(t.cpu, int(Cmd.GRANULAR_RATIO_WRITE), 0, 0, readings.granular, purpose)
        number += 1
        steps.append(
            PlanStep(
                number,
                StepKind.WRITE_GRANULAR,
                t.cpu,
                req.cmd,
                0,
                0,
                req.data,
                purpose,
                t.label,
                verdict=predict(policy, req, readings.granular, ctx),
                core_kind=t.kind,
            )
        )
    append_turbo(raising)
    warnings = list(plan_warnings(targets, policy, point_targets))
    warnings.extend(turbo_notes)
    if raising:
        warnings.append(TURBO_RAISE_CAVEAT)
    if profile.turbo is None or profile.turbo.e is None:
        warnings.extend(_capped_raise_warnings(targets, currents, msr_words.get(TURBO_MSR["e"]), profile))
    for cluster, point in sorted({(t.cluster, t.point) for t in point_targets if t.cluster is not None}):
        members = [t.cpu for t in point_targets if t.cluster == cluster and t.point == point]
        if len({point_words[(c, point)] for c in members}) > 1:
            warnings.append(f"the CPUs of E cluster {cluster} ({', '.join(str(c) for c in members)}) read different words for point {point}; the profile is one entry for the whole cluster")
    for cluster in sorted({t.cluster for t in targets if t.cluster is not None}):
        members = [t.cpu for t in targets if t.cluster == cluster]
        if len({currents[c].to_u32() for c in members}) > 1:  # the CPUs of one E cluster are expected to agree ([UNVERIFIED] scope, C-4)
            warnings.append(
                f"the CPUs of E cluster {cluster} ({', '.join(str(c) for c in members)}) read different current V/F words; the profile is one "
                "entry for the whole cluster, so a next step is computed from the first CPU that needs one"
            )
    if raises:
        warnings.append(RAISE_CAVEAT)
    # A next step is only offered when the step limits are the ONLY obstacle: with any other predicted refusal (a ceiling, the allowlist, ...)
    # walking the steps would end in a refusal after several sessions, so the plain refusals are reported instead.
    hard = any(not st.verdict.allowed and st.number not in step_only for st in steps)
    return ApplyPlan(
        profile_name=profile.name,
        per_core_enabled=per_core_enabled,
        targets=targets,
        steps=tuple(steps),
        allowed_operations=policy.max_operations_per_session,
        min_dwell_s=policy.min_dwell_s,
        warnings=tuple(warnings),
        assumed_bios_offset_mv=policy.assumed_bios_offset_mv,
        effective_offset_floor_mv=policy.effective_offset_floor_mv,
        next_profile_text=(
            _next_profile_text(profile.name, targets, ramps, point_targets, point_ramps) if (ramps or point_ramps) and not hard else None
        ),
        point_targets=point_targets,
    )


def _static_problem(policy: Policy, target: Target) -> str | None:
    """What the guard would refuse about a profile value whatever the hardware answers: a number above an *explicit* policy ceiling or outside
    the offset window. (A ceiling left at the no-raise default depends on the words read and is judged after the reads.)"""
    kind = policy.core_kind(target.cpu)
    if target.ratio is not None:
        cap = policy.ratio_max[kind]
        if cap is not None and target.ratio > cap:
            return f"ratio {target.ratio} is above the policy ratio_max[{kind}] = {cap}"
    if target.oc_voltage_mv is not None:
        cap = policy.v_target_max_mv[kind]
        if cap is not None and _v_raw_of_mv(target.oc_voltage_mv) > _v_raw_of_mv(cap):
            return f"OC-point voltage {target.oc_voltage_mv} mV is above the policy v_target_max_mv[{kind}] = {cap} mV"
    if target.offset_mv is not None:
        return _offset_problem(policy, target.cpu, target.offset_mv)
    return None


def _offset_problem(policy: Policy, cpu: int, offset_mv: int) -> str | None:
    """The offset window of the policy (floor after the assumed BIOS offset, ceiling) for ``offset_mv`` on ``cpu``: shared by the OC-point and V/F point offsets."""
    raw = VfWord.mv_to_offset_raw(offset_mv)
    floor, ceiling = policy.offset_floor_raw(cpu), VfWord.mv_to_offset_raw(policy.offset_ceiling_mv)
    if not floor <= raw <= ceiling:
        return (
            f"offset {offset_mv:+d} mV (raw {raw}) is outside the policy window {floor}..{ceiling} raw "
            f"(effective floor {policy.effective_offset_floor_mv} mV minus the assumed BIOS offset, D-015; offset_ceiling_mv "
            f"{policy.offset_ceiling_mv}, D-016)"
        )
    return None


def preflight(profile: Profile, topology: Topology, policy: Policy, *, boot_ops_used: int = 0) -> tuple[Target, ...]:
    """Checks that need no hardware read, run before the first request of ``apply``: the worst-case WRMSR count (reads, writes and the
    guard's read-backs) against ``policy.max_operations_per_session`` (``NeedsHigherLimit``) and against what is left of this boot's budget
    ``policy.max_operations_per_boot - boot_ops_used`` (``NeedsBootBudget``; ``boot_ops_used`` = operations the audit log already records
    for this boot), CPU set / allowlist for every read and for the 0x11 / 0x1D / 0x27 writes, and every profile value against the
    policy's explicit ceilings and the offset window (``PlanRefused``). Returns the targets. Sends nothing."""
    if isinstance(boot_ops_used, bool) or not isinstance(boot_ops_used, int) or boot_ops_used < 0:
        raise ValueError(f"boot_ops_used must be a non-negative int, got {boot_ops_used!r}")
    targets = resolve_targets(profile, topology)
    point_targets = resolve_point_targets(profile, topology)
    tables = _turbo_tables(profile)
    required = count_operations(targets, None, len(tables), len(point_targets))
    if required > policy.max_operations_per_session:
        raise NeedsHigherLimit(required, policy.max_operations_per_session)
    remaining = max(policy.max_operations_per_boot - boot_ops_used, 0)
    if required > remaining:
        raise NeedsBootBudget(required, remaining, boot_ops_used, policy.max_operations_per_boot)
    refusals: list[PredictedRefusal] = []
    for step in read_steps(targets, policy, profile, topology):
        if not step.verdict.allowed:
            refusals.append(PredictedRefusal(step.number, step.cpu, step.cmd, step.verdict.reason, step.verdict.detail, step.msr))
    number = 2 + len(point_targets) + sum(_reads_of(t) for t in targets) + len(_msr_read_wants(profile, targets, topology, policy))  # reads are 1..N; the worst case puts the 0x15 write right after
    for pt in point_targets:  # T-025: the point writes come first; the address checks and the offset window are decidable before the words are read
        number += 1
        _, verdict = _address_verdict(policy, Request(pt.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, pt.point, 0, "preflight"))
        if not verdict.allowed:
            refusals.append(PredictedRefusal(number, pt.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), verdict.reason, verdict.detail))
        else:
            problem = _offset_problem(policy, pt.cpu, pt.offset_mv)
            if problem is not None:
                refusals.append(PredictedRefusal(number, pt.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), Refusal.CHECK_FAILED.value, f"vf_point_offset_bounded: {problem}"))
    for t in targets:  # the writes: only the address checks and the absolute profile bounds are decidable before the words are read
        problem = _static_problem(policy, t)
        for cmd in (Cmd.VOLTAGE_FREQ_WRITE, Cmd.FAVORED_CORE_WRITE, Cmd.GRANULAR_RATIO_WRITE):
            number += 1
            _, verdict = _address_verdict(policy, Request(t.cpu, int(cmd), 0, 0, 0, "preflight"))
            if not verdict.allowed:
                refusals.append(PredictedRefusal(number, t.cpu, int(cmd), verdict.reason, verdict.detail))
            elif cmd == Cmd.VOLTAGE_FREQ_WRITE and problem is not None:
                refusals.append(PredictedRefusal(number, t.cpu, int(cmd), Refusal.CHECK_FAILED.value, f"vf_bounded: {problem}"))
    if tables:
        write_cpu, verify_cpu = turbo_cpus(topology)
        for msr, ratios in tables:  # the plain writes: address checks and the explicit ceiling are decidable before the words are read
            number += 1
            entry = policy.match_msr(msr, "write")
            kind = MSR_TURBO_KIND[msr]
            cap = policy.ratio_max[kind]
            if not policy.cpu_allowed(write_cpu) or entry is None or not entry.covers_cpu(write_cpu):
                why = "cpu not allowed" if entry is not None else "matches no msr_allow write entry (default deny; a plain write needs a reviewed policy)"
                reason = Refusal.CPU_NOT_ALLOWED if entry is not None else Refusal.NOT_ALLOWLISTED
                refusals.append(PredictedRefusal(number, write_cpu, 0, reason.value, f"plain write of {msr:#x}: {why}", msr))
            elif cap is not None and max(ratios) > cap:
                refusals.append(
                    PredictedRefusal(number, write_cpu, 0, Refusal.CHECK_FAILED.value, f"turbo_ratio_bounded: ratio {max(ratios)} is above the policy ratio_max[{kind}] = {cap}", msr)
                )
    if refusals:
        raise PlanRefused(tuple(refusals))
    return targets
