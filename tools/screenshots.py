#!/usr/bin/env python3
"""Capture the README screenshots of the arrowlake-tuner web page (G-136): headless Chrome driven over the DevTools protocol (stdlib only).

    python3 tools/screenshots.py --sim                      # start the GUI itself against the simulator (no hardware) -> a temp dir
    python3 tools/screenshots.py --launcher PATH            # a GUI you started (python3 tools/arltuner-gui.py): log in with its launcher file
    python3 tools/screenshots.py --launcher PATH --out docs/img --only dashboard,curves

The launcher (default: $XDG_RUNTIME_DIR/arltuner/launcher.html, as written by tools/arltuner-gui.py) carries a single-use token: if it is gone
(the page was already opened), run ``python3 tools/arltuner-gui.py`` again - it asks the running GUI for a fresh launcher. Only the one-time
bootstrap token is used; the tool never talks to the helper itself.

What it does to the page: opens tabs, switches display segments (chart / table, Simple / Per unit, "Per point" of one row), waits for data and
crops elements to PNG files at 1440 CSS px wide. It reads only what the page shows on its own; it never presses Read, Review, Apply, Measure,
Stress, Save or Accept. Belt and braces: every ``/api/call`` the page makes is intercepted and any operation outside a read-only allowlist
(READ_OPS below) is blocked and reported. The Tune shots need a stored read (``tune.last``) already on the page; without one they are skipped.

Output: one PNG per shot, device scale 2 (halved in steps when a file would exceed --max-kb). The page is dark-only today, so ``--theme light``
only changes the emulated browser preference. Nothing is committed by this tool.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The only operations the page may send while the tool drives it. Every one is a read that the page issues by itself on load or tab change
# (stored data, cheap system reads); nothing that writes hardware, starts a measurement or stress run, or saves anything.
READ_OPS = frozenset({"preflight", "audit.tail", "telemetry.start", "profiles.list", "profiles.get", "tune.last", "curves.list", "curves.get",
                      "mem.read", "stress.list"})

# What the tool may click. Each entry is a regex over the CSS selector the tool passes to ``click()``; nothing else is ever clicked.
SAFE_CLICKS = (
    re.compile(r"^#tab-(dashboard|curves|tune|stress|profiles|system|audit)$"),                       # tabs
    re.compile(r"^#(dash-sub|cv-view|cv-table-mode|tn-scope|map-metric) button$"),                     # display segments only
    re.compile(r"^#tn-rows \.tn-rowwrap:nth-of-type\(\d+\) \.tn-cell \.seg button$"),     # one Tune row: Whole curve / Per point (view only)
)

WIDTH = 1440
HEIGHT = 1000
SCALE_STEPS = (1.0, 0.75, 0.5)      # clip scales tried (on top of device scale 2) until the PNG is within --max-kb


class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------------------------------
# minimal websocket client (RFC 6455, client side, text frames, no extensions) - only what the DevTools endpoint needs
# ---------------------------------------------------------------------------------------------------------------------------------------
class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws":
            raise ToolError("only ws:// DevTools URLs are supported")
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        try:
            self._handshake(u)
        except BaseException:
            self.sock.close()
            raise

    def _handshake(self, u) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ToolError("websocket handshake: connection closed")
            head += chunk
        head, _, self._buf = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0]:
            raise ToolError("websocket handshake refused: " + lines[0])
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        if hdrs.get("sec-websocket-accept") != accept:
            raise ToolError("websocket handshake: bad accept key")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ToolError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _xor_mask(payload, mask)
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_text(self, timeout: float | None = None) -> str | None:
        """Next complete text message, or None when ``timeout`` seconds pass with nothing to read."""
        self.sock.settimeout(timeout if timeout is not None else 60.0)
        msg = b""
        try:
            while True:
                b0, b1 = self._read(2)
                fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                data = self._read(n) if not (b1 & 0x80) else None
                if data is None:
                    raise ToolError("server frames must not be masked")
                if opcode == 0x9:
                    self._send_frame(0xA, data)
                elif opcode == 0x8:
                    raise ToolError("websocket closed by the browser")
                elif opcode in (0x0, 0x1, 0x2):
                    msg += data
                    if fin:
                        return msg.decode()
        except socket.timeout:
            if not msg and not self._buf:
                return None
            raise ToolError("websocket timed out in the middle of a message") from None

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


def _xor_mask(payload: bytes, mask: bytes) -> bytes:
    k = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(k, "big")).to_bytes(len(payload), "big")


# ---------------------------------------------------------------------------------------------------------------------------------------
# Chrome + DevTools protocol
# ---------------------------------------------------------------------------------------------------------------------------------------
def find_chrome(explicit: str | None) -> str:
    for c in (explicit, os.environ.get("ARLTUNER_CHROME"), shutil.which("google-chrome"), shutil.which("google-chrome-stable"), shutil.which("chromium"),
              shutil.which("chromium-browser")):
        if c and os.access(c, os.X_OK):
            return c
    raise ToolError("no Chrome/Chromium found (use --chrome PATH or set ARLTUNER_CHROME)")


class Browser:
    """A headless Chrome with its own throw-away profile, one page, driven over CDP."""

    def __init__(self, chrome: str, theme: str = "dark"):
        self.profile = tempfile.mkdtemp(prefix="arltuner-shots-")
        args = [chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.profile}", "--no-first-run",
                "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars", "--mute-audio", f"--window-size={WIDTH},{HEIGHT}", "about:blank"]
        if os.geteuid() == 0:
            args.append("--no-sandbox")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._id = 0
        self.events: list[dict] = []
        self.console_errors: list[str] = []
        self.blocked: list[str] = []
        self.ops_seen: dict[str, int] = {}
        self.ws: WebSocket | None = None
        try:
            port = self._wait_port()
            targets = []
            for _ in range(50):
                try:
                    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
                except (OSError, ValueError):
                    targets = []
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    break
                time.sleep(0.1)
            else:
                raise ToolError("Chrome started but offered no page target")
            self.ws = WebSocket(pages[0]["webSocketDebuggerUrl"])
            self.call("Page.enable")
            self.call("Runtime.enable")
            self.call("Log.enable")
            self.call("Network.enable")
            self.call("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 2, "mobile": False})
            self.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": theme}]})
            self.call("Fetch.enable", {"patterns": [{"urlPattern": "*/api/*", "requestStage": "Request"}]})
        except BaseException:
            self.close()
            raise

    def _wait_port(self) -> int:
        f = Path(self.profile) / "DevToolsActivePort"
        for _ in range(100):
            if self.proc.poll() is not None:
                raise ToolError("Chrome exited at start (exit %s)" % self.proc.returncode)
            try:
                line = f.read_text().splitlines()[0]
                return int(line)
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        raise ToolError("Chrome did not publish its DevTools port")

    # -- protocol -----------------------------------------------------------------------------------------------------------------------
    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        assert self.ws
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise ToolError(f"{method}: no answer in {timeout:.0f} s")
            raw = self.ws.recv_text(min(left, 5.0))
            if raw is None:
                continue
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise ToolError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self._event(msg)

    def pump(self, seconds: float) -> None:
        """Let events (and the request interception) run for a while."""
        assert self.ws
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            raw = self.ws.recv_text(min(0.1, max(0.0, end - time.monotonic())) or 0.01)
            if raw is not None:
                self._event(json.loads(raw))

    def _event(self, msg: dict) -> None:
        m, p = msg.get("method"), msg.get("params", {})
        if m == "Fetch.requestPaused":
            self._gate(p)
        elif m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.console_errors.append("exception: " + str(d.get("exception", {}).get("description") or d.get("text")))
        elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error" and "favicon" not in p["entry"].get("url", "favicon"):
            if "404" not in p["entry"].get("text", ""):
                self.console_errors.append("log: " + p["entry"].get("text", ""))

    def _gate(self, p: dict) -> None:
        """Every /api/* request the page makes passes here: only the read-only allowlist is let through."""
        req = p["request"]
        rid = p["requestId"]
        path = urllib.parse.urlsplit(req["url"]).path
        allow, label = False, path
        if path == "/api/call":
            try:
                op = json.loads(req.get("postData") or "{}").get("op")
            except ValueError:
                op = None
            label = "op " + str(op)
            allow = op in READ_OPS
            if allow:
                self.ops_seen[op] = self.ops_seen.get(op, 0) + 1
        elif path in ("/api/status", "/api/events") and req.get("method") == "GET":
            allow = True
        try:
            if allow:
                self.call_nowait("Fetch.continueRequest", {"requestId": rid})
            else:
                self.blocked.append(label)
                self.call_nowait("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"})
        except OSError:
            pass

    def call_nowait(self, method: str, params: dict) -> None:
        assert self.ws
        self._id += 1
        self.ws.send_text(json.dumps({"id": self._id, "method": method, "params": params}))

    # -- page helpers -------------------------------------------------------------------------------------------------------------------
    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in r:
            raise ToolError("page script failed: " + str(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"].get("text")))
        return r["result"].get("value")

    def wait_for(self, expr: str, timeout: float = 10.0, what: str = "") -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.eval(expr):
                    return True
            except ToolError:
                pass
            self.pump(0.2)
        return False

    def goto(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        self.pump(0.5)
        self.wait_for("document.readyState === 'complete'", 15)

    def click(self, selector: str, text: str | None = None) -> bool:
        """Press one allowlisted display control (by CSS selector, optionally the button whose label is ``text``). False when it is not there."""
        if not any(rx.match(selector) for rx in SAFE_CLICKS):
            raise ToolError(f"refusing to click {selector!r}: not an allowlisted display control")
        js = ("(() => { const q = %s, t = %s; const all = Array.from(document.querySelectorAll(q)); "
              "const b = t === null ? all[0] : all.find((x) => x.textContent.trim() === t); if (!b || b.disabled) return false; b.click(); return true; })()"
              % (json.dumps(selector), json.dumps(text)))
        ok = bool(self.eval(js))
        self.pump(0.3)
        return ok

    def rect(self, selectors: list[str], pad: int = 12) -> dict | None:
        """The union of the visible elements' boxes in document coordinates, padded; None when none is visible."""
        js = ("(() => { let x0 = 1e9, y0 = 1e9, x1 = -1, y1 = -1; for (const s of %s) { const e = document.querySelector(s); if (!e || e.hidden) continue; "
              "const r = e.getBoundingClientRect(); if (!r.width || !r.height) continue; x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); "
              "x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom); } if (x1 < 0) return null; "
              "return { x: x0 + scrollX, y: y0 + scrollY, w: x1 - x0, h: y1 - y0 }; })()" % json.dumps(selectors))
        r = self.eval(js)
        if not r:
            return None
        x, y = max(0.0, r["x"] - pad), max(0.0, r["y"] - pad)
        return {"x": x, "y": y, "width": min(WIDTH - x, r["w"] + 2 * pad), "height": r["h"] + 2 * pad}

    def png(self, clip: dict, scale: float) -> bytes:
        res = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "fromSurface": True, "clip": {**clip, "scale": scale}})
        return base64.b64decode(res["data"])

    def close(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------------------------
# login (the launcher's single-use token -> a session cookie)
# ---------------------------------------------------------------------------------------------------------------------------------------
def read_launcher(path: Path) -> tuple[str, str]:
    """(origin, token) out of the launcher page tools/arltuner-gui.py wrote (a form posting the token to <origin>/bootstrap)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ToolError(f"cannot read the launcher {path}: {e.strerror}. Run 'python3 tools/arltuner-gui.py' again for a fresh one.") from None
    import html
    m_action = re.search(r'<form[^>]*action="([^"]+)/bootstrap"', text)
    m_token = re.search(r'name="token" value="([^"]+)"', text)
    if not m_action or not m_token:
        raise ToolError(f"{path} is not an arrowlake-tuner launcher page")
    origin = html.unescape(m_action.group(1))
    if not re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", origin):
        raise ToolError(f"refusing a launcher that points at {origin!r}: only a local GUI (http://127.0.0.1:PORT)")
    return origin, html.unescape(m_token.group(1))


def bootstrap(origin: str, token: str) -> tuple[str, str]:
    """POST the token as the launcher form does (Origin: null); returns the session cookie (name, value)."""
    req = urllib.request.Request(origin + "/bootstrap", data=urllib.parse.urlencode({"token": token}).encode(), method="POST",
                                 headers={"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            cookie = r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        raise ToolError(f"login refused (HTTP {e.code}): the launcher token was used already or expired. Run 'python3 tools/arltuner-gui.py' for a fresh launcher.") from None
    except OSError as e:
        raise ToolError(f"cannot reach the GUI at {origin}: {e}") from None
    m = re.match(r"([^=;\s]+)=([^;]+)", cookie)
    if not m:
        raise ToolError("login answered without a session cookie")
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------------------------------------------------------------------
# the shot list
# ---------------------------------------------------------------------------------------------------------------------------------------
class Skip(Exception):
    """A shot cannot be taken from what the page already shows (reported, not an error)."""


def show_tab(b: Browser, tab: str, ready: str, timeout: float = 12.0) -> None:
    if not b.click("#tab-" + tab):
        raise Skip(f"tab {tab} not found")
    if not b.wait_for(ready, timeout):
        raise Skip(f"tab {tab}: the data did not arrive in {timeout:.0f} s")
    b.pump(0.8)


def shot_dashboard(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "document.querySelectorAll('#die-cores > *').length > 8 && !/n\\/a/.test(document.getElementById('t-pkgw').textContent)")
    b.click("#dash-sub button", "Overview")
    b.pump(1.0)
    return [".ov-grid"]


def shot_dashboard_vid(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "document.querySelectorAll('#die-cores > *').length > 8")
    b.click("#dash-sub button", "Overview")
    if not b.click("#map-metric button", "VID"):
        raise Skip("no VID colouring on the core map")
    b.pump(1.2)
    return [".die-card"]


def shot_dashboard_cores(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "document.querySelectorAll('#die-cores > *').length > 8")
    if not b.click("#dash-sub button", "Cores") or not b.wait_for("document.querySelectorAll('#core-body tr').length > 8", 8):
        raise Skip("the per-core table did not fill")
    b.pump(1.0)
    return [".cores-grid .card.scroll"]


def shot_dashboard_memory(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "!!document.querySelector('#mem-body .mem-big')")
    b.click("#dash-sub button", "Overview")
    return ["#mem-card"]


def _curves_ready(b: Browser) -> None:
    show_tab(b, "curves", "document.querySelectorAll('#cv-chart .vc-line').length > 0", 12)


def shot_curves(b: Browser) -> list[str]:
    _curves_ready(b)
    b.click("#cv-view button", "Chart")
    b.pump(0.8)
    return ["#cv-card"]


def shot_curves_table(b: Browser) -> list[str]:
    _curves_ready(b)
    b.click("#cv-view button", "Table")
    if not b.wait_for("document.querySelectorAll('#cv-table tbody tr').length > 0", 5):
        raise Skip("the table has no rows")
    return ["#cv-card"]


def shot_tune_per_core(b: Browser) -> list[str]:
    # tune.last is asked by the page on load; no stored read -> nothing to show, and the tool does not press Read
    if not b.click("#tab-tune") or not b.wait_for("!document.getElementById('tn-console').hidden", 8):
        raise Skip("no stored tuning read on the page (tune.last empty): read it once in Tune, then run again")
    b.click("#tn-scope button", "Per-Core")
    b.pump(0.8)
    # one P row with its Per-V/F-point offsets open (a view toggle; no value is changed)
    if not b.wait_for("document.querySelectorAll('#tn-rows .tn-rowwrap').length > 4", 5):
        raise Skip("the Tune rows did not build")
    if not b.click("#tn-rows .tn-rowwrap:nth-of-type(2) .tn-cell .seg button", "Per V/F point Offsets"):
        raise Skip("no 'Per point' control on the first P row")
    b.wait_for("document.querySelectorAll('#tn-rows .tn-rowwrap:nth-of-type(2) .tn-points select').length > 0", 5)
    b.pump(0.6)
    return ["#tn-console"]


def shot_stress(b: Browser) -> list[str]:
    show_tab(b, "stress", "!!document.getElementById('st-setup') && !document.getElementById('panel-stress').hidden")
    return ["#panel-stress"]


def shot_profiles(b: Browser) -> list[str]:
    show_tab(b, "profiles", "!document.getElementById('panel-profiles').hidden")
    b.pump(1.0)
    return ["#panel-profiles"]


def shot_system(b: Browser) -> list[str]:
    show_tab(b, "system", "document.querySelectorAll('#preflight .pill').length > 0 || /\\S/.test(document.getElementById('preflight').textContent.replace(/Preflight/, ''))")
    b.pump(1.0)
    return ["#panel-system"]


# name -> (function returning the CSS selectors to crop). Order matters little: every shot opens its own tab.
SHOTS = {
    "dashboard": shot_dashboard,
    "dashboard-vid": shot_dashboard_vid,
    "dashboard-cores": shot_dashboard_cores,
    "dashboard-memory": shot_dashboard_memory,
    "curves": shot_curves,
    "curves-table": shot_curves_table,
    "tune-per-core": shot_tune_per_core,
    "stress": shot_stress,
    "profiles": shot_profiles,
    "system": shot_system,
}


MAX_HEIGHT = {"tune-per-core": 1000}     # CSS px: a very tall element is cut from the top (Tune: the header, the open row and the first rows)


def write_png(b: Browser, name: str, selectors: list[str], out: Path, max_kb: int) -> tuple[Path, int, float]:
    clip = b.rect(selectors)
    if clip is None:
        raise Skip("nothing visible to crop (" + ", ".join(selectors) + ")")
    clip["height"] = min(clip["height"], MAX_HEIGHT.get(name, 10 ** 6))
    data = b""
    used = 1.0
    for used in SCALE_STEPS:
        data = b.png(clip, used)
        if len(data) <= max_kb * 1024:
            break
    path = out / (name + ".png")
    path.write_bytes(data)
    return path, len(data), used


def run(b: Browser, origin: str, cookie: tuple[str, str], out: Path, only: list[str], max_kb: int) -> int:
    b.call("Network.setCookie", {"name": cookie[0], "value": cookie[1], "url": origin + "/", "httpOnly": True, "sameSite": "Strict"})
    b.goto(origin + "/")
    if not b.wait_for("document.getElementById('conn-helper').classList.contains('s-ok')", 20):
        print("note: the helper pill is not 'ok' (the page may show no data); continuing", file=sys.stderr)
    b.pump(2.0)   # first telemetry sample and the memory card
    out.mkdir(parents=True, exist_ok=True)
    taken = skipped = 0
    for name in only:
        try:
            selectors = SHOTS[name](b)
            path, size, scale = write_png(b, name, selectors, out, max_kb)
            print(f"  {name:18s} {path}  {size / 1024:.0f} KB" + (f"  (scaled {scale:g}x)" if scale != 1.0 else ""))
            taken += 1
        except Skip as e:
            print(f"  {name:18s} SKIPPED: {e}")
            skipped += 1
        except ToolError as e:
            print(f"  {name:18s} FAILED: {e}")
            skipped += 1
    print(f"{taken} taken, {skipped} skipped; read-only calls seen: " + (", ".join(f"{k} x{v}" for k, v in sorted(b.ops_seen.items())) or "none"))
    if b.blocked:
        print("BLOCKED page calls (outside the read allowlist): " + ", ".join(sorted(set(b.blocked))), file=sys.stderr)
    if b.console_errors:
        print("page errors: " + "; ".join(sorted(set(b.console_errors))[:5]), file=sys.stderr)
    return 0 if taken and not b.blocked else 1


def start_sim():
    """The GUI against the simulator (the dev harness: real WebServer, contract-9 fake helper; no hardware, no helper code)."""
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    from tests.web import v3_harness as h
    srv, fake, stop = h.start(measure_delay=0.0, apply_delay=0.0, seed=True)
    fake.seed_last_read()
    return srv, fake, stop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--sim", action="store_true", help="start the GUI against the simulator (needs the checkout's tests/ package); default output: a temp dir")
    src.add_argument("--launcher", type=Path, help="launcher.html of a running GUI (default: $XDG_RUNTIME_DIR/arltuner/launcher.html)")
    ap.add_argument("--out", type=Path, help="output directory (default docs/img; with --sim a temp dir)")
    ap.add_argument("--only", help="comma-separated shot names (default all): " + ", ".join(SHOTS))
    ap.add_argument("--theme", choices=("dark", "light"), default="dark", help="emulated colour scheme (the page is dark-only today)")
    ap.add_argument("--max-kb", type=int, default=400, help="shrink a PNG in steps until it is at most this big (default 400)")
    ap.add_argument("--chrome", help="Chrome/Chromium binary (default: ARLTUNER_CHROME, google-chrome, chromium)")
    a = ap.parse_args(argv)
    only = [s.strip() for s in a.only.split(",")] if a.only else list(SHOTS)
    bad = [s for s in only if s not in SHOTS]
    if bad:
        ap.error("unknown shot(s): " + ", ".join(bad))
    chrome = find_chrome(a.chrome)

    cleanup: list = []
    try:
        if a.sim:
            srv, fake, stop = start_sim()
            cleanup += [fake.close, srv.close, stop.set]
            origin, token = srv.origin, srv.token
            out = a.out or Path(tempfile.mkdtemp(prefix="arltuner-shots-out-"))
        else:
            launcher = a.launcher
            if launcher is None:
                xdg = os.environ.get("XDG_RUNTIME_DIR")
                launcher = Path(xdg) / "arltuner" / "launcher.html" if xdg else Path(f"/tmp/arltuner-{os.geteuid()}") / "launcher.html"
            origin, token = read_launcher(launcher)
            out = a.out or ROOT / "docs" / "img"
        cookie = bootstrap(origin, token)
        print(f"GUI {origin}; writing to {out}")
        b = Browser(chrome, a.theme)
        cleanup.append(b.close)
        return run(b, origin, cookie, out, only, a.max_kb)
    except ToolError as e:
        print("screenshots: " + str(e), file=sys.stderr)
        return 2
    finally:
        for fn in reversed(cleanup):
            try:
                fn()
            except Exception:  # noqa: BLE001 - cleanup must not hide the real result
                pass


if __name__ == "__main__":
    sys.exit(main())
