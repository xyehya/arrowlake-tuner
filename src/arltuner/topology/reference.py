"""Simulated reference CPUs: topology FIXTURES for the simulator and the tests. **Never used to decide anything on real hardware** (D-036).

``arl_s_285k()`` is the simulated reference CPU the ``--sim`` flag and ``SimConfig.measured()`` model: a 285K-like part (8 P + 16 E, favored P0 and
P3, clusters of four). ``synthetic()`` builds any other P/E shape (6P+8E, all-equal, ...) to prove that nothing is tied to the reference.
"""
from __future__ import annotations

from collections.abc import Sequence

from .cpumap import KIND_E, KIND_P, Core, Topology

__all__ = ["arl_s_285k", "synthetic"]


def synthetic(
    p_count: int,
    e_count: int,
    *,
    favored_p: Sequence[int] = (),
    e_cluster_size: int = 4,
    p_ratio: int = 55,
    favored_ratio: int = 57,
    e_ratio: int = 49,
    p_perf: int = 88,
    favored_perf: int = 92,
    e_perf: int = 68,
    favored_source: str | None = None,
) -> Topology:
    """P = CPU 0..p_count-1, E = the next ``e_count`` CPUs in clusters of ``e_cluster_size``; ``favored_p`` are P indexes."""
    if e_count % e_cluster_size:
        raise ValueError("e_count must be a multiple of e_cluster_size")
    fav = set(favored_p)
    cores = [
        Core(KIND_P, i, i, None, i in fav, favored_ratio if i in fav else p_ratio, favored_perf if i in fav else p_perf) for i in range(p_count)
    ]
    cores += [Core(KIND_E, k, p_count + k, k // e_cluster_size, False, e_ratio, e_perf) for k in range(e_count)]
    return Topology(tuple(cores), favored_source=favored_source if favored_source is not None else ("acpi_cppc" if fav else None))


def arl_s_285k() -> Topology:
    """The simulated reference CPU: 8 P (CPU 0-7, favored P0 and P3) + 16 E (CPU 8-23, clusters of four). A fixture, not a statement about any machine."""
    return synthetic(8, 16, favored_p=(0, 3))
