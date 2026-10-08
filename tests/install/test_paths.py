"""G-106 path rules: the CLI's installed-detection (D-030: absent => legacy, trusted => installed paths, present-but-untrusted => named refusal,
never a fallback), the platform-file path order, the GUI launcher defaults, and the packaging files."""
from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arltuner import cli
from arltuner.helper import config
from arltuner.helper import main as helper_main
from arltuner.platform.schema import PLATFORM_FILE, default_platform_path
from tests import test_cli

from ._support import DIR, FILE, REPO, SYMLINK, fake_lstat

LEGACY_HOME = {"HOME": "/home/x"}
LEGACY_AUDIT = Path("/home/x/.local/state/linuxmon/audit.jsonl")
LEGACY_LOCK = Path("/run/lock/linuxmon-mailbox.lock")
ROOTED = {"/": (0, DIR), "/etc": (0, DIR), "/etc/arltuner": (0, DIR), "/etc/arltuner/policy.toml": (0, FILE)}


def absent(path: str):
    raise FileNotFoundError(path)


class Constants(unittest.TestCase):
    def test_installed_constants(self) -> None:
        self.assertEqual(config.INSTALLED_LOCK, Path("/run/arltuner/mailbox.lock"))
        self.assertEqual(config.INSTALLED_LOCK_DIR, Path("/run/arltuner"))
        self.assertEqual(config.INSTALLED_AUDIT, Path("/var/lib/arltuner/audit.jsonl"))
        self.assertEqual(config.INSTALLED_POLICY, Path("/etc/arltuner/policy.toml"))
        self.assertEqual(config.INSTALLED_HELPER, Path("/usr/lib/arltuner/arltuner-helper"))
        self.assertEqual(config.INSTALLED_WEB_DIR, Path("/usr/share/arltuner/web"))
        self.assertEqual(config.INSTALLED_PLATFORMS_DIR, Path("/usr/lib/arltuner/platforms"))

    def test_the_root_config_carries_the_accepted_state_directory(self) -> None:
        import argparse

        ns = argparse.Namespace(sim=False, device=True, policy="/tmp/evil", audit="/tmp/evil", lock="/tmp/evil", allow_taint_bit=[])
        cfg = helper_main.build_config(ns, log=lambda t: None, root=True)
        self.assertEqual(cfg.accepted_dir, Path("/etc/arltuner/accepted"))
        self.assertEqual(cfg.accepted_dir, config.INSTALLED_ACCEPTED_DIR)
        sim = argparse.Namespace(sim=True, device=False, policy=None, audit="/tmp/a", lock="/tmp/l", allow_taint_bit=[])
        self.assertIsNone(helper_main.build_config(sim, log=lambda t: None).accepted_dir)
        self.assertIsNone(config.HelperConfig.__dataclass_fields__["accepted_dir"].default)

    def test_the_helper_launcher_module_agrees_with_config(self) -> None:
        for name in ("INSTALLED_POLICY", "INSTALLED_AUDIT", "INSTALLED_HELPER_TOML", "INSTALLED_ACCEPTED_DIR"):
            self.assertEqual(getattr(helper_main, name), getattr(config, name), name)


class CliInstalledRule(unittest.TestCase):
    def test_absent_policy_keeps_the_legacy_paths(self) -> None:
        self.assertFalse(cli.installed_state(absent))
        self.assertEqual(cli.default_audit_path(LEGACY_HOME, lstat_fn=absent), LEGACY_AUDIT)
        self.assertEqual(cli.default_lock_path(isdir=lambda p: p == "/run/lock", lstat_fn=absent), LEGACY_LOCK)

    def test_a_missing_parent_directory_is_also_absent(self) -> None:
        def not_a_dir(path: str):
            raise NotADirectoryError(path)

        self.assertFalse(cli.installed_state(not_a_dir))
        self.assertFalse(cli.installed_state(fake_lstat({"/": (0, DIR)})))  # /etc does not exist at all

    def test_a_trusted_policy_switches_both_paths(self) -> None:
        lstat = fake_lstat(ROOTED)
        self.assertTrue(cli.installed_state(lstat))
        self.assertEqual(cli.default_audit_path(LEGACY_HOME, lstat_fn=lstat), config.INSTALLED_AUDIT)
        self.assertEqual(cli.default_lock_path(isdir=lambda p: True, lstat_fn=lstat), config.INSTALLED_LOCK)

    def test_windows_never_uses_the_installed_paths(self) -> None:
        lstat = fake_lstat(ROOTED)
        env = {"LOCALAPPDATA": r"C:\L", "HOME": "/home/x"}
        self.assertEqual(cli.default_audit_path(env, windows=True, lstat_fn=lstat), Path(r"C:\L") / "linuxmon" / "audit.jsonl")
        self.assertEqual(cli.default_audit_path({"HOME": "/home/x"}, windows=True, lstat_fn=lstat), LEGACY_AUDIT)
        self.assertNotEqual(cli.default_lock_path(isdir=lambda p: False, lstat_fn=lstat, windows=True), config.INSTALLED_LOCK)

    def test_present_but_untrusted_is_a_named_refusal_for_both_paths(self) -> None:
        cases = {
            "owner": ROOTED | {"/etc/arltuner/policy.toml": (1000, FILE)},
            "group-writable file": ROOTED | {"/etc/arltuner/policy.toml": (0, stat.S_IFREG | 0o664)},
            "world-writable file": ROOTED | {"/etc/arltuner/policy.toml": (0, stat.S_IFREG | 0o666)},
            "symlink": ROOTED | {"/etc/arltuner/policy.toml": (0, SYMLINK)},
            "directory": ROOTED | {"/etc/arltuner/policy.toml": (0, DIR)},
            "user-owned directory": ROOTED | {"/etc/arltuner": (1000, DIR)},
            "world-writable directory": ROOTED | {"/etc/arltuner": (0, stat.S_IFDIR | 0o777)},
            "world-writable /etc": ROOTED | {"/etc": (0, stat.S_IFDIR | 0o777)},
        }
        for name, tree in cases.items():
            lstat = fake_lstat(tree)
            with self.subTest(name):
                with self.assertRaises(cli.InstalledStateRefused) as ctx:
                    cli.default_audit_path(LEGACY_HOME, lstat_fn=lstat)
                self.assertIn("/etc/arltuner/policy.toml", str(ctx.exception))
                self.assertIn("not trusted", str(ctx.exception))
                self.assertIn("legacy", str(ctx.exception))
                with self.assertRaises(cli.InstalledStateRefused):
                    cli.default_lock_path(lstat_fn=lstat)

    def test_an_unreadable_policy_is_refused_not_ignored(self) -> None:
        def denied(path: str):
            raise PermissionError(13, "Permission denied", path)

        with self.assertRaises(cli.InstalledStateRefused):
            cli.installed_state(denied)


class CliEndToEnd(test_cli.CliCase):
    """The rule through ``cli.main``: untrusted => exit 2 before anything is sent; explicit --audit/--lock bypass the defaults."""

    def args(self, *extra: object) -> list[object]:
        return ["read-vf", "--sim", "--cpu", 4, *extra]

    def test_untrusted_install_refuses_before_any_request(self) -> None:
        lstat = fake_lstat(ROOTED | {"/etc/arltuner/policy.toml": (1000, FILE)})
        code, out, err = self.run_cli(*self.args(), stdin=self.confirmed(), lstat_fn=lstat)
        self.assertEqual(code, 2, err)
        self.assertIn("refused:", err)
        self.assertIn("/etc/arltuner/policy.toml", err)
        self.assertIn("not trusted", err)
        self.nothing_sent()

    def test_untrusted_install_also_refuses_when_only_the_lock_is_defaulted(self) -> None:
        lstat = fake_lstat(ROOTED | {"/etc/arltuner/policy.toml": (0, stat.S_IFREG | 0o666)})
        code, _, err = self.run_cli(*self.args("--audit", self.audit), stdin=self.confirmed(), lstat_fn=lstat)
        self.assertEqual(code, 2, err)
        self.assertIn("not trusted", err)
        self.nothing_sent()

    def test_explicit_audit_and_lock_do_not_consult_the_installed_state(self) -> None:
        lstat = fake_lstat(ROOTED | {"/etc/arltuner/policy.toml": (1000, FILE)})
        code, out, err = self.run_cli(*self.args("--audit", self.audit, "--lock", self.lock), stdin=self.confirmed(), lstat_fn=lstat)
        self.assertEqual((code, err), (0, ""), out)

    def test_a_trusted_install_routes_the_defaults_to_the_installed_paths(self) -> None:
        lstat = fake_lstat(ROOTED)
        opened: list[Path] = []

        def fake_audit(path):
            opened.append(Path(path))
            raise OSError("stop here: this test must not create /var/lib/arltuner")

        with mock.patch.object(cli, "AuditLog", side_effect=fake_audit), mock.patch.object(Path, "mkdir"):
            code, _, err = self.run_cli(*self.args(), stdin=self.confirmed(), lstat_fn=lstat)
        self.assertEqual(opened, [config.INSTALLED_AUDIT])
        self.assertEqual(code, 1)
        self.assertIn("/var/lib/arltuner/audit.jsonl", err)
        self.nothing_sent()

    def test_absent_install_uses_the_legacy_default_audit_path(self) -> None:
        opened: list[Path] = []

        def fake_audit(path):
            opened.append(Path(path))
            raise OSError("stop")

        with mock.patch.object(cli, "AuditLog", side_effect=fake_audit), mock.patch.object(Path, "mkdir"):
            self.run_cli(*self.args(), stdin=self.confirmed(), lstat_fn=absent, env={**test_cli.ENV_OK, "HOME": "/home/x"})
        self.assertEqual(opened, [LEGACY_AUDIT])

    def test_the_guard_gets_the_installed_lock_when_installed(self) -> None:
        lstat = fake_lstat(ROOTED)
        with mock.patch.object(cli, "WriteGuard", side_effect=RuntimeError("stop")) as guard:
            code, _, err = self.run_cli(*self.args("--audit", self.audit), stdin=self.confirmed(), lstat_fn=lstat)
        self.assertEqual(guard.call_args.kwargs["lock_path"], config.INSTALLED_LOCK)
        self.assertEqual(code, 3)


class PlatformPathOrder(unittest.TestCase):
    def make(self, root: Path, *, installed: bool, with_file: bool = True) -> Path:
        here = root / ("arltuner" if installed else "src/arltuner") / "platform" / "schema.py"
        here.parent.mkdir(parents=True)
        here.touch()
        if with_file:
            (root / "platforms").mkdir()
            (root / "platforms" / PLATFORM_FILE).touch()
        return here

    def test_installed_tree_uses_only_its_own_platforms_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "usr" / "lib" / "arltuner"
            here = self.make(root, installed=True)
            self.assertEqual(default_platform_path(here), root / "platforms" / PLATFORM_FILE)

    def test_installed_tree_never_falls_back_to_a_checkout_or_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "usr" / "lib" / "arltuner"
            here = self.make(root, installed=True, with_file=False)
            self.assertEqual(default_platform_path(here), root / "platforms" / PLATFORM_FILE)  # missing: the error message names it

    def test_a_checkout_uses_its_own_platforms_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            here = self.make(repo, installed=False)
            self.assertEqual(default_platform_path(here), repo / "platforms" / PLATFORM_FILE)

    def test_the_real_checkout_resolves_to_this_repo(self) -> None:
        self.assertEqual(default_platform_path(), REPO / "platforms" / PLATFORM_FILE)

    def test_the_installed_fallback_constant_matches_config(self) -> None:
        self.assertEqual(Path("/usr/lib/arltuner/platforms"), config.INSTALLED_PLATFORMS_DIR)


class GuiDefaults(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("arltuner_gui_g106", REPO / "tools" / "arltuner-gui.py")
        cls.gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gui)

    def test_helper_command_is_pkexec_of_the_installed_launcher_with_only_device(self) -> None:
        self.assertEqual(self.gui.default_helper_command(isfile=lambda p: True), ["pkexec", "/usr/lib/arltuner/arltuner-helper", "--device"])

    def test_no_installed_helper_means_no_default_helper(self) -> None:
        self.assertIsNone(self.gui.default_helper_command(isfile=lambda p: False))

    def test_only_the_exact_installed_path_is_probed(self) -> None:
        seen: list[str] = []
        self.gui.default_helper_command(isfile=lambda p: seen.append(p) or False)
        self.assertEqual(seen, [str(config.INSTALLED_HELPER)])

    def test_constants_match_config(self) -> None:
        self.assertEqual(self.gui.INSTALLED_HELPER, str(config.INSTALLED_HELPER))
        self.assertEqual(self.gui.INSTALLED_WEB_DIR, config.INSTALLED_WEB_DIR)

    def test_web_root_prefers_the_checkout_then_the_installed_directory(self) -> None:
        self.assertEqual(self.gui.default_web_root(Path("/co/web"), Path("/usr/share/arltuner/web"), isdir=lambda p: True), Path("/co/web"))
        self.assertEqual(self.gui.default_web_root(Path("/co/web"), Path("/usr/share/arltuner/web"), isdir=lambda p: False), Path("/usr/share/arltuner/web"))

    def test_help_documents_no_helper(self) -> None:
        with mock.patch("sys.stdout"), self.assertRaises(SystemExit):
            self.gui.main(["--help"])


class PackagingFiles(unittest.TestCase):
    def test_tmpfiles_creates_the_run_dir_0755_root(self) -> None:
        lines = [l for l in (REPO / "packaging" / "arltuner.tmpfiles.conf").read_text().splitlines() if l and not l.startswith("#")]
        self.assertEqual([l.split() for l in lines], [["d", "/run/arltuner", "0755", "root", "root", "-"]])

    def test_the_launcher_is_isolated_python_and_takes_no_argv_itself(self) -> None:
        text = (REPO / "packaging" / "arltuner-helper").read_text()
        self.assertEqual(text.splitlines()[0], "#!/usr/bin/python3 -I")
        code = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
        self.assertNotIn("argv", code)  # nothing is parsed or forwarded here: the helper itself refuses any argv as root
        self.assertIn(str(config.INSTALLED_CODE_DIR), text)

    def test_the_scripts_are_lf_and_executable_in_the_repo(self) -> None:
        for rel in ("install.sh", "uninstall.sh", "packaging/arltuner-helper", "packaging/selfcheck.py", "packaging/arltuner.tmpfiles.conf"):
            data = (REPO / rel).read_bytes()
            self.assertNotIn(b"\r", data, rel)
        for rel in ("install.sh", "uninstall.sh", "packaging/arltuner-helper"):
            self.assertTrue((REPO / rel).stat().st_mode & stat.S_IXUSR, rel)

    def test_install_scripts_never_use_dangerous_constructs(self) -> None:
        for rel in ("install.sh", "uninstall.sh"):
            text = (REPO / rel).read_text()
            self.assertIn("set -euo pipefail", text)
            self.assertNotIn("sudo ", text.replace("sudo bash", ""))
            self.assertNotIn("curl", text)
            self.assertNotIn("wget", text)
            self.assertNotIn("pip ", text)


if __name__ == "__main__":
    unittest.main()
