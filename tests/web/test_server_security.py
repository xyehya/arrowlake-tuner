"""SPEC §12 G1 security tests for the web server. Binds only 127.0.0.1 ephemeral ports; the helper is a fake speaking Contract 8."""
from __future__ import annotations

import http.client
import json
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path

from arltuner.web.helperlink import HelperLink
from arltuner.web.launcher import write_launcher
from arltuner.web.server import Config, WebServer
from tests.web.fake_helper import FakeHelper

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
JSON_CT = "application/json"
SF = {"Sec-Fetch-Site": "same-origin"}


class Base(unittest.TestCase):
    cfg_kw: dict = {}

    def setUp(self) -> None:
        self.fake = FakeHelper()
        self.srv = WebServer(Config(web_root=WEB_ROOT, **self.cfg_kw), self.fake.link).start()
        self.addCleanup(self.srv.close)
        self.addCleanup(self.fake.close)

    # raw request with full control over headers (http.client would otherwise add Host)
    def req(self, method, path, headers=None, body=None, host="default", cookie=None, origin=None):
        h = dict(headers or {})
        if host == "default":
            host = self.srv.host_header
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            c.putheader("Host", host)
        if cookie:
            c.putheader("Cookie", f"arltuner_session={cookie}")
        if origin:
            c.putheader("Origin", origin)
        for k, v in h.items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r, data

    def bootstrap(self, token=None):
        body = f"token={token or self.srv.token}".encode()
        return self.req("POST", "/bootstrap", {"Content-Type": "application/x-www-form-urlencoded"}, body, origin="null")

    def login(self) -> str:
        st, r, _ = self.bootstrap()
        self.assertEqual(st, 200)
        sc = r.getheader("Set-Cookie")
        return sc.split(";")[0].split("=", 1)[1]

    def call(self, op, args=None, cookie=None, origin="default"):
        if origin == "default":
            origin = self.srv.origin
        body = json.dumps({"op": op, "args": args or {}}).encode()
        return self.req("POST", "/api/call", {"Content-Type": JSON_CT}, body, cookie=cookie, origin=origin)


class TestBootstrapAndHeaders(Base):
    def test_flow_cookie_flags_and_headers(self):
        st, r, body = self.bootstrap()
        self.assertEqual(st, 200)
        sc = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)
        self.assertNotIn(self.srv.token, sc)
        self.assertIn(b'url=/"', body)  # navigates to the clean URL
        cookie = sc.split(";")[0].split("=", 1)[1]
        st, r, body = self.req("GET", "/", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"arrowlake-tuner", body)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("<script>", body.decode())  # no inline script in the page
        for p in ("/app.js", "/style.css"):
            self.assertEqual(self.req("GET", p, cookie=cookie)[0], 200)

    def test_token_is_at_least_128_bit(self):
        self.assertGreaterEqual(len(self.srv.token) * 6, 128)

    def test_token_reuse_rejected(self):
        self.login()
        st, r, _ = self.bootstrap()
        self.assertEqual(st, 403)
        self.assertIsNone(r.getheader("Set-Cookie"))

    def test_wrong_token_and_bad_forms(self):
        self.assertEqual(self.bootstrap("x" * 43)[0], 403)
        for body in (b"", b"token=", b"foo=bar", b"token=a&token=b", b"\xff\xfe"):
            st, _, _ = self.req("POST", "/bootstrap", {"Content-Type": "application/x-www-form-urlencoded"}, body, origin="null")
            self.assertEqual(st, 403, body)
        # real token still works afterwards (failures do not burn it)
        self.assertEqual(self.bootstrap()[0], 200)

    def test_bootstrap_needs_origin_null_or_own(self):
        b = f"token={self.srv.token}".encode()
        ct = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.req("POST", "/bootstrap", ct, b)[0], 403)
        self.assertEqual(self.req("POST", "/bootstrap", ct, b, origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/bootstrap", {"Content-Type": "text/plain"}, b, origin="null")[0], 415)
        self.assertEqual(self.req("POST", "/bootstrap", ct, b, origin="null")[0], 200)

    def test_token_expires(self):
        now = [0.0]
        srv = WebServer(Config(web_root=WEB_ROOT, token_ttl_s=10, clock=lambda: now[0]), None)
        self.addCleanup(srv.httpd.server_close)
        now[0] = 11
        self.assertIsNone(srv.swap_token(srv.token))

    def test_launcher_file_is_0600_and_posts_to_origin(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_launcher(Path(d) / "sub" / "l.html", self.srv.origin, self.srv.token)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
            txt = p.read_text()
            self.assertIn(self.srv.token, txt)
            self.assertIn(f'action="{self.srv.origin}/bootstrap"', txt)
            self.assertNotIn("<script", txt)
            write_launcher(p, self.srv.origin, "t2")  # replaces an older file
            self.assertIn("t2", p.read_text())


class TestHostOrigin(Base):
    def test_host_missing_or_wrong(self):
        ck = self.login()
        for host in (None, "evil.example", "localhost:%d" % self.srv.port, "127.0.0.1", "127.0.0.1:%d" % (self.srv.port + 1),
                     "127.0.0.1:%d.evil.example" % self.srv.port):
            st, _, _ = self.req("GET", "/", cookie=ck, host=host)
            self.assertEqual(st, 403, host)
            st, _, _ = self.req("POST", "/api/call", {"Content-Type": JSON_CT}, b"{}", host=host, cookie=ck, origin=self.srv.origin)
            self.assertEqual(st, 403, host)

    def test_origin_missing_or_wrong_on_post_and_sse(self):
        ck = self.login()
        for origin in (None, "null", "http://evil.example", "http://127.0.0.1", "https://" + self.srv.host_header,
                       self.srv.origin + "/", self.srv.origin.upper()):
            st, _, _ = self.call("preflight", cookie=ck, origin=origin)
            self.assertEqual(st, 403, origin)
            if origin is not None:  # SSE: Origin is optional (browsers omit it), but when sent it must be exact
                st, _, _ = self.req("GET", "/api/events", cookie=ck, origin=origin, headers=SF)
                self.assertEqual(st, 403, origin)
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)

    def test_cross_site_simple_post(self):
        ck = self.login()
        # a classic CSRF form post: text/plain (or urlencoded) body, attacker origin, victim's cookie rides along
        for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
            st, _, _ = self.req("POST", "/api/call", {"Content-Type": ct}, b'{"op":"apply","args":{}}', cookie=ck,
                                origin="http://evil.example")
            self.assertEqual(st, 403, ct)
            # even with a forged-correct Origin the content type must be JSON
            st, _, _ = self.req("POST", "/api/call", {"Content-Type": ct}, b'{"op":"apply","args":{}}', cookie=ck, origin=self.srv.origin)
            self.assertEqual(st, 415, ct)
        st, _, _ = self.req("POST", "/api/call", {}, b'{"op":"apply"}', cookie=ck, origin=self.srv.origin)
        self.assertEqual(st, 415)
        self.assertEqual([r for r in self.fake.requests if r["op"] == "apply"], [])

    def test_other_methods_and_get_on_post_routes(self):
        ck = self.login()
        self.assertEqual(self.req("PUT", "/api/call", cookie=ck)[0], 405)
        self.assertEqual(self.req("DELETE", "/", cookie=ck)[0], 405)
        self.assertEqual(self.req("GET", "/api/call", cookie=ck)[0], 405)
        self.assertEqual(self.req("POST", "/", cookie=ck, origin=self.srv.origin, body=b"")[0], 405)


class TestAuth(Base):
    cfg_kw = {"fail_limit": 5}

    def test_cookie_absent_or_wrong(self):
        for path in ("/", "/app.js", "/api/status"):
            self.assertEqual(self.req("GET", path)[0], 401, path)
        self.assertEqual(self.call("preflight", cookie=None)[0], 401)
        self.assertEqual(self.req("GET", "/api/events", headers=SF)[0], 401)
        self.assertEqual(self.fake.requests, [])

    def test_wrong_cookie_and_malformed_cookie_header(self):
        self.assertEqual(self.req("GET", "/", cookie="A" * 43)[0], 401)
        self.assertEqual(self.req("GET", "/", headers={"Cookie": "arltuner_session"})[0], 401)
        self.assertEqual(self.req("GET", "/", headers={"Cookie": ";;;=="})[0], 401)

    def test_failed_auth_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.bootstrap("wrong-token")[0], 403)
        st, _, _ = self.bootstrap("wrong-token")
        self.assertEqual(st, 429)
        # the throttle only covers wrong tokens: the correct one still works, and the cookie limiter is a separate budget
        self.assertEqual(self.req("GET", "/", cookie="A" * 43)[0], 401)
        self.assertEqual(self.bootstrap()[0], 200)

    def test_rate_limit_window_expires(self):
        now = [0.0]
        srv = WebServer(Config(web_root=WEB_ROOT, fail_limit=2, fail_window_s=10, clock=lambda: now[0]), None)
        self.addCleanup(srv.httpd.server_close)
        srv.limiter.fail()
        srv.limiter.fail()
        self.assertTrue(srv.limiter.blocked())
        now[0] = 11
        self.assertFalse(srv.limiter.blocked())


class TestSizeAndPaths(Base):
    def test_oversize_body_rejected_before_reading(self):
        ck = self.login()
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        c.putrequest("POST", "/api/call", skip_host=True)
        c.putheader("Host", self.srv.host_header)
        c.putheader("Origin", self.srv.origin)
        c.putheader("Cookie", f"arltuner_session={ck}")
        c.putheader("Content-Type", JSON_CT)
        c.putheader("Content-Length", str(10 * 1024 * 1024))
        c.endheaders()  # the body is never sent
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        body = json.dumps({"op": "plan", "args": {"profile": {"x": "a" * 40000}}}).encode()
        self.assertEqual(self.req("POST", "/api/call", {"Content-Type": JSON_CT}, body, cookie=ck, origin=self.srv.origin)[0], 413)
        self.assertEqual(self.fake.requests, [])

    def test_bootstrap_body_cap(self):
        body = b"token=" + b"a" * 5000
        st, _, _ = self.req("POST", "/bootstrap", {"Content-Type": "application/x-www-form-urlencoded"}, body, origin="null")
        self.assertEqual(st, 413)

    def test_missing_or_bad_content_length_and_chunked(self):
        ck = self.login()
        base = {"Content-Type": JSON_CT}
        st, _, _ = self.req("POST", "/api/call", base, None, cookie=ck, origin=self.srv.origin)
        self.assertEqual(st, 411)
        st, _, _ = self.req("POST", "/api/call", {**base, "Transfer-Encoding": "chunked"}, b"0\r\n\r\n", cookie=ck, origin=self.srv.origin)
        self.assertEqual(st, 411)
        st, _, _ = self.req("POST", "/api/call", {**base, "Content-Length": "-5"}, None, cookie=ck, origin=self.srv.origin)
        self.assertEqual(st, 411)

    def test_path_traversal_and_unlisted_files(self):
        ck = self.login()
        for p in ("/../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2fetc/passwd", "//etc/passwd", "/app.js/../../x",
                  "/index.html", "/web/index.html", "/app.js%00.html", "/static/../app.js", "/.git/config", "/src/arltuner/web/server.py",
                  "/\\..\\etc\\passwd", "/%2e%2e", "/style.css/", "/ "):
            try:
                st, _, body = self.req("GET", p, cookie=ck)
            except http.client.InvalidURL:
                continue
            self.assertIn(st, (400, 404), p)
            self.assertNotIn(b"root:", body)
        # every served asset is exactly the manifest content
        self.assertEqual(set(self.srv.assets), {"/", "/app.js", "/style.css"})

    def test_query_string_ignored_not_joined(self):
        ck = self.login()
        self.assertEqual(self.req("GET", "/?file=../../etc/passwd", cookie=ck)[0], 200)


class TestJsonApi(Base):
    def test_validation(self):
        ck = self.login()
        h = {"Content-Type": JSON_CT}
        for body in (b"not json", b"[]", b"{}", b'{"op": 1}', b'{"op":"preflight","args":[]}', b'{"op":"preflight","extra":1}', b'{"op":"Bad Op"}',
                     b'{"op":"' + b"a" * 65 + b'"}'):
            st, _, _ = self.req("POST", "/api/call", h, body, cookie=ck, origin=self.srv.origin)
            self.assertEqual(st, 400, body)
        self.assertEqual(self.fake.requests, [])
        # charset parameter on the content type is fine
        st, _, _ = self.req("POST", "/api/call", {"Content-Type": "application/json; charset=utf-8"}, b'{"op":"preflight"}',
                            cookie=ck, origin=self.srv.origin)
        self.assertEqual(st, 200)

    def test_passthrough_and_status_mapping(self):
        ck = self.login()
        st, _, body = self.call("preflight", cookie=ck)
        self.assertEqual((st, json.loads(body)["ok"]), (200, True))
        st, _, body = self.call("cancel", cookie=ck)
        self.assertEqual(st, 409)
        self.assertEqual(json.loads(body)["error"]["detail"], {"state": "applying"})  # item 12: "internal_path" was dropped
        # the server sent exactly Contract 8 requests: id/op/args only
        for r in self.fake.requests:
            self.assertEqual(set(r), {"id", "op", "args"})
        self.assertEqual(len({r["id"] for r in self.fake.requests}), len(self.fake.requests))

    def test_plan_bound_nonce_replay(self):
        ck = self.login()
        sse = open_sse(self, ck)
        plan = json.loads(self.call("plan", {"profile": {}}, cookie=ck)[2])["result"]
        pid, nonce = plan["plan_id"], plan["nonce"]
        results = {}

        def apply(typed, plan_id=pid, origin="default"):
            st, _, body = self.call("apply", {"plan_id": plan_id, "typed": typed}, cookie=ck, origin=origin)
            return st, json.loads(body)

        def result_of(rid):
            if rid not in results:
                buf = read_until(sse, b'"id":"%s"' % rid.encode())
                for blk in buf.split(b"\n\n"):
                    if blk.startswith(b"event: result"):
                        d = json.loads(blk.split(b"data: ", 1)[1])
                        results[d["id"]] = d
            return results[rid]

        st, b = apply("CONFIRM WRONGZ")
        self.assertEqual(st, 202)  # long op: accepted at once, outcome arrives as an SSE result
        self.assertEqual(result_of(b["id"])["error"]["code"], "CONFIRM_MISMATCH")
        st, b = apply("CONFIRM " + nonce)
        self.assertEqual((st, result_of(b["id"])["ok"]), (202, True))
        st, b = apply("CONFIRM " + nonce)
        self.assertEqual(result_of(b["id"])["error"]["code"], "CONFIRM_MISMATCH")  # single-use nonce: replay refused
        st, b = apply("CONFIRM " + nonce, plan_id="0" * 64)
        self.assertEqual(result_of(b["id"])["error"]["code"], "PLAN_STALE")
        # a replay from another origin never reaches the helper
        n = len(self.fake.requests)
        self.assertEqual(apply("CONFIRM " + nonce, origin="http://evil.example")[0], 403)
        self.assertEqual(len(self.fake.requests), n)

    def test_helper_down(self):
        ck = self.login()
        self.fake.kill()
        deadline = time.monotonic() + 3
        while self.fake.link.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        st, _, body = self.call("preflight", cookie=ck)
        self.assertEqual(st, 503)
        self.assertEqual(json.loads(body)["server_error"], "helper_unavailable")
        st, _, body = self.req("GET", "/api/status", cookie=ck)
        self.assertFalse(json.loads(body)["helper_alive"])


def open_sse(tc, cookie, origin="", fetch_site="same-origin"):
    s = socket.create_connection(("127.0.0.1", tc.srv.port), timeout=5)
    h = f"GET /api/events HTTP/1.1\r\nHost: {tc.srv.host_header}\r\nCookie: arltuner_session={cookie}\r\n"
    if origin:
        h += f"Origin: {origin}\r\n"
    if fetch_site:
        h += f"Sec-Fetch-Site: {fetch_site}\r\n"
    s.sendall((h + "\r\n").encode())
    tc.addCleanup(s.close)
    return s


def read_until(s, needle: bytes, limit=5.0) -> bytes:
    buf = b""
    end = time.monotonic() + limit
    while needle not in buf and time.monotonic() < end:
        try:
            d = s.recv(4096)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
    return buf


class TestSse(Base):
    cfg_kw = {"sse_keepalive_s": 0.3, "max_sse_clients": 2}

    def open_sse(self, cookie, **kw):
        return open_sse(self, cookie, **kw)

    @staticmethod
    def read_until(s, needle, limit=5.0):
        return read_until(s, needle, limit)

    def test_stream_events_and_headers(self):
        ck = self.login()
        s = self.open_sse(ck)
        head = self.read_until(s, b"retry: 3000")
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertIn(b"text/event-stream", head)
        self.assertIn(b"X-Frame-Options: DENY", head)
        self.assertIn(b"Cache-Control: no-store", head)
        self.fake.emit({"event": "heartbeat", "op_id": None, "state": "idle"})
        got = self.read_until(s, b'"state":"idle"')
        self.assertIn(b"event: heartbeat\ndata: ", got)
        self.fake.emit({"event": "bad name\nx", "op_id": None})  # invalid event names are never forwarded
        self.fake.emit({"event": "progress", "op_id": "w1", "step": 1, "of": 2, "ops_used": 1, "ops_budget": 600})
        got = self.read_until(s, b'"ops_budget":600')
        self.assertNotIn(b"bad name", got)
        # keepalive comment arrives when idle
        self.assertIn(b": keepalive", self.read_until(s, b": keepalive"))

    def test_latest_state_replayed_and_client_limit(self):
        ck = self.login()
        self.fake.emit({"event": "state", "op_id": None, "state": "capped"})
        deadline = time.monotonic() + 3
        while "state" not in self.fake.link._latest and time.monotonic() < deadline:
            time.sleep(0.02)
        s1 = self.open_sse(ck)
        self.assertIn(b'"state":"capped"', self.read_until(s1, b'"state":"capped"'))
        s2 = self.open_sse(ck)
        self.read_until(s2, b"retry")
        s3 = self.open_sse(ck)
        self.assertIn(b"503", self.read_until(s3, b"too_many_streams").split(b"\r\n")[0])

    def test_oversize_helper_line_does_not_kill_link(self):
        self.fake.raw(b'{"event":"progress","pad":"' + b"a" * 70000 + b'"}\n')
        st, _, _ = self.call("preflight", cookie=self.login())
        self.assertEqual(st, 200)


class TestSlowClient(Base):
    cfg_kw = {"request_timeout_s": 0.6}

    def closed_within(self, s, secs=4.0) -> bool:
        s.settimeout(secs)
        try:
            while True:
                if not s.recv(4096):
                    return True
        except socket.timeout:
            return False
        except ConnectionError:
            return True

    def test_stalled_headers_dropped(self):
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(b"GET / HTTP/1.1\r\nHost: " + self.srv.host_header.encode() + b"\r\nX-Slow: ")
        t0 = time.monotonic()
        self.assertTrue(self.closed_within(s))
        self.assertLess(time.monotonic() - t0, 3.5)

    def test_stalled_body_gets_408(self):
        ck = self.login()
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(f"POST /api/call HTTP/1.1\r\nHost: {self.srv.host_header}\r\nOrigin: {self.srv.origin}\r\nCookie: arltuner_session={ck}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{{".encode())
        data = b""
        s.settimeout(4)
        while True:
            d = s.recv(4096)
            if not d:
                break
            data += d
        self.assertIn(b"408", data.split(b"\r\n")[0])
        self.assertEqual(self.fake.requests, [])

    def test_dribbling_body_hits_total_deadline(self):
        ck = self.login()
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(f"POST /api/call HTTP/1.1\r\nHost: {self.srv.host_header}\r\nOrigin: {self.srv.origin}\r\nCookie: arltuner_session={ck}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n".encode())
        t0 = time.monotonic()
        try:
            for _ in range(20):  # one byte every 0.2 s would take 20 s, each gap is below the per-recv timeout
                s.sendall(b" ")
                time.sleep(0.2)
        except OSError:
            pass
        self.assertLess(time.monotonic() - t0, 3.0)


class TestHelperLink(unittest.TestCase):
    def test_request_too_large_and_timeout_and_eof(self):
        from arltuner.web.helperlink import HelperTimeout, HelperUnavailable, RequestTooLarge
        fake = FakeHelper()
        self.addCleanup(fake.close)
        with self.assertRaises(RequestTooLarge):
            fake.link.call("plan", {"x": "a" * 70000})
        self.assertEqual(fake.link.call("preflight")["result"], {"ok": True})
        # a helper that never answers
        import os
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        link = HelperLink(os.fdopen(r2, "rb"), os.fdopen(w1, "wb", buffering=0)).start()
        self.addCleanup(link.close)
        with self.assertRaises(HelperTimeout):
            link.call("x", timeout=0.2)
        os.close(w2)  # EOF from helper
        self.addCleanup(lambda: None)
        deadline = time.monotonic() + 2
        while link.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        with self.assertRaises(HelperUnavailable):
            link.call("x")
        os.close(r1)


if __name__ == "__main__":
    unittest.main()
