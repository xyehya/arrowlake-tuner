"""Helper wire protocol: ARCHITECTURE contract 8 (JSON lines, 64 KiB cap both ways). Framing and message shapes only; no policy.

Requests are exactly ``{"id": <str, 1..64 chars>, "op": <str>, "args": <object>}`` (``args`` may be omitted: it then is ``{}``). Anything
else on a line is a ``BAD_REQUEST`` and the helper keeps running. Responses and events are written by :class:`Writer`, which serialises
the writers of the several threads (heartbeat, progress, telemetry, responses) and refuses to send a line over the cap.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

__all__ = [
    "BAD_REQUEST", "BUSY", "CANCELLED", "CONFIRM_MISMATCH", "ID_MAX", "INTERNAL", "MAX_LINE", "PLAN_STALE", "PREFLIGHT_FAILED", "REFUSED",
    "UNKNOWN_OP", "Frame", "OpError", "ProtocolError", "Request", "Writer", "encode", "error_response", "ok_response", "parse_request", "read_frame",
]

MAX_LINE = 64 * 1024  # bytes of JSON per line, newline excluded, both directions
ID_MAX = 64
_DRAIN_CHUNK = 8192

BAD_REQUEST = "BAD_REQUEST"
UNKNOWN_OP = "UNKNOWN_OP"
BUSY = "BUSY"
REFUSED = "REFUSED"
PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
PLAN_STALE = "PLAN_STALE"
CONFIRM_MISMATCH = "CONFIRM_MISMATCH"
CANCELLED = "CANCELLED"
INTERNAL = "INTERNAL"


class ProtocolError(Exception):
    """A request line that cannot be served. ``req_id`` is set when the line carried a valid ``id`` (so the answer can be correlated)."""

    def __init__(self, code: str, message: str, *, req_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.req_id, self.detail = code, message, req_id, detail


class OpError(Exception):
    """Raised by an op handler: becomes ``{"ok": false, "error": {...}}`` for that request."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


@dataclass(frozen=True)
class Request:
    id: str
    op: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Frame:
    """One unit read from the stream: ``kind`` is ``"line"`` (``data`` = the line without its newline), ``"oversize"`` or ``"eof"``."""

    kind: str
    data: bytes = b""


def read_frame(stream: BinaryIO) -> Frame:
    """Read one line of at most :data:`MAX_LINE` bytes. A longer line is drained to its newline (or EOF) and reported as ``oversize``; the
    stream stays usable. A last line without a newline is still a line."""
    raw = stream.readline(MAX_LINE + 2)
    if not raw:
        return Frame("eof")
    if raw.endswith(b"\n"):
        body = raw[:-1]
        return Frame("oversize") if len(body) > MAX_LINE else Frame("line", body)
    if len(raw) <= MAX_LINE:  # EOF in the middle of a short line
        return Frame("line", raw)
    while True:  # too long: skip the rest of the line
        chunk = stream.readline(_DRAIN_CHUNK)
        if not chunk or chunk.endswith(b"\n"):
            return Frame("oversize")


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_request(line: bytes) -> Request:
    """Decode and validate one request line; raises :class:`ProtocolError` (``BAD_REQUEST``) otherwise."""
    try:
        text = line.decode("utf-8")
        doc = json.loads(text, parse_constant=_no_constants, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolError(BAD_REQUEST, f"not a JSON line: {exc}") from None
    if not isinstance(doc, dict):
        raise ProtocolError(BAD_REQUEST, "a request is a JSON object")
    rid = doc.get("id")
    if not isinstance(rid, str) or not 1 <= len(rid) <= ID_MAX:
        raise ProtocolError(BAD_REQUEST, f"'id' must be a string of 1..{ID_MAX} characters")
    extra = sorted(set(doc) - {"id", "op", "args"})
    if extra:
        raise ProtocolError(BAD_REQUEST, f"unknown top-level key(s): {', '.join(map(repr, extra))}", req_id=rid)
    op = doc.get("op")
    if not isinstance(op, str) or not op:
        raise ProtocolError(BAD_REQUEST, "'op' must be a non-empty string", req_id=rid)
    args = doc.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError(BAD_REQUEST, "'args' must be an object", req_id=rid)
    return Request(rid, op, args)


def ok_response(req_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "result": result}


def error_response(req_id: str | None, code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        err["detail"] = detail
    return {"id": req_id, "ok": False, "error": err}


def encode(obj: dict[str, Any]) -> bytes:
    """Serialise one message (compact, ASCII-only, no NaN). ``ValueError`` if the line would exceed :data:`MAX_LINE`."""
    body = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(body) > MAX_LINE:
        raise ValueError(f"message of {len(body)} bytes exceeds the {MAX_LINE}-byte line cap")
    return body + b"\n"


class Writer:
    """Thread-safe line writer. A broken stdout (peer gone) calls ``on_broken`` once and drops everything after."""

    def __init__(self, stream: BinaryIO, on_broken: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._on_broken = on_broken
        self.broken = False

    def send(self, obj: dict[str, Any]) -> bool:
        """Write one message; ``False`` if it was not sent (stream gone, or over the cap: a response is then replaced by an ``INTERNAL``
        error so the request still gets its one answer; an oversized or unserialisable event is dropped)."""
        try:
            data = encode(obj)
        except (ValueError, TypeError) as exc:
            if "ok" in obj and "id" in obj:
                return self.send(error_response(obj["id"], INTERNAL, f"response could not be sent: {exc}"))
            return False
        callback = None
        with self._lock:
            if self.broken:
                return False
            try:
                self._stream.write(data)
                self._stream.flush()
                return True
            except (BrokenPipeError, ValueError, OSError):
                self.broken = True
                callback = self._on_broken
        if callback is not None:
            callback()
        return False
