"""Shared test helpers for tests/msr: a fake clock and a scriptable fake port. No hardware, no real sleeping."""
from __future__ import annotations

BUSY = 1 << 63


def resp(rc: int, data: int) -> int:
    """A completed (non-busy) mailbox response word."""
    return (rc << 32) | data


class FakeClock:
    """``sleep`` advances ``clock`` by exactly the requested time, so polling loops run instantly and deterministically."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) > 100_000:  # a broken timeout must fail the test, not hang the suite
            raise AssertionError("FakeClock: polling loop never terminated")


class ScriptedPort:
    """Fake ``MsrPort``: ``rdmsr`` answers from ``reads`` in order (an Exception item is raised), then repeats ``then``
    (or fails the test if ``then`` is None). Every call is recorded in ``ops`` as ('r', cpu, msr) / ('w', cpu, msr, value)."""

    writable = True

    def __init__(self, reads: list[object], *, then: int | None = None, write_error: Exception | None = None) -> None:
        self._reads = list(reads)
        self._then = then
        self._write_error = write_error
        self.ops: list[tuple] = []

    def rdmsr(self, cpu: int, msr: int) -> int:
        self.ops.append(("r", cpu, msr))
        if self._reads:
            item = self._reads.pop(0)
        elif self._then is not None:
            item = self._then
        else:
            raise AssertionError("ScriptedPort: rdmsr script exhausted")
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, int)
        return item

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:
        self.ops.append(("w", cpu, msr, value))
        if self._write_error is not None:
            raise self._write_error

    @property
    def writes(self) -> list[tuple]:
        return [op for op in self.ops if op[0] == "w"]

    @property
    def reads(self) -> list[tuple]:
        return [op for op in self.ops if op[0] == "r"]
