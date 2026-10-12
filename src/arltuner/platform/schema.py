"""Platform file loader and validator (SPEC section 3): ``platforms/<id>.toml`` -> an immutable ``Platform``.

The platform file identifies a CPU *family* (CPUID family/model, known steppings) and records the Vcore sensor channel of each known board (D-036). It holds no
topology, no V/F point ratios, no fused words and no turbo tables: those are read live. Standard library only (``tomllib``). Loading performs no hardware access
and no I/O besides reading the one file. Every structural problem raises ``PlatformError`` naming the offending key; nothing is guessed or defaulted.
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
    "default_platform",
    "default_platform_path",
    "load_platform",
    "parse_platform",
]

PLATFORM_FILE = "arl-s.toml"
SCHEMA_VERSION = 2


class PlatformError(ValueError):
    """The platform file is missing, unparsable or fails validation."""


@dataclass(frozen=True)
class CpuId:
    vendor: str
    family: int
    model: int
    steppings: tuple[int, ...]  # known steppings; any other stepping of this family/model is a preflight WARN, not a refusal


@dataclass(frozen=True)
class Sensors:
    """Where the hwmon readings come from on one board. Empty ``vcore_*`` = no Vcore channel recorded: the Vcore reading is unavailable (never a guess)."""

    temp_hwmon_name: str
    vcore_hwmon_vendor: str
    vcore_hwmon_name: str  # hwmon ``name`` of the board Super-I/O chip; "" = not recorded
    vcore_hwmon_label: str  # its ``in*_label`` that is Vcore; "" = not recorded


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    cpuid: CpuId
    temp_hwmon_name: str
    boards: Mapping[str, Sensors]  # DMI board_name -> sensors

    def sensors_for(self, board_name: str | None) -> Sensors:
        """The sensors of the board with this DMI ``board_name``; an unknown (or unreadable) board has temperatures but no Vcore channel."""
        known = self.boards.get((board_name or "").strip())
        return known if known is not None else Sensors(self.temp_hwmon_name, "", "", "")


def default_platform_path(here: Path | None = None) -> Path:
    """The platform file. ``here`` is this module's path (tests inject another).

    * Running from the **installed tree** (``<root>/arltuner/platform/schema.py``, ``<root>`` = ``/usr/lib/arltuner``, SPEC 4.4): only
      ``<root>/platforms/`` - the root-owned copy next to the code, never a checkout.
    * Running from a **source checkout** (``<repo>/src/arltuner/platform/schema.py``): ``<repo>/platforms/``, else the installed
      ``/usr/lib/arltuner/platforms/``.

    Returns the first existing candidate, else the first one (the missing path, for the error message)."""
    here = Path(__file__).resolve() if here is None else here
    above_package = here.parents[2]
    if above_package.name == "src":
        candidates = [above_package.parent / "platforms" / PLATFORM_FILE, Path("/usr/lib/arltuner/platforms") / PLATFORM_FILE]
    else:
        candidates = [above_package / "platforms" / PLATFORM_FILE]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


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


def _ints(table: Mapping[str, object], key: str, where: str, *, lo: int = 0, hi: int = 1 << 64) -> tuple[int, ...]:
    value = table.get(key)
    if not isinstance(value, list) or not value or any(type(v) is not int or not lo <= v < hi for v in value):
        raise PlatformError(f"platform file: [{where}] {key} must be a non-empty list of integers in {lo}..{hi - 1} (got {value!r})")
    if len(set(value)) != len(value):
        raise PlatformError(f"platform file: [{where}] {key} must not repeat a value (got {value!r})")
    return tuple(value)


def _check_known(table: Mapping[str, object], where: str, allowed: set[str]) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise PlatformError(f"platform file: [{where}] unknown key(s) {extra}")


# --- parse -------------------------------------------------------------------------------------------------------------------------------

def parse_platform(data: Mapping[str, object]) -> Platform:
    """Validate a decoded platform document and build the ``Platform``."""
    _check_known(data, "", {"schema", "platform", "cpuid", "sensors", "boards"})
    if data.get("schema") != SCHEMA_VERSION:
        raise PlatformError(f"platform file: schema must be {SCHEMA_VERSION} (got {data.get('schema')!r})")

    plat = _table(data, "platform")
    _check_known(plat, "platform", {"id", "name"})
    pid, name = (_str(plat, k, "platform") for k in ("id", "name"))

    c = _table(data, "cpuid")
    _check_known(c, "cpuid", {"vendor", "family", "model", "steppings"})
    cpuid = CpuId(
        vendor=_str(c, "vendor", "cpuid"),
        family=_int(c, "family", "cpuid", 0, 256),
        model=_int(c, "model", "cpuid", 0, 256),
        steppings=_ints(c, "steppings", "cpuid", hi=16),
    )

    s = _table(data, "sensors")
    _check_known(s, "sensors", {"temp_hwmon_name"})
    temp = _str(s, "temp_hwmon_name", "sensors")

    boards: dict[str, Sensors] = {}
    raw_boards = data.get("boards", {})
    if not isinstance(raw_boards, dict):
        raise PlatformError("platform file: [boards] must be a table of tables keyed by DMI board_name")
    for board, entry in raw_boards.items():
        where = f"boards.{board}"
        if not isinstance(entry, dict):
            raise PlatformError(f"platform file: [{where}] must be a table")
        _check_known(entry, where, {"vcore_hwmon_vendor", "vcore_hwmon_name", "vcore_hwmon_label"})
        boards[board] = Sensors(
            temp_hwmon_name=temp,
            vcore_hwmon_vendor=_str(entry, "vcore_hwmon_vendor", where),
            vcore_hwmon_name=_str(entry, "vcore_hwmon_name", where, allow_empty=True),
            vcore_hwmon_label=_str(entry, "vcore_hwmon_label", where, allow_empty=True),
        )

    return Platform(id=pid, name=name, cpuid=cpuid, temp_hwmon_name=temp, boards=MappingProxyType(boards))


def load_platform(path: Path | None = None) -> Platform:
    """Read, decode and validate a platform file (default: the Arrow Lake-S family file of the source/install tree)."""
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
    """The validated Arrow Lake-S family platform (cached; the file is read once per process)."""
    return load_platform()
