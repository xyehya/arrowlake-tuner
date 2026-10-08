"""SPEC section 4.4 trust boundary: the helper loads policy, accepted state and the audit log (and runs code) only from root-owned paths that
are not group/world-writable, have root-owned non-writable parents and contain no symlink. Pure over an injectable ``lstat``: tests feed fake
stat results and never touch the real filesystem or install anything."""
from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["TrustError", "check_code_tree", "check_lock_file", "check_trusted_path", "open_checked"]

LstatFn = Callable[[str], Any]  # os.lstat-like: the result has st_uid and st_mode


class TrustError(Exception):
    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path, self.reason = str(path), reason


def _check_one(path: str, st: Any, *, want: str | None, is_leaf: bool, root_uid: int) -> None:
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise TrustError(path, "is a symlink (a trusted path has none)")
    if st.st_uid != root_uid:
        raise TrustError(path, f"owner uid {st.st_uid} is not root")
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TrustError(path, f"mode {stat.S_IMODE(mode):04o} is group- or world-writable")
    if is_leaf and want == "file" and not stat.S_ISREG(mode):
        raise TrustError(path, "is not a regular file")
    if (not is_leaf or want == "dir") and not stat.S_ISDIR(mode):
        raise TrustError(path, "is not a directory")


def check_trusted_path(path: Path | str, *, want: str | None = "file", must_exist: bool = True, lstat_fn: LstatFn = os.lstat, root_uid: int = 0) -> None:
    """Raise :class:`TrustError` unless ``path`` (absolute) and every parent up to ``/`` is root-owned, not group/world-writable and not a
    symlink. ``want`` is ``"file"`` | ``"dir"`` | ``None`` (any kind) for the leaf. With ``must_exist=False`` a missing leaf passes (its parent must
    still be trusted: the file is then created there); a missing parent never does."""
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        raise TrustError(p, "must be an absolute path without '..'")
    chain = [str(a) for a in reversed(p.parents)] + [str(p)]
    for i, node in enumerate(chain):
        is_leaf = i == len(chain) - 1
        try:
            st = lstat_fn(node)
        except FileNotFoundError:
            if is_leaf and not must_exist:
                return
            raise TrustError(node, "does not exist") from None
        except OSError as exc:
            raise TrustError(node, f"cannot stat: {exc.strerror or exc}") from None
        _check_one(node, st, want=want, is_leaf=is_leaf, root_uid=root_uid)


def check_code_tree(package_dir: Path | str, *, lstat_fn: LstatFn = os.lstat, root_uid: int = 0) -> None:
    """The directory holding the helper's own code must be a trusted directory (never ``~/.local`` or a git checkout)."""
    check_trusted_path(package_dir, want="dir", lstat_fn=lstat_fn, root_uid=root_uid)


def open_checked(
    path: Path | str, *, write: bool, create: bool, mode: int = 0o600, euid: int, fstat_fn: Callable[[int], Any] = os.fstat, what: str = "file"
) -> int:
    """Open ``path`` the only safe way for a root process in a directory others may write to: ``O_NOFOLLOW | O_CLOEXEC`` (and ``O_EXCL`` when it
    creates), then ``fstat`` the opened descriptor - it must be a regular file owned by ``euid`` with no group/other write bit. Never open-then-check
    by name (TOCTOU). A symlink (dangling or not), a foreign owner or a writable-by-others file is a :class:`TrustError`; nothing is followed,
    created through or taken over. ``FileNotFoundError`` propagates when ``create`` is false."""
    flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            if not create:
                raise FileExistsError  # open the existing file below
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, mode)  # O_EXCL never follows a symlink: a planted link is EEXIST
        except FileExistsError:
            fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TrustError(path, f"{what} is a symlink (nothing was followed)") from None
        raise TrustError(path, f"cannot open {what}: {exc.strerror or exc}") from None
    try:
        st = fstat_fn(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TrustError(path, f"{what} is not a regular file")
        if st.st_uid != euid:
            raise TrustError(path, f"{what} is owned by uid {st.st_uid}, not by the helper (uid {euid}); not taken over")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise TrustError(path, f"{what} mode {stat.S_IMODE(st.st_mode):04o} is group- or world-writable")
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_lock_file(path: Path | str, *, euid: int, lstat_fn: Callable[[str], Any] = os.lstat) -> None:
    """The guard's per-transaction lock file lives in a directory others may write (``/run/lock`` is 1777). If something is already there it must
    be a regular file of ``euid``; a symlink or a foreign file is refused here and never followed or taken over (the guard's own ``O_EXCL`` create
    would refuse it as ``LOCKED`` anyway). A missing file is fine."""
    try:
        st = lstat_fn(os.fspath(path))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TrustError(path, f"cannot stat the lock file: {exc.strerror or exc}") from None
    if stat.S_ISLNK(st.st_mode):
        raise TrustError(path, "the lock file is a symlink; not followed")
    if st.st_uid != euid:
        raise TrustError(path, f"the lock file is owned by uid {st.st_uid}, not by the helper (uid {euid}); not taken over")
