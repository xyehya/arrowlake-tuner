#!/usr/bin/env python3
"""orchestrator_confirm.py - run ONE arltuner CLI command and answer its `CONFIRM <token>` prompt with the token it printed.

    sudo env ARLTUNER_ALLOW_MAILBOX=1 USER=yk python3 tools/lnx/orchestrator_confirm.py \
        python3 tools/arltuner.py read --device --cpu 8 --cmd 0x10 --allow-taint-bit 2 --audit /home/yk/.local/state/linuxmon/audit.jsonl

ORCHESTRATOR-ONLY (AGENTS.md "Hardware safety", docs/SAFETY.md). The typed confirmation exists so a human approves each sitting. This
wrapper is used only by the orchestrator session, only after the operator has given an explicit go-ahead in chat for that sitting
(operator decision 2026-10-01: "do what you have to do and orchestrate this, don't stop at every part"), and the operator is at the machine.
Worker / worktree agents must never run it, and it must never be scheduled or run unattended (no cron, no systemd unit, no boot hook).
It does not set ARLTUNER_ALLOW_MAILBOX: the caller's command line does, visibly. Output is streamed unchanged; the exit code is the CLI's.
"""

from __future__ import annotations

import re
import subprocess
import sys

_PROMPT = re.compile(r"CONFIRM (\S+)\n> $")


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(__doc__)
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
