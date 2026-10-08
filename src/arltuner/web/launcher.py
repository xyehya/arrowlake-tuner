"""Launcher page: a 0600 file:// page that carries the one-time bootstrap token (never argv, never a URL that reaches history)."""
from __future__ import annotations

import html
import os
from pathlib import Path


def write_launcher(path: Path, origin: str, token: str) -> Path:
    """Create ``path`` (parent dir 0700 if we create it) with mode 0600, replacing any older file. The page has a single POST form."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    body = (
        '<!doctype html>\n<meta charset="utf-8"><title>arrowlake-tuner launcher</title>\n'
        f'<form method="post" action="{html.escape(origin, quote=True)}/bootstrap">\n'
        f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">\n'
        '<button type="submit">Open arrowlake-tuner</button>\n</form>\n'
        '<p>One-time link. It stops working after the first use.</p>\n'
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return path
