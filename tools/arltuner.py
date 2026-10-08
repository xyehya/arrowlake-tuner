#!/usr/bin/env python3
"""Run the arltuner CLI without installing it:  python3 tools/arltuner.py <command> ...

Puts ./src on sys.path and calls ``arltuner.cli.main``. Commands: topology, plan, apply, read-vf (see docs/tasks/T-014-controls-cli.md).
Anything that talks to real hardware needs ``--device``, ``ARLTUNER_ALLOW_MAILBOX=1`` exported by YOU, and an interactive confirmation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arltuner.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
