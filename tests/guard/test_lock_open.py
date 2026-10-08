"""Lock and audit opens in a directory others may write (``/run/lock`` is 1777): O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, then fstat on the descriptor
(regular file, one link). A FIFO, symlink or hard-linked alias is a named refusal and never a hang, never read, never followed.
Every test runs under a 15 s alarm: a regression that blocks on a FIFO fails instead of hanging the suite. POSIX only."""
from __future__ import annotations

import os
import signal
import unittest

from arltuner.guard import AuditLog, Refusal, read_state

from . import test_guard as tg

POSIX = os.name == "posix"


class _Timeout(Exception):
    pass


def _alarm(signum, frame):  # type: ignore[no-untyped-def]
    raise _Timeout("blocked (a FIFO was opened for a blocking read?)")


@unittest.skipUnless(POSIX, "FIFOs, symlinks and hard links")
class LockOpenTests(tg.GuardCase):
    def setUp(self) -> None:
        super().setUp()
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(15)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        self.addCleanup(signal.alarm, 0)
        self.target = self.dir / "target"
        self.target.write_text("precious\n")

    def locked(self) -> str:
        exc = self.assert_refused(self.make_guard(), self.read_req(), Refusal.LOCKED)
        return str(exc)

    def test_fifo_at_the_lock_path_is_a_refusal_not_a_hang(self) -> None:
        os.mkfifo(self.lock_path)
        message = self.locked()
        self.assertIn("not a plain regular file", message)

    def test_symlink_at_the_lock_path_is_refused_and_not_followed(self) -> None:
        self.lock_path.symlink_to(self.target)
        message = self.locked()
        self.assertNotIn("precious", message)
        self.assertEqual(self.target.read_text(), "precious\n")
        self.assertTrue(self.lock_path.is_symlink())

    def test_dangling_symlink_at_the_lock_path_is_not_created_through(self) -> None:
        ghost = self.dir / "ghost"
        self.lock_path.symlink_to(ghost)
        self.locked()
        self.assertFalse(ghost.exists())

    def test_hard_linked_lock_file_is_not_read(self) -> None:
        os.link(self.target, self.lock_path)
        message = self.locked()
        self.assertNotIn("precious", message)
        self.assertIn("not a plain regular file", message)
        self.assertEqual(self.target.read_text(), "precious\n")

    def test_holder_text_is_capped_at_32_bytes_and_printable_only(self) -> None:
        self.lock_path.write_bytes(b"12345\x1b[31m\x00\xff" + b"A" * 100)
        message = self.locked()
        self.assertIn("held by pid ", message)
        holder = message.split("held by pid ", 1)[1].rstrip(")")
        self.assertLessEqual(len(holder), 32)
        self.assertTrue(all(" " <= c <= "~" for c in holder), holder)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("A" * 33, message)

    def test_a_normal_lock_still_reports_its_pid(self) -> None:
        self.lock_path.write_text("999\n")
        self.assertIn("held by pid 999", self.locked())


@unittest.skipUnless(POSIX, "FIFOs, symlinks and hard links")
class AuditOpenTests(tg.GuardCase):
    def setUp(self) -> None:
        super().setUp()
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(15)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        self.addCleanup(signal.alarm, 0)

    def test_fifo_at_the_audit_path_is_refused_without_blocking(self) -> None:
        os.mkfifo(self.audit_path)
        with self.assertRaises(OSError):  # ENXIO: no reader, and O_NONBLOCK keeps the open from waiting for one
            AuditLog(self.audit_path, fsync=False)
        with self.assertRaises(OSError):
            read_state(self.audit_path)

    def test_fifo_with_a_reader_attached_still_gets_no_record(self) -> None:
        os.mkfifo(self.audit_path)
        reader = os.open(self.audit_path, os.O_RDONLY | os.O_NONBLOCK)  # a reader makes a write-open succeed: the fstat is what refuses
        self.addCleanup(os.close, reader)
        with self.assertRaises(OSError) as ctx:
            AuditLog(self.audit_path, fsync=False)
        self.assertIn("not a regular file", str(ctx.exception))
        self.assertEqual(os.read(reader, 100), b"")  # nothing was ever written into the pipe (EOF: the refused writer is closed)

    def test_hard_linked_audit_log_is_refused_and_the_target_untouched(self) -> None:
        target = self.dir / "target"
        target.write_text("precious\n")
        os.link(target, self.audit_path)
        with self.assertRaises(OSError) as ctx:
            AuditLog(self.audit_path, fsync=False)
        self.assertIn("hard links", str(ctx.exception))
        with self.assertRaises(OSError):
            read_state(self.audit_path)
        self.assertEqual(target.read_text(), "precious\n")

    def test_symlink_at_the_audit_path_is_still_refused(self) -> None:
        target = self.dir / "target"
        target.write_text("precious\n")
        self.audit_path.symlink_to(target)
        with self.assertRaises(OSError):
            AuditLog(self.audit_path, fsync=False)
        self.assertEqual(target.read_text(), "precious\n")

    def test_normal_log_is_unchanged(self) -> None:
        log = AuditLog(self.audit_path, fsync=False)
        log.append("refused", reason="X")
        self.assertEqual(AuditLog(self.audit_path, fsync=False).append("refused", reason="Y")["seq"], 1)
        self.assertEqual(read_state(self.audit_path).last_attempt_wall, None)


if __name__ == "__main__":
    unittest.main()
