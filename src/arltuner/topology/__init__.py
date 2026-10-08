"""CPU topology for the Core Ultra 9 285K (Contract 3, docs/ARCHITECTURE.md). Import reads and validates ``platforms/arl-s-285k.toml`` (a missing or corrupt file raises ``PlatformError``); no hardware access."""

from .cpumap import (
    KIND_E,
    KIND_P,
    KINDS,
    Core,
    Topology,
    TopologyError,
    TopologyMismatch,
    UnknownCore,
    arl_s_285k,
    detect,
)

__all__ = [
    "KIND_E",
    "KIND_P",
    "KINDS",
    "Core",
    "Topology",
    "TopologyError",
    "TopologyMismatch",
    "UnknownCore",
    "arl_s_285k",
    "detect",
]
