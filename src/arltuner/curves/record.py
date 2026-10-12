"""The curve record (contract 9) and its small helpers: unit names, ids, names, the list summary and the 64 KiB fit.

Units (SPEC-v3 preamble, ``[MEASURED]`` one regulator per cluster of four): the tuning units of the live topology = P-cores ``P<n>`` and E-clusters ``EC<n>``
(``arltuner.topology``, derived from sysfs; D-036). The unit list and each unit's CPUs come from the topology, never from this module. A record is plain JSON-able data;
nothing here touches the filesystem.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

__all__ = ["MAX_NAME", "RECORD_BUDGET", "fit_record", "is_curve_id", "is_unit_id", "new_curve_id", "summary", "unit_kind", "unit_order", "validate_name"]

MAX_NAME = 64
_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}\Z")
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
#: Responses are one JSON line of at most 64 KiB (contract 8); 768 bytes are left for the envelope (``{"id" (<= 64 chars),"ok","result":{"curve_id",...}}`` is ~150 B at
#: most; ``problems`` is deducted by the caller). A dense run of all 12 units is ~60 KB, ~64.5 KB with the tuning words of 24 CPUs in ``tuning_state`` and the G-118
#: ``fused_point`` labels: the budget is sized so that both travel whole (G-118 cut the margin from 2048 to 768 for that).
RECORD_BUDGET = 64 * 1024 - 768


_UNIT_RE = re.compile(r"(P|EC)(0|[1-9][0-9]{0,2})\Z")


def is_unit_id(value: object) -> bool:
    """Shape of a unit id (``P<n>`` / ``EC<n>``); whether the unit exists is the topology's business."""
    return isinstance(value, str) and _UNIT_RE.match(value) is not None


def unit_kind(unit: str) -> str:
    return "P" if unit.startswith("P") else "E"


def unit_order(unit: str) -> tuple[int, int]:
    """Display order: P-cores by index, then E-clusters by index; anything else last."""
    m = _UNIT_RE.match(unit)
    if m is None:
        return (2, 0)
    return (0 if m.group(1) == "P" else 1, int(m.group(2)))


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
        "units": sorted((u for u in (record.get("units") or {})), key=unit_order),
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
    used = _size({**head, "units": {}, "omitted_units": sorted(units, key=unit_order)}) + 64
    for name in sorted(units, key=unit_order):
        cost = _size({name: units[name]}) + 1
        if used + cost <= budget:
            kept[name] = units[name]
            used += cost
        else:
            omitted.append(name)
    return {**head, "units": kept, "omitted_units": omitted}
