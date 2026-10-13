"""G-147: a residual OC point is neutralised, not left alone, and recognised as equivalent when it cannot be cleared.

**The problem** [MEASURED lnx 2026-10-13]: the OC word of a core (``0x10`` / ``0x11`` ``P2 = 0``) survives a warm reboot, and a ratio cannot be written back to 0 at
runtime (only a cold boot clears it; docs/knowledge/platform-arl-s.md section 3). A unit that holds an OC ratio above its top fused ratio while the accepted state has no
OC point therefore keeps an OC point with the V target the earlier sitting left in it (an E cluster: 48x with 1.137 V instead of the 1.1125 V the accepted tuning runs).

**The fix**: write the closest equivalent word -- the ratio the CPU already holds (it cannot be removed), the accepted whole-curve offset, and an adaptive target **below
the accepted top fused point voltage**. An OC target below the top fused point is clamped up to it [MEASURED lnx 2026-10-11, 2026-10-13: target 1.100 V read back
``0x00046630``, 48x measured 1.1125 V = the accepted point], so the unit runs the accepted voltage. Nothing in the guard is widened: the word keeps the ratio read (no
raise, never 0), lowers the target, and every step limit / ceiling still applies; a plan the guard would refuse is dropped (``executor``) and the unit is reported as
it is.

**The target rule** (``neutral_target``): ``T = min(fused V, fused V + accepted offset of the top fused point) - 25 mV``, but never lower than one step limit below the
V target the CPU holds (a bigger step is refused by the guard) and never above the fused V. The top fused voltage is the live ``0x09`` ``[19:8]`` (raw S2.10; offset
and target share the 1/1024 V unit). Why not "fused - 100 mV" in every case: with an accepted top-point offset deeper than -100 mV that target would sit *above* the
accepted point and the unit would run above the tested voltage; and a 100 mV step is refused by the default 20 mV step limit. Because the hardware clamps, the exact value
does not matter as long as it is below the effective top point.

**The compare rule** (``classify``): a unit is EQUIVALENT (``RESIDUAL_NOTE``) when the accepted word has no OC point, the live word has an OC ratio above the top fused
ratio, the offset field equals the accepted one, the target is at most the effective top point (fused V + the accepted top-point offset), every other point matches
and the one extra point the OC ratio creates has exactly that ratio. Anything else stays a difference.

Pure functions and data, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from arltuner.guard import Policy, Request
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox import Cmd, VfWord
from arltuner.profiles import CoreSetting, Profile, ProfileError
from arltuner.topology import Topology

from .model import PlanStep, StepKind
from .planner import _FUSED_P1, predict

__all__ = [
    "RESIDUAL_NOTE",
    "SCAN_READS_PER_UNIT",
    "TARGET_MARGIN_MV",
    "Neutralisation",
    "ResidualUnit",
    "classify",
    "merged_rows",
    "neutral_target",
    "neutralise",
    "scan_cpus",
    "scan_plan",
    "scan_steps",
]

#: The text of an EQUIVALENT verdict (restore result, ``restore_tuning.sh``, the Windows script, the Dashboard comparison).
RESIDUAL_NOTE = "residual OC point (cleared by a cold boot); voltages as accepted"
#: The target is this far below the accepted top fused point voltage (the hardware clamps it up to that point; the margin only has to keep it below).
TARGET_MARGIN_MV = 25
#: The scan reads ``0x10`` and ``0x09 P1=0x11`` on one CPU of every unit that is not set to an OC ratio by the accepted state.
SCAN_READS_PER_UNIT = 2


@dataclass(frozen=True)
class ResidualUnit:
    """One unit (P-core or E cluster) whose live OC word is a residual OC point that this plan rewrites."""

    unit: str  # "P3" / "EC1"
    cpu: int  # the CPU that was read
    cpus: tuple[int, ...]  # every CPU of the unit (an E cluster is written on all four)
    ratio: int  # the OC ratio the CPU holds (kept)
    target_before: int  # raw V target held
    target_raw: int  # raw V target written
    target_mv: int  # the same as a whole-mV profile value
    top_fused_ratio: int

    def describe(self) -> str:
        return (
            f"{self.unit}: residual OC point {self.ratio}x with V target {self.target_before / 1024:.4f} V (cleared by a cold boot only); rewritten with target "
            f"{self.target_raw / 1024:.4f} V, below the top fused point, so the unit runs the accepted voltages"
        )


@dataclass(frozen=True)
class Neutralisation:
    profile: Profile  # the profile with one ``[[core]]`` row per residual unit added (or replaced)
    units: tuple[ResidualUnit, ...]
    notes: tuple[str, ...]  # why a unit was left alone, and what was done

    @property
    def cpus(self) -> frozenset[int]:
        return frozenset(c for u in self.units for c in u.cpus)


# --------------------------------------------------------------------------------------------------------------
# units and the accepted rows
# --------------------------------------------------------------------------------------------------------------
def _unit_rep(topology: Topology) -> dict[str, tuple[str, int, int]]:
    """unit id -> (kind, XTU index of its first core, the CPU to read): one entry per P-core and per E cluster."""
    out: dict[str, tuple[str, int, int]] = {}
    for unit in topology.unit_ids():
        cpu = topology.unit_cpus(unit)[0]
        core = topology.by_cpu(cpu)
        out[unit] = (core.kind, core.index, cpu)
    return out


def merged_rows(profiles: Sequence[Profile], topology: Topology) -> dict[str, dict[str, int | None]]:
    """unit id -> ``{ratio, oc_voltage_mv, offset_mv}`` of ``profiles`` applied in order (a later row overrides the fields it sets), as the restore merges the accepted chain."""
    rows: dict[str, dict[str, int | None]] = {}
    for prof in profiles:
        for c in prof.entries:
            unit = topology.unit_of_cpu(topology.resolve(c.kind, c.index).cpu)
            cur = rows.setdefault(unit, {"ratio": None, "oc_voltage_mv": None, "offset_mv": None})
            for key in ("ratio", "oc_voltage_mv", "offset_mv"):
                value = getattr(c, key)
                if value is not None:
                    cur[key] = value
    return rows


def _top_offsets(profiles: Sequence[Profile], topology: Topology) -> dict[tuple[str, int], int]:
    """(unit id, point ratio) -> accepted offset in mV of the ``[[point]]`` rows that name that ratio (``expect_ratio``); a later profile replaces an earlier one."""
    out: dict[tuple[str, int], int] = {}
    for prof in profiles:
        for p in prof.points:
            if p.expect_ratio is None:
                continue
            unit = topology.unit_of_cpu(topology.resolve(p.kind, p.index).cpu)
            out[(unit, p.expect_ratio)] = p.offset_mv
    return out


def scan_cpus(profile: Profile, accepted_before: Sequence[Profile], topology: Topology) -> tuple[int, ...]:
    """The CPU to read in every unit for which the accepted state (``accepted_before`` + ``profile``) sets no OC ratio and no OC voltage: those are the units a residual OC
    point could sit in. A unit with an OC ratio in the accepted state is left to the normal plan."""
    rows = merged_rows([*accepted_before, profile], topology)
    out: list[int] = []
    for unit, (_, _, cpu) in _unit_rep(topology).items():
        row = rows.get(unit)
        if row is not None and (row["ratio"] is not None or row["oc_voltage_mv"] is not None):
            continue
        out.append(cpu)
    return tuple(out)


def scan_steps(cpus: Sequence[int], topology: Topology, policy: Policy, first_number: int = 1) -> tuple[PlanStep, ...]:
    """The read steps of the scan: ``0x10`` (P2 = 0) and ``0x09 P1=0x11 P2=0`` of each CPU, with the policy's predicted verdict (executed by the executor like any read)."""
    steps: list[PlanStep] = []
    number = first_number
    for cpu in cpus:
        core = topology.by_cpu(cpu)
        label = topology.xtu_name(core)
        for kind, cmd, p1, text in (
            (StepKind.READ_VF, int(Cmd.VOLTAGE_FREQ_READ), 0, "OC word (0x10)"),
            (StepKind.READ_FUSED, int(Cmd.VOLTAGE_FLOOR_READ), _FUSED_P1, "fused V/F point (0x09 P1=0x11)"),
        ):
            req = Request(cpu, cmd, p1, 0, 0, f"restore scan: read {text} of {label} to find a residual OC point")
            steps.append(PlanStep(number, kind, cpu, cmd, p1, 0, 0, req.purpose, label, verdict=predict(policy, req, None), core_kind=core.kind))
            number += 1
    return tuple(steps)


def scan_plan(profile: Profile, accepted_before: Sequence[Profile], topology: Topology, policy: Policy) -> tuple[PlanStep, ...]:
    """The scan reads that will be sent: ``scan_steps`` of ``scan_cpus``, minus the CPUs whose reads the policy does not allow (those units are not scanned)."""
    cpus = scan_cpus(profile, accepted_before, topology)
    steps = scan_steps(cpus, topology, policy)
    skipped = {st.cpu for st in steps if not st.verdict.allowed}
    # numbered -1, -2, ...: they are no steps of the plan, and a report keyed by step number (``tune.apply`` rows) must not mistake them for one
    return tuple(replace(st, number=-i) for i, st in enumerate((st for st in steps if st.cpu not in skipped), start=1))


# --------------------------------------------------------------------------------------------------------------
# the target
# --------------------------------------------------------------------------------------------------------------
def _mv_at_most(raw: int) -> int:
    """The largest whole-mV profile value whose raw V target (``round(mV * 1.024)``) is at most ``raw``."""
    mv = int(raw * 1000 / 1024)
    while VfWord.v_to_raw(mv / 1000) > raw:
        mv -= 1
    while VfWord.v_to_raw((mv + 1) / 1000) <= raw:
        mv += 1
    return mv


def neutral_target(fused_word: int, top_offset_mv: int, held_raw: int, max_v_step_raw: int) -> tuple[int, int] | None:
    """``(raw target, whole-mV profile value)`` of the neutralised word, or ``None`` when no target below the fused top point is reachable in one step (the module docstring
    has the rule). ``held_raw`` = the V target the CPU holds now (0 = none: the step is then measured from the fused voltage, as the guard does)."""
    fused_raw = (fused_word >> 8) & 0xFFF
    if fused_raw == 0:
        return None
    top_off_raw = VfWord.mv_to_offset_raw(top_offset_mv)
    margin = VfWord.v_to_raw(TARGET_MARGIN_MV / 1000)
    wanted = min(fused_raw, fused_raw + top_off_raw) - margin
    floor = (held_raw or fused_raw) - max_v_step_raw  # the guard refuses a bigger step
    want = max(wanted, floor, 1)
    if want > fused_raw:
        return None
    mv = _mv_at_most(want)
    raw = VfWord.v_to_raw(mv / 1000)
    if raw < floor or raw < 1 or raw > fused_raw:
        return None
    return raw, mv


# --------------------------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------------------------
def neutralise(
    profile: Profile,
    accepted_before: Sequence[Profile],
    topology: Topology,
    policy: Policy,
    vf_words: Mapping[int, int],
    fused_words: Mapping[int, int],
) -> Neutralisation:
    """``profile`` plus one ``[[core]]`` row per unit whose live OC word (``vf_words``, cpu -> ``0x10``) is a residual OC point against the accepted state
    (``accepted_before`` + ``profile``) with the top fused ratio of ``fused_words`` (cpu -> ``0x09``). A unit that is already neutral, whose word is static, whose
    ratio the policy forbids, or whose target is not reachable in one step is left alone (and named in ``notes``)."""
    chain = [*accepted_before, profile]
    rows = merged_rows(chain, topology)
    tops = _top_offsets(chain, topology)
    reps = _unit_rep(topology)
    new_rows: dict[str, CoreSetting] = {}
    units: list[ResidualUnit] = []
    notes: list[str] = []
    for unit, (kind, index, cpu) in reps.items():
        if cpu not in vf_words or cpu not in fused_words:
            continue
        live = VfWord.from_u32(vf_words[cpu])
        top = fused_top_ratio(fused_words[cpu])
        if top is None or live.static or live.ratio <= top:
            continue  # no OC point
        row = rows.get(unit) or {"ratio": None, "oc_voltage_mv": None, "offset_mv": None}
        if row["ratio"] is not None or row["oc_voltage_mv"] is not None:
            continue  # the accepted state sets an OC point here (it is not residual)
        offset_mv = row["offset_mv"] or 0
        offset_raw = VfWord.mv_to_offset_raw(offset_mv)
        fused_raw = (fused_words[cpu] >> 8) & 0xFFF
        top_off_mv = tops.get((unit, top), 0)
        bound = fused_raw + VfWord.mv_to_offset_raw(top_off_mv)
        if live.offset_raw == offset_raw and live.v_target_raw <= bound:
            continue  # already neutral: nothing to write
        cap = policy.ratio_max[kind]
        if cap is not None and live.ratio > cap:
            notes.append(f"{unit}: residual OC point {live.ratio}x is above the policy ratio_max {cap}; left as it is (a restore never widens the policy)")
            continue
        target = neutral_target(fused_words[cpu], top_off_mv, live.v_target_raw, v_step_raw(policy))
        if target is None:
            notes.append(f"{unit}: residual OC point {live.ratio}x with V target {live.v_target_raw / 1024:.4f} V cannot be brought below the top fused point in one step of the policy; left as it is")
            continue
        raw, mv = target
        try:
            new_rows[unit] = CoreSetting(kind, index, ratio=live.ratio, oc_voltage_mv=mv, offset_mv=offset_mv)
        except ProfileError as exc:
            notes.append(f"{unit}: residual OC point {live.ratio}x not neutralised: {exc}")
            continue
        unit_cpus = topology.unit_cpus(unit)
        res = ResidualUnit(unit, cpu, unit_cpus, live.ratio, live.v_target_raw, raw, mv, top)
        units.append(res)
        notes.append(res.describe())
    if not new_rows:
        return Neutralisation(profile, (), tuple(notes))
    kept = [e for e in profile.entries if topology.unit_of_cpu(topology.resolve(e.kind, e.index).cpu) not in new_rows]
    entries = (*kept, *new_rows.values())
    return Neutralisation(Profile(profile.name, entries, profile.turbo, profile.points), tuple(units), tuple(notes))


def v_step_raw(policy: Policy) -> int:
    """The policy's per-operation V target step limit in raw units (what the guard measures a target change against)."""
    return VfWord.v_to_raw(policy.max_v_step_mv / 1000)


# --------------------------------------------------------------------------------------------------------------
# the compare
# --------------------------------------------------------------------------------------------------------------
def classify(fused_word: int | None, want: Sequence[int], got: Sequence[int | None]) -> dict[str, object] | None:
    """EQUIVALENT test of one unit: ``want`` = the accepted words (``[OC word, point 1, ... point n]``), ``got`` = the live ones. Returns ``{"ratio": R, "extra_point": n}``
    when the unit differs from the accepted state only by a residual OC point as described in the module docstring (``extra_point`` = the index of the point the OC ratio
    created, or ``None`` when the live table does not show it), else ``None``. Without the unit's live ``0x09`` word nothing is judged equivalent."""
    if not want or not got or got[0] is None or fused_word is None:
        return None
    top = fused_top_ratio(fused_word)
    fused_raw = (fused_word >> 8) & 0xFFF
    if top is None or fused_raw == 0:
        return None
    exp, live = VfWord.from_u32(want[0]), VfWord.from_u32(got[0])
    if exp.static or live.static or live.ratio <= top or exp.ratio > top:
        return None  # no residual OC point, or the accepted state has one
    if exp.v_target_raw not in (0, fused_raw) or live.offset_raw != exp.offset_raw:
        return None
    n = len(want) - 1
    top_off = 0
    for word in want[1:]:
        w = VfWord.from_u32(word)
        if w.ratio == top:
            top_off = w.offset_raw  # the accepted offset of the top fused point
    if live.v_target_raw > fused_raw + top_off:
        return None  # the target runs above the accepted top point: the unit would not run the accepted voltage
    for i in range(1, n + 1):
        if (got[i] if i < len(got) else None) != want[i]:
            return None
    extra = None
    if len(got) > n + 1:
        if len(got) != n + 2 or got[n + 1] is None or VfWord.from_u32(got[n + 1]).ratio != live.ratio:
            return None
        extra = n + 1
    return {"ratio": live.ratio, "extra_point": extra}
