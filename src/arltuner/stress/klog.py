"""New MCE / hardware-error kernel-log lines during a stress run (G-116 health bracket).

Primary source ``/dev/kmsg`` [DOC Documentation/ABI/testing/dev-kmsg]: a non-blocking reader sees every record in the ring buffer, each with a monotonically
increasing sequence number; the *mark* is the highest sequence number present at the start and the run's lines are the records after it. Fallback
``journalctl -k -b -q -o short-monotonic`` (root-owned absolute path, fixed environment): the mark is the line count at the start. Neither is ever written.
The pattern is the one of ``tools/lnx/percore_stress.py`` (BAD_KLOG) narrowed to machine-check / hardware-error text; the expected "Write to unrecognized MSR"
notice is excluded.
"""
from __future__ import annotations

import errno
import os
import re
import subprocess
from collections.abc import Callable
from typing import Any

__all__ = ["BAD_KLOG", "JOURNALCTL", "JournalLog", "KernelLogWatch", "KmsgLog", "NullLog", "count_bad"]

BAD_KLOG = re.compile(r"\bmce\b|machine check|hardware error", re.I)
JOURNALCTL = "/usr/bin/journalctl"
_MAX_RECORDS = 500_000  # a bound on one drain (the ring buffer holds far fewer)
_MAX_JOURNAL_BYTES = 64 << 20


def count_bad(lines: list[str]) -> int:
    return sum(1 for line in lines if BAD_KLOG.search(line) and "Write to unrecognized MSR" not in line)


class NullLog:
    """No kernel log at all (the simulator): nothing new, ever."""

    def mark(self) -> int:
        return 0

    def new_lines(self, mark: int) -> list[str]:
        return []


class KmsgLog:
    def __init__(self, path: str = "/dev/kmsg", *, open_fn: Callable[..., int] = os.open, read_fn: Callable[[int, int], bytes] = os.read,
                 close_fn: Callable[[int], None] = os.close) -> None:
        self._path, self._open, self._read, self._close = path, open_fn, read_fn, close_fn

    def _drain(self) -> list[tuple[int, str]]:
        fd = self._open(self._path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        records: list[tuple[int, str]] = []
        try:
            for _ in range(_MAX_RECORDS):
                try:
                    raw = self._read(fd, 8192)
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        break
                    if exc.errno == errno.EPIPE:  # records were overwritten under us: the next read continues with the oldest one left
                        continue
                    raise
                if not raw:
                    break
                head, sep, body = raw.decode("utf-8", "replace").partition(";")
                fields = head.split(",")
                if not sep or len(fields) < 2 or not fields[1].isdigit():
                    continue
                records.append((int(fields[1]), body.split("\n", 1)[0]))
        finally:
            self._close(fd)
        return records

    def mark(self) -> int:
        records = self._drain()
        return max((seq for seq, _ in records), default=-1)

    def new_lines(self, mark: int) -> list[str]:
        return [text for seq, text in self._drain() if seq > mark]


class JournalLog:
    def __init__(self, *, run: Callable[..., Any] = subprocess.run, exe: str = JOURNALCTL, trust_check: Callable[[str], None] | None = None) -> None:
        self._run, self._exe, self._trust = run, exe, trust_check

    def _lines(self) -> list[str]:
        exe = os.path.realpath(self._exe)
        if self._trust is not None:
            self._trust(exe)
        done = self._run(
            [exe, "-k", "-b", "-q", "-o", "short-monotonic"], cwd="/", env={"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False, shell=False,
        )
        if done.returncode != 0:
            raise OSError(f"journalctl -k exited {done.returncode}")
        return bytes(done.stdout[:_MAX_JOURNAL_BYTES]).decode("utf-8", "replace").splitlines()

    def mark(self) -> int:
        return len(self._lines())

    def new_lines(self, mark: int) -> list[str]:
        return self._lines()[mark:]


class KernelLogWatch:
    """Marks the first usable source at ``begin()`` and gives the lines after the mark at ``new_lines()``. ``available`` is False when none could be read."""

    def __init__(self, sources: list[Any]) -> None:
        self._sources = sources
        self._source: Any = None
        self._mark: int = 0
        self.available = False
        self.why = "no kernel log source"

    def begin(self) -> None:
        problems = []
        for source in self._sources:
            try:
                self._mark = source.mark()
            except Exception as exc:  # best effort by design: an untrusted journalctl (TrustError), a missing device, a timeout all mean "not readable"
                problems.append(f"{type(source).__name__}: {getattr(exc, 'strerror', None) or exc}")
                continue
            self._source, self.available = source, True
            return
        self.why = "; ".join(problems) or self.why

    def new_lines(self) -> list[str]:
        if self._source is None:
            return []
        try:
            return list(self._source.new_lines(self._mark))
        except Exception as exc:
            self.available, self.why = False, f"{type(self._source).__name__}: {getattr(exc, 'strerror', None) or exc}"
            return []
