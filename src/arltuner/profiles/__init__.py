"""Tuning-profile schema and loader (Contract 6, docs/ARCHITECTURE.md). Import has no side effects."""

from .schema import (
    PLATFORM,
    POINT_RANGE,
    TURBO_BUCKETS,
    CoreSetting,
    CpuSetting,
    PointCpuSetting,
    PointSetting,
    Profile,
    ProfileError,
    TurboTables,
    load_profile,
)
from .complete import BuildError, complete_from_words, overlay

__all__ = [
    "BuildError",
    "complete_from_words",
    "overlay",
    "PLATFORM",
    "POINT_RANGE",
    "TURBO_BUCKETS",
    "CoreSetting",
    "CpuSetting",
    "PointCpuSetting",
    "PointSetting",
    "Profile",
    "ProfileError",
    "TurboTables",
    "load_profile",
]
