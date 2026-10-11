"""The curve store: one JSON file per record under a root-owned 0700 directory (``/var/lib/arltuner/curves`` on an installed machine; a scratch directory in
``--sim``). Contract 8 file rules: every file is opened ``O_NOFOLLOW`` with ``fstat`` on the descriptor (regular, one link, owner = the helper, no group/other
write); every write goes to a temp name in the same directory (``O_EXCL``, 0600), is fsynced and renamed over the final name, so a reader never sees half a
record. At most :data:`MAX_RECORDS` records; at most one carries ``baseline: true`` (setting it elsewhere clears the old one first).
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arltuner.helper.trust import TrustError, check_trusted_path, open_checked

from .record import is_curve_id, new_curve_id, summary, validate_name

__all__ = ["MAX_FILE_BYTES", "MAX_RECORDS", "CurveStore", "StoreError"]

MAX_RECORDS = 200
MAX_FILE_BYTES = 2 * 1024 * 1024  # a dense record is ~60 KiB; anything near this is not ours


class StoreError(Exception):
    """``code`` is a contract 8 error code (``BAD_REQUEST`` / ``REFUSED`` / ``INTERNAL``); ``detail`` may carry ``check``."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


class CurveStore:
    def __init__(
        self,
        root: os.PathLike[str] | str,
        *,
        euid: int | None = None,
        enforce_trust: bool = False,
        lstat_fn: Callable[[str], Any] = os.lstat,
        fstat_fn: Callable[[int], Any] = os.fstat,
    ) -> None:
        self.root = Path(root)
        self.euid = getattr(os, "geteuid", lambda: 0)() if euid is None else euid
        self.enforce_trust = enforce_trust
        self._lstat, self._fstat = lstat_fn, fstat_fn
        self.problems: list[str] = []  # files skipped by the last list() and why

    # ------------------------------------------------------------------ directory
    def _check_dir(self, *, create: bool) -> None:
        try:
            st = self._lstat(os.fspath(self.root))
        except FileNotFoundError:
            if not create:
                return
            try:
                os.mkdir(self.root, 0o700)  # the leaf only: the parent is the installed state directory
            except OSError as exc:
                raise StoreError("INTERNAL", f"cannot create the curve directory {self.root}: {exc.strerror or exc}") from None
            st = self._lstat(os.fspath(self.root))
        except OSError as exc:
            raise StoreError("INTERNAL", f"cannot stat the curve directory {self.root}: {exc.strerror or exc}") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise StoreError("REFUSED", f"{self.root} is not a plain directory", {"check": "TRUST_PATH"})
        if st.st_uid != self.euid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise StoreError("REFUSED", f"{self.root} is not owned by the helper or is group/world-writable", {"check": "TRUST_PATH"})
        if self.enforce_trust:
            try:
                check_trusted_path(self.root, want="dir", lstat_fn=self._lstat)
            except TrustError as exc:
                raise StoreError("REFUSED", f"untrusted path (SPEC 4.4): {exc}", {"check": "TRUST_PATH"}) from None

    def _path(self, curve_id: str) -> Path:
        if not is_curve_id(curve_id):
            raise StoreError("BAD_REQUEST", "'id' is not a curve id")
        return self.root / f"{curve_id}.json"

    # ------------------------------------------------------------------ read
    def _read(self, path: Path) -> dict[str, Any]:
        try:
            fd = open_checked(path, write=False, create=False, euid=self.euid, fstat_fn=self._fstat, what="curve record")
        except TrustError as exc:
            raise StoreError("REFUSED", f"untrusted curve record: {exc}", {"check": "TRUST_PATH"}) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise StoreError("INTERNAL", f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise StoreError("INTERNAL", f"{path.name} is not valid JSON") from None
        if not isinstance(doc, dict) or doc.get("id") != path.stem or not isinstance(doc.get("units"), dict):
            raise StoreError("INTERNAL", f"{path.name} is not a curve record")
        return doc

    def _ids(self) -> list[str]:
        try:
            names = os.listdir(self.root)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError("INTERNAL", f"cannot list {self.root}: {exc.strerror or exc}") from None
        return sorted((n[:-5] for n in names if n.endswith(".json") and is_curve_id(n[:-5])), reverse=True)

    def get(self, curve_id: str) -> dict[str, Any]:
        path = self._path(curve_id)
        self._check_dir(create=False)
        try:
            return self._read(path)
        except FileNotFoundError:
            raise StoreError("BAD_REQUEST", f"no curve {curve_id}", {"check": "NO_SUCH_CURVE"}) from None

    def summaries(self) -> list[dict[str, Any]]:
        """Summaries, newest first (ids start with the UTC time). Unreadable / untrusted files are skipped and named in :attr:`problems`."""
        self._check_dir(create=False)
        self.problems = []
        rows: list[dict[str, Any]] = []
        for curve_id in self._ids():
            try:
                rows.append(summary(self._read(self.root / f"{curve_id}.json")))
            except StoreError as exc:
                self.problems.append(f"{curve_id}: {exc.message}")
            except FileNotFoundError:
                continue
        return rows

    # ------------------------------------------------------------------ write
    def _write(self, record: dict[str, Any]) -> None:
        final = self._path(record["id"])
        data = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        tmp = self.root / f".{record['id']}.{secrets.token_hex(4)}.tmp"
        try:
            fd = open_checked(tmp, write=True, create=True, mode=0o600, euid=self.euid, fstat_fn=self._fstat, what="curve temp file")
        except TrustError as exc:
            raise StoreError("REFUSED", f"cannot create a curve temp file: {exc}", {"check": "TRUST_PATH"}) from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, final)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise StoreError("INTERNAL", f"cannot write {final.name}: {exc.strerror or exc}") from None
        try:
            dfd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass  # the rename happened; a failed directory fsync only weakens durability

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        """Store a new record (its ``id`` is assigned when missing). ``REFUSED`` (``CURVE_LIMIT``) at :data:`MAX_RECORDS`. Returns the stored record."""
        self._check_dir(create=True)
        if len(self._ids()) >= MAX_RECORDS:
            raise StoreError("REFUSED", f"{MAX_RECORDS} curves are stored; delete some first", {"check": "CURVE_LIMIT"})
        record = dict(record)
        if "id" not in record:
            for _ in range(20):
                record["id"] = new_curve_id()
                if not self._path(record["id"]).exists():
                    break
            else:
                raise StoreError("INTERNAL", "could not find a free curve id")
        elif self._path(record["id"]).exists():
            raise StoreError("REFUSED", f"curve {record['id']} already exists", {"check": "EXISTS"})
        if record.get("baseline"):
            self._clear_baselines(except_id=record["id"])
        self._write(record)
        return record

    def _clear_baselines(self, except_id: str) -> None:
        for curve_id in self._ids():
            if curve_id == except_id:
                continue
            try:
                other = self._read(self._path(curve_id))
            except (StoreError, FileNotFoundError):
                continue
            if other.get("baseline"):
                other["baseline"] = False
                self._write(other)

    def update(self, curve_id: str, *, name: object = None, baseline: object = None) -> dict[str, Any]:
        """Rename and / or (un)mark the stock baseline. Marking clears the previous baseline first (a crash in between leaves none, never two)."""
        record = self.get(curve_id)
        if name is not None:
            try:
                record["name"] = validate_name(name)
            except ValueError as exc:
                raise StoreError("BAD_REQUEST", str(exc)) from None
        if baseline is not None:
            if not isinstance(baseline, bool):
                raise StoreError("BAD_REQUEST", "'baseline' must be a boolean")
            if baseline:
                self._clear_baselines(except_id=curve_id)
            record["baseline"] = baseline
        self._write(record)
        return record

    def delete(self, curve_id: str) -> None:
        path = self._path(curve_id)
        self._check_dir(create=False)
        try:
            os.unlink(path)
        except FileNotFoundError:
            raise StoreError("BAD_REQUEST", f"no curve {curve_id}", {"check": "NO_SUCH_CURVE"}) from None
        except OSError as exc:
            raise StoreError("INTERNAL", f"cannot delete {path.name}: {exc.strerror or exc}") from None
