"""Hardened local HTTP server (SPEC §4.2). Standard library only; no request-derived file paths; no state-changing GET.

Request pipeline (every request): Host exact -> route -> Origin (POST, SSE) -> auth (session cookie) -> Content-Type -> size -> body.
Routes (fixed): ``POST /bootstrap`` (launcher form, token swap), ``GET`` manifest pages, ``GET /api/status``, ``GET /api/events`` (SSE),
``POST /api/call`` (JSON ``{"op", "args"}`` forwarded to the helper, response passed through).
"""
from __future__ import annotations

import collections
import hmac
import http.server
import json
import queue
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http import cookies
from pathlib import Path
from typing import Any, Callable

from .helperlink import CLOSED, HelperLink, HelperTimeout, HelperUnavailable, RequestTooLarge

COOKIE_NAME = "arltuner_session"
MANIFEST: dict[str, tuple[str, str]] = {  # URL path -> (file name inside web_root, content type); the only files ever served
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
_OP_RE = re.compile(r"[a-z0-9_.]{1,64}\Z")
_EVENT_RE = re.compile(r"[a-z0-9_.]{1,32}\Z")
# HTTP status for a helper error code (Contract 8 codes, passed through untouched; unknown codes -> 422)
_STATUS_FOR_CODE = {"BAD_REQUEST": 400, "UNKNOWN_OP": 400, "BUSY": 409, "PLAN_STALE": 409, "CONFIRM_MISMATCH": 409, "CANCELLED": 409,
                    "REFUSED": 403, "PREFLIGHT_FAILED": 412, "INTERNAL": 500}


@dataclass
class Config:
    web_root: Path
    token: str | None = None                  # bootstrap token; generated (256 bit) when None
    token_ttl_s: float = 900.0
    max_json_body: int = 32 * 1024            # must stay well under the helper's 64 KiB line
    max_form_body: int = 1024
    request_timeout_s: float = 10.0           # per-recv socket timeout AND total deadline for reading a body
    sse_write_timeout_s: float = 5.0
    sse_keepalive_s: float = 15.0
    max_sse_clients: int = 8
    call_timeout_s: float = 120.0
    fail_limit: int = 10                      # failed auth attempts ...
    fail_window_s: float = 60.0               # ... within this window block further unauthenticated requests
    clock: Callable[[], float] = field(default=time.monotonic)


class RateLimiter:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], float]) -> None:
        self._limit, self._window, self._clock = limit, window_s, clock
        self._fails: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._fails and now - self._fails[0] > self._window:
            self._fails.popleft()

    def blocked(self) -> bool:
        with self._lock:
            self._prune(self._clock())
            return len(self._fails) >= self._limit

    def fail(self) -> None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._fails.append(now)


class WebServer:
    """Owns the listening socket, the bootstrap token, the sessions and the manifest contents."""

    def __init__(self, config: Config, helper: HelperLink | None) -> None:
        self.cfg = config
        self.helper = helper
        self._token = config.token or secrets.token_urlsafe(32)
        self._token_born = config.clock()
        self._token_used = False
        self._sessions: set[str] = set()
        self._lock = threading.Lock()
        self.limiter = RateLimiter(config.fail_limit, config.fail_window_s, config.clock)
        self.stop = threading.Event()
        self.sse_clients = 0
        self.on_bootstrap: Callable[[], None] | None = None
        # manifest contents are read once, here; a request never reaches the filesystem
        self.assets: dict[str, tuple[bytes, str]] = {}
        for url, (name, ctype) in MANIFEST.items():
            p = config.web_root / name
            if p.is_file():
                self.assets[url] = (p.read_bytes(), ctype)
        outer = self

        class Handler(_Handler):
            srv = outer
            timeout = config.request_timeout_s

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port: int = self.httpd.server_address[1]
        self.host_header = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host_header}"
        self._thread: threading.Thread | None = None

    @property
    def token(self) -> str:
        return self._token

    def start(self) -> "WebServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, name="web", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- auth state -----------------------------------------------------------------------------------------------------------
    def swap_token(self, presented: str) -> str | None:
        """Single-use, constant-time. Returns a new session id, or None. The token is burned on the first correct presentation."""
        with self._lock:
            ok = hmac.compare_digest(presented.encode("utf-8", "replace"), self._token.encode("utf-8"))
            expired = self.cfg.clock() - self._token_born > self.cfg.token_ttl_s
            if not ok or self._token_used or expired:
                return None
            self._token_used = True
            sid = secrets.token_urlsafe(32)
            self._sessions.add(sid)
        if self.on_bootstrap:
            self.on_bootstrap()
        return sid

    def valid_session(self, presented: str | None) -> bool:
        if not presented:
            return False
        p = presented.encode("utf-8", "replace")
        with self._lock:
            sessions = list(self._sessions)
        found = False
        for s in sessions:  # no early exit: constant-time per candidate
            found |= hmac.compare_digest(p, s.encode("utf-8"))
        return found


class _Handler(http.server.BaseHTTPRequestHandler):
    srv: WebServer
    server_version = "arltuner"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection; no keep-alive state to abuse

    def log_message(self, fmt: str, *args: Any) -> None:  # silent; never log request contents (cookies, tokens)
        pass

    # ---- output ---------------------------------------------------------------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int | None, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self._headers(status, ctype, len(body), extra)
        self.wfile.write(body)

    def _json(self, status: int, obj: Any, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json", extra)

    def _err(self, status: int, what: str) -> None:
        self._json(status, {"ok": False, "server_error": what})

    # ---- checks ---------------------------------------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        return len(hosts) == 1 and hosts[0] == self.srv.host_header

    def _origin_ok(self, allow_null: bool = False) -> bool:
        origins = self.headers.get_all("Origin") or []
        if len(origins) != 1:
            return False
        return origins[0] == self.srv.origin or (allow_null and origins[0] == "null")

    def _cookie(self) -> str | None:
        raw = self.headers.get_all("Cookie") or []
        if len(raw) != 1:
            return None
        try:
            jar = cookies.SimpleCookie()
            jar.load(raw[0])
        except cookies.CookieError:
            return None
        m = jar.get(COOKIE_NAME)
        return m.value if m else None

    def _auth(self) -> bool:
        """Session cookie check with failure accounting. While rate-limited, unauthenticated requests get 429 without being evaluated."""
        if self.srv.limiter.blocked():
            self._err(429, "rate_limited")
            return False
        if self.srv.valid_session(self._cookie()):
            return True
        self.srv.limiter.fail()
        self._err(401, "unauthorized")
        return False

    def _read_body(self, cap: int) -> bytes | None:
        """Content-Length required (no chunked), capped, with a total deadline. Sends the error itself and returns None on failure."""
        if self.headers.get("Transfer-Encoding") is not None:
            self._err(411, "length_required")
            return None
        cl = self.headers.get("Content-Length")
        if cl is None or not cl.isascii() or not cl.isdigit() or len(cl) > 9:
            self._err(411, "length_required")
            return None
        n = int(cl)
        if n > cap:
            self._err(413, "body_too_large")
            return None
        deadline = time.monotonic() + self.srv.cfg.request_timeout_s
        buf = bytearray()
        try:
            while len(buf) < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(n - len(buf), 4096))
                if not chunk:
                    self.close_connection = True
                    return None
                buf += chunk
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            try:
                self._err(408, "request_timeout")
            except OSError:
                pass
            return None
        return bytes(buf)

    @staticmethod
    def _path(raw: str) -> str | None:
        if not raw.startswith("/") or raw.startswith("//"):
            return None
        return urllib.parse.urlsplit(raw).path

    # ---- dispatch -------------------------------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = lambda self: self._reject_method()  # type: ignore[assignment]

    def _reject_method(self) -> None:
        if self._host_ok():
            self._err(405, "method_not_allowed")
        else:
            self._err(403, "bad_host")

    def _route(self, method: str) -> None:
        try:
            if not self._host_ok():
                return self._err(403, "bad_host")
            path = self._path(self.path)
            if path is None:
                return self._err(404, "not_found")
            if path == "/bootstrap":
                return self._bootstrap() if method == "POST" else self._err(405, "method_not_allowed")
            if path == "/api/call":
                return self._call() if method == "POST" else self._err(405, "method_not_allowed")
            if path == "/api/events":
                return self._events() if method == "GET" else self._err(405, "method_not_allowed")
            if path == "/api/status":
                return self._status() if method == "GET" else self._err(405, "method_not_allowed")
            if path in MANIFEST:
                return self._static(path) if method == "GET" else self._err(405, "method_not_allowed")
            self._err(404, "not_found")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    # ---- handlers -------------------------------------------------------------------------------------------------------------
    def _static(self, path: str) -> None:
        if not self._auth():
            return
        asset = self.srv.assets.get(path)
        if asset is None:
            return self._err(404, "not_found")
        self._send(200, asset[0], asset[1])

    def _status(self) -> None:
        if not self._auth():
            return
        h = self.srv.helper
        self._json(200, {"ok": True, "helper_alive": bool(h and h.alive), "sse_clients": self.srv.sse_clients})

    def _bootstrap(self) -> None:
        # The launcher is a file:// page, so its form POST carries ``Origin: null``; that (or our own origin) is the only exception.
        if not self._origin_ok(allow_null=True):
            return self._err(403, "bad_origin")
        if self.srv.limiter.blocked():
            return self._err(429, "rate_limited")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._err(415, "unsupported_media_type")
        body = self._read_body(self.srv.cfg.max_form_body)
        if body is None:
            return
        try:
            fields = urllib.parse.parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        except (ValueError, UnicodeDecodeError):
            fields = {}
        tokens = fields.get("token", [])
        sid = self.srv.swap_token(tokens[0]) if len(tokens) == 1 else None
        if sid is None:
            self.srv.limiter.fail()
            return self._err(403, "bad_token")
        # A 303 here would be followed as a cross-site redirect chain (file:// origin) and SameSite=Strict would drop the cookie, so
        # answer 200 and let the now same-site document navigate itself to the clean URL.
        page = (b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
                b'<title>arrowlake-tuner</title><a href="/">Continue</a>')
        cookie = f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Strict"
        self._send(200, page, "text/html; charset=utf-8", {"Set-Cookie": cookie})

    def _call(self) -> None:
        if not self._origin_ok():
            return self._err(403, "bad_origin")
        if not self._auth():
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._err(415, "unsupported_media_type")
        body = self._read_body(self.srv.cfg.max_json_body)
        if body is None:
            return
        try:
            req = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._err(400, "bad_json")
        if (not isinstance(req, dict) or not set(req) <= {"op", "args"} or not isinstance(req.get("op"), str)
                or not _OP_RE.match(req["op"]) or not isinstance(req.get("args", {}), dict)):
            return self._err(400, "bad_request")
        helper = self.srv.helper
        if helper is None:
            return self._err(503, "helper_unavailable")
        try:
            resp = helper.call(req["op"], req.get("args", {}), timeout=self.srv.cfg.call_timeout_s)
        except HelperUnavailable:
            return self._err(503, "helper_unavailable")
        except HelperTimeout:
            return self._err(504, "helper_timeout")
        except RequestTooLarge:
            return self._err(413, "body_too_large")
        if resp.get("ok") is True:
            return self._json(200, resp)
        code = (resp.get("error") or {}).get("code") if isinstance(resp.get("error"), dict) else None
        self._json(_STATUS_FOR_CODE.get(code, 422), resp)

    def _events(self) -> None:
        if not self._origin_ok():
            return self._err(403, "bad_origin")
        if not self._auth():
            return
        srv = self.srv
        helper = srv.helper
        if helper is None:
            return self._err(503, "helper_unavailable")
        with srv._lock:
            if srv.sse_clients >= srv.cfg.max_sse_clients:
                full = True
            else:
                full = False
                srv.sse_clients += 1
        if full:
            return self._err(503, "too_many_streams")
        sub = helper.subscribe()
        try:
            self.connection.settimeout(srv.cfg.sse_write_timeout_s)  # a client that stops reading is dropped
            self._headers(200, "text/event-stream", None)
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not srv.stop.is_set():
                try:
                    ev = sub.queue.get(timeout=min(0.5, srv.cfg.sse_keepalive_s))
                except queue.Empty:
                    ev = None
                if ev is CLOSED:
                    break
                if sub.overflowed:
                    break
                if ev is None:
                    if time.monotonic() - getattr(self, "_last_write", 0.0) < srv.cfg.sse_keepalive_s:
                        continue
                    self.wfile.write(b": keepalive\n\n")
                else:
                    name = ev.get("event")
                    if not isinstance(name, str) or not _EVENT_RE.match(name):
                        continue
                    data = json.dumps(ev, separators=(",", ":"))
                    self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                self._last_write = time.monotonic()
        finally:
            sub.close()
            with srv._lock:
                srv.sse_clients -= 1
            self.close_connection = True
