"""The user's accepted state, written by the root helper (G-129, D-036, ``profiles.accept``): ``/etc/arltuner/accepted/``.

Layout (the same the restore scripts and the helper's ``accepted_match`` / ``_accepted_chain`` read)::

    accepted/CURRENT                  ``profile = profiles/<name>.toml``  (one line per profile, applied in order)
    accepted/EXPECTED-words.txt       ``<cpu> <OC word> <point 1> ... <point n>`` (hex, no 0x), one line per core / E-cluster
    accepted/profiles/<name>.toml     the profile

The directory is **replaced as a whole**, atomically from a reader's point of view: the new tree is built next to it (``.accepted.new.<pid>``, files
created ``O_CREAT | O_EXCL | O_NOFOLLOW`` 0644, directories 0755, ``fsync``), the old one moves aside (``accepted.old.<pid>``), the new one moves in, the old
one is removed; a failure moves the old one back. The parent must be a plain directory owned by the helper's euid and not group/world-writable
(``/etc/arltuner`` is created by ``install.sh``); a symlink at the final name is moved aside, never followed. Standard library only, no hardware.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import threading
from pathlib import Path

__all__ = ["AcceptedError", "AcceptedStore", "SAFE_PROFILE_NAME"]

#: A name an accepted profile can carry: the helper's ``_accepted_chain`` accepts ``profiles/[A-Za-z0-9._-]{1,80}.toml`` without ``..``.
SAFE_PROFILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z")
_LOCK = threading.Lock()


class AcceptedError(Exception):
    """``code`` is ``UNTRUSTED`` | ``BAD_NAME`` | ``IO``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _write_file(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o644)
    try:
        os.fchmod(fd, 0o644)  # the umask must not decide the mode
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


class AcceptedStore:
    def __init__(self, directory: Path | str, *, euid: int) -> None:
        self.directory = Path(directory)
        self._euid = euid

    def exists(self) -> bool:
        try:
            os.lstat(self.directory)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def _check_parent(self) -> None:
        parent = self.directory.parent
        try:
            st = os.lstat(parent)
        except FileNotFoundError:
            raise AcceptedError("IO", f"{parent} does not exist (run install.sh first)") from None
        except OSError as exc:
            raise AcceptedError("IO", f"cannot stat {parent}: {exc.strerror or exc}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise AcceptedError("UNTRUSTED", f"{parent} is not a plain directory")
        if st.st_uid != self._euid:
            raise AcceptedError("UNTRUSTED", f"{parent} is owned by uid {st.st_uid}, not by the helper (uid {self._euid})")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AcceptedError("UNTRUSTED", f"{parent} mode {stat.S_IMODE(st.st_mode):04o} is group- or world-writable")

    def replace(self, name: str, profile_toml: str, expected_words: str, current_header: str = "") -> None:
        """Make ``name`` the whole accepted state: ``CURRENT`` (one ``profile =`` line), the profile and ``EXPECTED-words.txt``."""
        if not SAFE_PROFILE_NAME.match(name) or ".." in name:
            raise AcceptedError("BAD_NAME", "an accepted profile name is 1..64 characters of A-Z a-z 0-9 . _ - without '..'")
        self._check_parent()
        pid = os.getpid()
        new = self.directory.parent / f".accepted.new.{pid}"
        old = self.directory.parent / f"{self.directory.name}.old.{pid}"
        current = "".join(f"# {line}".rstrip() + "\n" for line in current_header.splitlines()) + f"profile = profiles/{name}.toml\n"
        with _LOCK:
            try:
                _remove(new)
                _remove(old)
                os.mkdir(new, 0o700)
                os.chmod(new, 0o755)
                os.mkdir(new / "profiles", 0o700)
                os.chmod(new / "profiles", 0o755)
                _write_file(new / "CURRENT", current)
                _write_file(new / "EXPECTED-words.txt", expected_words)
                _write_file(new / "profiles" / f"{name}.toml", profile_toml)
                self._swap(new, old)
            except OSError as exc:
                raise AcceptedError("IO", f"cannot write {self.directory}: {exc.strerror or exc}") from None
            finally:
                for leftover in (new, old):
                    try:
                        _remove(leftover)
                    except OSError:
                        pass

    def _swap(self, new: Path, old: Path) -> None:
        final = self.directory
        if os.path.lexists(final):
            os.rename(final, old)
            try:
                os.rename(new, final)
            except OSError:
                os.rename(old, final)  # roll back: the final path is never left missing
                raise
        else:
            os.rename(new, final)
