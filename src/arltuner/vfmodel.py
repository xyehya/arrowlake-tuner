"""Pure V/F model of an Arrow Lake-S part (SPEC-v3 section 9, F4/F5/F6): monotonic clamp, interpolation, VccIA prediction, turbo-order check.

No I/O, no hardware. Every function takes plain numbers; ``helper/ops_tune.py`` feeds it the measured baseline curve (a ``vf.measure`` record flagged
``baseline``), the words of the last ``tune.read`` and the turbo tables. The results are PREDICTIONS (they carry no more certainty than their inputs).

Facts (``docs/knowledge/platform-285k.md`` sections 3 and 5):
  * F4 monotonic rule: hardware enforces V(n) >= V(n-1); a deeper offset is silently clamped up (P0 BIOS 57x -49.8 mV landed at -42.5 mV, i.e. clamped to
    the 56x voltage) ``[MEASURED lnx 2026-10-02]``. Between two points the VID interpolates linearly (54x = midpoint of 53x/55x, +-2 mV
    ``[MEASURED lnx 2026-10-03]``; E 45x -7.4 of -10 mV ``[MEASURED lnx 2026-10-02]``). Above the top point the bins follow the top point (E 47-48x run on
    point 6) ``[MEASURED lnx 2026-10-02]``.
  * OC point (G-120, ``[MEASURED lnx 2026-10-11]``, ``docs/knowledge/tuning-results.md``): an OC ratio above the top fused ratio adds a point whose adaptive target is
    held up to the top fused point's effective voltage (EC1 48x target 1.117 V < point 6 1.1375 V read back 1.1375 V; target 1.147 V gave 48x 1.1475 V); the bins
    between the top fused point and the OC ratio interpolate linearly (47x = midpoint of 46x and 48x = 1.1425 V).
  * F6 VccIA follows the highest VID ALLOWED for the active-core count (D-036: reported as that VID, no margin and no ring figure are assumed; callers label the
    result a prediction).
  * F5 turbo tables: bucket ratios non-increasing with the active-core count ``[MEASURED lnx 2026-10-01]`` (the guard's ``turbo_ratio_bounded`` demands it).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "E_NO_OFFSET_RATIO",
    "E_WHEA_V",
    "CurvePoint",
    "check_turbo_order",
    "complete_baseline",
    "effective_curve",
    "interpolate",
    "oc_point_voltage",
    "predict_vccia",
    "voltage_at",
]

#: Community report (F8): a negative offset on an E-core 40x point has caused OCCT errors. Warning code ``E40_OFFSET`` only; nothing is blocked.
E_NO_OFFSET_RATIO = 40
#: ``[COMMUNITY skatterbencher.com/2025/02/14/285k-whea-when-e-core-voltage-over-1-2v/]`` E-core WHEA reports above ~1.2 V (F8). A warning threshold only.
E_WHEA_V = 1.2

@dataclass(frozen=True)
class CurvePoint:
    """One fused point after applying offsets. ``base_v`` None = no baseline voltage for this point (``effective_v`` is None too).
    ``clamped_by`` = 1-based index (P2) of the point whose voltage held this one up (F4), else None."""

    index: int
    ratio: int
    base_v: float | None
    offset_mv: float
    effective_v: float | None
    clamped_by: int | None


def interpolate(known: Mapping[int, float | None], ratio: float) -> float | None:
    """Linear interpolation of ``ratio`` between the known ``{ratio: volts}`` entries; flat outside the known range (below the first, above the last).
    ``None`` if nothing is known."""
    pts = sorted((r, v) for r, v in known.items() if v is not None)
    if not pts:
        return None
    if ratio <= pts[0][0]:
        return pts[0][1]
    if ratio >= pts[-1][0]:
        return pts[-1][1]
    for (r0, v0), (r1, v1) in zip(pts, pts[1:]):
        if r0 <= ratio <= r1:
            return v0 + (v1 - v0) * (ratio - r0) / (r1 - r0)
    return None  # unreachable


def complete_baseline(
    point_ratios: Sequence[int], measured: Mapping[int, float | None], fused_top_v: float | None = None
) -> dict[int, float | None]:
    """The stock voltage of every fused point: the measured one where there is one; the top point from the fused ``0x09`` voltage when it was not measured
    (F7: favored 57x often "mismatch"); the rest interpolated between measured neighbours. Points with nothing on either side stay None."""
    known: dict[int, float | None] = {r: measured.get(r) for r in point_ratios}
    top = max(point_ratios)
    if known[top] is None and fused_top_v is not None:
        known[top] = fused_top_v
    have = {r: v for r, v in known.items() if v is not None}
    return {r: (v if v is not None else interpolate(have, r)) for r, v in known.items()}


def effective_curve(point_ratios: Sequence[int], baseline: Mapping[int, float | None], offsets_mv: Mapping[int, float]) -> list[CurvePoint]:
    """Baseline voltage + offset per point, then the F4 clamp: V_eff(n) = max(V_eff(n-1), base(n) + offset(n)) (a point without a known baseline is left
    out of the chain and stays None). ``offsets_mv`` is keyed by ratio. ``clamped_by`` names the point whose (effective) voltage was used."""
    out: list[CurvePoint] = []
    prev_v: float | None = None
    prev_idx: int | None = None
    for n, ratio in enumerate(point_ratios, start=1):
        base = baseline.get(ratio)
        off = float(offsets_mv.get(ratio, 0.0))
        if base is None:
            out.append(CurvePoint(n, ratio, None, off, None, None))
            continue
        want = base + off / 1000.0
        if prev_v is not None and want < prev_v - 1e-9:
            out.append(CurvePoint(n, ratio, base, off, prev_v, prev_idx))
            continue  # prev stays the reference: the clamped value equals it
        out.append(CurvePoint(n, ratio, base, off, want, None))
        prev_v, prev_idx = want, n
    return out


def oc_point_voltage(curve: Sequence[CurvePoint], target_v: float) -> tuple[float, bool] | None:
    """G-120: the effective voltage of an OC point with adaptive ``target_v``: ``max(target, V(top fused point))`` (F4 holds for the OC point too). Returns
    ``(volts, clamped)`` (``clamped`` = the target was below the top fused point and was held up to it), or None when the curve has no known voltage."""
    known = [p.effective_v for p in curve if p.effective_v is not None]
    if not known:
        return None
    top_v = known[-1]
    return (top_v, True) if target_v < top_v - 1e-9 else (target_v, False)


def voltage_at(curve: Sequence[CurvePoint], ratio: float, oc: tuple[int, float] | None = None) -> float | None:
    """The effective voltage at any ratio: interpolated between the clamped points, flat above the top point unless an OC point ``(ratio, v_target)`` is
    given (then linear from the top point to the OC ratio at ``max(v_target, V(top point))``, :func:`oc_point_voltage`; flat above the OC ratio)."""
    known = {p.ratio: p.effective_v for p in curve if p.effective_v is not None}
    if not known:
        return None
    top_r = max(known)
    if oc is not None and oc[0] > top_r and ratio > top_r:
        oc_v = oc_point_voltage(curve, oc[1])
        assert oc_v is not None
        return interpolate({top_r: known[top_r], oc[0]: oc_v[0]}, min(ratio, oc[0]))
    return interpolate(known, ratio)


def predict_vccia(contributors: Sequence[tuple[str, int, float | None]]) -> dict[str, object] | None:
    """F6: the highest requested VID among the units. ``contributors`` = ``(unit, allowed_ratio, volts)``; a unit without a voltage is skipped. Returns
    ``{"predicted_v", "set_by": {"unit", "ratio"}, "margin_v": None}`` or None if no unit has a voltage."""
    rows = [(u, r, v) for u, r, v in contributors if v is not None]
    if not rows:
        return None
    unit, ratio, volts = max(rows, key=lambda row: row[2])
    return {"predicted_v": round(volts, 4), "set_by": {"unit": unit, "ratio": ratio}, "margin_v": None}


def check_turbo_order(buckets: Sequence[int]) -> int | None:
    """F5: index (0-based bucket) of the first bucket whose ratio is HIGHER than the bucket before it (more active cores must never raise the ratio), or
    None if the table is non-increasing."""
    for k in range(1, len(buckets)):
        if buckets[k] > buckets[k - 1]:
            return k
    return None
