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
        self.assertRegex(text, r"\[string\]\$From = \(Join-Path \$env:LOCALAPPDATA 'arltuner\\accepted'\)")
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
        self.assertLess(text.index("fingerprint.json in"), text.index("sc.exe start ThrottleStop"))


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

    def test_unknown_arguments_are_refused(self) -> None:
        for rel in SCRIPTS:
            self.assertEqual(run(rel, "--bogus").returncode, 2)
