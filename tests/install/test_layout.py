"""install.sh / uninstall.sh into a DESTDIR temp root (G-106). Ownership (root:root) cannot be verified from a user-run install: file lists, modes,
contents and idempotence are checked here; the real ownership check is the post-install self-check on the T1 install (tested with fake stat in
``test_selfcheck.py``, and here against the real DESTDIR tree with ``root_uid`` = the test user)."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
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

ADVANCED_POLICY = "policies/advanced-2026-10-11.toml"  # D-032: the seed source of /etc/arltuner/policy.toml

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
                self.assertEqual(mode, 0o700 if rel in ("var/lib/arltuner/curves", "var/lib/arltuner/profiles", "var/lib/arltuner/state", "var/lib/arltuner/stress") else 0o755, rel)  # G-111/G-112/G-116/G-123: the curve, profile, stress and last-read stores are root-only

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
        self.assertEqual(self.path("/etc/arltuner/policy.toml").read_bytes(), (REPO / ADVANCED_POLICY).read_bytes())  # D-032: the seed is the advanced policy
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
        self.path("/var/lib/arltuner/audit.jsonl").write_text("legacy\nroot-wrote-this\n")
        self.legacy.write_text("a different legacy log\n")
        r = self.ok_install()
        self.assertEqual(self.path("/etc/arltuner/policy.toml").read_text(), "# operator edit\n")
        self.assertEqual(self.path("/etc/arltuner/helper.toml").read_text(), "allow_taint_bits = [2, 5]\n")
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "legacy\nroot-wrote-this\n")
        self.assertIn("KEPT", r.stdout)

    def test_accepted_state_follows_the_repo_on_every_run(self) -> None:
        self.ok_install()
        (self.path("/etc/arltuner/accepted") / "EXPECTED-words.txt").write_text("stale\n")
        (self.path("/etc/arltuner/accepted") / "junk").write_text("x")
        self.ok_install()
        acc = self.path("/etc/arltuner/accepted")
        self.assertEqual((acc / "EXPECTED-words.txt").read_bytes(), (REPO / "profiles/EXPECTED-words.txt").read_bytes())
        self.assertFalse((acc / "junk").exists())

    def test_reseed_config_restores_the_repo_config_but_never_the_audit_log(self) -> None:
        self.ok_install()
        self.path("/etc/arltuner/policy.toml").write_text("# operator edit\n")
        self.path("/var/lib/arltuner/audit.jsonl").write_text("keep me\n")
        self.ok_install("--reseed-config")
        self.assertEqual(self.path("/etc/arltuner/policy.toml").read_bytes(), (REPO / ADVANCED_POLICY).read_bytes())
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

    def fake_proc(self, **cmdlines: list[str]) -> Path:
        proc = self.tmp / "proc"
        for pid, argv in cmdlines.items():
            (proc / pid.lstrip("p")).mkdir(parents=True)
            (proc / pid.lstrip("p") / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
        (proc / "self").mkdir(parents=True, exist_ok=True)
        return proc

    def test_a_running_helper_blocks_the_install_and_nothing_is_written(self) -> None:
        for argv in (
            ["pkexec", "/usr/lib/arltuner/arltuner-helper", "--device"],
            ["/usr/bin/python3", "-I", "/usr/lib/arltuner/arltuner-helper", "--device"],
            ["python3", "/x/arltuner-helper.py"],
        ):
            proc = self.fake_proc(p4242=["bash"], p4243=argv)
            r = self.install(extra_env={"ARLTUNER_PROC": str(proc)})
            self.assertNotEqual(r.returncode, 0, argv)
            self.assertIn("arltuner-helper process is running", r.stderr)
            self.assertIn("4243", r.stderr)
            self.assertEqual(list(self.dest.iterdir()), [], "refused before writing anything")
            import shutil

            shutil.rmtree(proc)

    def test_unrelated_processes_do_not_block_the_install(self) -> None:
        proc = self.fake_proc(p10=["bash", "install.sh"], p11=["vim", "notes-on-arltuner.txt"], p12=["/usr/bin/python3", "-m", "http.server"])
        self.assertEqual(self.install(extra_env={"ARLTUNER_PROC": str(proc)}).returncode, 0)

    def test_a_missing_proc_directory_is_harmless(self) -> None:
        r = self.install(extra_env={"ARLTUNER_PROC": str(self.tmp / "does-not-exist")})
        self.assertEqual(r.returncode, 0, r.stderr)

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


def bash_function(name: str) -> str:
    """The text of a shell function of install.sh, so it can be exercised alone (swap_dir, copy_one)."""
    m = re.search(rf"^{name}\(\) \{{.*?^\}}$", (REPO / "install.sh").read_text(), re.S | re.M)
    assert m, name
    return m.group(0)


def run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    prelude = 'set -euo pipefail\ndie() { echo "die: $*" >&2; exit 1; }\nOWN=()\n'
    return subprocess.run(["bash", "-c", prelude + script], capture_output=True, text=True, cwd=cwd, env={"PATH": os.environ["PATH"]}, timeout=60)


class SourceTreeAttacks(InstallCase):
    def test_a_symlinked_source_directory_is_refused_and_nothing_is_installed(self) -> None:
        for rel in ("src", "src/arltuner", "platforms", "web", "packaging", "profiles", "policies"):
            with self.subTest(rel):
                src = copy_checkout(self.tmp / ("c-" + rel.replace("/", "_")))
                elsewhere = self.tmp / ("elsewhere-" + rel.replace("/", "_"))
                (src / rel).rename(elsewhere)
                (src / rel).symlink_to(elsewhere)
                dest = self.tmp / ("root-" + rel.replace("/", "_"))
                dest.mkdir()
                r = run_script(src / "install.sh", destdir=dest, legacy_audit=self.legacy)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("symlink", r.stderr)
                self.assertEqual(list(dest.iterdir()), [], "refused before writing anything")

    def test_a_symlinked_python_file_inside_the_package_is_not_installed(self) -> None:
        src = copy_checkout(self.tmp / "checkout")
        secret = self.tmp / "secret.py"
        secret.write_text("SECRET = 1\n")
        (src / "src" / "arltuner" / "leak.py").symlink_to(secret)
        self.ok_install(script=src / "install.sh")
        self.assertFalse((self.path("/usr/lib/arltuner/arltuner") / "leak.py").exists())
        self.assertNotIn(b"SECRET", b"".join(p.read_bytes() for p in self.path("/usr/lib/arltuner").rglob("*.py")))

    def test_two_policy_lines_in_current_are_refused(self) -> None:
        src = copy_checkout(self.tmp / "checkout")
        cur = src / "profiles" / "CURRENT"
        cur.write_text("policy = policies/example-oc-ramp.toml\n" + cur.read_text())
        r = self.install(script=src / "install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("more than one 'policy =' line", r.stderr)
        self.assertFalse((self.dest / "etc/arltuner/policy.toml").exists())

    def test_no_bare_empty_array_expansion_under_set_u(self) -> None:
        self.assertNotIn('"${OWN[@]}"', (REPO / "install.sh").read_text().replace('${OWN[@]+"${OWN[@]}"}', ""))


class ShellFunctions(InstallCase):
    def test_swap_dir_replaces_and_removes_the_old_tree(self) -> None:
        (self.tmp / "final").mkdir()
        (self.tmp / "final" / "old.txt").write_text("old")
        (self.tmp / "new").mkdir()
        (self.tmp / "new" / "new.txt").write_text("new")
        r = run_bash(bash_function("swap_dir") + '\nswap_dir "$PWD/new" "$PWD/final"', self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(p.name for p in (self.tmp / "final").iterdir()), ["new.txt"])
        self.assertEqual([p.name for p in self.tmp.iterdir() if ".old." in p.name], [])

    def test_swap_dir_with_nothing_there_yet(self) -> None:
        (self.tmp / "new").mkdir()
        r = run_bash(bash_function("swap_dir") + '\nswap_dir "$PWD/new" "$PWD/final"', self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.tmp / "final").is_dir())

    def test_a_failing_swap_rolls_back_and_the_final_path_is_never_missing(self) -> None:
        (self.tmp / "final").mkdir()
        (self.tmp / "final" / "keep.txt").write_text("keep")
        r = run_bash(bash_function("swap_dir") + '\nswap_dir "$PWD/does-not-exist" "$PWD/final"', self.tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rolled back", r.stderr)
        self.assertEqual((self.tmp / "final" / "keep.txt").read_text(), "keep")
        self.assertEqual([p.name for p in self.tmp.iterdir() if ".old." in p.name], [])

    def test_swap_dir_moves_a_symlink_aside_instead_of_following_it(self) -> None:
        target = self.tmp / "victim"
        target.mkdir()
        (target / "precious").write_text("x")
        (self.tmp / "final").symlink_to(target)
        (self.tmp / "new").mkdir()
        r = run_bash(bash_function("swap_dir") + '\nswap_dir "$PWD/new" "$PWD/final"', self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.tmp / "final").is_dir() and not (self.tmp / "final").is_symlink())
        self.assertEqual((target / "precious").read_text(), "x", "the link target was not touched")

    def test_copy_one_copies_a_regular_file_with_the_requested_mode(self) -> None:
        (self.tmp / "a.txt").write_text("data")
        os.chmod(self.tmp / "a.txt", 0o666)
        r = run_bash(bash_function("copy_one") + '\ncopy_one "$PWD/a.txt" "$PWD/sub/b.txt" 0640', self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "sub" / "b.txt").read_text(), "data")
        self.assertEqual(stat.S_IMODE((self.tmp / "sub" / "b.txt").stat().st_mode), 0o640)

    def test_copy_one_refuses_a_source_that_is_a_link_and_leaves_no_destination(self) -> None:
        (self.tmp / "secret").write_text("TOP SECRET")
        (self.tmp / "a.txt").symlink_to(self.tmp / "secret")
        r = run_bash(bash_function("copy_one") + '\ncopy_one "$PWD/a.txt" "$PWD/b.txt" 0644', self.tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("changed while it was being copied", r.stderr)
        self.assertFalse((self.tmp / "b.txt").exists() or (self.tmp / "b.txt").is_symlink())

    def test_copy_one_replaces_a_planted_destination_link_without_following_it(self) -> None:
        (self.tmp / "a.txt").write_text("data")
        (self.tmp / "victim").write_text("untouched")
        (self.tmp / "b.txt").symlink_to(self.tmp / "victim")
        r = run_bash(bash_function("copy_one") + '\ncopy_one "$PWD/a.txt" "$PWD/b.txt" 0644', self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "victim").read_text(), "untouched")
        self.assertFalse((self.tmp / "b.txt").is_symlink())


class LegacySeed(InstallCase):
    def me(self) -> tuple[str, str]:
        import pwd

        return str(os.getuid()), pwd.getpwuid(os.getuid()).pw_name

    def test_sudo_user_root_with_a_different_pkexec_uid_is_refused_before_anything_is_written(self) -> None:
        uid, _ = self.me()
        self.legacy.write_text("x\n")
        r = self.install(extra_env={"SUDO_USER": "root", "PKEXEC_UID": uid})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("disagree", r.stderr)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_two_different_users_are_refused(self) -> None:
        uid, name = self.me()
        r = self.install(extra_env={"SUDO_USER": name + "x", "PKEXEC_UID": uid})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("disagree", r.stderr)

    def test_matching_users_are_accepted(self) -> None:
        uid, name = self.me()
        self.legacy.write_text("legacy\n")
        self.ok_install(extra_env={"SUDO_USER": name, "PKEXEC_UID": uid})
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "legacy\n")

    def test_a_bad_or_unknown_pkexec_uid_is_refused(self) -> None:
        for bad in ("abc", "4294967290"):
            r = self.install(extra_env={"PKEXEC_UID": bad})
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertEqual(list(self.dest.iterdir()), [], bad)

    def test_root_is_never_the_user_whose_log_is_seeded(self) -> None:
        self.legacy.write_text("root's idea of a log\n")
        r = self.ok_install(extra_env={"SUDO_USER": "root"}, legacy_audit=None)
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_bytes(), b"")
        self.assertIn("created empty", r.stdout)

    def test_a_legacy_log_over_64_mib_is_refused_and_leaves_no_audit_log(self) -> None:
        with open(self.legacy, "wb") as fh:
            fh.truncate(67108864 + 1)
        r = self.install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("larger than 64 MiB", r.stderr)
        self.assertFalse((self.dest / "var/lib/arltuner/audit.jsonl").exists())
        self.assertEqual(sorted(p.name for p in (self.dest / "var/lib/arltuner").iterdir()), ["curves", "profiles", "state", "stress"], "no staging file left behind (the store directories are made before the audit log)")

    def test_a_legacy_log_of_exactly_64_mib_is_accepted(self) -> None:
        with open(self.legacy, "wb") as fh:
            fh.truncate(67108864)
        self.ok_install()
        self.assertEqual((self.dest / "var/lib/arltuner/audit.jsonl").stat().st_size, 67108864)

    def test_the_size_cap_does_not_depend_on_a_stat_of_the_source(self) -> None:
        text = (REPO / "install.sh").read_text()
        self.assertIn("head -c 67108865", text)
        self.assertNotIn("stat -c %s \"$LEGACY\"", text)
        self.assertIn("runuser -u", text)


class Uninstall(InstallCase):
    def setUp(self) -> None:
        super().setUp()
        self.legacy.write_text("legacy\n")
        self.ok_install()
        self.path("/run/arltuner").mkdir(parents=True)  # tmpfiles.d's job on a real system (a DESTDIR install never runs it)
        (self.path("/run/arltuner") / "mailbox.lock").write_text("")

    def uninstall(self, *args: str) -> subprocess.CompletedProcess[str]:
        r = run_script(REPO / "uninstall.sh", *args, destdir=self.dest)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_uninstall_removes_code_polkit_and_web_but_keeps_config_the_audit_log_and_the_lock_directory_setup(self) -> None:
        r = self.uninstall()
        for gone in ("/usr/lib/arltuner", "/usr/share/arltuner", "/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy"):
            self.assertFalse(self.path(gone).exists(), gone)
        # G-108: the CLI still counts as installed (policy.toml stays), so it still needs its lock directory and the tmpfiles.d file that recreates it at boot
        self.assertTrue(self.path("/usr/lib/tmpfiles.d/arltuner.conf").is_file())
        self.assertTrue(self.path("/run/arltuner").is_dir())
        self.assertIn("kept " + str(self.path("/usr/lib/tmpfiles.d/arltuner.conf")), r.stdout)
        self.assertIn("kept " + str(self.path("/run/arltuner")), r.stdout)
        self.assertTrue(self.path("/var/lib/arltuner/audit.jsonl").is_file())
        self.assertEqual(self.path("/var/lib/arltuner/audit.jsonl").read_text(), "legacy\n")
        self.assertTrue(self.path("/etc/arltuner/policy.toml").is_file())
        self.assertIn("--purge", r.stdout)

    def test_uninstall_is_idempotent(self) -> None:
        self.uninstall()
        self.uninstall()
        self.assertTrue(self.path("/var/lib/arltuner/audit.jsonl").is_file())

    def test_purge_removes_config_and_state_too(self) -> None:
        self.uninstall("--purge", "--yes-reset-budget")
        self.assertFalse(self.path("/var/lib/arltuner").exists())
        self.assertFalse(self.path("/etc/arltuner").exists())
        self.assertFalse(self.path("/usr/lib/tmpfiles.d/arltuner.conf").exists())
        self.assertFalse(self.path("/run/arltuner").exists())
        self.uninstall("--purge", "--yes-reset-budget")

    def test_a_plain_uninstall_twice_still_keeps_the_tmpfiles_file_and_the_lock_directory(self) -> None:
        self.uninstall()
        self.uninstall()
        self.assertTrue(self.path("/usr/lib/tmpfiles.d/arltuner.conf").is_file())
        self.assertTrue(self.path("/run/arltuner").is_dir())

    def test_the_header_says_what_is_kept(self) -> None:
        head = (REPO / "uninstall.sh").read_text().split("set -euo", 1)[0]
        self.assertIn("tmpfiles.d file", head)
        self.assertIn("/run/arltuner", head)
        r = run_script(REPO / "uninstall.sh", "--help", destdir=self.dest)
        self.assertEqual(r.returncode, 0)
        self.assertIn("DESTDIR", r.stdout)

    def test_purge_alone_is_refused_and_removes_nothing(self) -> None:
        r = run_script(REPO / "uninstall.sh", "--purge", destdir=self.dest)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--yes-reset-budget", r.stderr)
        self.assertIn("RESETS the per-boot", r.stderr)
        self.assertTrue(self.path("/usr/lib/arltuner").is_dir())
        self.assertTrue(self.path("/var/lib/arltuner/audit.jsonl").is_file())
        self.assertTrue(self.path("/etc/arltuner/policy.toml").is_file())

    def test_the_budget_flag_alone_is_refused(self) -> None:
        r = run_script(REPO / "uninstall.sh", "--yes-reset-budget", destdir=self.dest)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.path("/var/lib/arltuner/audit.jsonl").is_file())

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
        self.assertEqual((self.dest / "usr/lib/arltuner/arltuner-helper").read_bytes(), (REPO / "packaging/arltuner-helper").read_bytes())
        results = mod.selfcheck(lstat_fn=lstat, listdir_fn=listdir, read_fn=read, root_uid=os.geteuid())
        self.assertEqual([r for r in results if r[1] is not None], [])
        self.assertGreaterEqual(len(results), 12)


if __name__ == "__main__":
    unittest.main()
