"""T-015 health-gate tests: the stateful ``health_gate(phase)`` callable, its use as ``WriteGuard(health_gate=...)``, and the
read-only discipline of the package source.

Rules: docs/SAFETY.md "Mailbox-session discipline" (acceptance = CLEAN delta), docs/tasks/T-013-guard.md (a False "before"
refuses with HEALTH_GATE; a False "after" trips the guard), docs/tasks/T-015-health-gate.md. Fake roots only: no real /proc or /sys.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import unittest
from pathlib import Path
from unittest import mock

from arltuner.guard import ENV_FLAG, AuditLog, Confirmation, GuardRefused, Policy, Refusal, Request, WriteGuard, default_m1_policy, read_all
from arltuner.health import HealthError, make_health_gate, take_snapshot
from arltuner.mailbox import VfWord
from tests.guard.fakes import FakeClock, RecordingTransport

from .fakeroot import DEFAULT_MODULES, DEFAULT_PCI, FakeRootCase

STOCK = VfWord(ratio=55, v_target_raw=0x4A0, static=False, offset_raw=0)
OUT_OF_SPEC_LIKE_BIT = 29  # arbitrary example index: the gate never learns which bit means what
SRC = Path(__file__).resolve().parents[2] / "src" / "arltuner" / "health"


class GateCase(FakeRootCase):
    def make_gate(self, **kwargs: object):  # type: ignore[no-untyped-def]
        return make_health_gate(self.root.path, **kwargs)  # type: ignore[arg-type]


class GateBasics(GateCase):
    def test_before_then_after_happy_path(self) -> None:
        gate = self.make_gate()
        ok, detail = gate("before")
        self.assertIs(ok, True)
        self.assertEqual(detail, f"snapshot taken: taint=0 pci={len(DEFAULT_PCI)} modules={len(DEFAULT_MODULES)}")
        ok, detail = gate("after")
        self.assertIs(ok, True)
        self.assertTrue(detail.startswith("CLEAN"), detail)

    def test_before_reports_the_taint_integer(self) -> None:
        self.root.set_taint(4 | (1 << 29))
        self.assertEqual(self.make_gate()("before")[1], f"snapshot taken: taint={4 | (1 << 29)} pci=3 modules=3")

    def test_after_without_before_fails_closed(self) -> None:
        ok, detail = self.make_gate()("after")
        self.assertIs(ok, False)
        self.assertIn("before", detail)

    def test_unknown_phase_fails_closed(self) -> None:
        gate = self.make_gate()
        gate("before")
        for phase in ("", "BEFORE", "During", "before ", None, 1, b"after"):
            with self.subTest(phase=phase):
                ok, detail = gate(phase)  # type: ignore[arg-type]
                self.assertIs(ok, False)
                self.assertIn("unknown", detail)
        self.assertIs(gate("after")[0], True)  # the unknown phases neither consumed nor replaced the snapshot

    def test_health_error_in_after_fails_closed(self) -> None:
        gate = self.make_gate()
        self.assertIs(gate("before")[0], True)
        self.root.tainted.unlink()
        ok, detail = gate("after")
        self.assertIs(ok, False)
        self.assertIn("taint", detail)

    def test_health_error_in_before_fails_closed_and_forgets_the_old_snapshot(self) -> None:
        gate = self.make_gate()
        self.assertIs(gate("before")[0], True)
        self.root.modules_file.unlink()
        self.assertIs(gate("before")[0], False)
        self.root.set_modules(DEFAULT_MODULES)  # health readable again -- but the earlier "before" must not be reused
        self.assertIs(gate("after")[0], False)

    def test_unexpected_exception_fails_closed(self) -> None:
        gate = self.make_gate()
        gate("before")
        with mock.patch("arltuner.health.gate.take_snapshot", side_effect=RuntimeError("boom")):
            ok, detail = gate("after")
        self.assertIs(ok, False)
        self.assertIn("boom", detail)

    def test_each_kind_of_change_fails_the_gate(self) -> None:
        changes = {
            "taint bit": lambda: self.root.set_taint(1 << OUT_OF_SPEC_LIKE_BIT),
            "pci lost": lambda: self.root.remove_pci("0000:2b:00.0"),
            "pci added": lambda: self.root.add_pci("0000:2c:00.0"),
            "module added": lambda: self.root.set_modules([*DEFAULT_MODULES, "vfio"]),
            "module removed": lambda: self.root.set_modules(DEFAULT_MODULES[:-1]),
        }
        for label, change in changes.items():
            with self.subTest(label):
                self.setUp()  # fresh fake root for each change
                gate = self.make_gate()
                self.assertIs(gate("before")[0], True)
                change()
                ok, detail = gate("after")
                self.assertIs(ok, False)
                self.assertTrue(detail.startswith("UNCLEAN"), detail)

    def test_informational_changes_alone_keep_the_gate_open(self) -> None:
        gate = self.make_gate()
        gate("before")
        self.root.set_thermal({"thermal_zone0": 99000})
        self.root.set_machinecheck({"machinecheck0/bank0": "ff"})
        self.assertIs(gate("after")[0], True)

    def test_expected_taint_bit_passes_any_other_fails(self) -> None:
        gate = self.make_gate(allowed_new_taint_bits=frozenset({OUT_OF_SPEC_LIKE_BIT}))
        gate("before")
        self.root.set_taint(1 << OUT_OF_SPEC_LIKE_BIT)
        ok, detail = gate("after")
        self.assertIs(ok, True, detail)
        self.root.set_taint((1 << OUT_OF_SPEC_LIKE_BIT) | (1 << 4))
        self.assertIs(gate("after")[0], False)

    def test_a_new_before_replaces_the_baseline(self) -> None:
        gate = self.make_gate()
        gate("before")
        self.root.set_taint(1 << OUT_OF_SPEC_LIKE_BIT)
        self.assertIs(gate("after")[0], False)
        self.assertIs(gate("before")[0], True)  # next transaction starts from the new (already tainted) state
        self.assertIs(gate("after")[0], True)

    def test_repeated_after_compares_with_the_same_before(self) -> None:
        gate = self.make_gate()
        gate("before")
        self.assertIs(gate("after")[0], True)
        self.assertIs(gate("after")[0], True)
        self.root.remove_pci("0000:00:1f.6")  # a device lost during the dwell is caught by the later re-check
        self.assertIs(gate("after")[0], False)

    def test_clock_is_injected(self) -> None:
        clock = FakeClock(50.0)
        gate = self.make_gate(clock=clock)
        with mock.patch("arltuner.health.gate.take_snapshot", wraps=take_snapshot) as spy:
            gate("before")
            clock.advance(1)
            gate("after")
        self.assertEqual([call.kwargs["clock"] for call in spy.call_args_list], [clock, clock])
        self.assertEqual([call.args[0] for call in spy.call_args_list], [self.root.path] * 2)

    def test_bad_allowed_bits_fail_at_construction(self) -> None:
        for bad in ({-1}, {"29"}, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(HealthError):
                    self.make_gate(allowed_new_taint_bits=bad)

    def test_default_root_is_the_real_root_but_is_never_read_here(self) -> None:
        self.assertEqual(inspect.signature(make_health_gate).parameters["root"].default, Path("/"))


class WithWriteGuard(GateCase):
    """The gate as ``WriteGuard(health_gate=...)`` with the recording fake transport (no hardware)."""

    TOKEN = "tok-15"

    def setUp(self) -> None:
        super().setUp()
        self.transport = RecordingTransport()
        self.transport.state[(0, 0x10, 0, 0)] = STOCK.to_u32()
        self.clock = FakeClock(1000.0)
        self.audit_path = self.dir / "audit.jsonl"
        self.confirm = Confirmation(self.TOKEN, "yk")

    def guard(self, gate) -> WriteGuard:  # type: ignore[no-untyped-def]
        policy: Policy = dataclasses.replace(default_m1_policy(), max_operations_per_session=10, min_dwell_s=0.0)
        return WriteGuard(
            self.transport,
            policy,
            AuditLog(self.audit_path, fsync=False),
            session_token=self.TOKEN,
            boot_id_fn=lambda: "test-boot",  # T-016: never read the real /proc in a test
            env={ENV_FLAG: "1"},
            health_gate=gate,
            lock_path=self.dir / "guard.lock",
            clock=self.clock,
            wall_clock=FakeClock(1_700_000_000.0),
        )

    @staticmethod
    def read_req() -> Request:
        return Request(0, 0x10, 0, 0, 0, "baseline read")

    @staticmethod
    def write_req() -> Request:
        return Request(0, 0x11, 0, 0, STOCK.with_(offset_raw=-5).to_u32(), "test write")

    def records(self) -> list[dict]:
        return read_all(self.audit_path)

    def test_healthy_transactions_pass_and_the_before_detail_is_audited(self) -> None:
        guard = self.guard(self.make_gate())
        guard.execute(self.read_req(), self.confirm)
        result = guard.execute(self.write_req(), self.confirm)
        self.assertTrue(result.verified)
        self.assertFalse(guard.tripped)
        attempt = next(r for r in self.records() if r["event"] == "attempt")
        self.assertEqual(attempt["health_gate"], {"before": "snapshot taken: taint=0 pci=3 modules=3"})

    def test_unreadable_health_before_refuses_and_sends_nothing(self) -> None:
        self.root.tainted.unlink()
        guard = self.guard(self.make_gate())
        with self.assertRaises(GuardRefused) as cm:
            guard.execute(self.read_req(), self.confirm)
        self.assertIs(cm.exception.reason, Refusal.HEALTH_GATE)
        self.assertIn("taint", cm.exception.detail)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(guard.tripped)
        self.assertEqual(self.records()[-1]["reason"], "HEALTH_GATE")

    def test_pci_device_lost_during_the_transaction_trips_the_guard(self) -> None:
        guard = self.guard(self.make_gate())
        self.transport.before_return = lambda call: self.root.remove_pci("0000:2b:00.0")  # the endpoint vanishes mid-transaction
        result = guard.execute(self.read_req(), self.confirm)  # T-013: the Result is still returned ...
        self.assertEqual(result.response.rc, 0)
        self.assertTrue(guard.tripped)  # ... but the guard is tripped
        trip = [r for r in self.records() if r["event"] == "trip"]
        self.assertEqual(len(trip), 1)
        self.assertIn("PCI lost: 0000:2b:00.0", trip[0]["detail"])
        with self.assertRaises(GuardRefused) as cm:
            guard.execute(self.read_req(), self.confirm)
        self.assertIs(cm.exception.reason, Refusal.TRIPPED)
        self.assertEqual(len(self.transport.calls), 1)

    def test_unexpected_taint_after_a_write_trips_but_the_expected_bit_does_not(self) -> None:
        def taint_on_write(bit: int):  # type: ignore[no-untyped-def]
            return lambda call: self.root.set_taint(1 << bit) if call[1] == 0x11 else None

        for label, allowed, bit, tripped in (("expected", {29}, 29, False), ("other", {29}, 30, True), ("none allowed", set(), 29, True)):
            with self.subTest(label):
                self.setUp()
                guard = self.guard(self.make_gate(allowed_new_taint_bits=frozenset(allowed)))
                guard.execute(self.read_req(), self.confirm)
                self.transport.before_return = taint_on_write(bit)
                result = guard.execute(self.write_req(), self.confirm)
                self.assertTrue(result.verified)
                self.assertIs(guard.tripped, tripped)
                if tripped:
                    trip = next(r for r in self.records() if r["event"] == "trip")
                    self.assertIn(f"new taint bit(s) {bit}", trip["detail"])

    def test_the_gate_brackets_every_transaction(self) -> None:
        seen: list[str] = []
        gate = self.make_gate()

        def spying(phase: str) -> tuple[bool, str]:
            seen.append(phase)
            return gate(phase)

        guard = self.guard(spying)
        guard.execute(self.read_req(), self.confirm)
        guard.execute(self.write_req(), self.confirm)
        self.assertEqual(seen, ["before", "after", "before", "after"])


class ReadOnlyDiscipline(unittest.TestCase):
    """Static checks on the package source: no subprocess/sleep/network, no writes except ``save_snapshot``'s new file."""

    FORBIDDEN_MODULES = {
        "subprocess", "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "asyncio", "multiprocessing", "ctypes", "shutil",
        "tempfile", "requests",
    }
    FORBIDDEN_ATTRS = {
        "sleep", "system", "popen", "Popen", "write_text", "write_bytes", "rename", "remove", "rmdir", "mkdir", "makedirs",
        "chmod", "chown", "truncate", "symlink", "touch", "removedirs", "rmtree", "urlopen", "connect",
    }

    def sources(self) -> dict[str, ast.Module]:
        files = sorted(SRC.glob("*.py"))
        self.assertEqual([f.name for f in files], ["__init__.py", "gate.py", "snapshot.py", "whea.py"])
        return {f.name: ast.parse(f.read_text(encoding="utf-8")) for f in files}

    def test_no_forbidden_imports_or_calls(self) -> None:
        for name, tree in self.sources().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    modules = {(node.module or "").split(".")[0]} if node.level == 0 else set()
                else:
                    modules = set()
                self.assertFalse(modules & self.FORBIDDEN_MODULES, f"{name}: imports {modules & self.FORBIDDEN_MODULES}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, self.FORBIDDEN_ATTRS, f"{name}:{node.lineno}")
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, self.FORBIDDEN_ATTRS, f"{name}:{node.lineno}")

    def test_open_is_read_binary_only(self) -> None:
        for name, tree in self.sources().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    self.assertIsInstance(mode, ast.Constant, f"{name}:{node.lineno}")
                    self.assertEqual(mode.value, "rb", f"{name}:{node.lineno}")  # type: ignore[union-attr]

    def test_os_level_writes_exist_only_inside_save_snapshot(self) -> None:
        writers = {"open", "write", "fsync", "unlink", "close"}
        for name, tree in self.sources().items():
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef):
                    continue
                for node in ast.walk(func):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "os"
                        and node.func.attr in writers
                    ):
                        self.assertEqual((name, func.name), ("snapshot.py", "save_snapshot"), f"os.{node.func.attr} at {name}:{node.lineno}")

    def test_save_snapshot_opens_exclusively(self) -> None:
        text = (SRC / "snapshot.py").read_text(encoding="utf-8")
        self.assertIn("os.O_CREAT | os.O_EXCL | os.O_WRONLY", text)
        self.assertEqual(text.count("os.open("), 1)

    def test_sources_are_lf_only(self) -> None:
        for path in SRC.glob("*.py"):
            self.assertNotIn(b"\r", path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
