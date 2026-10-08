"""Tests for ``arltuner.msr`` (T-011). No test may open a real ``/dev/cpu/*`` or ``/dev/mem``.

Importing this package installs a guard on ``os.open`` (the only call ``DevCpuMsrPort`` uses): any attempt to open
``/dev/cpu``, ``/dev/mem``, ``/dev/kmem`` or ``/dev/port`` raises ``AssertionError`` instead of reaching the OS. All other
paths (the temp-dir fake files) pass straight through. ``test_devcpu.py`` verifies the guard is active.
"""
from __future__ import annotations

import os

FORBIDDEN_PREFIXES = ("/dev/cpu", "/dev/mem", "/dev/kmem", "/dev/port")


def is_forbidden_device(path: object) -> bool:
    try:
        norm = os.fsdecode(path).replace("\\", "/")  # type: ignore[arg-type]
    except TypeError:  # an int fd or something else: not a path
        return False
    return any(norm == p or norm.startswith(p + "/") for p in FORBIDDEN_PREFIXES)


def _install_device_guard() -> None:
    if getattr(os.open, "_arltuner_msr_guard", False):
        return
    real_open = os.open

    def guarded_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_forbidden_device(path):
            raise AssertionError(f"test suite attempted to open a real device: {path!r}")
        return real_open(path, *args, **kwargs)

    guarded_open._arltuner_msr_guard = True  # type: ignore[attr-defined]
    os.open = guarded_open  # type: ignore[assignment]


_install_device_guard()
