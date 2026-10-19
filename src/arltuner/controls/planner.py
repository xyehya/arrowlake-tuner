"""Pure planner: profile + topology + words read from the core -> ordered, pre-checked mailbox request sequence. No I/O.

XTU's per-core Apply, as **measured** on this system (``[MEASURED win 2026-10-01]``, S-03d,
S-04...S-07; ``docs/knowledge/registers.md`` "Measured via XTU's level-4 log"), is reproduced here:

1. read cmd ``0x14`` (P1=0, P2=0) once, on the first target CPU;
2. if bit 3 (PerCore Voltage Enable) is clear, write cmd ``0x15`` with ``old | 0x8`` (XTU skips it when the bit is set; ``0x14`` is
   ``0x8`` on this system);
3. per target CPU read ``0x10`` (the baseline, ``0x0`` on 23 of 24 cores), ``0x1C`` (favored word), ``0x09 P1=0x11 P2=0`` (the fused V/F
   point) and ``0x26``; then write ``0x11`` (P1=0, P2=0; the full word), ``0x1D`` (data = that word's ratio) and ``0x27`` (data = the
   ``0x26`` word read).

The ``0x11`` word is built as XTU builds it: **ratio** = the core's non-zero baseline ratio, else ``0x1C[7:0]``; **V target** = the core's
non-zero baseline V target, else the fused voltage (``0x09`` ``[19:8]``: the XTU UI "Voltage Override", ``[MEASURED]``); adaptive mode; only
the requested fields changed. XTU never sends a ratio of 0 or a V target of 0, and neither does this planner -- except (G-124, D-035) the V target of a word with no OC point
(ratio <= the top fused ratio), which is always 0 because the hardware keeps none. All reads are ordered before all writes (including the ``0x15``), so pre-flight judges every write against the real words
before the first write is sent; XTU's own order interleaves them (``0x26`` is read between ``0x1D`` and ``0x27``) and, for the 16
E-cores, batches ``0x11`` x16, ``0x1D`` x16, ``0x27`` x16 -- whether the hardware needs XTU's order is ``[UNVERIFIED]``. An E-core entry
applies to all four CPUs of its cluster (XTU wrote every core of an E tile and never one alone; whether one write is cluster-wide is
``[UNVERIFIED]``, C-4), de-duplicated by cluster; a single-CPU E write is not planned.

T-022 / D-016 (the operator's scope: ratio, adaptive OC-point voltage and offsets of P and E, raised *or* lowered, bounded by a reviewed
policy; static / Power-Gate stay out of scope):

* the profile fields may exceed the baseline; the guard's ``vf_bounded`` check decides by bounds (this module mirrors it);
* D-049: there are no step limits and no operator ceilings: a target is judged by the CPU's own limits only (cmd ``0x01`` ratio, cmd ``0x28`` voltage, the offset field) and any
  size of change is one round (``ramp_rounds`` is always 1; ``ApplyPlan.next_profile_text`` is always ``None``). A large change, a positive or deep offset, a non-monotonic
  or raised turbo table are *warnings* of the plan (:func:`change_notes`), never refusals;
* a ratio **and** V target that move the same way take two ``0x11`` words: raising = **voltage first** (word 1: the new V target with the
  old ratio; word 2: the full word), lowering = **frequency first** (word 1: the new ratio with the old V target) -- XTU's rule
  (registers.md "Ordering", ``[DECOMPILED]``); the guard refuses a single word that moves both the same way;
* a profile with an OC-point voltage or a positive offset also reads ``0x28`` (the hardware maximum, XTU's ``raw - 1``) of that CPU: the guard refuses a predicted voltage
  above the current one without that read (``CPU_LIMIT_UNREAD``), and any above the hardware maximum;
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
the write step (predicted ``CHECK_FAILED``, nothing is sent). Ordering: ``0x15``, a lowering-only turbo table, the **point writes and per-core words**, then a raising turbo
table. G-151: those point and per-core writes are put in a transition-safe order per unit (``controls.order``): no ratio's voltage dips below ``min(before, after)`` at any
prefix, so writes that raise a voltage go first and an OC word that creates or raises the OC point goes before the point offsets that lower the curve under it; the planner's
order (points, then the OC word) is kept wherever it is already safe, and a unit with no safe order is refused (``write_order``). The budget counts every point read, write and
read-back. The hardware's V(n) >= V(n-1) rule across points is not checked here (fused-point voltages are not readable). A point whose word reads a ratio above the top fused
ratio is the OC point's slot (``P2 = n + 1``): never written as a fused point (``oc_point_slot``).

G-154 (D-045): the ring (cache, ``P1 = 2``, read and written through one CPU) is **one more unit**, planned apart from the cores: a profile's ``[ring]`` adds, after the per-core reads,
``0x10 P1=2 P2=0`` (the ring OC word) and ``0x10 P1=2 P2=k`` per ring point, and after the per-core writes one ``0x11 P1=2 P2=k`` per ring point that differs (the word read with only
``[31:21]`` replaced, read back by the guard) -- every ring point, the slot the BIOS ring OC ratio creates (``P2 = 9``) included; unlike a core's OC slot it is not derived from the OC word,
because the ring OC word is BIOS-owned: recorded and compared, **never written**. The ring is judged as a unit: when the live ring OC word is not the profile's (``ring_oc_differs``), a
point's ratio is not its ``expect_ratio``, a point does not exist (rc ``0x11``) or the policy/guard refuses a ring write, **no ring step is planned** and the reasons are in
``ApplyPlan.ring_refused`` (the P/E steps stand; nothing is skipped silently). Ring steps are ordered by the same transition-safe rule as a core's (``controls.order``), on the
ring's own curve; they never share a dictionary key with a core (``ring_words``: point -> word, key 0 = the OC word).

D-049, positive point offsets: a ``[[point]]`` / ``[ring]`` offset above 0 is checked by the guard against the ``0x28`` limit of its domain (the core kind's, or the ring's ``P1=2`` limit): the
point's own V target (else the unit's fused ``0x09`` voltage) + the offset must stay at or under it. The plan adds no extra read for that: the limit is the one bound to the policy from
``tune.read`` (or this session's ``0x28``), the fused voltage is the one of the CPU's core reads when the profile also sets that core; a positive point offset whose base or limit is
unread is predicted ``CPU_LIMIT_UNREAD`` (fail closed; a ring has no readable fused voltage, so it needs a V target in its point word). Residual cleanup (``controls.residual``) may now
lower a leftover OC point's voltage in one step (no step limit, D-049: intended).

Pre-flight mirrors the guard: ``Policy.cpu_allowed``, ``Policy.match`` and the entry's named predicate from ``guard.CHECKS``, with the same
session facts (``guard.build_context``: the ``0x1C`` / ``0x09`` / ``0x28`` / ``0x01`` words, the planned ``0x11`` word, the CPU's own limits), so a
predicted refusal is exactly what the guard would answer (the guard stays authoritative).
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace

from arltuner.guard import CHECKS, MSR_CHECKS, AllowEntry, MsrAllowEntry, MsrRequest, Policy, Refusal, Request
from arltuner.guard.policy import (
    DEEP_WHOLE_CURVE_MV,
    ENCODING_OFFSET_CEILING_MV,
    ENCODING_OFFSET_FLOOR_MV,
    LARGE_OFFSET_CHANGE_MV,
    LARGE_RATIO_CHANGE,
    MSR_TURBO_KIND,
    CheckContext,
    build_context,
    build_msr_context,
    fused_top_ratio,
    hw_ratio_max,
    turbo_bytes,
)
from arltuner.mailbox import Cmd, VfWord
from arltuner.profiles import Profile
from arltuner.topology import Topology

from .model import (
    OFFSET_ONLY_WRITES,
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
    NeedsRounds,
    PlanError,
    PlanRefused,
    PlanStep,
    PointTarget,
    PredictedRefusal,
    RingTarget,
    StepKind,
    Target,
    Verdict,
)
from .order import reorder_writes
from .ring import RING_P1, ring_cpu

__all__ = [
    "PLATFORM_INFO_MSR",
    "RAISE_CAVEAT",
    "TURBO_MSR",
    "Warn",
    "build_plan",
    "count_operations",
    "oc_cap_kinds",
    "oc_cap_reads_of",
    "pack_turbo",
    "plan_warnings",
    "predict",
    "ramp_rounds",
    "resolve_ring_targets",
    "check_rounds",
    "MAX_RAMP_ROUNDS",
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
    "cluster-wide is UNVERIFIED"
)

#: Warning on a plan that raises the ratio, the V target or the offset (D-016). ASCII only.
RAISE_CAVEAT = (
    "this plan RAISES a ratio, V target or offset: only the CPU's own limits bound it, and that the hardware accepts a raise at "
    "all is UNVERIFIED; take a stability check between steps; a reboot restores the BIOS-programmed state"
)


#: Warning on a plan that raises a turbo table bucket (D-017). ASCII only.
TURBO_RAISE_CAVEAT = (
    "this plan RAISES a turbo ratio table bucket (plain wrmsr of 0x650 / 0x1AD, bounded by the CPU's own ratio limit, cmd 0x01): that the hardware accepts it and "
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
                    fused_top_ratio=setting.fused_top_ratio,
                )
            )
    if not targets and not profile.points and profile.turbo is None and profile.ring is None:  # G-117: a [turbo]-only plan has no mailbox target
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


def resolve_ring_targets(profile: Profile, topology: Topology) -> tuple[RingTarget, ...]:
    """G-154: the ring points of the profile's ``[ring]`` (profile order), each with the CPU the ring is addressed through and the ring OC word the profile was made with. Empty
    without a ``[ring]``."""
    if profile.ring is None:
        return ()
    cpu = ring_cpu(topology)
    return tuple(RingTarget(cpu, p.point, p.offset_mv, p.expect_ratio, profile.ring.oc_word) for p in profile.ring.points)


def needs_vmax(target: Target) -> bool:
    """D-049: a target whose predicted voltage can rise reads the CPU's own voltage limit (``0x28``): it sets an OC-point voltage or a positive whole-curve offset."""
    return target.oc_voltage_mv is not None or (target.offset_mv is not None and target.offset_mv > 0)


def _reads_of(target: Target) -> int:
    """Reads of one target: the four of XTU's sequence, plus 0x28 when the profile can raise the voltage (``needs_vmax``)."""
    return READS_PER_CPU + (VMAX_EXTRA_READS if needs_vmax(target) else 0)


def _max_writes_of(target: Target) -> int:
    """Writes of one target, worst case: 0x11, and (G-122: only when the profile sets an OC ratio) 0x1D, 0x27; a second 0x11 when ratio and V target could move
    the same way (both set)."""
    both = target.ratio is not None and target.oc_voltage_mv is not None
    return (WRITES_PER_CPU if target.ratio is not None else OFFSET_ONLY_WRITES) + (SPLIT_EXTRA_WRITES if both else 0)


def count_operations(
    targets: int | Sequence[Target], per_core_enabled: bool | None, msr_writes: int = 0, n_points: int = 0, oc_cap_reads: int = 0, ring_points: int = 0
) -> int:
    """Mailbox WRMSRs of a plan (worst case for the targets given): the 0x14 read, per target its reads (0x10, 0x1C, 0x09, 0x26, and 0x28 with an
    OC-point voltage) and writes (0x11, 0x1D, 0x27, and a second 0x11 when ratio and OC-point voltage are both set), plus the 0x15 write unless
    PerCore Voltage Enable is known to be set -- and **one read-back per write** (the guard reads every write back through the same transport;
    T-022 counts them, the guard's own counters do not). ``None`` (unknown) counts the 0x15 write: the conservative worst case.

    ``msr_writes`` (T-023): plain-MSR table writes of the plan; each is one more guard operation (a WRMSR); plain reads and the ``rdmsr`` read-backs
    are not WRMSRs and are not counted.

    ``n_points`` (T-025): CPU x point offset targets of the profile; each is one ``0x10 P2`` read, one ``0x11 P2`` write and its read-back (3 WRMSRs). With
    ``n_points > 0`` the core ``targets`` may be empty (a point-only profile).

    G-117: with no targets, no points and ``msr_writes > 0`` (a turbo-table-only plan) nothing touches the mailbox: no ``0x14`` read, no ``0x15`` write; the
    count is the plain-MSR writes alone.

    ``oc_cap_reads`` (G-137, D-039): the ``0x01`` reads of the plan (one per core kind whose ratio ceiling needs the CPU's own limit; see ``oc_cap_kinds``).

    ``ring_points`` (G-154): ring points of the profile's ``[ring]``; the ring adds the OC word read and one ``0x10 P1=2`` read per point, one ``0x11 P1=2`` write per point and its
    read-back (``2n + 1 + n`` WRMSRs, worst case). A ring-only profile (no cores, no points) has no ``0x14`` read and no ``0x15`` write.

    ``targets`` is a number of plain (offset-only) targets or the ``Target`` objects themselves. G-122: a target that sets no OC ratio writes the 0x11 alone (no 0x1D / 0x27)."""
    if ring_points and n_points == 0 and not isinstance(targets, int) and not tuple(targets):  # G-154: a ring-only profile: no 0x14, no 0x15
        return (ring_points + 1) + 2 * ring_points + msr_writes + oc_cap_reads
    if isinstance(msr_writes, int) and not isinstance(msr_writes, bool) and msr_writes > 0 and n_points == 0 and not isinstance(targets, int):
        try:
            if not tuple(targets):
                return msr_writes + oc_cap_reads
        except TypeError:
            pass
    if isinstance(n_points, bool) or not isinstance(n_points, int) or n_points < 0:
        raise ValueError(f"n_points must be an int >= 0, got {n_points!r}")
    if isinstance(targets, int) and not isinstance(targets, bool):
        if targets < 1:
            raise ValueError(f"n_targets must be an int >= 1, got {targets!r}")
        reads = 1 + READS_PER_CPU * targets
        writes = OFFSET_ONLY_WRITES * targets  # G-122: plain targets set no OC ratio: one 0x11 each
    else:
        try:
            listed = tuple(targets)
        except TypeError:
            listed = ()
        if (not listed and n_points == 0) or not all(isinstance(t, Target) for t in listed):
            raise ValueError(f"count_operations needs a number of targets >= 1 or Target objects, got {targets!r}")
        reads = 1 + sum(_reads_of(t) for t in listed)
        writes = sum(_max_writes_of(t) for t in listed)
    reads += n_points + oc_cap_reads + ((ring_points + 1) if ring_points else 0)
    writes += n_points + ring_points + (0 if per_core_enabled is True else 1)
    return reads + writes + writes + msr_writes


def oc_cap_kinds(policy: Policy, targets: Sequence[Target], table_kinds: Sequence[str] = ()) -> tuple[str, ...]:
    """G-137 / D-049: the core kinds (``"P"``, ``"E"``) whose ``0x01`` limit the plan reads: a kind with a ratio target (or a ``[turbo]`` table, ``table_kinds``) whose CPU limit is not
    yet bound to the policy (``Policy.hw_ratio_max``, e.g. by the helper's ``tune.read``). The limit is the CPU's own bound of every ratio, and an unread limit refuses a raise."""
    need = {t.kind for t in targets if t.ratio is not None} | set(table_kinds)
    return tuple(k for k in ("P", "E") if k in need and policy.hw_ratio_max[k] is None)


def oc_cap_reads_of(policy: Policy, targets: Sequence[Target], profile: Profile | None = None) -> int:
    """Number of ``0x01`` reads of a plan (``oc_cap_kinds``), for ``count_operations(..., oc_cap_reads=...)``."""
    return len(oc_cap_kinds(policy, targets, [MSR_TURBO_KIND[msr] for msr, _ in _turbo_tables(profile)]))


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


def predict_msr(policy: Policy, req: MsrRequest, baseline_word: int | None) -> Verdict:
    """What the guard will answer for the plain-MSR ``req`` (the plain twin of :func:`predict`): CPU set, ``msr_allow`` entry (and its CPUs), then
    for a write the verify CPU (other core kind, covered by a read entry) and ``turbo_ratio_bounded`` on the word read earlier (``baseline_word``) against the CPU's
    ratio limit the ``policy`` is bound to (D-049). Dwell / lock / gates are not predictable here."""
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
        ok, detail = check(req, baseline_word, entry.params, build_msr_context(policy, req.msr))
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
_OC_CAP_READ = (StepKind.READ_OC_CAP, int(Cmd.OC_CAPABILITY_READ), 0, "the CPU's own ratio limit (0x01)")
_VMAX_READ = (StepKind.READ_VMAX, int(Cmd.MAXIMUM_VOLTAGE_LIMIT_READ), 0, "hardware maximum voltage (0x28)")


def read_steps(
    targets: tuple[Target, ...], policy: Policy, profile: Profile | None = None, topology: Topology | None = None
) -> tuple[PlanStep, ...]:
    """The read phase (T-025: ``0x10 P2=point`` of each ``[[point]]`` target follows the ``0x14`` read): with a ``profile`` and ``topology`` (T-023) first the plain ``rdmsr`` steps (``0xCE`` and the ``[turbo]`` tables, or ``0x650``
    for the capped-raise warning) on the first P CPU; then 0x14 on the first target CPU, then per target 0x10, 0x1C, 0x09 P1=0x11 and 0x26 (P2=0 for
    all), and 0x28 when the target sets an OC-point voltage (D-016: the hardware maximum bounds a raised V target). G-137 (D-039): last, ``0x01`` once per core kind
    whose ratio ceiling needs the CPU's own limit and whose limit the policy is not bound to yet (``oc_cap_kinds``): on the first target CPU of the kind, else on the
    first CPU of the kind in ``topology``."""
    steps: list[PlanStep] = []

    def with_oc_cap_reads() -> tuple[PlanStep, ...]:
        kinds = oc_cap_kinds(policy, targets, [MSR_TURBO_KIND[msr] for msr, _ in _turbo_tables(profile)])
        for kind in kinds:
            cpu = next((t.cpu for t in targets if t.kind == kind), None)
            if cpu is None and topology is not None:
                cpu = next((c.cpu for c in topology.cores if c.kind == kind), None)
            if cpu is None:
                continue
            _, cmd, p1, text = _OC_CAP_READ
            req = Request(cpu, cmd, p1, 0, 0, f"apply: read {text} of a {kind} CPU")
            steps.append(PlanStep(len(steps) + 1, StepKind.READ_OC_CAP, cpu, cmd, p1, 0, 0, req.purpose, f"{kind} CPUs", verdict=predict(policy, req, None), core_kind=kind))
        return tuple(steps)

    points = resolve_point_targets(profile, topology) if profile is not None and topology is not None else ()
    ring_targets = resolve_ring_targets(profile, topology) if profile is not None and topology is not None else ()

    def ring_reads() -> None:
        """G-154: the ring OC word (``P2 = 0``) and each ring point of the profile, read through the ring CPU (all P1 = 2)."""
        for p2 in (0, *(t.point for t in ring_targets)):
            req = Request(ring_targets[0].cpu, int(Cmd.VOLTAGE_FREQ_READ), RING_P1, p2, 0,
                          f"apply: read ring {'OC word' if p2 == 0 else f'point {p2}'} (0x10 P1=2 P2={p2}) before any write")
            steps.append(
                PlanStep(len(steps) + 1, StepKind.READ_RING_OC if p2 == 0 else StepKind.READ_RING_POINT, req.cpu, req.cmd, RING_P1, p2, 0, req.purpose,
                         "Ring" if p2 == 0 else f"Ring point {p2}", verdict=predict(policy, req, None))
            )

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
    if not points and not targets:  # G-117: a turbo-table-only plan reads the plain MSRs only (and, G-137, the CPU's ratio limit); no 0x14 (G-154: a ring-only plan reads the ring)
        if ring_targets:
            ring_reads()
        return with_oc_cap_reads()
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
        for kind, cmd, p1, text in (*_CPU_READS, *((_VMAX_READ,) if needs_vmax(t) else ())):
            number += 1
            req = Request(t.cpu, cmd, p1, 0, 0, f"apply: read {text} of {t.label}")
            steps.append(PlanStep(number, kind, t.cpu, cmd, p1, 0, 0, req.purpose, t.label, verdict=predict(policy, req, None), core_kind=t.kind))
    if ring_targets:
        ring_reads()
    return with_oc_cap_reads()


class Warn(str):
    """A plan warning with a machine-readable ``code`` (contract 9 addendum: the result carries ``{code, text}``). Everything untagged is ``NOTE``. A ``str``, so every reader of the
    plain-text warnings (CLI, executor report) keeps working."""

    code: str

    def __new__(cls, code: str, text: str) -> "Warn":
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


#: Warning on every plan with a ``[[point]]`` (T-025). ASCII only.
POINT_CAVEAT = (
    "per-point offsets change one V/F point only (0x11 with P2 = the point; the OC point is the P2 = 0 word): the hardware enforces V(n) >= V(n-1) across "
    "the points of a cluster, arltuner does not check it (the voltages of the fused points are not readable) [COMMUNITY skatterbencher.com]; whether "
    "the hardware accepts a point write at all on Linux is UNVERIFIED (OCCT is reported to write such words); a reboot restores the BIOS-programmed state"
)


def plan_warnings(
    targets: tuple[Target, ...], policy: Policy | None = None, point_targets: tuple[PointTarget, ...] = ()
) -> tuple[str, ...]:
    """Warnings that apply to a plan for ``targets`` (and, T-025, ``point_targets``): the XTU-shape caveat (not for a point-only profile) (0x1D/0x27 and zero-field acceptance are UNVERIFIED on
    Linux) and the cluster caveat for E cores. D-049: there is no policy offset window any more, so no note about one (``policy`` is kept for the callers' signature); the warnings that
    depend on the words (large change, positive / deep offset, turbo table shape) come from :func:`change_notes`."""
    warnings: list[str] = [XTU_SHAPE_CAVEAT] if targets else []  # T-025: a point-only profile sends no 0x1D / 0x27 and no OC-point word
    if any(t.cluster is not None for t in (*targets, *point_targets)):
        warnings.append(_CLUSTER_WARNING)
    if point_targets:
        warnings.append(POINT_CAVEAT)
    return tuple(warnings)


#: D-049 warning codes of :func:`change_notes` (the Review shows them; none is ever a refusal).
LARGE_CHANGE = "LARGE_CHANGE"
POSITIVE_OFFSET = "POSITIVE_OFFSET"
DEEP_WHOLE_CURVE = "DEEP_WHOLE_CURVE"
TURBO_NOT_MONOTONIC = "TURBO_NOT_MONOTONIC"
TURBO_BELOW_BASE = "TURBO_BELOW_BASE"


def change_notes(
    label: str,
    *,
    ratio: tuple[int | None, int | None] = (None, None),
    offset_mv: tuple[float | None, float | None] = (None, None),
    whole_curve: bool = False,
) -> list[tuple[str, str]]:
    """``[(code, text)]`` D-049 warnings for one word ``(before, after)``: ``LARGE_CHANGE`` when the offset moves by more than ``LARGE_OFFSET_CHANGE_MV`` or the ratio by more than
    ``LARGE_RATIO_CHANGE`` (constants of ``guard.policy``, not policy); ``POSITIVE_OFFSET`` when the offset becomes positive (it was not); ``DEEP_WHOLE_CURVE`` when a ``whole_curve``
    offset (the OC word's ``[31:21]``, every point) goes deeper than ``DEEP_WHOLE_CURVE_MV`` -- the CPU's V/F monotonic clamp does not protect a whole-curve offset
    (docs/knowledge/platform-arl-s.md; cpu-rules-audit.md row 4a), so such an offset costs stability without any CPU refusal. A side that is ``None`` is not known: nothing is compared."""
    out: list[tuple[str, str]] = []
    r0, r1 = ratio
    if r0 is not None and r1 is not None and abs(r1 - r0) > LARGE_RATIO_CHANGE:
        out.append((LARGE_CHANGE, f"{label}: ratio {r0} -> {r1} ({r1 - r0:+d}) is a large single change (more than {LARGE_RATIO_CHANGE} bins); the CPU accepts it, take a stability check"))
    o0, o1 = offset_mv
    if o0 is not None and o1 is not None and abs(o1 - o0) > LARGE_OFFSET_CHANGE_MV:
        out.append((LARGE_CHANGE, f"{label}: offset {o0:+.1f} -> {o1:+.1f} mV ({o1 - o0:+.1f}) is a large single change (more than {LARGE_OFFSET_CHANGE_MV} mV); the CPU accepts it, take a stability check"))
    if o1 is not None and o1 > 0 and (o0 is None or o0 <= 0):
        out.append((POSITIVE_OFFSET, f"{label}: positive offset {o1:+.1f} mV raises the voltage"))
    if whole_curve and o1 is not None and o1 < DEEP_WHOLE_CURVE_MV and (o0 is None or o1 < o0):
        out.append((DEEP_WHOLE_CURVE, f"{label}: whole-curve offset {o1:+.1f} mV is deeper than {DEEP_WHOLE_CURVE_MV} mV; the CPU's V/F clamp does not protect a whole-curve offset"))
    return out


def turbo_change_notes(label: str, new: Sequence[int], old: Sequence[int] | None = None, base_ratio: int | None = None) -> list[tuple[str, str]]:
    """``[(code, text)]`` D-049 warnings for one turbo table: ``TURBO_NOT_MONOTONIC`` when a bucket is above the one before it (more active cores should never turbo higher; SPEC-v3 F5, not
    a CPU rule), ``TURBO_BELOW_BASE`` when a bucket is below ``0xCE[15:8]`` (the base ratio), and ``LARGE_CHANGE`` when a bucket moves by more than ``LARGE_RATIO_CHANGE`` bins."""
    out: list[tuple[str, str]] = []
    for k in range(len(new) - 1):
        if new[k] < new[k + 1]:
            out.append((TURBO_NOT_MONOTONIC, f"{label}: bucket {k} ({new[k]}x) is below bucket {k + 1} ({new[k + 1]}x); more active cores usually never turbo higher, the CPU accepts the table"))
            break
    if base_ratio is not None and any(b < base_ratio for b in new):
        out.append((TURBO_BELOW_BASE, f"{label}: a bucket is below the base ratio {base_ratio}x (0xCE[15:8]); the CPU accepts the table"))
    if old is not None and any(abs(n - o) > LARGE_RATIO_CHANGE for n, o in zip(new, old)):
        out.append((LARGE_CHANGE, f"{label}: a bucket moves by more than {LARGE_RATIO_CHANGE} bins; the CPU accepts it, take a stability check"))
    return out


def fill_ratio(base_ratio: int, favored_word: int, top: int | None = None) -> int:
    """The ratio of a core's current OC word: the baseline's non-zero ratio, else ``0x1C[7:0]``; with ``top`` (G-122: the core's top fused ratio, live from its
    ``0x09`` word) that fill is capped at it, so a zero baseline never creates an OC point -- an E core's ``0x1C[7:0]`` can sit above its top fused point, and an OC
    ratio above it adds a point (``[MEASURED lnx 2026-10-11]``). A non-zero baseline ratio is an existing word and is left alone."""
    if base_ratio:
        return base_ratio
    favored = favored_word & 0xFF
    return favored if top is None or not favored else min(favored, top)


def xtu_word(base: VfWord, favored_word: int, fused_word: int, cap: bool = False) -> VfWord:
    """The word XTU writes for a core before any change ``[MEASURED win 2026-10-01]``: the baseline word with ratio = its non-zero ratio,
    else ``0x1C[7:0]``, and V target = its non-zero V target, else the fused ``0x09`` voltage ``[19:8]``; mode and offset are the baseline's.
    (A baseline of ``0x0`` therefore becomes ``ratio | fused V << 8``, e.g. CPU 1: ``0x00053837``.) This is also the *current state* the
    step limits are measured from. D-016: a V target the core already carries is kept -- it is no longer reset to the fused voltage, which
    would silently undo an earlier step of a ramp (``[MEASURED]`` XTU only ever met baselines without a V target).
    G-122: with ``cap`` the filled ratio is capped at the core's top fused ratio (``[7:0]`` of ``fused_word``, ``fill_ratio``): a whole-curve offset on an E core
    must not write its turbo ratio and create an OC point."""
    ratio = fill_ratio(base.ratio, favored_word, fused_top_ratio(fused_word) if cap else None)
    v_target = base.v_target_raw or ((fused_word >> 8) & 0xFFF)
    return base.with_(ratio=ratio, v_target_raw=v_target)


def current_word(readings: CoreReadings, sets_oc: bool = False) -> VfWord:
    """The current OC word of a core (``xtu_word``) for the planner. G-122: ``fill_ratio`` caps a zero baseline's ratio at the core's top fused ratio (an E core's
    ``0x1C[7:0]`` can be above it), so an offset-only write never creates an OC point ([MEASURED lnx 2026-10-11]: an extra point appears); the guard's ratio-step rule
    (``vf_bounded``) measures that cold word from the same cap. A profile that sets an OC ratio or an OC voltage (``sets_oc``) wants the OC point and keeps the XTU fill."""
    return xtu_word(readings.vf, readings.favored, readings.fused, not sets_oc)


def has_oc_point(top: int | None, ratio: int) -> bool:
    """G-120: an OC ratio (the ``P2 = 0`` word) above the core's top fused ratio ``top`` (live, ``[7:0]`` of its ``0x09`` word) creates the OC point; at or below it there is
    none ``[MEASURED lnx 2026-10-11]`` (docs/knowledge/tuning-results.md "First write sittings"). ``top`` unknown (``0x09`` not read): assume there is one (nothing is dropped)."""
    return top is None or ratio > top


def kept_word(top: int | None, word: VfWord) -> VfWord:
    """The word the hardware keeps for ``word`` on a core whose top fused ratio is ``top`` (G-124, ``[MEASURED lnx 2026-10-11/12]``): with no OC point (ratio <= the top fused ratio)
    the V target is always 0, whatever the offset (0xFEC5B439 read back 0xFEC00039; offset 0: 0x0005B439 was kept once, but 0x00000039 when only the 0x11 was
    sent). The planner writes the kept word, so the guard's whole-word read-back compares equal. Only an OC point (ratio above the top fused ratio) carries a target."""
    if word.v_target_raw and not has_oc_point(top, word.ratio):
        return word.with_(v_target_raw=0)
    return word


def _goal_word(target: Target, current: VfWord, top: int | None) -> VfWord:
    """``current`` with the profile's fields applied (``static`` is never in the changes); the V target is 0 when the ratio leaves no OC point (``kept_word``)."""
    try:
        changes: dict[str, int] = {}
        if target.ratio is not None:
            changes["ratio"] = target.ratio
        if target.oc_voltage_mv is not None:
            changes["v_target_raw"] = VfWord.v_to_raw(target.oc_voltage_mv / 1000)
        if target.offset_mv is not None:
            changes["offset_raw"] = VfWord.mv_to_offset_raw(target.offset_mv)
        return kept_word(top, current.with_(**changes))
    except ValueError as exc:
        raise PlanError(f"{target.label}: requested value does not fit the V/F word: {exc}") from exc


def _vf_words(
    current: VfWord, goal: VfWord, top: int | None = None, label: str = "", sends_1d: bool = True, max_ratio_now: int | None = None
) -> tuple[tuple[VfWord, str], ...]:
    """The 0x11 words to send, in order, with their stage label. Ratio and V target moving the same way take two words (registers.md
    "Ordering"): rising -> V target first (the old ratio stays), falling -> ratio first (the old V target stays); the offset moves in the
    second word. Otherwise one word. A goal without an OC point (V target 0, ``kept_word``) is always one word: there is no target to order.
    G-241: when the current word has no OC point (ratio <= the top fused ratio) there is no voltage-first word -- the hardware keeps no target there (G-124,
    ``[MEASURED lnx 2026-10-11/12]``) -- so the OC point is created by ONE word carrying the new ratio, the target and the offset. That is safe: the CPU cannot run
    the new ratio until the ``0x1D`` (and the turbo table) the plan sends after the word, so the voltage rises with the OC point before any frequency can use it
    (P0 58x ``0x0005B43A``, the target landed 3 ms later, G-237 re-read ``[MEASURED lnx 2026-10-19]``). Only a plan that sends no ``0x1D`` after the word
    (``sends_1d`` false) keeps the G-124 refusal."""
    if goal.v_target_raw == 0:
        return ((goal, ""),)
    d_ratio = goal.ratio - current.ratio
    d_v = goal.v_target_raw - current.v_target_raw
    if d_ratio > 0 and d_v > 0:
        if not has_oc_point(top, current.ratio):
            if sends_1d and top is not None and max_ratio_now is not None and max_ratio_now <= top:
                return ((goal, "creates the OC point (ratio + voltage in one word; the ratio is usable only after 0x1D)"),)
            raise PlanError(
                f"{label}: raising the OC ratio and its voltage together needs the voltage first, but at ratio {current.ratio} (no OC point) the hardware drops the "
                f"V target (G-124: a word without an OC point always keeps target 0), so the voltage-first word cannot be verified; raise the ratio alone first "
                f"(the OC point appears at the current V target), then the voltage in a second profile"
            )
        return ((current.with_(v_target_raw=goal.v_target_raw), "1/2 voltage first"), (goal, "2/2 full word"))
    if d_ratio < 0 and d_v < 0:
        return ((current.with_(ratio=goal.ratio), "1/2 frequency first"), (goal, "2/2 full word"))
    return ((goal, ""),)


def _same_state(target: Target, readings: CoreReadings, goal: VfWord, top: int | None) -> bool:
    """G-148: the core already holds what ``target`` asks for, so a plan that writes only what differs (``only_changed``) sends nothing for it: the goal word is the
    word read (and, when the target sets an OC ratio, ``0x1C[7:0]`` is that ratio already), or -- the target sets no OC ratio and no OC voltage -- the core has no OC
    point (ratio field 0, or at most the top fused ratio: G-120/G-124, the hardware keeps no target there) and its offset is the goal's. A cold word ``0`` is
    therefore left as ``0`` (not rewritten as the XTU fill ``0x39`` / ``0x2E``), exactly what the accepted words record after a cold boot."""
    live = readings.vf
    if goal.to_u32() == live.to_u32() and (target.ratio is None or (readings.favored & 0xFF) == goal.ratio):
        return True
    if target.ratio is not None or target.oc_voltage_mv is not None or live.static:
        return False
    no_oc_point = live.ratio == 0 or (top is not None and live.ratio <= top)
    return no_oc_point and live.offset_raw == goal.offset_raw


#: G-148 (decision 5): a one-shot apply used to split into at most this many guarded rounds. D-049 removed the step limits, so every apply is one round; the bound stays for ``check_rounds``.
MAX_RAMP_ROUNDS = 16


def ramp_rounds(
    profile: Profile,
    topology: Topology,
    baseline: Mapping[int, CoreReadings],
    point_words: Mapping[tuple[int, int], int],
    policy: Policy,
    *,
    only_changed: bool = False,
    ring_words: Mapping[int, int] | None = None,
) -> tuple[int, str | None]:
    """D-049: ``(1, None)`` always. The per-operation step limits that made a big change take several guarded rounds are gone (the CPU's own limits judge the final values), so one
    round -- read, write every word, read back -- does it. The signature is kept for the callers (``tune.plan``, the executor)."""
    return 1, None


def check_rounds(rounds: int, worst: str | None, per_round: int, extra_ops: int, policy: Policy, boot_ops_used: int = 0) -> int:
    """The operations ``rounds`` rounds of ``per_round`` (worst case) plus ``extra_ops`` cost; ``NeedsRounds`` when they pass the session cap, this boot's remaining
    budget or :data:`MAX_RAMP_ROUNDS`. One round is always allowed here (the single-plan checks judge it)."""
    total = extra_ops + rounds * per_round
    if rounds <= 1:
        return total
    left = max(policy.max_operations_per_boot - boot_ops_used, 0)
    allowed, where = (policy.max_operations_per_session, "per session") if policy.max_operations_per_session <= left else (left, "left in this boot's budget")
    if rounds > MAX_RAMP_ROUNDS or total > allowed:
        raise NeedsRounds(rounds, worst or "a step limit", total, allowed, MAX_RAMP_ROUNDS, f"{where}, {policy.where}")
    return total


def _kind_top_ratio(policy: Policy, baseline: Mapping[int, CoreReadings], cpu: int) -> int | None:
    """G-151: the top fused ratio of ``cpu``'s core kind -- from its own ``0x09`` word, else from any CPU of the kind the plan read (every P-core carries the same fused table,
    every E cluster another [MEASURED lnx 2026-10-02]); ``None`` when none was read."""
    own = baseline.get(cpu)
    if own is not None:
        return fused_top_ratio(own.fused)
    kind = policy.core_kind(cpu)
    return next((fused_top_ratio(r.fused) for c, r in baseline.items() if policy.core_kind(c) == kind), None)


def _ring_unread(policy: Policy, cpu: int, p2: int) -> str:
    """Why a ring word is missing from the readings: the guard would refuse the read (its own reason), or the point does not exist (rc ``0x11``)."""
    req = Request(cpu, int(Cmd.VOLTAGE_FREQ_READ), RING_P1, p2, 0, "preflight")
    verdict = predict(policy, req, None)
    what = "the ring OC word (P2 = 0)" if p2 == 0 else f"ring point {p2}"
    if not verdict.allowed:
        return f"{what} was not read: the guard refuses the read ({verdict.reason}: {verdict.detail})"
    if p2 == 0:
        return f"{what} was not read"
    return f"{what} does not exist now (cmd 0x10 P1=2 P2={p2} answered rc 0x11), but the accepted state has it"


def _ring_steps(
    ring_targets: tuple[RingTarget, ...],
    ring_words: Mapping[int, int],
    policy: Policy,
    topology: Topology,
    only_changed: bool,
) -> tuple[list[PlanStep], list[str]]:
    """G-154 (D-045): the ring's write steps (``number`` 0; the caller numbers them) and the reasons the ring unit alone is refused. The ring is judged as one unit: any reason drops
    every ring step (the P/E steps of the plan stand), and the reasons are returned, never swallowed. The ring OC word is BIOS-owned: it is compared with the profile's and **never written**."""
    if not ring_targets:
        return [], []
    cpu = ring_targets[0].cpu
    refused: list[str] = []
    live_oc = ring_words.get(0)
    if live_oc is None:
        refused.append(f"ring_oc_unread (planner): {_ring_unread(policy, cpu, 0)}; the ring is not restored")
    elif live_oc != ring_targets[0].oc_word:
        refused.append(
            f"ring_oc_differs (planner): the ring OC word reads {live_oc:#010x}, the accepted state has {ring_targets[0].oc_word:#010x}; it is BIOS-owned and never written, so the "
            f"ring is not restored (a BIOS ring ratio change needs a new accepted state)"
        )
    steps: list[PlanStep] = []
    for t in ring_targets:
        word = ring_words.get(t.point)
        if word is None:
            refused.append(f"ring point {t.point}: {_ring_unread(policy, cpu, t.point)}")
            continue
        base = VfWord.from_u32(word)
        try:
            goal_raw = VfWord.mv_to_offset_raw(t.offset_mv)
            new = base.with_(offset_raw=goal_raw)
        except ValueError as exc:
            raise PlanError(f"ring point {t.point}: requested offset does not fit the V/F word: {exc}") from exc
        ctx = build_context(policy, cpu, favored_word=None, fused_word=None, vf_word_sent=None)
        purpose = f"apply: write ring point {t.point} offset (0x11 P1=2 P2={t.point}): {t.describe()}"
        req = Request(cpu, int(Cmd.VOLTAGE_FREQ_WRITE), RING_P1, t.point, new.to_u32(), purpose)
        verdict = predict(policy, req, word, ctx)
        mismatch = base.ratio != t.expect_ratio
        if only_changed and not mismatch and new.to_u32() == word:
            continue  # the ring point already holds this offset
        if mismatch and verdict.reason in (OK_VERDICT.reason, Refusal.CHECK_FAILED.value):
            verdict = Verdict(
                False,
                Refusal.CHECK_FAILED.value,
                f"expect_ratio (planner): the ratio field read for ring point {t.point} is {base.ratio} (word {word:#010x}), the profile expects {t.expect_ratio}; nothing is sent",
            )
        steps.append(
            PlanStep(0, StepKind.WRITE_RING_POINT, cpu, req.cmd, RING_P1, t.point, req.data, purpose, f"Ring point {t.point}", before=base, after=new, verdict=verdict)
        )
    if not refused and steps:
        numbered = [replace(st, number=i) for i, st in enumerate(steps, start=1)]
        ordered, notes = reorder_writes(numbered, topology, {}, {}, ring_words)  # the same transition-safe rule as a core, on the ring's own curve
        steps = [replace(st, number=0) for st in ordered]
        refused.extend(f"{n}" for n in notes)
    for st in steps:
        if not st.verdict.allowed:
            refused.append(f"ring point {st.p2}: {st.verdict.reason}: {st.verdict.detail}")
    if refused:
        return [], refused
    return steps, []


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
    favored_words: Mapping[int, int] | None = None,
    oc_cap_words: Mapping[int, int] | None = None,
    residual_cpus: Collection[int] = (),
    only_changed: bool = False,
    ring_words: Mapping[int, int] | None = None,
) -> ApplyPlan:
    """Build the full plan from the profile and the **actual** words read. Pure: no I/O, nothing is sent.

    ``oc_cap_words`` (G-137, D-039; cpu -> raw ``0x01`` word): the CPU's own ratio limit as this session read it; the policy used for every ratio ceiling is bound to it
    (``Policy.with_hw_limits``), as the guard's own context is by its baselines. The plan's read steps are still those of the policy as given.

    ``msr_words`` (T-023): the plain MSR words read on the write CPU (``0xCE``, ``0x650``, ``0x1AD``); every ``[turbo]`` table of the profile needs
    its MSR here (else ``PlanError``). The turbo writes are ordered by direction (module docstring).

    ``favored_words`` (cpu -> raw ``0x1C`` word): kept for the callers' signature; since D-049 (no ``"auto"`` ratio ceiling) nothing is derived from it.

    ``point_words`` (T-025): the ``0x10 P2=point`` word read for every ``[[point]]`` target, keyed ``(cpu, point)`` (else ``PlanError``).

    ``residual_cpus`` (G-147): the CPUs whose row is a residual-OC-point neutralisation (``controls.residual``): the word keeps the ratio the CPU already holds, so no
    ``0x1D`` / ``0x27`` is sent for them. G-147 also: a ``0x1D`` is never sent for a word whose ratio is at or below the top fused ratio and below the ``0x1C[7:0]`` read
    before (it would lower the CPU's maximum, ``[MEASURED lnx 2026-10-13]``: ``0x1D`` = 46 took an E cluster from 48x / 4.9 GHz to 46x / 4.7 GHz).

    ``only_changed`` (G-148, D-044: a complete profile, the restore): a word the CPU already holds is not written (a point whose word read equals the goal; a core in the
    same state, ``_same_state``), and without any mailbox write the ``0x15`` step is dropped too. A target whose ``fused_top_ratio`` (a complete profile's record of the
    unit) differs from the top fused ratio of the ``0x09`` word read is refused (``CHECK_FAILED``): the profile was made on another CPU.

    ``ring_words`` (G-154): the ring words read for the profile's ``[ring]``, point -> word, key 0 = the ring OC word (never keyed by a CPU: the ring is read through CPU 0 but is not
    P-core 0). A ring word missing from it (the read was refused, or rc ``0x11``) refuses the ring unit with a named reason; the P/E plan is unaffected.

    ``baseline`` maps every target CPU to its ``CoreReadings`` (0x10, 0x1C, 0x09, 0x26 and, for a profile with an OC-point voltage, 0x28).
    ``per_core_enabled``: ``True`` -> no 0x15 step, ``False`` -> a 0x15 step, ``None`` -> unknown: a conditional 0x15 step is included (worst
    case). ``misc_word`` (optional) is the 0x14 word read earlier; it fixes the 0x15 data (``old | 0x8``) and must agree with
    ``per_core_enabled``. Every step carries a predicted guard ``Verdict``; the plan never raises for a predicted refusal (see
    ``ApplyPlan.require_allowed``). D-049: no step limit, so ``ApplyPlan.next_profile_text`` is always ``None``.
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
    ring_targets = resolve_ring_targets(profile, topology)
    ring_words = {} if ring_words is None else dict(ring_words)
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
    if oc_cap_words:  # bound only after the read steps: they follow the policy the executor ran with
        limits: dict[str, int | None] = {}
        for kind in ("P", "E"):
            words = [w for c, w in oc_cap_words.items() if policy.core_kind(c) == kind]
            if words:
                seen = hw_ratio_max(words)
                known = policy.hw_ratio_max[kind]
                limits[kind] = None if seen is None else seen if known is None else min(seen, known)
        policy = policy.with_hw_limits(limits)
    mailbox = bool(targets or point_targets)  # G-117: False for a turbo-table-only plan (no 0x14, no 0x15, no mailbox word)
    first_cpu = point_targets[0].cpu if point_targets else (targets[0].cpu if targets else 0)
    if mailbox and per_core_enabled is not True:
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
    change_warns: list[tuple[str, str]] = []  # D-049 (code, text) warnings of the words the plan writes; none is a refusal
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
        verdict = predict_msr(policy, mreq, old)
        base_word = msr_words.get(PLATFORM_INFO_MSR)
        change_warns.extend(turbo_change_notes(_TURBO_LABEL[msr], turbo_bytes(new), turbo_bytes(old), None if base_word is None else (base_word >> 8) & 0xFF))
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
    currents: dict[int, VfWord] = {}
    oc_notes: list[str] = []
    raises = False
    segment = len(steps)  # G-151: the point and per-core writes from here on are put in a transition-safe order below
    for pt in point_targets:  # T-025: V/F point offsets, then the per-core words (G-151: reordered below)
        word = point_words[(pt.cpu, pt.point)]
        base = VfWord.from_u32(word)
        try:
            goal_raw = VfWord.mv_to_offset_raw(pt.offset_mv)
            new = base.with_(offset_raw=goal_raw)
        except ValueError as exc:
            raise PlanError(f"{pt.label} point {pt.point}: requested offset does not fit the V/F word: {exc}") from exc
        raises = raises or goal_raw > base.offset_raw
        seen = baseline.get(pt.cpu)  # the facts the guard has of this CPU when the plan is applied: its fused 0x09 / 0x28 words, if the profile also reads the core
        ctx = build_context(
            policy, pt.cpu, favored_word=None, fused_word=None if seen is None else seen.fused, vf_word_sent=None, max_voltage_word=None if seen is None else seen.vmax
        )
        purpose = f"apply: write V/F point {pt.point} offset (0x11 P2={pt.point}) of {pt.label}: {pt.describe()}"
        req = Request(pt.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, pt.point, new.to_u32(), purpose)
        verdict = predict(policy, req, word, ctx)
        mismatch = pt.expect_ratio is not None and base.ratio != pt.expect_ratio
        if only_changed and not mismatch and new.to_u32() == word:
            continue  # G-148: the point already holds this offset
        if mismatch and verdict.reason in (OK_VERDICT.reason, Refusal.CHECK_FAILED.value):
            verdict = Verdict(
                False,
                Refusal.CHECK_FAILED.value,
                f"expect_ratio (planner): the ratio field read for {pt.label} point {pt.point} is {base.ratio} (word {word:#010x}), the profile expects "
                f"{pt.expect_ratio}; nothing is sent",
            )
        elif verdict.allowed and base.ratio > (_kind_top_ratio(policy, baseline, pt.cpu) or base.ratio):
            verdict = Verdict(
                False,
                Refusal.CHECK_FAILED.value,
                f"oc_point_slot (planner): point {pt.point} of {pt.label} reads ratio {base.ratio} (word {word:#010x}), above the top fused ratio: it is the OC point the P2 = 0 word "
                f"creates, never a fused point; set the OC ratio and voltage instead; nothing is sent",
            )
        change_warns.extend(change_notes(f"{pt.label} point {pt.point}", offset_mv=(base.offset_mv, new.offset_mv)))
        number += 1
        steps.append(
            PlanStep(
                number, StepKind.WRITE_VF_POINT, pt.cpu, req.cmd, 0, pt.point, req.data, purpose, f"{pt.label} point {pt.point}",
                before=base, after=new, verdict=verdict, core_kind=pt.kind,
            )
        )
    for t in targets:
        readings = baseline[t.cpu]
        top = fused_top_ratio(readings.fused)
        current = current_word(readings, t.ratio is not None or t.oc_voltage_mv is not None)
        currents[t.cpu] = current
        goal = _goal_word(t, current, top)
        if t.fused_top_ratio is not None and top != t.fused_top_ratio:
            purpose = f"apply: write V/F word (0x11) of {t.label}: {t.describe()}"
            number += 1
            steps.append(
                PlanStep(
                    number, StepKind.WRITE_VF, t.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, 0, goal.to_u32(), purpose, t.label, before=readings.vf, after=goal, core_kind=t.kind,
                    verdict=Verdict(
                        False,
                        Refusal.CHECK_FAILED.value,
                        f"fused_top_ratio (planner): {t.label} reads top fused ratio {top} (0x09 word {readings.fused:#x}), the complete profile was made where it was "
                        f"{t.fused_top_ratio}: another CPU; nothing is sent",
                    ),
                )
            )
            continue
        if only_changed and _same_state(t, readings, goal, top):
            continue  # G-148: the core already holds this state
        if t.oc_voltage_mv is not None and goal.v_target_raw == 0:
            oc_notes.append(
                f"{t.label}: OC-point voltage {t.oc_voltage_mv} mV is not written: ratio {goal.ratio} is at or below the top fused ratio {top}, so there is "
                f"no OC point and the hardware keeps no V target (the word is ratio | offset << 21)"
            )
        raises = raises or goal.ratio > current.ratio or goal.v_target_raw > current.v_target_raw or goal.offset_raw > current.offset_raw
        facts = dict(favored_word=readings.favored, fused_word=readings.fused, max_voltage_word=readings.vmax)
        change_warns.extend(
            change_notes(
                t.label,
                ratio=(current.ratio, goal.ratio) if t.ratio is not None else (None, None),
                offset_mv=(readings.vf.offset_mv, goal.offset_mv) if t.offset_mv is not None else (None, None),
                whole_curve=True,
            )
        )
        words = _vf_words(current, goal, top, t.label, sends_1d=t.ratio is not None and t.cpu not in residual_cpus, max_ratio_now=readings.favored & 0xFF)
        sent: int | None = None
        for word, stage in words:
            ctx = build_context(policy, t.cpu, vf_word_sent=sent, **facts)
            part = f" [{stage}]" if stage else ""
            purpose = f"apply: write V/F word (0x11) of {t.label}{part}: {t.describe()}"
            req = Request(t.cpu, int(Cmd.VOLTAGE_FREQ_WRITE), 0, 0, word.to_u32(), purpose)
            verdict = predict(policy, req, readings.vf.to_u32(), ctx)
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
        if t.ratio is None:
            continue  # G-122: an offset / V-target-only write sends no 0x1D (favored ratio) and no 0x27 (granular): its ratio field is the current one, not a ratio the user set -- 0x1D = 46 would likely cap E turbo at 46
        favored_now = readings.favored & 0xFF
        if t.cpu in residual_cpus:
            continue  # G-147: a neutralised residual OC point rewrites the ratio the CPU already holds: nothing to restate in 0x1D / 0x27
        if top is not None and not has_oc_point(top, goal.ratio) and current.ratio <= goal.ratio < favored_now:
            # G-147: 0x1D is the CPU's maximum ratio, not a copy of the OC word. With no OC point (ratio <= the top fused ratio) a word whose ratio is the one in force (or above it)
            # but still below the 0x1C[7:0] read before restates a fill, and sending it would LOWER the maximum (cpuinfo_max_freq 4.9 -> 4.7 GHz) [MEASURED lnx 2026-10-13: EC2 0x1D = 46
            # with 0x1C[7:0] = 48]. The guard needs 0x1D == the 0x11 ratio, so the pair is not sent. A deliberate lowering below the ratio in force still sends its 0x1D.
            oc_notes.append(
                f"{t.label}: no 0x1D / 0x27 sent: ratio {goal.ratio} is at or below the top fused ratio {top}, not below the ratio in force ({current.ratio}), and below the favored "
                f"ratio {favored_now} (0x1C[7:0]); a 0x1D would lower the CPU's maximum ratio"
            )
            continue
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
    ordered, order_notes = reorder_writes(steps[segment:], topology, baseline, point_words)  # G-151
    new_numbers = {st.number: segment + i for i, st in enumerate(ordered, start=1)}  # ordered keeps the step numbers it was given: the same set, new places
    steps[segment:] = [replace(st, number=new_numbers[st.number]) for st in ordered]
    oc_notes.extend(order_notes)
    ring_steps, ring_refused = _ring_steps(ring_targets, ring_words, policy, topology, only_changed)
    number = len(steps)
    for st in ring_steps:  # G-154: the ring unit after the per-core words, ordered inside itself
        number += 1
        steps.append(replace(st, number=number))
        if st.before is not None and st.after is not None:
            change_warns.extend(change_notes(f"ring point {st.p2}", offset_mv=(st.before.offset_mv, st.after.offset_mv)))
    append_turbo(raising)
    if only_changed and not any(s.kind in (StepKind.WRITE_VF, StepKind.WRITE_VF_POINT, StepKind.WRITE_FAVORED, StepKind.WRITE_GRANULAR) for s in steps):
        # G-148: nothing to write through the mailbox: no 0x15 either (renumbered; G-154: the ring writes need no 0x15)
        kept = [s for s in steps if s.kind is not StepKind.WRITE_PERCORE_ENABLE]
        remap = {s.number: i for i, s in enumerate(kept, start=1)}
        steps = [replace(s, number=remap[s.number]) for s in kept]
    warnings = list(plan_warnings(targets, policy, point_targets))
    warnings.extend(turbo_notes)
    warnings.extend(Warn(code, f"{code}: {text}") for code, text in change_warns)
    warnings.extend(oc_notes)
    warnings.extend(Warn("RING_NOT_RESTORED", f"ring not written: {why}") for why in ring_refused)
    if ring_steps and any(st.after is not None and st.before is not None and st.after.offset_raw > st.before.offset_raw for st in ring_steps):
        raises = True
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
                "entry for the whole cluster"
            )
    if raises:
        warnings.append(RAISE_CAVEAT)
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
        next_profile_text=None,  # D-049: no step limits, so never a next step
        point_targets=point_targets,
        ring_targets=ring_targets,
        ring_refused=tuple(ring_refused),
    )


def _static_problem(policy: Policy, target: Target) -> str | None:
    """What the guard would refuse about a profile value whatever the hardware answers: a number outside a field encoding (D-049: the policy has no value limit, the CPU's own
    limits depend on the words read and are judged after the reads)."""
    if target.offset_mv is not None:
        return _offset_problem(policy, target.cpu, target.offset_mv)
    return None


def _offset_problem(policy: Policy, cpu: int, offset_mv: int) -> str | None:
    """The 11-bit offset field (``-1024..1023`` raw, XTU's ``-1000..+999`` mV) for ``offset_mv``: shared by the OC-point and V/F point offsets. The only offset bound left."""
    try:
        VfWord.mv_to_offset_raw(offset_mv)
    except ValueError:
        return f"offset {offset_mv:+d} mV does not fit the 11-bit offset field (registers.md V/F data word: -1024..1023 raw, XTU range {ENCODING_OFFSET_FLOOR_MV}..+{ENCODING_OFFSET_CEILING_MV} mV)"
    return None


def preflight(profile: Profile, topology: Topology, policy: Policy, *, boot_ops_used: int = 0, extra_ops: int = 0) -> tuple[Target, ...]:
    """Checks that need no hardware read, run before the first request of ``apply``: the worst-case WRMSR count (reads, writes and the
    guard's read-backs) against ``policy.max_operations_per_session`` (``NeedsHigherLimit``) and against what is left of this boot's budget
    ``policy.max_operations_per_boot - boot_ops_used`` (``NeedsBootBudget``; ``boot_ops_used`` = operations the audit log already records
    for this boot), CPU set / allowlist for every read and for the 0x11 / 0x1D / 0x27 writes, and every profile value against the
    policy's explicit ceilings and the offset window (``PlanRefused``). Returns the targets. Sends nothing.

    ``extra_ops`` (G-147): further mailbox operations the run spends before this plan (the restore's residual-OC-point scan reads); added to the worst-case count."""
    if isinstance(boot_ops_used, bool) or not isinstance(boot_ops_used, int) or boot_ops_used < 0:
        raise ValueError(f"boot_ops_used must be a non-negative int, got {boot_ops_used!r}")
    targets = resolve_targets(profile, topology)
    point_targets = resolve_point_targets(profile, topology)
    tables = _turbo_tables(profile)
    ring_targets = resolve_ring_targets(profile, topology)
    n_oc = len(oc_cap_kinds(policy, targets, [MSR_TURBO_KIND[msr] for msr, _ in tables]))
    if isinstance(extra_ops, bool) or not isinstance(extra_ops, int) or extra_ops < 0:
        raise ValueError(f"extra_ops must be a non-negative int, got {extra_ops!r}")
    required = count_operations(targets, None, len(tables), len(point_targets), n_oc, ring_points=len(ring_targets)) + extra_ops
    if required > policy.max_operations_per_session:
        raise NeedsHigherLimit(required, policy.max_operations_per_session)
    remaining = max(policy.max_operations_per_boot - boot_ops_used, 0)
    if required > remaining:
        raise NeedsBootBudget(required, remaining, boot_ops_used, policy.max_operations_per_boot)
    refusals: list[PredictedRefusal] = []
    for step in read_steps(targets, policy, profile, topology):
        if step.kind.is_ring:
            continue  # G-154: a ring read the policy refuses refuses the ring unit alone (build_plan names it); it never stops the P/E restore
        if not step.verdict.allowed:
            refusals.append(PredictedRefusal(step.number, step.cpu, step.cmd, step.verdict.reason, step.verdict.detail, step.msr))
    mailbox = bool(targets or point_targets)  # G-117: a turbo-table-only plan has no 0x14 read and no 0x15 write
    number = (2 if mailbox else 0) + len(point_targets) + sum(_reads_of(t) for t in targets) + len(_msr_read_wants(profile, targets, topology, policy)) + n_oc + (len(ring_targets) + 1 if ring_targets else 0)  # reads are 1..N; the worst case puts the 0x15 write right after
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
        for msr, ratios in tables:  # the plain writes: only the address checks are decidable before the words are read (the CPU's ratio limit needs the 0x01 read)
            number += 1
            entry = policy.match_msr(msr, "write")
            if not policy.cpu_allowed(write_cpu) or entry is None or not entry.covers_cpu(write_cpu):
                why = "cpu not allowed" if entry is not None else "matches no msr_allow write entry (default deny; a plain write needs a reviewed policy)"
                reason = Refusal.CPU_NOT_ALLOWED if entry is not None else Refusal.NOT_ALLOWLISTED
                refusals.append(PredictedRefusal(number, write_cpu, 0, reason.value, f"plain write of {msr:#x}: {why}", msr))
    if refusals:
        raise PlanRefused(tuple(refusals))
    return targets
