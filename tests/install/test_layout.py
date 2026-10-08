"""install.sh / uninstall.sh into a DESTDIR temp root (G-106). Ownership (root:root) cannot be verified from a user-run install: file lists, modes,
contents and idempotence are checked here; the real ownership check is the post-install self-check on the T1 install (tested with fake stat in
``test_selfcheck.py``, and here against the real DESTDIR tree with ``root_uid`` = the test user)."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from arltuner.helper.trust import check_code_tree, check_trusted_path

from ._support import REPO, copy_checkout, run_script

NOT_ROOT = os.geteuid() != 0  # tests that run a script WITHOUT a DESTDIR are only safe (they die at the root check) when we are not root

PY_FILES = sorted(p.relative_to(REPO / "src") for p in (REPO / "src" / "arltuner").rglob("*.py") if "__pycache__" not in p.parts)


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """relative path -> (mode, sha256 or 'dir'); __pycache__ contents are compared by name and mode only (they embed source mtimes)."""
    out: dict[str, tuple[int, str]] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        mode = stat.S_IMODE(p.lstat().st_mode)
        if p.is_dir():
            out[rel] = (mode, "dir")
        elif "__pycache__" in p.parts:
            out[rel] = (mode, "pyc")
        else:
            out[rel] = (mode, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.dest = self.tmp / "root"
        self.dest.mkdir()
        self.legacy = self.tmp / "legacy-audit.jsonl"

    def install(self, *args: str, **kw) -> subprocess.CompletedProcess[str]:
        kw.setdefault("legacy_audit", self.legacy)
        return run_script(kw.pop("script", REPO / "install.sh"), *args, destdir=self.dest, **kw)

    def ok_install(self, *args: str, **kw) -> subprocess.CompletedProcess[str]:
        r = self.install(*args, **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def path(self, absolute: str) -> Path:
        return self.dest / absolute.lstrip("/")

    def mode(self, absolute: str) -> int:
        return stat.S_IMODE(self.path(absolute).lstat().st_mode)


class InstallLayout(InstallCase):
    def setUp(self) -> None:
        super().setUp()
        self.legacy.write_text('{"event":"attempt"}\n{"event":"result"}\n', encoding="utf-8")
        self.result = self.ok_install()

    def test_file_list_and_modes(self) -> None:
        got = {rel: m for rel, (m, kind) in snapshot(self.dest).items() if "__pycache__" not in rel and kind != "dir"}
        want: dict[str, int] = {f"usr/lib/arltuner/{rel.as_posix()}": 0o644 for rel in PY_FILES}
        want.update(
            {
                "usr/lib/arltuner/arltuner-helper": 0o755,
                "usr/lib/arltuner/arltuner-selfcheck": 0o755,
                "usr/lib/arltuner/platforms/arl-s-285k.toml": 0o644,
                "usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy": 0o644,
                "usr/lib/tmpfiles.d/arltuner.conf": 0o644,
                "etc/arltuner/policy.toml": 0o644,
                "etc/arltuner/helper.toml": 0o644,
                "etc/arltuner/accepted/CURRENT": 0o644,
                "etc/arltuner/accepted/EXPECTED-words.txt": 0o644,
                "etc/arltuner/accepted/profiles/e-p6-split3.toml": 0o644,
                "etc/arltuner/accepted/profiles/p-boot-ramp.toml": 0o644,
                "etc/arltuner/accepted/profiles/p-step7.toml": 0o644,
                "var/lib/arltuner/audit.jsonl": 0o600,
            }
        )
        want.update({f"usr/share/arltuner/web/{p.name}": 0o644 for p in (REPO / "web").iterdir() if p.is_file()})
        self.assertEqual(got, want)
        self.assertEqual(sorted(p for p in self.dest.rglob(".*")), [], "no stray hidden/staging files")

    def test_every_directory_is_0755_and_nothing_is_writable_by_others(self) -> None:
        for rel, (mode, _) in snapshot(self.dest).items():
            self.assertFalse(mode & 0o022, f"{rel} is group/world-writable ({mode:04o})")
            if (self.dest / rel).is_dir():
                self.assertEqual(mode, 0o755, rel)

    def test_code_is_precompiled_so_pycache_is_part_of_the_installed_tree(self) -> None:
        for rel in PY_FILES:
            pycs = list((self.path("/usr/lib/arltuner") / rel.parent / "__pycache__").glob(f"{rel.stem}.*.pyc"))
            self.assertEqual(len(pycs), 1, f"{rel}: expected one .pyc")
            self.assertEqual(stat.S_IMODE(pycs[0].stat().st_mode), 0o644)

    def test_pyc_records_the_final_path_not_the_destdir(self) -> None:
        pyc = next((self.path("/usr/lib/arltuner/arltuner") / "__pycache__").glob("__init__.*.pyc"))
        self.assertIn(b"/usr/lib/arltuner/arltuner/__init__.py", pyc.read_bytes())
        self.assertNotIn(str(self.dest).encode(), pyc.read_bytes())

    def test_seeded_config_comes_from_the_repo(self) -> None:
        current = (REPO / "profiles" / "CURRENT").read_text(encoding="utf-8")
        policy = next(line.split("=", 1)[1].strip() for line in current.splitlines() if line.startswith("policy"))
        self.assertEqual(self.path("/etc/arltuner/policy.toml").read_bytes(), (REPO / policy).read_bytes())
        self.assertEqual(self.path("/etc/arltuner/accepted/CURRENT").read_bytes(), (REPO / "profiles" / "CURRENT").read_bytes())
        self.assertEqual(self.path("/etc/arltuner/accepted/EXPECTED-words.txt").read_bytes(), (REPO / "profiles" / "EXPECTED-words.txt").read_bytes())
        for line in current.splitlines():
            if line.startswith("profile"):
                rel = line.split("=", 1)[1].strip()
                self.assertEqual(self.path(f"/etc/arltuner/accepted/{rel}").read_bytes(), (REPO / rel).read_bytes())
        words = self.path("/etc/arltuner/accepted/EXPECTED-words.txt")  # what the helper's accepted_dir points at (G-107): regular, 0644, one link
        self.assertEqual((stat.S_IMODE(words.lstat().st_mode), words.lstat().st_nlink, words.is_symlink()), (0o644, 1, False))
        self.assertEqual(tomllib.loads(self.path("/etc/arltuner/helper.toml").read_text()), {"allow_taint_bits": [2]})

    def test_audit_is_seeded_from_the_legacy_log_with_mode_0600(self) -> None:
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_bytes(), self.legacy.read_bytes())
        self.assertEqual(self.mode("/var/lib/arltuner/audit.jsonl"), 0o600)
        self.assertIn("seeded from", self.result.stdout)

    def test_the_polkit_action_and_tmpfiles_are_the_packaging_files(self) -> None:
        self.assertEqual(
            self.path("/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy").read_bytes(),
            (REPO / "packaging" / "io.github.arrowlake-tuner.helper.policy").read_bytes(),
        )
        self.assertEqual(self.path("/usr/lib/tmpfiles.d/arltuner.conf").read_bytes(), (REPO / "packaging" / "arltuner.tmpfiles.conf").read_bytes())

    def test_installed_tree_passes_the_helpers_own_trust_check_modulo_ownership(self) -> None:
        """The strongest check a user-run install allows: the real ``check_code_tree`` / ``check_trusted_path`` over the DESTDIR tree, with the
        test user standing in for root and ``/`` mapped to the temp root."""
        uid = os.geteuid()
        lstat = lambda p: os.lstat(self.dest / str(p).lstrip("/"))  # noqa: E731
        listdir = lambda p: os.listdir(self.dest / str(p).lstrip("/"))  # noqa: E731
        check_code_tree("/usr/lib/arltuner/arltuner", lstat_fn=lstat, listdir_fn=listdir, root_uid=uid)
        check_code_tree("/usr/lib/arltuner/platforms", lstat_fn=lstat, listdir_fn=listdir, root_uid=uid)
        check_code_tree("/etc/arltuner/accepted", lstat_fn=lstat, listdir_fn=listdir, root_uid=uid)
        for f in ("/usr/lib/arltuner/arltuner-helper", "/etc/arltuner/policy.toml", "/etc/arltuner/helper.toml", "/var/lib/arltuner/audit.jsonl"):
            check_trusted_path(f, want="file", lstat_fn=lstat, root_uid=uid)

    def test_the_installed_tree_imports_and_loads_its_own_platform_file(self) -> None:
        code = (
            "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1])\n"
            "import arltuner.helper.main, arltuner.platform.schema as s\n"
            "print(arltuner.helper.main.__file__); print(s.default_platform_path())\n"
        )
        lib = self.path("/usr/lib/arltuner")
        r = subprocess.run([sys.executable, "-I", "-c", code, str(lib)], capture_output=True, text=True, cwd="/", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        main_file, platform_file = r.stdout.split("\n")[:2]
        self.assertEqual(Path(main_file), lib / "arltuner" / "helper" / "main.py")
        self.assertEqual(Path(platform_file), lib / "platforms" / "arl-s-285k.toml")

    def test_the_launcher_names_the_installed_tree_and_python_in_isolated_mode(self) -> None:
        text = self.path("/usr/lib/arltuner/arltuner-helper").read_text()
        self.assertEqual(text.splitlines()[0], "#!/usr/bin/python3 -I")
        self.assertIn('sys.path.insert(0, "/usr/lib/arltuner")', text)
        self.assertIn("process_main", text)


class Idempotence(InstallCase):
    def test_a_second_run_changes_nothing_and_replaces_stale_code(self) -> None:
        self.ok_install()
        stale = self.path("/usr/lib/arltuner/arltuner/stale_module.py")
        stale.write_text("x = 1\n")
        first = snapshot(self.dest)
        first.pop("usr/lib/arltuner/arltuner/stale_module.py")
        second_run = self.ok_install()
        self.assertFalse(stale.exists(), "code is replaced, not merged: a deleted module must not survive")
        self.assertEqual(snapshot(self.dest), first)
        self.assertIn("kept", second_run.stdout)

    def test_operator_owned_config_and_the_audit_log_survive_a_rerun(self) -> None:
        self.legacy.write_text("legacy\n")
        self.ok_install()
        self.path("/etc/arltuner/policy.toml").write_text("# operator edit\n")
        self.path("/etc/arltuner/helper.toml").write_text("allow_taint_bits = [2, 5]\n")
        (self.path("/etc/arltuner/accepted") / "CURRENT").write_text("# newer acceptance\n")
        self.path("/var/lib/arltuner/audit.jsonl").write_text("legacy\nroot-wrote-this\n")
        self.legacy.write_text("a different legacy log\n")
        r = self.ok_install()
        self.assertEqual(self.path("/etc/arltuner/policy.toml").read_text(), "# operator edit\n")
        self.assertEqual(self.path("/etc/arltuner/helper.toml").read_text(), "allow_taint_bits = [2, 5]\n")
        self.assertEqual((self.path("/etc/arltuner/accepted") / "CURRENT").read_text(), "# newer acceptance\n")
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "legacy\nroot-wrote-this\n")
        self.assertIn("KEPT", r.stdout)

    def test_reseed_config_restores_the_repo_config_but_never_the_audit_log(self) -> None:
        self.ok_install()
        self.path("/etc/arltuner/policy.toml").write_text("# operator edit\n")
        self.path("/var/lib/arltuner/audit.jsonl").write_text("keep me\n")
        self.ok_install("--reseed-config")
        policy = [l.split("=", 1)[1].strip() for l in (REPO / "profiles/CURRENT").read_text().splitlines() if l.startswith("policy")][0]
        self.assertEqual(self.path("/etc/arltuner/policy.toml").read_bytes(), (REPO / policy).read_bytes())
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "keep me\n")

    def test_no_legacy_log_gives_an_empty_0600_audit_log(self) -> None:
        r = self.ok_install()  # self.legacy does not exist
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_bytes(), b"")
        self.assertEqual(self.mode("/var/lib/arltuner/audit.jsonl"), 0o600)
        self.assertIn("created empty", r.stdout)

    def test_a_symlinked_legacy_log_is_not_followed(self) -> None:
        secret = self.tmp / "secret"
        secret.write_text("TOP SECRET\n")
        self.legacy.symlink_to(secret)
        self.ok_install()
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_bytes(), b"")

    def test_an_existing_symlink_at_the_audit_path_is_left_alone(self) -> None:
        (self.dest / "var/lib/arltuner").mkdir(parents=True)
        target = self.tmp / "elsewhere"
        target.write_text("x")
        (self.dest / "var/lib/arltuner/audit.jsonl").symlink_to(target)
        self.ok_install()
        self.assertTrue((self.dest / "var/lib/arltuner/audit.jsonl").is_symlink())
        self.assertEqual(target.read_text(), "x")


class HostileCheckout(InstallCase):
    def test_group_writable_sources_and_a_umask_of_002_still_give_0644_0755(self) -> None:
        src = copy_checkout(self.tmp / "checkout", open_modes=True)
        self.ok_install(script=src / "install.sh", umask=0o002)
        for rel, (mode, _) in snapshot(self.dest).items():
            self.assertFalse(mode & 0o022, f"{rel} {mode:04o}")

    def test_no_code_of_the_checkout_runs_after_the_copy(self) -> None:
        """A planted ``compileall.py`` / ``sitecustomize.py`` / ``usercustomize.py`` in the checkout and as the cwd must never run (isolated mode, cwd /)."""
        src = copy_checkout(self.tmp / "checkout")
        marker = self.tmp / "PWNED"
        evil = f"open({str(marker)!r}, 'w').write('ran')\n"
        for name in ("compileall.py", "sitecustomize.py", "usercustomize.py", "arltuner.py"):
            (src / name).write_text(evil)
        self.ok_install(script=src / "install.sh", cwd=src, extra_env={"PYTHONPATH": str(src), "PYTHONSTARTUP": str(src / "sitecustomize.py")})
        self.assertFalse(marker.exists(), "checkout code ran during the install")

    def test_python_below_3_12_is_refused_before_anything_is_written(self) -> None:
        r = self.install(extra_env={"ARLTUNER_PYTHON": "/bin/false"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Python >= 3.12", r.stderr)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_a_current_file_that_points_outside_profiles_and_policies_is_refused(self) -> None:
        for bad in ("../../../etc/passwd", "/etc/passwd", "profiles/../x.toml"):
            src = copy_checkout(self.tmp / ("c" + str(abs(hash(bad)))))
            (src / "profiles" / "CURRENT").write_text(f"policy = {bad}\nprofile = profiles/p-step7.toml\n")
            r = self.install(script=src / "install.sh")
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("unacceptable path", r.stderr)

    def test_a_symlinked_profile_is_refused(self) -> None:
        src = copy_checkout(self.tmp / "checkout")
        (src / "profiles" / "p-step7.toml").unlink()
        (src / "profiles" / "p-step7.toml").symlink_to("/etc/passwd")
        r = self.install(script=src / "install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a regular file", r.stderr)

    def test_relative_destdir_is_refused(self) -> None:
        env = {"PATH": os.environ["PATH"], "DESTDIR": "relative/dir"}
        r = subprocess.run(["bash", str(REPO / "install.sh")], env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("absolute", r.stderr)

    @unittest.skipUnless(NOT_ROOT, "would really install when run as root")
    def test_without_a_destdir_a_non_root_run_refuses_and_writes_nothing(self) -> None:
        for script in ("install.sh", "uninstall.sh"):
            r = subprocess.run(["bash", str(REPO / script)], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, cwd=self.tmp)
            self.assertNotEqual(r.returncode, 0, script)
            self.assertIn("must run as root", r.stderr)


class Uninstall(InstallCase):
    def setUp(self) -> None:
        super().setUp()
        self.legacy.write_text("legacy\n")
        self.ok_install()

    def uninstall(self, *args: str) -> subprocess.CompletedProcess[str]:
        r = run_script(REPO / "uninstall.sh", *args, destdir=self.dest)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_uninstall_removes_code_polkit_tmpfiles_and_web_but_keeps_config_and_the_audit_log(self) -> None:
        r = self.uninstall()
        for gone in ("/usr/lib/arltuner", "/usr/share/arltuner", "/usr/lib/tmpfiles.d/arltuner.conf", "/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy"):
            self.assertFalse(self.path(gone).exists(), gone)
        self.assertTrue(self.path("/var/lib/arltuner/audit.jsonl").is_file())
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "legacy\n")
        self.assertTrue(self.path("/etc/arltuner/policy.toml").is_file())
        self.assertIn("--purge", r.stdout)

    def test_uninstall_is_idempotent(self) -> None:
        self.uninstall()
        self.uninstall()
        self.assertTrue(self.path("/var/lib/arltuner/audit.jsonl").is_file())

    def test_purge_removes_config_and_state_too(self) -> None:
        self.uninstall("--purge")
        self.assertFalse(self.path("/var/lib/arltuner").exists())
        self.assertFalse(self.path("/etc/arltuner").exists())
        self.uninstall("--purge")

    def test_reinstall_after_uninstall_keeps_the_old_audit_log(self) -> None:
        self.uninstall()
        self.legacy.write_text("different\n")
        self.ok_install()
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "legacy\n")

    def test_unknown_argument_is_refused(self) -> None:
        r = run_script(REPO / "uninstall.sh", "--everything", destdir=self.dest)
        self.assertNotEqual(r.returncode, 0)


class SelfcheckOnTheRealInstallTree(InstallCase):
    def test_every_selfcheck_item_passes_on_the_destdir_tree_with_the_test_user_as_root(self) -> None:
        self.ok_install()
        (self.dest / "run/arltuner").mkdir(parents=True)  # tmpfiles.d's job on a real system
        spec = importlib.util.spec_from_file_location("arltuner_selfcheck", REPO / "packaging" / "selfcheck.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        lstat = lambda p: os.lstat(self.dest / str(p).lstrip("/"))  # noqa: E731
        listdir = lambda p: os.listdir(self.dest / str(p).lstrip("/"))  # noqa: E731
        read = lambda p: (self.dest / str(p).lstrip("/")).read_bytes()  # noqa: E731
        results = mod.selfcheck(lstat_fn=lstat, listdir_fn=listdir, read_fn=read, root_uid=os.geteuid())
        self.assertEqual([r for r in results if r[1] is not None], [])
        self.assertGreaterEqual(len(results), 12)


if __name__ == "__main__":
    unittest.main()
