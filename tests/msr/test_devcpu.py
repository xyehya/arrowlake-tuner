"""Tests for arltuner.msr.devcpu (T-011). ``DevCpuMsrPort`` is exercised ONLY against fake regular files in temp
directories; the real ``/dev/cpu`` is never opened (see the os.open guard installed by ``tests/msr/__init__.py``)."""
from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arltuner.msr import DevCpuMsrPort, MsrError, devcpu

from . import is_forbidden_device


class OpenRecorder:
    """Wraps ``os.open`` (passing through to the real one) and records ``(path, flags)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self._real = os.open

    def __call__(self, path, flags, *args, **kwargs):
        self.calls.append((os.fspath(path), flags))
        return self._real(path, flags, *args, **kwargs)

    def patch(self):
        return mock.patch("os.open", self)

    @property
    def write_opens(self) -> list[tuple[str, int]]:
        return [c for c in self.calls if c[1] & (os.O_WRONLY | os.O_RDWR)]


class DevCpuCases:
    """Mixin: run once with os.pread/pwrite and once with the lseek fallback (the only path on Windows)."""

    USE_PREAD = True

    def setUp(self):
        patcher = mock.patch.object(devcpu, "_USE_PREAD", self.USE_PREAD)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # registered first, so it runs after any port.close() cleanup
        self.root = Path(tmp.name)

    def make_msr_file(self, cpu: int, values: dict[int, int]) -> Path:
        d = self.root / str(cpu)
        d.mkdir(exist_ok=True)
        path = d / "msr"
        if not path.exists():
            path.write_bytes(b"")
        with open(path, "r+b") as f:
            for msr, value in values.items():
                f.seek(msr)
                f.write(value.to_bytes(8, "little"))
        return path

    def raw(self, cpu: int, msr: int) -> bytes:
        with open(self.root / str(cpu) / "msr", "rb") as f:
            f.seek(msr)
            return f.read(8)

    def port(self, **kwargs) -> DevCpuMsrPort:
        port = DevCpuMsrPort(self.root, **kwargs)
        self.addCleanup(port.close)
        return port

    # ---- reads -------------------------------------------------------------------------------------------------

    def test_rdmsr_reads_little_endian_u64_at_msr_offset(self):
        self.make_msr_file(3, {0x1A2: 0x80691400, 0x150: (1 << 63) | 0x05, 0x1AD: 0x3737373737373939})
        port = self.port()
        self.assertEqual(port.rdmsr(3, 0x1A2), 0x80691400)
        self.assertEqual(port.rdmsr(3, 0x150), (1 << 63) | 0x05)
        self.assertEqual(port.rdmsr(3, 0x1AD), 0x3737373737373939)

    def test_rdmsr_selects_the_cpu_directory(self):
        self.make_msr_file(0, {0x10: 1})
        self.make_msr_file(17, {0x10: 2})
        port = self.port()
        self.assertEqual((port.rdmsr(0, 0x10), port.rdmsr(17, 0x10)), (1, 2))

    def test_rdmsr_missing_cpu_maps_to_enoent(self):
        port = self.port()
        with self.assertRaises(MsrError) as ctx:
            port.rdmsr(5, 0x10)
        self.assertEqual(ctx.exception.errno, errno.ENOENT)
        self.assertIsInstance(ctx.exception, OSError)

    def test_rdmsr_past_end_of_file_is_eio(self):
        self.make_msr_file(0, {0x10: 1})
        with self.assertRaises(MsrError) as ctx:
            self.port().rdmsr(0, 0x2000)
        self.assertEqual(ctx.exception.errno, errno.EIO)

    def test_rdmsr_short_read_is_eio(self):
        path = self.make_msr_file(0, {})
        path.write_bytes(b"\x01\x02\x03")
        with self.assertRaises(MsrError) as ctx:
            self.port().rdmsr(0, 0)
        self.assertEqual(ctx.exception.errno, errno.EIO)

    def test_os_errors_keep_their_errno(self):
        self.make_msr_file(0, {0x10: 1})
        port = self.port()
        for code in (errno.EPERM, errno.EACCES, errno.ENODEV):
            with self.subTest(errno=code), mock.patch("os.open", side_effect=OSError(code, "denied")):
                with self.assertRaises(MsrError) as ctx:
                    port.rdmsr(0, 0x10)
                self.assertEqual(ctx.exception.errno, code)

    def test_arguments_are_validated_before_any_open(self):
        rec = OpenRecorder()
        port = self.port(writable=True)
        with rec.patch():
            for call in (
                lambda: port.rdmsr(-1, 0x10),
                lambda: port.rdmsr(0, 2**32),
                lambda: port.rdmsr(True, 0x10),
                lambda: port.wrmsr(0, 0x10, -1),
                lambda: port.wrmsr(0, 0x10, 2**64),
                lambda: port.wrmsr(0, -5, 1),
            ):
                with self.assertRaises(ValueError):
                    call()
        self.assertEqual(rec.calls, [])

    # ---- writes ------------------------------------------------------------------------------------------------

    def test_writable_port_writes_at_msr_offset(self):
        path = self.make_msr_file(2, {0x150: 0})
        port = self.port(writable=True)
        port.wrmsr(2, 0x150, 0x80000011F9A00000)
        self.assertEqual(self.raw(2, 0x150), (0x80000011F9A00000).to_bytes(8, "little"))
        self.assertEqual(port.rdmsr(2, 0x150), 0x80000011F9A00000)
        self.assertEqual(path.stat().st_size, 0x150 + 8)  # nothing else was written

    def test_write_extends_fake_file_and_leaves_other_registers_alone(self):
        self.make_msr_file(1, {0x10: 0xAAAA})
        port = self.port(writable=True)
        port.wrmsr(1, 0x20, 0xBBBB)
        self.assertEqual(self.raw(1, 0x10), (0xAAAA).to_bytes(8, "little"))
        self.assertEqual(self.raw(1, 0x20), (0xBBBB).to_bytes(8, "little"))

    def test_read_only_port_refuses_wrmsr_before_opening_anything(self):
        path = self.make_msr_file(2, {0x150: 7})
        rec = OpenRecorder()
        port = self.port()
        self.assertFalse(port.writable)
        with rec.patch():
            with self.assertRaises(MsrError) as ctx:
                port.wrmsr(2, 0x150, 0x1234)
        self.assertEqual(ctx.exception.errno, errno.EPERM)
        self.assertEqual(rec.calls, [])  # no open() at all, not even read-only
        self.assertEqual(self.raw(2, 0x150), (7).to_bytes(8, "little"))
        self.assertEqual(path.stat().st_size, 0x150 + 8)

    def test_read_only_port_never_opens_a_file_for_writing(self):
        self.make_msr_file(2, {0x10: 1, 0x150: 7})
        rec = OpenRecorder()
        port = self.port()
        with rec.patch():
            port.rdmsr(2, 0x10)
            port.rdmsr(2, 0x150)
            with self.assertRaises(MsrError):
                port.wrmsr(2, 0x10, 5)
        self.assertEqual(len(rec.calls), 1)  # cached descriptor
        self.assertEqual(rec.write_opens, [])
        self.assertEqual(rec.calls[0][1] & (os.O_WRONLY | os.O_RDWR), os.O_RDONLY)

    def test_writable_port_reads_with_a_readonly_descriptor_and_writes_with_a_separate_rdwr_one(self):
        self.make_msr_file(2, {0x10: 1})
        rec = OpenRecorder()
        port = self.port(writable=True)
        with rec.patch():
            port.rdmsr(2, 0x10)
            port.wrmsr(2, 0x10, 9)
            port.rdmsr(2, 0x10)
            port.wrmsr(2, 0x10, 10)
        self.assertEqual(len(rec.calls), 2)  # one O_RDONLY, one O_RDWR, both cached
        self.assertEqual([bool(flags & os.O_RDWR) for _, flags in rec.calls], [False, True])
        self.assertEqual(len(rec.write_opens), 1)

    def test_only_root_cpu_msr_paths_are_ever_opened(self):
        self.make_msr_file(0, {0x10: 1})
        self.make_msr_file(4, {0x10: 2})
        rec = OpenRecorder()
        port = self.port(writable=True)
        with rec.patch():
            port.rdmsr(0, 0x10)
            port.wrmsr(4, 0x10, 3)
            with self.assertRaises(MsrError):
                port.rdmsr(9, 0x10)  # missing: the attempt is still only root/9/msr
        self.assertEqual(
            sorted(Path(p) for p, _ in rec.calls),
            sorted([self.root / "0" / "msr", self.root / "4" / "msr", self.root / "9" / "msr"]),
        )

    def test_write_open_failure_maps_errno(self):
        self.make_msr_file(2, {0x10: 1})
        port = self.port(writable=True)
        with mock.patch("os.open", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            with self.assertRaises(MsrError) as ctx:
                port.wrmsr(2, 0x10, 1)
        self.assertEqual(ctx.exception.errno, errno.EPERM)

    def test_write_to_missing_cpu_maps_to_enoent(self):
        port = self.port(writable=True)
        with self.assertRaises(MsrError) as ctx:
            port.wrmsr(6, 0x150, 1)
        self.assertEqual(ctx.exception.errno, errno.ENOENT)

    def test_short_write_is_eio(self):
        self.make_msr_file(2, {0x10: 1})
        port = self.port(writable=True)
        with mock.patch.object(devcpu, "_pwrite", return_value=4):
            with self.assertRaises(MsrError) as ctx:
                port.wrmsr(2, 0x10, 1)
        self.assertEqual(ctx.exception.errno, errno.EIO)

    # ---- descriptors -------------------------------------------------------------------------------------------

    def test_descriptors_are_opened_lazily_cached_and_closed(self):
        self.make_msr_file(1, {0x10: 1})
        rec = OpenRecorder()
        with rec.patch():
            port = self.port()
            self.assertEqual(rec.calls, [])  # construction opens nothing
            port.rdmsr(1, 0x10)
            port.rdmsr(1, 0x10)
            self.assertEqual(len(rec.calls), 1)
            port.close()
            port.close()  # idempotent
            self.assertEqual((port._rfds, port._wfds), ({}, {}))
            self.assertEqual(port.rdmsr(1, 0x10), 1)  # reopens lazily
            self.assertEqual(len(rec.calls), 2)

    def test_context_manager_closes(self):
        self.make_msr_file(1, {0x10: 1})
        with DevCpuMsrPort(self.root) as port:
            port.rdmsr(1, 0x10)
            self.assertEqual(len(port._rfds), 1)
        self.assertEqual(port._rfds, {})

    def test_root_accepts_str_and_path(self):
        self.assertEqual(DevCpuMsrPort(str(self.root)).root, self.root)
        self.assertEqual(DevCpuMsrPort(self.root).root, self.root)


@unittest.skipUnless(hasattr(os, "pread") and hasattr(os, "pwrite"), "os.pread/os.pwrite are POSIX-only")
class TestDevCpuPread(DevCpuCases, unittest.TestCase):
    USE_PREAD = True


class TestDevCpuSeekFallback(DevCpuCases, unittest.TestCase):
    USE_PREAD = False


class TestPreadPathArguments(unittest.TestCase):
    """The os.pread/os.pwrite branch, exercised on every platform with fakes (real ones are POSIX-only)."""

    def test_pread_and_pwrite_get_fd_length_and_msr_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "0").mkdir()
            (root / "0" / "msr").write_bytes(bytes(0x200))
            calls = []

            def fake_pread(fd, n, offset):
                calls.append(("pread", n, offset))
                return (0x1122334455667788).to_bytes(8, "little")

            def fake_pwrite(fd, data, offset):
                calls.append(("pwrite", bytes(data), offset))
                return len(data)

            with mock.patch.object(devcpu, "_USE_PREAD", True), mock.patch.object(
                os, "pread", fake_pread, create=True
            ), mock.patch.object(os, "pwrite", fake_pwrite, create=True):
                with DevCpuMsrPort(root, writable=True) as port:
                    self.assertEqual(port.rdmsr(0, 0x1A2), 0x1122334455667788)
                    port.wrmsr(0, 0x150, 0x80000011F9A00000)
        self.assertEqual(
            calls,
            [("pread", 8, 0x1A2), ("pwrite", (0x80000011F9A00000).to_bytes(8, "little"), 0x150)],
        )


class TestNeverTouchesTheRealDevice(unittest.TestCase):
    """The suite must never open the default root (/dev/cpu)."""

    def test_default_root_is_dev_cpu_and_read_only(self):
        port = DevCpuMsrPort()
        self.assertEqual(port.root, Path("/dev/cpu"))
        self.assertFalse(port.writable)

    def test_constructing_and_closing_default_port_opens_nothing(self):
        with mock.patch("os.open", side_effect=AssertionError("os.open must not be called")):
            port = DevCpuMsrPort()
            port.close()
            with DevCpuMsrPort(writable=True):
                pass

    def test_default_read_only_port_refuses_writes_without_opening(self):
        with mock.patch("os.open", side_effect=AssertionError("os.open must not be called")):
            with self.assertRaises(MsrError) as ctx:
                DevCpuMsrPort().wrmsr(0, 0x150, 1)
        self.assertEqual(ctx.exception.errno, errno.EPERM)

    def test_suite_guard_blocks_real_device_paths(self):
        for path in ("/dev/cpu/0/msr", "/dev/cpu", "/dev/mem", "/dev/kmem", "/dev/port", "\\dev\\cpu\\3\\msr"):
            with self.subTest(path=path):
                self.assertTrue(is_forbidden_device(path))
                with self.assertRaises(AssertionError):
                    os.open(path, os.O_RDONLY)
        for path in (str(Path(tempfile.gettempdir()) / "msr"), "/dev/null", "/dev/cpuinfo-not-a-device"):
            self.assertFalse(is_forbidden_device(path), path)


if __name__ == "__main__":
    unittest.main()
