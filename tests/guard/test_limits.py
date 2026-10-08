"""T-016 tests: limits that survive process boundaries (cross-process dwell, per-boot operation cap, fail-closed audit).

Rules under test: docs/tasks/T-016-cross-process-limits.md; docs/SAFETY.md "Mailbox-session discipline"; D-011 (2026-08-26
incident: sweep-like mailbox traffic). "Another process" is simulated by a second ``AuditLog`` appending ``attempt``
records to the same file. No real ``/proc`` is read (``boot_id_fn`` is injected), no sleeping, nothing touches hardware.

Every refusal test goes through ``assert_refused`` (reason + the recording transport saw nothing new + audited), or its
corrupt-log twin ``assert_refused_unreadable`` (the log itself is unreadable, so it is inspected as raw text). Each test
fails if the check it names is removed from ``guard.py`` / ``audit.py`` / ``policy.py``.
"""
from __future__ import annotations

import dataclasses
import json
import math
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arltuner.guard import (
    AuditLog,
    AuditState,
    AuditUnreadable,
    GuardRefused,
    Policy,
    PolicyError,
    Refusal,
    Request,
    WriteGuard,
    anchored_wall_clock,
    default_m1_policy,
    load_policy,
    read_boot_id,
    read_state,
)
from arltuner.guard import guard as guard_module

from .fakes import FakeClock
from .test_guard import GuardCase

T0 = 1_700_000_000.0
BOOT_A = "aaaaaaaa-0000-4000-8000-000000000001"
BOOT_B = "bbbbbbbb-0000-4000-8000-000000000002"
OTHER = Request(0, 0x10, 0, 0, 0, "another process")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=guard_module.timezone.utc).isoformat(timespec="milliseconds")


class LimitsCase(GuardCase):
    """A guard whose boot id is fixed, with the T-013 default of no dwell unless a test sets one."""

    boot = BOOT_A

    def setUp(self) -> None:
        super().setUp()
        self.boot_calls = 0

    def boot_id(self) -> str | None:
        self.boot_calls += 1
        return self.boot

    def make_guard(self, **overrides: object) -> WriteGuard:
        overrides.setdefault("boot_id_fn", self.boot_id)
        return super().make_guard(**overrides)

    def other_process(self, *, seconds_ago: float = 0.0, boot_id: object = BOOT_A, count: int = 1, at: float | None = None) -> None:
        """Another process appended ``count`` ``attempt`` records ``seconds_ago`` before the fake wall clock's now."""
        log = AuditLog(self.audit_path, fsync=False)
        stamp = self.wall.now - seconds_ago if at is None else at
        for _ in range(count):
            log.append("attempt", ts=stamp, request=OTHER, operator="other", boot_id=boot_id)

    def raw_lines(self) -> list[str]:
        return self.audit_path.read_text(encoding="utf-8").splitlines()

    def assert_refused_unreadable(self, guard: WriteGuard, req: Request, reason: Refusal) -> GuardRefused:
        """``assert_refused`` without ``read_all`` (which itself refuses a corrupt log): inspect the last raw line."""
        before = list(self.transport.calls)
        with self.assertRaises(GuardRefused) as cm:
            guard.execute(req, self.confirm)
        self.assertIs(cm.exception.reason, reason, cm.exception.detail)
        self.assertEqual(self.transport.calls, before, "a refusal must send nothing")
        last = json.loads(self.raw_lines()[-1])
        self.assertEqual((last["event"], last["reason"]), ("refused", reason.value))
        return cm.exception


# =====================================================================================================================
class ReadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "audit.jsonl"

    def write(self, *lines: str, tail: str = "\n") -> None:
        self.path.write_bytes(("\n".join(lines) + tail).encode("utf-8") if lines else b"")

    @staticmethod
    def attempt(ts: str = "2023-11-14T22:13:20.000+00:00", **extra: object) -> str:
        return json.dumps({"ts": ts, "seq": 0, "event": "attempt", **extra})

    def test_missing_and_empty_files_are_no_history(self) -> None:
        empty = AuditState(None, {})
        self.assertEqual(read_state(self.path), empty)  # missing
        self.path.write_bytes(b"")
        self.assertEqual(read_state(self.path), empty)
        self.path.write_bytes(b"\n  \n\n")  # only blank lines
        self.assertEqual(read_state(self.path), empty)

    def test_latest_attempt_over_all_lines_and_counts_per_boot(self) -> None:
        self.write(
            self.attempt("2023-11-14T22:13:20.000+00:00", boot_id="A"),
            self.attempt("2023-11-14T22:15:00.500+00:00", boot_id="A"),
            self.attempt("2023-11-14T22:14:00.000+00:00", boot_id="B"),  # out of order: the max wins, not the last
            self.attempt("2023-11-14T22:14:30.000+00:00", boot_id=None),
            self.attempt("2023-11-14T22:14:31.000+00:00"),  # no boot_id key (pre-T-016 record): the None bucket
        )
        state = read_state(self.path)
        self.assertEqual(state.last_attempt_wall, datetime.fromisoformat("2023-11-14T22:15:00.500+00:00").timestamp())
        self.assertEqual(dict(state.attempts_by_boot), {"A": 2, "B": 1, None: 2})

    def test_only_attempt_records_count(self) -> None:
        self.write(
            json.dumps({"event": "refused", "ts": "garbage", "reason": "DWELL"}),  # ts of other events is not interpreted
            json.dumps({"event": "result", "ts": "2023-11-14T22:13:20.000+00:00", "boot_id": "A"}),
            json.dumps({"event": "readback", "ts": "2023-11-14T22:13:21.000+00:00", "boot_id": "A"}),
            json.dumps({"event": "trip", "note": "no ts at all"}),
        )
        self.assertEqual(read_state(self.path), AuditState(None, {}))

    def test_a_truncated_trailing_line_is_tolerated(self) -> None:
        self.write(self.attempt(boot_id="A"), tail='\n{"ts": "2023-11-14T22:1')  # no newline: a write cut short
        state = read_state(self.path)
        self.assertEqual(dict(state.attempts_by_boot), {"A": 1})

    def test_a_complete_trailing_line_without_newline_still_counts(self) -> None:
        self.write(self.attempt(boot_id="A"), self.attempt(boot_id="A"), tail="")
        self.assertEqual(dict(read_state(self.path).attempts_by_boot), {"A": 2})

    def test_a_corrupted_middle_line_is_unreadable(self) -> None:
        self.write(self.attempt(boot_id="A"), '{"ts": "2023-11-14', self.attempt(boot_id="A"))
        with self.assertRaises(AuditUnreadable):
            read_state(self.path)

    def test_a_malformed_last_line_that_ended_in_a_newline_is_not_a_partial_line(self) -> None:
        self.write(self.attempt(boot_id="A"), "{not json")  # "\n" terminated: a complete, corrupt line
        with self.assertRaises(AuditUnreadable):
            read_state(self.path)

    def test_other_malformed_content_is_unreadable(self) -> None:
        cases = {
            "not an object": "[1, 2]",
            "object without event": json.dumps({"ts": "2023-11-14T22:13:20.000+00:00"}),
            "non-string event": json.dumps({"event": 3}),
            "attempt without ts": json.dumps({"event": "attempt", "boot_id": "A"}),
            "attempt with numeric ts": json.dumps({"event": "attempt", "ts": 1700000000, "boot_id": "A"}),
            "attempt with bad ts": self.attempt("yesterday", boot_id="A"),
            "attempt with naive ts": self.attempt("2023-11-14T22:13:20", boot_id="A"),
            "attempt with numeric boot_id": self.attempt(boot_id=7),
        }
        for label, line in cases.items():
            with self.subTest(label):
                self.write(self.attempt(boot_id="A"), line, self.attempt(boot_id="A"))
                with self.assertRaises(AuditUnreadable):
                    read_state(self.path)
                self.write(line)  # also when it is the only (complete) line
                with self.assertRaises(AuditUnreadable):
                    read_state(self.path)

    def test_invalid_utf8_in_a_complete_line_is_unreadable(self) -> None:
        self.path.write_bytes(b'{"event":"attempt","ts":"\xff\xfe"}\n' + self.attempt(boot_id="A").encode() + b"\n")
        with self.assertRaises(AuditUnreadable):
            read_state(self.path)

    def test_a_symlink_is_refused(self) -> None:
        self.path.write_bytes(b"")
        with mock.patch("arltuner.guard.audit.os.lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK)):
            with self.assertRaises(OSError):
                read_state(self.path)

    def test_the_state_is_immutable(self) -> None:
        self.write(self.attempt(boot_id="A"))
        state = read_state(self.path)
        with self.assertRaises(TypeError):
            state.attempts_by_boot["A"] = 0  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.last_attempt_wall = 0.0  # type: ignore[misc]

    def test_a_directory_is_an_oserror_not_no_history(self) -> None:
        self.path.mkdir()
        with self.assertRaises(OSError):
            read_state(self.path)


class AttemptRecordTests(LimitsCase):
    def test_every_attempt_record_carries_the_boot_id(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        (attempt,) = [r for r in self.records() if r["event"] == "attempt"]
        self.assertEqual(attempt["boot_id"], BOOT_A)

    def test_an_unknown_boot_id_is_recorded_as_null(self) -> None:
        for label, fn in {
            "None": lambda: None,
            "raises": lambda: (_ for _ in ()).throw(OSError("no /proc")),
            "not a string": lambda: 12345,
            "empty": lambda: "",
            "blank": lambda: "   ",
        }.items():
            with self.subTest(label):
                self.audit_path.write_bytes(b"")
                guard = self.make_guard(boot_id_fn=fn, lock_path=self.dir / f"{label}.lock".replace(" ", "_"))
                guard.execute(self.read_req(), self.confirm)
                attempt = [r for r in self.records() if r["event"] == "attempt"][-1]
                self.assertIn("boot_id", attempt)
                self.assertIsNone(attempt["boot_id"])

    def test_the_boot_id_fn_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            self.make_guard(boot_id_fn="not callable")

    def test_read_boot_id_reads_the_kernel_file_or_none(self) -> None:
        fake = self.dir / "boot_id"
        with mock.patch.object(guard_module, "_BOOT_ID_PATH", str(fake)):
            self.assertIsNone(read_boot_id())  # unreadable / non-Linux
            fake.write_text("7F3A9C2E-0000-4000-8000-ABCDEF012345\n")
            self.assertEqual(read_boot_id(), "7f3a9c2e-0000-4000-8000-abcdef012345")
            fake.write_text("not a uuid at all\n")
            self.assertIsNone(read_boot_id())
            fake.write_text("")
            self.assertIsNone(read_boot_id())

    def test_the_default_boot_id_fn_is_read_boot_id(self) -> None:
        guard = WriteGuard(
            self.transport, self.policy, AuditLog(self.audit_path, fsync=False),
            session_token=self.TOKEN, env=self.env, lock_path=self.lock_path, clock=self.clock, wall_clock=self.wall,
        )
        with mock.patch.object(guard_module, "_BOOT_ID_PATH", str(self.dir / "absent")):  # never the real /proc
            guard.execute(self.read_req(), self.confirm)
        self.assertIsNone([r for r in self.records() if r["event"] == "attempt"][0]["boot_id"])


# =====================================================================================================================
class CrossProcessDwell(LimitsCase):
    def setUp(self) -> None:
        super().setUp()
        self.policy = dataclasses.replace(self.policy, min_dwell_s=60.0)

    def test_an_attempt_10s_ago_by_another_process_is_refused_and_61s_ago_is_accepted(self) -> None:
        self.other_process(seconds_ago=10)
        guard = self.make_guard()
        exc = self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertIn(iso(self.wall.now - 10), exc.detail)  # names the previous attempt's ISO time
        self.assertIn("another process", exc.detail)
        self.assertAlmostEqual(exc.retry_after_s or 0, 50.0, places=3)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(guard.tripped)
        self.advance(51.0)  # now 61 s after the other process's attempt
        self.assertEqual(guard.execute(self.read_req(), self.confirm).response.rc, 0)

    def test_exactly_min_dwell_later_is_accepted_and_just_before_is_not(self) -> None:
        self.other_process(seconds_ago=59.9)
        guard = self.make_guard()
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.advance(0.1)
        guard.execute(self.read_req(), self.confirm)

    def test_the_window_is_measured_from_the_latest_attempt_of_any_process(self) -> None:
        self.other_process(seconds_ago=300)
        self.other_process(seconds_ago=5)  # appended later in the file, but the max timestamp is what counts
        self.other_process(seconds_ago=200)
        self.assert_refused(self.make_guard(), self.read_req(), Refusal.DWELL)

    def test_a_previous_attempt_from_a_different_boot_still_enforces_the_dwell(self) -> None:
        self.other_process(seconds_ago=10, boot_id=BOOT_B)  # the dwell is about time, not about the boot
        self.assert_refused(self.make_guard(), self.read_req(), Refusal.DWELL)

    def test_a_future_dated_previous_attempt_is_refused(self) -> None:
        self.other_process(seconds_ago=-3600)  # an hour in the future: the wall clock went backwards
        guard = self.make_guard()
        exc = self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertIn("future", exc.detail)
        self.assertIsNone(exc.retry_after_s)  # no honest wait time can be given
        self.advance(3600 + 61)
        guard.execute(self.read_req(), self.confirm)  # and it passes once the clock has really caught up

    def test_the_future_check_allows_one_second_of_slack_and_applies_even_with_no_dwell(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=0.0)
        self.other_process(seconds_ago=-0.9)
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)  # 0.9 s ahead: inside the 1 s slack
        self.other_process(seconds_ago=-1.5)
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertEqual(len(self.transport.calls), 1)

    def test_no_dwell_configured_means_a_recent_attempt_is_fine(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=0.0)
        self.other_process(seconds_ago=0.0)
        self.make_guard().execute(self.read_req(), self.confirm)

    def test_no_history_is_accepted_empty_and_missing(self) -> None:
        guard = self.make_guard()
        self.assertEqual(self.audit_path.read_bytes(), b"")  # AuditLog created it, empty
        guard.execute(self.read_req(), self.confirm)
        self.audit_path.unlink()  # missing: no history either
        self.advance(61.0)
        guard.execute(self.read_req(), self.confirm)

    def test_a_dwell_refusal_does_not_restart_the_window(self) -> None:
        self.other_process(seconds_ago=40)
        guard = self.make_guard()
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.advance(20.0)
        guard.execute(self.read_req(), self.confirm)  # 60 s after the other process, not after the refusal

    def test_the_readback_of_a_write_is_exempt(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=0.0)
        guard = self.guard_with_baseline()
        self.transport.before_return = lambda call: self.wall.advance(5.0)
        result = guard.execute(self.write_req(self.vf(-5)), self.confirm)
        self.assertIs(result.verified, True)
        records = self.records()
        attempts = [r for r in records if r["event"] == "attempt"]
        readback = next(r for r in records if r["event"] == "readback")
        state = read_state(self.audit_path)
        self.assertEqual(state.last_attempt_wall, datetime.fromisoformat(attempts[-1]["ts"]).timestamp())
        self.assertGreater(datetime.fromisoformat(readback["ts"]).timestamp(), state.last_attempt_wall)  # later, yet ignored
        self.assertEqual(dict(state.attempts_by_boot), {BOOT_A: 4})  # three reads (0x10, 0x1C, 0x09) and the write; not the read-back

    def test_the_in_process_dwell_still_applies_with_no_audit_history(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.audit_path.write_bytes(b"")  # the log says nothing; the guard's own memory still refuses
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)

    def test_an_unusable_wall_clock_refuses(self) -> None:
        # The same clock stamps the audit records, so the refusal itself cannot always be logged: check what is raised and sent.
        self.other_process(seconds_ago=1000)
        clocks = {
            "nan": lambda: math.nan,
            "inf": lambda: math.inf,
            "text": lambda: "now",
            "bool": lambda: True,
            "raises": mock.Mock(side_effect=RuntimeError("no clock")),
        }
        for label, wall in clocks.items():
            with self.subTest(label):
                guard = self.make_guard(wall_clock=wall, lock_path=self.dir / f"{label}.lock")
                with self.assertRaises(GuardRefused) as cm:
                    guard.execute(self.read_req(), self.confirm)
                self.assertIs(cm.exception.reason, Refusal.DWELL)
                self.assertEqual(self.transport.calls, [])

    def test_the_guard_never_sleeps_for_the_dwell(self) -> None:
        self.other_process(seconds_ago=1)
        guard = self.make_guard()
        with mock.patch("time.sleep", side_effect=AssertionError("the guard must not sleep")):
            self.assert_refused(guard, self.read_req(), Refusal.DWELL)

    def test_the_audit_dwell_precedes_the_lock_and_the_health_gate(self) -> None:
        self.other_process(seconds_ago=1)
        self.lock_path.write_text("999\n")
        guard = self.make_guard(health_gate=lambda phase: (False, "gate says no"))
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertEqual(self.lock_path.read_text(), "999\n")  # a foreign lock is left alone

    def test_the_in_process_dwell_reports_the_remaining_wait_too(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.clock.advance(20.0)  # only the monotonic clock: the in-process check is the one that fires
        self.wall.advance(61.0)
        exc = self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertAlmostEqual(exc.retry_after_s or 0, 40.0, places=3)

    def vf(self, offset_raw: int) -> int:
        from .test_guard import STOCK

        return STOCK.with_(offset_raw=offset_raw).to_u32()


class WallClockDefault(LimitsCase):
    """``wall_clock=None``: the wall clock moves with an injected ``clock`` so fake-clock dwell waits are seen by the audit."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = dataclasses.replace(self.policy, min_dwell_s=60.0)

    def test_dwell_waits_on_the_injected_clock_satisfy_the_audit_dwell(self) -> None:
        guard = self.make_guard(wall_clock=None)
        guard.execute(self.read_req(), self.confirm)
        self.clock.advance(59.0)
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.clock.advance(1.0)
        guard.execute(self.read_req(), self.confirm)

    def test_anchored_wall_clock_follows_the_clock_from_the_wall_now(self) -> None:
        clock, wall = FakeClock(5.0), FakeClock(T0)
        anchored = anchored_wall_clock(clock, wall)
        self.assertEqual(anchored(), T0)
        clock.advance(12.5)
        wall.advance(9999.0)  # the real wall clock is not consulted again
        self.assertEqual(anchored(), T0 + 12.5)

    def test_the_default_clocks_are_the_real_ones(self) -> None:
        import time

        guard = WriteGuard(
            self.transport, self.policy, AuditLog(self.audit_path, fsync=False),
            session_token=self.TOKEN, env=self.env, lock_path=self.lock_path, boot_id_fn=self.boot_id,
        )
        self.assertIs(guard._wall_clock, time.time)  # noqa: SLF001


# =====================================================================================================================
class BootLimit(LimitsCase):
    def test_twelve_attempts_for_this_boot_refuse_and_eleven_do_not(self) -> None:
        self.assertEqual(self.policy.max_operations_per_boot, 12)  # the T-016 default, through the default policy
        self.other_process(count=11)
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)  # the 12th is allowed ...
        exc = self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)  # ... the 13th is not
        self.assertIn(BOOT_A, exc.detail)
        self.assertIn("12", exc.detail)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertFalse(guard.tripped)

    def test_twelve_attempts_already_in_the_log_refuse_the_first_request(self) -> None:
        self.other_process(count=12)
        self.assert_refused(self.make_guard(), self.read_req(), Refusal.BOOT_LIMIT)
        self.assertEqual(self.transport.calls, [])

    def test_attempts_of_a_different_boot_do_not_count(self) -> None:
        self.other_process(count=12, boot_id=BOOT_B)
        self.make_guard().execute(self.read_req(), self.confirm)

    def test_the_unknown_boot_is_its_own_capped_bucket(self) -> None:
        self.boot = None  # type: ignore[assignment]
        self.other_process(count=12, boot_id=None)
        guard = self.make_guard()
        exc = self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)
        self.assertIn("unknown boot", exc.detail)

    def test_unknown_boot_attempts_do_not_count_against_a_known_boot_and_vice_versa(self) -> None:
        self.other_process(count=12, boot_id=None)
        self.make_guard().execute(self.read_req(), self.confirm)  # known boot A: fine
        self.other_process(count=12, boot_id=BOOT_A)
        self.boot = None  # type: ignore[assignment]
        self.lock_path.unlink(missing_ok=True)
        self.assert_refused(self.make_guard(lock_path=self.dir / "n.lock"), self.read_req(), Refusal.BOOT_LIMIT)  # bucket None is full

    def test_records_written_before_t016_have_no_boot_id_and_count_as_unknown(self) -> None:
        legacy = {"ts": iso(T0 - 3600), "seq": 0, "event": "attempt", "cpu": 0, "cmd": "0x10"}
        self.audit_path.write_text("".join(json.dumps(legacy) + "\n" for _ in range(12)), encoding="utf-8")
        self.boot = None  # type: ignore[assignment]
        self.assert_refused(self.make_guard(), self.read_req(), Refusal.BOOT_LIMIT)

    def test_a_failing_boot_id_fn_falls_into_the_capped_unknown_bucket(self) -> None:
        self.other_process(count=12, boot_id=None)
        guard = self.make_guard(boot_id_fn=mock.Mock(side_effect=OSError("/proc gone")))
        self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)

    def test_the_cap_is_shared_by_separate_guards_on_one_audit_file(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_boot=2)
        self.make_guard(lock_path=self.dir / "1.lock").execute(self.read_req(), self.confirm)  # "process" 1
        self.make_guard(lock_path=self.dir / "2.lock").execute(self.read_req(cpu=1), self.confirm)  # "process" 2
        third = self.make_guard(lock_path=self.dir / "3.lock")
        self.assert_refused(third, self.read_req(cpu=2), Refusal.BOOT_LIMIT)

    def test_reads_count_and_the_readback_does_not(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_boot=5)
        guard = self.guard_with_baseline()  # operations 1-3 (reads: 0x10, 0x1C, 0x09)
        result = guard.execute(self.write_req(self.stock(-5)), self.confirm)  # operation 4 (+ read-back through the transport)
        self.assertIs(result.verified, True)
        self.assertEqual(len(self.transport.calls), 5)  # three reads, write, read-back: five transactions, four operations
        guard.execute(self.read_req(), self.confirm)  # operation 5: allowed only if the read-back was not counted
        self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)
        self.assertEqual(dict(read_state(self.audit_path).attempts_by_boot), {BOOT_A: 5})

    def test_refusals_do_not_consume_the_cap(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_boot=1)
        guard = self.make_guard()
        self.assert_refused(guard, self.read_req(cpu=99), Refusal.CPU_NOT_ALLOWED)
        self.assert_refused(guard, Request(0, 0x12, 0, 0, 0, "x"), Refusal.NOT_ALLOWLISTED)
        guard.execute(self.read_req(), self.confirm)

    def test_a_transport_failure_still_counts(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_boot=1)
        self.transport.raise_on_call[1] = RuntimeError("EIO")
        with self.assertRaises(RuntimeError):
            self.make_guard().execute(self.read_req(), self.confirm)
        self.assert_refused(self.make_guard(lock_path=self.dir / "n.lock"), self.read_req(), Refusal.BOOT_LIMIT)

    def test_the_session_limit_comes_first_and_the_boot_limit_beats_the_dwell(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_session=1, max_operations_per_boot=1, min_dwell_s=60.0)
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.assert_refused(guard, self.read_req(), Refusal.SESSION_LIMIT)  # both limits reached: the earlier check names it
        other = self.make_guard(lock_path=self.dir / "o.lock")  # a fresh session: its own counter is 0
        self.assert_refused(other, self.read_req(), Refusal.BOOT_LIMIT)  # the dwell (unmet too) comes after the boot cap

    def test_the_boot_limit_precedes_the_lock_and_the_health_gate(self) -> None:
        self.other_process(count=12)
        self.lock_path.write_text("999\n")
        guard = self.make_guard(health_gate=lambda phase: (False, "no"))
        self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)

    def test_the_boot_id_is_read_once_per_request_and_matches_the_record(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.assertEqual(self.boot_calls, 1)
        self.boot = BOOT_B  # a (hypothetical) change between requests is picked up: the id is not cached
        guard.execute(self.read_req(cpu=1), self.confirm)
        self.assertEqual([r["boot_id"] for r in self.records() if r["event"] == "attempt"], [BOOT_A, BOOT_B])

    def stock(self, offset_raw: int) -> int:
        from .test_guard import STOCK

        return STOCK.with_(offset_raw=offset_raw).to_u32()


# =====================================================================================================================
class AuditUnavailable(LimitsCase):
    def test_a_corrupted_middle_line_refuses_and_sends_nothing(self) -> None:
        self.other_process()
        with open(self.audit_path, "ab") as handle:
            handle.write(b'{"ts": "2023-11-14T22:13\n')  # corrupt, and complete (newline-terminated)
        self.other_process()
        guard = self.make_guard()
        exc = self.assert_refused_unreadable(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)
        self.assertIn("audit", exc.detail)
        self.assertFalse(guard.tripped)
        self.assertEqual(self.transport.calls, [])

    def test_a_truncated_trailing_line_is_tolerated_by_the_guard(self) -> None:
        self.other_process(seconds_ago=100)
        with open(self.audit_path, "ab") as handle:
            handle.write(b'{"ts": "2023-11-14T22:1')  # a crashed writer left half a line, no newline
        guard = self.make_guard()
        self.assertEqual(guard.execute(self.read_req(), self.confirm).response.rc, 0)

    def test_an_unreadable_log_beats_every_later_check(self) -> None:
        self.audit_path.write_text("garbage\n" + json.dumps({"event": "trip"}) + "\n", encoding="utf-8")
        self.lock_path.write_text("999\n")
        guard = self.make_guard(health_gate=lambda phase: (False, "no"))
        self.assert_refused_unreadable(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)
        self.assertEqual(self.lock_path.read_text(), "999\n")

    def test_an_oserror_reading_the_log_refuses(self) -> None:
        guard = self.make_guard()
        with mock.patch.object(guard_module, "read_state", side_effect=PermissionError("denied")):
            exc = self.assert_refused(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)
        self.assertIn("denied", exc.detail)

    def test_any_failure_to_evaluate_the_log_refuses(self) -> None:
        guard = self.make_guard()
        for bad in (RuntimeError("boom"), ValueError("odd")):
            with self.subTest(bad=bad), mock.patch.object(guard_module, "read_state", side_effect=bad):
                self.assert_refused(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)
        with mock.patch.object(guard_module, "read_state", return_value=object()):  # not an AuditState
            self.assert_refused(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)

    def test_the_log_becoming_unreadable_mid_session_refuses_the_next_request(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        with open(self.audit_path, "ab") as handle:
            handle.write(b"oops\n")
        self.advance(1.0)
        self.assert_refused_unreadable(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)
        self.assertEqual(len(self.transport.calls), 1)

    def test_the_refusal_is_raised_even_when_it_cannot_be_audited(self) -> None:
        class Mute(AuditLog):
            def append(self, *a, **k):  # noqa: ANN002, ANN003
                raise OSError("disk full")

        guard = self.make_guard(audit=Mute(self.audit_path, fsync=False))
        with mock.patch.object(guard_module, "read_state", side_effect=OSError("cannot read")):
            with self.assertRaises(GuardRefused) as cm:
                guard.execute(self.read_req(), self.confirm)
        self.assertIs(cm.exception.reason, Refusal.AUDIT_UNAVAILABLE)
        self.assertIn("audit write failed", cm.exception.detail)
        self.assertEqual(self.transport.calls, [])

    def test_an_audit_object_without_a_path_fails_closed(self) -> None:
        class NoPath:
            def append(self, *a, **k):  # noqa: ANN002, ANN003
                return {}

        guard = self.make_guard(audit=NoPath())
        with self.assertRaises(GuardRefused) as cm:
            guard.execute(self.read_req(), self.confirm)
        self.assertIs(cm.exception.reason, Refusal.AUDIT_UNAVAILABLE)
        self.assertEqual(self.transport.calls, [])


# =====================================================================================================================
class UnderTheLock(LimitsCase):
    """Two processes that start together both pass the first look; the second look, under the lock, must refuse."""

    def race(self, guard: WriteGuard, **winner: object) -> None:
        """Right after ``guard`` takes the lock, 'another process' records attempts (it raced us past the first checks)."""
        real = guard._take_lock  # noqa: SLF001

        def take(req: Request, confirm: object) -> None:
            real(req, confirm)
            self.other_process(**winner)  # type: ignore[arg-type]

        guard._take_lock = take  # type: ignore[method-assign]  # noqa: SLF001

    def test_a_dwell_violation_that_appears_after_the_first_look_is_refused_and_the_lock_released(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=60.0)
        guard = self.make_guard()
        self.race(guard, seconds_ago=0.0)
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.lock_path.exists())  # released on the way out

    def test_a_boot_limit_violation_that_appears_after_the_first_look_is_refused(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_boot=3)
        self.other_process(count=2)
        guard = self.make_guard()
        self.race(guard, count=1)
        self.assert_refused(guard, self.read_req(), Refusal.BOOT_LIMIT)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.lock_path.exists())

    def test_an_audit_that_turns_unreadable_after_the_first_look_is_refused(self) -> None:
        guard = self.make_guard()
        real = guard._take_lock  # noqa: SLF001

        def take(req: Request, confirm: object) -> None:
            real(req, confirm)
            with open(self.audit_path, "ab") as handle:
                handle.write(b"garbage\nmore\n")

        guard._take_lock = take  # type: ignore[method-assign]  # noqa: SLF001
        self.assert_refused_unreadable(guard, self.read_req(), Refusal.AUDIT_UNAVAILABLE)
        self.assertEqual(self.transport.calls, [])


# =====================================================================================================================
class PolicyField(unittest.TestCase):
    TOML = 'cpus = [0]\n{extra}'

    def test_defaults(self) -> None:
        self.assertEqual(default_m1_policy().max_operations_per_boot, 12)
        self.assertEqual(Policy(cpus=frozenset({0})).max_operations_per_boot, 12)
        self.assertEqual(load_policy(self.TOML.format(extra="")).max_operations_per_boot, 12)

    def test_loaded_from_toml(self) -> None:
        policy = load_policy(self.TOML.format(extra="max_operations_per_boot = 5\nmax_operations_per_session = 2\n"))
        self.assertEqual((policy.max_operations_per_boot, policy.max_operations_per_session), (5, 2))

    def test_invalid_values_are_rejected(self) -> None:
        for label, value in {"zero": "0", "negative": "-3", "bool": "true", "string": '"12"', "float": "1.5"}.items():
            with self.subTest(label), self.assertRaises(PolicyError) as cm:
                load_policy(self.TOML.format(extra=f"max_operations_per_boot = {value}\n"))
            self.assertIn("max_operations_per_boot", str(cm.exception))
        for bad in (0, -1, True, 2.0, "12", None):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                Policy(cpus=frozenset({0}), max_operations_per_boot=bad)  # type: ignore[arg-type]

    def test_minimum_value_one_is_accepted(self) -> None:
        self.assertEqual(Policy(cpus=frozenset({0}), max_operations_per_boot=1).max_operations_per_boot, 1)

    def test_unknown_keys_are_still_rejected(self) -> None:
        with self.assertRaises(PolicyError):
            load_policy(self.TOML.format(extra="max_operations_per_reboot = 5\n"))

    def test_existing_positional_construction_is_unchanged(self) -> None:
        policy = Policy(frozenset({0}), 3, 5.0, ())  # (cpus, max_operations_per_session, min_dwell_s, allow)
        self.assertEqual((policy.max_operations_per_session, policy.min_dwell_s, policy.max_operations_per_boot), (3, 5.0, 12))


class Refusals(unittest.TestCase):
    def test_new_reasons_are_string_enum_members(self) -> None:
        self.assertEqual(Refusal.BOOT_LIMIT, "BOOT_LIMIT")
        self.assertEqual(Refusal.AUDIT_UNAVAILABLE, "AUDIT_UNAVAILABLE")

    def test_guard_refused_carries_an_optional_retry_time(self) -> None:
        self.assertIsNone(GuardRefused(Refusal.DWELL, "x").retry_after_s)
        self.assertEqual(GuardRefused(Refusal.DWELL, "x", 12.5).retry_after_s, 12.5)


if __name__ == "__main__":
    unittest.main()
