"""The sampling thread must never touch the mailbox: no imports of mailbox/guard/transport, no wrmsr, 0x150 not readable."""
import ast
import unittest
from pathlib import Path

import arltuner.telemetry as pkg
from arltuner.telemetry import msrio

FORBIDDEN = ("arltuner.mailbox", "arltuner.guard", "arltuner.msr.transport", "arltuner.controls", "arltuner.cli")


class Isolation(unittest.TestCase):
    def test_no_forbidden_imports_or_writes(self):
        root = Path(pkg.__file__).parent
        files = sorted(root.glob("*.py"))
        self.assertGreaterEqual(len(files), 6)
        for f in files:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = "arltuner.telemetry" if node.level == 1 else (node.module or "")
                    mods = [base] + [f"{base}.{a.name}" for a in node.names]
                for m in mods:
                    self.assertFalse(m.startswith(FORBIDDEN), f"{f.name} imports {m}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("wrmsr", "write_msr"), f"{f.name} references {node.attr}")
                if isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, "wrmsr")

    def test_allowlist_excludes_mailbox(self):
        self.assertNotIn(msrio.MAILBOX_MSR, msrio.TELEMETRY_MSRS)
        self.assertNotIn(msrio.MAILBOX_MSR, msrio.TURBO_TABLE_MSRS)

    def test_turbo_tables_are_readable_only_through_the_measurement_allowlist(self):
        """G-118: ``vf.measure`` reads 0x1AD / 0x650 (rdmsr); the sampler's default allowlist still refuses them."""
        from arltuner.msr.port import MsrError

        class Port:
            def rdmsr(self, cpu, msr):
                return 0x3030303030303030

        self.assertNotIn(0x650, msrio.TELEMETRY_MSRS)
        with self.assertRaises(MsrError):
            msrio.TelemetryMsr(Port()).rdmsr(0, 0x650)
        wide = msrio.TelemetryMsr(Port(), msrio.TURBO_TABLE_MSRS)
        self.assertEqual((wide.rdmsr(0, 0x650), wide.rdmsr(0, 0x1AD)), (0x3030303030303030,) * 2)
        with self.assertRaises(MsrError):
            wide.rdmsr(0, msrio.MAILBOX_MSR)
        self.assertFalse(hasattr(wide, "wrmsr"))


if __name__ == "__main__":
    unittest.main()
