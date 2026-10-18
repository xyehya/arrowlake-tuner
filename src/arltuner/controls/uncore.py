"""G-216: the uncore (ring) frequency limits -- ``intel_uncore_frequency/package_00_die_00/{min,max}_freq_khz``. Pure apart from the files it is given.

Facts (``[MEASURED lnx 2026-10-18]``): ``min_freq_khz`` / ``max_freq_khz`` 800000 / 4000000 and ``initial_min_freq_khz`` / ``initial_max_freq_khz`` 800000 / 4000000 on the reference
machine; one ratio = 100000 kHz (ratio 8..40). The limits the driver starts with (``initial_*``) are the hardware's own: nothing outside them is ever written (D-049: no policy range on
top). ``min <= max`` holds after **every single write** (the driver refuses otherwise): raising the minimum above the current maximum writes the maximum first, anything else writes the
minimum first.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arltuner.guard.policy import LARGE_RATIO_CHANGE, Policy

from .caps import KHZ_PER_RATIO, Refused, RuntimeFiles, RuntimeStep, SysfsError

__all__ = ["DIR", "KEYS", "UncoreState", "limits_json", "plan", "plan_revert", "read_state", "warnings"]

DIR = "intel_uncore_frequency"
KEYS = ("min_ratio", "max_ratio")
_ATTR = {"min_ratio": "min_freq_khz", "max_ratio": "max_freq_khz"}


@dataclass(frozen=True)
class UncoreState:
    """The limits in force and the hardware's own, kHz; ``domain`` is the directory name (``package_00_die_00``)."""

    domain: str
    min_khz: int
    max_khz: int
    initial_min_khz: int
    initial_max_khz: int

    def file(self, key: str) -> str:
        return f"{DIR}/{self.domain}/{_ATTR[key]}"

    def value(self, key: str) -> int:
        return self.min_khz if key == "min_ratio" else self.max_khz

    def as_json(self) -> dict[str, Any]:
        return {
            "domain": self.domain, "min_khz": self.min_khz, "max_khz": self.max_khz, "initial_min_khz": self.initial_min_khz, "initial_max_khz": self.initial_max_khz,
            "min_ratio": self.min_khz / KHZ_PER_RATIO, "max_ratio": self.max_khz / KHZ_PER_RATIO,
            "initial_min_ratio": self.initial_min_khz / KHZ_PER_RATIO, "initial_max_ratio": self.initial_max_khz / KHZ_PER_RATIO,
        }


def read_state(files: RuntimeFiles) -> UncoreState:
    """The one uncore domain. ``SysfsError`` when there is none, more than one (nothing is assumed about which is meant) or a file of it is missing / not a number."""
    domains = [d for d in files.listdir(DIR) if d.startswith(("package_", "uncore"))]
    if not domains:
        raise SysfsError(f"no uncore frequency domain under {files.path(DIR)} (is intel_uncore_frequency loaded?)")
    if len(domains) > 1:
        raise SysfsError(f"{len(domains)} uncore domains ({', '.join(domains)}): this build handles exactly one and assumes nothing about the others")
    d = domains[0]
    values = [files.read_int(f"{DIR}/{d}/{attr}") for attr in ("min_freq_khz", "max_freq_khz", "initial_min_freq_khz", "initial_max_freq_khz")]
    if any(v is None for v in values):
        raise SysfsError(f"uncore domain {d} lacks one of min_freq_khz / max_freq_khz / initial_min_freq_khz / initial_max_freq_khz")
    return UncoreState(d, *values)  # type: ignore[arg-type]


def _steps(new: Mapping[str, int], state: UncoreState) -> list[RuntimeStep]:
    """Order and list the writes of ``new`` (kHz per key) over ``state``: min first unless the new minimum exceeds the current maximum (then max first)."""
    final_min, final_max = new.get("min_ratio", state.min_khz), new.get("max_ratio", state.max_khz)
    if final_min > final_max:
        raise Refused("UNCORE_ORDER", f"the minimum ({final_min / KHZ_PER_RATIO:g}) would be above the maximum ({final_max / KHZ_PER_RATIO:g}): min_ratio <= max_ratio always", "min_ratio")
    order = ["max_ratio", "min_ratio"] if final_min > state.max_khz else ["min_ratio", "max_ratio"]
    steps: list[RuntimeStep] = []
    for key in order:
        if key in new and new[key] != state.value(key):
            steps.append(RuntimeStep(len(steps) + 1, "uncore", key, state.file(key), state.value(key), new[key], (new[key], new[key])))
    return steps


def _ratio(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Refused("UNCORE_VALUE", f"uncore.{key} must be a whole ratio (an integer >= 1; 1 ratio = 100 MHz)", key)
    return value


def plan(intent: Mapping[str, int], policy: Policy, state: UncoreState) -> list[RuntimeStep]:
    """The write steps for ``intent`` (``{min_ratio?, max_ratio?}``) over the limits in force, or :class:`Refused`. D-049, checks in order: the hardware's own range
    ``initial_min_freq_khz..initial_max_freq_khz`` (``UNCORE_RANGE``), ``min <= max`` of the final pair (``UNCORE_ORDER``: the driver refuses otherwise). Steps that change nothing are
    dropped. ``policy`` is kept for the signature: no policy range applies."""
    want = {k: _ratio(k, v) for k, v in intent.items() if v is not None}
    if not want:
        raise Refused("NOTHING_TO_DO", "the uncore intent changes nothing (set min_ratio and/or max_ratio)")
    new: dict[str, int] = {}
    for key, ratio in want.items():
        khz = ratio * KHZ_PER_RATIO
        if not state.initial_min_khz <= khz <= state.initial_max_khz:
            raise Refused(
                "UNCORE_RANGE",
                f"{key} = {ratio} is outside the hardware's own uncore range {state.initial_min_khz / KHZ_PER_RATIO:g}..{state.initial_max_khz / KHZ_PER_RATIO:g} (initial_min/max_freq_khz)",
                key,
            )
        new[key] = khz
    return _steps(new, state)


def plan_revert(values: Mapping[str, int], state: UncoreState) -> list[RuntimeStep]:
    """The writes that put the journaled limits back (``values`` = ``{min_ratio?, max_ratio?}`` in kHz). The policy does not apply (a recorded state is restored); the hardware's
    own range does."""
    for key, khz in values.items():
        if not state.initial_min_khz <= khz <= state.initial_max_khz:
            raise Refused("UNCORE_RANGE", f"the journaled {key} ({khz} kHz) is outside the hardware's own uncore range {state.initial_min_khz}..{state.initial_max_khz} kHz", key)
    return _steps(dict(values), state)


def warnings(intent: Mapping[str, int], state: UncoreState) -> list[tuple[str, str]]:
    """D-049 ``[(code, text)]`` warnings of an uncore intent: ``LARGE_CHANGE`` when a limit moves by more than ``LARGE_RATIO_CHANGE`` bins. Nothing refuses (the driver bounds it)."""
    out: list[tuple[str, str]] = []
    for key, ratio in intent.items():
        if ratio is None or key not in KEYS:
            continue
        now = state.value(key) / KHZ_PER_RATIO
        if abs(ratio - now) > LARGE_RATIO_CHANGE:
            out.append(("LARGE_CHANGE", f"uncore {key}: {now:g} -> {ratio} is a large single change (more than {LARGE_RATIO_CHANGE} bins); the driver accepts it"))
    return out


def limits_json(state: UncoreState) -> list[int]:
    """D-049 ``cpu_limits.uncore_khz``: ``[initial_min_freq_khz, initial_max_freq_khz]``, the hardware's own range (sysfs)."""
    return [state.initial_min_khz, state.initial_max_khz]
