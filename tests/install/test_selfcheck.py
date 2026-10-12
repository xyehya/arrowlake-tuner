"""The post-install self-check (``packaging/selfcheck.py``, installed as ``/usr/lib/arltuner/arltuner-selfcheck``), driven with fake stat results:
one good tree passes every item; each kind of damage fails the item that names it."""
from __future__ import annotations

import importlib.util
import stat
import unittest
from pathlib import Path

from ._support import DIR, FILE, REPO, SYMLINK, fake_listdir, fake_lstat

spec = importlib.util.spec_from_file_location("arltuner_selfcheck", REPO / "packaging" / "selfcheck.py")
selfcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selfcheck)

EXE = stat.S_IFREG | 0o755
PRIVATE = stat.S_IFREG | 0o600
POLKIT = (REPO / "packaging" / "io.github.arrowlake-tuner.helper.policy").read_bytes()
LAUNCHER = (REPO / "packaging" / "arltuner-helper").read_bytes()
CODE_TREE = "code tree /usr/lib/arltuner (top level, arltuner/, platforms/, every dir and file, __pycache__ included)"

GOOD: dict[str, tuple[int, int]] = {
    "/": (0, DIR), "/usr": (0, DIR), "/usr/lib": (0, DIR), "/usr/share": (0, DIR), "/usr/share/polkit-1": (0, DIR), "/usr/share/polkit-1/actions": (0, DIR),
    "/usr/lib/tmpfiles.d": (0, DIR), "/usr/lib/tmpfiles.d/arltuner.conf": (0, FILE),
    "/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy": (0, FILE),
    "/usr/lib/arltuner": (0, DIR), "/usr/lib/arltuner/arltuner-helper": (0, EXE), "/usr/lib/arltuner/arltuner-selfcheck": (0, EXE),
    "/usr/lib/arltuner/arltuner": (0, DIR), "/usr/lib/arltuner/arltuner/__init__.py": (0, FILE),
    "/usr/lib/arltuner/arltuner/__pycache__": (0, DIR), "/usr/lib/arltuner/arltuner/__pycache__/__init__.cpython-314.pyc": (0, FILE),
    "/usr/lib/arltuner/platforms": (0, DIR), "/usr/lib/arltuner/platforms/arl-s.toml": (0, FILE),
    "/etc": (0, DIR), "/etc/arltuner": (0, DIR), "/etc/arltuner/policy.toml": (0, FILE), "/etc/arltuner/helper.toml": (0, FILE),
    "/etc/arltuner/accepted": (0, DIR), "/etc/arltuner/accepted/CURRENT": (0, FILE),
    "/var": (0, DIR), "/var/lib": (0, DIR), "/var/lib/arltuner": (0, DIR), "/var/lib/arltuner/audit.jsonl": (0, PRIVATE),
    "/run": (0, DIR), "/run/arltuner": (0, DIR),
}


def run(tree, polkit: bytes = POLKIT, launcher: bytes = LAUNCHER, files: dict[str, bytes] | None = None) -> dict[str, str | None]:
    def read(p) -> bytes:
        p = str(p)
        if p in (files or {}):
            return files[p]
        return launcher if p == str(selfcheck.HELPER) else polkit

    results = selfcheck.selfcheck(lstat_fn=fake_lstat(tree), listdir_fn=fake_listdir(tree), read_fn=read)
    return dict(results)


def failures(tree, polkit: bytes = POLKIT, **kw) -> dict[str, str]:
    return {k: v for k, v in run(tree, polkit, **kw).items() if v is not None}


class Good(unittest.TestCase):
    def test_a_correct_install_passes_every_check(self) -> None:
        results = run(GOOD)
        self.assertEqual({k: v for k, v in results.items() if v}, {})
        self.assertGreaterEqual(len(results), 14)

    def test_optional_files_may_be_absent(self) -> None:
        tree = {k: v for k, v in GOOD.items() if k not in ("/etc/arltuner/helper.toml", "/var/lib/arltuner/audit.jsonl")}
        self.assertEqual(failures(tree), {})

    def test_the_accepted_state_is_optional(self) -> None:
        """D-036: a fresh install has no /etc/arltuner/accepted (it is the user's, written by the app)."""
        tree = {k: v for k, v in GOOD.items() if not k.startswith("/etc/arltuner/accepted")}
        self.assertEqual(failures(tree), {})
        self.assertTrue([k for k in run(tree) if "accepted state" in k])

    def test_an_accepted_directory_that_is_there_is_still_checked(self) -> None:
        tree = {k: v for k, v in GOOD.items() if k != "/etc/arltuner/accepted/CURRENT"} | {"/etc/arltuner/accepted/EXPECTED-words.txt": (1000, FILE)}
        self.assertTrue([k for k in failures(tree) if "accepted" in k])


class Damage(unittest.TestCase):
    def only(self, tree, **kw) -> tuple[str, str]:
        bad = failures(tree, **kw)
        self.assertEqual(len(bad), 1, bad)
        return next(iter(bad.items()))

    def test_a_user_owned_source_file_in_the_code_tree(self) -> None:
        label, why = self.only(GOOD | {"/usr/lib/arltuner/arltuner/__init__.py": (1000, FILE)})
        self.assertIn("code tree", label)
        self.assertIn("not root", why)

    def test_a_group_writable_pyc(self) -> None:
        label, why = self.only(GOOD | {"/usr/lib/arltuner/arltuner/__pycache__/__init__.cpython-314.pyc": (0, stat.S_IFREG | 0o664)})
        self.assertIn("code tree", label)
        self.assertIn("writable", why)

    def test_a_user_owned_platform_file(self) -> None:
        label, why = self.only(GOOD | {"/usr/lib/arltuner/platforms/arl-s.toml": (1000, FILE)})
        self.assertIn("code tree", label)
        self.assertIn("arl-s.toml", why)

    def test_a_symlinked_code_directory(self) -> None:
        label, why = self.only(GOOD | {"/usr/lib/arltuner/arltuner": (0, SYMLINK)})
        self.assertIn("code tree", label)
        self.assertIn("symlink", why)

    def test_a_user_owned_file_at_the_top_level_of_the_code_directory(self) -> None:
        label, why = self.only(GOOD | {"/usr/lib/arltuner/stray.py": (1000, FILE)})
        self.assertEqual(label, CODE_TREE)
        self.assertIn("stray.py", why)

    def test_a_launcher_with_other_content_is_refused(self) -> None:
        label, why = self.only(GOOD, launcher=LAUNCHER + b"# extra\n")
        self.assertIn("launcher content", label)
        self.assertIn("sha256", why)

    def test_a_launcher_with_another_shebang_is_refused(self) -> None:
        label, why = self.only(GOOD, launcher=b"#!/usr/bin/python3\n" + LAUNCHER.split(b"\n", 1)[1])
        self.assertIn("shebang", why)

    def test_the_pinned_launcher_hash_matches_the_packaged_launcher(self) -> None:
        import hashlib

        self.assertEqual(selfcheck.LAUNCHER_SHA256, hashlib.sha256(LAUNCHER).hexdigest())
        self.assertEqual(selfcheck.LAUNCHER_SHEBANG, LAUNCHER.split(b"\n", 1)[0])

    def test_leftover_staging_paths_are_flagged(self) -> None:
        for extra in (
            "/usr/lib/arltuner.old.123", "/usr/lib/.arltuner.new.9", "/usr/share/arltuner/web.old.4",
            "/etc/arltuner/.accepted.new.7", "/etc/arltuner/accepted.old.2", "/var/lib/arltuner/.audit.new.1",
        ):
            tree = GOOD | {extra: (0, DIR), "/usr/share/arltuner": (0, DIR), "/usr/share": (0, DIR)}
            label, why = self.only(tree)
            self.assertIn("leftover", label, extra)
            self.assertIn(extra, why)

    def test_other_packages_in_usr_lib_are_not_leftovers(self) -> None:
        tree = GOOD | {"/usr/lib/python3.old.1": (0, DIR), "/usr/lib/foo.new.2": (0, DIR)}
        self.assertEqual(failures(tree), {})

    def test_a_polkit_rule_mentioning_the_action_is_flagged(self) -> None:
        rule = b'polkit.addRule(function(a, s) { if (a.id == "io.github.arrowlake-tuner.helper") return polkit.Result.YES; });'
        for d in ("/etc/polkit-1/rules.d", "/usr/share/polkit-1/rules.d"):
            tree = GOOD | {d: (0, DIR), f"{d}/49-x.rules": (0, FILE), f"{d}/50-other.rules": (0, FILE)}
            label, why = self.only(tree, files={f"{d}/49-x.rules": rule, f"{d}/50-other.rules": b"// unrelated"})
            self.assertIn("polkit rules.d", label)
            self.assertIn("49-x.rules", why)
            self.assertNotIn("50-other", why)

    def test_unrelated_polkit_rules_are_fine(self) -> None:
        tree = GOOD | {"/etc/polkit-1/rules.d": (0, DIR), "/etc/polkit-1/rules.d/50-default.rules": (0, FILE)}
        self.assertEqual(failures(tree, files={"/etc/polkit-1/rules.d/50-default.rules": b"// wheel may do things"}), {})

    def test_a_missing_launcher(self) -> None:
        bad = failures({k: v for k, v in GOOD.items() if k != "/usr/lib/arltuner/arltuner-helper"})
        self.assertEqual(set(bad), {"helper launcher", "helper launcher is executable"})
        self.assertNotIn("helper launcher content = packaged launcher, isolated-python shebang", bad)

    def test_a_non_executable_launcher(self) -> None:
        label, why = self.only(GOOD | {"/usr/lib/arltuner/arltuner-helper": (0, FILE)})
        self.assertEqual(label, "helper launcher is executable")
        self.assertIn("not executable", why)

    def test_a_world_writable_parent_directory(self) -> None:
        bad = failures(GOOD | {"/usr/lib": (0, stat.S_IFDIR | 0o777)})
        self.assertTrue(bad)
        self.assertIn("helper launcher", bad)

    def test_a_missing_policy(self) -> None:
        label, why = self.only({k: v for k, v in GOOD.items() if k != "/etc/arltuner/policy.toml"})
        self.assertIn("policy", label)
        self.assertIn("does not exist", why)

    def test_a_user_owned_helper_toml(self) -> None:
        label, _ = self.only(GOOD | {"/etc/arltuner/helper.toml": (1000, FILE)})
        self.assertIn("helper.toml", label)

    def test_a_tampered_accepted_state(self) -> None:
        label, _ = self.only(GOOD | {"/etc/arltuner/accepted/CURRENT": (1000, FILE)})
        self.assertIn("accepted", label)

    def test_an_audit_log_readable_by_others(self) -> None:
        label, why = self.only(GOOD | {"/var/lib/arltuner/audit.jsonl": (0, FILE)})
        self.assertEqual(label, "audit log mode 0600")
        self.assertIn("0644", why)

    def test_a_user_owned_audit_log(self) -> None:
        bad = failures(GOOD | {"/var/lib/arltuner/audit.jsonl": (1000, PRIVATE)})
        self.assertIn("audit log (may be absent)", bad)

    def test_a_missing_run_directory(self) -> None:
        label, _ = self.only({k: v for k, v in GOOD.items() if k != "/run/arltuner"})
        self.assertIn("run dir", label)

    def test_a_user_owned_run_directory(self) -> None:
        label, _ = self.only(GOOD | {"/run/arltuner": (1000, DIR)})
        self.assertIn("run dir", label)


class PolkitContent(unittest.TestCase):
    def bad(self, text: bytes) -> str:
        with self.assertRaises(ValueError) as ctx:
            selfcheck.check_polkit_text(text)
        return str(ctx.exception)

    def test_the_shipped_file_passes(self) -> None:
        selfcheck.check_polkit_text(POLKIT)

    def test_auth_admin_keep_is_refused(self) -> None:
        self.assertIn("auth_admin", self.bad(POLKIT.replace(b"<allow_any>auth_admin<", b"<allow_any>auth_admin_keep<")))

    def test_allow_active_yes_is_refused(self) -> None:
        self.assertIn("allow_active", self.bad(POLKIT.replace(b"<allow_active>auth_admin<", b"<allow_active>yes<")))

    def test_another_exec_path_is_refused(self) -> None:
        self.assertIn("exec.path", self.bad(POLKIT.replace(b"/usr/lib/arltuner/arltuner-helper<", b"/home/dev/arrowlake-tuner/tools/arltuner-helper.py<")))

    def test_a_second_action_is_refused(self) -> None:
        extra = POLKIT.replace(b"</policyconfig>", b'<action id="io.github.arrowlake-tuner.other"><defaults/></action></policyconfig>')
        self.assertIn("exactly one action", self.bad(extra))

    def test_garbage_and_entities_are_refused(self) -> None:
        self.assertIn("XML", self.bad(b"not xml"))
        self.assertIn("entity", self.bad(b'<!DOCTYPE x [<!ENTITY a "b">]><policyconfig/>'))

    def test_content_is_not_read_when_the_file_fails_the_trust_check(self) -> None:
        tree = GOOD | {"/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy": (1000, FILE)}
        results = run(tree)
        self.assertNotIn("polkit action content", results)
        self.assertIsNotNone(results["polkit action file"])

    def test_the_constants_match_the_helpers_config(self) -> None:
        from arltuner.helper import config

        self.assertEqual(selfcheck.CODE_DIR, config.INSTALLED_CODE_DIR)
        self.assertEqual(selfcheck.HELPER, config.INSTALLED_HELPER)
        self.assertEqual(selfcheck.STATE_DIR, config.INSTALLED_STATE_DIR)
        self.assertEqual(selfcheck.STATE_DIR / "audit.jsonl", config.INSTALLED_AUDIT)
        self.assertEqual(selfcheck.RUN_DIR, config.INSTALLED_LOCK_DIR)
        self.assertEqual(selfcheck.ETC_DIR / "policy.toml", config.INSTALLED_POLICY)
        self.assertEqual(selfcheck.ETC_DIR / "helper.toml", config.INSTALLED_HELPER_TOML)
        self.assertEqual(selfcheck.ETC_DIR / "accepted", config.INSTALLED_ACCEPTED_DIR)
        self.assertEqual(Path(selfcheck.CODE_DIR) / "platforms", config.INSTALLED_PLATFORMS_DIR)


if __name__ == "__main__":
    unittest.main()
