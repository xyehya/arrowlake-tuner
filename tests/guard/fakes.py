"""Test doubles for the T-013 guard tests. No hardware, no ``arltuner.msr`` import (another task owns that package)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from arltuner.mailbox import Response

#: write cmd -> the read cmd that returns the word it writes (registers.md: 0x10/0x11, 0x14/0x15, 0x1C/0x1D, 0x26/0x27 pairs)
READ_OF_WRITE = {0x11: 0x10, 0x15: 0x14, 0x1D: 0x1C, 0x27: 0x26}


class FakeMailboxTimeout(Exception):
    """Stands in for ``arltuner.msr.MailboxTimeout`` (Contract 4); the guard must treat *any* transport exception alike."""


class FakeMsrError(OSError):
    """Stands in for ``arltuner.msr.MsrError``."""


class FakeClock:
    """Callable clock whose time only moves when a test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    """Structural ``Transport``: records every call, models the per-(cpu, p1, p2) words behind cmd 0x10/0x14 reads.

    ``calls`` is the ground truth for "was anything sent": a refusal must leave it unchanged.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, int, int]] = []  # (cpu, cmd, p1, p2, data)
        self.state: dict[tuple[int, int, int, int], int] = {}  # (cpu, read cmd, p1, p2) -> u32 word
        self.write_rc = 0
        self.read_rc = 0
        self.apply_write: Callable[[int], int] = lambda data: data  # what the "hardware" stores for a written word
        self.raise_on_call: dict[int, BaseException] = {}  # 1-based call number -> exception to raise instead
        self.rc_on_call: dict[int, int] = {}  # 1-based call number -> forced rc
        self.data_on_call: dict[int, Any] = {}  # 1-based call number -> forced response data (any object)
        self.before_return: Callable[[tuple[int, int, int, int, int]], None] | None = None  # hook, runs inside transact

    def transact(self, cpu: int, cmd: int, p1: int, p2: int, data: int = 0) -> Response:
        call = (cpu, cmd, p1, p2, data)
        self.calls.append(call)
        number = len(self.calls)
        if self.before_return is not None:
            self.before_return(call)
        if number in self.raise_on_call:
            raise self.raise_on_call[number]
        if cmd in READ_OF_WRITE:
            rc = self.rc_on_call.get(number, self.write_rc)
            if rc == 0:
                self.state[(cpu, READ_OF_WRITE[cmd], p1, p2)] = self.apply_write(data)
            response = Response(rc, 0)
        else:
            rc = self.rc_on_call.get(number, self.read_rc)
            response = Response(rc, self.state.get((cpu, cmd, p1, p2), 0))
        if number in self.data_on_call:
            return Response(response.rc, self.data_on_call[number])  # type: ignore[arg-type]
        return response
