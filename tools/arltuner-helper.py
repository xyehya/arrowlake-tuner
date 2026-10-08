#!/usr/bin/env python3
"""Run the arltuner root helper from a checkout:  python3 tools/arltuner-helper.py --sim [--policy F --audit F --lock F]

JSON lines on stdin/stdout (docs/ARCHITECTURE.md contract 8). ``--sim`` is the simulator and touches no hardware. ``--device`` refuses to start
from a checkout (SPEC 4.4: helper code is never run from a user-writable tree) and needs root; the installed copy is started by pkexec (G-106).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arltuner.helper.main import process_main  # noqa: E402

if __name__ == "__main__":
    process_main()
