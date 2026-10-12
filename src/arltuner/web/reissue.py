"""Way back in without restarting the server: a second ``arltuner-gui.py`` run signals the running one (SIGUSR1), which mints a fresh
single-use bootstrap token and rewrites the launcher file. No network control endpoint; the pid file lives in the private launcher directory
(see ``launcher.make_private_dir``), so only the same user can place or replace it."""
from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from typing import Callable

from .launcher import write_launcher
from .server import WebServer

PID_NAME = "server.pid"


def write_pidfile(directory: Path) -> Path:
    p = Path(directory) / PID_NAME
    tmp = p.with_name(PID_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()}\n")
    os.replace(tmp, p)
    return p


def _is_server_argv(argv: list[bytes], marker: bytes) -> bool:
    """True for ``[python*, option..., <path>/<marker>.py, ...]`` or ``[<path>/<marker>.py, ...]``: the script is the program, not a data
    argument of some other command (``vim tools/arltuner-gui.py`` must not receive our SIGUSR1)."""
    if not argv:
        return False
    names = (marker, marker + b".py")
    base = argv[0].rsplit(b"/", 1)[-1]
    if base in names:
        return True
    if not (base == b"python" or base.startswith(b"python3")):
        return False
    for a in argv[1:]:
        if a.startswith(b"-"):
            continue
        return a.rsplit(b"/", 1)[-1] in names
    return False


def signal_running(directory: Path, marker: bytes = b"arltuner-gui", proc_root: Path = Path("/proc")) -> int | None:
    """Pid of a live server recorded in ``directory`` that was sent SIGUSR1, else None (no file, stale pid, or the pid is some other program;
    the recorded pid's argv must name ``marker`` as the program, an exact match, not a substring)."""
    try:
        pid = int((Path(directory) / PID_NAME).read_text().strip())
        if pid <= 1:
            return None
        argv = (Path(proc_root) / str(pid) / "cmdline").read_bytes().split(b"\0")
        if argv and argv[-1] == b"":
            argv.pop()
        if not _is_server_argv(argv, marker):
            return None
        os.kill(pid, signal.SIGUSR1)
        return pid
    except (OSError, ValueError):
        return None


def install_reissue(srv: WebServer, launcher_path: Path, on_reissue: Callable[[Path], None] | None = None) -> Callable[[], None]:
    """SIGUSR1 -> new token + new launcher file (the work runs in a thread, not in the signal handler). Returns an uninstall function."""
    ev = threading.Event()
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            if not ev.wait(0.2):
                continue
            ev.clear()
            try:
                path = write_launcher(launcher_path, srv.origin, srv.new_bootstrap())
                if on_reissue:
                    on_reissue(path)
            except Exception:  # never let a bad directory kill the server
                pass

    threading.Thread(target=work, name="web-reissue", daemon=True).start()
    previous = signal.signal(signal.SIGUSR1, lambda *_: ev.set())

    def uninstall() -> None:
        stop.set()
        signal.signal(signal.SIGUSR1, previous)

    return uninstall
