"""Launcher page: a 0600 file:// page that carries the one-time bootstrap token (never argv, never a URL that reaches history)."""
from __future__ import annotations

import html
import os
import stat
from pathlib import Path


class LauncherDirError(Exception):
    """The directory meant to hold the launcher is not a private directory we own."""


def check_private_dir(path: Path) -> None:
    """``path`` must be a real directory (not a symlink), owned by the effective user, with no group/other permission bits."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise LauncherDirError(f"{path}: {e.strerror}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise LauncherDirError(f"{path}: not a real directory (symlink or file); refusing to put the bootstrap token there")
    if st.st_uid != os.geteuid():
        raise LauncherDirError(f"{path}: owned by uid {st.st_uid}, not by you; refusing")
    if st.st_mode & 0o077:
        raise LauncherDirError(f"{path}: mode {stat.S_IMODE(st.st_mode):04o} lets others in (need 0700); refusing")


def make_private_dir(path: Path) -> None:
    """Create ``path`` (0700, no parents) if missing, then verify it with ``check_private_dir`` (also when it already existed)."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        raise LauncherDirError(f"{path}: parent directory does not exist") from None
    check_private_dir(path)


def write_launcher(path: Path, origin: str, token: str) -> Path:
    """Create ``path`` with mode 0600 inside a verified private directory, replacing any older file. The page has a single POST form."""
    path = Path(path)
    make_private_dir(path.parent)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    body = (
        '<!doctype html>\n<meta charset="utf-8"><title>arrowlake-tuner launcher</title>\n'
        f'<form method="post" action="{html.escape(origin, quote=True)}/bootstrap">\n'
        f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">\n'
        '<button type="submit">Open arrowlake-tuner</button>\n</form>\n'
        '<p>One-time link. It stops working after the first use.</p>\n'
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return path
