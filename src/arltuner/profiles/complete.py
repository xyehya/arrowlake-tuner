"""G-148 (D-044): complete profiles from V/F words, and a partial profile laid over a complete one. Pure: no files, no hardware.

A **complete** profile is the whole tuning state of the CPU (:mod:`arltuner.profiles.schema`, ``[meta] complete = true``): per unit (each P-core, each E
cluster) the OC word's fields and the offset of every fused V/F point, plus both turbo tables when they are known. It is derived here from words
(``<cpu> -> [OC word (P2 = 0), point 1, ..., point n]``, the shape of ``tune.read`` / ``EXPECTED-words.txt``) and the top fused ratio of each unit
(``0x09 P1=0x11 [7:0]``, live or from ``fingerprint.json``):

* OC word: an OC point (ratio above the top fused ratio, with an adaptive target) becomes ``ratio`` + ``oc_voltage_mv``; at or below the top fused ratio the
  word is neutral (G-120/G-124: the hardware keeps no target there) and no ratio is recorded. The whole-curve offset is always recorded (``0`` included).
* Points: every point whose ratio is non-zero and at most the top fused ratio, with ``expect_ratio``; the point above it is the one the OC ratio created.
* A value the profile format cannot hold exactly (whole millivolts) is refused (``NOT_REPRESENTABLE``) rather than rounded: the restore would write a
  different word than the one recorded.

:func:`overlay` lays the rows of a partial profile (the Tune editor's edits) over a complete one: what ``profiles.save`` stores.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from arltuner.mailbox import VfWord
from arltuner.topology import Topology

from .schema import CoreSetting, PointSetting, Profile, ProfileError, TurboTables

__all__ = ["BuildError", "complete_from_words", "overlay", "tables_of"]


class BuildError(ValueError):
    """``code`` is ``NO_READ`` | ``NOT_REPRESENTABLE`` | ``NOTHING_TO_ACCEPT``; ``message`` says which unit and why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _whole_mv(raw_mv: float, back: int, what: str) -> int:
    mv = round(raw_mv)
    if VfWord.mv_to_offset_raw(mv) != back:
        raise BuildError("NOT_REPRESENTABLE", f"{what} ({back} raw) is not a whole number of millivolts, so a profile file cannot record it exactly.")
    return mv


def tables_of(tables: Mapping[str, Sequence[int]] | None) -> TurboTables | None:
    """Both turbo tables (``{"e": 8 buckets, "p": 8 buckets}``) as a :class:`TurboTables`, or ``None`` unless both are known (a complete profile holds both or neither)."""
    if tables and all(isinstance(tables.get(k), (list, tuple)) and len(tables[k]) == 8 for k in ("e", "p")):
        return TurboTables(e=tuple(tables["e"]), p=tuple(tables["p"]))
    return None


def complete_from_words(
    topology: Topology,
    name: str,
    words: Mapping[int, Sequence[int]],
    fused_tops: Mapping[int, int | None],
    tables: Mapping[str, Sequence[int]] | None = None,
) -> Profile:
    """The complete profile that puts back ``words`` (cpu -> ``[OC word, point 1, ...]``; a unit is taken from the first of its CPUs that has words) on a CPU whose
    top fused ratio per CPU is ``fused_tops``; ``tables`` (both or the profile has none) become its ``[turbo]``. ``BuildError`` names what is missing or not
    representable."""
    entries: list[CoreSetting] = []
    points: list[PointSetting] = []
    for unit in topology.unit_ids():
        cpu = next((c for c in topology.unit_cpus(unit) if words.get(c)), None)
        if cpu is None:
            raise BuildError("NO_READ", f"the words do not cover {unit}; read the whole system again (Tune > Read).")
        top = fused_tops.get(cpu)
        if not top:
            raise BuildError("NO_READ", f"the top fused ratio of {unit} (0x09 P1=0x11) was not read, so its OC point cannot be told from its fused points.")
        ws = words[cpu]
        core = topology.by_cpu(cpu)
        oc = VfWord.from_u32(int(ws[0]))
        if oc.static:
            raise BuildError("NOT_REPRESENTABLE", f"{unit} is in static (override) voltage mode; only adaptive words are recorded.")
        values: dict[str, int] = {}
        if oc.v_target_raw and oc.ratio > top:  # an OC point: ratio and adaptive target are the setting; at or below the top fused ratio the word is neutral
            mv = round(oc.v_target_v * 1000)
            if VfWord.v_to_raw(mv / 1000) != oc.v_target_raw:
                raise BuildError("NOT_REPRESENTABLE", f"{unit}'s OC V target ({oc.v_target_raw} raw) is not a whole number of millivolts.")
            values["ratio"], values["oc_voltage_mv"] = oc.ratio, mv
        values["offset_mv"] = _whole_mv(oc.offset_mv, oc.offset_raw, f"{unit} OC word offset")
        entries.append(CoreSetting(core.kind, core.index, fused_top_ratio=top, **values))
        for n, word in enumerate(ws[1:], start=1):
            w = VfWord.from_u32(int(word))
            if w.ratio == 0 or w.ratio > top:
                continue  # an empty word, or the point the OC ratio created (above the top fused ratio): not a fused point
            points.append(PointSetting(core.kind, core.index, n, _whole_mv(w.offset_mv, w.offset_raw, f"{unit} point {n} offset"), w.ratio))
    try:
        return Profile(name, tuple(entries), tables_of(tables), tuple(points), complete=True)
    except ProfileError as exc:
        raise BuildError("NOT_REPRESENTABLE", f"the words are not a complete tuning state ({exc}).") from None


def overlay(base: Profile, edits: Profile, topology: Topology, name: str | None = None) -> Profile:
    """``base`` (complete) with the rows of ``edits`` (partial) laid over it, per unit: a field ``edits`` sets replaces the base value of that unit's ``[[core]]``,
    a ``[[point]]`` of ``edits`` replaces that point's offset (the base keeps its ``expect_ratio``), a turbo table of ``edits`` replaces the base one. ``ProfileError``
    if ``edits`` names a unit or point that ``base`` does not have (the edits were made against another read)."""
    if not base.complete:
        raise ProfileError("overlay: the base profile is not complete")

    def unit_of(kind: str, index: int) -> str:
        return topology.unit_of_cpu(topology.resolve(kind, index).cpu)

    cores = {unit_of(e.kind, e.index): e for e in base.entries}
    for e in edits.entries:
        unit = unit_of(e.kind, e.index)
        if unit not in cores:
            raise ProfileError(f"overlay: {unit} is not in the base profile")
        changes = {k: getattr(e, k) for k in ("ratio", "oc_voltage_mv", "offset_mv") if getattr(e, k) is not None}
        cores[unit] = dataclasses.replace(cores[unit], **changes)
    pts = {(unit_of(p.kind, p.index), p.point): p for p in base.points}
    for p in edits.points:
        key = (unit_of(p.kind, p.index), p.point)
        if key not in pts:
            raise ProfileError(f"overlay: {key[0]} has no fused point {p.point} in the base profile")
        pts[key] = dataclasses.replace(pts[key], offset_mv=p.offset_mv)
    turbo = base.turbo
    if edits.turbo is not None:
        e_tab = edits.turbo.e if edits.turbo.e is not None else (None if turbo is None else turbo.e)
        p_tab = edits.turbo.p if edits.turbo.p is not None else (None if turbo is None else turbo.p)
        turbo = TurboTables(e=e_tab, p=p_tab)
    return Profile(name or base.name, tuple(cores.values()), turbo, tuple(pts.values()), complete=True)
