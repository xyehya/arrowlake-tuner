"""G-136 / G-136b: the README screenshot tool (``tools/screenshots.py``): the DevTools pipe transport, launcher trust checks, the click and API
allowlists (offline), and a browser smoke against the simulator GUI (opt-in: ``ARLTUNER_BROWSER_TESTS=1`` and a Chrome binary). No hardware, no
real helper."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("screenshots_tool", ROOT / "tools" / "screenshots.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
BROWSER = os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and bool(CHROME)

FAKE_CHROME = r"""#!/usr/bin/env python3
# stands in for Chrome: speaks the --remote-debugging-pipe framing on fds 3 (in) / 4 (out) and reports what it was started with
import json, os, sys
buf = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, _, buf = buf.partition(b"\0")
        m = json.loads(raw)
        extra = []
        for x in os.listdir("/proc/self/fd"):
            try:
                if int(x) > 4 and os.readlink("/proc/self/fd/" + x).startswith("pipe:"):
                    extra.append(int(x))
            except OSError:   # the descriptor listdir itself used is already gone
                pass
        m["result"] = {"argv": sys.argv[1:], "high_fds": extra}
        os.write(4, json.dumps(m).encode() + b"\0")
"""


class TestPipeTransport(unittest.TestCase):
    def test_framing_split_and_joined_messages_and_timeout(self):
        r, w = os.pipe()
        r2, w2 = os.pipe()
        t = tool.PipeTransport(r, w2)   # we read what "Chrome" writes on r; we write to w2
        os.write(w, b'{"a":1}\0{"b"')   # one whole message and half of the next
        self.assertEqual(t.recv_text(1), '{"a":1}')
        self.assertIsNone(t.recv_text(0.05))   # the half message is not delivered; a timeout, not an error
        os.write(w, b':2}\0')
        self.assertEqual(t.recv_text(1), '{"b":2}')
        t.send_text("hello é")
        self.assertEqual(os.read(r2, 100), "hello é".encode() + b"\0")   # NUL-terminated frames go out
        os.close(w)
        with self.assertRaises(tool.ToolError):   # Chrome gone: EOF is an error
            t.recv_text(1)
        os.close(r2)
        t.close()

    def test_spawn_puts_the_pipes_on_fds_3_and_4_and_nothing_else_leaks(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        fake = d / "fakechrome"
        fake.write_text(FAKE_CHROME)
        fake.chmod(0o755)
        proc, t = tool.spawn_chrome_pipe([str(fake), "--remote-debugging-pipe", "x"])
        self.addCleanup(proc.kill)
        self.addCleanup(t.close)
        t.send_text(json.dumps({"id": 7, "method": "Ping"}))
        reply = json.loads(t.recv_text(10))
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["result"]["argv"], ["--remote-debugging-pipe", "x"])
        self.assertEqual(reply["result"]["high_fds"], [])   # the original (>= 10) pipe ends were closed in the child
        t.close()   # closing our end of Chrome's input ends it
        proc.wait(timeout=5)


class TestBrowserSession(unittest.TestCase):
    def test_messages_carry_the_session_except_target_commands(self):
        b = tool.Browser.__new__(tool.Browser)
        b.session = "S1"
        self.assertEqual(json.loads(b._msg(1, "Page.enable", None))["sessionId"], "S1")
        self.assertNotIn("sessionId", json.loads(b._msg(2, "Target.getTargets", None)))

    @unittest.skipUnless(BROWSER, "set ARLTUNER_BROWSER_TESTS=1 (needs a Chrome binary)")
    def test_chrome_has_no_debug_port_and_a_private_profile_that_is_removed(self):
        b = tool.Browser(CHROME)
        prof = Path(b.profile)
        self.assertEqual(stat.S_IMODE(os.stat(prof).st_mode), 0o700)
        cmd = Path(f"/proc/{b.proc.pid}/cmdline").read_bytes().replace(b"\0", b" ")   # Chrome rewrites its argv into one string
        self.assertIn(b"--remote-debugging-pipe", cmd)
        self.assertNotIn(b"--remote-debugging-port", cmd)
        self.assertEqual(b.eval("1 + 1"), 2)
        b.close()
        self.assertFalse(prof.exists())
        self.assertIsNotNone(b.proc.poll())


class TestLauncherAndGuards(unittest.TestCase):
    GOOD = '<form method="post" action="http://127.0.0.1:5123/bootstrap"><input type="hidden" name="token" value="t">'

    def _launcher(self, text=GOOD, dmode=0o700, fmode=0o600) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        sub = d / "arltuner"
        sub.mkdir()
        sub.chmod(dmode)
        f = sub / "launcher.html"
        f.write_text(text)
        f.chmod(fmode)
        return f

    def test_read_launcher_matches_what_the_gui_writes(self):
        sys.path.insert(0, str(ROOT / "src"))
        from arltuner.web.launcher import write_launcher
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o700)
        p = write_launcher(d / "launcher.html", "http://127.0.0.1:5123", "tok&en<1>")
        self.assertEqual(tool.read_launcher(p), ("http://127.0.0.1:5123", "tok&en<1>"))

    def test_launcher_must_point_at_a_local_gui(self):
        for action in ("http://evil.example:80", "http://localhost:5123", "https://127.0.0.1:5123"):
            f = self._launcher(f'<form method="post" action="{action}/bootstrap"><input type="hidden" name="token" value="t">')
            with self.assertRaises(tool.ToolError, msg=action):
                tool.read_launcher(f)
        with self.assertRaises(tool.ToolError):
            tool.read_launcher(f.parent / "missing.html")
        self.assertEqual(tool.read_launcher(self._launcher()), ("http://127.0.0.1:5123", "t"))

    def test_launcher_trust_checks(self):
        ok = self._launcher()
        self.assertIn("bootstrap", tool.open_launcher(ok))
        for what, f in (("file group-readable", self._launcher(fmode=0o640)), ("file world-writable", self._launcher(fmode=0o602)),
                        ("dir group-writable", self._launcher(dmode=0o770)), ("dir world-writable", self._launcher(dmode=0o757))):
            with self.assertRaises(tool.ToolError, msg=what):
                tool.open_launcher(f)
        real = self._launcher()   # a symlinked launcher file, a symlinked directory
        link = real.parent / "link.html"
        link.symlink_to(real)
        with self.assertRaises(tool.ToolError):
            tool.open_launcher(link)
        dlink = real.parent.parent / "dirlink"
        dlink.symlink_to(real.parent)
        with self.assertRaises(tool.ToolError):
            tool.open_launcher(dlink / "launcher.html")
        fifo = real.parent / "fifo.html"   # something that is not a regular file, planted in its place: refused without blocking
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(tool.ToolError):
            tool.open_launcher(fifo)
        with mock.patch.object(tool.os, "geteuid", return_value=os.geteuid() + 1):   # owned by somebody else
            with self.assertRaises(tool.ToolError):
                tool.open_launcher(ok)

    def test_click_allowlist(self):
        ok = ["#tab-tune", "#tab-dashboard", "#dash-sub button", "#cv-view button", "#tn-scope button", "#map-metric button",
              "#tn-rows .tn-rowwrap:nth-of-type(2) .tn-cell .seg button"]
        bad = ["#btn-tune-read", "#btn-tn-plan", "#plan-go", "#btn-measure", "#btn-stress-start", "#tab-evil", "button",
               "#tn-rows .tn-rowwrap:nth-of-type(2) button", "#btn-pr-save", "#btn-tn-reset", "#btn-tn-restore", "#dash-sub button, #plan-go"]
        b = tool.Browser.__new__(tool.Browser)   # no Chrome: the refusal comes before any protocol call
        for s in bad:
            with self.assertRaises(tool.ToolError, msg=s):
                b.click(s)
        for s in ok:
            self.assertTrue(any(rx.match(s) for rx in tool.SAFE_CLICKS), s)

    def test_only_read_ops_pass_the_api_gate(self):
        b = tool.Browser.__new__(tool.Browser)
        b.blocked, b.ops_seen, sent = [], {}, []
        b.call_nowait = lambda m, p: sent.append((m, p["requestId"]))

        def req(rid, path, body=None, method="POST"):
            r = {"url": "http://127.0.0.1:1" + path, "method": method}
            if body is not None:
                r["postData"] = json.dumps(body)
            b._gate({"requestId": rid, "request": r})

        for i, op in enumerate(("tune.last", "mem.read", "preflight", "curves.list")):
            req("ok%d" % i, "/api/call", {"op": op, "args": {}})
        writes = ("tune.read", "tune.plan", "tune.apply", "vf.measure", "stress.start", "profiles.save", "profiles.accept", "restore", "apply", "cancel", "bogus")
        for i, op in enumerate(writes):
            req("no%d" % i, "/api/call", {"op": op, "args": {}})
        req("logout", "/api/logout")
        req("garbage", "/api/call")
        req("sse", "/api/events", method="GET")
        passed = [rid for m, rid in sent if m == "Fetch.continueRequest"]
        failed = [rid for m, rid in sent if m == "Fetch.failRequest"]
        self.assertEqual(passed, ["ok0", "ok1", "ok2", "ok3", "sse"])
        self.assertEqual(len(failed), len(writes) + 2)
        self.assertEqual(len(b.blocked), len(writes) + 2)
        for op in writes:
            self.assertNotIn(op, tool.READ_OPS)


@unittest.skipUnless(BROWSER, "set ARLTUNER_BROWSER_TESTS=1 (needs a Chrome binary)")
class TestShotsAgainstSimulator(unittest.TestCase):
    def test_every_shot_is_taken_without_a_blocked_call(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = tool.main(["--sim", "--out", str(out), "--chrome", CHROME])
        self.assertEqual(rc, 0)
        names = sorted(p.stem for p in out.glob("*.png"))
        self.assertEqual(names, sorted(tool.SHOTS))   # the simulator has a stored read, curves, memory: nothing is skipped
        for p in out.glob("*.png"):
            data = p.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", p.name)
            self.assertLessEqual(len(data), 400 * 1024, p.name)
            w, h = struct.unpack(">II", data[16:24])
            self.assertGreater(w, 500, p.name)
            self.assertGreater(h, 100, p.name)

    def test_tune_shot_is_skipped_without_a_stored_read(self):
        """No tune.last answer -> the tool must not press Read; it skips the shot and says so."""
        sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
        from tests.web import v3_harness as h
        srv, fake, stop = h.start(seed=True)   # no seed_last_read()
        self.addCleanup(fake.close)
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        b = tool.Browser(CHROME)
        self.addCleanup(b.close)
        cookie = tool.bootstrap(srv.origin, srv.token)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = tool.run(b, srv.origin, cookie, out, ["tune-per-core", "stress"], 400)
        self.assertIn("SKIPPED", buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.stem for p in out.glob("*.png")), ["stress"])
        self.assertEqual(b.blocked, [])
        self.assertEqual([r["op"] for r in fake.requests if r["op"] in ("tune.read", "tune.plan", "tune.apply")], [])


if __name__ == "__main__":
    unittest.main()
