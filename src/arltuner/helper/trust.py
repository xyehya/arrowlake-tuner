"""SPEC section 4.4 trust boundary: the helper loads policy, accepted state and the audit log (and runs code) only from root-owned paths that
are not group/world-writable, have root-owned non-writable parents and contain no symlink. Pure over an injectable ``lstat``: tests feed fake
stat results and never touch the real filesystem or install anything."""
from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["TrustError", "check_code_tree", "check_trusted_path"]

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
