"""The one place the Windows health gate spawns a process: ``wevtutil.exe`` (read-only event-log query, no shell).

Lives beside ``cli`` for the same reason as ``vfdump.SysfsMeasureIO``: ``health/`` and ``controls/`` are pinned to "no subprocess".
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

_TIMEOUT_S = 30


def run_wevtutil(argv: Sequence[str]) -> tuple[int, str]:
    """Run ``argv`` (``argv[0]`` is ignored: the System32 ``wevtutil.exe`` is used) and return ``(returncode, stdout)``."""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wevtutil.exe")
    done = subprocess.run([exe, *argv[1:]], capture_output=True, timeout=_TIMEOUT_S, check=False)
    return done.returncode, done.stdout.decode("utf-8", "replace")
