"""Windows health gate: WHEA-Logger events in the System event log, bracketing each mailbox transaction.

The Linux gate (taint word, PCI set, module set) has no Windows counterpart. What Windows records when the CPU/uncore reports a
hardware error is a ``Microsoft-Windows-WHEA-Logger`` event, so this gate remembers the EventRecordIDs present at ``"before"`` and
fails at ``"after"`` if any new one appeared. It fails closed: ``wevtutil`` missing, non-zero exit, unparseable output, an
unknown phase or ``"after"`` without ``"before"`` is ``(False, reason)``.

Limit: the event log is written asynchronously, so an event can land a moment after the transaction. The guard's dwell between
transactions (and the next ``"before"``/``"after"`` pair) is what catches a late one; this gate is not instantaneous.

``run(argv) -> (returncode, stdout_text)`` is required and injected: this package never spawns a process (its source is pinned
read-only by ``tests/health``); the real runner is ``arltuner.wevtutil.run_wevtutil``, wired in by the CLI.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence

__all__ = ["make_whea_health_gate", "WHEA_QUERY"]

WHEA_QUERY = "*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger']]]"
_MAX_EVENTS = 1000  # newest first; a new event is always inside this window
_RECORD_ID = re.compile(r"<EventRecordID>\s*(\d+)\s*</EventRecordID>")
_EVENT_OPEN = re.compile(r"<Event[\s>]")

Runner = Callable[[Sequence[str]], "tuple[int, str]"]


def _whea_ids(run: Runner) -> frozenset[int]:
    rc, text = run(["wevtutil", "qe", "System", f"/q:{WHEA_QUERY}", "/rd:true", f"/c:{_MAX_EVENTS}", "/f:xml"])
    if rc != 0:
        raise RuntimeError(f"wevtutil exited with {rc}")
    ids = frozenset(int(m) for m in _RECORD_ID.findall(text))
    if not ids and _EVENT_OPEN.search(text):
        raise RuntimeError("wevtutil output has events but no EventRecordID (unparseable)")
    return ids


def make_whea_health_gate(run: Runner) -> Callable[[str], tuple[bool, str]]:
    """Build the stateful ``health_gate(phase)`` callable for ``WriteGuard(health_gate=...)``: ``"before"`` records the WHEA
    event ids (replacing any earlier set); ``"after"`` is clean iff no id is new."""
    runner = run
    lock = threading.Lock()
    remembered: list[frozenset[int]] = []

    def gate(phase: str) -> tuple[bool, str]:
        if phase not in ("before", "after"):
            return False, f"unknown health-gate phase {phase!r} (expected 'before' or 'after')"
        with lock:
            try:
                ids = _whea_ids(runner)
            except Exception as exc:  # fail closed: a gate that cannot decide says no
                if phase == "before":
                    remembered.clear()
                return False, f"health gate ({phase}) could not read the WHEA-Logger events: {type(exc).__name__}: {exc}"
            if phase == "before":
                remembered[:] = [ids]
                return True, f"snapshot taken: whea_events={len(ids)}"
            if not remembered:
                return False, "health gate 'after' called without a successful 'before' snapshot"
            new = sorted(ids - remembered[0])
            if new:
                shown = ", ".join(map(str, new[:8])) + (f", +{len(new) - 8} more" if len(new) > 8 else "")
                return False, f"{len(new)} new Microsoft-Windows-WHEA-Logger event(s) in the System log (record ids {shown})"
            return True, f"no new WHEA-Logger events (whea_events={len(ids)})"

    return gate
