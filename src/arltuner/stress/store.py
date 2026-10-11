"""The stress result store: one JSON file per run under a root-owned 0700 directory (``/var/lib/arltuner/stress``; a scratch directory in ``--sim``).

Same file rules as the curve store (contract 8: ``O_NOFOLLOW`` + ``fstat`` on the descriptor, temp + fsync + rename, 0600), so it is built on
:class:`~arltuner.curves.CurveStore` (directory check, atomic write, id listing) with its own record check. Ids have the curve id shape
(``YYYYMMDDTHHMMSSZ-xxxx``). At most :data:`MAX_RECORDS` are kept: the oldest are deleted when a new one is saved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from arltuner.curves import StoreError, new_curve_id
from arltuner.curves.store import MAX_FILE_BYTES, CurveStore
from arltuner.helper.trust import TrustError, open_checked

__all__ = ["LIST_LIMIT", "MAX_RECORDS", "StressStore", "summary_row"]

MAX_RECORDS = 200
LIST_LIMIT = 50
_SUMMARY_KEYS = (
    "id", "kind", "started", "ended", "duration_s", "elapsed_s", "stopped_by", "errors", "cap_valid", "health", "mce_new", "max_temp_c", "max_pkg_w",
    "method", "units", "ratio_cap", "memory_mb",
)


def summary_row(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in _SUMMARY_KEYS}


class StressStore(CurveStore):
    def _read(self, path: Path) -> dict[str, Any]:
        try:
            fd = open_checked(path, write=False, create=False, euid=self.euid, fstat_fn=self._fstat, what="stress record")
        except TrustError as exc:
            raise StoreError("REFUSED", f"untrusted stress record: {exc}", {"check": "TRUST_PATH"}) from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise StoreError("INTERNAL", f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise StoreError("INTERNAL", f"{path.name} is not valid JSON") from None
        if not isinstance(doc, dict) or doc.get("id") != path.stem or doc.get("kind") not in ("cpu", "memory"):
            raise StoreError("INTERNAL", f"{path.name} is not a stress record")
        return doc

    def save(self, record: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        """Store a finished run (its ``id`` is assigned when missing), then drop the oldest records beyond :data:`MAX_RECORDS`. Returns the record."""
        self._check_dir(create=True)
        record = dict(record)
        if "id" not in record:
            for _ in range(20):
                record["id"] = new_curve_id(now)
                if not self._path(record["id"]).exists():
                    break
            else:
                raise StoreError("INTERNAL", "could not find a free stress id")
        elif self._path(record["id"]).exists():
            raise StoreError("REFUSED", f"stress result {record['id']} already exists", {"check": "EXISTS"})
        self._write(record)
        for old in self._ids()[MAX_RECORDS:]:
            try:
                os.unlink(self._path(old))
            except OSError:
                pass
        return record

    def path_of(self, record_id: str) -> Path:
        return self._path(record_id)

    def summaries(self, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:  # type: ignore[override]
        """The newest ``limit`` runs as summary rows; unreadable / untrusted files are skipped and named in :attr:`problems`."""
        self._check_dir(create=False)
        self.problems = []
        rows: list[dict[str, Any]] = []
        for record_id in self._ids():
            if len(rows) >= limit:
                break
            try:
                rows.append(summary_row(self._read(self._path(record_id))))
            except StoreError as exc:
                self.problems.append(f"{record_id}: {exc.message}")
            except FileNotFoundError:
                continue
        return rows
