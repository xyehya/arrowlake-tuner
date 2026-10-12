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

__all__ = [
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
