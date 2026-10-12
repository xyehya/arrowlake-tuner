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
SOCKET_UNIT = (REPO / "packaging" / "arltuner-helper.socket").read_bytes()
SERVICE_UNIT = (REPO / "packaging" / "arltuner-helper.service").read_bytes()
HELPER_TOML = b'allow_taint_bits = [2]\n[access]\nusers = ["yk"]\ngroup = "arltuner"\n'
LAUNCHER = (REPO / "packaging" / "arltuner-helper").read_bytes()
CODE_TREE = "code tree /usr/lib/arltuner (top level, arltuner/, platforms/, every dir and file, __pycache__ included)"

GOOD: dict[str, tuple[int, int]] = {
    "/": (0, DIR), "/usr": (0, DIR), "/usr/lib": (0, DIR), "/usr/share": (0, DIR), "/usr/share/polkit-1": (0, DIR), "/usr/share/polkit-1/actions": (0, DIR),
    "/usr/lib/tmpfiles.d": (0, DIR), "/usr/lib/tmpfiles.d/arltuner.conf": (0, FILE),
    "/etc/systemd": (0, DIR), "/etc/systemd/system": (0, DIR), "/etc/systemd/system/arltuner-helper.socket": (0, FILE),
    "/etc/systemd/system/arltuner-helper.service": (0, FILE),
    "/usr/lib/arltuner": (0, DIR), "/usr/lib/arltuner/arltuner-helper": (0, EXE), "/usr/lib/arltuner/arltuner-selfcheck": (0, EXE),
    "/usr/lib/arltuner/arltuner": (0, DIR), "/usr/lib/arltuner/arltuner/__init__.py": (0, FILE),
    "/usr/lib/arltuner/arltuner/__pycache__": (0, DIR), "/usr/lib/arltuner/arltuner/__pycache__/__init__.cpython-314.pyc": (0, FILE),
    "/usr/lib/arltuner/platforms": (0, DIR), "/usr/lib/arltuner/platforms/arl-s.toml": (0, FILE),
    "/etc": (0, DIR), "/etc/arltuner": (0, DIR), "/etc/arltuner/policy.toml": (0, FILE), "/etc/arltuner/helper.toml": (0, FILE),
    "/etc/arltuner/accepted": (0, DIR), "/etc/arltuner/accepted/CURRENT": (0, FILE),
    "/var": (0, DIR), "/var/lib": (0, DIR), "/var/lib/arltuner": (0, DIR), "/var/lib/arltuner/audit.jsonl": (0, PRIVATE),
    "/run": (0, DIR), "/run/arltuner": (0, DIR),
}


def run(tree, launcher: bytes = LAUNCHER, files: dict[str, bytes] | None = None) -> dict[str, str | None]:
    contents = {
        str(selfcheck.HELPER): launcher,
        "/etc/systemd/system/arltuner-helper.socket": SOCKET_UNIT,
        "/etc/systemd/system/arltuner-helper.service": SERVICE_UNIT,
        "/etc/arltuner/helper.toml": HELPER_TOML,
    } | (files or {})

    def read(p) -> bytes:
        return contents[str(p)]

    results = selfcheck.selfcheck(lstat_fn=fake_lstat(tree), listdir_fn=fake_listdir(tree), read_fn=read)
    return dict(results)


def failures(tree, **kw) -> dict[str, str]:
    return {k: v for k, v in run(tree, **kw).items() if v is not None}


class Good(unittest.TestCase):
    def test_a_correct_install_passes_every_check(self) -> None:
        results = run(GOOD)
        self.assertEqual({k: v for k, v in results.items() if v}, {})
        self.assertEqual(len(results), 20)
        self.assertFalse([k for k in results if "polkit" in k and "old" not in k])  # no polkit action/rules checks any more, only "no old polkit action file"

    def test_the_audit_log_may_be_absent(self) -> None:
        tree = {k: v for k, v in GOOD.items() if k != "/var/lib/arltuner/audit.jsonl"}
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

    def test_the_old_polkit_action_file_must_be_gone(self) -> None:
        tree = GOOD | {"/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy": (0, FILE)}
        label, why = self.only(tree)
        self.assertEqual(label, "no old polkit action file")
        self.assertIn("must be removed", why)

    def test_unit_files_are_required(self) -> None:
        for name in ("socket", "service"):
            tree = {k: v for k, v in GOOD.items() if not k.endswith("." + name)}
            label, why = self.only(tree)
            self.assertIn(f"arltuner-helper.{name}", label)
            self.assertIn("none of", why)

    def test_a_unit_in_usr_lib_systemd_is_found_too(self) -> None:
        tree = {k: v for k, v in GOOD.items() if not k.endswith(".socket")} | {
            "/usr/lib/systemd": (0, DIR), "/usr/lib/systemd/system": (0, DIR), "/usr/lib/systemd/system/arltuner-helper.socket": (0, FILE)}
        self.assertEqual(failures(tree, files={"/usr/lib/systemd/system/arltuner-helper.socket": SOCKET_UNIT}), {})

    def test_a_user_owned_or_group_writable_unit_is_refused_and_not_read(self) -> None:
        for change in ((1000, FILE), (0, stat.S_IFREG | 0o664)):
            tree = GOOD | {"/etc/systemd/system/arltuner-helper.service": change}
            bad = failures(tree)
            self.assertEqual(list(bad), ["systemd service unit arltuner-helper.service"])
            self.assertNotIn("systemd service unit arltuner-helper.service: content", run(tree))

    def test_no_group_is_required_any_more(self) -> None:
        self.assertFalse([k for k in run(GOOD) if "group" in k])

    def test_helper_toml_is_required_and_must_admit_somebody(self) -> None:
        label, _ = self.only({k: v for k, v in GOOD.items() if k != "/etc/arltuner/helper.toml"})
        self.assertEqual(label, "helper.toml")
        for text in (b"allow_taint_bits = [2]\n", b"[access]\nusers = []\n", b"[access\n", b'[access]\nusr = ["a"]\n'):
            with self.subTest(text=text):
                label, why = self.only(GOOD, files={"/etc/arltuner/helper.toml": text})
                self.assertIn("[access]", label)
        label, why = self.only(GOOD, files={"/etc/arltuner/helper.toml": b"allow_taint_bits = [2]\n"})
        self.assertIn("--add-user", why)

    def test_users_only_or_group_only_access_is_fine(self) -> None:
        for text in (b'[access]\nusers = ["a"]\n', b'[access]\ngroup = "arltuner"\n'):
            self.assertEqual(failures(GOOD, files={"/etc/arltuner/helper.toml": text}), {})

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


class UnitContent(unittest.TestCase):
    def bad(self, fn, text: bytes) -> str:
        with self.assertRaises(ValueError) as ctx:
            fn(text)
        return str(ctx.exception)

    def test_the_shipped_units_pass(self) -> None:
        selfcheck.check_socket_unit_text(SOCKET_UNIT)
        selfcheck.check_service_unit_text(SERVICE_UNIT)

    def test_the_shipped_units_say_what_the_task_says(self) -> None:
        sock = selfcheck.unit_sections(SOCKET_UNIT)["Socket"]
        self.assertEqual((sock["ListenStream"], sock["SocketMode"], sock["SocketUser"], sock["RemoveOnStop"]),
                         (["/run/arltuner/helper.sock"], ["0666"], ["root"], ["yes"]))
        self.assertNotIn("SocketGroup", sock)  # the peer check is the gate (D-043 amendment); no group is needed
        svc = selfcheck.unit_sections(SERVICE_UNIT)["Service"]
        self.assertEqual(svc["ExecStart"], ["/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket"])
        self.assertEqual((svc["Type"], svc["NoNewPrivileges"]), (["simple"], ["yes"]))
        self.assertNotIn("Install", selfcheck.unit_sections(SERVICE_UNIT))  # started by the socket, never enabled by itself

    def test_socket_mode_owner_group_and_path_are_pinned(self) -> None:
        for old, new in ((b"SocketMode=0666", b"SocketMode=0660"), (b"SocketMode=0666", b"SocketMode=0664"), (b"SocketMode=0666", b"SocketMode=0777"),
                         (b"SocketUser=root", b"SocketUser=yk"), (b"ListenStream=/run/arltuner/helper.sock", b"ListenStream=/run/user/1000/helper.sock"),
                         (b"Accept=no", b"Accept=yes")):
            with self.subTest(new=new):
                self.assertTrue(self.bad(selfcheck.check_socket_unit_text, SOCKET_UNIT.replace(old, new)))

    def test_a_second_listener_or_an_unknown_setting_is_refused(self) -> None:
        self.assertIn("ListenStream", self.bad(selfcheck.check_socket_unit_text, SOCKET_UNIT.replace(b"Accept=no", b"Accept=no\nListenStream=0.0.0.0:80")))
        self.assertIn("unexpected", self.bad(selfcheck.check_socket_unit_text, SOCKET_UNIT.replace(b"Accept=no", b"Accept=no\nFreeBind=yes")))

    def test_the_exec_start_is_pinned(self) -> None:
        pinned = b"ExecStart=/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket"
        for new in (b"ExecStart=/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device", b"ExecStart=/usr/bin/python3 /usr/lib/arltuner/arltuner-helper --device --socket",
                    b"ExecStart=/home/dev/checkout/tools/arltuner-helper.py --device --socket",
                    b"ExecStart=/usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket --sim"):
            with self.subTest(new=new):
                self.assertIn("ExecStart", self.bad(selfcheck.check_service_unit_text, SERVICE_UNIT.replace(pinned, new)))

    def test_a_second_exec_start_is_refused(self) -> None:
        self.assertIn("ExecStart", self.bad(selfcheck.check_service_unit_text, SERVICE_UNIT + b"ExecStart=/bin/sh\n"))

    def test_settings_that_break_the_helper_or_change_who_runs_it_are_refused(self) -> None:
        for extra in (b"User=nobody", b"DynamicUser=yes", b"PrivateDevices=yes", b"ProtectKernelTunables=yes", b"ProtectSystem=strict", b"ExecStartPre=/bin/true",
                      b"Environment=PYTHONPATH=/tmp", b"Group=users", b"InaccessiblePaths=/dev/cpu"):
            with self.subTest(extra=extra):
                self.assertIn("must not be set", self.bad(selfcheck.check_service_unit_text, SERVICE_UNIT + extra + b"\n"))

    def test_garbage_is_refused(self) -> None:
        self.assertIn("unparsable", self.bad(selfcheck.check_service_unit_text, b"[Service]\nnot a setting\n"))
        self.assertIn("[Service]", self.bad(selfcheck.check_service_unit_text, b"[Unit]\nDescription=x\n"))
        self.assertIn("[Socket]", self.bad(selfcheck.check_socket_unit_text, b"[Unit]\nDescription=x\n"))
        self.assertIn("UTF-8", self.bad(selfcheck.check_socket_unit_text, b"\xff\xfe"))

    def test_content_is_not_read_when_the_file_fails_the_trust_check(self) -> None:
        tree = GOOD | {"/etc/systemd/system/arltuner-helper.socket": (1000, FILE)}
        results = run(tree)
        self.assertNotIn("systemd socket unit arltuner-helper.socket: content", results)
        self.assertIsNotNone(results["systemd socket unit arltuner-helper.socket"])

    def test_the_unit_constants_match_the_helpers_service(self) -> None:
        from arltuner.helper import config, service

        self.assertEqual(selfcheck.SOCKET_PATH, service.SOCKET_PATH)
        self.assertEqual(selfcheck.SOCKET_PATH, config.INSTALLED_SOCKET)

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
