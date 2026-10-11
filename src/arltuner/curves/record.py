"""The curve record (contract 9) and its small helpers: unit names, ids, names, the list summary and the 64 KiB fit.

Units (SPEC-v3 preamble, ``[MEASURED]`` one regulator per cluster of four): 12 tuning units = P-cores ``P0``..``P7`` (CPU n) and E-clusters ``EC0``..``EC3``
(CPUs 8-11, 12-15, 16-19, 20-23). A record is plain JSON-able data; nothing here touches the filesystem.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

__all__ = ["MAX_NAME", "QUICK_RATIOS", "RECORD_BUDGET", "UNITS", "fit_record", "is_curve_id", "new_curve_id", "summary", "unit_cpus", "unit_kind", "validate_name"]

#: unit -> CPUs, in display order.
UNITS: dict[str, tuple[int, ...]] = {
    **{f"P{n}": (n,) for n in range(8)},
    **{f"EC{n}": tuple(range(8 + 4 * n, 12 + 4 * n)) for n in range(4)},
}
#: Quick mode ratio sets = the fused V/F point ratios (SPEC-v3 section 3; P 8 points, E 6 points).
QUICK_RATIOS: dict[str, tuple[int, ...]] = {"P": (8, 20, 30, 48, 53, 55, 56, 57), "E": (8, 15, 23, 30, 40, 46)}
MAX_NAME = 64
_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}\Z")
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
#: Responses are one JSON line of at most 64 KiB (contract 8); 768 bytes are left for the envelope (``{"id" (<= 64 chars),"ok","result":{"curve_id",...}}`` is ~150 B at
#: most; ``problems`` is deducted by the caller). A dense run of all 12 units is ~60 KB, ~64.5 KB with the tuning words of 24 CPUs in ``tuning_state`` and the G-118
#: ``fused_point`` labels: the budget is sized so that both travel whole (G-118 cut the margin from 2048 to 768 for that).
RECORD_BUDGET = 64 * 1024 - 768


def unit_kind(unit: str) -> str:
    return "P" if unit.startswith("P") else "E"


def unit_cpus(unit: str) -> tuple[int, ...]:
    return UNITS[unit]


def is_curve_id(value: object) -> bool:
    return isinstance(value, str) and _ID_RE.match(value) is not None


def new_curve_id(now: float | None = None, rand: Any = None) -> str:
    """``YYYYMMDDTHHMMSSZ-xxxx`` (UTC, four random [a-z0-9])."""
    pick = (rand or secrets).choice
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now)) + "-" + "".join(pick(_ID_ALPHABET) for _ in range(4))


def validate_name(name: object) -> str:
    """A curve name: 1..64 printable characters (no control characters), surrounding blanks stripped. ``ValueError`` otherwise."""
    if not isinstance(name, str):
        raise ValueError("'name' must be a string")
    text = name.strip()
    if not 1 <= len(text) <= MAX_NAME:
        raise ValueError(f"'name' must be 1..{MAX_NAME} characters")
    if not text.isprintable():
        raise ValueError("'name' must not contain control characters")
    return text


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """The ``curves.list`` row. ``tuning_state`` is the nested subset ``{kind}`` (the contract writes ``tuning_state.kind``)."""
    state = record.get("tuning_state")
    return {
        "id": record["id"],
        "name": record.get("name", ""),
        "created": record.get("created", ""),
        "mode": record.get("mode", ""),
        "complete": bool(record.get("complete", False)),
        "tuning_state": {"kind": state.get("kind", "unknown") if isinstance(state, dict) else "unknown"},
        "units": [u for u in UNITS if u in (record.get("units") or {})],
        "baseline": bool(record.get("baseline", False)),
    }


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False))


def fit_record(record: dict[str, Any], budget: int = RECORD_BUDGET) -> dict[str, Any]:
    """The record as it may travel in one response line: whole when it fits, else with as many units (display order) as fit, plus
    ``omitted_units`` (a deviation from contract 9, only ever for a very dense run; ``curves.get {id, units}`` fetches the rest)."""
    if _size(record) <= budget:
        return record
    units = record.get("units") or {}
    head = {k: v for k, v in record.items() if k != "units"}
    kept: dict[str, Any] = {}
    omitted: list[str] = []
    used = _size({**head, "units": {}, "omitted_units": list(UNITS)}) + 64
    for name in [u for u in UNITS if u in units] + [u for u in units if u not in UNITS]:
        cost = _size({name: units[name]}) + 1
        if used + cost <= budget:
            kept[name] = units[name]
            used += cost
        else:
            omitted.append(name)
    return {**head, "units": kept, "omitted_units": omitted}
