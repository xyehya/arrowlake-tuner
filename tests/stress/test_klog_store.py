"""The kernel-log watch (kmsg + journalctl fallback, over injected IO) and the result store (temp directory)."""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from arltuner.curves import StoreError
from arltuner.helper.trust import TrustError
from arltuner.stress import JournalLog, KernelLogWatch, KmsgLog, NullLog, StressStore, count_bad, summary_row
from arltuner.stress.store import LIST_LIMIT, MAX_RECORDS


class FakeKmsg:
    """Hands out ``/dev/kmsg`` records one per read, then EAGAIN; an optional EPIPE (overwritten records) before the n-th record."""

    def __init__(self, records: list[str]) -> None:
        self.records, self.pos, self.opened, self.closed = records, 0, 0, 0
        self.epipe_at: int | None = None
        self._piped = False

    def open(self, path: str, flags: int) -> int:
        self.opened += 1
        self.pos = 0
        assert flags & os.O_NONBLOCK, "the kmsg reader must never block"
        return 77

    def read(self, fd: int, n: int) -> bytes:
        if self.epipe_at == self.pos and not self._piped:
            self._piped = True
            raise OSError(errno.EPIPE, "records overwritten")
        if self.pos >= len(self.records):
            raise BlockingIOError(errno.EAGAIN, "end of the ring buffer")
        raw = self.records[self.pos]
        self.pos += 1
        return raw.encode()

    def close(self, fd: int) -> None:
        self.closed += 1


def kmsg(seq: int, text: str) -> str:
    return f"6,{seq},{seq * 1000},-;{text}\n SUBSYSTEM=pci\n"


class KmsgTests(unittest.TestCase):
    def test_only_records_after_the_mark_count(self) -> None:
        fake = FakeKmsg([kmsg(1, "mce: boot banner"), kmsg(2, "something")])
        log = KmsgLog(open_fn=fake.open, read_fn=fake.read, close_fn=fake.close)
        mark = log.mark()
        self.assertEqual(mark, 2)
        fake.records += [kmsg(3, "mce: [Hardware Error]: Machine check events logged"), kmsg(4, "usb 1-1: new device")]
        self.assertEqual(log.new_lines(mark), ["mce: [Hardware Error]: Machine check events logged", "usb 1-1: new device"])
        self.assertEqual(fake.opened, fake.closed, "the descriptor is closed after every drain")

    def test_an_empty_ring_and_an_overwrite_are_handled(self) -> None:
        fake = FakeKmsg([])
        log = KmsgLog(open_fn=fake.open, read_fn=fake.read, close_fn=fake.close)
        self.assertEqual(log.mark(), -1)
        fake.records = [kmsg(5, "a"), kmsg(6, "b")]
        fake.epipe_at = 1
        self.assertEqual(log.new_lines(-1), ["a", "b"])

    def test_malformed_records_are_skipped(self) -> None:
        fake = FakeKmsg(["no header", "x,y;text", kmsg(9, "ok")])
        log = KmsgLog(open_fn=fake.open, read_fn=fake.read, close_fn=fake.close)
        self.assertEqual(log.new_lines(0), ["ok"])


class JournalTests(unittest.TestCase):
    def runner(self, outputs: list[str], rc: int = 0) -> Any:
        calls: list[dict] = []

        def run(cmd: list[str], **kwargs: Any) -> Any:
            calls.append({"cmd": cmd, **kwargs})
            return subprocess.CompletedProcess(cmd, rc, stdout=outputs.pop(0).encode() if outputs else b"")

        run.calls = calls  # type: ignore[attr-defined]
        return run

    def test_the_line_count_is_the_mark(self) -> None:
        run = self.runner(["[ 1.0] a\n[ 2.0] b\n", "[ 1.0] a\n[ 2.0] b\n[ 9.0] mce: [Hardware Error]: x\n"])
        log = JournalLog(run=run, exe="/usr/bin/journalctl")
        self.assertEqual(log.mark(), 2)
        self.assertEqual(log.new_lines(2), ["[ 9.0] mce: [Hardware Error]: x"])
        call = run.calls[0]
        self.assertEqual(call["cmd"][1:], ["-k", "-b", "-q", "-o", "short-monotonic"])
        self.assertEqual((call["env"], call["cwd"], call["shell"]), ({"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"}, "/", False))

    def test_the_watch_falls_back_to_journalctl_and_counts(self) -> None:
        class Dead:
            def mark(self) -> int:
                raise OSError(errno.EACCES, "denied")

        run = self.runner(["a\n", "a\nmce: bad\nmachine check events logged\nWrite to unrecognized MSR 0x1\n"])
        watch = KernelLogWatch([Dead(), JournalLog(run=run)])
        watch.begin()
        self.assertTrue(watch.available)
        self.assertEqual(count_bad(watch.new_lines()), 2, "the expected MSR notice is excluded")

    def test_no_source_means_not_available(self) -> None:
        run = self.runner([], rc=1)
        watch = KernelLogWatch([JournalLog(run=run)])
        watch.begin()
        self.assertFalse(watch.available)
        self.assertIn("journalctl", watch.why)
        self.assertEqual(watch.new_lines(), [])
        quiet = KernelLogWatch([NullLog()])
        quiet.begin()
        self.assertTrue(quiet.available)

    def test_an_untrusted_journalctl_is_not_run(self) -> None:
        def refuse(path: str) -> None:
            raise TrustError(path, "owner uid 1000 is not root")  # not an OSError: the production trust check raises this

        run = self.runner(["x"])
        watch = KernelLogWatch([JournalLog(run=run, trust_check=refuse)])
        watch.begin()
        self.assertFalse(watch.available)
        self.assertEqual(run.calls, [])


def record(n: int, **extra: Any) -> dict[str, Any]:
    return {"kind": "cpu", "started": f"2026-10-11T12:{n % 60:02d}:00Z", "stopped_by": "time", "errors": 0, "health": "clean", **extra}


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "stress"
        self.store = StressStore(self.root)

    def test_save_creates_a_private_directory_and_file(self) -> None:
        saved = self.store.save(record(1), now=1_700_000_000.0)
        self.assertRegex(saved["id"], r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}$")
        path = self.store.path_of(saved["id"])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(json.loads(path.read_text())["id"], saved["id"])
        self.assertEqual([p.name for p in self.root.iterdir()], [path.name], "no temp file left behind")

    def test_list_is_newest_first_and_limited_to_fifty(self) -> None:
        for n in range(LIST_LIMIT + 5):
            self.store.save(record(n), now=1_700_000_000.0 + n)
        rows = self.store.summaries()
        self.assertEqual(len(rows), LIST_LIMIT)
        self.assertEqual([r["id"] for r in rows], sorted((r["id"] for r in rows), reverse=True))
        self.assertEqual(set(rows[0]), set(summary_row({})))

    def test_the_oldest_are_dropped_beyond_the_cap(self) -> None:
        for n in range(MAX_RECORDS + 3):
            self.store.save(record(n), now=1_700_000_000.0 + n)
        self.assertEqual(len(list(self.root.glob("*.json"))), MAX_RECORDS)

    @unittest.skipUnless(os.name == "posix", "symlink / mode semantics")
    def test_a_planted_symlink_or_loose_file_is_not_read(self) -> None:
        saved = self.store.save(record(1), now=1_700_000_000.0)
        loose = self.store.save(record(2), now=1_700_000_100.0)
        os.chmod(self.store.path_of(loose["id"]), 0o666)
        link_id = "20991231T235959Z-link"
        os.symlink(self.store.path_of(saved["id"]), self.root / f"{link_id}.json")
        rows = self.store.summaries()
        self.assertEqual([r["id"] for r in rows], [saved["id"]])
        self.assertEqual(len(self.store.problems), 2)

    @unittest.skipUnless(os.name == "posix", "mode semantics")
    def test_a_loose_directory_is_refused(self) -> None:
        self.store.save(record(1), now=1_700_000_000.0)
        os.chmod(self.root, 0o777)
        with self.assertRaises(StoreError) as cm:
            self.store.summaries()
        self.assertEqual(cm.exception.detail, {"check": "TRUST_PATH"})

    def test_a_missing_directory_lists_nothing(self) -> None:
        self.assertEqual(self.store.summaries(), [])
        self.assertFalse(self.root.exists(), "listing never creates the directory")


if __name__ == "__main__":
    unittest.main()
