"""Read-only health snapshot: the taint integer, the PCI device set, the module set (+ informational thermal/MCE values).

This is arltuner's own implementation of the "health bracket" from earlier research on the reference machine (safety lessons
to inherit: taint integer + set bits, PCI device set, module set), required by ``docs/SAFETY.md`` "Mailbox-session
discipline": *after each transaction, check kernel/RAS/PCI health before the next; acceptance = a CLEAN delta*. Two snapshots are
compared by :func:`arltuner.health.gate.diff`.

Reads only, and only under ``root`` (``/`` in production; tests always pass a fake tree):

* ``root/proc/sys/kernel/tainted``            -- mandatory (missing/unparseable -> :class:`HealthError`, fail closed)
* ``root/sys/bus/pci/devices``                -- mandatory directory; entry names are the device addresses
* ``root/proc/modules``                       -- mandatory; first column of every line is the module name
* ``root/sys/class/thermal/thermal_zone*/temp`` and ``root/sys/devices/system/machinecheck/machinecheck*/bank*`` -- optional,
  informational, never part of a verdict (an unreadable value is skipped, not an error).

The only write this module ever performs is :func:`save_snapshot` creating a NEW file (``O_CREAT | O_EXCL``, never overwriting).
No subprocess, no sleep, no network. Taint *letters* are deliberately not decoded (bit indices only); sticky
bits are never cleared.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "HealthError",
    "HealthSnapshot",
    "decode_bits",
    "load_snapshot",
    "save_snapshot",
    "take_snapshot",
]

SCHEMA_VERSION = 1
_MAX_READ_BYTES = 4 << 20  # far above any real /proc/modules; a bigger file is refused rather than silently truncated
_READ_CHUNK_BYTES = 64 << 10  # per read() call; procfs/sysctl handlers allocate a buffer of the requested size
_MAX_TAINT_DIGITS = 40  # the kernel prints an unsigned long (<= 20 digits); bound int() on garbage input
_DIGITS = re.compile(r"[0-9]+")


class HealthError(Exception):
    """A health fact could not be established (missing/garbled file, bad snapshot file, refused overwrite). Callers fail closed."""


def decode_bits(taint: int) -> tuple[int, ...]:
    """Indices (ascending) of the bits set in a kernel taint integer. Indices only: letters are deliberately not decoded."""
    if isinstance(taint, bool) or not isinstance(taint, int) or taint < 0:
        raise HealthError(f"taint must be a non-negative int, got {taint!r}")
    return tuple(i for i in range(taint.bit_length()) if (taint >> i) & 1)


@dataclass(frozen=True)
class HealthSnapshot:
    """One observation of kernel/PCI/module health. ``thermal_mc`` and ``machinecheck`` are informational only.

    ``thermal_mc``: ``{"thermal_zone0": millidegC, ...}`` or ``None`` when no zone could be read.
    ``machinecheck``: ``{"machinecheck0/bank0": value, ...}`` (raw per-CPU bank files, parsed as hex) or ``None`` when absent;
    what these values mean is [UNVERIFIED] -- they are recorded, never interpreted.
    """

    taken_at: float
    taint: int
    taint_bits: tuple[int, ...]  # bit indices set in ``taint`` (NO letter decoding)
    pci: frozenset[str]
    modules: frozenset[str]
    thermal_mc: Mapping[str, int] | None
    machinecheck: Mapping[str, int] | None

    def __post_init__(self) -> None:
        if isinstance(self.taken_at, bool) or not isinstance(self.taken_at, (int, float)) or not math.isfinite(self.taken_at):
            raise HealthError(f"taken_at must be a finite number, got {self.taken_at!r}")
        if tuple(self.taint_bits) != decode_bits(self.taint):  # also validates ``taint``
            raise HealthError("taint_bits does not match the taint integer")
        for name in ("pci", "modules"):
            value = getattr(self, name)
            if not isinstance(value, frozenset) or not all(isinstance(item, str) for item in value):
                raise HealthError(f"{name} must be a frozenset[str]")
        for name in ("thermal_mc", "machinecheck"):
            value = getattr(self, name)
            if value is not None and not (
                isinstance(value, Mapping)
                and all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) for k, v in value.items())
            ):
                raise HealthError(f"{name} must be None or a mapping[str, int]")


# ------------------------------------------------------------------------------------------------------------------ reading
def _read_bounded(handle) -> bytes:
    """Up to ``_MAX_READ_BYTES + 1`` bytes in chunks: /proc/sys rejects one huge read() with ENOMEM
    (measured lnx 2026-10-01, kernel 7.2: a 4 MiB read of /proc/sys/kernel/tainted fails, 1 MiB succeeds)."""
    chunks: list[bytes] = []
    size = 0
    while size <= _MAX_READ_BYTES:
        chunk = handle.read(min(_READ_CHUNK_BYTES, _MAX_READ_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _read_text(path: Path, what: str) -> str:
    try:
        with open(path, "rb") as handle:
            raw = _read_bounded(handle)
    except OSError as exc:
        raise HealthError(f"cannot read {what} ({path}): {exc!r}") from exc
    if len(raw) > _MAX_READ_BYTES:
        raise HealthError(f"{what} ({path}) is larger than {_MAX_READ_BYTES} bytes; refusing to guess")
    return raw.decode("utf-8", "replace")


def _read_taint(root: Path) -> int:
    path = root / "proc" / "sys" / "kernel" / "tainted"
    text = _read_text(path, "taint word").strip()
    if not _DIGITS.fullmatch(text) or len(text) > _MAX_TAINT_DIGITS:
        raise HealthError(f"taint word ({path}) is not a decimal integer: {text[:40]!r}")
    return int(text)


def _read_pci(root: Path) -> frozenset[str]:
    path = root / "sys" / "bus" / "pci" / "devices"
    try:
        names = os.listdir(path)
    except OSError as exc:
        raise HealthError(f"cannot list PCI devices ({path}): {exc!r}") from exc
    if not names:  # a check that examined nothing looks like a clean negative (a known trap): sysfs not mounted, container...
        raise HealthError(f"PCI device directory {path} is empty; refusing to treat 'nothing seen' as healthy")
    return frozenset(names)


def _read_modules(root: Path) -> frozenset[str]:
    text = _read_text(root / "proc" / "modules", "module list")
    names = set()
    for line in text.splitlines():
        fields = line.split()
        if fields:
            names.add(fields[0])
    return frozenset(names)


def _read_number(path: Path, base: int) -> int | None:
    """One informational number from a small file; ``None`` when unreadable or not a number (never an error)."""
    try:
        with open(path, "rb") as handle:
            return int(handle.read(256).decode("ascii", "replace").strip(), base)
    except (OSError, ValueError):
        return None


def _listdir_sorted(path: Path, prefix: str) -> list[str] | None:
    try:
        return sorted(name for name in os.listdir(path) if name.startswith(prefix))
    except OSError:
        return None


def _read_thermal(root: Path) -> dict[str, int] | None:
    """``{"thermal_zoneN": millidegC}`` from ``sys/class/thermal/thermal_zone*/temp``; ``None`` when nothing could be read."""
    base = root / "sys" / "class" / "thermal"
    found: dict[str, int] = {}
    for zone in _listdir_sorted(base, "thermal_zone") or ():
        value = _read_number(base / zone / "temp", 10)
        if value is not None:
            found[zone] = value
    return found or None


def _read_machinecheck(root: Path) -> dict[str, int] | None:
    """``{"machinecheckN/bankM": value}`` from ``sys/devices/system/machinecheck/machinecheck*/bank*`` (hex); ``None`` if absent."""
    base = root / "sys" / "devices" / "system" / "machinecheck"
    found: dict[str, int] = {}
    for cpu in _listdir_sorted(base, "machinecheck") or ():
        for bank in _listdir_sorted(base / cpu, "bank") or ():
            value = _read_number(base / cpu / bank, 16)
            if value is not None:
                found[f"{cpu}/{bank}"] = value
    return found or None


def take_snapshot(root: Path = Path("/"), *, clock: Callable[[], float] = time.time) -> HealthSnapshot:
    """Read the health facts under ``root`` (read-only). A missing/unreadable taint word, PCI directory or module list raises
    :class:`HealthError` (fail closed, SAFETY.md "Mailbox-session discipline"); thermal/machinecheck data is best effort."""
    root = Path(root)
    taint = _read_taint(root)
    pci = _read_pci(root)
    modules = _read_modules(root)
    try:
        taken_at = float(clock())
    except (TypeError, ValueError) as exc:
        raise HealthError(f"clock returned a non-number: {exc!r}") from exc
    return HealthSnapshot(
        taken_at=taken_at,
        taint=taint,
        taint_bits=decode_bits(taint),
        pci=pci,
        modules=modules,
        thermal_mc=_read_thermal(root),
        machinecheck=_read_machinecheck(root),
    )


# ------------------------------------------------------------------------------------------------------------ persistence
def _to_jsonable(snap: HealthSnapshot) -> dict[str, object]:
    return {
        "schema": SCHEMA_VERSION,
        "taken_at": snap.taken_at,
        "taint": snap.taint,
        "taint_bits": list(snap.taint_bits),
        "pci": sorted(snap.pci),
        "modules": sorted(snap.modules),
        "thermal_mc": None if snap.thermal_mc is None else dict(snap.thermal_mc),
        "machinecheck": None if snap.machinecheck is None else dict(snap.machinecheck),
    }


def save_snapshot(snap: HealthSnapshot, path: str | os.PathLike[str]) -> None:
    """Write ``snap`` as sorted-key, newline-terminated JSON to a NEW file (``O_CREAT | O_EXCL``; an existing file is never
    overwritten -- :class:`HealthError`). This is the only write in the health package."""
    if not isinstance(snap, HealthSnapshot):
        raise HealthError("save_snapshot needs a HealthSnapshot")
    try:
        data = (json.dumps(_to_jsonable(snap), indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HealthError(f"snapshot is not serialisable: {exc!r}") from exc
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)  # O_BINARY: never translate LF on Windows
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise HealthError(f"refusing to overwrite existing file {os.fspath(path)!r}") from exc
    except OSError as exc:
        raise HealthError(f"cannot create {os.fspath(path)!r}: {exc!r}") from exc
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)  # the point of a "before" snapshot is to outlive an abrupt boot end
        finally:
            os.close(fd)
    except OSError as exc:
        try:
            os.unlink(path)  # we created this file a moment ago; do not leave a truncated snapshot behind
        except OSError:
            pass
        raise HealthError(f"cannot write {os.fspath(path)!r}: {exc!r}") from exc


def _require(cond: bool, why: str) -> None:
    if not cond:
        raise HealthError(f"bad snapshot file: {why}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(value: object, name: str) -> frozenset[str]:
    _require(isinstance(value, list) and all(isinstance(item, str) for item in value), f"{name} must be a list of strings")
    return frozenset(value)  # type: ignore[arg-type]


def _int_map(value: object, name: str) -> dict[str, int] | None:
    if value is None:
        return None
    _require(
        isinstance(value, dict) and all(isinstance(k, str) and _is_int(v) for k, v in value.items()),
        f"{name} must be null or an object of integers",
    )
    return dict(value)  # type: ignore[arg-type]


def load_snapshot(path: str | os.PathLike[str]) -> HealthSnapshot:
    """Read a file written by :func:`save_snapshot`. Anything unexpected is a :class:`HealthError`, never a guess."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_READ_BYTES + 1)
    except OSError as exc:
        raise HealthError(f"cannot read snapshot file {os.fspath(path)!r}: {exc!r}") from exc
    _require(len(raw) <= _MAX_READ_BYTES, "file is too large")

    def _no_constants(name: str) -> object:
        raise ValueError(f"non-finite number {name}")

    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constants)
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise HealthError(f"bad snapshot file: not valid JSON: {exc!r}") from exc
    _require(isinstance(doc, dict), "top level must be an object")
    expected = {"schema", "taken_at", "taint", "taint_bits", "pci", "modules", "thermal_mc", "machinecheck"}
    _require(set(doc) == expected, f"keys must be exactly {sorted(expected)}, got {sorted(doc)}")
    _require(doc["schema"] == SCHEMA_VERSION and _is_int(doc["schema"]), f"unsupported schema {doc['schema']!r}")
    _require(_is_int(doc["taint"]) and doc["taint"] >= 0, "taint must be a non-negative integer")
    bits = doc["taint_bits"]
    _require(isinstance(bits, list) and all(_is_int(b) for b in bits), "taint_bits must be a list of integers")
    return HealthSnapshot(
        taken_at=doc["taken_at"],
        taint=doc["taint"],
        taint_bits=tuple(bits),
        pci=_str_list(doc["pci"], "pci"),
        modules=_str_list(doc["modules"], "modules"),
        thermal_mc=_int_map(doc["thermal_mc"], "thermal_mc"),
        machinecheck=_int_map(doc["machinecheck"], "machinecheck"),
    )
