"""Regression tests for the G-103 security review (items 1-12). Loopback ephemeral ports only; fake helper speaks Contract 8."""
from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from arltuner.web import launcher
from arltuner.web.helperlink import MAX_LINE, HelperLink, HelperTimeout, HelperUnavailable
from arltuner.web.server import Config, WebServer, public_response
from tests.web.fake_helper import FakeHelper
from tests.web.test_server_security import SF, Base, open_sse, read_until


def results_from(buf: bytes) -> dict:
    out = {}
    for blk in buf.split(b"\n\n"):
        if blk.startswith(b"event: result"):
            d = json.loads(blk.split(b"data: ", 1)[1])
            out[d["id"]] = d
    return out


class TestAuthLockout(Base):  # item 1
    cfg_kw = {"fail_limit": 5}

    def test_anonymous_flood_does_not_lock_out_valid_session(self):
        ck = self.login()
        for _ in range(30):
            self.assertEqual(self.req("GET", "/")[0], 401)  # no cookie at all: refused, not counted
        self.assertEqual(self.req("GET", "/", cookie=ck)[0], 200)
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)

    def test_wrong_credential_flood_blocks_guessers_but_not_valid_session(self):
        ck = self.login()
        for _ in range(5):
            self.assertEqual(self.req("GET", "/", cookie="B" * 43)[0], 401)
        self.assertEqual(self.req("GET", "/", cookie="B" * 43)[0], 429)
        self.assertEqual(self.bootstrap("nope")[0], 429)
        self.assertEqual(self.req("GET", "/", cookie=ck)[0], 200)  # the valid session is never rate-limited
        self.assertEqual(self.call("preflight", cookie=ck)[0], 200)


class TestSseFetchMetadata(Base):  # item 2
    def test_same_origin_without_origin_header_ok(self):
        s = open_sse(self, self.login())
        self.assertIn(b"200", read_until(s, b"retry: 3000").split(b"\r\n")[0])

    def test_cross_site_or_absent_fetch_site_refused(self):
        ck = self.login()
        for fs in ("cross-site", "same-site", "none", "", None):
            s = open_sse(self, ck, fetch_site=fs)
            self.assertIn(b"403", read_until(s, b"\r\n").split(b"\r\n")[0], fs)

    def test_wrong_origin_refused_even_with_same_origin_fetch_site(self):
        ck = self.login()
        s = open_sse(self, ck, origin="http://evil.example")
        self.assertIn(b"403", read_until(s, b"\r\n").split(b"\r\n")[0])
        s = open_sse(self, ck, origin=self.srv.origin)
        self.assertIn(b"200", read_until(s, b"\r\n").split(b"\r\n")[0])

    def test_cookie_still_required(self):
        s = open_sse(self, "A" * 43)
        self.assertIn(b"401", read_until(s, b"\r\n").split(b"\r\n")[0])


class TestOpsAllowlist(Base):  # item 3
    def test_unknown_ops_and_bad_args_never_forwarded(self):
        ck = self.login()
        good_id = "a" * 64
        bad = [
            ("stress.start", {}), ("freq.cap", {}), ("nope", {}), ("preflight", {"x": 1}), ("telemetry.start", {"hz": 1}),
            ("audit.tail", {"n": 0}), ("audit.tail", {"n": "5"}), ("audit.tail", {"n": True}), ("audit.tail", {"m": 1}),
            ("cancel", {"op_id": "has space"}), ("cancel", {"op_id": 5}), ("cancel", {"x": 1}),
            ("plan", {}), ("plan", {"profile": "x"}), ("plan", {"profile": {}, "x": 1}),
            ("apply", {}), ("apply", {"plan_id": good_id}), ("apply", {"plan_id": good_id, "typed": "CONFIRM abcdef"}),
            ("apply", {"plan_id": good_id, "typed": "CONFIRM ABCDE1"}), ("apply", {"plan_id": good_id, "typed": "CONFIRM ABCDEFG"}),
            ("apply", {"plan_id": good_id, "typed": "confirm ABCDEF"}), ("apply", {"plan_id": good_id.upper(), "typed": "CONFIRM ABCDEF"}),
            ("apply", {"plan_id": "ab", "typed": "CONFIRM ABCDEF"}), ("apply", {"plan_id": good_id, "typed": "CONFIRM ABCDEF", "x": 1}),
            ("restore", {"x": 1}), ("vf.dump", {"cpus": [0]}),
        ]
        for op, args in bad:
            st, _, _ = self.call(op, args, cookie=ck)
            self.assertEqual(st, 400, (op, args))
        self.assertEqual(self.fake.requests, [])

    def test_g1_ops_forwarded(self):
        ck = self.login()
        for op, args in (("preflight", {}), ("audit.tail", {"n": 10}), ("telemetry.start", {}), ("telemetry.stop", {}), ("cancel", {"op_id": "w1"}),
                         ("plan", {"profile": {}})):
            self.call(op, args, cookie=ck)
        self.assertEqual([r["op"] for r in self.fake.requests],
                         ["preflight", "audit.tail", "telemetry.start", "telemetry.stop", "cancel", "plan"])


class TestLongOps(Base):  # item 4
    cfg_kw = {"max_pending_long": 2}

    def test_202_then_sse_result_and_cap(self):
        ck = self.login()
        sse = open_sse(self, ck)
        read_until(sse, b"retry: 3000")
        self.fake.hold.clear()
        t0 = time.monotonic()
        st, _, b1 = self.call("vf.dump", {}, cookie=ck)
        self.assertEqual(st, 202)
        self.assertLess(time.monotonic() - t0, 1.0)  # answered at once although the helper is still working
        id1 = json.loads(b1)["id"]
        st, _, b2 = self.call("vf.dump", {}, cookie=ck)
        self.assertEqual(st, 202)
        st, _, b3 = self.call("vf.dump", {}, cookie=ck)
        self.assertEqual(st, 429)
        self.assertEqual(json.loads(b3)["server_error"], "too_many_pending")
        self.fake.hold.set()
        id2 = json.loads(b2)["id"]
        buf = b""
        end = time.monotonic() + 5
        while not {id1, id2} <= set(results_from(buf)) and time.monotonic() < end:
            buf += sse.recv(4096)
        got = results_from(buf)
        self.assertTrue(got[id1]["ok"])
        self.assertEqual(got[id1]["result"], {"cpus": []})
        deadline = time.monotonic() + 3
        while self.srv.pending_long and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.call("restore", {}, cookie=ck)[0], 202)  # slot freed

    def test_result_replayed_to_late_subscriber(self):
        ck = self.login()
        st, _, b = self.call("vf.dump", {}, cookie=ck)
        rid = json.loads(b)["id"]
        time.sleep(0.3)
        sse = open_sse(self, ck)
        self.assertIn(rid, results_from(read_until(sse, rid.encode() + b'","ok"')))

    def test_helper_death_reported_as_result_not_hang(self):
        ck = self.login()
        sse = open_sse(self, ck)
        self.fake.hold.clear()
        rid = json.loads(self.call("vf.dump", {}, cookie=ck)[2])["id"]
        self.fake.link.close()
        got = results_from(read_until(sse, b"helper_unavailable"))
        self.assertEqual(got[rid]["server_error"], "helper_unavailable")
        self.fake.hold.set()


class TestSlowloris(unittest.TestCase):  # item 5
    def make(self, **kw):
        fake = FakeHelper()
        srv = WebServer(Config(web_root=Path(__file__).resolve().parents[2] / "web", **kw), fake.link).start()
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        return srv

    def test_header_dribble_hits_total_deadline(self):
        srv = self.make(header_deadline_s=0.6, request_timeout_s=30)
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        self.addCleanup(s.close)
        t0 = time.monotonic()
        try:
            s.sendall(b"GET / HTTP/1.1\r\n")
            for _ in range(40):  # a header byte every 0.15 s never trips a per-recv timeout of 30 s
                s.sendall(b"X")
                time.sleep(0.15)
        except OSError:
            pass
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_connection_cap(self):
        srv = self.make(max_connections=3, request_timeout_s=30, header_deadline_s=30)
        held = [socket.create_connection(("127.0.0.1", srv.port), timeout=5) for _ in range(3)]
        for h in held:
            self.addCleanup(h.close)
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        self.addCleanup(extra.close)
        extra.settimeout(2)
        try:
            data = extra.recv(10)  # closed immediately by the server
        except ConnectionError:
            data = b""
        self.assertEqual(data, b"")
        held[0].close()
        time.sleep(0.4)
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(b"GET / HTTP/1.1\r\nHost: " + srv.host_header.encode() + b"\r\n\r\n")
        s.settimeout(3)
        self.assertIn(b"401", s.recv(100))  # a freed slot serves again


class TestSessions(Base):  # item 6
    def setUp(self):
        self.now = [1000.0]
        self.cfg_kw = {"clock": lambda: self.now[0], "session_idle_s": 100, "session_max_s": 500}
        super().setUp()

    def test_idle_expiry_and_activity_extends(self):
        ck = self.login()
        for _ in range(4):
            self.now[0] += 60
            self.assertEqual(self.req("GET", "/", cookie=ck)[0], 200)  # activity keeps it alive beyond 100 s total
        self.now[0] += 101
        self.assertEqual(self.req("GET", "/", cookie=ck)[0], 401)

    def test_absolute_expiry(self):
        ck = self.login()
        for _ in range(6):
            self.now[0] += 90
            st = self.req("GET", "/", cookie=ck)[0]
        self.assertEqual(st, 401)  # 540 s > 500 s absolute although never idle

    def test_logout_invalidates_and_requires_origin(self):
        ck = self.login()
        self.assertEqual(self.req("POST", "/api/logout", cookie=ck, body=b"")[0], 403)  # Origin required
        self.assertEqual(self.req("GET", "/api/logout", cookie=ck)[0], 405)
        st, r, _ = self.req("POST", "/api/logout", cookie=ck, origin=self.srv.origin, body=b"")
        self.assertEqual(st, 200)
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        self.assertEqual(self.req("GET", "/", cookie=ck)[0], 401)

    def test_logout_ends_open_stream(self):
        ck = self.login()
        sse = open_sse(self, ck)
        read_until(sse, b"retry: 3000")
        self.req("POST", "/api/logout", cookie=ck, origin=self.srv.origin, body=b"")
        sse.settimeout(3)
        self.assertEqual(sse.recv(100), b"")

    def test_cookie_rotated_only_at_bootstrap(self):
        ck = self.login()
        for path in ("/", "/app.js", "/api/status"):
            self.assertIsNone(self.req("GET", path, cookie=ck)[1].getheader("Set-Cookie"))
        self.assertIsNone(self.call("preflight", cookie=ck)[1].getheader("Set-Cookie"))
        self.assertEqual(self.bootstrap()[0], 403)  # token is single-use, so there is no second cookie

    def test_residual_risk_documented(self):
        import arltuner.web.server as m
        self.assertIn("not port-isolated", m.__doc__)


class TestLauncherDir(unittest.TestCase):  # item 7
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def test_created_0700_no_parents(self):
        p = self.d / "arl"
        launcher.make_private_dir(p)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700)
        with self.assertRaises(launcher.LauncherDirError):
            launcher.make_private_dir(self.d / "a" / "b")  # no parents are created
        self.assertFalse((self.d / "a").exists())

    def test_symlink_refused(self):
        real = self.d / "real"
        real.mkdir(mode=0o700)
        (self.d / "link").symlink_to(real)
        with self.assertRaises(launcher.LauncherDirError):
            launcher.make_private_dir(self.d / "link")
        with self.assertRaises(launcher.LauncherDirError):
            launcher.write_launcher(self.d / "link" / "l.html", "http://127.0.0.1:1", "t")
        self.assertFalse((real / "l.html").exists())

    def test_group_or_world_bits_refused(self):
        for mode in (0o755, 0o710, 0o707, 0o770):
            p = self.d / f"m{mode:o}"
            p.mkdir()
            os.chmod(p, mode)
            with self.assertRaises(launcher.LauncherDirError, msg=oct(mode)):
                launcher.make_private_dir(p)

    def test_foreign_owner_refused(self):
        p = self.d / "x"
        launcher.make_private_dir(p)
        with mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(launcher.LauncherDirError):
                launcher.check_private_dir(p)

    def test_file_instead_of_dir_refused(self):
        (self.d / "f").write_text("x")
        with self.assertRaises(launcher.LauncherDirError):
            launcher.check_private_dir(self.d / "f")

    def test_gui_refuses_insecure_xdg_runtime_dir(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("arltuner_gui", Path(__file__).resolve().parents[2] / "tools" / "arltuner-gui.py")
        gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gui)
        bad = self.d / "xdg"
        bad.mkdir()
        os.chmod(bad, 0o755)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(bad)}):
            with mock.patch("sys.stderr"):
                self.assertEqual(gui.main([]), 2)
        self.assertFalse((bad / "arltuner").exists())


class TestWriterNeverBlocks(unittest.TestCase):  # item 8
    def test_stuck_helper_does_not_hang_callers(self):
        gate = threading.Event()

        class Stuck:
            def write(self, b):
                gate.wait(30)

            def flush(self):
                pass

            def close(self):
                pass

        r, w = os.pipe()
        self.addCleanup(os.close, w)
        link = HelperLink(os.fdopen(r, "rb"), Stuck()).start()  # type: ignore[arg-type]
        self.addCleanup(gate.set)
        t0 = time.monotonic()
        with self.assertRaises(HelperTimeout):
            link.call("x", timeout=0.3)
        saw_unavailable = False
        for _ in range(100):  # the queue is bounded: eventually callers are refused instantly
            try:
                link.submit("x", {}, lambda *_: None)
            except HelperUnavailable:
                saw_unavailable = True
                break
        self.assertTrue(saw_unavailable)
        self.assertLess(time.monotonic() - t0, 3.0)
        with self.assertRaises(HelperUnavailable):
            link.call("x", timeout=5)


class TestErrorPathsHeaders(Base):  # item 9
    def raw(self, data: bytes) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(data)
        buf = b""
        while True:
            try:
                d = s.recv(4096)
            except OSError:
                break
            if not d:
                break
            buf += d
        return buf

    def check(self, resp: bytes, status: bytes):
        self.assertIn(status, resp.split(b"\r\n")[0])
        low = resp.lower()
        for h in (b"content-security-policy:", b"x-frame-options: deny", b"x-content-type-options: nosniff", b"cache-control: no-store",
                  b"referrer-policy: no-referrer", b"content-type: application/json"):
            self.assertIn(h, low)
        json.loads(resp.split(b"\r\n\r\n", 1)[1])

    def test_malformed_request_line(self):
        self.check(self.raw(b"GARBAGE\r\n\r\n"), b"400")
        self.check(self.raw(b"GET / HTTP/9.9\r\n\r\n"), b"505")

    def test_long_uri_414(self):
        self.check(self.raw(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n"), b"414")

    def test_too_many_headers_431(self):
        h = b"".join(b"X-%d: y\r\n" % i for i in range(150))
        self.check(self.raw(b"GET / HTTP/1.1\r\nHost: x\r\n" + h + b"\r\n"), b"431")

    def test_unsupported_method_no_body_leak(self):
        self.check(self.raw(b"BREW / HTTP/1.1\r\nHost: " + self.srv.host_header.encode() + b"\r\n\r\n"), b"501")


class TestLineLimits(unittest.TestCase):  # items 10, 11
    def test_boundary_64kib_including_newline(self):
        fake = FakeHelper()
        self.addCleanup(fake.close)
        sub = fake.link.subscribe()
        base = b'{"event":"progress","pad":""}\n'

        def line(total: int) -> bytes:
            return base[:-3] + b"a" * (total - len(base)) + b'"}\n'

        ok, too_long = line(MAX_LINE), line(MAX_LINE + 1)
        self.assertEqual((len(ok), len(too_long)), (MAX_LINE, MAX_LINE + 1))
        fake.raw(too_long)
        fake.raw(ok)
        fake.raw(b'{"event":"state","state":"idle"}\n')
        evs = []
        while True:
            ev = sub.queue.get(timeout=3)
            evs.append(ev)
            if ev.get("event") == "state":
                break
        progress = [e for e in evs if e.get("event") == "progress"]
        self.assertEqual(len(progress), 1)  # exactly the 65536-byte line got through; the 65537-byte one was dropped
        self.assertEqual(len(progress[0]["pad"]), MAX_LINE - len(base))
        self.assertEqual(fake.link.call("preflight")["ok"], True)

    def test_odd_ids_and_shapes_do_not_kill_reader_or_hang_caller(self):
        fake = FakeHelper()
        self.addCleanup(fake.close)
        for junk in (b'{"id": 5, "ok": true}\n', b'{"id": null, "ok": false, "error": {"code": "BAD_REQUEST", "message": "x"}}\n',
                     b'{"id": {"a": 1}, "ok": true}\n', b'{"id": ["x"], "ok": true}\n', b'[1,2]\n', b'"str"\n', b'null\n', b'\xff\xfe\n',
                     b'{"event": 5}\n', b'{"event": ["x"]}\n', b'{"id": "never-asked", "ok": true}\n', b'{}\n', b'\n'):
            fake.raw(junk)
        self.assertEqual(fake.link.call("preflight", timeout=3)["result"], {"ok": True})
        self.assertTrue(fake.link.alive)
        # a caller whose own response never comes still gets its own timeout, not a hang
        r, w = os.pipe()
        r2, w2 = os.pipe()
        link = HelperLink(os.fdopen(r2, "rb"), os.fdopen(w, "wb", buffering=0)).start()
        self.addCleanup(link.close)
        self.addCleanup(os.close, w2)
        self.addCleanup(os.close, r)
        os.write(w2, b'{"id": 99, "ok": true}\n')
        with self.assertRaises(HelperTimeout):
            link.call("x", timeout=0.3)


class TestPublicResponse(unittest.TestCase):  # item 12
    def test_detail_allowlist_and_fields(self):
        resp = {"id": "w1", "ok": False, "extra": 1, "error": {"code": "REFUSED", "message": "no", "detail": {
            "check": "floor", "state": "idle", "path": "/root/x", "trace": "tb", "nested": {"a": 1}}, "stack": "tb"}}
        out = public_response(resp)
        self.assertEqual(out, {"id": "w1", "ok": False, "error": {"code": "REFUSED", "message": "no", "detail": {"check": "floor", "state": "idle"}}})
        self.assertNotIn("detail", public_response({"id": "w", "ok": False, "error": {"code": "INTERNAL", "message": "m", "detail": {"x": 1}}})["error"])
        self.assertEqual(public_response({"id": "w", "ok": True, "result": {"a": 1}, "secret": 1}), {"id": "w", "ok": True, "result": {"a": 1}})
        long = public_response({"id": "w", "ok": False, "error": {"code": "INTERNAL", "message": "m" * 5000}})
        self.assertEqual(len(long["error"]["message"]), 500)
        self.assertEqual(public_response({"id": "w", "ok": False, "error": "weird"})["error"]["code"], "INTERNAL")


if __name__ == "__main__":
    unittest.main()
