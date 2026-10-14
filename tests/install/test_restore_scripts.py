"""G-129 (D-036): the Linux restore scripts read the USER's accepted state (``/etc/arltuner/accepted`` by default, ``--from DIR``), never a path in the repo.

Only the paths that stop BEFORE any sudo / mailbox traffic are run here (no accepted state, an unusable CURRENT, a refused CONFIRM): no hardware, no root.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ._support import REPO

SCRIPTS = ("tools/lnx/restore_tuning.sh", "tools/lnx/reapply_current.sh")


def run(rel: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(REPO / rel), *args], input=stdin, capture_output=True, text=True, timeout=60, cwd="/",
                          env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "USER": "tester"})


class Static(unittest.TestCase):
    def test_the_scripts_read_the_installed_accepted_state_not_the_repo(self) -> None:
        for rel in SCRIPTS:
            text = (REPO / rel).read_text()
            self.assertIn("/etc/arltuner/accepted", text, rel)
            self.assertIn("--from", text, rel)
            self.assertNotIn("profiles/CURRENT", text, rel)
            self.assertNotIn("profiles/EXPECTED-words.txt", text, rel)
            self.assertNotIn("policies/", text, rel)

    def test_the_windows_script_has_no_inline_words_of_a_machine(self) -> None:
        text = (REPO / "tools/win/restore_tuning.ps1").read_text()
        self.assertRegex(text, r"\[string\]\$From = \(Join-Path \$env:ProgramData 'arltuner\\accepted'\)")
        self.assertIn("EXPECTED-words.txt", text)
        for needle in ("p-step7", "F9A00035", "FC600037", "$Expected = [ordered]@{\n"):
            self.assertNotIn(needle, text)
        self.assertNotIn("profiles\\CURRENT", (REPO / "tools/win/reapply_current.ps1").read_text())

    def test_the_windows_scripts_need_the_fingerprint_before_they_touch_the_driver(self) -> None:
        """G-138: no map of any chip in the scripts; the accepted state's fingerprint.json is checked first and handed to every --device-win command."""
        for rel in ("tools/win/restore_tuning.ps1", "tools/win/reapply_current.ps1"):
            text = (REPO / rel).read_text()
            self.assertIn("Re-accept on Linux to record the fingerprint", text, rel)
            self.assertIn("'--fingerprint', $fingerprint", text, rel)
            self.assertLess(text.index("fingerprint.json in"), text.index("--device-win"), rel)
            for word in ("0x5B439", "5B439", "WINDOWS_LP_OF_CPU", "fused-0x09-cpu"):
                self.assertNotIn(word, text, rel)
        text = (REPO / "tools/win/restore_tuning.ps1").read_text()
        self.assertLess(text.index("fingerprint.json in"), text.index("Ts-Driver start"))


class OneProfile(unittest.TestCase):
    """G-147 / G-148 (D-044): both restore scripts apply THE one accepted profile with --neutralize-residual (no chain, no --accepted-before) and judge the read-back
    with ``compare-accepted``, where an EQUIVALENT unit is a success. Text-level: the scripts cannot run without sudo / a driver. The Windows scripts get static
    checks only (no PowerShell here)."""

    def test_the_linux_script_applies_one_profile_and_uses_the_shared_verdict(self) -> None:
        text = (REPO / "tools/lnx/restore_tuning.sh").read_text()
        self.assertIn('lm "apply-$(basename "$prof" .toml)" apply "$from/$prof" --neutralize-residual', text)
        self.assertNotIn("--accepted-before", text)
        self.assertNotIn('for i in "${!profiles[@]}"', text)
        self.assertIn("compare-accepted --readback", text)
        self.assertIn('--fingerprint "$from/fingerprint.json"', text)
        self.assertNotIn("sys.exit(1 if bad else 0)", text)  # the inline compare is gone: one verdict for both scripts

    def test_the_windows_scripts_do_the_same(self) -> None:
        text = (REPO / "tools/win/restore_tuning.ps1").read_text()
        self.assertIn("@('apply', $prof, '--neutralize-residual')", text)
        self.assertIn("'compare-accepted'", text)
        self.assertIn("'--fingerprint' $fingerprint", text)
        self.assertNotIn("ConvertFrom-Json", text)  # the compare is the CLI's, on THIS run's read-back file
        for rel in ("tools/win/restore_tuning.ps1", "tools/win/reapply_current.ps1"):
            text = (REPO / rel).read_text()
            self.assertNotIn("--accepted-before", text, rel)
            self.assertNotIn("foreach ($prof in $profiles)", text, rel)
            self.assertIn("if ($profiles.Count -gt 1)", text, rel)
            self.assertIn("accepted-migrate", text, rel)
            self.assertLess(text.index("if ($profiles.Count -gt 1)"), text.index("ARLTUNER_ALLOW_MAILBOX"), rel)  # refused before anything is armed

    def test_the_write_order_and_the_oc_slot_rule_are_the_shared_codes_not_the_scripts(self) -> None:
        """G-151: each script makes ONE apply (the planner orders its writes: raising voltages first) and ONE compare-accepted (the OC point's slot rule); neither orders words
        or judges points itself. Static checks only for the PowerShell script."""
        sh = (REPO / "tools/lnx/restore_tuning.sh").read_text()
        ps = (REPO / "tools/win/restore_tuning.ps1").read_text()
        self.assertEqual(sh.count(' apply "$from/$prof"'), 1)
        self.assertEqual(ps.count("@('apply', $prof"), 1)
        self.assertEqual(sh.count("compare-accepted --readback"), 1)
        self.assertEqual(ps.count("'compare-accepted'"), 1)
        for text in (sh, ps):
            self.assertIn("G-151", text)
            self.assertNotIn("--point", text)  # no per-point writes of its own
        self.assertNotIn("EXPECTED_POINTS", ps)

    def test_reapply_current_is_the_restore(self) -> None:
        text = (REPO / "tools/lnx/reapply_current.sh").read_text()
        self.assertIn('exec bash "$(cd "$(dirname "$0")" && pwd)/restore_tuning.sh" "$@"', text)


class Stops(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.policy = self.tmp / "policy.toml"
        self.policy.write_text('cpus = "auto"\n')

    def state(self, current: str, words: bool = True) -> Path:
        acc = self.tmp / "accepted"
        shutil.rmtree(acc, ignore_errors=True)
        (acc / "profiles").mkdir(parents=True)
        (acc / "CURRENT").write_text(current)
        (acc / "profiles" / "mine.toml").write_text('[meta]\nname = "mine"\nplatform = "arl-s"\n')
        if words:
            (acc / "EXPECTED-words.txt").write_text("0  39 8\n")
        return acc

    def test_no_accepted_state_stops_with_the_way_forward(self) -> None:
        for rel in SCRIPTS:
            r = run(rel, "--from", str(self.tmp / "nothing"), "--policy", str(self.policy), "--check")
            self.assertEqual(r.returncode, 2, (rel, r.stdout, r.stderr))
            self.assertIn("no accepted state", r.stdout)
            self.assertIn("Set as accepted", r.stdout)

    def test_a_state_without_words_stops_restore(self) -> None:
        acc = self.state("profile = profiles/mine.toml\n", words=False)
        r = run(SCRIPTS[0], "--from", str(acc), "--policy", str(self.policy))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no accepted state", r.stdout)

    def test_an_unusable_profile_path_is_refused(self) -> None:
        for bad in ("profiles/../x.toml", "/etc/passwd", "profiles/nope.toml", "profiles/mine.txt"):
            acc = self.state(f"profile = {bad}\n")
            for rel in SCRIPTS:
                r = run(rel, "--from", str(acc), "--policy", str(self.policy))
                self.assertEqual(r.returncode, 2, (bad, rel, r.stdout))
                self.assertIn("unusable profile", r.stdout)

    def test_a_missing_policy_stops(self) -> None:
        acc = self.state("profile = profiles/mine.toml\n")
        r = run(SCRIPTS[0], "--from", str(acc), "--policy", str(self.tmp / "none.toml"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("policy", r.stdout)

    def test_a_legacy_policy_line_is_ignored_and_nothing_is_sent_without_confirm(self) -> None:
        acc = self.state("policy = policies/session-2026-10-02-points.toml\nprofile = profiles/mine.toml\n")
        r = run(SCRIPTS[0], "--from", str(acc), "--policy", str(self.policy), stdin="no\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"Policy:   {self.policy}", r.stdout)
        self.assertIn(f"{acc}/profiles/mine.toml", r.stdout)
        self.assertIn("not confirmed - nothing was sent", r.stdout)

    def test_an_older_chain_is_refused_with_the_migrate_command(self) -> None:  # G-148 (D-044): a chain is never replayed
        acc = self.state("profile = profiles/mine.toml\nprofile = profiles/mine.toml\n")
        for rel in SCRIPTS:
            r = run(rel, "--from", str(acc), "--policy", str(self.policy), stdin="CONFIRM\n")
            self.assertEqual(r.returncode, 2, (rel, r.stdout, r.stderr))
            self.assertIn("lists 2 profiles", r.stdout)
            self.assertIn("accepted-migrate --dir", r.stdout)
            self.assertNotIn("Type CONFIRM", r.stdout)  # refused before the confirmation: nothing armed

    def test_unknown_arguments_are_refused(self) -> None:
        for rel in SCRIPTS:
            self.assertEqual(run(rel, "--bogus").returncode, 2)


class RootRunsIsolatedPython(unittest.TestCase):
    """G-142 item 3: the restore scripts run python as root; user-writable code (PYTHONPATH, user site, cwd, script dir) must not be importable."""

    def test_every_python_command_in_the_scripts_is_isolated(self) -> None:
        import re
        for rel in SCRIPTS:
            for n, line in enumerate((REPO / rel).read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for m in re.finditer(r"\bpython3?\b(?! --version)(\s+\S+)?", line):
                    self.assertEqual(m.group(1).strip() if m.group(1) else "", "-I", f"{rel}:{n}: {line.strip()}")

    def _confirm(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("orchestrator_confirm_g142", REPO / "tools/lnx/orchestrator_confirm.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_orchestrator_confirm_inserts_the_isolated_flag(self) -> None:
        iso = self._confirm().isolate
        self.assertEqual(iso(["python3", "tools/arltuner.py", "read"]), ["python3", "-I", "tools/arltuner.py", "read"])
        self.assertEqual(iso(["python3", "-I", "x.py"]), ["python3", "-I", "x.py"])
        self.assertEqual(iso(["python3.13", "-u", "x.py"]), ["python3.13", "-I", "-u", "x.py"])
        self.assertEqual(iso(["python3", "-c", "print(1)"]), ["python3", "-I", "-c", "print(1)"])
        self.assertEqual(iso(["/usr/bin/python3", "-sI", "x.py"]), ["/usr/bin/python3", "-sI", "x.py"])
        self.assertEqual(iso(["cat", "x"]), ["cat", "x"])
        self.assertEqual(iso([]), [])

    def test_the_child_cannot_import_from_pythonpath_cwd_or_the_script_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            evil = Path(d) / "evil"
            evil.mkdir()
            (evil / "g142_planted.py").write_text("print('PLANTED RAN')\n")
            env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(evil)}
            for cwd in (str(evil), "/"):
                r = subprocess.run(["python3", str(REPO / "tools/lnx/orchestrator_confirm.py"), "python3", "-c", "import g142_planted"],
                                   capture_output=True, text=True, timeout=60, cwd=cwd, env=env)
                self.assertNotIn("PLANTED RAN", r.stdout)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("ModuleNotFoundError", r.stdout)

    def test_tools_arltuner_adds_src_from_its_own_path_under_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(["python3", "-I", str(REPO / "tools/arltuner.py"), "--help"], capture_output=True, text=True, timeout=60, cwd=d,
                               env={"PATH": os.environ["PATH"], "PYTHONPATH": d})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("topology", r.stdout)
