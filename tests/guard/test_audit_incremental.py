"""G-211 tests: ``read_state`` parses only what was appended since its last call, and every semantic of the whole-file scan is kept.

The oracle is ``whole_file_state`` below: the pre-G-211 implementation of ``read_state`` (scan the file from byte 0 on every call), copied verbatim in
behaviour. Every scripted step compares the incremental answer -- value or exception -- with the oracle. The counters ``audit.SCAN_STATS``
(``bytes_read``, ``full_scans``) are the direct evidence for "appended bytes only". Temp files only, no hardware, no real ``/proc``.

Also here (guard level): the per-boot cap and the cross-process dwell (``min_dwell_s > 0``) still see records another process appended between two operations
of a running guard, and the lock file still excludes a second real process while a transaction is in flight.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from arltuner.guard import AuditLog, AuditState, AuditUnreadable, Policy, Refusal, Request, read_state
from arltuner.guard import audit as audit_module
from arltuner.guard.audit import SCAN_STATS, parse_audit_ts

from .test_limits import BOOT_A, BOOT_B, LimitsCase

ROOT = Path(__file__).resolve().parents[2]
TS = "2023-11-14T22:13:20.000+00:00"


def whole_file_state(path: Path) -> AuditState:
    """The pre-G-211 ``read_state``: a fresh scan of the whole file, line by line."""
    last: float | None = None
    by_boot: dict[str | None, int] = {}
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, 1):
            partial = not raw.endswith(b"\n")
            text = raw.strip()
            if not text:
                continue
            where = f"{path}:{number}"
            try:
                record = json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if partial:
                    continue
                raise AuditUnreadable(f"{where}: not a valid audit record: {exc}") from exc
            if not isinstance(record, Mapping) or not isinstance(record.get("event"), str):
                raise AuditUnreadable(f"{where}: audit record is not an object with an 'event'")
            if record["event"] != "attempt":
                continue
            stamp = parse_audit_ts(record.get("ts"), where)
            boot = record.get("boot_id")
            if boot is not None and not isinstance(boot, str):
                raise AuditUnreadable(f"{where}: attempt 'boot_id' is neither a string nor null")
            last = stamp if last is None else max(last, stamp)
            by_boot[boot] = by_boot.get(boot, 0) + 1
    return AuditState(last, by_boot)


def attempt(ts: str = TS, **extra: object) -> str:
    return json.dumps({"ts": ts, "seq": 0, "event": "attempt", **extra})


def other(event: str = "result") -> str:
    return json.dumps({"ts": TS, "seq": 0, "event": event})


class IncrementalReadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "audit.jsonl"

    # ---- helpers
    def put(self, data: bytes | str) -> None:
        """Replace the content in place (same inode, like ``Path.write_bytes``)."""
        self.path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

    def add(self, data: bytes | str) -> None:
        with open(self.path, "ab") as handle:
            handle.write(data.encode("utf-8") if isinstance(data, str) else data)

    def same_as_oracle(self) -> AuditState:
        """``read_state`` and the whole-file oracle agree (same value, or both raise ``AuditUnreadable``); returns the state."""
        try:
            want: AuditState | None = whole_file_state(self.path)
        except AuditUnreadable:
            want = None
        if want is None:
            with self.assertRaises(AuditUnreadable):
                read_state(self.path)
            return AuditState(None, {})
        got = read_state(self.path)
        self.assertEqual(got.last_attempt_wall, want.last_attempt_wall)
        self.assertEqual(dict(got.attempts_by_boot), dict(want.attempts_by_boot))
        return got

    def stats(self) -> tuple[int, int]:
        return SCAN_STATS["bytes_read"], SCAN_STATS["full_scans"]

    # ---- appended bytes only
    def test_only_the_appended_bytes_are_parsed(self) -> None:
        first = "".join(attempt(f"2023-11-14T22:{n % 60:02d}:00.000+00:00", boot_id=BOOT_A) + "\n" + other() + "\n" for n in range(500))
        self.put(first)
        b0, s0 = self.stats()
        self.same_as_oracle()
        b1, s1 = self.stats()
        self.assertEqual((b1 - b0, s1 - s0), (len(first), 1), "the first call reads the whole file once")
        for _ in range(3):  # nothing appended: nothing parsed
            read_state(self.path)
        self.assertEqual(self.stats(), (b1, s1))
        added = attempt("2023-11-14T23:00:00.000+00:00", boot_id=BOOT_B) + "\n" + other("readback") + "\n"
        self.add(added)
        state = self.same_as_oracle()
        b2, s2 = self.stats()
        self.assertEqual((b2 - b1, s2 - s1), (len(added), 0), "an append is parsed alone, no rescan")
        self.assertEqual(dict(state.attempts_by_boot), {BOOT_A: 500, BOOT_B: 1})

    def test_every_tail_and_line_case_matches_the_whole_file_scan(self) -> None:
        steps: list[tuple[str, bytes | str]] = [
            ("one attempt", attempt(boot_id="A") + "\n"),
            ("blank lines", "\n   \n\n"),
            ("a non-attempt record", other() + "\n"),
            ("a cut-short tail", '{"ts": "2023-11-14T22:1'),  # ignored
            ("the tail completes into a bad line", 'garbage"\n'),  # now a complete corrupt line -> unreadable, both
        ]
        self.put(b"")
        self.same_as_oracle()
        for label, data in steps:
            with self.subTest(label):
                self.add(data)
                self.same_as_oracle()
        self.put(attempt(boot_id="A") + "\n")  # reset for the tail cases proper
        self.same_as_oracle()
        with self.subTest("a complete event without a newline counts"):
            self.add(attempt("2023-11-14T22:14:00.000+00:00", boot_id="A"))
            self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 2})
            self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 2}, "judged afresh, not committed or doubled")
        with self.subTest("... and is not counted twice once its newline arrives"):
            self.add("\n")
            self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 2})
            self.add(attempt("2023-11-14T22:15:00.000+00:00", boot_id="A") + "\n")
            self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 3})
        with self.subTest("a cut-short tail that later becomes a good record"):
            self.add(attempt("2023-11-14T22:16:00.000+00:00", boot_id="B")[:-6])
            self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 3})
            self.add(attempt("2023-11-14T22:16:00.000+00:00", boot_id="B")[-6:] + "\n")
            self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 3, "B": 1})
        with self.subTest("a tail that parses to a non-object raises, as before"):
            self.add("[1]")
            self.same_as_oracle()
            self.add("\n")  # complete line now: still raises
            self.same_as_oracle()

    def test_a_malformed_complete_line_raises_every_time_and_commits_nothing(self) -> None:
        self.put(attempt(boot_id="A") + "\n")
        read_state(self.path)
        self.add(attempt(boot_id="A") + "\n{not json\n" + attempt(boot_id="A") + "\n")
        for _ in range(3):
            with self.assertRaises(AuditUnreadable):
                read_state(self.path)
        self.put(attempt(boot_id="A") + "\n" + attempt(boot_id="A") + "\n")  # repaired
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 2})

    def test_bad_timestamps_and_boot_ids_in_an_appended_attempt_still_raise(self) -> None:
        self.put(attempt(boot_id="A") + "\n")
        read_state(self.path)
        for label, line in (("bad ts", attempt("yesterday", boot_id="A")), ("naive ts", attempt("2023-11-14T22:13:20", boot_id="A")), ("numeric boot", attempt(boot_id=7))):
            with self.subTest(label):
                self.put(attempt(boot_id="A") + "\n")
                read_state(self.path)
                self.add(line + "\n")
                with self.assertRaises(AuditUnreadable):
                    read_state(self.path)

    # ---- rescans
    def test_truncation_rescans(self) -> None:
        self.put("".join(attempt(boot_id="A") + "\n" for _ in range(50)))
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 50})
        _, scans = self.stats()
        self.put("".join(attempt(boot_id="B") + "\n" for _ in range(3)))  # shorter, same inode
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"B": 3})
        self.assertEqual(self.stats()[1], scans + 1)
        self.put(b"")  # truncated to nothing
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {})

    def test_a_different_file_at_the_same_path_rescans(self) -> None:
        self.put("".join(attempt(boot_id="A") + "\n" for _ in range(5)))
        self.same_as_oracle()
        _, scans = self.stats()
        replacement = self.dir / "new.jsonl"
        replacement.write_text("".join(attempt(boot_id="B") + "\n" for _ in range(9)), encoding="utf-8")  # longer, and a different inode
        self.assertNotEqual(os.stat(replacement).st_ino, os.stat(self.path).st_ino)
        os.replace(replacement, self.path)
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"B": 9})
        self.assertEqual(self.stats()[1], scans + 1)

    def test_each_cache_check_refuses_on_its_own(self) -> None:
        """The inode, size and content checks overlap; each one alone must still say no (a mutation of one must not go unnoticed)."""
        content = "".join(attempt(boot_id="A") + "\n" for _ in range(5))
        self.put(content)
        read_state(self.path)
        tail = audit_module._TAILS[os.path.abspath(self.path)]
        st = os.stat(self.path)
        with open(self.path, "rb") as handle:
            self.assertTrue(audit_module._cache_matches(tail, st, handle))
            same = dict(st_dev=st.st_dev, st_ino=st.st_ino, st_size=st.st_size, st_mtime_ns=st.st_mtime_ns)
            for label, changed in (
                ("another inode", dict(same, st_ino=st.st_ino + 1)),
                ("another device", dict(same, st_dev=st.st_dev + 1)),
                ("shorter than what was summarised", dict(same, st_size=tail.offset - 1)),
                ("no inode number", dict(same, st_ino=0)),
                ("same size, other mtime", dict(same, st_mtime_ns=st.st_mtime_ns + 1)),
            ):
                with self.subTest(label):
                    self.assertFalse(audit_module._cache_matches(tail, SimpleNamespace(**changed), handle))  # type: ignore[arg-type]
        # and observably: a byte-identical copy at another inode, plus one more record, is a rescan (the answer is the same either way)
        _, scans = self.stats()
        copy = self.dir / "copy.jsonl"
        copy.write_text(content + attempt(boot_id="A") + "\n", encoding="utf-8")
        os.replace(copy, self.path)
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 6})
        self.assertEqual(self.stats()[1], scans + 1)

    def test_a_rewrite_in_place_that_grows_the_file_rescans(self) -> None:
        old = "".join(attempt(boot_id="A") + "\n" for _ in range(5))
        self.put(old)
        self.same_as_oracle()
        _, scans = self.stats()
        self.put("".join(attempt(boot_id="B") + "\n" for _ in range(20)))  # same inode, bigger, different bytes everywhere
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"B": 20})
        self.assertEqual(self.stats()[1], scans + 1)
        # the one thing the model does not detect (module comment in audit.py): an in-place edit of already-summarised bytes *between* the first and the
        # last 256 bytes that keeps the file size. An edit that grows/shrinks the file, or touches its start or the end of what was read, is caught.
        body = "".join(attempt(boot_id="B") + "\n" for _ in range(20))
        self.put(body.replace("B", "C", 1))
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"C": 1, "B": 19}, "the head check catches the in-place edit near the start")

    def test_a_same_size_rewrite_of_the_middle_rescans(self) -> None:
        """Size == the cached offset, first and last 256 bytes untouched, but records 2-48 rewritten: only the mtime check can see it."""
        self.put("".join(attempt(boot_id="A") + "\n" for _ in range(50)))
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 50})
        _, scans = self.stats()
        lines = [attempt(boot_id="A") + "\n"] + [attempt(boot_id="B") + "\n"] * 47 + [attempt(boot_id="A") + "\n"] * 2
        rewritten = "".join(lines)
        self.assertEqual(len(rewritten), self.path.stat().st_size)
        before = self.path.stat().st_mtime_ns
        self.put(rewritten)
        os.utime(self.path, ns=(before + 5_000_000_000, before + 5_000_000_000))  # a coarse-grained filesystem clock must not hide the write
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 3, "B": 47})
        self.assertEqual(self.stats()[1], scans + 1)
        # and an untouched file of exactly the cached size is still served from the cache
        read_state(self.path)
        self.assertEqual(self.stats()[1], scans + 1)

    def test_a_replacement_file_with_an_identical_start_rescans(self) -> None:
        head = "".join(attempt(boot_id="A") + "\n" for _ in range(8))  # > 256 bytes, so the head anchor is full
        self.put(head + "".join(attempt(boot_id="A") + "\n" for _ in range(4)))
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 12})
        _, scans = self.stats()
        replacement = self.dir / "new.jsonl"
        replacement.write_text(head + "".join(attempt(boot_id="B") + "\n" for _ in range(9)), encoding="utf-8")  # same start, longer, new inode
        os.replace(replacement, self.path)
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 8, "B": 9})
        self.assertEqual(self.stats()[1], scans + 1)

    def test_the_start_anchor_is_topped_up_while_it_is_short(self) -> None:
        self.put(attempt(boot_id="A") + "\n")
        read_state(self.path)
        tail = audit_module._TAILS[os.path.abspath(self.path)]
        self.assertEqual(len(tail.head), tail.offset)  # short file: the anchor is the whole of it
        self.assertLess(len(tail.head), audit_module._ANCHOR)
        for _ in range(10):
            self.add(attempt(boot_id="A") + "\n")
            read_state(self.path)
        with open(self.path, "rb") as handle:
            on_disk = handle.read(audit_module._ANCHOR)
        self.assertEqual(len(tail.head), audit_module._ANCHOR)
        self.assertEqual(tail.head, on_disk)
        self.assertEqual(tail.anchor, self.path.read_bytes()[tail.offset - audit_module._ANCHOR : tail.offset])
        _, scans = self.stats()
        # an in-place edit inside the first 256 bytes is seen by the topped-up head even though the file only grew since
        self.add(attempt(boot_id="A") + "\n")
        read_state(self.path)
        _, scans = self.stats()
        data = self.path.read_bytes()
        at = data.index(b'"A"', 100)
        self.assertLess(at, audit_module._ANCHOR)
        self.put(data[:at] + b'"B"' + data[at + 3 :] + attempt(boot_id="A").encode() + b"\n")
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 12, "B": 1})
        self.assertEqual(self.stats()[1], scans + 1)

    def test_readers_racing_an_appender_see_a_consistent_state(self) -> None:
        log = AuditLog(self.path, fsync=False)
        self.put("")
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    read_state(self.path)
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for _ in range(150):
                log.append("attempt", request=Request(0, 0x10, 0, 0, 0, "race"), boot_id="A")
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"A": 150})

    def test_the_file_disappearing_is_no_history_and_it_coming_back_is_read_afresh(self) -> None:
        self.put(attempt(boot_id="A") + "\n")
        self.same_as_oracle()
        self.path.unlink()
        self.assertEqual(read_state(self.path), AuditState(None, {}))
        self.put(attempt(boot_id="B") + "\n" + attempt(boot_id="B") + "\n")
        self.assertEqual(dict(self.same_as_oracle().attempts_by_boot), {"B": 2})

    def test_a_symlink_is_refused_even_when_the_real_file_was_cached(self) -> None:
        self.put(attempt(boot_id="A") + "\n")
        read_state(self.path)
        real = self.dir / "real.jsonl"
        os.replace(self.path, real)
        self.path.symlink_to(real)
        with self.assertRaises(OSError):
            read_state(self.path)

    def test_a_returned_state_is_not_changed_by_later_appends(self) -> None:
        self.put(attempt(boot_id="A") + "\n")
        before = read_state(self.path)
        self.add(attempt(boot_id="A") + "\n")
        after = read_state(self.path)
        self.assertEqual(dict(before.attempts_by_boot), {"A": 1})
        self.assertEqual(dict(after.attempts_by_boot), {"A": 2})
        with self.assertRaises(TypeError):
            after.attempts_by_boot["A"] = 0  # type: ignore[index]

    def test_line_numbers_in_errors_count_every_line_of_the_file(self) -> None:
        self.put(attempt(boot_id="A") + "\n\n" + other() + "\n")
        read_state(self.path)
        self.add("\n{bad\n")
        with self.assertRaises(AuditUnreadable) as cm:
            read_state(self.path)
        self.assertIn(":5:", str(cm.exception))  # lines: attempt, blank, result, blank, {bad

    def test_the_cache_is_bounded(self) -> None:
        for n in range(audit_module._TAIL_CACHE_MAX + 5):
            path = self.dir / f"log{n}.jsonl"
            path.write_text(attempt(boot_id="A") + "\n", encoding="utf-8")
            read_state(path)
        self.assertLessEqual(len(audit_module._TAILS), audit_module._TAIL_CACHE_MAX)


class IncrementalGuardTests(LimitsCase):
    """The guard's cross-process limits still see what another process appended between two operations of one running guard."""

    def test_the_per_boot_cap_sees_attempts_another_process_appended_in_between(self) -> None:
        guard = self.make_guard(policy=self._policy(max_operations_per_boot=4))
        guard.execute(self.read_req(), self.confirm)  # 1 attempt of ours: the state is cached now
        self.other_process(count=3)
        self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_cross_process_dwell_still_refuses_and_clears_when_the_wall_clock_passes_it(self) -> None:
        guard = self.make_guard(policy=self._policy(min_dwell_s=30.0))
        self.other_process(seconds_ago=5.0)
        exc = self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertIsNotNone(exc.retry_after_s)
        self.other_process(seconds_ago=1.0)  # appended after the guard looked once
        self.advance(10.0)
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.advance(30.0)
        guard.execute(self.read_req(), self.confirm)  # honoured, then allowed
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_log_corrupted_while_the_guard_runs_is_audit_unavailable(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        with open(self.audit_path, "ab") as handle:
            handle.write(b"{corrupt\n")
        self.assert_refused_unreadable(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)

    def test_a_log_replaced_by_a_shorter_one_is_read_afresh(self) -> None:
        guard = self.make_guard(policy=self._policy(max_operations_per_boot=2))
        guard.execute(self.read_req(), self.confirm)
        guard.execute(self.read_req(), self.confirm)
        self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)
        self.audit_path.write_bytes(b"")  # the human emptied the log: no history, the cap counts from zero
        guard.execute(self.read_req(), self.confirm)

    def test_the_lock_file_still_excludes_a_second_process_during_a_transaction(self) -> None:
        script = (
            "import dataclasses, sys\n"
            f"sys.path[:0] = [{str(ROOT / 'src')!r}]\n"
            "from arltuner.guard import AuditLog, Confirmation, GuardRefused, Request, WriteGuard, default_m1_policy, ENV_FLAG\n"
            "class T:\n"
            "    def transact(self, *a, **k):\n"
            "        raise SystemExit('the second process must not send anything')\n"
            "audit, lock = sys.argv[1], sys.argv[2]\n"
            "policy = dataclasses.replace(default_m1_policy(), max_operations_per_session=5, max_operations_per_boot=100, min_dwell_s=0.0)\n"
            "guard = WriteGuard(T(), policy, AuditLog(audit, fsync=False), session_token='t', env={ENV_FLAG: '1'}, lock_path=lock, boot_id_fn=lambda: 'other-boot')\n"
            "try:\n"
            "    guard.execute(Request(0, 0x10, 0, 0, 0, 'second process'), Confirmation('t', 'x'))\n"
            "except GuardRefused as exc:\n"
            "    print(exc.reason.value)\n"
        )
        outcome: list[str] = []

        def second_process(call: tuple[int, int, int, int, int]) -> None:  # runs inside the first guard's transact: the lock is held
            run = subprocess.run(
                [sys.executable, "-I", "-c", script, str(self.dir / "second-audit.jsonl"), str(self.lock_path)], capture_output=True, text=True, timeout=60, check=False
            )
            outcome.append(run.stdout.strip() or run.stderr.strip())

        self.transport.before_return = second_process
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.assertEqual(outcome, [Refusal.LOCKED.value])
        self.assertFalse(self.lock_path.exists(), "released after the transaction")
        guard.execute(self.read_req(), self.confirm)  # and free for the next one
        self.assertEqual(len(outcome), 2)

    def _policy(self, **changes: object) -> Policy:
        return dataclasses.replace(self.policy, **changes)


if __name__ == "__main__":
    unittest.main()
