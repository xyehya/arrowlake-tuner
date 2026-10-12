#!/usr/bin/env python3
"""Start the local web UI:  python3 tools/arltuner-gui.py [--helper CMD ...] [--no-helper] [--launcher PATH] [--open]

Writes a 0600 launcher page (token inside) and prints its file:// URL; open it once and click the button. The server listens on a random loopback
address ``127.x.y.z`` (its own cookie jar; the line ``listening on http://127.x.y.z:PORT`` names it), never on 127.0.0.1 unless that bind fails. ``--helper`` is the command that
speaks the helper protocol (Contract 8) on stdin/stdout. Default (G-106): when ``install.sh`` has put the helper at
``/usr/lib/arltuner/arltuner-helper``, ``pkexec /usr/lib/arltuner/arltuner-helper --device`` (polkit asks for the admin password: the human act,
SPEC 4.1); otherwise no helper and API calls answer 503. ``--no-helper`` forces the no-helper mode. This script never elevates by itself: only
polkit does, and only for exactly that installed path. Web files: the checkout's ``web/`` if present, else ``/usr/share/arltuner/web``.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arltuner.web.helperlink import set_undumpable, spawn_helper  # noqa: E402
from arltuner.web.launcher import LauncherDirError, check_private_dir, make_private_dir, write_launcher  # noqa: E402
from arltuner.web.reissue import install_reissue, signal_running, write_pidfile  # noqa: E402
from arltuner.web.server import Config, WebServer  # noqa: E402


INSTALLED_HELPER = "/usr/lib/arltuner/arltuner-helper"  # = arltuner.helper.config.INSTALLED_HELPER (the polkit action's exec.path)
INSTALLED_WEB_DIR = Path("/usr/share/arltuner/web")  # = arltuner.helper.config.INSTALLED_WEB_DIR


def default_helper_command(isfile=os.path.isfile) -> list[str] | None:
    """``pkexec <installed helper> --device`` when the installed helper exists, else ``None`` (a checkout run must not pop a polkit prompt)."""
    return ["pkexec", INSTALLED_HELPER, "--device"] if isfile(INSTALLED_HELPER) else None


def default_web_root(checkout_web: Path = ROOT / "web", installed: Path = INSTALLED_WEB_DIR, isdir=os.path.isdir) -> Path:
    """The checkout's ``web/`` when it exists (development), else the installed, version-matched ``/usr/share/arltuner/web``."""
    return checkout_web if isdir(checkout_web) else installed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--helper", nargs=argparse.REMAINDER, help="helper command (rest of the line); default: pkexec the installed helper, when installed")
    ap.add_argument("--no-helper", action="store_true", help="do not start any helper (API calls answer 503)")
    ap.add_argument("--launcher", type=Path, default=None, help="default: $XDG_RUNTIME_DIR/arltuner/launcher.html (else /tmp/arltuner-UID/)")
    ap.add_argument("--web-root", type=Path, default=default_web_root(), help="default: ./web of the checkout, else /usr/share/arltuner/web")
    ap.add_argument("--open", action="store_true", help="open the launcher in the default browser")
    a = ap.parse_args(argv)
    if not set_undumpable():  # same-user processes may not ptrace us or read /proc/<pid>/{fd,mem,environ} (G-142)
        print("arltuner-gui: warning: could not make this process undumpable (prctl)", file=sys.stderr)
    try:
        if a.launcher is None:
            xdg = os.environ.get("XDG_RUNTIME_DIR")
            if xdg:
                check_private_dir(Path(xdg))
                a.launcher = Path(xdg) / "arltuner" / "launcher.html"
            else:
                a.launcher = Path(f"/tmp/arltuner-{os.geteuid()}") / "launcher.html"
        make_private_dir(a.launcher.parent)  # created 0700 without parents, then lstat-checked (real dir, ours, no group/other bits)
    except LauncherDirError as e:
        print(f"arltuner-gui: {e}", file=sys.stderr)
        return 2

    pid = signal_running(a.launcher.parent)
    if pid:  # a server is already running: ask it for a fresh single-use launcher instead of starting another
        print(f"server already running (pid {pid}); wrote a fresh launcher shortly:\nopen once: {a.launcher.resolve().as_uri()}")
        if a.open:
            import time
            time.sleep(0.5)
            webbrowser.open(a.launcher.resolve().as_uri())
        return 0
    link = None
    helper_cmd = None if a.no_helper else (a.helper or default_helper_command())
    if helper_cmd:
        _proc, link = spawn_helper(helper_cmd)  # a socketpair, not pipes: no /proc/<pid>/fd reopen by another process of this user (G-142)
    srv = WebServer(Config(web_root=a.web_root), link)
    if srv.bind_note:
        print(f"arltuner-gui: warning: {srv.bind_note}", file=sys.stderr)
    path = write_launcher(a.launcher, srv.origin, srv.token)
    srv.on_bootstrap = lambda: path.unlink(missing_ok=True)
    srv.start()
    pidfile = write_pidfile(a.launcher.parent)
    install_reissue(srv, path)  # SIGUSR1 from a second run: new token + launcher
    url = path.resolve().as_uri()
    print(f"listening on {srv.origin}\nopen once: {url}", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        path.unlink(missing_ok=True)
        pidfile.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
