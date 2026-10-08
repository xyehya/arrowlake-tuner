"""A fake ``/proc`` + ``/sys`` tree under a temp directory for the T-015 health tests. Nothing here touches the real ones.

The layout mirrors what ``take_snapshot`` reads: ``proc/sys/kernel/tainted``, ``sys/bus/pci/devices/<addr>``,
``proc/modules`` and, optionally, ``sys/class/thermal/thermal_zone*/temp`` and
``sys/devices/system/machinecheck/machinecheck*/bank*``.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DEFAULT_PCI = ("0000:00:00.0", "0000:00:1f.6", "0000:2b:00.0")  # host bridge, Ethernet, a Wi-Fi endpoint (name shape only)
DEFAULT_MODULES = ("snd_hda_intel", "i915", "msr")


# PCI addresses contain ':', which Windows file names cannot. On Windows the fake tree stores U+FF1A (fullwidth colon) and
# ``FakeRootCase`` patches ``os.listdir`` to hand the real ':' back, so the code under test always sees genuine addresses.
_DISK_COLON = "：" if os.name == "nt" else ":"


def module_line(name: str) -> str:
    """One ``/proc/modules`` line: name, size, refcount, dependents, state, address (the parser must use only column 1)."""
    return f"{name} 61440 3 snd_hda_codec, Live 0x0000000000000000"


class FakeRoot:
    def __init__(self, base: Path, *, taint: int | str = 0, pci=DEFAULT_PCI, modules=DEFAULT_MODULES) -> None:
        self.path = Path(base) / "fakeroot"
        self.tainted = self.path / "proc" / "sys" / "kernel" / "tainted"
        self.pci_dir = self.path / "sys" / "bus" / "pci" / "devices"
        self.modules_file = self.path / "proc" / "modules"
        self.tainted.parent.mkdir(parents=True)
        self.pci_dir.mkdir(parents=True)
        self.set_taint(taint)
        for addr in pci:
            self.add_pci(addr)
        self.set_modules(modules)

    # ---- mandatory facts
    def set_taint(self, value: int | str) -> None:
        self.tainted.write_text(f"{value}\n", encoding="ascii")

    def add_pci(self, addr: str) -> None:
        (self.pci_dir / addr.replace(":", _DISK_COLON)).mkdir()

    def remove_pci(self, addr: str) -> None:
        shutil.rmtree(self.pci_dir / addr.replace(":", _DISK_COLON))

    def set_modules(self, names) -> None:
        self.modules_file.write_text("".join(module_line(n) + "\n" for n in names), encoding="ascii")

    # ---- optional, informational
    def set_thermal(self, zones: dict[str, str | int]) -> None:
        """``{"thermal_zone0": 45000}`` -> ``sys/class/thermal/thermal_zone0/temp`` (other dirs are created too: ``cooling_device0``)."""
        base = self.path / "sys" / "class" / "thermal"
        (base / "cooling_device0").mkdir(parents=True, exist_ok=True)
        for zone, value in zones.items():
            (base / zone).mkdir(parents=True, exist_ok=True)
            (base / zone / "temp").write_text(f"{value}\n", encoding="ascii")

    def set_machinecheck(self, banks: dict[str, str]) -> None:
        """``{"machinecheck0/bank0": "ffffffffffffffff"}``; also drops a non-bank file that must be ignored."""
        base = self.path / "sys" / "devices" / "system" / "machinecheck"
        for key, value in banks.items():
            cpu, bank = key.split("/")
            (base / cpu).mkdir(parents=True, exist_ok=True)
            (base / cpu / bank).write_text(f"{value}\n", encoding="ascii")
            (base / cpu / "tolerant").write_text("1\n", encoding="ascii")


class FakeRootCase(unittest.TestCase):
    """``self.dir`` (temp dir, removed afterwards) and ``self.root`` (a default :class:`FakeRoot`)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        if _DISK_COLON != ":":
            real_listdir = os.listdir

            def listdir(path: str | os.PathLike[str] = ".") -> list[str]:
                return [name.replace(_DISK_COLON, ":") for name in real_listdir(path)]

            patcher = mock.patch.object(os, "listdir", listdir)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = FakeRoot(self.dir)
