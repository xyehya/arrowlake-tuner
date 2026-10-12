#!/usr/bin/env python3
"""orchestrator_confirm.py - run ONE arltuner CLI command and answer its `CONFIRM <token>` prompt with the token it printed.

    sudo env ARLTUNER_ALLOW_MAILBOX=1 USER=$USER python3 -I tools/lnx/orchestrator_confirm.py \
        python3 -I tools/arltuner.py read --device --cpu 8 --cmd 0x10 --allow-taint-bit 2 --audit ~/.local/state/arltuner/audit.jsonl

ORCHESTRATOR-ONLY (AGENTS.md "Hardware safety", docs/SAFETY.md). The typed confirmation exists so a human approves each sitting. This
wrapper is used only by the orchestrator session, only after the operator has given an explicit go-ahead in chat for that sitting
(operator decision 2026-10-01: "do what you have to do and orchestrate this, don't stop at every part"), and the operator is at the machine.
Worker / worktree agents must never run it, and it must never be scheduled or run unattended (no cron, no systemd unit, no boot hook).
Root runs user-writable code here, so Python runs isolated (``-I``: no PYTHON* variables, no user site, no script directory or cwd on sys.path;
tools/arltuner.py adds ./src from its own path): a python command line without ``-I`` gets it inserted (G-142). Longer term, restore from the
installed root-owned tree (docs/ROADMAP.md).
It does not set ARLTUNER_ALLOW_MAILBOX: the caller's command line does, visibly. Output is streamed unchanged; the exit code is the CLI's.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_PROMPT = re.compile(r"CONFIRM (\S+)\n> $")


def isolate(argv: list[str]) -> list[str]:
    """``python3 x.py ...`` -> ``python3 -I x.py ...`` (unless an ``-I`` option is already given); any other program is left alone."""
    if not argv or not re.fullmatch(r"python[0-9.]*", os.path.basename(argv[0])):
        return argv
    for a in argv[1:]:
        if not a.startswith("-"):
            break
        if a == "-I" or (not a.startswith("--") and "I" in a[1:] and a[1] not in "cmW"):
            return argv
    return [argv[0], "-I", *argv[1:]]


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(__doc__)
    argv = isolate(argv)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=0)
    assert proc.stdin is not None and proc.stdout is not None
    tail = ""
    answered = False
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        sys.stdout.write(ch)
        tail = (tail + ch)[-200:]
        match = _PROMPT.search(tail)
        if match and not answered:  # exactly one confirmation per run: a second prompt is never answered
            proc.stdin.write(f"CONFIRM {match.group(1)}\n")
            proc.stdin.flush()
            answered = True
    sys.stdout.flush()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
