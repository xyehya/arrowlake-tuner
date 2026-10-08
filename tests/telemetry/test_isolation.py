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


if __name__ == "__main__":
    unittest.main()
