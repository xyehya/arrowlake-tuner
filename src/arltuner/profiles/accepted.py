"""The user's accepted state, written by the root helper (G-129, D-036, ``profiles.accept``): ``/etc/arltuner/accepted/``.

Layout (the same the restore scripts and the helper's ``accepted_match`` / ``_accepted_chain`` read)::

    accepted/CURRENT                  ``profile = profiles/<name>.toml``  (one line per profile, applied in order)
    accepted/EXPECTED-words.txt       ``<cpu> <OC word> <point 1> ... <point n>`` (hex, no 0x), one line per core / E-cluster
    accepted/fingerprint.json         G-138: per Linux CPU its kind / cluster / fused 0x09 word + the CPU's family / model / brand, for the Windows restore
                                      (:mod:`arltuner.profiles.fingerprint`); absent in a state accepted before G-138
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

from .fingerprint import FINGERPRINT_FILE

__all__ = ["AcceptedError", "AcceptedStore", "SAFE_PROFILE_NAME"]

#: A name an accepted profile can carry: what ``profiles.get`` / ``profiles.list`` handle (a-z 0-9 . _ -, 1..64) and ``_accepted_chain`` accepts (no ``..``).
SAFE_PROFILE_NAME = re.compile(r"[a-z0-9_][a-z0-9._-]{0,63}\Z")
_LOCK = threading.Lock()


class AcceptedError(Exception):
    """``code`` is ``UNTRUSTED`` | ``BAD_NAME`` | ``IO`` | ``MISSING``."""

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

    def replace(self, name: str, profile_toml: str, expected_words: str, current_header: str = "", fingerprint: str | None = None) -> None:
        """Make ``name`` the whole accepted state: ``CURRENT`` (one ``profile =`` line), the profile and ``EXPECTED-words.txt`` (and ``fingerprint.json`` when given)."""
        if not SAFE_PROFILE_NAME.match(name) or ".." in name:
            raise AcceptedError("BAD_NAME", "an accepted profile name is 1..64 characters of a-z 0-9 . _ - without '..'")
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
                if fingerprint is not None:
                    _write_file(new / FINGERPRINT_FILE, fingerprint)
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

    def write_fingerprint(self, text: str) -> None:
        """G-141 (``profiles.fingerprint``): write ``fingerprint.json`` into the EXISTING accepted directory and nothing else -- ``CURRENT``, the profiles and
        ``EXPECTED-words.txt`` are not touched. A temp file in the directory (``O_EXCL | O_NOFOLLOW`` 0644, ``fsync``) is renamed over the old file. The parent must pass the
        same trust check as for :meth:`replace`; the accepted directory itself must be a plain directory (not a symlink) holding a ``CURRENT`` file (``AcceptedError``
        ``MISSING`` otherwise: this op never creates an accepted state)."""
        self._check_parent()
        try:
            st = os.lstat(self.directory)
        except FileNotFoundError:
            raise AcceptedError("MISSING", f"{self.directory} does not exist: there is no accepted state to record a fingerprint for") from None
        except OSError as exc:
            raise AcceptedError("IO", f"cannot stat {self.directory}: {exc.strerror or exc}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != self._euid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise AcceptedError("UNTRUSTED", f"{self.directory} is not a plain directory owned by the helper and not group/world-writable")
        if not (self.directory / "CURRENT").is_file():
            raise AcceptedError("MISSING", f"{self.directory} has no CURRENT file: there is no accepted state to record a fingerprint for")
        tmp = self.directory / f".{FINGERPRINT_FILE}.{os.getpid()}.tmp"
        with _LOCK:
            try:
                _remove(tmp)
                _write_file(tmp, text)
                os.replace(tmp, self.directory / FINGERPRINT_FILE)
            except OSError as exc:
                raise AcceptedError("IO", f"cannot write {self.directory / FINGERPRINT_FILE}: {exc.strerror or exc}") from None
            finally:
                try:
                    _remove(tmp)
                except OSError:
                    pass
