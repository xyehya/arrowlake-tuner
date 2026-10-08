"""Hardened local HTTP server (SPEC §4.2, Contract 8 addendum D-027). Standard library only; no request-derived file paths; no state-changing GET.

Request pipeline: Host exact -> route -> Origin (exact; required on POST, optional-but-exact on SSE) -> Sec-Fetch-Site (SSE) -> session cookie ->
Content-Type -> size -> body -> op allowlist + args schema -> helper.
Routes (fixed): ``POST /bootstrap`` (launcher form, token swap), ``GET`` manifest pages, ``GET /api/status``, ``GET /api/events`` (SSE),
``POST /api/call`` (allowlisted ``op`` + schema-checked ``args``), ``POST /api/logout``.

Long ops (apply, restore, vf.dump) answer ``202 {"id"}`` at once; the helper's final response arrives as SSE event ``result``.

Auth: a valid session cookie is never rate-limited. Only a *wrong credential* (bad bootstrap token, unknown cookie value) counts as a failure;
anonymous requests (no cookie) are refused with 401 and not counted. Sessions expire (idle 30 min, absolute 12 h) and can be logged out.

Residual risk (documented, accepted): cookies are not port-isolated on 127.0.0.1 - another local service on the same host name receives the
cookie. Mitigations: session expiry, exact Host / Origin / Sec-Fetch-Site checks, the plan-bound CONFIRM nonce (SPEC §4.3), and polkit
authentication at helper start (SPEC §4.1).
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
_EVENT_RE = re.compile(r"[a-z0-9_.]{1,32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TYPED = re.compile(r"CONFIRM [A-Z2-9]{6}\Z")
_REQ_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


# ---- /api/call allowlist (Contract 8 addendum D-027 d): op -> (args validator, long-running). Schemas mirror the G-102 helper validators (helper/ops.py).
def _empty(a: dict) -> bool:
    return a == {}


def _audit_tail(a: dict) -> bool:
    return set(a) <= {"n"} and (("n" not in a) or (type(a["n"]) is int and 1 <= a["n"] <= 200))


def _vf_dump(a: dict) -> bool:  # outer filter; the helper (ops._validate_vf_dump) is the authority
    if not set(a) <= {"cpus", "all_e"} or ("all_e" in a and type(a["all_e"]) is not bool):
        return False
    c = a.get("cpus")
    return c is None or (isinstance(c, list) and 1 <= len(c) <= 64 and all(type(x) is int and 0 <= x < 1024 for x in c))


def _telemetry_start(a: dict) -> bool:
    i = a.get("interval_s", 1.0)
    return set(a) <= {"interval_s"} and type(i) in (int, float) and 0.2 <= i <= 60.0


def _cancel(a: dict) -> bool:
    return set(a) <= {"op_id"} and (("op_id" not in a) or (isinstance(a["op_id"], str) and bool(_REQ_ID.match(a["op_id"]))))


def _plan(a: dict) -> bool:
    return set(a) == {"profile"} and isinstance(a["profile"], dict)


def _apply(a: dict) -> bool:
    return (set(a) == {"plan_id", "typed"} and isinstance(a["plan_id"], str) and bool(_HEX64.match(a["plan_id"]))
            and isinstance(a["typed"], str) and bool(_TYPED.match(a["typed"])))


OPS: dict[str, tuple[Callable[[dict], bool], bool]] = {
    "preflight": (_empty, False),
    "audit.tail": (_audit_tail, False),
    "telemetry.start": (_telemetry_start, False),
    "telemetry.stop": (_empty, False),
    "cancel": (_cancel, False),
    "plan": (_plan, False),
    "vf.dump": (_vf_dump, True),
    "apply": (_apply, True),
    "restore": (_empty, True),
}

# HTTP status for a helper error code (Contract 8 codes; unknown codes -> 422)
_STATUS_FOR_CODE = {"BAD_REQUEST": 400, "UNKNOWN_OP": 400, "BUSY": 409, "PLAN_STALE": 409, "CONFIRM_MISMATCH": 409, "CANCELLED": 409,
                    "REFUSED": 403, "PREFLIGHT_FAILED": 412, "INTERNAL": 500}
_DETAIL_ALLOW = ("state", "check")  # the only error.detail keys that reach the browser


def public_response(resp: dict[str, Any]) -> dict[str, Any]:
    """What the browser may see of a helper response: id, ok, result, or error {code, message, detail restricted to an allowlist}."""
    if resp.get("ok") is True:
        return {"id": resp.get("id"), "ok": True, "result": resp.get("result")}
    err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
    out: dict[str, Any] = {"code": err.get("code") if isinstance(err.get("code"), str) else "INTERNAL",
                           "message": str(err.get("message", ""))[:500]}
    det = err.get("detail")
    if isinstance(det, dict):
        keep = {k: det[k] for k in _DETAIL_ALLOW if k in det and isinstance(det[k], (str, int, float, bool, type(None)))}
        if keep:
            out["detail"] = keep
    return {"id": resp.get("id"), "ok": False, "error": out}


@dataclass
class Config:
    web_root: Path
    token: str | None = None                  # bootstrap token; generated (256 bit) when None
    token_ttl_s: float = 900.0
    max_json_body: int = 32 * 1024            # must stay well under the helper's 64 KiB line
    max_form_body: int = 1024
    request_timeout_s: float = 10.0           # per-recv socket timeout AND total deadline for reading a body
    header_deadline_s: float = 10.0           # total time to receive the request line + headers (slowloris)
    max_connections: int = 16                 # concurrent connections; excess are closed immediately
    sse_write_timeout_s: float = 5.0
    sse_keepalive_s: float = 15.0
    max_sse_clients: int = 8
    call_timeout_s: float = 30.0              # short ops only; long ops are 202 + SSE result
    max_pending_long: int = 4
    session_idle_s: float = 1800.0
    session_max_s: float = 12 * 3600.0
    fail_limit: int = 10                      # wrong-credential attempts ...
    fail_window_s: float = 60.0               # ... within this window block further wrong-credential attempts
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


class _HTTPD(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], handler: type, max_connections: int) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(addr, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # over the connection cap: closed immediately
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WebServer:
    """Owns the listening socket, the bootstrap token, the sessions and the manifest contents."""

    def __init__(self, config: Config, helper: HelperLink | None) -> None:
        self.cfg = config
        self.helper = helper
        self._token = config.token or secrets.token_urlsafe(32)
        self._token_born = config.clock()
        self._token_used = False
        self._sessions: dict[str, list[float]] = {}  # id -> [created, last_seen]
        self._lock = threading.Lock()
        self.limiter = RateLimiter(config.fail_limit, config.fail_window_s, config.clock)       # wrong session cookies
        self.boot_limiter = RateLimiter(config.fail_limit, config.fail_window_s, config.clock)  # wrong bootstrap tokens (own budget)
        self.stop = threading.Event()
        self.sse_clients = 0
        self._long_lock = threading.RLock()
        self._long: dict[str, list] = {}  # request id -> [session id, consecutive idle heartbeats] of long ops awaiting the helper's answer
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

        self.httpd = _HTTPD(("127.0.0.1", 0), Handler, config.max_connections)
        self.port: int = self.httpd.server_address[1]
        self.host_header = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host_header}"
        self._thread: threading.Thread | None = None

    @property
    def token(self) -> str:
        return self._token

    def start(self) -> "WebServer":
        if self.helper is not None:
            threading.Thread(target=self._watch_helper, name="web-helper-watch", daemon=True).start()
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
            now = self.cfg.clock()
            ok = hmac.compare_digest(presented.encode("utf-8", "replace"), self._token.encode("utf-8"))
            expired = now - self._token_born > self.cfg.token_ttl_s
            if not ok or self._token_used or expired:
                return None
            self._token_used = True
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = [now, now]
        if self.on_bootstrap:
            self.on_bootstrap()
        return sid

    def valid_session(self, presented: str | None, touch: bool = True) -> str | None:
        """Return the matching session id (expired ones are dropped; ``touch`` refreshes the idle timer) or None."""
        if not presented:
            return None
        p = presented.encode("utf-8", "replace")
        now = self.cfg.clock()
        found: str | None = None
        with self._lock:
            for sid in list(self._sessions):
                created, last = self._sessions[sid]
                if now - created > self.cfg.session_max_s or now - last > self.cfg.session_idle_s:
                    del self._sessions[sid]
                    continue
                if hmac.compare_digest(p, sid.encode("utf-8")):  # no early exit: constant-time per candidate
                    found = sid
            if found and touch:
                self._sessions[found][1] = now
        return found

    def new_bootstrap(self) -> str:
        """Mint a fresh single-use bootstrap token (the previous one stops working). Existing sessions are untouched."""
        with self._lock:
            self._token = secrets.token_urlsafe(32)
            self._token_born = self.cfg.clock()
            self._token_used = False
            return self._token

    def logout(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    # ---- long-op bookkeeping --------------------------------------------------------------------------------------------------
    @property
    def pending_long(self) -> int:
        with self._long_lock:
            return len(self._long)

    def submit_long(self, helper: HelperLink, op: str, args: dict[str, Any], sid: str) -> str | None:
        """Submit a long op if a slot is free (None = no slot). The slot is held until the helper answers, the link dies, or the helper
        reports ``idle`` for 3 consecutive heartbeats while the op is still pending (``helper_lost``). No wall-clock timeout."""
        with self._long_lock:
            if len(self._long) >= self.cfg.max_pending_long:
                return None

            def done(rid: str, resp: dict[str, Any] | None) -> None:
                with self._long_lock:
                    if self._long.pop(rid, None) is None:  # already reported lost
                        return
                if resp is None:
                    ev: dict[str, Any] = {"event": "result", "id": rid, "ok": False, "server_error": "helper_lost"}
                else:
                    ev = {"event": "result", **public_response(resp)}
                helper.publish(ev, sid)

            rid = helper.submit(op, args, done)  # raises HelperUnavailable / RequestTooLarge; nothing is registered then
            self._long[rid] = [sid, 0]
            return rid

    def _watch_helper(self) -> None:
        sub = self.helper.subscribe()  # type: ignore[union-attr]
        try:
            while not self.stop.is_set():
                try:
                    ev = sub.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if ev is CLOSED:
                    return
                if ev.get("event") != "heartbeat":
                    continue
                lost: list[tuple[str, str]] = []
                with self._long_lock:
                    for rid, rec in list(self._long.items()):
                        rec[1] = rec[1] + 1 if ev.get("state") == "idle" else 0
                        if rec[1] >= 3:
                            lost.append((rid, rec[0]))
                            del self._long[rid]
                for rid, sid in lost:
                    self.helper.publish({"event": "result", "id": rid, "ok": False, "server_error": "helper_lost"}, sid)  # type: ignore[union-attr]
        finally:
            sub.close()


class _Handler(http.server.BaseHTTPRequestHandler):
    srv: WebServer
    server_version = "arltuner"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection; no keep-alive state to abuse

    def log_message(self, fmt: str, *args: Any) -> None:  # silent; never log request contents (cookies, tokens)
        pass

    # ---- slowloris: total deadline for the request line + headers ------------------------------------------------------------
    def setup(self) -> None:
        super().setup()
        self._hdr_timer: threading.Timer | None = threading.Timer(self.srv.cfg.header_deadline_s, self._kill_socket)
        self._hdr_timer.daemon = True
        self._hdr_timer.start()

    def _kill_socket(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _headers_done(self) -> None:
        t, self._hdr_timer = self._hdr_timer, None
        if t is not None:
            t.cancel()

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            self._headers_done()

    def finish(self) -> None:
        self._headers_done()
        try:
            super().finish()
        except OSError:
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

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Errors raised inside http.server itself (malformed request line, 414, 431, ...) get the same headers and a JSON body."""
        self.close_connection = True
        try:
            self._err(code, "bad_request" if code < 500 else "server_error")
        except OSError:
            pass

    # ---- checks ---------------------------------------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        return len(hosts) == 1 and hosts[0] == self.srv.host_header

    def _origin_ok(self, allow_null: bool = False, required: bool = True) -> bool:
        origins = self.headers.get_all("Origin") or []
        if not origins:
            return not required
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

    def _auth(self) -> str | None:
        """Session id of a valid cookie, else None after sending the error. A valid session is never rate-limited; no cookie at all is a plain
        401 and is not counted; a *wrong* cookie value counts as a failed attempt (429 once the limit is reached)."""
        presented = self._cookie()
        sid = self.srv.valid_session(presented)
        if sid is not None:
            return sid
        if presented is None:
            self._err(401, "unauthorized")
            return None
        if self.srv.limiter.blocked():
            self._err(429, "rate_limited")
            return None
        self.srv.limiter.fail()
        self._err(401, "unauthorized")
        return None

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
            routes: dict[str, tuple[str, Callable[[], None]]] = {
                "/bootstrap": ("POST", self._bootstrap), "/api/call": ("POST", self._call), "/api/logout": ("POST", self._logout),
                "/api/events": ("GET", self._events), "/api/status": ("GET", self._status),
            }
            if path in routes:
                want, fn = routes[path]
                return fn() if method == want else self._err(405, "method_not_allowed")
            if path in MANIFEST:
                return self._static(path) if method == "GET" else self._err(405, "method_not_allowed")
            self._err(404, "not_found")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    # ---- handlers -------------------------------------------------------------------------------------------------------------
    def _static(self, path: str) -> None:
        if self._auth() is None:
            return
        asset = self.srv.assets.get(path)
        if asset is None:
            return self._err(404, "not_found")
        self._send(200, asset[0], asset[1])

    def _status(self) -> None:
        if self._auth() is None:
            return
        h = self.srv.helper
        self._json(200, {"ok": True, "helper_alive": bool(h and h.alive), "sse_clients": self.srv.sse_clients})

    def _logout(self) -> None:
        if not self._origin_ok():
            return self._err(403, "bad_origin")
        sid = self._auth()
        if sid is None:
            return
        self.srv.logout(sid)
        self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    def _bootstrap(self) -> None:
        # The launcher is a file:// page, so its form POST carries ``Origin: null``; that (or our own origin) is the only exception.
        if not self._origin_ok(allow_null=True):
            return self._err(403, "bad_origin")
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
        if sid is None:  # only a wrong token is throttled (own limiter); a correct one is never refused by a limiter
            if self.srv.boot_limiter.blocked():
                return self._err(429, "rate_limited")
            self.srv.boot_limiter.fail()
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
        sid = self._auth()
        if sid is None:
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
        if not isinstance(req, dict) or not set(req) <= {"op", "args"} or not isinstance(req.get("op"), str):
            return self._err(400, "bad_request")
        args = req.get("args", {})
        spec = OPS.get(req["op"])
        if spec is None:
            return self._err(400, "unknown_op")
        validate, is_long = spec
        if not isinstance(args, dict) or not validate(args):
            return self._err(400, "bad_args")
        helper = self.srv.helper
        if helper is None:
            return self._err(503, "helper_unavailable")
        if is_long:
            return self._call_long(helper, req["op"], args, sid)
        try:
            resp = helper.call(req["op"], args, timeout=self.srv.cfg.call_timeout_s)
        except HelperUnavailable:
            return self._err(503, "helper_unavailable")
        except HelperTimeout:
            return self._err(504, "helper_timeout")
        except RequestTooLarge:
            return self._err(413, "body_too_large")
        out = public_response(resp)
        if out["ok"]:
            return self._json(200, out)
        self._json(_STATUS_FOR_CODE.get(out["error"]["code"], 422), out)

    def _call_long(self, helper: HelperLink, op: str, args: dict[str, Any], sid: str) -> None:
        try:
            rid = self.srv.submit_long(helper, op, args, sid)
        except HelperUnavailable:
            return self._err(503, "helper_unavailable")
        except RequestTooLarge:
            return self._err(413, "body_too_large")
        if rid is None:
            return self._err(429, "too_many_pending")
        self._json(202, {"id": rid})

    def _events(self) -> None:
        # Browsers omit Origin on same-origin GET, so it cannot be required (D-027 b): cookie + exact Host + Sec-Fetch-Site: same-origin;
        # an Origin that is sent must be exact.
        if not self._origin_ok(required=False):
            return self._err(403, "bad_origin")
        if (self.headers.get("Sec-Fetch-Site") or "") != "same-origin":
            return self._err(403, "bad_fetch_site")
        sid = self._auth()
        if sid is None:
            return
        srv = self.srv
        helper = srv.helper
        if helper is None:
            return self._err(503, "helper_unavailable")
        with srv._lock:
            full = srv.sse_clients >= srv.cfg.max_sse_clients
            if not full:
                srv.sse_clients += 1
        if full:
            return self._err(503, "too_many_streams")
        sub = helper.subscribe(sid)
        last_write = time.monotonic()
        try:
            self.connection.settimeout(srv.cfg.sse_write_timeout_s)  # a client that stops reading is dropped
            self._headers(200, "text/event-stream", None)
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not srv.stop.is_set():
                if srv.valid_session(sid) is None:  # an open stream is activity (idle timer); logged out / expired / 12 h cap ends it
                    break
                try:
                    ev = sub.queue.get(timeout=min(0.5, srv.cfg.sse_keepalive_s))
                except queue.Empty:
                    ev = None
                if ev is CLOSED or sub.overflowed:
                    break
                if ev is None:
                    if time.monotonic() - last_write < srv.cfg.sse_keepalive_s:
                        continue
                    self.wfile.write(b": keepalive\n\n")
                else:
                    name = ev.get("event")
                    if not isinstance(name, str) or not _EVENT_RE.match(name):
                        continue
                    data = json.dumps(ev, separators=(",", ":"))
                    self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_write = time.monotonic()
        finally:
            sub.close()
            with srv._lock:
                srv.sse_clients -= 1
            self.close_connection = True
