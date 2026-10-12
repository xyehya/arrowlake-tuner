"""G-136: the README screenshot tool (``tools/screenshots.py``): websocket framing, launcher parsing, the click and API allowlists (offline), and a
browser smoke against the simulator GUI (opt-in: ``ARLTUNER_BROWSER_TESTS=1`` and a Chrome binary). No hardware, no real helper."""
from __future__ import annotations

import base64
import contextlib
import io
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("screenshots_tool", ROOT / "tools" / "screenshots.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

CHROME = os.environ.get("ARLTUNER_CHROME") or shutil.which("google-chrome") or shutil.which("chromium") or ""
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n)
    return head + payload


class EchoServer:
    """A one-connection websocket server that plays a script of server frames and records the (unmasked) client text frames."""

    def __init__(self, script: list[bytes]):
        self.script = script
        self.got: list[bytes] = []
        self.all_masked = True
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        c, _ = self.sock.accept()
        c.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            data += c.recv(4096)
        key = [ln.split(b":", 1)[1].strip() for ln in data.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        for f in self.script:
            c.sendall(f)
        buf = b""
        try:
            while True:
                chunk = c.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        while len(buf) >= 6:
            b0, b1 = buf[0], buf[1]
            n, off = b1 & 0x7F, 2
            if n == 126:
                n, off = struct.unpack(">H", buf[2:4])[0], 4
            self.all_masked = self.all_masked and bool(b1 & 0x80)
            mask = buf[off:off + 4]
            body = bytes(x ^ mask[i % 4] for i, x in enumerate(buf[off + 4:off + 4 + n]))
            if b0 & 0x0F == 0x1:
                self.got.append(body)
            buf = buf[off + 4 + n:]
        c.close()
        self.sock.close()


class TestWebSocket(unittest.TestCase):
    def test_handshake_frames_ping_fragments_and_masking(self):
        big = ("x" * 70000).encode()
        srv = EchoServer([frame(0x1, b"hello"), frame(0x9, b"p"), frame(0x1, b"frag", fin=False), frame(0x0, b"ment"), frame(0x1, big)])
        ws = tool.WebSocket(f"ws://127.0.0.1:{srv.port}/devtools/page/x")
        self.assertEqual(ws.recv_text(2), "hello")
        self.assertEqual(ws.recv_text(2), "fragment")   # the ping in front was answered, the fragments joined
        self.assertEqual(ws.recv_text(2).encode(), big)  # 64-bit length form
        self.assertIsNone(ws.recv_text(0.1))             # nothing more: a timeout, not an error
        ws.send_text(json.dumps({"id": 1, "method": "X"}))
        payload = "x" * 3000   # 126-form length
        ws.send_text(payload)
        ws.sock.shutdown(socket.SHUT_WR)
        srv.t.join(5)
        self.assertEqual(srv.got, [b'{"id": 1, "method": "X"}', payload.encode()])
        self.assertTrue(srv.all_masked)
        ws.sock.close()

    def test_bad_accept_key_is_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)

        def bad():
            c, _ = s.accept()
            c.recv(4096)
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: nope\r\n\r\n")
            c.close()
        threading.Thread(target=bad, daemon=True).start()
        with self.assertRaises(tool.ToolError):
            tool.WebSocket(f"ws://127.0.0.1:{s.getsockname()[1]}/")
        s.close()


class TestLauncherAndGuards(unittest.TestCase):
    def test_read_launcher_matches_what_the_gui_writes(self):
        sys.path.insert(0, str(ROOT / "src"))
        from arltuner.web.launcher import write_launcher
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o700)
        p = write_launcher(d / "launcher.html", "http://127.0.0.1:5123", "tok&en<1>")
        self.assertEqual(tool.read_launcher(p), ("http://127.0.0.1:5123", "tok&en<1>"))

    def test_launcher_must_point_at_a_local_gui(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "l.html"
        p.write_text('<form method="post" action="http://evil.example:80/bootstrap"><input type="hidden" name="token" value="t">')
        with self.assertRaises(tool.ToolError):
            tool.read_launcher(p)
        with self.assertRaises(tool.ToolError):
            tool.read_launcher(d / "missing.html")

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


@unittest.skipUnless(os.environ.get("ARLTUNER_BROWSER_TESTS") == "1" and CHROME, "set ARLTUNER_BROWSER_TESTS=1 (needs a Chrome binary)")
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
