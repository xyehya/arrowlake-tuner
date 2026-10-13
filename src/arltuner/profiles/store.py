"""Root-owned store of saved tuning intents (G-112, contract 9 ``profiles.*``): ``/var/lib/arltuner/profiles/<name>.json``.

G-148 (D-044): a profile saved now is **complete**: ``{name, created, profile}`` where ``profile`` is the TOML text of a complete profile (this boot's read
with the editor's rows laid over it, :mod:`arltuner.profiles.complete`). A profile saved before G-148 is ``{name, created, intent}``: the *intent* the Tune page
sent (``{scope, rows}``), resolved against the words read this boot every time it is planned -- "partial (legacy)", still loadable. Names are ``^[a-z0-9._-]{1,64}$`` and **never overwritten** (``EXISTS``). The same open rules as the rest of the helper (SPEC 4.4,
contract 8): the directory is created 0700 and must be owned by the helper's euid and not group/world-writable; files are created with
``O_CREAT | O_EXCL | O_NOFOLLOW`` (mode 0600), read with ``O_NOFOLLOW`` (the same checks on the opened descriptor).
Standard library only, no hardware.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["MAX_PROFILES", "MAX_PROFILE_BYTES", "NAME_RE", "ProfileStore", "ProfileStoreError"]

NAME_RE = re.compile(r"[a-z0-9._-]{1,64}\Z")
MAX_PROFILE_BYTES = 32 * 1024
MAX_PROFILES = 500  # saved profiles; ``save`` refuses (TOO_BIG) past this, so the root-owned directory cannot be filled through the GUI (G-142)
_MAX_LISTED = 500  # profiles.list never returns more than this many (a runaway directory must not blow the 64 KiB response line)
_SUFFIX = ".json"


class ProfileStoreError(Exception):
    """``code`` is one of ``BAD_NAME`` ``EXISTS`` ``NOT_FOUND`` ``UNTRUSTED`` ``TOO_BIG`` ``IO``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _json_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("utf-8")


class ProfileStore:
    def __init__(self, directory: Path | str, *, euid: int, wall_clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self._euid = euid
        self._wall = wall_clock

    # ---------------------------------------------------------------- directory
    def _check_directory(self, *, create: bool) -> bool:
        """True when the directory exists and is trusted; False when it is absent and ``create`` is false."""
        try:
            if create:
                os.makedirs(self.directory.parent, mode=0o755, exist_ok=True)
                try:
                    os.mkdir(self.directory, 0o700)
                except FileExistsError:
                    pass
            st = os.lstat(self.directory)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ProfileStoreError("IO", f"cannot prepare {self.directory}: {exc.strerror or exc}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ProfileStoreError("UNTRUSTED", f"{self.directory} is not a plain directory")
        if st.st_uid != self._euid:
            raise ProfileStoreError("UNTRUSTED", f"{self.directory} is owned by uid {st.st_uid}, not by the helper (uid {self._euid})")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ProfileStoreError("UNTRUSTED", f"{self.directory} mode {stat.S_IMODE(st.st_mode):04o} is group- or world-writable")
        return True

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ProfileStoreError("BAD_NAME", "a profile name is 1..64 characters of a-z 0-9 . _ -")
        return self.directory / (name + _SUFFIX)

    # ---------------------------------------------------------------- operations
    def save(self, name: str, intent: dict[str, Any] | None = None, *, profile: str | None = None) -> dict[str, Any]:
        """Create ``<name>.json`` (never overwrite: ``EXISTS``) holding a complete profile's TOML text (``profile``, G-148) or -- legacy shape, kept for the tests and
        older callers -- an ``intent``. Returns the stored record ``{name, created, profile | intent}``."""
        path = self._path(name)
        if (intent is None) == (profile is None):
            raise ValueError("save needs exactly one of intent / profile")
        body: dict[str, Any] = {"profile": profile} if profile is not None else {"intent": intent}
        record = {"name": name, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall())), **body}
        data = _json_bytes(record)
        if len(data) > MAX_PROFILE_BYTES:
            raise ProfileStoreError("TOO_BIG", f"the profile is {len(data)} bytes (limit {MAX_PROFILE_BYTES})")
        self._check_directory(create=True)
        try:
            have = sum(1 for n in os.listdir(self.directory) if n.endswith(_SUFFIX))
        except OSError as exc:
            raise ProfileStoreError("IO", f"cannot list {self.directory}: {exc.strerror or exc}") from None
        if have >= MAX_PROFILES:
            raise ProfileStoreError("TOO_BIG", f"there are already {have} saved profiles (limit {MAX_PROFILES}); delete some files in {self.directory} first")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            raise ProfileStoreError("EXISTS", f"a profile named {name!r} already exists (profiles are never overwritten)") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ProfileStoreError("EXISTS", f"{name!r} is taken (a symlink is in the way; nothing was followed)") from None
            raise ProfileStoreError("IO", f"cannot create {path}: {exc.strerror or exc}") from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                os.unlink(path)  # a half-written file must not block the name
            except OSError:
                pass
            raise ProfileStoreError("IO", f"cannot write {path}: {exc.strerror or exc}") from None
        return record

    def load(self, name: str) -> dict[str, Any]:
        """The stored record; ``NOT_FOUND`` / ``UNTRUSTED`` / ``IO``."""
        path = self._path(name)
        if not self._check_directory(create=False):
            raise ProfileStoreError("NOT_FOUND", f"no saved profile named {name!r}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            raise ProfileStoreError("NOT_FOUND", f"no saved profile named {name!r}") from None
        except OSError as exc:
            raise ProfileStoreError("UNTRUSTED", f"cannot open {path}: {exc.strerror or exc}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != self._euid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ProfileStoreError("UNTRUSTED", f"{path} is not a regular, single-link, helper-owned file without group/other write")
        except BaseException:
            os.close(fd)
            raise
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_PROFILE_BYTES + 1)
        except OSError as exc:
            raise ProfileStoreError("IO", f"cannot read {path}: {exc.strerror or exc}") from None
        if len(data) > MAX_PROFILE_BYTES:
            raise ProfileStoreError("UNTRUSTED", f"{path} is larger than {MAX_PROFILE_BYTES} bytes")
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProfileStoreError("UNTRUSTED", f"{path} is not valid JSON") from None
        if not isinstance(record, dict) or record.get("name") != name or not (isinstance(record.get("intent"), dict) or isinstance(record.get("profile"), str)):
            raise ProfileStoreError("UNTRUSTED", f"{path} is not a saved profile named {name!r}")
        return record

    def list(self) -> list[dict[str, Any]]:
        """``[{name, source: "saved", created, complete}]``, oldest first by name (``complete``: G-148 complete profile, else a legacy partial intent). A file that is not a
        trusted profile is skipped (never an error)."""
        if not self._check_directory(create=False):
            return []
        try:
            names = sorted(n[: -len(_SUFFIX)] for n in os.listdir(self.directory) if n.endswith(_SUFFIX) and NAME_RE.match(n[: -len(_SUFFIX)]))
        except OSError as exc:
            raise ProfileStoreError("IO", f"cannot list {self.directory}: {exc.strerror or exc}") from None
        out: list[dict[str, Any]] = []
        for name in names[:_MAX_LISTED]:
            try:
                record = self.load(name)
            except ProfileStoreError:
                continue
            created = record.get("created")
            out.append({"name": name, "source": "saved", "created": created if isinstance(created, str) else None, "complete": isinstance(record.get("profile"), str)})
        return out
