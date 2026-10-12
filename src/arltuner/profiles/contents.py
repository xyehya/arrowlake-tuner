"""What a profile holds, in the shape of a Tune intent (G-123, contract 9 ``profiles.get``): per unit ``{target, offset, v_target_v, ratio}``.

Pure: no files, no hardware. A saved profile is already an intent (rows over ``P`` / ``E`` groups or units); an installed accepted profile is a
:class:`~arltuner.profiles.schema.Profile` (TOML) whose ``[[point]]`` entries are numbered points, converted here to ``{ratio: mV}`` with the platform's
point ratios of the last read (live; a point without a known ratio falls back to its ``expect_ratio``). A unit with a whole-curve offset *and* point entries becomes ``points`` over every fused ratio (the curve
value first, the point entries over it), the order the planner writes them in.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from arltuner.curves.record import unit_order

from .schema import Profile

__all__ = ["intent_contents", "profile_contents", "summarize"]


def _row(unit: str, offset: dict[str, Any] | None, v: float | None, ratio: int | None) -> dict[str, Any]:
    return {"target": unit, "offset": offset, "v_target_v": v, "ratio": ratio}


def _turbo(profile: Profile) -> dict[str, list[int]] | None:
    if profile.turbo is None:
        return None
    out = {key.upper(): list(getattr(profile.turbo, key)) for key in ("p", "e") if getattr(profile.turbo, key) is not None}
    return out or None


def profile_contents(profile: Profile, topology: Any, unit_of_cpu: Callable[[int], str], point_ratios: Mapping[str, Sequence[int]] | None = None) -> dict[str, Any]:
    """``{rows: [{target: unit, offset, v_target_v, ratio}], turbo: {P?, E?} | None}`` in unit order. ``point_ratios``: unit id (or core kind) -> its fused point ratios from the last read."""
    point_ratios = point_ratios or {}
    curve: dict[str, int] = {}
    vt: dict[str, float] = {}
    oc: dict[str, int] = {}
    kinds: dict[str, str] = {}
    pts: dict[str, dict[int, int]] = {}
    for s in profile.expand(topology):
        unit = unit_of_cpu(s.cpu)
        kinds[unit] = s.kind
        if s.offset_mv is not None:
            curve[unit] = s.offset_mv
        if s.oc_voltage_mv is not None:
            vt[unit] = round(s.oc_voltage_mv / 1000, 4)
        if s.ratio is not None:
            oc[unit] = s.ratio
    for p in profile.expand_points(topology):
        unit = unit_of_cpu(p.cpu)
        kinds[unit] = p.kind
        table = point_ratios.get(unit) or point_ratios.get(p.kind) or ()
        ratio = table[p.point - 1] if 1 <= p.point <= len(table) else p.expect_ratio
        if ratio is not None:
            pts.setdefault(unit, {})[int(ratio)] = p.offset_mv
    rows: list[dict[str, Any]] = []
    for unit in sorted({*curve, *vt, *oc, *pts}, key=unit_order):
        offset: dict[str, Any] | None = None
        if unit in pts:
            base = {int(r): curve[unit] for r in (point_ratios.get(unit) or point_ratios.get(kinds[unit]) or ())} if unit in curve else {}
            base.update(pts[unit])
            offset = {"kind": "points", "mv": {str(r): base[r] for r in sorted(base)}}
        elif unit in curve:
            offset = {"kind": "curve", "mv": curve[unit]}
        rows.append(_row(unit, offset, vt.get(unit), oc.get(unit)))
    return {"rows": rows, "turbo": _turbo(profile)}


def intent_contents(intent: Mapping[str, Any], unit_ids: Sequence[str]) -> dict[str, Any]:
    """The same shape for a saved ``{scope, rows}`` intent: ``P`` / ``E`` rows are spread over the units of ``unit_ids`` (the live topology's) of that kind."""
    groups = {"P": [u for u in unit_ids if u.startswith("P")], "E": [u for u in unit_ids if u.startswith("EC")]}
    by_unit: dict[str, dict[str, Any]] = {}
    for r in intent.get("rows", []) if isinstance(intent.get("rows"), list) else []:
        target = r.get("target")
        for unit in groups.get(target, (target,)):
            if unit in unit_ids:
                by_unit[unit] = _row(unit, r.get("offset"), r.get("v_target_v"), r.get("ratio"))
    turbo = intent.get("turbo")
    return {"rows": [by_unit[u] for u in sorted(by_unit, key=unit_order)], "turbo": turbo if isinstance(turbo, dict) and turbo else None}


def summarize(contents: Mapping[str, Any]) -> dict[str, Any]:
    """``{units, points, deepest_offset_mv, oc_ratios, v_targets, turbo}`` for the list view and the compare header."""
    deepest: int | None = None
    points = oc = vt = 0
    rows = contents.get("rows", [])
    for r in rows:
        off = r.get("offset")
        if off:
            values = [off["mv"]] if off["kind"] == "curve" else list(off["mv"].values())
            points += len(off["mv"]) if off["kind"] == "points" else 0
            low = min(values)
            deepest = low if deepest is None else min(deepest, low)
        oc += r.get("ratio") is not None
        vt += r.get("v_target_v") is not None
    return {"units": len(rows), "points": points, "deepest_offset_mv": deepest, "oc_ratios": oc, "v_targets": vt, "turbo": bool(contents.get("turbo"))}
