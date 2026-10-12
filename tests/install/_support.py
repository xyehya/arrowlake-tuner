"""Shared helpers for the install tests: fake ``lstat`` trees and a throw-away copy of the checkout to install from."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
DIR = stat.S_IFDIR | 0o755
FILE = stat.S_IFREG | 0o644
SYMLINK = stat.S_IFLNK | 0o777

# What install.sh copies out of a checkout (everything else is not needed to build the install).
CHECKOUT_ITEMS = ("install.sh", "uninstall.sh", "src", "platforms", "packaging", "policies", "web")


def fake_lstat(tree: dict[str, tuple[int, int]]) -> Callable[[str], SimpleNamespace]:
    """``tree``: absolute path -> (uid, st_mode). Anything else does not exist."""

    def lstat(path: str) -> SimpleNamespace:
        try:
            uid, mode = tree[os.fspath(path)]
        except KeyError:
            raise FileNotFoundError(path) from None
        return SimpleNamespace(st_uid=uid, st_mode=mode, st_nlink=1)

    return lstat


def fake_listdir(tree: dict[str, tuple[int, int]]) -> Callable[[str], list[str]]:
    def listdir(path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/", 1)[0] for p in tree if p.startswith(prefix) and p != path})

    return listdir


def copy_checkout(dest: Path, *, open_modes: bool = False) -> Path:
    """A copy of the tracked sources to run install.sh from (so tests may edit CURRENT, plant files, make everything group-writable...)."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in CHECKOUT_ITEMS:
        src = REPO / item
        if src.is_dir():
            shutil.copytree(src, dest / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dest / item)
    if open_modes:  # a user with umask 0002: every file and directory group-writable
        for p in [dest, *dest.rglob("*")]:
            if not p.is_symlink():
                os.chmod(p, (p.stat().st_mode & 0o7777) | 0o020)
    return dest


def run_script(
    script: Path, *args: str, destdir: Path | None, user_audit: Path | None = None, umask: int | None = None, cwd: Path | None = None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run install.sh / uninstall.sh. SAFETY: always with a DESTDIR, so a test can never touch the real system."""
    assert destdir is not None and destdir.is_absolute(), "tests never run the install scripts without a DESTDIR"
    env = {"PATH": os.environ["PATH"], "DESTDIR": str(destdir), "ARLTUNER_PYTHON": sys.executable, "HOME": str(destdir / "nohome")}
    fake_proc = destdir.parent / "fake-proc"  # never scan the real /proc: a running helper (dev GUI, sim) must not fail install tests
    fake_proc.mkdir(exist_ok=True)
    env["ARLTUNER_PROC"] = str(fake_proc)
    if user_audit is not None:
        env["ARLTUNER_USER_AUDIT"] = str(user_audit)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script), *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=300,
        preexec_fn=(lambda: os.umask(umask)) if umask is not None else None,
    )
