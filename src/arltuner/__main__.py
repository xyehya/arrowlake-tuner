"""``python -m arltuner``: entry point of ``arltuner.cli`` (topology / plan / apply / read-vf)."""
from __future__ import annotations

import sys
from typing import TextIO

from arltuner.cli import main


def _tolerant(stream: TextIO) -> None:
    """Never die on an unencodable character (a legacy Windows console cannot print the ratio caveat's em dash)."""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _tolerant(sys.stdout)
    _tolerant(sys.stderr)
    raise SystemExit(main())
