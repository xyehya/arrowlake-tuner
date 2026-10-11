"""What a profile holds, in the shape of a Tune intent (G-123, contract 9 ``profiles.get``): per unit ``{target, offset, v_target_v, ratio}``.

Pure: no files, no hardware. A saved profile is already an intent (rows over ``P`` / ``E`` groups or units); an installed accepted profile is a
:class:`~arltuner.profiles.schema.Profile` (TOML) whose ``[[point]]`` entries are numbered points, converted here to ``{ratio: mV}`` with the platform's
point ratios (``[MEASURED lnx 2026-10-02]``). A unit with a whole-curve offset *and* point entries becomes ``points`` over every fused ratio (the curve
value first, the point entries over it), the order the planner writes them in.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .schema import Profile

__all__ = ["UNIT_ORDER", "intent_contents", "profile_contents", "summarize"]

UNIT_ORDER: tuple[str, ...] = tuple(f"P{i}" for i in range(8)) + tuple(f"EC{i}" for i in range(4))
_GROUPS = {"P": UNIT_ORDER[:8], "E": UNIT_ORDER[8:]}


def _row(unit: str, offset: dict[str, Any] | None, v: float | None, ratio: int | None) -> dict[str, Any]:
    return {"target": unit, "offset": offset, "v_target_v": v, "ratio": ratio}


def _turbo(profile: Profile) -> dict[str, list[int]] | None:
    if profile.turbo is None:
        return None
    out = {key.upper(): list(getattr(profile.turbo, key)) for key in ("p", "e") if getattr(profile.turbo, key) is not None}
    return out or None


def profile_contents(profile: Profile, topology: Any, unit_of_cpu: Callable[[int], str], point_ratios: Mapping[str, Sequence[int]]) -> dict[str, Any]:
    """``{rows: [{target: unit, offset, v_target_v, ratio}], turbo: {P?, E?} | None}`` in unit order."""
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
        table = point_ratios[p.kind]
        ratio = table[p.point - 1] if 1 <= p.point <= len(table) else p.expect_ratio
        if ratio is not None:
            pts.setdefault(unit, {})[int(ratio)] = p.offset_mv
    rows: list[dict[str, Any]] = []
    for unit in UNIT_ORDER:
        if unit not in curve and unit not in vt and unit not in oc and unit not in pts:
            continue
        offset: dict[str, Any] | None = None
        if unit in pts:
            base = {int(r): curve[unit] for r in point_ratios[kinds[unit]]} if unit in curve else {}
            base.update(pts[unit])
            offset = {"kind": "points", "mv": {str(r): base[r] for r in sorted(base)}}
        elif unit in curve:
            offset = {"kind": "curve", "mv": curve[unit]}
        rows.append(_row(unit, offset, vt.get(unit), oc.get(unit)))
    return {"rows": rows, "turbo": _turbo(profile)}


def intent_contents(intent: Mapping[str, Any]) -> dict[str, Any]:
    """The same shape for a saved ``{scope, rows}`` intent: ``P`` / ``E`` rows are spread over their units."""
    by_unit: dict[str, dict[str, Any]] = {}
    for r in intent.get("rows", []) if isinstance(intent.get("rows"), list) else []:
        target = r.get("target")
        for unit in _GROUPS.get(target, (target,)):
            if unit in UNIT_ORDER:
                by_unit[unit] = _row(unit, r.get("offset"), r.get("v_target_v"), r.get("ratio"))
    turbo = intent.get("turbo")
    return {"rows": [by_unit[u] for u in UNIT_ORDER if u in by_unit], "turbo": turbo if isinstance(turbo, dict) and turbo else None}


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
