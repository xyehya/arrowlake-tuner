"""Strict TOML tuning-profile schema and loader (Contract 6, docs/ARCHITECTURE.md; spec: docs/tasks/T-012).

Standard library only (``tomllib``), no hardware access. A profile is validated into frozen, typed objects;
unknown keys are rejected. The checks here are **unit/type/range sanity only** - safety bounds (how low an
offset or how high a ratio may go) belong to ``guard.Policy`` (T-013), never to the profile.

Profile format::

    [meta]
    name = "example"
    platform = "arl-s-285k"

    [[core]]
    kind = "P"             # "P" | "E"
    index = 4              # P 0..7 ; E 0..15 (XTU numbering)
    ratio = 55             # optional, 1..255 (multiplier; the ratio field [7:0] of the V/F word)
    oc_voltage_mv = 1300   # optional, 500..3999 (adaptive OC-point voltage, mV; the voltage field [19:8], S2.10)
    offset_mv = -30        # optional, -1000..999 (mV)

Turbo ratio tables (T-023, D-017; optional)::

    [turbo]
    e = [49, 49, 49, 49, 49, 49, 49, 49]   # E-core table (MSR 0x650), bucket 0 (= one active core) first; 8 ints 1..255
    p = [57, 57, 55, 55, 55, 55, 55, 55]   # P-core table (MSR 0x1AD); either key or both

The tables are unit/type sanity only here (8 values, 1..255); the bounds (non-zero, non-increasing, ceilings, steps) are the guard's
``turbo_ratio_bounded`` check. A profile with a ``[turbo]`` table still needs at least one ``[[core]]`` or ``[[point]]`` entry.

Per-V/F-point offsets (T-025; optional, distinct from the whole-curve ``offset_mv`` of ``[[core]]``)::

    [[point]]
    kind = "E"             # "P" | "E"
    index = 8              # XTU core numbering, like [[core]]; E entries are cluster-wide (same (cluster, point) must agree)
    point = 6              # a FUSED point only: E 1..6, P 1..8 (the OC point is [[core]] oc_voltage_mv, mailbox P2 = 0)
    offset_mv = -20        # -1000..999 mV; changes this point's offset only (ratio and V target of the point word are kept)
    expect_ratio = 46      # optional, 1..255: the planner refuses if the point's ratio field read from the hardware differs

A profile may hold only ``[[point]]`` entries: it needs at least one ``[[core]]`` or ``[[point]]`` (``[turbo]`` alone is still empty).
Monotonicity V(n) >= V(n-1) across points is enforced by the hardware, not by arltuner (the voltages of fused points are not readable).

Complete profiles (G-148, D-044)::

    [meta]
    name = "my-tuning"
    platform = "arl-s"
    complete = true        # the whole tuning state of the CPU, not a delta

    [[core]]               # exactly one per unit of the CPU (each P-core, each E cluster: the cluster's first E-core)
    kind = "E"
    index = 0
    fused_top_ratio = 46   # the unit's top fused ratio (0x09 P1=0x11 [7:0]) where the profile was made: names the CPU (refused on another one)
    offset_mv = 0          # the OC word's whole-curve offset, explicit even when 0; ratio / oc_voltage_mv absent = no OC point

    [[point]]              # every fused point of the unit, 1..n with expect_ratio, the last one at fused_top_ratio; offset_mv explicit (0 included)

A complete profile carries both turbo tables or neither (``[turbo]`` absent = the tables are not part of this profile and are left alone). Applying one
writes only the words that differ from what the CPU holds (``controls.planner``). Whether every unit of the CPU is present is judged where the profile
meets a topology (:meth:`Profile.check_complete`); the rest is checked here.

The ring (G-154, D-045; optional): the cache ring (mailbox domain P1 = 2, package-level) is one more unit, in its own table::

    [ring]
    oc_word = 0x00000028   # the ring OC word as read (BIOS-owned: recorded and compared, NEVER written; a restore refuses the ring when the live word differs)

    [[ring.point]]         # every ring point 1..n of a complete profile, the OC-created slot (P2 = 9, 40x) included: all are plain offset-only points
    point = 8
    offset_mv = -25        # whole mV, -1000..999
    expect_ratio = 39      # required: the planner refuses the ring if the point's ratio field read differs

A profile without a [ring] table never reads or writes the ring (every state saved before G-154). Planning a complete profile writes only the ring points that differ.

E-cluster rule: the hardware regulator is per cluster of four E-cores (docs/knowledge/platform-arl-s.md section 2;
whether a per-E-core write is cluster-wide is ``[UNVERIFIED]``), so two entries in the same cluster must carry
identical ``ratio/oc_voltage_mv/offset_mv`` and ``Profile.expand`` reports every E entry as cluster-wide.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from arltuner.mailbox.encoding import RATIO_RANGE, RATIO_SOURCE, TURBO_RATIO_RANGE, V_TARGET_RANGE_MV, V_TARGET_SOURCE
from arltuner.topology import KIND_E, KINDS, Topology, UnknownCore

__all__ = [
    "PLATFORM",
    "POINT_RANGE",
    "TURBO_BUCKETS",
    "TurboTables",
    "CoreSetting",
    "CpuSetting",
    "PointCpuSetting",
    "PointSetting",
    "Profile",
    "RingPoint",
    "RingSetting",
    "ProfileError",
    "load_profile",
]

PLATFORM = "arl-s"
#: Profiles written before the platform file became a family file (D-036) carry the old id; they are the same format (the installed accepted profiles do).
LEGACY_PLATFORMS = ("arl-s-285k",)

# Field-encoding ranges (G-141, D-039: no narrower hidden ceiling; the policy and the CPU's own limits are the bounds). RATIO_RANGE / TURBO_RATIO_RANGE and the V target
# range come from arltuner.mailbox.encoding, shared with the policy loader and the helper's intent validation; a refusal names it as the source.
OC_VOLTAGE_MV_RANGE = V_TARGET_RANGE_MV  # adaptive OC-point voltage, mV
OFFSET_MV_RANGE = (-1000, 999)  # mV

_MAX_PROFILE_BYTES = 1 << 20
TURBO_BUCKETS = 8  # buckets of MSR 0x1AD / 0x650, bucket 0 (LSB) = one active core

#: T-025: a ``[[point]]`` names a V/F point by its ``P2`` (1..15, registers.md "Mailbox commands"); how many points a core has is read live (``tune.read``) and judged
#: where the profile meets a CPU (``expect_ratio`` guards the write). Unit/type sanity here, like every range in this file.
POINT_RANGE = (1, 15)
EXPECT_RATIO_RANGE = RATIO_RANGE

_TOP_KEYS = ("meta", "core", "point", "turbo", "ring")
_RING_KEYS = ("oc_word", "point")
_RING_POINT_KEYS = ("point", "offset_mv", "expect_ratio")
_TURBO_KEYS = ("e", "p")
_META_KEYS = ("name", "platform", "complete")
_META_REQUIRED = ("name", "platform")
_CORE_KEYS = ("kind", "index", "ratio", "oc_voltage_mv", "offset_mv", "fused_top_ratio")
_SETTING_KEYS = ("ratio", "oc_voltage_mv", "offset_mv")
_POINT_KEYS = ("kind", "index", "point", "offset_mv", "expect_ratio")

# Core index limits and E-cluster membership depend on the part: a profile is only unit-sane here (index 0..255); the topology it is expanded against decides the rest.
_MAX_INDEX = {kind: 255 for kind in KINDS}


class ProfileError(ValueError):
    """The profile is not valid; the message names the offending table/key."""


@dataclass(frozen=True)
class CoreSetting:
    """One ``[[core]]`` entry. ``None`` means "leave that control alone"."""

    kind: str
    index: int
    ratio: int | None = None
    oc_voltage_mv: int | None = None
    offset_mv: int | None = None
    fused_top_ratio: int | None = None  # G-148: the unit's top fused ratio where a complete profile was made (``None`` in a partial profile)

    def __post_init__(self) -> None:
        _check_setting("CoreSetting", self.kind, self.index, self.ratio, self.oc_voltage_mv, self.offset_mv)
        if self.fused_top_ratio is not None:
            _check_int("CoreSetting", "fused_top_ratio", self.fused_top_ratio, *RATIO_RANGE, f" ({RATIO_SOURCE})")


@dataclass(frozen=True)
class PointSetting:
    """One ``[[point]]`` entry (T-025): the offset of one fused V/F point of a core. ``expect_ratio`` ``None`` = no check of the point's ratio field."""

    kind: str
    index: int
    point: int
    offset_mv: int
    expect_ratio: int | None = None

    def __post_init__(self) -> None:
        where = "PointSetting"
        if not isinstance(self.kind, str):
            raise ProfileError(f"{where}.kind: expected a string 'P' or 'E', got {type(self.kind).__name__}")
        if self.kind not in KINDS:
            raise ProfileError(f"{where}.kind: unknown kind {self.kind!r} (expected 'P' or 'E')")
        _check_int(where, "index", self.index, 0, _MAX_INDEX[self.kind], f" for kind {self.kind!r}")
        _check_int(where, "point", self.point, *POINT_RANGE, " (the OC point is [[core]] oc_voltage_mv)")
        _check_int(where, "offset_mv", self.offset_mv, *OFFSET_MV_RANGE)
        if self.expect_ratio is not None:
            _check_int(where, "expect_ratio", self.expect_ratio, *EXPECT_RATIO_RANGE, f" ({RATIO_SOURCE})")


@dataclass(frozen=True)
class TurboTables:
    """The profile's ``[turbo]`` table: ``e`` (MSR ``0x650``) and/or ``p`` (MSR ``0x1AD``), each 8 ratios, bucket 0 first. ``None`` = leave alone."""

    e: tuple[int, ...] | None = None
    p: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.e is None and self.p is None:
            raise ProfileError("turbo: sets nothing (need e and/or p)")
        for key in _TURBO_KEYS:
            value = getattr(self, key)
            if value is None:
                continue
            if not isinstance(value, (list, tuple)) or len(value) != TURBO_BUCKETS:
                raise ProfileError(f"turbo.{key}: expected a list of {TURBO_BUCKETS} integers, got {value!r}")
            for i, ratio in enumerate(value):
                _check_int("turbo", f"{key}[{i}]", ratio, *TURBO_RATIO_RANGE, f" ({RATIO_SOURCE}, one byte per bucket)")
            object.__setattr__(self, key, tuple(value))


@dataclass(frozen=True)
class RingPoint:
    """One ``[[ring.point]]``: the offset of one ring V/F point (``P2 = point`` of ``0x10`` / ``0x11`` with ``P1 = 2``). ``expect_ratio`` is required: the ring's points are told apart by
    their ratio, and the planner refuses the write when the ratio field read differs."""

    point: int
    offset_mv: int
    expect_ratio: int

    def __post_init__(self) -> None:
        where = "RingPoint"
        _check_int(where, "point", self.point, *POINT_RANGE)
        _check_int(where, "offset_mv", self.offset_mv, *OFFSET_MV_RANGE)
        _check_int(where, "expect_ratio", self.expect_ratio, *EXPECT_RATIO_RANGE, f" ({RATIO_SOURCE})")


@dataclass(frozen=True)
class RingSetting:
    """The ``[ring]`` table (G-154, D-045): the ring OC word as read (``oc_word``, compared with the live word, never written) and the ring points' offsets."""

    oc_word: int
    points: tuple[RingPoint, ...]

    def __post_init__(self) -> None:
        _check_int("RingSetting", "oc_word", self.oc_word, 0, 0xFFFFFFFF)
        object.__setattr__(self, "points", tuple(self.points))
        seen: set[int] = set()
        for i, p in enumerate(self.points):
            if not isinstance(p, RingPoint):
                raise ProfileError(f"ring.point[{i}]: expected a RingPoint, got {type(p).__name__}")
            if p.point in seen:
                raise ProfileError(f"ring.point[{i}]: duplicate ring point {p.point}")
            seen.add(p.point)
        ordered = sorted(self.points, key=lambda p: p.point)
        if any(b.expect_ratio <= a.expect_ratio for a, b in zip(ordered, ordered[1:])):
            raise ProfileError(f"ring: point ratios {[p.expect_ratio for p in ordered]} do not rise")


@dataclass(frozen=True)
class CpuSetting:
    """One entry expanded against a topology.

    ``cpu`` is the Linux CPU of the named core. ``cpus`` is every CPU the setting is expected to reach:
    ``(cpu,)`` for a P-core, the whole cluster of four for an E-core (``cluster_wide=True``). Entries of one
    cluster are identical (schema rule), so a consumer may de-duplicate by ``cluster``.
    """

    kind: str
    index: int
    cpu: int
    cluster: int | None
    cpus: tuple[int, ...]
    cluster_wide: bool
    ratio: int | None
    oc_voltage_mv: int | None
    offset_mv: int | None
    fused_top_ratio: int | None = None


@dataclass(frozen=True)
class PointCpuSetting:
    """One ``[[point]]`` entry expanded against a topology (like :class:`CpuSetting`): ``cpus`` is every CPU it is written to (the whole cluster for E)."""

    kind: str
    index: int
    point: int
    cpu: int
    cluster: int | None
    cpus: tuple[int, ...]
    cluster_wide: bool
    offset_mv: int
    expect_ratio: int | None


@dataclass(frozen=True)
class Profile:
    """A validated profile: a name, ``CoreSetting`` entries, (T-025) ``PointSetting`` entries -- at least one of the two -- and (T-023) optional
    turbo ratio tables. ``complete`` (G-148, D-044): the profile is the whole tuning state of the CPU (module docstring), not a delta."""

    name: str
    entries: tuple[CoreSetting, ...]
    turbo: TurboTables | None = None
    points: tuple[PointSetting, ...] = ()
    complete: bool = False
    ring: RingSetting | None = None  # G-154 (D-045): the ring unit; None = the ring is not part of this profile (never read for a write, never written)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProfileError("meta.name: must be a non-empty string")
        if not isinstance(self.complete, bool):
            raise ProfileError(f"meta.complete: expected true or false, got {self.complete!r}")
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "points", tuple(self.points))
        if self.ring is not None and not isinstance(self.ring, RingSetting):
            raise ProfileError(f"ring: expected a RingSetting, got {type(self.ring).__name__}")
        _check_entries(self.entries, "entries", self.points, turbo_only_ok=self.turbo is not None or self.ring is not None)
        _check_points(self.points, "points")
        if self.turbo is not None and not isinstance(self.turbo, TurboTables):
            raise ProfileError(f"turbo: expected a TurboTables, got {type(self.turbo).__name__}")
        if self.complete:
            _check_complete_shape(self)

    def check_complete(self, topology: Topology) -> None:
        """G-148: a complete profile names every unit of ``topology`` exactly once (one ``[[core]]`` per P-core and per E cluster) and nothing else; ``ProfileError``
        otherwise (a profile made on another CPU layout). The per-unit fused top ratio is judged against the live ``0x09`` word by the planner. No-op for a partial profile."""
        if not self.complete:
            return
        units: dict[str, int] = {}
        for i, e in enumerate(self.entries):
            try:
                unit = topology.unit_of_cpu(topology.resolve(e.kind, e.index).cpu)
            except UnknownCore as exc:
                raise ProfileError(f"complete profile: entries[{i}] {e.kind}-Core {e.index} is not in this CPU's topology ({exc}); it was made on another CPU") from None
            if unit in units:
                raise ProfileError(f"complete profile: unit {unit} appears twice (entries[{units[unit]}] and entries[{i}]); a complete profile has one [[core]] per unit")
            units[unit] = i
        missing = [u for u in topology.unit_ids() if u not in units]
        if missing:
            raise ProfileError(f"complete profile: no [[core]] for {', '.join(missing)}; it was made on another CPU layout (a complete profile names every unit)")

    def check_clusters(self, topology: Topology) -> None:
        """The cluster rule (E entries of one cluster of ``topology`` are identical); ``ProfileError`` otherwise. Done where a profile meets a topology."""
        _check_clusters(self.entries, "entries", topology)
        _check_point_clusters(self.points, "points", topology)

    def expand_points(self, topology: Topology) -> tuple[PointCpuSetting, ...]:
        """Resolve each ``[[point]]`` against ``topology`` (profile order); an E entry applies to its whole cluster like a ``[[core]]`` one."""
        _check_point_clusters(self.points, "points", topology)
        out: list[PointCpuSetting] = []
        for i, e in enumerate(self.points):
            try:
                core = topology.resolve(e.kind, e.index)
                cluster = core.cluster
                cpus = (core.cpu,) if cluster is None else topology.cluster_cpus(cluster)
            except UnknownCore as exc:
                raise ProfileError(f"points[{i}]: {e.kind}-Core {e.index} is not in this topology ({exc})") from None
            out.append(PointCpuSetting(e.kind, e.index, e.point, core.cpu, cluster, cpus, cluster is not None, e.offset_mv, e.expect_ratio))
        return tuple(out)

    def expand(self, topology: Topology) -> tuple[CpuSetting, ...]:
        """Resolve each entry against ``topology`` (profile order).

        P entries apply to their one CPU; every E entry applies to its whole cluster (``cluster_wide=True``,
        four CPUs), because the regulator is per cluster ``[UNVERIFIED whether a write is cluster-wide]``.
        """
        _check_clusters(self.entries, "entries", topology)
        out: list[CpuSetting] = []
        for i, e in enumerate(self.entries):
            try:
                core = topology.resolve(e.kind, e.index)
                cluster = core.cluster
                cpus = (core.cpu,) if cluster is None else topology.cluster_cpus(cluster)
            except UnknownCore as exc:
                raise ProfileError(f"entries[{i}]: {e.kind}-Core {e.index} is not in this topology ({exc})") from None
            out.append(
                CpuSetting(
                    kind=e.kind,
                    index=e.index,
                    cpu=core.cpu,
                    cluster=cluster,
                    cpus=cpus,
                    cluster_wide=cluster is not None,
                    ratio=e.ratio,
                    oc_voltage_mv=e.oc_voltage_mv,
                    offset_mv=e.offset_mv,
                    fused_top_ratio=e.fused_top_ratio,
                )
            )
        return tuple(out)


# --- validation helpers -------------------------------------------------------------------------------------


def _check_int(where: str, key: str, value: object, lo: int, hi: int, note: str = "") -> None:
    if type(value) is not int:  # also rejects bool
        raise ProfileError(f"{where}.{key}: expected an integer, got {type(value).__name__} ({value!r})")
    if not lo <= value <= hi:
        raise ProfileError(f"{where}.{key}: {value} is out of range {lo}..{hi}{note}")


def _check_setting(
    where: str, kind: object, index: object, ratio: object, oc_voltage_mv: object, offset_mv: object
) -> None:
    if not isinstance(kind, str):
        raise ProfileError(f"{where}.kind: expected a string 'P' or 'E', got {type(kind).__name__}")
    if kind not in KINDS:
        raise ProfileError(f"{where}.kind: unknown kind {kind!r} (expected 'P' or 'E')")
    _check_int(where, "index", index, 0, _MAX_INDEX[kind], f" for kind {kind!r}")
    if ratio is not None:
        _check_int(where, "ratio", ratio, *RATIO_RANGE, f" ({RATIO_SOURCE})")
    if oc_voltage_mv is not None:
        _check_int(where, "oc_voltage_mv", oc_voltage_mv, *OC_VOLTAGE_MV_RANGE, f" ({V_TARGET_SOURCE})")
    if offset_mv is not None:
        _check_int(where, "offset_mv", offset_mv, *OFFSET_MV_RANGE)
    if ratio is None and oc_voltage_mv is None and offset_mv is None:
        raise ProfileError(
            f"{where}: {kind}-Core {index} sets nothing (need at least one of ratio, oc_voltage_mv, offset_mv)"
        )


def _values(e: CoreSetting) -> tuple[int | None, int | None, int | None, int | None]:
    return (e.ratio, e.oc_voltage_mv, e.offset_mv, e.fused_top_ratio)


def _describe(e: CoreSetting) -> str:
    return f"ratio={e.ratio}, oc_voltage_mv={e.oc_voltage_mv}, offset_mv={e.offset_mv}"


def _check_entries(entries: tuple[CoreSetting, ...], label: str, points: tuple[PointSetting, ...] = (), turbo_only_ok: bool = False) -> None:
    """Whole-profile rules: non-empty (a ``[[core]]`` or a ``[[point]]``, or -- G-119 -- a ``[turbo]`` table alone), no duplicate (kind, index),
    E entries agree within a cluster."""
    if not entries and not points and not turbo_only_ok:
        raise ProfileError("profile is empty: at least one [[core]] or [[point]] entry is required")
    seen: dict[tuple[str, int], int] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, CoreSetting):
            raise ProfileError(f"{label}[{i}]: expected a CoreSetting, got {type(e).__name__}")
        key = (e.kind, e.index)
        if key in seen:
            raise ProfileError(
                f"{label}[{i}]: duplicate {e.kind}-Core {e.index} (already defined at {label}[{seen[key]}])"
            )
        seen[key] = i


def _check_clusters(entries: tuple[CoreSetting, ...], label: str, topology: Topology) -> None:
    """E entries agree within a cluster of ``topology``."""
    first_in_cluster: dict[int, tuple[int, CoreSetting]] = {}
    for i, e in enumerate(entries):
        if e.kind != KIND_E:
            continue
        try:
            cluster = topology.resolve(KIND_E, e.index).cluster
        except UnknownCore as exc:
            raise ProfileError(f"{label}[{i}]: E-Core {e.index} is not in this topology ({exc})") from None
        assert cluster is not None
        j, first = first_in_cluster.setdefault(cluster, (i, e))
        if _values(first) != _values(e):
            raise ProfileError(
                f"cluster {cluster} disagrees: {label}[{j}] E-Core {first.index} ({_describe(first)}) vs "
                f"{label}[{i}] E-Core {e.index} ({_describe(e)}); E-cores of one cluster share a regulator, "
                "so their entries must be identical"
            )


def _check_complete_shape(profile: Profile) -> None:
    """G-148: the topology-free rules of a complete profile: every ``[[core]]`` has ``fused_top_ratio`` and an explicit ``offset_mv``; the ``[[point]]`` entries of a unit
    are its points 1..n (no gap), each with ``expect_ratio``, ratios rising, the last one at the unit's ``fused_top_ratio`` (so every fused point is present); every point
    belongs to a ``[[core]]`` of the profile; the turbo tables are both present or both absent."""
    cores = {(e.kind, e.index): e for e in profile.entries}
    if not cores:
        raise ProfileError("complete profile: no [[core]] entries (a complete profile has one per unit)")
    by_core: dict[tuple[str, int], list[PointSetting]] = {}
    for p in profile.points:
        if (p.kind, p.index) not in cores:
            raise ProfileError(f"complete profile: [[point]] {p.kind}-Core {p.index} point {p.point} has no [[core]] entry of its own (one per unit)")
        by_core.setdefault((p.kind, p.index), []).append(p)
    for (kind, index), e in cores.items():
        where = f"complete profile: {kind}-Core {index}"
        if e.fused_top_ratio is None:
            raise ProfileError(f"{where}: fused_top_ratio is missing (a complete profile names the top fused ratio of every unit)")
        if e.offset_mv is None:
            raise ProfileError(f"{where}: offset_mv is missing (a complete profile states every offset, 0 included)")
        pts = sorted(by_core.get((kind, index), []), key=lambda p: p.point)
        if not pts:
            raise ProfileError(f"{where}: no [[point]] entries (a complete profile lists every fused point)")
        if [p.point for p in pts] != list(range(1, len(pts) + 1)):
            raise ProfileError(f"{where}: points {[p.point for p in pts]} are not 1..{len(pts)} (a complete profile lists every fused point)")
        ratios = [p.expect_ratio for p in pts]
        if any(r is None for r in ratios):
            raise ProfileError(f"{where}: every point of a complete profile needs expect_ratio")
        if any(b <= a for a, b in zip(ratios, ratios[1:])):  # type: ignore[operator]
            raise ProfileError(f"{where}: point ratios {ratios} do not rise")
        if ratios[-1] != e.fused_top_ratio:
            raise ProfileError(f"{where}: the last point is at ratio {ratios[-1]}, not at the top fused ratio {e.fused_top_ratio} (a fused point is missing)")
    if profile.turbo is not None and (profile.turbo.e is None) != (profile.turbo.p is None):
        raise ProfileError("complete profile: [turbo] has one table; a complete profile carries both (e and p) or neither")
    if profile.ring is not None:
        numbers = sorted(p.point for p in profile.ring.points)
        if not numbers or numbers != list(range(1, len(numbers) + 1)):
            raise ProfileError(f"complete profile: ring points {numbers} are not 1..{len(numbers)} (a complete profile lists every ring point)")


def _check_points(points: tuple[PointSetting, ...], label: str) -> None:
    """``[[point]]`` rules: typed, no duplicate (kind, index, point), E entries of one (cluster, point) agree in ``offset_mv`` / ``expect_ratio``."""
    seen: dict[tuple[str, int, int], int] = {}
    for i, e in enumerate(points):
        if not isinstance(e, PointSetting):
            raise ProfileError(f"{label}[{i}]: expected a PointSetting, got {type(e).__name__}")
        key = (e.kind, e.index, e.point)
        if key in seen:
            raise ProfileError(f"{label}[{i}]: duplicate {e.kind}-Core {e.index} point {e.point} (already defined at {label}[{seen[key]}])")
        seen[key] = i


def _check_point_clusters(points: tuple[PointSetting, ...], label: str, topology: Topology) -> None:
    """E point entries of one (cluster, point) agree in ``offset_mv`` / ``expect_ratio``."""
    first: dict[tuple[int, int], tuple[int, PointSetting]] = {}
    for i, e in enumerate(points):
        if e.kind != KIND_E:
            continue
        try:
            cluster = topology.resolve(KIND_E, e.index).cluster
        except UnknownCore as exc:
            raise ProfileError(f"{label}[{i}]: E-Core {e.index} is not in this topology ({exc})") from None
        assert cluster is not None
        j, ref = first.setdefault((cluster, e.point), (i, e))
        if (ref.offset_mv, ref.expect_ratio) != (e.offset_mv, e.expect_ratio):
            raise ProfileError(
                f"cluster {cluster} point {e.point} disagrees: {label}[{j}] E-Core {ref.index} (offset_mv={ref.offset_mv}, "
                f"expect_ratio={ref.expect_ratio}) vs {label}[{i}] E-Core {e.index} (offset_mv={e.offset_mv}, expect_ratio={e.expect_ratio}); "
                "E-cores of one cluster share a regulator, so their entries for the same point must be identical"
            )


# --- parsing ------------------------------------------------------------------------------------------------


def _reject_unknown(where: str, table: dict[str, object], allowed: tuple[str, ...]) -> None:
    for key in table:
        if key not in allowed:
            raise ProfileError(f"{where}: unknown key {key!r} (allowed: {', '.join(allowed)})")


def _parse_core(i: int, table: object) -> CoreSetting:
    where = f"core[{i}]"
    if not isinstance(table, dict):
        raise ProfileError(f"{where}: expected a table, got {type(table).__name__}")
    _reject_unknown(where, table, _CORE_KEYS)
    for key in ("kind", "index"):
        if key not in table:
            raise ProfileError(f"{where}: missing required key {key!r}")
    kind, index = table["kind"], table["index"]
    ratio, oc, off = (table.get(k) for k in _SETTING_KEYS)
    _check_setting(where, kind, index, ratio, oc, off)
    top = table.get("fused_top_ratio")
    if top is not None:
        _check_int(where, "fused_top_ratio", top, *RATIO_RANGE, f" ({RATIO_SOURCE})")
    return CoreSetting(kind, index, ratio, oc, off, top)  # type: ignore[arg-type]


def _parse_point(i: int, table: object) -> PointSetting:
    where = f"point[{i}]"
    if not isinstance(table, dict):
        raise ProfileError(f"{where}: expected a table, got {type(table).__name__}")
    _reject_unknown(where, table, _POINT_KEYS)
    for key in ("kind", "index", "point", "offset_mv"):
        if key not in table:
            raise ProfileError(f"{where}: missing required key {key!r}")
    try:
        return PointSetting(table["kind"], table["index"], table["point"], table["offset_mv"], table.get("expect_ratio"))
    except ProfileError as exc:
        raise ProfileError(str(exc).replace("PointSetting", where, 1)) from None


def _parse_ring(table: object) -> RingSetting | None:
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ProfileError(f"ring: expected a table, got {type(table).__name__}")
    _reject_unknown("ring", table, _RING_KEYS)
    if "oc_word" not in table:
        raise ProfileError("ring: missing required key 'oc_word' (the ring OC word as read: compared, never written)")
    raw = table.get("point", [])
    if not isinstance(raw, list):
        raise ProfileError("ring.point: expected an array of tables ([[ring.point]]), got a single table")
    points: list[RingPoint] = []
    for i, entry in enumerate(raw):
        where = f"ring.point[{i}]"
        if not isinstance(entry, dict):
            raise ProfileError(f"{where}: expected a table, got {type(entry).__name__}")
        _reject_unknown(where, entry, _RING_POINT_KEYS)
        for key in _RING_POINT_KEYS:
            if key not in entry:
                raise ProfileError(f"{where}: missing required key {key!r}")
        try:
            points.append(RingPoint(entry["point"], entry["offset_mv"], entry["expect_ratio"]))
        except ProfileError as exc:
            raise ProfileError(str(exc).replace("RingPoint", where, 1)) from None
    try:
        return RingSetting(table["oc_word"], tuple(points))
    except ProfileError as exc:
        raise ProfileError(str(exc).replace("RingSetting", "ring", 1)) from None


def _parse_turbo(table: object) -> TurboTables | None:
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ProfileError(f"turbo: expected a table, got {type(table).__name__}")
    _reject_unknown("turbo", table, _TURBO_KEYS)
    return TurboTables(e=table.get("e"), p=table.get("p"))  # type: ignore[arg-type]


def _parse(text: str, topology: Topology | None = None) -> Profile:
    if len(text.encode("utf-8", errors="replace")) > _MAX_PROFILE_BYTES:
        raise ProfileError(f"profile is larger than {_MAX_PROFILE_BYTES} bytes")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"invalid TOML: {exc}") from None
    _reject_unknown("profile", doc, _TOP_KEYS)

    meta = doc.get("meta")
    if meta is None:
        raise ProfileError("missing [meta] table")
    if not isinstance(meta, dict):
        raise ProfileError(f"meta: expected a table, got {type(meta).__name__}")
    _reject_unknown("meta", meta, _META_KEYS)
    for key in _META_REQUIRED:
        if key not in meta:
            raise ProfileError(f"meta: missing required key {key!r}")
    name = meta["name"]
    if not isinstance(name, str) or not name.strip():
        raise ProfileError(f"meta.name: must be a non-empty string, got {name!r}")
    if meta["platform"] != PLATFORM and meta["platform"] not in LEGACY_PLATFORMS:
        raise ProfileError(f"meta.platform: {meta['platform']!r} is not supported (only {PLATFORM!r})")

    cores = doc.get("core")
    raw_points = doc.get("point")
    if raw_points is not None and not isinstance(raw_points, list):
        raise ProfileError("point: expected an array of tables ([[point]]), got a single table")
    if (cores is None or cores == []) and not raw_points and doc.get("turbo") is None and doc.get("ring") is None:
        raise ProfileError("profile is empty: at least one [[core]] or [[point]] entry is required")
    if cores is None:
        cores = []
    if not isinstance(cores, list):
        raise ProfileError("core: expected an array of tables ([[core]]), got a single table")
    entries = tuple(_parse_core(i, table) for i, table in enumerate(cores))
    points = tuple(_parse_point(i, table) for i, table in enumerate(raw_points or []))
    turbo = _parse_turbo(doc.get("turbo"))
    ring = _parse_ring(doc.get("ring"))
    complete = meta.get("complete", False)
    if not isinstance(complete, bool):
        raise ProfileError(f"meta.complete: expected true or false, got {complete!r}")
    _check_entries(entries, "core", points, turbo_only_ok=turbo is not None or ring is not None)
    _check_points(points, "point")
    profile = Profile(name=name, entries=entries, turbo=turbo, points=points, complete=complete, ring=ring)
    if topology is not None:
        profile.check_clusters(topology)
        profile.check_complete(topology)
    return profile


def load_profile(text_or_path: str | os.PathLike[str], topology: Topology | None = None) -> Profile:
    """Load and validate a profile.

    A ``str`` containing a newline is TOML text (every valid profile spans several lines); a single-line
    ``str`` or any ``os.PathLike`` (e.g. ``pathlib.Path``) is a file path, read as UTF-8.
    With a ``topology`` the cluster rule is checked at load; without one it is checked when the profile is expanded against a topology.
    Raises ``ProfileError`` for anything invalid; ``TypeError`` for any other argument type.
    """
    if isinstance(text_or_path, str) and "\n" in text_or_path:
        return _parse(text_or_path, topology)
    if isinstance(text_or_path, (str, os.PathLike)):
        path = Path(text_or_path)
        try:
            if path.stat().st_size > _MAX_PROFILE_BYTES:
                raise ProfileError(f"{path}: profile is larger than {_MAX_PROFILE_BYTES} bytes")
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileError(
                f"cannot read profile file {str(path)!r}: {exc} "
                "(a str without a newline is treated as a path; pass a pathlib.Path or TOML text)"
            ) from exc
        return _parse(text, topology)
    raise TypeError(f"load_profile expects TOML text or a path, got {type(text_or_path).__name__}")
