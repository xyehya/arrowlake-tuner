"""The real MSR port: ``/dev/cpu/N/msr`` (Linux ``msr`` driver). Written here, exercised by the suite ONLY against fake
files in temp directories (docs/tasks/T-011); the suite never opens the default root.

Facts used (``docs/research/registers.md`` "MSRs" table, "OC mailbox -- request model"): an MSR access is a 64-bit
little-endian ``pread``/``pwrite`` at file offset = MSR number on ``<root>/<cpu>/msr``; every OC-mailbox request, reads
included, is a WRMSR (hardware-write tier, docs/SAFETY.md). Therefore:

* the port is read-only unless constructed ``writable=True``; a read-only port refuses ``wrmsr`` with ``MsrError(EPERM)``
  BEFORE opening anything;
* reads always use a separate ``O_RDONLY`` descriptor, even on a writable port; the ``O_RDWR`` descriptor is only ever
  opened by ``wrmsr``;
* it never loads kernel modules and never touches any path except ``<root>/<cpu>/msr``.

``os.pread``/``os.pwrite`` are POSIX-only; where they are missing (Windows, where the suite also runs against fake files)
a lock-protected ``lseek`` + ``read``/``write`` fallback is used. Import has no side effects.
"""
from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from types import TracebackType

from .port import MsrError, MsrPort, check_cpu, check_msr, check_value

_USE_PREAD = hasattr(os, "pread") and hasattr(os, "pwrite")
_SEEK_LOCK = threading.Lock()  # lseek+read/write fallback is not atomic per fd
_OPEN_EXTRA = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
_MSR_SIZE = 8


def _pread(fd: int, length: int, offset: int) -> bytes:
    if _USE_PREAD:
        return os.pread(fd, length, offset)
    with _SEEK_LOCK:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)


def _pwrite(fd: int, data: bytes, offset: int) -> int:
    if _USE_PREAD:
        return os.pwrite(fd, data, offset)
    with _SEEK_LOCK:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, data)


def _as_msr_error(exc: OSError, what: str) -> MsrError:
    code = exc.errno if isinstance(exc.errno, int) and exc.errno else errno.EIO
    return MsrError(code, f"{what}: {exc.strerror or exc}")


class DevCpuMsrPort(MsrPort):
    """``MsrPort`` over ``<root>/<cpu>/msr`` with lazily opened, cached descriptors (``close()`` / ``with`` releases them)."""

    def __init__(self, root: Path | str = "/dev/cpu", *, writable: bool = False) -> None:
        self.root = Path(root)
        self.writable = bool(writable)
        self._rfds: dict[int, int] = {}
        self._wfds: dict[int, int] = {}

    def _fd(self, cpu: int, *, write: bool) -> int:
        cache = self._wfds if write else self._rfds
        fd = cache.get(cpu)
        if fd is None:
            path = self.root / str(cpu) / "msr"
            flags = (os.O_RDWR if write else os.O_RDONLY) | _OPEN_EXTRA
            try:
                fd = os.open(os.fspath(path), flags)
            except OSError as exc:
                raise _as_msr_error(exc, f"open {path}") from exc
            cache[cpu] = fd
        return fd

    def rdmsr(self, cpu: int, msr: int) -> int:
        """Read MSR ``msr`` on ``cpu`` (``O_RDONLY`` descriptor, 8-byte pread at offset ``msr``)."""
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        fd = self._fd(cpu, write=False)
        try:
            raw = _pread(fd, _MSR_SIZE, msr)
        except OSError as exc:
            raise _as_msr_error(exc, f"rdmsr cpu={cpu} msr={msr:#x}") from exc
        if len(raw) != _MSR_SIZE:
            raise MsrError(errno.EIO, f"rdmsr cpu={cpu} msr={msr:#x}: short read ({len(raw)} bytes)")
        return int.from_bytes(raw, "little")

    def wrmsr(self, cpu: int, msr: int, value: int) -> None:
        """Write MSR ``msr`` on ``cpu``. Read-only port: ``MsrError(EPERM)`` and nothing is opened."""
        cpu = check_cpu(cpu)
        msr = check_msr(msr)
        value = check_value(value)
        if not self.writable:
            raise MsrError(errno.EPERM, f"wrmsr cpu={cpu} msr={msr:#x}: port is read-only")
        fd = self._fd(cpu, write=True)
        try:
            written = _pwrite(fd, value.to_bytes(_MSR_SIZE, "little"), msr)
        except OSError as exc:
            raise _as_msr_error(exc, f"wrmsr cpu={cpu} msr={msr:#x}") from exc
        if written != _MSR_SIZE:
            raise MsrError(errno.EIO, f"wrmsr cpu={cpu} msr={msr:#x}: short write ({written} bytes)")

    def close(self) -> None:
        """Close every cached descriptor (idempotent). The port may be used again; it reopens lazily."""
        for cache in (self._rfds, self._wfds):
            for fd in cache.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            cache.clear()

    def __enter__(self) -> DevCpuMsrPort:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
