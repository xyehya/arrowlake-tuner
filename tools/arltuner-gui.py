#!/usr/bin/env python3
"""Start the local web UI:  python3 tools/arltuner-gui.py [--helper CMD ...] [--launcher PATH] [--open]

Writes a 0600 launcher page (token inside) and prints its file:// URL; open it once and click the button. ``--helper`` is the command that
speaks the helper protocol (Contract 8) on stdin/stdout; without it the server runs with no helper (API calls answer 503). This script never
elevates and never starts the root helper by itself.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arltuner.web.helperlink import HelperLink  # noqa: E402
from arltuner.web.launcher import write_launcher  # noqa: E402
from arltuner.web.server import Config, WebServer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--helper", nargs=argparse.REMAINDER, help="helper command (rest of the line)")
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/arltuner-{os.getuid()}"
    ap.add_argument("--launcher", type=Path, default=Path(rt) / "arltuner" / "launcher.html")
    ap.add_argument("--web-root", type=Path, default=ROOT / "web")
    ap.add_argument("--open", action="store_true", help="open the launcher in the default browser")
    a = ap.parse_args(argv)

    link = None
    if a.helper:
        proc = subprocess.Popen(a.helper, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        assert proc.stdin and proc.stdout
        link = HelperLink(proc.stdout, proc.stdin).start()
    srv = WebServer(Config(web_root=a.web_root), link)
    path = write_launcher(a.launcher, srv.origin, srv.token)
    srv.on_bootstrap = lambda: path.unlink(missing_ok=True)
    srv.start()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
