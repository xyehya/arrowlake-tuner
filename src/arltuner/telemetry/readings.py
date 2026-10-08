"""The telemetry value type. A source that cannot be read yields an *unavailable* Reading, never a guessed number."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    """One telemetry value. ``value`` is ``None`` exactly when the reading is unavailable; ``reason`` then says why."""

    value: float | int | None
    unit: str = ""
    source: str = ""
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_json(self) -> dict:
        d: dict = {"value": self.value, "unit": self.unit, "source": self.source}
        if self.value is None:
            d["status"] = "unavailable"
            d["reason"] = self.reason or "unknown"
        else:
            d["status"] = "ok"
        return d


def unavailable(source: str, reason: str, unit: str = "") -> Reading:
    return Reading(None, unit, source, reason)
