"""T-014 static rules, checked on the source: every mailbox request goes through the guard; no subprocess / network; the CLI never
sets ``ARLTUNER_ALLOW_MAILBOX`` and never takes the confirmation token as an argument.

Rule sources: docs/ARCHITECTURE.md ("only ``guard`` may call ``MailboxTransport.transact``"), docs/tasks/T-014-controls-cli.md
("Acceptance", "CLI"), docs/SAFETY.md ("Implementation requirements for write-capable code").
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "arltuner"
CONTROLS = sorted((SRC / "controls").glob("*.py"))
CLI_FILES = [SRC / "cli.py", SRC / "__main__.py"]
ALL = CONTROLS + CLI_FILES

FORBIDDEN_MODULES = {"subprocess", "socket", "urllib", "http", "ftplib", "ctypes", "asyncio", "multiprocessing", "pip", "shutil"}
FORBIDDEN_ATTRS = {"transact", "rdmsr", "wrmsr"}  # only guard.py may reach the transport; only msr/* touches a port


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module if node.level == 0 else "." * node.level + node.module)
    return found


class StaticRules(unittest.TestCase):
    def test_the_files_exist(self) -> None:
        self.assertGreaterEqual(len(CONTROLS), 5)  # __init__, model, planner, executor, report
        for path in ALL:
            self.assertTrue(path.is_file(), path)

    def test_no_subprocess_or_network(self) -> None:
        for path in ALL:
            roots = {m.split(".")[0] for m in imported_modules(parse(path))}
            self.assertEqual(roots & FORBIDDEN_MODULES, set(), path.name)

    def test_nothing_reaches_the_transport_or_a_port_except_through_the_guard(self) -> None:
        for path in ALL:
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, FORBIDDEN_ATTRS, f"{path.name}:{node.lineno} uses .{node.attr}")
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, FORBIDDEN_ATTRS, f"{path.name}:{node.lineno} uses {node.id}")

    def test_controls_depend_on_the_guard_not_on_the_msr_layer(self) -> None:
        for path in CONTROLS:
            mods = imported_modules(parse(path))
            self.assertFalse(any(m.startswith("arltuner.msr") for m in mods), path.name)
            self.assertFalse(any(m.startswith("arltuner.cli") for m in mods), path.name)  # no upward import

    def test_only_the_cli_builds_ports_and_only_through_the_msr_package(self) -> None:
        tree = parse(SRC / "cli.py")
        mods = imported_modules(tree)
        self.assertIn("arltuner.msr", mods)
        self.assertFalse(any(m.startswith("arltuner.msr.") for m in mods))  # public package API only

    def test_the_cli_never_writes_the_environment(self) -> None:
        for path in CLI_FILES:
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    base = node.value
                    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    self.assertNotIn(name, {"env", "environ"}, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"putenv", "unsetenv"}, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"setdefault", "update", "pop", "clear"}:
                        value = node.func.value
                        name = value.id if isinstance(value, ast.Name) else getattr(value, "attr", "")
                        self.assertNotIn(name, {"env", "environ"}, f"{path.name}:{node.lineno}")

    def test_the_cli_takes_no_token_argument(self) -> None:
        options = [
            node.value
            for node in ast.walk(parse(SRC / "cli.py"))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("--")
        ]
        self.assertIn("--allow-taint-bit", options)
        self.assertIn("--device", options)
        for option in options:
            self.assertNotIn("token", option)
            self.assertNotIn("confirm", option)


if __name__ == "__main__":
    unittest.main()
