"""Fakes for telemetry tests: a dict-backed MSR port (read-only use) and a manual clock. No hardware, no real /sys."""
from __future__ import annotations

import errno

from arltuner.msr.port import MsrError


class FakeMsrPort:
    writable = False

    def __init__(self, values: dict[tuple[int, int], int] | None = None, default: dict[int, int] | None = None) -> None:
        self.values = dict(values or {})
        self.default = dict(default or {})
        self.reads: list[tuple[int, int]] = []

    def rdmsr(self, cpu: int, msr: int) -> int:
        self.reads.append((cpu, msr))
        if (cpu, msr) in self.values:
            return self.values[(cpu, msr)]
        if msr in self.default:
            return self.default[msr]
        raise MsrError(errno.EIO, f"fake: msr {msr:#x} unimplemented")

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:  # pragma: no cover - must never be called
        raise AssertionError("telemetry wrote an MSR")


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t
