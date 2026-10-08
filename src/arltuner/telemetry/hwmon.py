"""hwmon readers over an injectable ``/sys/class/hwmon`` root: coretemp temperatures and board Vcore.

* coretemp: ``temp*_input`` millidegrees C with ``temp*_label`` "Package id N" / "Core N" [DOC kernel Documentation/hwmon/coretemp.rst].
  Whether coretemp labels E-cores on Arrow Lake is [UNVERIFIED]; we report exactly the labels present, nothing inferred.
* Vcore: board Super-I/O driver, ``in*_input`` millivolts (hwmon ABI) with ``in*_label`` matching a configurable name
  (default "Vcore"). The chip is [MEASURED lnx 2026-10-01] only as "Nuvoton Super-I/O, 16 mV step" (linux-path.md s7); the exact
  hwmon ``name`` and label are [UNVERIFIED] -> no match => unavailable, never a guess.
"""
from __future__ import annotations

from pathlib import Path

from .readings import Reading, unavailable

DEFAULT_HWMON_ROOT = Path("/sys/class/hwmon")
VCORE_CHIP_CANDIDATES = ("nct6775", "nct6776", "nct6779", "nct6791", "nct6792", "nct6793", "nct6795", "nct6796", "nct6797",
                         "nct6798", "nct6799")  # kernel nct6775 driver names [DOC]; which one this board binds: [UNVERIFIED]


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


def read_vcore(root: Path = DEFAULT_HWMON_ROOT, chips: tuple[str, ...] = VCORE_CHIP_CANDIDATES,
               label: str = "Vcore") -> Reading:
    """Board Vcore in V (hwmon ``in*_input`` is mV). First device in ``chips`` with a matching label wins."""
    for dev in _devices(root, chips):
        for lab in sorted(dev.glob("in*_label")):
            if _read(lab) == label:
                inp = dev / (lab.name[:-6] + "_input")
                return _milli(inp, "V", 1000.0, f"hwmon {dev.name}/{inp.name}")
    return unavailable("hwmon", f"no hwmon device in {chips[0]}..{chips[-1]} with label {label!r}", "V")
