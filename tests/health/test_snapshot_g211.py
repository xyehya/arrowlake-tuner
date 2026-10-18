"""G-211 tests: the cheaper snapshot reads (plain ``str`` paths, unbuffered ``rb`` files) give the same ``HealthSnapshot`` as the ``Path``/buffered reads they
replaced, on a tree full of awkward files; and the real gate still stops a guard on each kind of injected fault (new taint bit, cleared taint bit, PCI device
lost / added, module added / removed), before and after a transaction. Fake roots only: no real /proc or /sys.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from arltuner.guard import ENV_FLAG, AuditLog, Confirmation, GuardRefused, Refusal, Request, WriteGuard, default_m1_policy, read_all
from arltuner.health import make_health_gate, take_snapshot
from tests.guard.fakes import FakeClock, RecordingTransport

from .fakeroot import DEFAULT_MODULES, FakeRootCase


def reference_number(path: Path, base: int) -> int | None:
    """The pre-G-211 ``_read_number``."""
    try:
        with open(path, "rb") as handle:
            return int(handle.read(256).decode("ascii", "replace").strip(), base)
    except (OSError, ValueError):
        return None


def reference_thermal(root: Path) -> dict[str, int] | None:
    base = root / "sys" / "class" / "thermal"
    found: dict[str, int] = {}
    try:
        zones = sorted(n for n in os.listdir(base) if n.startswith("thermal_zone"))
    except OSError:
        zones = []
    for zone in zones:
        value = reference_number(base / zone / "temp", 10)
        if value is not None:
            found[zone] = value
    return found or None


def reference_machinecheck(root: Path) -> dict[str, int] | None:
    base = root / "sys" / "devices" / "system" / "machinecheck"
    found: dict[str, int] = {}
    try:
        cpus = sorted(n for n in os.listdir(base) if n.startswith("machinecheck"))
    except OSError:
        cpus = []
    for cpu in cpus:
        try:
            banks = sorted(n for n in os.listdir(base / cpu) if n.startswith("bank"))
        except OSError:
            banks = []
        for bank in banks:
            value = reference_number(base / cpu / bank, 16)
            if value is not None:
                found[f"{cpu}/{bank}"] = value
    return found or None


class SnapshotEquivalence(FakeRootCase):
    def awkward_tree(self) -> None:
        thermal = self.root.path / "sys" / "class" / "thermal"
        for zone, content in (("thermal_zone0", "45000\n"), ("thermal_zone1", ""), ("thermal_zone2", "garbage\n"), ("thermal_zone3", "  -3000  \n"), ("thermal_zone4", "9" * 300)):
            (thermal / zone).mkdir(parents=True)
            (thermal / zone / "temp").write_text(content, encoding="ascii")
        (thermal / "thermal_zone5" / "temp").mkdir(parents=True)  # a directory where the file should be
        (thermal / "thermal_zone6").mkdir()  # no temp file at all
        (thermal / "cooling_device0").mkdir()
        mc = self.root.path / "sys" / "devices" / "system" / "machinecheck"
        for cpu, banks in (("machinecheck0", {"bank0": "ffff\n", "bank1": "0xBE00\n", "bank2": "zz\n", "bank3": "", "bank10": "1\n"}), ("machinecheck1", {}), ("machinecheck2", {"bank0": "f" * 300})):
            (mc / cpu).mkdir(parents=True)
            for bank, content in banks.items():
                (mc / cpu / bank).write_text(content, encoding="ascii")
        (mc / "machinecheck0" / "bank4").mkdir()  # a directory named like a bank
        (mc / "machinecheck0" / "tolerant").write_text("1\n", encoding="ascii")
        (mc / "machinecheckX").write_text("a file named like a cpu directory\n", encoding="ascii")

    def test_informational_values_equal_the_pathlib_reference_on_an_awkward_tree(self) -> None:
        self.awkward_tree()
        snap = take_snapshot(self.root.path)
        self.assertEqual(dict(snap.thermal_mc or {}), reference_thermal(self.root.path))
        self.assertEqual(dict(snap.machinecheck or {}), reference_machinecheck(self.root.path))
        self.assertEqual(snap.thermal_mc, {"thermal_zone0": 45000, "thermal_zone3": -3000, "thermal_zone4": int("9" * 256)})
        self.assertEqual(snap.machinecheck, {"machinecheck0/bank0": 0xFFFF, "machinecheck0/bank1": 0xBE00, "machinecheck0/bank10": 1, "machinecheck2/bank0": int("f" * 256, 16)})

    def test_no_informational_files_at_all_is_none_not_empty(self) -> None:
        snap = take_snapshot(self.root.path)
        self.assertIsNone(snap.thermal_mc)
        self.assertIsNone(snap.machinecheck)
        self.assertEqual((snap.taint, snap.pci, snap.modules), (0, frozenset(os.listdir(self.root.pci_dir)), frozenset(DEFAULT_MODULES)))

    def test_a_root_given_as_a_path_or_a_string_gives_the_same_snapshot(self) -> None:
        self.awkward_tree()
        a, b = take_snapshot(self.root.path), take_snapshot(str(self.root.path))  # type: ignore[arg-type]
        self.assertEqual(dataclasses.replace(a, taken_at=0.0), dataclasses.replace(b, taken_at=0.0))


class RealGateStopsTheGuard(FakeRootCase):
    """The real ``make_health_gate`` behind a real ``WriteGuard``: each injected fault is a refusal (before) or a trip (after)."""

    TOKEN = "tok-g211"

    def setUp(self) -> None:
        super().setUp()
        self.transport = RecordingTransport()
        self.audit_path = self.dir / "audit.jsonl"
        self.confirm = Confirmation(self.TOKEN, "yk")
        self.root.set_taint(1 << 12)

    def guard(self) -> WriteGuard:
        policy = dataclasses.replace(default_m1_policy(), max_operations_per_session=10, min_dwell_s=0.0)
        return WriteGuard(
            self.transport, policy, AuditLog(self.audit_path, fsync=False), session_token=self.TOKEN, boot_id_fn=lambda: "test-boot", env={ENV_FLAG: "1"},
            health_gate=make_health_gate(self.root.path), lock_path=self.dir / "guard.lock", clock=FakeClock(1000.0), wall_clock=FakeClock(1_700_000_000.0),
        )

    @staticmethod
    def read_req() -> Request:
        return Request(0, 0x10, 0, 0, 0, "read")

    def faults(self) -> dict[str, tuple]:
        return {
            "new taint bit": (lambda: self.root.set_taint((1 << 12) | (1 << 3)), "new taint bit(s) 3"),
            "cleared taint bit": (lambda: self.root.set_taint(0), "cleared"),
            "pci lost": (lambda: self.root.remove_pci("0000:2b:00.0"), "PCI lost: 0000:2b:00.0"),
            "pci added": (lambda: self.root.add_pci("0000:ff:00.0"), "PCI added: 0000:ff:00.0"),
            "module added": (lambda: self.root.set_modules((*DEFAULT_MODULES, "evil")), "modules added: evil"),
            "module removed": (lambda: self.root.set_modules(DEFAULT_MODULES[1:]), f"modules removed: {DEFAULT_MODULES[0]}"),
        }

    def test_a_fault_during_the_transaction_trips_the_guard(self) -> None:
        for label, (inject, text) in self.faults().items():
            with self.subTest(label):
                self.setUp()
                guard = self.guard()
                self.transport.before_return = lambda call, inject=inject: inject()
                result = guard.execute(self.read_req(), self.confirm)
                self.assertEqual(result.response.rc, 0)
                self.assertTrue(guard.tripped, label)
                self.assertIn(text, guard.trip_reason)
                with self.assertRaises(GuardRefused) as cm:
                    guard.execute(self.read_req(), self.confirm)
                self.assertIs(cm.exception.reason, Refusal.TRIPPED)
                self.assertEqual(len(self.transport.calls), 1)
                self.assertTrue(any(r["event"] == "trip" for r in read_all(self.audit_path)))

    def test_every_transaction_takes_a_before_and_an_after_snapshot(self) -> None:
        guard = self.guard()
        takes: list[str] = []
        real = make_health_gate(self.root.path)

        def counting(phase: str) -> tuple[bool, str]:
            takes.append(phase)
            return real(phase)

        guard._health_gate = counting  # type: ignore[attr-defined]
        for _ in range(3):
            guard.execute(self.read_req(), self.confirm)
        self.assertEqual(takes, ["before", "after"] * 3)
        self.assertFalse(guard.tripped)
