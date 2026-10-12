"""Platform file (``platforms/arl-s.toml``) loader and the SPEC section 3 preflight. Import has no side effects."""
from .preflight import OPTIONAL_TOOLS, CheckResult, PreflightReport, Status, preflight
from .schema import CpuId, Platform, PlatformError, Sensors, default_platform, load_platform, parse_platform

__all__ = [
    "OPTIONAL_TOOLS",
    "CheckResult",
    "CpuId",
    "Platform",
    "PlatformError",
    "PreflightReport",
    "Sensors",
    "Status",
    "default_platform",
    "load_platform",
    "parse_platform",
    "preflight",
]
