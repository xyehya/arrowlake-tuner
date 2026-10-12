"""CPU topology of an Arrow Lake-S part, derived live from sysfs (Contract 3, docs/ARCHITECTURE.md; D-036). No hardware access beyond reading sysfs.

``reference`` holds the simulated reference CPU (a fixture for the simulator and the tests, never used to decide anything on hardware).
"""

from .cpumap import (
    KIND_E,
    KIND_P,
    KINDS,
    Core,
    Topology,
    TopologyError,
    TopologyMismatch,
    UnknownCore,
    detect,
)
from .live import LiveTopology, LiveUnit, contract_dict, read_live, topology_problems, unit_table
from .reference import arl_s_285k, synthetic

__all__ = [
    "KIND_E",
    "KIND_P",
    "KINDS",
    "Core",
    "LiveTopology",
    "LiveUnit",
    "Topology",
    "TopologyError",
    "TopologyMismatch",
    "UnknownCore",
    "arl_s_285k",
    "contract_dict",
    "detect",
    "read_live",
    "synthetic",
    "topology_problems",
    "unit_table",
]
