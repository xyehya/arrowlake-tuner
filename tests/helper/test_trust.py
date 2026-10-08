"""SPEC 4.4 trust-path checks with injected fake stat results. Nothing is installed, chowned or stat-ed for real."""
from __future__ import annotations

import stat
import types
import unittest
from pathlib import Path

from arltuner.helper.trust import TrustError, check_code_tree, check_trusted_path

from ._support import HelperCase, error_code

DIR = stat.S_IFDIR | 0o755
FILE = stat.S_IFREG | 0o644


def fake_lstat(tree: dict[str, tuple[int, int]]):
    def lstat(path: str):
        try:
            uid, mode = tree[path]
        except KeyError:
            raise FileNotFoundError(path) from None
        return types.SimpleNamespace(st_uid=uid, st_mode=mode)

    return lstat


def fake_listdir(tree: dict[str, tuple[int, int]]):
    def listdir(path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/", 1)[0] for p in tree if p.startswith(prefix) and p != path})

    return listdir


def good_tree() -> dict[str, tuple[int, int]]:
    return {
        "/": (0, DIR), "/etc": (0, DIR), "/etc/arltuner": (0, DIR), "/etc/arltuner/policy.toml": (0, FILE),
        "/var": (0, DIR), "/var/lib": (0, DIR), "/var/lib/arltuner": (0, DIR), "/var/lib/arltuner/audit.jsonl": (0, FILE),
        "/usr": (0, DIR), "/usr/lib": (0, DIR), "/usr/lib/arltuner": (0, DIR),
    }


class TrustPathTests(unittest.TestCase):
    def check(self, tree, path="/etc/arltuner/policy.toml", **kw) -> None:
        check_trusted_path(path, lstat_fn=fake_lstat(tree), **kw)

    def refused(self, tree, path="/etc/arltuner/policy.toml", **kw) -> TrustError:
        with self.assertRaises(TrustError) as ctx:
            self.check(tree, path, **kw)
        return ctx.exception

    def test_root_owned_tree_passes(self) -> None:
        self.check(good_tree())
        self.check(good_tree(), "/var/lib/arltuner/audit.jsonl")
        self.check(good_tree(), "/etc/arltuner", want="dir")
        check_code_tree("/usr/lib/arltuner", lstat_fn=fake_lstat(good_tree()), listdir_fn=fake_listdir(good_tree()))

    def test_user_owned_file_is_refused(self) -> None:
        tree = good_tree() | {"/etc/arltuner/policy.toml": (1000, FILE)}
        exc = self.refused(tree)
        self.assertEqual(exc.path, "/etc/arltuner/policy.toml")
        self.assertIn("not root", exc.reason)

    def test_group_or_world_writable_file_is_refused(self) -> None:
        for mode in (0o664, 0o666, 0o620, 0o602, 0o777):
            with self.subTest(mode=oct(mode)):
                exc = self.refused(good_tree() | {"/etc/arltuner/policy.toml": (0, stat.S_IFREG | mode)})
                self.assertIn("writable", exc.reason)

    def test_non_root_parent_is_refused_at_every_level(self) -> None:
        for parent in ("/", "/etc", "/etc/arltuner"):
            with self.subTest(parent=parent):
                exc = self.refused(good_tree() | {parent: (1000, DIR)})
                self.assertEqual(exc.path, parent)

    def test_writable_parent_is_refused_at_every_level(self) -> None:
        for parent in ("/", "/etc", "/etc/arltuner"):
            for mode in (0o775, 0o777, 0o1777):
                with self.subTest(parent=parent, mode=oct(mode)):
                    exc = self.refused(good_tree() | {parent: (0, stat.S_IFDIR | mode)})
                    self.assertEqual(exc.path, parent)

    def test_sticky_tmp_style_directory_is_not_trusted(self) -> None:
        self.refused(good_tree() | {"/etc/arltuner": (0, stat.S_IFDIR | 0o1777)})

    def test_symlinks_are_refused_in_the_leaf_and_in_parents(self) -> None:
        link = stat.S_IFLNK | 0o777
        self.assertIn("symlink", self.refused(good_tree() | {"/etc/arltuner/policy.toml": (0, link)}).reason)
        self.assertIn("symlink", self.refused(good_tree() | {"/etc/arltuner": (0, link)}).reason)

    def test_wrong_kind_is_refused(self) -> None:
        self.assertIn("regular file", self.refused(good_tree() | {"/etc/arltuner/policy.toml": (0, DIR)}).reason)
        self.assertIn("directory", self.refused(good_tree() | {"/etc/arltuner": (0, FILE)}).reason)
        self.assertIn("regular file", self.refused(good_tree(), "/etc/arltuner", want="file").reason)

    def test_missing_leaf_is_refused_unless_it_may_be_created(self) -> None:
        tree = good_tree()
        del tree["/var/lib/arltuner/audit.jsonl"]
        self.assertIn("does not exist", self.refused(tree, "/var/lib/arltuner/audit.jsonl").reason)
        self.check(tree, "/var/lib/arltuner/audit.jsonl", must_exist=False)  # the parent chain is trusted: the audit log may be created
        tree["/var/lib/arltuner"] = (1000, DIR)
        self.refused(tree, "/var/lib/arltuner/audit.jsonl", must_exist=False)  # but not in a user-owned directory

    def test_missing_parent_is_refused_even_if_the_leaf_may_be_created(self) -> None:
        tree = good_tree()
        del tree["/var/lib/arltuner"], tree["/var/lib/arltuner/audit.jsonl"]
        self.assertIn("does not exist", self.refused(tree, "/var/lib/arltuner/audit.jsonl", must_exist=False).reason)

    def test_relative_and_dotdot_paths_are_refused(self) -> None:
        self.assertIn("absolute", self.refused(good_tree(), "etc/arltuner/policy.toml").reason)
        self.assertIn("absolute", self.refused(good_tree(), "/etc/../tmp/x").reason)

    def test_stat_errors_are_refusals(self) -> None:
        def broken(path: str):
            raise PermissionError(13, "Permission denied")

        with self.assertRaises(TrustError):
            check_trusted_path("/etc/arltuner/policy.toml", lstat_fn=broken)

    def test_a_checkout_or_home_tree_is_not_a_trusted_code_tree(self) -> None:
        tree = good_tree() | {"/home": (0, DIR), "/home/u": (1000, DIR), "/home/u/src": (1000, DIR), "/home/u/src/arltuner": (1000, DIR)}
        with self.assertRaises(TrustError):
            check_code_tree("/home/u/src/arltuner", lstat_fn=fake_lstat(tree), listdir_fn=fake_listdir(tree))


PKG = "/usr/lib/arltuner"


def package_tree() -> dict[str, tuple[int, int]]:
    """An installed package: root-owned dirs and files, including a ``__pycache__``."""
    tree = good_tree()
    for rel, kind in (("arltuner", DIR), ("arltuner/__init__.py", FILE), ("arltuner/helper", DIR), ("arltuner/helper/core.py", FILE),
                      ("arltuner/helper/__pycache__", DIR), ("arltuner/helper/__pycache__/core.cpython-314.pyc", FILE)):
        tree[f"{PKG}/{rel}"] = (0, kind)
    return tree


class CodeTreeTests(unittest.TestCase):
    """Item 8: the whole installed package tree (every dir and file, __pycache__ included) is checked, not just its top directory."""

    def check(self, tree) -> None:
        check_code_tree(f"{PKG}/arltuner", lstat_fn=fake_lstat(tree), listdir_fn=fake_listdir(tree))

    def test_clean_tree_passes(self) -> None:
        self.check(package_tree())

    def test_every_node_is_examined(self) -> None:
        bad_nodes = {
            "writable py": (f"{PKG}/arltuner/helper/core.py", (0, stat.S_IFREG | 0o666)),
            "group-writable pyc": (f"{PKG}/arltuner/helper/__pycache__/core.cpython-314.pyc", (0, stat.S_IFREG | 0o664)),
            "user-owned pyc": (f"{PKG}/arltuner/helper/__pycache__/core.cpython-314.pyc", (1000, FILE)),
            "writable pycache dir": (f"{PKG}/arltuner/helper/__pycache__", (0, stat.S_IFDIR | 0o777)),
            "user-owned subpackage": (f"{PKG}/arltuner/helper", (1000, DIR)),
            "symlinked module": (f"{PKG}/arltuner/__init__.py", (0, stat.S_IFLNK | 0o777)),
            "fifo": (f"{PKG}/arltuner/__init__.py", (0, stat.S_IFIFO | 0o644)),
        }
        for name, (path, entry) in bad_nodes.items():
            with self.subTest(name), self.assertRaises(TrustError) as ctx:
                self.check(package_tree() | {path: entry})
            self.assertEqual(ctx.exception.path, path)

    def test_an_unlistable_directory_and_an_implausible_tree_are_refused(self) -> None:
        tree = package_tree()

        def broken(path: str):
            raise PermissionError(13, "Permission denied")

        with self.assertRaises(TrustError):
            check_code_tree(f"{PKG}/arltuner", lstat_fn=fake_lstat(tree), listdir_fn=broken)
        with self.assertRaises(TrustError):
            check_code_tree(f"{PKG}/arltuner", lstat_fn=fake_lstat(tree), listdir_fn=fake_listdir(tree), max_entries=2)


class HelperTrustTests(HelperCase):
    """The helper applies the check to the policy and audit paths before it reads or opens them, and refuses with REFUSED / TRUST_PATH."""

    def trusted_cfg(self, tree, **kw):
        return self.config(enforce_trust=True, lstat_fn=fake_lstat(tree), policy_path=Path("/etc/arltuner/policy.toml"),
                           audit_path=Path("/var/lib/arltuner/audit.jsonl"), **kw)

    def test_user_writable_policy_refuses_vf_dump_before_anything_is_read_or_sent(self) -> None:
        cfg = self.trusted_cfg(good_tree() | {"/etc/arltuner/policy.toml": (0, stat.S_IFREG | 0o666)})
        h = self.start(cfg)
        reply = h.call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(error_code(reply), "REFUSED")
        self.assertEqual(reply["error"]["detail"]["check"], "TRUST_PATH")
        self.assertEqual(reply["error"]["detail"]["path"], "/etc/arltuner/policy.toml")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_user_owned_audit_directory_refuses_vf_dump_and_audit_tail(self) -> None:
        cfg = self.trusted_cfg(good_tree() | {"/var/lib/arltuner": (1000, DIR)})
        h = self.start(cfg)
        for rid, op, args in (("d", "vf.dump", {"cpus": "0"}), ("t", "audit.tail", {})):
            reply = h.call(rid, op, args)
            self.assertEqual(error_code(reply), "REFUSED", reply)
            self.assertEqual(reply["error"]["detail"]["check"], "TRUST_PATH")
        self.assertEqual(self.sim.wrmsr_count, 0)

    def test_world_writable_parent_of_the_policy_refuses(self) -> None:
        cfg = self.trusted_cfg(good_tree() | {"/etc": (0, stat.S_IFDIR | 0o777)})
        reply = self.start(cfg).call("d", "vf.dump", {"cpus": "0"})
        self.assertEqual(reply["error"]["detail"]["path"], "/etc")

    def test_trust_checks_are_off_for_the_simulator_default_only(self) -> None:
        # enforce_trust defaults to False (temp paths can never be root-owned); the launcher turns it on for --device
        self.assertFalse(self.config().enforce_trust)


if __name__ == "__main__":
    unittest.main()
