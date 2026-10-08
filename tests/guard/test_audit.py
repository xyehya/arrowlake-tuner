"""T-013 audit-log tests: append-only JSON lines, never truncating, no symlinks, fsync after each record."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arltuner.guard import AuditLog, Request, read_all
from arltuner.mailbox import Response

REQ = Request(cpu=3, cmd=0x11, p1=0, p2=0, data=0x0004A02D, purpose="unit test")


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "audit.jsonl"

    def test_record_format(self) -> None:
        log = AuditLog(self.path, fsync=False)
        rec = log.append(
            "result", ts=1_700_000_000.25, request=REQ, operator="yk", response=Response(3, 0x1F), baseline=0x4A037, stage="x"
        )
        self.assertEqual(
            rec,
            {
                "ts": "2023-11-14T22:13:20.250+00:00",
                "seq": 0,
                "event": "result",
                "cpu": 3,
                "cmd": "0x11",
                "p1": "0x00",
                "p2": "0x00",
                "req_data": "0x0004A02D",
                "purpose": "unit test",
                "operator": "yk",
                "rc": 3,
                "rc_name": "RatioExceedsLimits",
                "data": "0x0000001F",
                "baseline": "0x0004A037",
                "stage": "x",
            },
        )
        self.assertEqual(read_all(self.path), [rec])

    def test_lf_only_one_object_per_line(self) -> None:
        log = AuditLog(self.path, fsync=False)
        log.append("attempt", request=REQ)
        log.append("refused", reason="DWELL", detail="line\r\nbreak in detail")
        raw = self.path.read_bytes()
        self.assertNotIn(b"\r", raw)  # no CRLF translation on Windows, and control characters are escaped
        self.assertEqual(raw.count(b"\n"), 2)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual([r["seq"] for r in read_all(self.path)], [0, 1])
        self.assertEqual(read_all(self.path)[1]["detail"], "line\r\nbreak in detail")

    def test_fsync_per_record(self) -> None:
        with mock.patch("arltuner.guard.audit.os.fsync") as fsync:
            log = AuditLog(self.path, fsync=True)
            log.append("attempt", request=REQ)
            log.append("result", request=REQ, response=Response(0, 0))
            self.assertEqual(fsync.call_count, 2)
        with mock.patch("arltuner.guard.audit.os.fsync") as fsync:
            AuditLog(self.dir / "b.jsonl", fsync=False).append("attempt", request=REQ)
            fsync.assert_not_called()

    def test_never_truncates_and_appends_across_instances(self) -> None:
        self.path.write_bytes(b'{"event":"trip","legacy":true}\n')
        first = AuditLog(self.path, fsync=False)
        first.append("attempt", request=REQ)
        second = AuditLog(self.path, fsync=False)  # a later session on the same file
        second.append("result", request=REQ, response=Response(0, 0))
        records = read_all(self.path)
        self.assertEqual([r["event"] for r in records], ["trip", "attempt", "result"])
        self.assertTrue(records[0]["legacy"])
        self.assertEqual([r["seq"] for r in records[1:]], [1, 2])  # seq continues across instances

    def test_opening_an_existing_log_does_not_modify_it(self) -> None:
        body = b'{"event":"trip"}\n{"event":"trip"}\n'
        self.path.write_bytes(body)
        AuditLog(self.path)
        self.assertEqual(self.path.read_bytes(), body)

    def test_unterminated_last_line_stays_isolated(self) -> None:
        self.path.write_bytes(b'{"event":"trip"}\n{"event":"tr')  # crash mid-write
        AuditLog(self.path, fsync=False).append("attempt", request=REQ)
        lines = self.path.read_bytes().split(b"\n")
        self.assertEqual(lines[1], b'{"event":"tr')  # the damaged line is untouched, on its own line
        self.assertIn(b'"event":"attempt"', lines[2])

    def test_symlink_refused(self) -> None:
        target = self.dir / "target.jsonl"
        target.write_bytes(b"")
        link = self.dir / "link.jsonl"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        with self.assertRaises(OSError):
            AuditLog(link)
        self.assertEqual(target.read_bytes(), b"")

    def test_symlink_swapped_in_later_is_refused(self) -> None:
        log = AuditLog(self.path, fsync=False)
        log.append("attempt", request=REQ)
        target = self.dir / "target.jsonl"
        target.write_bytes(b"")
        self.path.unlink()
        try:
            os.symlink(target, self.path)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        with self.assertRaises(OSError):
            log.append("attempt", request=REQ)
        self.assertEqual(target.read_bytes(), b"")

    def test_explicit_symlink_check_does_not_depend_on_o_nofollow(self) -> None:
        # Windows has no O_NOFOLLOW (and often cannot create symlinks), so the lstat check must stand on its own.
        fake_link = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        with mock.patch("arltuner.guard.audit.os.lstat", return_value=fake_link):
            with self.assertRaises(OSError) as cm:
                AuditLog(self.path)
        self.assertIn("symlink", str(cm.exception))
        self.assertFalse(self.path.exists())

    def test_unwritable_location_fails_at_construction(self) -> None:
        with self.assertRaises(OSError):
            AuditLog(self.dir / "no-such-dir" / "audit.jsonl")

    def test_bad_event_and_reserved_extras_rejected(self) -> None:
        log = AuditLog(self.path, fsync=False)
        with self.assertRaises(ValueError):
            log.append("whatever")
        with self.assertRaises(ValueError):
            log.append("attempt", seq=99)
        self.assertEqual(self.path.read_bytes(), b"")  # nothing half-written

    def test_malformed_request_fields_do_not_crash(self) -> None:
        log = AuditLog(self.path, fsync=False)
        bad = SimpleNamespace(cpu=-1, cmd=None, p1="x", p2=True, data=2**40 * -1, purpose=None)
        rec = log.append("refused", request=bad, response=SimpleNamespace(rc=300, data="zz"), reason="CPU_NOT_ALLOWED")
        self.assertEqual(rec["cpu"], -1)
        self.assertEqual(rec["cmd"], "None")
        self.assertEqual(rec["p2"], "True")
        self.assertEqual(rec["rc_name"], "INVALID")
        self.assertEqual(len(read_all(self.path)), 1)

    def test_read_all_rejects_garbage(self) -> None:
        self.path.write_bytes(b'{"event":"trip"}\nnot json\n')
        with self.assertRaises(ValueError):
            read_all(self.path)

    def test_default_timestamp_is_wall_clock_now(self) -> None:
        with mock.patch("arltuner.guard.audit.time.time", return_value=1_700_000_000.0):
            rec = AuditLog(self.path, fsync=False).append("attempt")
        self.assertEqual(rec["ts"], "2023-11-14T22:13:20.000+00:00")


if __name__ == "__main__":
    unittest.main()
