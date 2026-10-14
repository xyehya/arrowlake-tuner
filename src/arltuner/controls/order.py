"""G-151: the write order of one plan is transition-safe -- no ratio's requested voltage dips below what it was before the plan or will be after it.

**The problem** [MEASURED lnx 2026-10-14]: an E cluster split live (an OC point at 48x holding the old 48x voltage, point 6 lowered by 15 mV). One plan that wrote every
point word first and the OC word last ran 48x on the lowered point 6 in between (an E turbo ratio above the top fused point runs on that point's voltage while there is no OC
point): a voltage that was never tested. The same window would open on every cold-boot restore of such an accepted state.

**The rule** (per unit: a P-core, or an E cluster): for every ratio and every prefix of the unit's write sequence, ``V(prefix) >= min(V(before), V(after))``. Writes that raise
a voltage therefore go before the ones that lower it; where that is not enough (two lowering writes that each dip a ratio the other one holds up), the order is searched, and a
unit for which no order keeps the rule is refused (``write_order``), never written in a window.

**The model** (``level``; raw 1/1024 V, relative to the unit's fused curve; the shapes are [MEASURED lnx 2026-10-02/11/12/13], docs/knowledge/platform-arl-s.md section 3):
a ratio at or below the top fused ratio sits on the linear interpolation of the point offsets (the whole-curve offset of the OC word added); above it, without an OC point
(an OC ratio at or below the top fused ratio), it runs flat on the top point's voltage; with an OC point it is linear from the top point to the OC point, and the OC point
runs at ``max(OC target, top point's voltage)`` (the CPU raises a lower target to the top point). Absolute voltages of the lower fused points are not readable and not needed:
only the offsets change. [UNVERIFIED]: whether the OC word's offset field also shifts the OC target (the model leaves the target alone).

Pure functions, no I/O. ``reorder_writes`` is called by ``planner.build_plan`` on the point and per-core write steps.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from arltuner.guard import Refusal
from arltuner.guard.policy import fused_top_ratio
from arltuner.mailbox import VfWord
from arltuner.topology import Topology

from .model import CoreReadings, PlanStep, StepKind, Verdict

__all__ = ["CHECK_NAME", "level", "reorder_writes"]

#: The check name of a refusal for a unit with no transition-safe order.
CHECK_NAME = "write_order"
_SEARCH_LIMIT = 50_000  # states the search may visit per unit; a unit has a handful of write groups, so this is never reached by a real plan

_Key = tuple[str, int]  # ("pt", point) | ("oc", 0)


def level(points: Mapping[int, int], oc: VfWord | None, ratio: int, top: int | None, fused_raw: int) -> Fraction:
    """The voltage of ``ratio`` relative to the fused curve (raw units) for a unit whose known point offsets are ``points`` (point ratio -> offset raw) and whose OC word is
    ``oc``. A ratio below the lowest known point is not modelled (the caller does not ask). See the module docstring."""
    ratios = sorted(points)
    whole = 0 if oc is None or oc.static else oc.offset_raw
    if top is not None and ratio > top:
        top_off = points.get(top, 0)
        v_top = top_off + whole
        if oc is not None and not oc.static and oc.ratio > top:
            v_oc = max(oc.v_target_raw - fused_raw, v_top)
            if ratio >= oc.ratio:
                return Fraction(v_oc)
            return v_top + Fraction((v_oc - v_top) * (ratio - top), oc.ratio - top)
        return Fraction(v_top)
    if not ratios:
        return Fraction(whole)
    if ratio <= ratios[0]:
        return Fraction(points[ratios[0]] + whole)
    if ratio >= ratios[-1]:
        return Fraction(points[ratios[-1]] + whole)
    hi = next(r for r in ratios if r >= ratio)
    lo = max(r for r in ratios if r <= ratio)
    if hi == lo:
        return Fraction(points[lo] + whole)
    return points[lo] + Fraction((points[hi] - points[lo]) * (ratio - lo), hi - lo) + whole


@dataclass
class _Cpu:
    """One CPU's modelled curve while the search applies steps to it."""

    points: dict[int, int]
    oc: VfWord | None
    top: int | None
    fused_raw: int
    ratios: tuple[int, ...]
    before: dict[int, Fraction]  # level of every checked ratio before the plan
    after: dict[int, Fraction]  # ... and after all of the unit's steps
    bound: dict[int, Fraction]

    def snapshot(self) -> tuple[dict[int, int], VfWord | None]:
        return dict(self.points), self.oc

    def restore(self, snap: tuple[dict[int, int], VfWord | None]) -> None:
        self.points, self.oc = dict(snap[0]), snap[1]

    def apply(self, step: PlanStep) -> None:
        if step.after is None:
            return
        if step.kind is StepKind.WRITE_VF_POINT:
            if self.top is None or step.after.ratio <= self.top:  # the slot past the top point is the OC point, never a fused one
                self.points[step.after.ratio] = step.after.offset_raw
        elif step.kind is StepKind.WRITE_VF:
            self.oc = step.after

    def levels(self) -> dict[int, Fraction]:
        return {r: level(self.points, self.oc, r, self.top, self.fused_raw) for r in self.ratios}

    def dips(self) -> bool:
        return any(v < self.bound[r] for r, v in self.levels().items())


def _model(cpu: int, steps: Sequence[PlanStep], baseline: Mapping[int, CoreReadings], point_words: Mapping[tuple[int, int], int]) -> _Cpu | None:
    """The modelled curve of ``cpu`` before the plan, with the bounds the rule needs; ``None`` when the plan gives no point to model."""
    readings = baseline.get(cpu)
    top = None if readings is None else fused_top_ratio(readings.fused)
    fused_raw = 0 if readings is None else (readings.fused >> 8) & 0xFFF
    points: dict[int, int] = {}
    for (c, _), word in point_words.items():
        w = VfWord.from_u32(word)
        if c == cpu and w.ratio and (top is None or w.ratio <= top):
            points[w.ratio] = w.offset_raw
    for st in steps:
        if st.kind is StepKind.WRITE_VF_POINT and st.before is not None and st.before.ratio and (top is None or st.before.ratio <= top):
            points.setdefault(st.before.ratio, st.before.offset_raw)
    oc = None if readings is None else readings.vf
    model = _Cpu(dict(points), oc, top, fused_raw, (), {}, {}, {})
    final = _Cpu(dict(points), oc, top, fused_raw, (), {}, {}, {})
    for st in steps:
        final.apply(st)
    known = sorted(set(model.points) | set(final.points))
    if not known:
        return None
    high = max([known[-1], *(w.ratio for w in (model.oc, final.oc) if w is not None and top is not None and w.ratio > top), top or 0])
    ratios = tuple(range(known[0], high + 1))
    model.ratios = final.ratios = ratios
    model.before = model.levels()
    model.after = final.levels()
    model.bound = {r: min(model.before[r], model.after[r]) for r in ratios}
    return model


def _groups(steps: Sequence[PlanStep]) -> list[tuple[int, _Key, list[PlanStep]]]:
    """The write groups of a segment, in order: ``(cpu, key, steps)``. A point word is one group; the OC word of a CPU with its ``0x11`` stages, ``0x1D`` and ``0x27`` is one."""
    out: dict[tuple[int, _Key], list[PlanStep]] = {}
    for st in steps:
        key: _Key = ("pt", st.p2) if st.kind is StepKind.WRITE_VF_POINT else ("oc", 0)
        out.setdefault((st.cpu, key), []).append(st)
    return [(cpu, key, group) for (cpu, key), group in out.items()]


def _rank(cpus: Mapping[int, _Cpu], members: Sequence[tuple[int, list[PlanStep]]]) -> int:
    """0 = the group raises voltages only, 2 = lowers only, 1 = mixed or no change, judged from the state before the plan."""
    up = down = False
    for cpu, steps in members:
        m = cpus.get(cpu)
        if m is None:
            continue
        snap = m.snapshot()
        for st in steps:
            m.apply(st)
        for r, v in m.levels().items():
            up = up or v > m.before[r]
            down = down or v < m.before[r]
        m.restore(snap)
    return 0 if up and not down else 2 if down and not up else 1


def _search(cpus: dict[int, _Cpu], keys: list[_Key], members: Mapping[_Key, list[tuple[int, list[PlanStep]]]]) -> list[_Key] | None:
    """The first order of ``keys`` (taken in the given preference order) in which no prefix dips a ratio below its bound on any CPU, or ``None``."""
    visited = 0
    snaps = {c: m.snapshot() for c, m in cpus.items()}
    for c, m in cpus.items():
        m.restore(snaps[c])

    def go(done: list[_Key], left: list[_Key]) -> list[_Key] | None:
        nonlocal visited
        if not left:
            return done
        for key in left:
            visited += 1
            if visited > _SEARCH_LIMIT:
                return None
            saved = {c: m.snapshot() for c, m in cpus.items()}
            ok = True
            for cpu, steps in members[key]:
                m = cpus.get(cpu)
                for st in steps:
                    if m is None:
                        continue
                    m.apply(st)
                    if m.dips():
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                found = go([*done, key], [k for k in left if k != key])
                if found is not None:
                    return found
            for c, m in cpus.items():
                m.restore(saved[c])
        return None

    result = go([], list(keys))
    for c, m in cpus.items():
        m.restore(snaps[c])
    return result


def reorder_writes(
    steps: Sequence[PlanStep],
    topology: Topology,
    baseline: Mapping[int, CoreReadings],
    point_words: Mapping[tuple[int, int], int],
) -> tuple[list[PlanStep], list[str]]:
    """``steps`` (the point and per-core write steps of a plan, in the order the planner built them) in a transition-safe order, with the numbers unchanged
    (the caller renumbers): per unit, the steps are placed in the unit's own slots, raising writes before lowering ones and otherwise in the planner's order, a different
    order being searched when the preferred one would dip a ratio. A unit with no safe order keeps its order and every allowed step of it is marked ``CHECK_FAILED``
    (``write_order``). Returns ``(steps, notes)``; ``notes`` names every unit that was refused."""
    out = list(steps)
    notes: list[str] = []
    by_unit: dict[str, list[int]] = {}
    for i, st in enumerate(steps):
        by_unit.setdefault(topology.unit_of_cpu(st.cpu), []).append(i)
    for unit, slots in by_unit.items():
        unit_steps = [steps[i] for i in slots]
        groups = _groups(unit_steps)
        keys: list[_Key] = []
        members: dict[_Key, list[tuple[int, list[PlanStep]]]] = {}
        for cpu, key, group in groups:
            if key not in members:
                keys.append(key)
                members[key] = []
            members[key].append((cpu, group))
        cpus: dict[int, _Cpu] = {}
        for cpu in dict.fromkeys(c for c, _, _ in groups):
            model = _model(cpu, [st for c, _, g in groups if c == cpu for st in g], baseline, point_words)
            if model is not None:
                cpus[cpu] = model
        ranks = {k: _rank(cpus, members[k]) for k in keys}
        preferred = sorted(keys, key=lambda k: (ranks[k], keys.index(k)))
        order = _search(cpus, preferred, members)
        if order is None:
            label = unit_steps[0].label
            why = (
                f"write_order (planner): no order of the writes for {label} keeps every ratio's voltage at or above what it is before and after the plan "
                f"(a window of an untested voltage would open between the writes); split the change into two profiles, raising voltages first"
            )
            notes.append(f"{unit}: {why}")
            for i in slots:
                if out[i].verdict.allowed:
                    out[i] = replace(out[i], verdict=Verdict(False, Refusal.CHECK_FAILED.value, why))
            continue
        placed = [st for key in order for _, g in members[key] for st in g]
        for i, st in zip(slots, placed):
            out[i] = st
    return out, notes
