"""hwmon readers over an injectable ``/sys/class/hwmon`` root: coretemp temperatures and board Vcore.

* coretemp: ``temp*_input`` millidegrees C with ``temp*_label`` "Package id N" / "Core N" [DOC kernel Documentation/hwmon/coretemp.rst].
  On Arrow Lake the labels are "Package id 0" + "Core N" with N = the CPU's sysfs ``topology/core_id`` (not its CPU number), E-cores included
  [MEASURED lnx 2026-10-11]; ``read_core_ids`` + the sampler map them to the dashboard's core names. We report exactly the labels present.
* Vcore: board Super-I/O driver, ``in*_input`` millivolts (hwmon ABI). The chip ``name`` and the ``in*_label`` that is Vcore come from the
  platform file (``[sensors] vcore_hwmon_name / vcore_hwmon_label``): on this board hwmon ``nct6686`` (driver nct6683), ``in0`` label
  "VIN16" [MEASURED lnx 2026-10-11]. Chip or label absent => unavailable, never another input.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .readings import Reading, unavailable

DEFAULT_HWMON_ROOT = Path("/sys/class/hwmon")
DEFAULT_CPU_ROOT = Path("/sys/devices/system/cpu")


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _devices(root: Path, names: tuple[str, ...]) -> list[Path]:
    try:
        entries = sorted(Path(root).iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [d for d in entries if _read(d / "name") in names]


def _milli(path: Path, unit: str, scale: float, src: str) -> Reading:
    raw = _read(path)
    try:
        return Reading(int(raw) / scale, unit, src)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return unavailable(src, "unreadable or non-numeric", unit)


def read_core_ids(cpus: Iterable[int], cpu_root: Path = DEFAULT_CPU_ROOT) -> dict[int, int]:
    """Linux CPU -> sysfs ``topology/core_id`` for the CPUs whose file is readable and numeric (others are left out, never guessed)."""
    out: dict[int, int] = {}
    for cpu in cpus:
        raw = _read(Path(cpu_root) / f"cpu{cpu}" / "topology" / "core_id")
        try:
            out[cpu] = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
    return out


def read_coretemp(root: Path = DEFAULT_HWMON_ROOT) -> dict[str, Reading]:
    """label -> Reading (C). Empty dict when no coretemp device exists (caller shows 'unavailable')."""
    out: dict[str, Reading] = {}
    for dev in _devices(root, ("coretemp",)):
        for lab in sorted(dev.glob("temp*_label")):
            label = _read(lab)
            if not label:
                continue
            inp = dev / (lab.name[:-6] + "_input")
            out[label] = _milli(inp, "C", 1000.0, f"hwmon {dev.name}/{inp.name}")
    return out


def read_vcore(root: Path = DEFAULT_HWMON_ROOT, chip: str = "", label: str = "") -> Reading:
    """Board Vcore in V (hwmon ``in*_input`` is mV) from the ``in*`` whose label is exactly ``label`` on the hwmon device named ``chip``.

    Nothing else is ever read: ``VIN0`` / ``in1`` on this board is a CPU-0-activity-gated signal, not Vcore [MEASURED lnx 2026-10-11]."""
    if not chip or not label:
        return unavailable("hwmon", "Vcore chip/label not recorded in the platform file", "V")
    for dev in _devices(root, (chip,)):
        for lab in sorted(dev.glob("in*_label")):
            if _read(lab) == label:
                inp = dev / (lab.name[:-6] + "_input")
                return _milli(inp, "V", 1000.0, f"hwmon {dev.name}/{inp.name}")
    return unavailable("hwmon", f"no hwmon device {chip!r} with label {label!r}", "V")
