"""Platform file loader and validator (SPEC section 3): ``platforms/<id>.toml`` -> an immutable ``Platform``.

Standard library only (``tomllib``). Loading performs no hardware access and no I/O besides reading the one file. Every structural problem
raises ``PlatformError`` naming the offending key; nothing is guessed or defaulted, because a platform file is what authorises writes.
"""
from __future__ import annotations

import functools
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "PLATFORM_FILE",
    "CpuId",
    "Platform",
    "PlatformError",
    "Sensors",
    "TopologySpec",
    "default_platform",
    "default_platform_path",
    "load_platform",
    "parse_platform",
]

PLATFORM_FILE = "arl-s-285k.toml"
SCHEMA_VERSION = 1
_TURBO_MSRS = (0x1AD, 0x1AE, 0x650, 0x651)
_MAX_RATIO = 0xFF
_WORD20 = (1 << 20) - 1


class PlatformError(ValueError):
    """The platform file is missing, unparsable or fails validation."""


@dataclass(frozen=True)
class CpuId:
    vendor: str
    signature: int  # CPUID leaf 1 EAX
    family: int
    model: int
    stepping: int


@dataclass(frozen=True)
class TopologySpec:
    cpu_core_cpus: str  # cpulist string of /sys/devices/cpu_core/cpus
    cpu_atom_cpus: str  # cpulist string of /sys/devices/cpu_atom/cpus
    p_count: int
    e_count: int
    e_first_cpu: int
    e_cluster_size: int
    favored_p: tuple[int, ...]
    lp_p: tuple[int, ...]
    lp_e: tuple[int, ...]

    @property
    def cpu_count(self) -> int:
        return self.p_count + self.e_count

    @property
    def p_cpus(self) -> tuple[int, ...]:
        return tuple(range(self.p_count))

    @property
    def e_cpus(self) -> tuple[int, ...]:
        return tuple(range(self.e_first_cpu, self.e_first_cpu + self.e_count))


@dataclass(frozen=True)
class Sensors:
    temp_hwmon_name: str
    vcore_hwmon_vendor: str
    vcore_hwmon_name: str  # "" = not recorded


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    board: str
    cpuid: CpuId
    topology: TopologySpec
    p_point_ratios: tuple[int, ...]
    e_point_ratios: tuple[int, ...]
    ring_point_ratios: tuple[int, ...]
    fused_point: Mapping[int, int]  # Linux CPU -> word (cmd 0x09 P1=0x11 P2=0)
    turbo_defaults: Mapping[int, int]  # MSR -> stock 64-bit value
    sensors: Sensors


def default_platform_path() -> Path:
    """``platforms/arl-s-285k.toml`` of the source tree, else of the installed tree (``/usr/lib/arltuner/platforms``, SPEC 4.4)."""
    here = Path(__file__).resolve()
    for candidate in (here.parents[3] / "platforms" / PLATFORM_FILE, Path("/usr/lib/arltuner/platforms") / PLATFORM_FILE):
        if candidate.is_file():
            return candidate
    return here.parents[3] / "platforms" / PLATFORM_FILE  # the missing path, for the error message


# --- validation helpers ------------------------------------------------------------------------------------------------------------------

def _table(data: Mapping[str, object], key: str, where: str = "") -> Mapping[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise PlatformError(f"platform file: [{where}{key}] table is missing")
    return value


def _int(table: Mapping[str, object], key: str, where: str, lo: int = 0, hi: int = 1 << 64) -> int:
    value = table.get(key)
    if type(value) is not int:
        raise PlatformError(f"platform file: [{where}] {key} must be an integer (got {value!r})")
    if not lo <= value < hi:
        raise PlatformError(f"platform file: [{where}] {key}={value:#x} outside {lo:#x}..{hi - 1:#x}")
    return value


def _str(table: Mapping[str, object], key: str, where: str, *, allow_empty: bool = False) -> str:
    value = table.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise PlatformError(f"platform file: [{where}] {key} must be a {'string' if allow_empty else 'non-empty string'} (got {value!r})")
    return value


def _ints(table: Mapping[str, object], key: str, where: str, *, length: int | None = None, lo: int = 0, hi: int = 1 << 64) -> tuple[int, ...]:
    value = table.get(key)
    if not isinstance(value, list) or not value or any(type(v) is not int or not lo <= v < hi for v in value):
        raise PlatformError(f"platform file: [{where}] {key} must be a non-empty list of integers in {lo}..{hi - 1} (got {value!r})")
    if length is not None and len(value) != length:
        raise PlatformError(f"platform file: [{where}] {key} must have {length} entries (got {len(value)})")
    return tuple(value)


def _ratios(table: Mapping[str, object], key: str, where: str) -> tuple[int, ...]:
    ratios = _ints(table, key, where, lo=1, hi=_MAX_RATIO + 1)
    if any(b <= a for a, b in zip(ratios, ratios[1:])):
        raise PlatformError(f"platform file: [{where}] {key} must be strictly ascending (got {list(ratios)})")
    return ratios


def _signature(family: int, model: int, stepping: int) -> int:
    """CPUID leaf 1 EAX from the (display) family/model/stepping as /proc/cpuinfo prints them (processor type 0)."""
    ext_family = family - 0xF if family >= 0xF else 0
    base_family = 0xF if family >= 0xF else family
    return (ext_family << 20) | ((model >> 4) << 16) | (base_family << 8) | ((model & 0xF) << 4) | stepping


def _check_known(table: Mapping[str, object], where: str, allowed: set[str]) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise PlatformError(f"platform file: [{where}] unknown key(s) {extra}")


# --- parse -------------------------------------------------------------------------------------------------------------------------------

def parse_platform(data: Mapping[str, object]) -> Platform:
    """Validate a decoded platform document and build the ``Platform``."""
    _check_known(data, "", {"schema", "platform", "cpuid", "topology", "vf_points", "fused_point", "turbo", "sensors"})
    if data.get("schema") != SCHEMA_VERSION:
        raise PlatformError(f"platform file: schema must be {SCHEMA_VERSION} (got {data.get('schema')!r})")

    plat = _table(data, "platform")
    _check_known(plat, "platform", {"id", "name", "board"})
    pid, name, board = (_str(plat, k, "platform") for k in ("id", "name", "board"))

    c = _table(data, "cpuid")
    _check_known(c, "cpuid", {"vendor", "signature", "family", "model", "stepping"})
    cpuid = CpuId(
        vendor=_str(c, "vendor", "cpuid"),
        signature=_int(c, "signature", "cpuid", 1, 1 << 32),
        family=_int(c, "family", "cpuid", 0, 256),
        model=_int(c, "model", "cpuid", 0, 256),
        stepping=_int(c, "stepping", "cpuid", 0, 16),
    )
    if _signature(cpuid.family, cpuid.model, cpuid.stepping) != cpuid.signature:
        raise PlatformError(
            f"platform file: [cpuid] signature {cpuid.signature:#x} does not match family/model/stepping "
            f"{cpuid.family}/{cpuid.model}/{cpuid.stepping} (= {_signature(cpuid.family, cpuid.model, cpuid.stepping):#x})"
        )

    t = _table(data, "topology")
    _check_known(t, "topology", {"cpu_core_cpus", "cpu_atom_cpus", "p_count", "e_count", "e_first_cpu", "e_cluster_size", "favored_p", "lp_p", "lp_e"})
    topo = TopologySpec(
        cpu_core_cpus=_str(t, "cpu_core_cpus", "topology"),
        cpu_atom_cpus=_str(t, "cpu_atom_cpus", "topology"),
        p_count=_int(t, "p_count", "topology", 1, 256),
        e_count=_int(t, "e_count", "topology", 1, 256),
        e_first_cpu=_int(t, "e_first_cpu", "topology", 1, 512),
        e_cluster_size=_int(t, "e_cluster_size", "topology", 1, 256),
        favored_p=_ints(t, "favored_p", "topology", hi=256),
        lp_p=_ints(t, "lp_p", "topology", hi=1024),
        lp_e=_ints(t, "lp_e", "topology", hi=1024),
    )
    if topo.cpu_core_cpus != f"0-{topo.p_count - 1}" or topo.cpu_atom_cpus != f"{topo.e_first_cpu}-{topo.e_first_cpu + topo.e_count - 1}":
        raise PlatformError("platform file: [topology] cpu_core_cpus / cpu_atom_cpus must be '0-<p_count-1>' / '<e_first_cpu>-<last E cpu>'")
    if topo.e_first_cpu != topo.p_count:
        raise PlatformError("platform file: [topology] e_first_cpu must equal p_count (P = CPU 0..p_count-1, E follows)")
    if topo.e_count % topo.e_cluster_size:
        raise PlatformError("platform file: [topology] e_count must be a multiple of e_cluster_size")
    if len(topo.lp_p) != topo.p_count or len(topo.lp_e) != topo.e_count:
        raise PlatformError("platform file: [topology] lp_p / lp_e lengths must equal p_count / e_count")
    if len(set(topo.lp_p + topo.lp_e)) != topo.cpu_count:
        raise PlatformError("platform file: [topology] lp_p + lp_e must be distinct")
    if any(f >= topo.p_count for f in topo.favored_p) or len(set(topo.favored_p)) != len(topo.favored_p):
        raise PlatformError("platform file: [topology] favored_p must be distinct P CPUs")

    v = _table(data, "vf_points")
    _check_known(v, "vf_points", {"p", "e", "ring"})
    p_points, e_points, ring_points = (_ratios(v, k, "vf_points") for k in ("p", "e", "ring"))

    f = _table(data, "fused_point")
    _check_known(f, "fused_point", {"p", "e_first_cluster", "e_other_clusters"})
    p_words = _ints(f, "p", "fused_point", length=topo.p_count, hi=_WORD20 + 1)
    e_first = _int(f, "e_first_cluster", "fused_point", 1, _WORD20 + 1)
    e_other = _int(f, "e_other_clusters", "fused_point", 1, _WORD20 + 1)
    if len(set(p_words)) != len(p_words):
        raise PlatformError("platform file: [fused_point] p words must be distinct per core (they are the identity check)")
    fused: dict[int, int] = {}
    for cpu, word in enumerate(p_words):
        fused[cpu] = word
    for k in range(topo.e_count):
        cpu = topo.e_first_cpu + k
        fused[cpu] = e_first if k < topo.e_cluster_size else e_other
    # The ratio field [7:0] of a fused word is the core's top V/F point.
    for cpu, word in fused.items():
        top = p_points[-1] if cpu < topo.p_count else e_points[-1]
        if word & 0xFF != top:
            raise PlatformError(f"platform file: [fused_point] CPU {cpu} word {word:#x} ratio {word & 0xFF} is not the top point {top}")

    tu = _table(data, "turbo")
    _check_known(tu, "turbo", {f"msr_0x{m:X}" for m in _TURBO_MSRS})
    turbo = {m: _int(tu, f"msr_0x{m:X}", "turbo", 0, 1 << 64) for m in _TURBO_MSRS}

    s = _table(data, "sensors")
    _check_known(s, "sensors", {"temp_hwmon_name", "vcore_hwmon_vendor", "vcore_hwmon_name"})
    sensors = Sensors(
        temp_hwmon_name=_str(s, "temp_hwmon_name", "sensors"),
        vcore_hwmon_vendor=_str(s, "vcore_hwmon_vendor", "sensors"),
        vcore_hwmon_name=_str(s, "vcore_hwmon_name", "sensors", allow_empty=True),
    )

    return Platform(
        id=pid,
        name=name,
        board=board,
        cpuid=cpuid,
        topology=topo,
        p_point_ratios=p_points,
        e_point_ratios=e_points,
        ring_point_ratios=ring_points,
        fused_point=MappingProxyType(fused),
        turbo_defaults=MappingProxyType(turbo),
        sensors=sensors,
    )


def load_platform(path: Path | None = None) -> Platform:
    """Read, decode and validate a platform file (default: the 285K file of the source/install tree)."""
    p = Path(path) if path is not None else default_platform_path()
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise PlatformError(f"platform file {p}: cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PlatformError(f"platform file {p}: invalid TOML: {exc}") from exc
    return parse_platform(data)


@functools.cache
def default_platform() -> Platform:
    """The validated 285K platform (cached; the file is read once per process)."""
    return load_platform()
