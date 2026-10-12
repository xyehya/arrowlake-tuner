"""G-142 (security review 2026-10-12, D-042): random loopback bind, read-scope sessions, strict JSON, dead ops, duplicate cookie, the socketpair helper
link and undumpable GUI. Loopback only; the helper is a fake."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import arltuner.web.server as server_mod
from arltuner.web import helperlink
from arltuner.web.server import Config, OPS, READ_OPS, WebServer
from tests.web.fake_helper import FakeHelper
from tests.web.test_server_security import Base, JSON_CT, SF, WEB_ROOT, open_sse, read_until

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("screenshots_tool_g142", ROOT / "tools" / "screenshots.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


class TestRandomLoopbackBind(Base):
    def test_binds_a_random_127_xyz_never_127_0_0_1(self):
        m = re.fullmatch(r"127\.(\d+)\.(\d+)\.(\d+)", self.srv.addr)
        self.assertIsNotNone(m, self.srv.addr)
        self.assertTrue(all(1 <= int(g) <= 254 for g in m.groups()), self.srv.addr)  # never 127.0.0.1, never a .0 / .255 octet
        self.assertEqual(self.srv.host_header, f"{self.srv.addr}:{self.srv.port}")
        self.assertEqual(self.srv.origin, f"http://{self.srv.addr}:{self.srv.port}")
        self.assertIsNone(self.srv.bind_note)

    def test_every_run_has_its_own_host_name_so_its_own_cookie_jar(self):
        others = [WebServer(Config(web_root=WEB_ROOT), None) for _ in range(6)]
        for o in others:
            self.addCleanup(o.httpd.server_close)
        self.assertGreater(len({o.addr for o in others} | {self.srv.addr}), 4)  # 254**3 candidates: a collision of several is not plausible

    def test_the_old_fixed_host_is_refused(self):
        ck = self.login()
        for host in (f"127.0.0.1:{self.srv.port}", "127.0.0.1", f"localhost:{self.srv.port}"):
            self.assertEqual(self.req("GET", "/", cookie=ck, host=host)[0], 403, host)
        self.assertEqual(self.req("GET", "/", cookie=ck)[0], 200)

    def test_launcher_form_points_at_the_bound_address(self):
        import tempfile
        from arltuner.web.launcher import write_launcher
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            p = write_launcher(Path(d) / "l.html", self.srv.origin, self.srv.token)
            self.assertIn(f'action="http://{self.srv.addr}:{self.srv.port}/bootstrap"', p.read_text())

    def test_falls_back_to_127_0_0_1_only_when_the_bind_fails_and_says_so(self):
        real = server_mod._HTTPD
        calls = []

        def fake_httpd(addr, handler, n):
            calls.append(addr[0])
            if len(calls) == 1:
                raise OSError(99, "Cannot assign requested address")
            return real(addr, handler, n)

        with mock.patch.object(server_mod, "_HTTPD", fake_httpd):
            srv = WebServer(Config(web_root=WEB_ROOT), None)
        self.addCleanup(srv.httpd.server_close)
        self.assertTrue(calls[0].startswith("127.") and calls[0] != "127.0.0.1")
        self.assertEqual(calls[1], "127.0.0.1")
        self.assertEqual(srv.addr, "127.0.0.1")
        self.assertIn("falling back to 127.0.0.1", srv.bind_note)

    def test_a_pinned_bind_host_is_honoured(self):
        srv = WebServer(Config(web_root=WEB_ROOT, bind_host="127.0.0.1"), None)
        self.addCleanup(srv.httpd.server_close)
        self.assertEqual(srv.host_header, f"127.0.0.1:{srv.port}")


class ReadLogin(Base):
    def login_read(self) -> str:
        body = f"token={self.srv.token}&scope=read".encode()
        st, r, _ = self.req("POST", "/bootstrap", {"Content-Type": "application/x-www-form-urlencoded"}, body, origin="null")
        self.assertEqual(st, 200)
        return r.getheader("Set-Cookie").split(";")[0].split("=", 1)[1]


class TestReadScope(ReadLogin):
    def test_read_ops_are_the_screenshot_tools_allowlist_and_all_short_reads(self):
        self.assertEqual(READ_OPS, tool.READ_OPS)
        self.assertTrue(READ_OPS <= set(OPS))
        self.assertFalse([op for op in READ_OPS if OPS[op][1]], "no long-running op in the read scope")
        for op in ("tune.apply", "vf.measure", "stress.start", "profiles.save", "profiles.accept", "curves.update", "curves.delete", "cancel",
                   "telemetry.stop", "stress.stop", "tune.plan", "tune.read", "vf.dump"):
            self.assertNotIn(op, READ_OPS)

    def test_read_session_may_read_but_never_forwards_anything_else(self):
        ck = self.login_read()
        for op in sorted(READ_OPS):
            args = {"name": "x"} if op == "profiles.get" else {"id": "20260101T000000Z-abcd"} if op == "curves.get" else {}
            st, _, _ = self.call(op, args, cookie=ck)
            self.assertNotEqual(st, 403, op)
        n = len(self.fake.requests)
        for op, args in (("tune.apply", {"plan_id": "a" * 64, "typed": "CONFIRM ABCDEF"}), ("tune.plan", {"intent": {}}), ("vf.measure", {}),
                         ("stress.start", {"kind": "cpu", "duration_s": 60}), ("profiles.save", {"name": "x", "intent": {}}),
                         ("profiles.accept", {"from": "read"}), ("curves.delete", {"id": "20260101T000000Z-abcd"}), ("cancel", {}),
                         ("telemetry.stop", {}), ("stress.stop", {}), ("vf.dump", {})):
            st, _, body = self.call(op, args, cookie=ck)
            self.assertEqual(st, 403, op)
            self.assertEqual(json.loads(body)["server_error"], "read_only_session")
        self.assertEqual(len(self.fake.requests), n, "nothing outside the read set reached the helper")

    def test_unknown_ops_still_400_in_a_read_session(self):
        self.assertEqual(self.call("nope", cookie=self.login_read())[0], 400)

    def test_read_session_gets_pages_and_events(self):
        ck = self.login_read()
        self.assertEqual(self.req("GET", "/", cookie=ck)[0], 200)
        self.assertEqual(self.req("GET", "/api/status", cookie=ck)[0], 200)
        s = open_sse(self, ck)
        self.assertIn(b"200", read_until(s, b"\r\n").split(b"\r\n")[0])

    def test_default_and_explicit_full_sessions_are_unrestricted(self):
        for ck in (self.login(),):
            self.assertNotEqual(self.call("vf.measure", {}, cookie=ck)[0], 403)
        srv2 = WebServer(Config(web_root=WEB_ROOT), self.fake.link)
        self.addCleanup(srv2.httpd.server_close)
        sid = srv2.swap_token(srv2.token, "full")
        self.assertEqual(srv2.session_scope(sid), "full")

    def test_bad_scope_is_refused_without_burning_the_token(self):
        ct = {"Content-Type": "application/x-www-form-urlencoded"}
        for extra in (b"&scope=admin", b"&scope=", b"&scope=read&scope=full", b"&scope=READ"):
            st, _, _ = self.req("POST", "/bootstrap", ct, f"token={self.srv.token}".encode() + extra, origin="null")
            self.assertEqual(st, 400, extra)
        self.assertEqual(self.req("POST", "/bootstrap", ct, f"token={self.srv.token}&scope=read".encode(), origin="null")[0], 200)

    def test_screenshot_tool_logs_in_with_read_scope(self):
        name, value = tool.bootstrap(self.srv.origin, self.srv.token)
        self.assertEqual(name, server_mod.COOKIE_NAME)
        self.assertEqual(self.srv.session_scope(value), "read")
        self.assertEqual(self.call("tune.apply", {"plan_id": "a" * 64, "typed": "CONFIRM ABCDEF"}, cookie=value)[0], 403)
        self.assertEqual(self.call("preflight", cookie=value)[0], 200)

    def test_screenshot_tool_accepts_only_loopback_127_8_origins(self):
        import html
        import tempfile
        from arltuner.web.launcher import write_launcher
        ok = ("http://127.0.0.1:5123", "http://127.17.200.3:80", "http://127.254.254.254:65535")
        bad = ("http://128.1.1.1:5123", "http://127.1.1:5123", "http://127.300.1.1:5123", "http://10.0.0.1:5123", "http://localhost:5123",
               "http://127.1.2.3", "http://127.1.2.3:0", "http://127.1.2.3:70000", "https://127.1.2.3:5123", "http://127.1.2.3:5123.evil.example",
               "http://0.0.0.0:5123")
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            for origin in ok:
                self.assertEqual(tool.read_launcher(write_launcher(Path(d) / "l.html", origin, "t")), (origin, "t"), origin)
            for origin in bad:
                p = Path(d) / "l.html"
                p.unlink(missing_ok=True)
                fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
                os.write(fd, f'<form method="post" action="{html.escape(origin)}/bootstrap"><input type="hidden" name="token" value="t">'.encode())
                os.close(fd)
                with self.assertRaises(tool.ToolError, msg=origin):
                    tool.read_launcher(p)


class TestScopeFailsClosed(ReadLogin):
    """Orchestrator note: every authorization decision fails closed (unknown / garbled scope, later-added ops, a scope the client picks, SSE, errors)."""

    def test_unknown_or_garbled_scopes_may_call_nothing(self):
        ck = self.login()
        sid = self.srv.valid_session(ck, touch=False)
        for garbled in (None, "", "FULL", "Full", "admin", "read ", "full\n", 5, True, ["full"], {"full": 1}):
            with self.srv._lock:
                self.srv._sessions[sid][2] = garbled
            for op in ("preflight", "tune.apply", "vf.measure"):
                self.assertFalse(self.srv.may_call(sid, op), (garbled, op))
                st, _, body = self.call(op, {}, cookie=ck)
                self.assertEqual((st, json.loads(body)["server_error"]), (403, "read_only_session"), (garbled, op))
        with self.srv._lock:
            self.srv._sessions[sid][2] = "full"
        self.assertTrue(self.srv.may_call(sid, "vf.measure"))

    def test_a_session_record_without_a_scope_and_an_unknown_session_are_refused(self):
        ck = self.login()
        sid = self.srv.valid_session(ck, touch=False)
        with self.srv._lock:
            del self.srv._sessions[sid][2:]
        self.assertFalse(self.srv.may_call(sid, "preflight"))
        self.assertEqual(self.call("preflight", cookie=ck)[0], 403)
        self.assertFalse(self.srv.may_call("no-such-session", "preflight"))
        s = open_sse(self, ck)   # the stream of such a record is the restricted one, not a dropped connection
        self.assertIn(b"200", read_until(s, b"\r\n").split(b"\r\n")[0])
        self.fake.progress("w98", 1, 2, 5)
        self.fake.telemetry()
        got = read_until(s, b"event: telemetry")
        self.assertNotIn(b"event: progress", got)

    def test_an_error_in_the_scope_check_refuses(self):
        ck = self.login()
        with mock.patch.object(WebServer, "session_scope", side_effect=RuntimeError("boom")):
            self.assertFalse(self.srv.may_call("x", "preflight"))
            self.assertEqual(self.call("preflight", cookie=ck)[0], 403)
        self.assertEqual(self.fake.requests, [])

    def test_swap_token_refuses_an_unknown_scope_without_burning_the_token(self):
        for bad in ("", "FULL", "admin", None, 0):
            self.assertIsNone(self.srv.swap_token(self.srv.token, bad), bad)
        self.assertIsNotNone(self.srv.swap_token(self.srv.token, "read"))

    def test_the_client_cannot_pick_or_raise_its_scope_after_bootstrap(self):
        ck = self.login_read()
        name = server_mod.COOKIE_NAME
        body = json.dumps({"op": "vf.measure", "args": {}}).encode()
        for extra in ({"X-Scope": "full"}, {"X-Arltuner-Scope": "full"}, {"Cookie": f"{name}={ck}; scope=full"}, {"Authorization": "full"}):
            h = {"Content-Type": JSON_CT, **extra}
            st, _, _ = self.req("POST", "/api/call", h, body, cookie=None if "Cookie" in extra else ck, origin=self.srv.origin)
            self.assertEqual(st, 403, extra)
        st, _, _ = self.req("POST", "/api/call", {"Content-Type": JSON_CT}, json.dumps({"op": "vf.measure", "args": {"scope": "full"}}).encode(),
                            cookie=ck, origin=self.srv.origin)
        self.assertEqual(st, 403)
        # the token is burned: a second bootstrap cannot mint a full session
        st, _, _ = self.req("POST", "/bootstrap", {"Content-Type": "application/x-www-form-urlencoded"}, f"token={self.srv.token}&scope=full".encode(),
                            origin="null")
        self.assertEqual(st, 403)
        self.assertEqual(self.srv.session_scope(self.srv.valid_session(ck, touch=False)), "read")

    def test_read_ops_is_an_explicit_frozen_allowlist_and_a_later_added_op_is_refused(self):
        self.assertIsInstance(READ_OPS, frozenset)
        ck = self.login_read()
        sid = self.srv.valid_session(ck, touch=False)
        with mock.patch.dict(OPS, {"future.op": (lambda a: a == {}, False)}):
            self.assertFalse(self.srv.may_call(sid, "future.op"))
            self.assertEqual(self.call("future.op", cookie=ck)[0], 403)
        self.assertEqual(self.fake.requests, [])

    def test_a_read_stream_carries_only_the_dashboard_feeds_and_none_of_the_results(self):
        read_ck = self.login_read()
        self.srv.new_bootstrap()   # a fresh launcher token for the full session (existing sessions are untouched)
        full_ck = self.login()
        rs, fs = open_sse(self, read_ck), open_sse(self, full_ck)
        read_until(rs, b"retry: 3000")
        read_until(fs, b"retry: 3000")
        st, _, _ = self.call("vf.dump", {}, cookie=full_ck)   # a long op of the full session: its result belongs to that session only
        self.assertEqual(st, 202)
        got_full = read_until(fs, b"event: result")
        self.fake.progress("w99", 1, 2, 5)
        self.fake.emit({"event": "vf.point", "op_id": "w99", "cpu": 0})
        self.fake.emit({"event": "future.event", "x": 1})
        self.fake.heartbeat("idle")
        self.fake.telemetry()
        got_full += read_until(fs, b"event: telemetry")
        got_read = read_until(rs, b"event: telemetry")
        for banned in (b"event: progress", b"event: vf.point", b"event: result", b"event: future.event"):
            self.assertNotIn(banned, got_read, banned)
        self.assertIn(b"event: heartbeat", got_read)
        self.assertIn(b"event: progress", got_full)   # the full session still sees everything
        self.assertIn(b"event: result", got_full)


class TestStrictJsonAndOps(Base):
    def post(self, raw: bytes, ck: str):
        return self.req("POST", "/api/call", {"Content-Type": JSON_CT}, raw, cookie=ck, origin=self.srv.origin)

    def test_nan_and_infinity_are_refused_with_400_and_never_forwarded(self):
        ck = self.login()
        for const in (b"NaN", b"Infinity", b"-Infinity"):
            st, _, body = self.post(b'{"op":"preflight","args":{"x":' + const + b"}}", ck)
            self.assertEqual((st, json.loads(body)["server_error"]), (400, "bad_json"), const)
            st, _, _ = self.post(b'{"op":"telemetry.start","args":{"interval_s":' + const + b"}}", ck)
            self.assertEqual(st, 400, const)
        self.assertEqual(self.post(b'{"op":"telemetry.start","args":{"interval_s":1.5}}', ck)[0], 200)
        self.assertEqual(self.fake.requests[-1]["args"], {"interval_s": 1.5})
        self.assertEqual(len(self.fake.requests), 1)

    def test_deeply_nested_json_is_a_400_not_a_dropped_connection(self):
        ck = self.login()
        for deep in (b"[" * 9000 + b"]" * 9000, b'{"a":' * 3000 + b"1" + b"}" * 3000):  # RecursionError on older Pythons, schema refusal on newer
            st, _, body = self.post(b'{"op":"preflight","args":' + deep + b"}", ck)
            self.assertEqual((st, json.loads(body)["ok"]), (400, False))
            st, _, body = self.post(b'{"op":"tune.plan","args":{"intent":' + deep + b"}}", ck)
            self.assertIn(st, (200, 400, 409, 422, 413))  # an answer of some kind, never a dropped connection
            json.loads(body)
        self.assertEqual(self.post(b'{"op":"preflight","args":{}}', ck)[0], 200)  # the server is still answering

    def test_helper_request_serializer_refuses_nan(self):
        for bad in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ValueError):
                self.fake.link.submit("preflight", {"x": bad}, lambda *_: None)
        self.fake.link.submit("preflight", {"x": 1.5}, lambda *_: None)

    def test_dead_allowlist_entries_are_gone(self):
        for op in ("plan", "apply", "restore"):
            self.assertNotIn(op, OPS)
        ck = self.login()
        for op in ("plan", "apply", "restore"):
            st, _, body = self.call(op, {}, cookie=ck)
            self.assertEqual((st, json.loads(body)["server_error"]), (400, "unknown_op"), op)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("tune.apply", OPS)
        self.assertIn("tune.plan", OPS)


class TestDuplicateCookie(Base):
    def get(self, cookie_header: str):
        return self.req("GET", "/api/status", {"Cookie": cookie_header})

    def test_two_cookies_of_our_name_are_refused(self):
        ck = self.login()
        name = server_mod.COOKIE_NAME
        self.assertEqual(self.get(f"{name}={ck}")[0], 200)
        self.assertEqual(self.get(f"other=1; {name}={ck}; x=2")[0], 200)  # unrelated cookies are fine
        for hdr in (f"{name}={ck}; {name}={ck}", f"{name}=bad; {name}={ck}", f"{name}={ck}; {name}=bad", f"{name}={ck};{name}={ck}"):
            self.assertEqual(self.get(hdr)[0], 401, hdr)

    def test_duplicate_is_a_plain_401_not_a_counted_failure(self):
        ck = self.login()
        name = server_mod.COOKIE_NAME
        for _ in range(self.srv.cfg.fail_limit + 3):
            self.assertEqual(self.get(f"{name}={ck}; {name}={ck}")[0], 401)
        self.assertEqual(self.get(f"{name}={ck}")[0], 200)


class TestSocketpairHelperLink(unittest.TestCase):
    ECHO = (
        "import json, os, stat, sys\n"
        "for line in sys.stdin.buffer:\n"
        "    r = json.loads(line)\n"
        "    kinds = [stat.S_ISSOCK(os.fstat(fd).st_mode) for fd in (0, 1)]\n"
        "    print(json.dumps({'id': r['id'], 'ok': True, 'result': {'sock': kinds, 'echo': r['args']}}), flush=True)\n"
    )

    def test_helper_gets_a_socket_on_fd_0_and_1_and_the_link_works(self):
        proc, link = helperlink.spawn_helper([sys.executable, "-I", "-c", self.ECHO])
        self.addCleanup(proc.wait)
        self.addCleanup(link.close)
        resp = link.call("x", {"a": 1}, timeout=10)
        self.assertEqual(resp["result"], {"sock": [True, True], "echo": {"a": 1}})
        self.assertIsNone(proc.stdin)   # no pipe objects exist
        self.assertIsNone(proc.stdout)

    def test_no_pipe_to_reopen_through_proc(self):
        proc, link = helperlink.spawn_helper([sys.executable, "-I", "-c", self.ECHO])
        self.addCleanup(proc.wait)
        self.addCleanup(link.close)
        link.call("x", {}, timeout=10)
        for fd in (0, 1):
            target = os.readlink(f"/proc/{proc.pid}/fd/{fd}")
            self.assertTrue(target.startswith("socket:"), target)
            with self.assertRaises(OSError):   # a socket cannot be opened through /proc (ENXIO); a pipe could
                os.open(f"/proc/{proc.pid}/fd/{fd}", os.O_RDWR)

    def test_closing_the_link_half_closes_so_the_helper_sees_eof_and_exits(self):
        proc, link = helperlink.spawn_helper([sys.executable, "-I", "-c", self.ECHO])
        link.call("x", {}, timeout=10)
        link.close()
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_helper_exit_marks_the_link_dead(self):
        proc, link = helperlink.spawn_helper([sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.readline()"])
        self.addCleanup(link.close)
        link.submit("x", {}, lambda *_: None)
        proc.wait(timeout=10)
        end = __import__("time").monotonic() + 5
        while link.alive and __import__("time").monotonic() < end:
            __import__("time").sleep(0.02)
        self.assertFalse(link.alive)

    def test_a_missing_helper_command_raises_and_leaks_nothing(self):
        with self.assertRaises(OSError):
            helperlink.spawn_helper(["/nonexistent/arltuner-helper"])


class TestUndumpable(unittest.TestCase):
    def test_prctl_dumpable_is_cleared_in_a_child_process_and_children_still_spawn(self):
        code = (
            "import ctypes, subprocess, sys\n"
            "sys.path.insert(0, %r)\n"
            "from arltuner.web.helperlink import set_undumpable, spawn_helper\n"
            "assert set_undumpable()\n"
            "assert ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) == 0   # PR_GET_DUMPABLE\n"
            "proc, link = spawn_helper([sys.executable, '-I', '-c', 'import sys; print(sys.stdin.readline().strip() and 1, flush=True)'])\n"
            "link.close(); proc.wait(10)\n"
            "print('ok')\n" % str(ROOT / "src")
        )
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)

    def test_gui_script_wires_both(self):
        text = (ROOT / "tools" / "arltuner-gui.py").read_text()
        self.assertIn("set_undumpable()", text)
        self.assertIn("spawn_helper(helper_cmd)", text)
        self.assertNotIn("subprocess.PIPE", text)


if __name__ == "__main__":
    unittest.main()
