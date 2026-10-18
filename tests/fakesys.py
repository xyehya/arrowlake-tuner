"""Fake sysfs trees for the live-topology tests (G-128). The real /sys is never read by the test suite."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path


def cpulist(cpus: Sequence[int]) -> str:
    """Kernel cpulist syntax of a sorted CPU set (``[0,1,2,5]`` -> ``0-2,5``)."""
    out: list[str] = []
    cpus = sorted(cpus)
    i = 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else f"{cpus[i]}-{cpus[j]}")
        i = j + 1
    return ",".join(out)


def make_sysfs(
    root: Path,
    p_count: int = 8,
    e_count: int = 16,
    *,
    cluster_size: int = 4,
    perf: Mapping[int, int | None] | None = None,
    max_khz: Mapping[int, int | None] | None = None,
    p_perf: Mapping[int, int] | None = None,
    clusters: bool = True,
) -> Path:
    """``p_count`` P CPUs (0..) then ``e_count`` E CPUs in clusters of ``cluster_size``. ``perf`` / ``max_khz``: cpu -> value (``None`` or a missing key leaves the
    file absent). ``p_perf`` is shorthand for perf of P CPUs; default perf/max_khz: none (CPPC missing)."""
    p_cpus = list(range(p_count))
    e_cpus = list(range(p_count, p_count + e_count))
    (root / "devices" / "cpu_core").mkdir(parents=True, exist_ok=True)
    (root / "devices" / "cpu_atom").mkdir(parents=True, exist_ok=True)
    (root / "devices" / "cpu_core" / "cpus").write_text(cpulist(p_cpus) + "\n", encoding="ascii")
    (root / "devices" / "cpu_atom" / "cpus").write_text(cpulist(e_cpus) + "\n", encoding="ascii")
    perf = {**(p_perf or {}), **(perf or {})}
    for cpu in p_cpus + e_cpus:
        d = root / "devices" / "system" / "cpu" / f"cpu{cpu}"
        (d / "topology").mkdir(parents=True, exist_ok=True)
        if clusters:
            if cpu in p_cpus:
                members = [cpu]
            else:
                lo = p_count + ((cpu - p_count) // cluster_size) * cluster_size
                members = list(range(lo, min(lo + cluster_size, p_count + e_count)))
            (d / "topology" / "cluster_cpus_list").write_text(cpulist(members) + "\n", encoding="ascii")
        if max_khz and max_khz.get(cpu):
            (d / "cpufreq").mkdir(exist_ok=True)
            (d / "cpufreq" / "cpuinfo_max_freq").write_text(f"{max_khz[cpu]}\n", encoding="ascii")
        if perf.get(cpu):
            (d / "acpi_cppc").mkdir(exist_ok=True)
            (d / "acpi_cppc" / "highest_perf").write_text(f"{perf[cpu]}\n", encoding="ascii")
    return root


def reference_like(root: Path, *, favored: Sequence[int] = (0, 3), cppc: bool = True, p: int = 8, e: int = 16) -> Path:
    """8P+16E (or ``p``P+``e``E) with CPPC perf 92 on ``favored`` P CPUs, 88 on the other P, 68 on E; cpuinfo_max_freq 5.7/5.5/4.9 GHz."""
    perf: dict[int, int | None] = {}
    khz: dict[int, int | None] = {}
    for cpu in range(p):
        perf[cpu] = 92 if cpu in favored else 88
        khz[cpu] = 5_700_000 if cpu in favored else 5_500_000
    for cpu in range(p, p + e):
        perf[cpu] = 68
        khz[cpu] = 4_900_000
    return make_sysfs(root, p, e, perf=perf if cppc else None, max_khz=khz)


def add_dmi(root: Path, board_name: str | None, board_vendor: str | None = None) -> Path:
    d = root / "class" / "dmi" / "id"
    d.mkdir(parents=True, exist_ok=True)
    if board_name is not None:
        (d / "board_name").write_text(board_name + "\n", encoding="utf-8")
    if board_vendor is not None:
        (d / "board_vendor").write_text(board_vendor + "\n", encoding="utf-8")
    return root


def make_powercap(
    root: Path,
    *,
    pl1_uw: int = 250_000_000,
    pl2_uw: int | None = 295_000_000,
    tau_us: int = 127_926_272,
    names: tuple[str, str] = ("long_term", "short_term"),
    max_power_uw: int | None = 125_000_000,
) -> Path:
    """G-214: ``<root>/intel-rapl:0`` as the kernel shows it on the reference machine ``[MEASURED lnx 2026-10-18]``: constraint_0 long_term (PL1 + window), constraint_1 short_term
    (PL2, window 2440 us), no constraint_2. ``pl2_uw=None`` leaves constraint_1 out."""
    zone = root / "intel-rapl:0"
    zone.mkdir(parents=True, exist_ok=True)
    (zone / "name").write_text("package-0\n", encoding="ascii")

    def put(name: str, value: object) -> None:
        (zone / name).write_text(f"{value}\n", encoding="ascii")

    put("constraint_0_name", names[0])
    put("constraint_0_power_limit_uw", pl1_uw)
    put("constraint_0_time_window_us", tau_us)
    if max_power_uw is not None:
        put("constraint_0_max_power_uw", max_power_uw)
    if pl2_uw is not None:
        put("constraint_1_name", names[1])
        put("constraint_1_power_limit_uw", pl2_uw)
        put("constraint_1_time_window_us", 2440)
    return root


def make_runtime_sysfs(
    root: Path,
    p_count: int = 8,
    e_count: int = 16,
    *,
    p_khz: int = 5_700_000,
    e_khz: int = 4_900_000,
    uncore: tuple[int, int] | None = (800_000, 4_000_000),
    offline: Sequence[int] = (),
) -> Path:
    """G-216: ``<root>`` as ``/sys/devices/system/cpu`` shows the runtime controls on the reference machine ``[MEASURED lnx 2026-10-18]``: per CPU ``cpufreq/{cpuinfo_max_freq,
    scaling_max_freq,scaling_min_freq}`` (none for ``offline`` CPUs), ``online`` for every CPU **except cpu0**, and ``intel_uncore_frequency/package_00_die_00/{min,max,initial_min,
    initial_max}_freq_khz`` (``uncore=None`` leaves the directory out)."""
    for cpu in range(p_count + e_count):
        d = root / f"cpu{cpu}"
        d.mkdir(parents=True, exist_ok=True)
        if cpu != 0:
            (d / "online").write_text("0\n" if cpu in offline else "1\n", encoding="ascii")
        if cpu in offline:
            continue
        (d / "cpufreq").mkdir(exist_ok=True)
        khz = p_khz if cpu < p_count else e_khz
        (d / "cpufreq" / "cpuinfo_max_freq").write_text(f"{khz}\n", encoding="ascii")
        (d / "cpufreq" / "scaling_max_freq").write_text(f"{khz}\n", encoding="ascii")
        (d / "cpufreq" / "scaling_min_freq").write_text("800000\n", encoding="ascii")
    if uncore is not None:
        u = root / "intel_uncore_frequency" / "package_00_die_00"
        u.mkdir(parents=True, exist_ok=True)
        for name, value in (("min", uncore[0]), ("max", uncore[1]), ("initial_min", uncore[0]), ("initial_max", uncore[1])):
            (u / f"{name}_freq_khz").write_text(f"{value}\n", encoding="ascii")
    return root
