#!/usr/bin/env python3
"""Capture the README screenshots of the arrowlake-tuner web page (G-136): headless Chrome driven over the DevTools protocol (stdlib only).

    python3 tools/screenshots.py --sim                      # start the GUI itself against the simulator (no hardware) -> a temp dir
    python3 tools/screenshots.py --launcher PATH            # a GUI you started (python3 tools/arltuner-gui.py): log in with its launcher file
    python3 tools/screenshots.py --launcher PATH --out docs/img --only dashboard,curves

The launcher (default: $XDG_RUNTIME_DIR/arltuner/launcher.html, as written by tools/arltuner-gui.py) carries a single-use token: if it is gone
(the page was already opened), run ``python3 tools/arltuner-gui.py`` again - it asks the running GUI for a fresh launcher. Only the one-time
bootstrap token is used; the tool never talks to the helper itself. The launcher and its directory must be yours and private (not a symlink, not
writable by others, file 0600) or the tool refuses to read the token. Chrome is driven over a private pipe (--remote-debugging-pipe), not a TCP
port, and runs in a throw-away 0700 profile that is deleted at exit.

What it does to the page: opens tabs, switches display segments (chart / table, Simple / Per-Core, the per-V/F-point offsets of one row), waits for data and
crops elements to PNG files at 1440 CSS px wide. It reads only what the page shows on its own; it never presses Read, Review, Apply, Measure,
Stress, Save or Accept. Belt and braces: every ``/api/call`` the page makes is intercepted and any operation outside a read-only allowlist
(READ_OPS below) is blocked and reported. The Tune shots need a stored read (``tune.last``) already on the page; without one they are skipped.

Output: one PNG per shot, device scale 2 (halved in steps when a file would exceed --max-kb). The page is dark-only today, so ``--theme light``
only changes the emulated browser preference. Nothing is committed by this tool.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
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
    re.compile(r"^#tn-rows \.tn-rowwrap:nth-of-type\(\d+\) \.tn-cell \.seg button$"),     # one Tune row: whole curve / per-point offsets (view only)
)

WIDTH = 1440
HEIGHT = 1000
SCALE_STEPS = (1.0, 0.75, 0.5)      # clip scales tried (on top of device scale 2) until the PNG is within --max-kb


class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------------------------------
# DevTools protocol over a pipe (Chrome --remote-debugging-pipe: JSON messages, each ended by a NUL byte, on fds 3 (to Chrome) / 4 (from Chrome)).
# No TCP port is opened, so no other local user can attach to the browser while it holds the session cookie.
# ---------------------------------------------------------------------------------------------------------------------------------------
class PipeTransport:
    def __init__(self, rfd: int, wfd: int):
        """``rfd``: read end of Chrome's output; ``wfd``: write end of Chrome's input (both ours, closed by ``close``)."""
        self.rfd, self.wfd = rfd, wfd
        self._buf = b""

    def send_text(self, text: str) -> None:
        data = text.encode() + b"\0"
        try:
            while data:
                data = data[os.write(self.wfd, data):]
        except OSError as e:
            raise ToolError("the browser's pipe is closed: " + (e.strerror or str(e))) from None

    def recv_text(self, timeout: float | None = None) -> str | None:
        """Next complete message, or None when ``timeout`` seconds pass with nothing complete to read."""
        end = time.monotonic() + (timeout if timeout is not None else 60.0)
        while b"\0" not in self._buf:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.rfd], [], [], left)[0]:
                return None
            chunk = os.read(self.rfd, 1 << 20)
            if not chunk:
                raise ToolError("the browser closed its DevTools pipe (Chrome exited)")
            self._buf += chunk
        msg, _, self._buf = self._buf.partition(b"\0")
        return msg.decode()

    def close(self) -> None:
        for fd in (self.wfd, self.rfd):
            try:
                os.close(fd)
            except OSError:
                pass
        self.rfd = self.wfd = -1


def spawn_chrome_pipe(args: list[str]) -> tuple[subprocess.Popen, PipeTransport]:
    """Start Chrome with ``--remote-debugging-pipe``: it reads commands on fd 3 and writes replies on fd 4, so a small ``sh`` puts our pipe ends there
    (``exec`` keeps the same process) and closes the originals. Both pipes are private to this process and Chrome."""
    to_r, to_w = os.pipe()          # us -> Chrome
    from_r, from_w = os.pipe()      # Chrome -> us
    hi = [fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in (to_r, from_w)]   # numbers >= 10 so the redirections below cannot clash with 3 and 4
    for fd in (to_r, from_w):
        os.close(fd)
    r, w = hi
    try:
        proc = subprocess.Popen(["sh", "-c", f'exec "$0" "$@" 3<&{r} 4>&{w} {r}<&- {w}>&-', *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, pass_fds=(r, w))
    except BaseException:
        for fd in (to_w, from_r):
            os.close(fd)
        raise
    finally:
        for fd in (r, w):
            os.close(fd)
    return proc, PipeTransport(from_r, to_w)


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
        self.profile = tempfile.mkdtemp(prefix="arltuner-shots-")   # 0700, ours; removed again by close()
        os.chmod(self.profile, 0o700)
        args = [chrome, "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={self.profile}", "--no-first-run",
                "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars", "--mute-audio", f"--window-size={WIDTH},{HEIGHT}", "about:blank"]
        if os.geteuid() == 0:
            args.append("--no-sandbox")
        self._id = 0
        self.session: str | None = None
        self.console_errors: list[str] = []
        self.blocked: list[str] = []
        self.ops_seen: dict[str, int] = {}
        self.ws: PipeTransport | None = None
        self.proc: subprocess.Popen | None = None
        try:
            self.proc, self.ws = spawn_chrome_pipe(args)
            targets = self.call("Target.getTargets", timeout=60).get("targetInfos", [])
            page = next((t["targetId"] for t in targets if t.get("type") == "page"), None)
            if page is None:
                page = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.session = self.call("Target.attachToTarget", {"targetId": page, "flatten": True})["sessionId"]
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

    def _msg(self, mid: int, method: str, params: dict | None) -> str:
        m = {"id": mid, "method": method, "params": params or {}}
        if self.session and not method.startswith("Target."):
            m["sessionId"] = self.session
        return json.dumps(m)

    # -- protocol -----------------------------------------------------------------------------------------------------------------------
    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        assert self.ws
        self._id += 1
        mid = self._id
        self.ws.send_text(self._msg(mid, method, params))
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
        except ToolError:
            pass

    def call_nowait(self, method: str, params: dict) -> None:
        assert self.ws
        self._id += 1
        self.ws.send_text(self._msg(self._id, method, params))

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

    def click(self, selector: str, value: str | None = None) -> bool:
        """Press one allowlisted display control: the first match of ``selector``, or the button whose ``data-value`` is ``value`` (a segment's value
        does not change when its label is reworded). False when it is not there or disabled."""
        if not any(rx.match(selector) for rx in SAFE_CLICKS):
            raise ToolError(f"refusing to click {selector!r}: not an allowlisted display control")
        js = ("(() => { const q = %s, v = %s; const all = Array.from(document.querySelectorAll(q)); "
              "const b = v === null ? all[0] : all.find((x) => x.dataset.value === v); if (!b || b.disabled) return false; b.click(); return true; })()"
              % (json.dumps(selector), json.dumps(value)))
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
        if self.ws:
            self.ws.close()   # Chrome sees its input pipe close and exits by itself; terminate below is the backstop
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        for _ in range(5):   # Chrome's helper processes may still be flushing into the profile for a moment
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                break
            time.sleep(0.2)


# ---------------------------------------------------------------------------------------------------------------------------------------
# login (the launcher's single-use token -> a session cookie)
# ---------------------------------------------------------------------------------------------------------------------------------------
def open_launcher(path: Path) -> str:
    """The text of the launcher file, after checking that nobody else could have planted it: the file and the directory holding it must be ours
    (effective uid), not symlinks, and not writable by group/other; the file (it holds a secret) must have no group/other access, as
    ``tools/arltuner-gui.py`` writes it (0600 inside a 0700 directory). The file is opened with O_NOFOLLOW and checked on the open descriptor."""
    path = Path(os.path.abspath(path))
    fix = " Run 'python3 tools/arltuner-gui.py' to get a launcher it wrote itself."
    try:
        dst = os.lstat(path.parent)
    except OSError as e:
        raise ToolError(f"cannot use the launcher {path}: {e.strerror}.{fix}") from None
    if not stat.S_ISDIR(dst.st_mode):   # lstat: a symlinked directory is not a directory here
        raise ToolError(f"refusing the launcher {path}: its directory is a symlink or not a directory.{fix}")
    if dst.st_uid != os.geteuid():
        raise ToolError(f"refusing the launcher {path}: its directory is owned by uid {dst.st_uid}, not by you.{fix}")
    if dst.st_mode & 0o022:
        raise ToolError(f"refusing the launcher {path}: its directory is writable by others (mode {stat.S_IMODE(dst.st_mode):04o}; the GUI makes it 0700).{fix}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ToolError(f"cannot read the launcher {path}: {e.strerror}.{fix}") from None
    with os.fdopen(fd, "rb") as f:
        fst = os.fstat(f.fileno())
        if not stat.S_ISREG(fst.st_mode):
            raise ToolError(f"refusing the launcher {path}: not a regular file.{fix}")
        if fst.st_uid != os.geteuid():
            raise ToolError(f"refusing the launcher {path}: owned by uid {fst.st_uid}, not by you.{fix}")
        if fst.st_mode & 0o077:
            raise ToolError(f"refusing the launcher {path}: mode {stat.S_IMODE(fst.st_mode):04o} lets others in (the GUI writes it 0600).{fix}")
        data = f.read(65536)
    return data.decode("utf-8", "replace")


def read_launcher(path: Path) -> tuple[str, str]:
    """(origin, token) out of the launcher page tools/arltuner-gui.py wrote (a form posting the token to <origin>/bootstrap)."""
    text = open_launcher(path)
    import html
    m_action = re.search(r'<form[^>]*action="([^"]+)/bootstrap"', text)
    m_token = re.search(r'name="token" value="([^"]+)"', text)
    if not m_action or not m_token:
        raise ToolError(f"{path} is not an arrowlake-tuner launcher page")
    origin = html.unescape(m_action.group(1))
    if not re.fullmatch(r"http://127\.0\.0\.1:\d+", origin):
        raise ToolError(f"refusing a launcher that points at {origin!r}: only a local GUI (http://127.0.0.1:PORT)")
    return origin, html.unescape(m_token.group(1))


def bootstrap(origin: str, token: str) -> tuple[str, str]:
    """POST the token as the launcher form does (Origin: null); returns the session cookie (name, value)."""
    req = urllib.request.Request(origin + "/bootstrap", data=urllib.parse.urlencode({"token": token}).encode(), method="POST",
                                 headers={"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:   # never via an environment proxy
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
    b.click("#dash-sub button", "overview")
    b.pump(1.0)
    return [".ov-grid"]


def shot_dashboard_vid(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "document.querySelectorAll('#die-cores > *').length > 8")
    b.click("#dash-sub button", "overview")
    if not b.click("#map-metric button", "vid"):
        raise Skip("no VID colouring on the core map")
    b.pump(1.2)
    return [".die-card"]


def shot_dashboard_cores(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "document.querySelectorAll('#die-cores > *').length > 8")
    if not b.click("#dash-sub button", "cores") or not b.wait_for("document.querySelectorAll('#core-body tr').length > 8", 8):
        raise Skip("the per-core table did not fill")
    b.pump(1.0)
    return [".cores-grid .card.scroll"]


def shot_dashboard_memory(b: Browser) -> list[str]:
    show_tab(b, "dashboard", "!!document.querySelector('#mem-body .mem-big')")
    b.click("#dash-sub button", "overview")
    return ["#mem-card"]


def _curves_ready(b: Browser) -> None:
    show_tab(b, "curves", "document.querySelectorAll('#cv-chart .vc-line').length > 0", 12)


def shot_curves(b: Browser) -> list[str]:
    _curves_ready(b)
    b.click("#cv-view button", "chart")
    b.pump(0.8)
    return ["#cv-card"]


def shot_curves_table(b: Browser) -> list[str]:
    _curves_ready(b)
    b.click("#cv-view button", "table")
    if not b.wait_for("document.querySelectorAll('#cv-table tbody tr').length > 0", 5):
        raise Skip("the table has no rows")
    return ["#cv-card"]


def shot_tune_per_core(b: Browser) -> list[str]:
    # tune.last is asked by the page on load; no stored read -> nothing to show, and the tool does not press Read
    if not b.click("#tab-tune") or not b.wait_for("!document.getElementById('tn-console').hidden", 20):
        raise Skip("no stored tuning read on the page (tune.last empty): read it once in Tune, then run again")
    b.click("#tn-scope button", "unit")
    b.pump(0.8)
    # one P row with its Per-V/F-point offsets open (a view toggle; no value is changed)
    if not b.wait_for("document.querySelectorAll('#tn-rows .tn-rowwrap').length > 4", 5):
        raise Skip("the Tune rows did not build")
    if not b.click("#tn-rows .tn-rowwrap:nth-of-type(2) .tn-cell .seg button", "points"):
        raise Skip("no per-V/F-point control on the first P row")
    b.wait_for("document.querySelectorAll('#tn-rows .tn-rowwrap:nth-of-type(2) .tn-points select').length > 0", 5)
    b.pump(0.6)
    return ["#tn-console"]


def shot_stress(b: Browser) -> list[str]:
    show_tab(b, "stress", "!!document.getElementById('st-setup') && !document.getElementById('panel-stress').hidden")
    return ["#panel-stress"]


def shot_profiles(b: Browser) -> list[str]:
    show_tab(b, "profiles", "!document.getElementById('panel-profiles').hidden")
    # the accepted chain and the list read each profile's contents; do not shoot while one is still loading or failed to load
    if not b.wait_for("!/reading\\.\\.\\.|contents unavailable/.test(document.getElementById('panel-profiles').textContent)", 20):
        raise Skip("profile contents did not load (still 'reading' or 'unavailable'); run again")
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


MAX_HEIGHT = {"tune-per-core": 1000, "profiles": 790}     # CSS px: a very tall element is cut from the top (Tune: the header, the open row and the first rows)


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
