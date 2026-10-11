"""The last complete ``tune.read`` of this boot, kept by the root helper (G-123, contract 9 ``tune.last``): ``<state>/last-read.json``.

One small record ``{version, boot_id, at, words, result, applied_at}``: ``result`` is the ``tune.read`` result as sent to the page, ``words`` the
authoritative ``{cpu: ["0x..", ...]}`` (OC word first), updated after an apply so a helper restart never serves pre-apply words. The record is
**overwritten atomically** (a temporary file created ``O_EXCL | O_NOFOLLOW`` 0600, ``fsync``, ``rename``): a planted symlink at the final name is replaced,
never followed. The directory is created 0700 and must be owned by the helper's euid and not group/world-writable; the file is opened ``O_NOFOLLOW |
O_NONBLOCK`` and its descriptor checked (regular, one link, helper-owned, no group/other write). Standard library only, no hardware.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

__all__ = ["LastReadError", "LastReadStore", "MAX_LAST_READ_BYTES"]

MAX_LAST_READ_BYTES = 256 * 1024
_NAME = "last-read.json"


class LastReadError(Exception):
    """``code`` is ``UNTRUSTED`` | ``TOO_BIG`` | ``IO``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LastReadStore:
    def __init__(self, directory: Path | str, *, euid: int) -> None:
        self.directory = Path(directory)
        self._euid = euid

    @property
    def path(self) -> Path:
        return self.directory / _NAME

    def _check_directory(self, *, create: bool) -> bool:
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
            raise LastReadError("IO", f"cannot prepare {self.directory}: {exc.strerror or exc}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise LastReadError("UNTRUSTED", f"{self.directory} is not a plain directory")
        if st.st_uid != self._euid:
            raise LastReadError("UNTRUSTED", f"{self.directory} is owned by uid {st.st_uid}, not by the helper (uid {self._euid})")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise LastReadError("UNTRUSTED", f"{self.directory} mode {stat.S_IMODE(st.st_mode):04o} is group- or world-writable")
        return True

    def save(self, record: dict[str, Any]) -> None:
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_LAST_READ_BYTES:
            raise LastReadError("TOO_BIG", f"the record is {len(data)} bytes (limit {MAX_LAST_READ_BYTES})")
        self._check_directory(create=True)
        tmp = self.directory / f"{_NAME}.tmp-{secrets.token_hex(6)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(tmp, flags, 0o600)
        except OSError as exc:
            raise LastReadError("IO", f"cannot create {tmp}: {exc.strerror or exc}") from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)  # rename: replaces whatever sits at the final name (a symlink included) without following it
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise LastReadError("IO", f"cannot write {self.path}: {exc.strerror or exc}") from None

    def load(self) -> dict[str, Any] | None:
        """The stored record, or ``None`` when there is none; ``UNTRUSTED`` / ``IO`` otherwise."""
        if not self._check_directory(create=False):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LastReadError("UNTRUSTED", f"cannot open {self.path}: {exc.strerror or exc}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != self._euid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise LastReadError("UNTRUSTED", f"{self.path} is not a regular, single-link, helper-owned file without group/other write")
        except BaseException:
            os.close(fd)
            raise
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_LAST_READ_BYTES + 1)
        except OSError as exc:
            raise LastReadError("IO", f"cannot read {self.path}: {exc.strerror or exc}") from None
        if len(data) > MAX_LAST_READ_BYTES:
            raise LastReadError("UNTRUSTED", f"{self.path} is larger than {MAX_LAST_READ_BYTES} bytes")
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise LastReadError("UNTRUSTED", f"{self.path} is not valid JSON") from None
        if not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("result"), dict) or not isinstance(record.get("words"), dict):
            raise LastReadError("UNTRUSTED", f"{self.path} is not a last-read record")
        return record
