"""T-013 WriteGuard tests (no hardware: a recording fake transport and a fake clock).

Rules under test: docs/tasks/T-013-guard.md "execute -- checks in this order", docs/SAFETY.md "Write-session protocol" and
"Mailbox-session discipline". Facts: docs/knowledge/registers.md ("V/F data word", "Mailbox commands", "Return codes").

Every refusal test goes through ``assert_refused``: it checks the reason, that the recording transport saw *nothing new*,
and that the refusal was audited. Each test is written so that removing the corresponding check in ``guard.py`` or
``policy.py`` makes it fail (something would be sent, or the wrong reason would come back).
"""
from __future__ import annotations

import ast
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arltuner.guard import (
    ENV_FLAG,
    AllowEntry,
    AuditLog,
    Confirmation,
    GuardRefused,
    Policy,
    Refusal,
    Request,
    Result,
    WriteGuard,
    default_m1_policy,
    read_all,
)
from arltuner.mailbox import WRITE_COMMANDS, Cmd, Response, VfWord

from .fakes import FakeClock, FakeMailboxTimeout, FakeMsrError, RecordingTransport

STOCK = VfWord(ratio=55, v_target_raw=0x4A0, static=False, offset_raw=0)
STOCK_WORD = STOCK.to_u32()
STOCK_REVERT = STOCK.with_(v_target_raw=0).to_u32()  # G-124: a revert word without an OC point (ratio <= the top fused ratio) carries V target 0
#: the session facts a 0x11 needs (T-021): the 0x1C word (favored ratio [7:0], fused [15:8]) and the 0x09 P1=0x11 fused point
FAVORED_WORD = 55 | 55 << 8
FUSED_WORD = 55 | 0x4A0 << 8
ALLOWED_CMDS = {0x01, 0x02, 0x09, 0x1C, 0x28, 0x14, 0x10, 0x11, 0x15, 0x1D, 0x26, 0x27}
SRC = Path(__file__).resolve().parents[2] / "src" / "arltuner" / "guard"
_DEFAULT_CONFIRM = object()


class GuardCase(unittest.TestCase):
    TOKEN = "tok-7f3a9c"
    OPERATOR = "yk"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.audit_path = self.dir / "audit.jsonl"
        self.lock_path = self.dir / "guard.lock"
        self.transport = RecordingTransport()
        self.clock = FakeClock(1000.0)
        self.wall = FakeClock(1_700_000_000.0)
        self.env: dict[str, str] = {ENV_FLAG: "1"}
        # D-049: no operator value limits; the CPU's own ratio limit (cmd 0x01) is bound as 120x, the voltage limit (cmd 0x28) stays unread
        self.policy = dataclasses.replace(
            default_m1_policy(),
            max_operations_per_session=10,
            min_dwell_s=0.0,
            hw_ratio_max={"P": 120, "E": 120},
        )
        self.confirm = Confirmation(self.TOKEN, self.OPERATOR)
        self.gate_calls: list[str] = []

    # ------------------------------------------------------------ helpers
    def advance(self, seconds: float) -> None:
        """Time passes for both clocks: the in-process dwell uses the monotonic one, the audit-based dwell (T-016) the wall one."""
        self.clock.advance(seconds)
        self.wall.advance(seconds)

    def make_guard(self, **overrides: object) -> WriteGuard:
        kwargs: dict[str, object] = dict(
            session_token=self.TOKEN,
            env=self.env,
            lock_path=self.lock_path,
            clock=self.clock,
            wall_clock=self.wall,
            boot_id_fn=lambda: "test-boot",  # T-016: never read the real /proc in a test
        )
        kwargs.update(overrides)
        audit = kwargs.pop("audit", None) or AuditLog(self.audit_path, fsync=False)
        policy = kwargs.pop("policy", self.policy)
        transport = kwargs.pop("transport", self.transport)
        return WriteGuard(transport, policy, audit, **kwargs)  # type: ignore[arg-type]

    def records(self) -> list[dict]:
        return read_all(self.audit_path)

    @staticmethod
    def read_req(cpu: int = 0, cmd: int = 0x10, p1: int = 0, p2: int = 0) -> Request:
        return Request(cpu, cmd, p1, p2, 0, "test read")

    @staticmethod
    def write_req(data: int, cpu: int = 0, cmd: int = 0x11, p1: int = 0, p2: int = 0) -> Request:
        return Request(cpu, cmd, p1, p2, data, "test write")

    def assert_refused(
        self, guard: WriteGuard, req: Request, reason: Refusal, confirm: object = _DEFAULT_CONFIRM
    ) -> GuardRefused:
        before = list(self.transport.calls)
        with self.assertRaises(GuardRefused) as cm:
            guard.execute(req, self.confirm if confirm is _DEFAULT_CONFIRM else confirm)  # type: ignore[arg-type]
        self.assertIs(cm.exception.reason, reason, cm.exception.detail)
        self.assertEqual(self.transport.calls, before, "a refusal must send nothing")
        last = self.records()[-1]
        self.assertEqual((last["event"], last["reason"]), ("refused", reason.value))
        return cm.exception

    def seed_vf(self, cpu: int = 0, word: int = STOCK_WORD) -> None:
        self.transport.state[(cpu, 0x10, 0, 0)] = word

    def seed_facts(self, cpu: int = 0, favored: int = FAVORED_WORD, fused: int = FUSED_WORD) -> None:
        self.transport.state[(cpu, 0x1C, 0, 0)] = favored
        self.transport.state[(cpu, 0x09, 0x11, 0)] = fused

    def baseline_read(self, guard: WriteGuard, cpu: int = 0, cmd: int = 0x10) -> Result:
        return guard.execute(self.read_req(cpu, cmd, p1=0x11 if cmd == 0x09 else 0), self.confirm)

    def fact_reads(self, guard: WriteGuard, cpu: int = 0) -> None:
        """The two reads (0x1C, 0x09 P1=0x11) the 0x11 check needs besides the 0x10 baseline."""
        self.baseline_read(guard, cpu, 0x1C)
        self.baseline_read(guard, cpu, 0x09)

    @staticmethod
    def baseline_calls(cpu: int = 0) -> list[tuple[int, int, int, int, int]]:
        """What ``guard_with_baseline`` puts on the wire."""
        return [(cpu, 0x10, 0, 0, 0), (cpu, 0x1C, 0, 0, 0), (cpu, 0x09, 0x11, 0, 0)]

    def guard_with_baseline(self, cpu: int = 0, word: int = STOCK_WORD, **overrides: object) -> WriteGuard:
        """A guard that has read the 0x10 baseline AND the session facts (0x1C, 0x09) of ``cpu``: 3 operations."""
        self.seed_vf(cpu, word)
        self.seed_facts(cpu)
        guard = self.make_guard(**overrides)
        self.assertEqual(self.baseline_read(guard, cpu).response, Response(0, word))
        self.fact_reads(guard, cpu)
        return guard


# =====================================================================================================================
class Ordering(GuardCase):
    def test_refusal_enum_is_exactly_the_spec(self) -> None:
        self.assertEqual(
            [r.name for r in Refusal],
            ["NO_ENV_FLAG", "BAD_CONFIRMATION", "CPU_NOT_ALLOWED", "NOT_ALLOWLISTED", "CHECK_FAILED", "BASELINE_MISSING",
             "SESSION_LIMIT", "AUDIT_UNAVAILABLE", "BOOT_LIMIT", "DWELL", "LOCKED", "HEALTH_GATE", "TRIPPED"],
        )
        self.assertEqual(Refusal.TRIPPED, "TRIPPED")  # str enum

    def test_earliest_failing_check_wins(self) -> None:
        guard = self.make_guard()
        bad_confirm = Confirmation("wrong", self.OPERATOR)
        bad_req = self.write_req(data=1 << 40, cpu=99, cmd=0x12)  # bad cpu, non-allowlisted cmd, bad data
        del self.env[ENV_FLAG]
        self.assert_refused(guard, bad_req, Refusal.NO_ENV_FLAG, bad_confirm)
        self.env[ENV_FLAG] = "1"
        self.assert_refused(guard, bad_req, Refusal.BAD_CONFIRMATION, bad_confirm)
        self.assert_refused(guard, bad_req, Refusal.CPU_NOT_ALLOWED)
        self.assert_refused(guard, dataclasses.replace(bad_req, cpu=0), Refusal.NOT_ALLOWLISTED)
        self.assert_refused(guard, dataclasses.replace(bad_req, cpu=0, cmd=0x11), Refusal.BASELINE_MISSING)
        self.seed_vf()
        guard.execute(self.read_req(), self.confirm)
        self.assert_refused(guard, dataclasses.replace(bad_req, cpu=0, cmd=0x11), Refusal.CHECK_FAILED)

    def test_session_limit_precedes_dwell_precedes_lock_precedes_health(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_session=2, min_dwell_s=60.0)
        healthy = {"ok": True}
        guard = self.make_guard(health_gate=lambda phase: (healthy["ok"], "gate detail"))
        guard.execute(self.read_req(), self.confirm)  # operation 1
        healthy["ok"] = False
        self.lock_path.write_text("999" + chr(10))  # a lock, an unmet dwell and a failing gate all apply now
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.advance(60.0)
        self.assert_refused(guard, self.read_req(), Refusal.LOCKED)
        self.lock_path.unlink()
        self.assert_refused(guard, self.read_req(), Refusal.HEALTH_GATE)
        healthy["ok"] = True
        guard.execute(self.read_req(), self.confirm)  # operation 2
        self.lock_path.write_text("999" + chr(10))
        self.assert_refused(guard, self.read_req(), Refusal.SESSION_LIMIT)  # beats the unmet dwell and the lock

    def test_tripped_beats_everything(self) -> None:
        self.transport.raise_on_call[1] = FakeMsrError("EIO")
        guard = self.make_guard()
        with self.assertRaises(FakeMsrError):
            guard.execute(self.read_req(), self.confirm)
        del self.env[ENV_FLAG]
        self.assert_refused(guard, self.read_req(), Refusal.TRIPPED, Confirmation("wrong", ""))

    def test_execute_needs_a_request_object(self) -> None:
        guard = self.make_guard()
        with self.assertRaises(TypeError):
            guard.execute((0, 0x10, 0, 0, 0), self.confirm)  # type: ignore[arg-type]
        self.assertEqual(self.transport.calls, [])

    def test_constructor_fails_closed(self) -> None:
        for token in ("", None, b"x"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.make_guard(session_token=token)
        with self.assertRaises(TypeError):
            self.make_guard(policy={"cpus": [0]})
        with self.assertRaises(TypeError):
            self.make_guard(transport=object())


class EnvFlag(GuardCase):
    def test_absent_or_not_exactly_one_is_refused(self) -> None:
        guard = self.make_guard()
        for value in (None, "", "0", "true", "yes", " 1", "1 ", "01"):
            with self.subTest(value=value):
                if value is None:
                    self.env.pop(ENV_FLAG, None)
                else:
                    self.env[ENV_FLAG] = value
                self.assert_refused(guard, self.read_req(), Refusal.NO_ENV_FLAG)
        self.env[ENV_FLAG] = "1"
        guard.execute(self.read_req(), self.confirm)  # and with the flag it goes through
        self.assertEqual(len(self.transport.calls), 1)

    def test_default_env_is_the_live_process_environment(self) -> None:
        guard = self.make_guard(env=None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_FLAG, None)
            self.assert_refused(guard, self.read_req(), Refusal.NO_ENV_FLAG)
            os.environ[ENV_FLAG] = "1"
            guard.execute(self.read_req(), self.confirm)
            os.environ[ENV_FLAG] = "0"  # withdrawn mid-session
            self.assert_refused(guard, self.read_req(), Refusal.NO_ENV_FLAG)

    def test_broken_env_mapping_fails_closed(self) -> None:
        class Broken(dict):
            def get(self, *a, **k):  # noqa: ANN002, ANN003
                raise RuntimeError("boom")

        guard = self.make_guard(env=Broken())
        self.assert_refused(guard, self.read_req(), Refusal.NO_ENV_FLAG)


class Confirmations(GuardCase):
    def test_bad_confirmations_are_refused(self) -> None:
        guard = self.make_guard()
        cases = {
            "wrong token": Confirmation("nope", self.OPERATOR),
            "empty token": Confirmation("", self.OPERATOR),
            "token prefix": Confirmation(self.TOKEN[:-1], self.OPERATOR),
            "token superstring": Confirmation(self.TOKEN + "x", self.OPERATOR),
            "empty operator": Confirmation(self.TOKEN, ""),
            "blank operator": Confirmation(self.TOKEN, "   "),
            "non-str token": Confirmation(123, self.OPERATOR),  # type: ignore[arg-type]
            "non-str operator": Confirmation(self.TOKEN, None),  # type: ignore[arg-type]
            "no confirmation at all": None,
            "wrong type": (self.TOKEN, self.OPERATOR),
        }
        for label, confirm in cases.items():
            with self.subTest(label):
                self.assert_refused(guard, self.read_req(), Refusal.BAD_CONFIRMATION, confirm)  # type: ignore[arg-type]
        guard.execute(self.read_req(), self.confirm)

    def test_token_never_reaches_repr_or_audit_log(self) -> None:
        guard = self.make_guard()
        wrong = Confirmation("hunter2-wrong", self.OPERATOR)
        self.assert_refused(guard, self.read_req(), Refusal.BAD_CONFIRMATION, wrong)
        guard.execute(self.read_req(), self.confirm)
        self.assertNotIn(self.TOKEN, repr(self.confirm))
        log = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(self.TOKEN, log)
        self.assertNotIn("hunter2-wrong", log)
        self.assertIn(self.OPERATOR, log)


class CpuSet(GuardCase):
    def test_cpu_outside_the_policy_set(self) -> None:
        guard = self.make_guard()
        for cpu in (24, -1, 1000, True, "0", 0.0, None):
            with self.subTest(cpu=cpu):
                self.assert_refused(guard, self.read_req(cpu=cpu), Refusal.CPU_NOT_ALLOWED)  # type: ignore[arg-type]
        for cpu in (0, 23):
            guard.execute(self.read_req(cpu=cpu), self.confirm)

    def test_policy_can_narrow_the_cpu_set(self) -> None:
        guard = self.make_guard(policy=dataclasses.replace(self.policy, cpus=frozenset({0, 3})))
        self.assert_refused(guard, self.read_req(cpu=1), Refusal.CPU_NOT_ALLOWED)
        guard.execute(self.read_req(cpu=3), self.confirm)


class Allowlist(GuardCase):
    def test_every_cmd_not_in_the_allowlist_is_refused(self) -> None:
        guard = self.make_guard()
        self.assertEqual({e.cmd for e in default_m1_policy().allow}, ALLOWED_CMDS)
        refused = 0
        for cmd in Cmd:
            if int(cmd) in ALLOWED_CMDS:
                continue
            with self.subTest(cmd=cmd.name):
                self.assert_refused(guard, Request(0, cmd, 0, 0, 0, "probe"), Refusal.NOT_ALLOWLISTED)
                refused += 1
        self.assertEqual(refused, len(Cmd) - len(ALLOWED_CMDS))
        self.assertEqual(self.transport.calls, [])

    def test_every_write_command_except_the_four_allowlisted_is_refused_even_with_data(self) -> None:
        guard = self.make_guard()
        for cmd in sorted(WRITE_COMMANDS - {0x11, 0x15, 0x1D, 0x27}):
            with self.subTest(cmd=hex(cmd)):
                self.assert_refused(guard, Request(0, cmd, 0, 0, 8, "probe"), Refusal.NOT_ALLOWLISTED)

    def test_unnamed_command_ids_are_refused(self) -> None:
        guard = self.make_guard()
        for cmd in (0x00, 0x06, 0x08, 0x0A, 0x29, 0x30, 0xFE, 0xFF):
            with self.subTest(cmd=hex(cmd)):
                self.assert_refused(guard, Request(0, cmd, 0, 0, 0, "probe"), Refusal.NOT_ALLOWLISTED)

    def test_allowlisted_cmd_with_p1_or_p2_outside_its_entry_is_refused(self) -> None:
        guard = self.make_guard()
        for cmd, p1, p2 in (
            (0x10, 1, 0), (0x10, 3, 0), (0x10, 0, 1), (0x10, 0, 15),  # domain GT, V/F points (hidden on ARL-S)
            (0x11, 2, 0), (0x11, 0, 1), (0x11, 7, 0),
            (0x14, 0, 1), (0x14, 1, 0), (0x15, 1, 0), (0x15, 0, 1),
            (0x1D, 1, 0), (0x1D, 0, 1), (0x26, 1, 0), (0x26, 0, 1), (0x27, 1, 0), (0x27, 0, 1),
            (0x01, 8, 0), (0x01, 0, 1), (0x1C, 1, 0), (0x28, 1, 0), (0x09, 0, 0), (0x09, 0x21, 0),
        ):
            with self.subTest(cmd=hex(cmd), p1=p1, p2=p2):
                self.assert_refused(guard, Request(0, cmd, p1, p2, 0, "probe"), Refusal.NOT_ALLOWLISTED)

    def test_non_int_fields_are_refused(self) -> None:
        guard = self.make_guard()
        for req in (
            Request(0, True, 0, 0, 0, "x"),  # type: ignore[arg-type]
            Request(0, 0x10, "0", 0, 0, "x"),  # type: ignore[arg-type]
            Request(0, 0x10, 0, 0.0, 0, "x"),  # type: ignore[arg-type]
            Request(0, None, 0, 0, 0, "x"),  # type: ignore[arg-type]
        ):
            with self.subTest(req=req):
                self.assert_refused(guard, req, Refusal.NOT_ALLOWLISTED)

    def test_every_allowlisted_read_is_sent_exactly_once_when_allowed(self) -> None:
        for e in default_m1_policy().allow:
            if e.kind != "read":
                continue
            with self.subTest(e.name):
                self.transport = RecordingTransport()
                guard = self.make_guard(transport=self.transport)
                p1, p2 = min(e.p1), min(e.p2)
                guard.execute(Request(0, e.cmd, p1, p2, 0, "probe"), self.confirm)
                self.assertEqual(self.transport.calls, [(0, e.cmd, p1, p2, 0)])


class Baseline(GuardCase):
    def test_write_without_baseline_is_refused(self) -> None:
        guard = self.make_guard()
        self.assert_refused(guard, self.write_req(STOCK.with_(offset_raw=-10).to_u32()), Refusal.BASELINE_MISSING)
        self.assert_refused(guard, self.write_req(0x8, cmd=0x15), Refusal.BASELINE_MISSING)

    def test_baseline_must_match_cpu_cmd_and_be_successful(self) -> None:
        self.seed_vf(cpu=1)
        self.transport.read_rc = 0
        guard = self.make_guard()
        guard.execute(self.read_req(cpu=1), self.confirm)  # baseline for cpu 1 only
        self.assert_refused(guard, self.write_req(STOCK.to_u32(), cpu=0), Refusal.BASELINE_MISSING)
        # a V/F baseline does not baseline the per-core enable word, and vice versa
        self.assert_refused(guard, self.write_req(0x8, cpu=1, cmd=0x15), Refusal.BASELINE_MISSING)
        # a read that completed with a non-zero rc is not a baseline
        self.transport.read_rc = 0x13
        guard.execute(self.read_req(cpu=2), self.confirm)
        self.transport.read_rc = 0
        self.assert_refused(guard, self.write_req(STOCK.to_u32(), cpu=2), Refusal.BASELINE_MISSING)

    def test_write_after_baseline_read_is_sent(self) -> None:
        guard = self.guard_with_baseline()
        new = STOCK.with_(offset_raw=-50).to_u32()
        result = guard.execute(self.write_req(new), self.confirm)
        self.assertEqual(result.response, Response(0, 0))
        self.assertEqual(self.transport.calls, [*self.baseline_calls(), (0, 0x11, 0, 0, new), (0, 0x10, 0, 0, 0)])

    def test_percore_enable_write_after_baseline(self) -> None:
        self.transport.state[(0, 0x14, 0, 0)] = 0x1
        guard = self.make_guard()
        guard.execute(self.read_req(cmd=0x14), self.confirm)
        result = guard.execute(self.write_req(0x9, cmd=0x15), self.confirm)
        self.assertIs(result.verified, True)
        self.assertEqual(self.transport.calls[1:], [(0, 0x15, 0, 0, 0x9), (0, 0x14, 0, 0, 0)])

    def test_write_with_no_read_twin_is_refused_even_if_a_policy_lists_it(self) -> None:
        policy = Policy(
            cpus=frozenset({0}),
            max_operations_per_session=5,
            min_dwell_s=0,
            allow=(
                AllowEntry("icc_read", Cmd.ICC_MAX_READ, frozenset({0}), frozenset({0}), "read"),
                AllowEntry("icc_write", Cmd.ICC_MAX_WRITE, frozenset({0}), frozenset({0}), "write", "percore_enable_only"),
            ),
        )
        guard = self.make_guard(policy=policy)
        guard.execute(Request(0, 0x16, 0, 0, 0, "read"), self.confirm)
        self.assert_refused(guard, Request(0, 0x17, 0, 0, 8, "write"), Refusal.BASELINE_MISSING)  # 0x17 has no read twin
        # (0x1D and 0x27 have twins since T-021: 0x1C and 0x26 -- see test_measured_checks.py)


class Checks(GuardCase):
    """CHECK_FAILED: nothing is sent for the write; only the baseline read is on the wire."""

    def fresh_session_files(self) -> None:
        """A new audit log per case: each case reads three baseline words and the per-boot cap (12) is shared through that file."""
        self._cases = getattr(self, "_cases", 0) + 1
        self.audit_path = self.dir / f"audit-case{self._cases}.jsonl"

    def refused_vf(self, word: VfWord, baseline: VfWord = STOCK, cpu: int = 0) -> GuardRefused:
        self.fresh_session_files()
        self.transport = RecordingTransport()
        guard = self.guard_with_baseline(cpu=cpu, word=baseline.to_u32(), transport=self.transport)
        exc = self.assert_refused(guard, self.write_req(word.to_u32(), cpu=cpu), Refusal.CHECK_FAILED)
        self.assertEqual(self.transport.calls, self.baseline_calls(cpu))  # only the three baseline reads: the write was not sent
        self.assertFalse(guard.tripped)
        return exc

    def accepted_vf(self, word: VfWord, cpu: int = 0) -> Result:
        self.fresh_session_files()
        self.transport = RecordingTransport()
        guard = self.guard_with_baseline(cpu=cpu, transport=self.transport)
        result = guard.execute(self.write_req(word.to_u32(), cpu=cpu), self.confirm)
        self.assertEqual(self.transport.calls[3], (cpu, 0x11, 0, 0, word.to_u32()))
        return result

    def test_overvolt_refused(self) -> None:
        self.refused_vf(STOCK.with_(v_target_raw=STOCK.v_target_raw + 1, offset_raw=-10))

    def test_ratio_above_baseline_refused(self) -> None:
        self.refused_vf(STOCK.with_(ratio=STOCK.ratio + 1, offset_raw=-10))

    def test_static_mode_refused(self) -> None:
        self.refused_vf(STOCK.with_(static=True, offset_raw=-10))
        self.refused_vf(STOCK.with_(offset_raw=-10), baseline=STOCK.with_(static=True))  # cannot bound a static baseline

    def test_offset_bounds_on_an_e_core(self) -> None:
        # E core (cpu 8): assumed BIOS offset 0 -> runtime floor -100 mV = raw -102 (== the policy minimum -102)
        self.assertEqual(self.accepted_vf(STOCK.with_(offset_raw=-102), cpu=8).verified, True)
        self.assertEqual(self.accepted_vf(STOCK.with_(offset_raw=0), cpu=8).verified, True)
        self.refused_vf(STOCK.with_(offset_raw=-103), cpu=8)
        self.refused_vf(STOCK.with_(offset_raw=1), cpu=8)
        self.refused_vf(STOCK.with_(offset_raw=-1024), cpu=8)

    def test_offset_bounds_on_a_p_core_follow_the_assumed_bios_offset(self) -> None:
        # P core (cpu 0): assumed BIOS offset -50 mV (D-015) -> runtime floor -50 mV = raw -51
        self.assertEqual(self.accepted_vf(STOCK.with_(offset_raw=-51)).verified, True)
        self.assertEqual(self.accepted_vf(STOCK.with_(offset_raw=0)).verified, True)
        self.refused_vf(STOCK.with_(offset_raw=-52))
        self.refused_vf(STOCK.with_(offset_raw=-102))  # the policy minimum alone would have allowed it
        self.refused_vf(STOCK.with_(offset_raw=1))

    def test_lower_ratio_and_voltage_are_accepted_each_in_its_own_word(self) -> None:
        # D-016: lowering is not refused for its direction; a ratio and a V target that both fall take two words (frequency first)
        self.assertIs(self.accepted_vf(STOCK.with_(ratio=50, offset_raw=-40)).verified, True)
        self.assertIs(self.accepted_vf(STOCK.with_(v_target_raw=0x480, offset_raw=-40)).verified, True)
        self.refused_vf(STOCK.with_(ratio=50, v_target_raw=0x480, offset_raw=-40))  # both in one word: the ordering rule (test_bounds.py)

    def test_percore_enable_changing_other_bits_refused(self) -> None:
        self.transport.state[(0, 0x14, 0, 0)] = 0x11
        guard = self.make_guard()
        guard.execute(self.read_req(cmd=0x14), self.confirm)
        for data in (0x11 | 0x8 | 0x100, 0x8, 0x19 & ~0x1, 0x11, 0xFFFFFFFF, 0x0):
            with self.subTest(data=hex(data)):
                self.assert_refused(guard, self.write_req(data, cmd=0x15), Refusal.CHECK_FAILED)
        self.assertEqual(self.transport.calls, [(0, 0x14, 0, 0, 0)])
        self.assertIs(guard.execute(self.write_req(0x19, cmd=0x15), self.confirm).verified, True)

    def test_malformed_request_fields_are_check_failures(self) -> None:
        guard = self.guard_with_baseline()
        for data in (-1, 1 << 32, 1.5, True, None, "8"):
            with self.subTest(data=data):
                self.assert_refused(guard, self.write_req(data), Refusal.CHECK_FAILED)  # type: ignore[arg-type]
        for purpose in ("", "   ", None):
            with self.subTest(purpose=purpose):
                self.assert_refused(
                    guard, Request(0, 0x11, 0, 0, STOCK.to_u32(), purpose), Refusal.CHECK_FAILED  # type: ignore[arg-type]
                )

    def test_reads_carry_no_payload(self) -> None:
        guard = self.make_guard()
        self.assert_refused(guard, Request(0, 0x10, 0, 0, 1, "read with data"), Refusal.CHECK_FAILED)
        self.assert_refused(guard, Request(0, 0x10, 0, 0, 1 << 32, "read with huge data"), Refusal.CHECK_FAILED)
        # values that compare equal to 0 but are not ints the codec accepts (pack() would raise inside the transport)
        for data in (False, 0.0, None):
            with self.subTest(data=data):
                self.assert_refused(guard, Request(0, 0x10, 0, 0, data, "read with odd data"), Refusal.CHECK_FAILED)  # type: ignore[arg-type]

    def test_offset_min_comes_from_the_policy_entry(self) -> None:
        entries = tuple(
            dataclasses.replace(e, params={"offset_raw_min": -20}) if e.cmd == 0x11 else e for e in self.policy.allow
        )
        self.policy = dataclasses.replace(self.policy, allow=entries)
        guard = self.guard_with_baseline()
        self.assert_refused(guard, self.write_req(STOCK.with_(offset_raw=-21).to_u32()), Refusal.CHECK_FAILED)
        self.assertIs(guard.execute(self.write_req(STOCK.with_(offset_raw=-20).to_u32()), self.confirm).verified, True)


class SessionLimit(GuardCase):
    def test_second_operation_refused_with_default_limit(self) -> None:
        self.policy = dataclasses.replace(default_m1_policy(), min_dwell_s=0.0)
        self.assertEqual(self.policy.max_operations_per_session, 1)
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.assert_refused(guard, self.read_req(), Refusal.SESSION_LIMIT)
        self.assert_refused(guard, self.read_req(cpu=1), Refusal.SESSION_LIMIT)
        self.assertEqual(len(self.transport.calls), 1)

    def test_limit_counts_reads_and_writes_but_not_the_readback(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_session=4)
        guard = self.guard_with_baseline()  # operations 1-3 (0x10, 0x1C, 0x09)
        result = guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)  # operation 4 (+ read-back)
        self.assertIs(result.verified, True)
        self.assertEqual(len(self.transport.calls), 5)  # three reads, write, read-back
        self.assert_refused(guard, self.read_req(), Refusal.SESSION_LIMIT)

    def test_refusals_do_not_consume_the_limit(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_session=1)
        guard = self.make_guard()
        self.assert_refused(guard, self.read_req(cpu=99), Refusal.CPU_NOT_ALLOWED)
        self.assert_refused(guard, Request(0, 0x12, 0, 0, 0, "x"), Refusal.NOT_ALLOWLISTED)
        guard.execute(self.read_req(), self.confirm)  # still allowed: nothing above was executed


class Dwell(GuardCase):
    def test_refuses_until_the_clock_has_advanced_and_never_sleeps(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=60.0)
        guard = self.make_guard()
        with mock.patch("time.sleep", side_effect=AssertionError("the guard must not sleep")):
            guard.execute(self.read_req(), self.confirm)  # first operation: nothing to dwell after
            self.assert_refused(guard, self.read_req(), Refusal.DWELL)
            self.advance(59.5)
            self.assert_refused(guard, self.read_req(), Refusal.DWELL)
            self.advance(0.5)
            guard.execute(self.read_req(), self.confirm)  # exactly min_dwell_s later
            self.assert_refused(guard, self.read_req(), Refusal.DWELL)  # and the window restarts
        self.assertEqual(len(self.transport.calls), 2)

    def test_dwell_is_measured_from_the_end_of_the_previous_operation(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=60.0)
        guard = self.make_guard()
        self.transport.before_return = lambda call: self.advance(30.0)  # the operation itself takes 30 s
        guard.execute(self.read_req(), self.confirm)
        self.advance(59.0)
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.advance(1.0)
        guard.execute(self.read_req(), self.confirm)

    def test_a_refused_request_does_not_restart_the_window(self) -> None:
        self.policy = dataclasses.replace(self.policy, min_dwell_s=60.0)
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        self.advance(40.0)
        self.assert_refused(guard, self.read_req(), Refusal.DWELL)
        self.advance(20.0)
        guard.execute(self.read_req(), self.confirm)


class LockFile(GuardCase):
    def test_existing_lock_refuses_and_is_left_alone(self) -> None:
        self.lock_path.write_text("4242\n")
        guard = self.make_guard()
        exc = self.assert_refused(guard, self.read_req(), Refusal.LOCKED)
        self.assertIn("4242", exc.detail)
        self.assertEqual(self.lock_path.read_text(), "4242\n")  # a foreign lock is never removed
        self.assertFalse(guard.tripped)
        self.lock_path.unlink()
        guard.execute(self.read_req(), self.confirm)  # and works once the human cleared it

    def test_lock_exists_while_sending_holds_our_pid_and_is_removed_after(self) -> None:
        seen: list[tuple[bool, str]] = []
        self.transport.before_return = lambda call: seen.append(
            (self.lock_path.exists(), self.lock_path.read_text() if self.lock_path.exists() else "")
        )
        guard = self.guard_with_baseline()
        guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)
        self.assertTrue(all(present for present, _ in seen))
        self.assertEqual({text.strip() for _, text in seen}, {str(os.getpid())})
        self.assertEqual(len(seen), 5)  # three baseline reads, write and read-back: every transact ran under the lock
        self.assertFalse(self.lock_path.exists())

    def test_lock_removed_after_exception(self) -> None:
        self.transport.raise_on_call[1] = FakeMsrError("EIO")
        guard = self.make_guard()
        with self.assertRaises(FakeMsrError):
            guard.execute(self.read_req(), self.confirm)
        self.assertFalse(self.lock_path.exists())

    def test_lock_removed_after_health_gate_refusal(self) -> None:
        present: list[bool] = []

        def gate(phase: str) -> tuple[bool, str]:
            present.append(self.lock_path.exists())
            return False, "no"

        guard = self.make_guard(health_gate=gate)
        self.assert_refused(guard, self.read_req(), Refusal.HEALTH_GATE)
        self.assertEqual(present, [True])  # the lock was taken before the gate ran
        self.assertFalse(self.lock_path.exists())

    def test_lock_removed_after_readback_mismatch(self) -> None:
        self.transport.apply_write = lambda data: data ^ 1
        guard = self.guard_with_baseline()
        guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)
        self.assertFalse(self.lock_path.exists())

    def test_lock_that_cannot_be_created_refuses(self) -> None:
        guard = self.make_guard(lock_path=self.dir / "missing-dir" / "guard.lock")
        self.assert_refused(guard, self.read_req(), Refusal.LOCKED)

    def test_reentrant_call_is_refused_not_queued(self) -> None:
        inner: list[object] = []

        def reenter(call: tuple[int, int, int, int, int]) -> None:
            try:
                guard.execute(self.read_req(cpu=1), self.confirm)
            except GuardRefused as exc:
                inner.append(exc)

        guard = self.make_guard()
        self.transport.before_return = reenter
        guard.execute(self.read_req(), self.confirm)
        self.assertEqual(len(inner), 1)
        self.assertIs(inner[0].reason, Refusal.LOCKED)  # type: ignore[attr-defined]
        self.assertEqual(len(self.transport.calls), 1)  # the nested request never reached the transport


class HealthGate(GuardCase):
    def gate(self, phase: str) -> tuple[bool, str]:
        self.gate_calls.append(phase)
        return True, f"clean {phase}"

    def test_false_before_refuses_and_sends_nothing(self) -> None:
        guard = self.make_guard(health_gate=lambda phase: (False, "taint word changed"))
        exc = self.assert_refused(guard, self.read_req(), Refusal.HEALTH_GATE)
        self.assertIn("taint word changed", exc.detail)
        self.assertFalse(guard.tripped)

    def test_gate_that_raises_or_answers_oddly_refuses(self) -> None:
        def boom(phase: str) -> tuple[bool, str]:
            raise RuntimeError("snapshot failed")

        for label, gate in (("raises", boom), ("non-tuple", lambda p: True), ("non-bool", lambda p: ("yes", "x")), ("None", lambda p: None)):
            with self.subTest(label):
                guard = self.make_guard(health_gate=gate)  # type: ignore[arg-type]
                self.assert_refused(guard, self.read_req(), Refusal.HEALTH_GATE)

    def test_called_before_and_after_around_the_transaction(self) -> None:
        order: list[str] = []
        self.transport.before_return = lambda call: order.append(f"tx:{call[1]:#x}")

        def gate(phase: str) -> tuple[bool, str]:
            order.append(f"gate:{phase}")
            return True, "ok"

        guard = self.make_guard(health_gate=gate)
        self.seed_vf()
        self.seed_facts()
        guard.execute(self.read_req(), self.confirm)
        self.fact_reads(guard)
        del order[:]
        guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)
        self.assertEqual(order, ["gate:before", "tx:0x11", "tx:0x10", "gate:after"])

    def test_after_gate_failure_trips_but_still_returns_the_result(self) -> None:
        guard = self.make_guard(health_gate=lambda phase: (phase == "before", "pci device lost"))
        result = guard.execute(self.read_req(), self.confirm)
        self.assertEqual(result.response, Response(0, 0))
        self.assertTrue(guard.tripped)
        trip = [r for r in self.records() if r["event"] == "trip"]
        self.assertEqual(len(trip), 1)
        self.assertIn("pci device lost", trip[0]["detail"])
        self.assert_refused(guard, self.read_req(), Refusal.TRIPPED)

    def test_after_gate_that_raises_trips(self) -> None:
        def gate(phase: str) -> tuple[bool, str]:
            if phase == "after":
                raise RuntimeError("cannot snapshot")
            return True, "ok"

        guard = self.make_guard(health_gate=gate)
        guard.execute(self.read_req(), self.confirm)
        self.assertTrue(guard.tripped)

    def test_absent_gate_is_audited_as_none(self) -> None:
        guard = self.make_guard()
        guard.execute(self.read_req(), self.confirm)
        attempt = next(r for r in self.records() if r["event"] == "attempt")
        self.assertIn("health_gate", attempt)
        self.assertIsNone(attempt["health_gate"])

    def test_present_gate_detail_is_audited(self) -> None:
        guard = self.make_guard(health_gate=self.gate)
        guard.execute(self.read_req(), self.confirm)
        attempt = next(r for r in self.records() if r["event"] == "attempt")
        self.assertEqual(attempt["health_gate"], {"before": "clean before"})
        self.assertEqual(self.gate_calls, ["before", "after"])


class Trip(GuardCase):
    def test_transport_exceptions_are_audited_reraised_and_trip(self) -> None:
        for exc_type in (FakeMailboxTimeout, FakeMsrError):
            with self.subTest(exc_type.__name__):
                self.transport = RecordingTransport()
                self.audit_path = self.dir / f"audit-{exc_type.__name__}.jsonl"
                self.transport.raise_on_call[1] = exc_type("device went away")
                guard = self.make_guard(transport=self.transport)
                with self.assertRaises(exc_type):
                    guard.execute(self.read_req(), self.confirm)
                self.assertTrue(guard.tripped)
                events = [(r["event"], r.get("stage")) for r in self.records()]
                self.assertEqual(events, [("attempt", None), ("error", "transact"), ("trip", None)])
                self.assertIn("device went away", self.records()[1]["detail"])
                self.assertFalse(self.lock_path.exists())
                # every later call, read or write, is refused and sends nothing
                self.assert_refused(guard, self.read_req(), Refusal.TRIPPED)
                self.assert_refused(guard, self.read_req(cpu=1), Refusal.TRIPPED)
                self.assertEqual(len(self.transport.calls), 1)

    def test_baseline_is_not_usable_after_a_trip(self) -> None:
        guard = self.guard_with_baseline()
        self.transport.raise_on_call[4] = FakeMsrError("EIO")  # the write is the 4th transaction
        with self.assertRaises(FakeMsrError):
            guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)
        self.assert_refused(guard, self.write_req(STOCK.with_(offset_raw=-5).to_u32()), Refusal.TRIPPED)

    def test_any_exception_trips_not_just_the_named_ones(self) -> None:
        self.transport.raise_on_call[1] = ValueError("pack() rejected a field")
        guard = self.make_guard()
        with self.assertRaises(ValueError):
            guard.execute(self.read_req(), self.confirm)
        self.assertTrue(guard.tripped)

    def test_keyboard_interrupt_trips_and_releases_the_lock(self) -> None:
        self.transport.raise_on_call[1] = KeyboardInterrupt()
        guard = self.make_guard()
        with self.assertRaises(KeyboardInterrupt):
            guard.execute(self.read_req(), self.confirm)
        self.assertTrue(guard.tripped)
        self.assertFalse(self.lock_path.exists())

    def test_malformed_responses_trip(self) -> None:
        for label, kwargs in (
            ("rc out of range", {"rc_on_call": {1: 300}}),
            ("data out of u32", {"data_on_call": {1: 1 << 40}}),
            ("negative data", {"data_on_call": {1: -1}}),
            ("non-int data", {"data_on_call": {1: "0x0"}}),
        ):
            with self.subTest(label):
                self.transport = RecordingTransport()
                for name, value in kwargs.items():
                    setattr(self.transport, name, value)
                guard = self.make_guard(transport=self.transport)
                with self.assertRaises((TypeError, ValueError)):
                    guard.execute(self.read_req(), self.confirm)
                self.assertTrue(guard.tripped)
                self.assert_refused(guard, self.read_req(), Refusal.TRIPPED)

    def test_transport_returning_a_non_response_trips(self) -> None:
        class Liar:
            calls: list = []

            def transact(self, cpu, cmd, p1, p2, data=0):  # noqa: ANN001
                self.calls.append((cpu, cmd, p1, p2, data))
                return (0, 0)

        guard = self.make_guard(transport=Liar())
        with self.assertRaises(TypeError):
            guard.execute(self.read_req(), self.confirm)
        self.assertTrue(guard.tripped)

    def test_nonzero_rc_is_returned_not_raised_and_does_not_trip(self) -> None:
        guard = self.guard_with_baseline()
        self.transport.write_rc = 0x03  # RatioExceedsLimits (registers.md "Return codes")
        result = guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)
        self.assertEqual(result.response, Response(3, 0))
        self.assertIsNone(result.readback)
        self.assertIsNone(result.verified)  # nothing to verify: pcode refused the write
        self.assertFalse(guard.tripped)
        self.assertEqual(len(self.transport.calls), 4)  # three baseline reads + write, no read-back
        rec = [r for r in self.records() if r["event"] == "result"][-1]
        self.assertEqual((rec["rc"], rec["rc_name"]), (3, "RatioExceedsLimits"))
        self.assertEqual(guard.revert_plan(), [])  # an unapplied write has nothing to revert
        guard.execute(self.read_req(), self.confirm)  # and the session carries on

    def test_nonzero_rc_on_a_read_returns_and_stores_no_baseline(self) -> None:
        self.transport.read_rc = 0x05  # OCNotSupported
        guard = self.make_guard()
        result = guard.execute(self.read_req(), self.confirm)
        self.assertEqual(result.response.rc, 5)
        self.assertFalse(guard.tripped)
        self.assert_refused(guard, self.write_req(STOCK.to_u32()), Refusal.BASELINE_MISSING)


class Readback(GuardCase):
    def test_success_is_verified_true(self) -> None:
        guard = self.guard_with_baseline()
        new = STOCK.with_(offset_raw=-40).to_u32()
        result = guard.execute(self.write_req(new), self.confirm)
        self.assertIs(result.verified, True)
        self.assertEqual(result.readback, Response(0, new))
        self.assertEqual(result.request, self.write_req(new))
        self.assertFalse(guard.tripped)
        self.assertEqual(self.transport.calls[-1], (0, 0x10, 0, 0, 0))  # read-back = the twin read, through the same transport
        events = [r["event"] for r in self.records()]
        self.assertEqual(events[-3:], ["attempt", "result", "readback"])
        self.assertIs(self.records()[-1]["verified"], True)

    def test_mismatch_is_verified_false_and_trips(self) -> None:
        self.transport.apply_write = lambda data: data ^ 0x00200000  # hardware stored a different offset
        guard = self.guard_with_baseline()
        new = STOCK.with_(offset_raw=-40).to_u32()
        result = guard.execute(self.write_req(new), self.confirm)
        self.assertIs(result.verified, False)
        self.assertEqual(result.readback, Response(0, new ^ 0x00200000))
        self.assertEqual(result.response.rc, 0)
        self.assertTrue(guard.tripped)
        tail = self.records()[-2:]
        self.assertEqual([r["event"] for r in tail], ["readback", "trip"])
        self.assertIs(tail[0]["verified"], False)
        self.assertEqual(tail[0]["expected"], f"0x{new:08X}")
        self.assert_refused(guard, self.read_req(), Refusal.TRIPPED)
        self.assertEqual(len(guard.revert_plan()), 1)  # the write happened; it stays revertible

    def test_readback_with_error_rc_counts_as_not_verified(self) -> None:
        guard = self.guard_with_baseline()
        self.transport.rc_on_call[5] = 0x13  # the read-back itself fails (UndervoltProtection)
        result = guard.execute(self.write_req(STOCK.with_(offset_raw=-40).to_u32()), self.confirm)
        self.assertIs(result.verified, False)
        self.assertTrue(guard.tripped)

    def test_exception_during_readback_is_audited_reraised_and_trips(self) -> None:
        guard = self.guard_with_baseline()
        self.transport.raise_on_call[5] = FakeMailboxTimeout("busy stuck")
        with self.assertRaises(FakeMailboxTimeout):
            guard.execute(self.write_req(STOCK.with_(offset_raw=-40).to_u32()), self.confirm)
        self.assertTrue(guard.tripped)
        error = [r for r in self.records() if r["event"] == "error"][-1]
        self.assertEqual(error["stage"], "readback")
        self.assertEqual(len(guard.revert_plan()), 1)
        self.assertFalse(self.lock_path.exists())

    def test_reads_have_no_readback(self) -> None:
        guard = self.make_guard()
        result = guard.execute(self.read_req(), self.confirm)
        self.assertIsNone(result.readback)
        self.assertIsNone(result.verified)
        self.assertEqual(len(self.transport.calls), 1)

    def test_baseline_is_the_operator_read_not_the_readback(self) -> None:
        guard = self.guard_with_baseline()
        guard.execute(self.write_req(STOCK.with_(offset_raw=-40).to_u32()), self.confirm)
        guard.execute(self.write_req(STOCK.with_(offset_raw=-45).to_u32()), self.confirm)
        # D-016: the writes are undone newest first through the state each one left (the second is undone to the first's word, the first
        # to the word the OPERATOR read), never with the word a read-back returned
        self.assertEqual([r.data for r in guard.revert_plan()], [STOCK.with_(offset_raw=-40).to_u32(), STOCK_REVERT])


class RevertPlan(GuardCase):
    def test_inverse_words_newest_cpu_first_and_not_executed(self) -> None:
        other = STOCK.with_(ratio=54, v_target_raw=0x490, offset_raw=-2).to_u32()
        self.seed_vf(cpu=0, word=STOCK_WORD)
        self.seed_vf(cpu=3, word=other)
        self.seed_facts(cpu=0)
        self.seed_facts(cpu=3, favored=54 | 54 << 8, fused=54 | 0x490 << 8)
        self.transport.state[(0, 0x14, 0, 0)] = 0x1
        guard = self.make_guard()
        for cpu in (0, 3):
            guard.execute(self.read_req(cpu=cpu), self.confirm)
            self.fact_reads(guard, cpu)
        guard.execute(self.read_req(cmd=0x14), self.confirm)
        guard.execute(self.write_req(0x9, cmd=0x15), self.confirm)
        guard.execute(self.write_req(STOCK.with_(offset_raw=-40).to_u32(), cpu=0), self.confirm)
        guard.execute(self.write_req(STOCK.with_(ratio=54, v_target_raw=0x490, offset_raw=-50).to_u32(), cpu=3), self.confirm)
        sent = list(self.transport.calls)
        plan = guard.revert_plan()
        self.assertEqual(
            plan,
            [
                Request(3, 0x11, 0, 0, STOCK.with_(ratio=54, v_target_raw=0, offset_raw=-2).to_u32(), "revert"),
                Request(0, 0x11, 0, 0, STOCK_REVERT, "revert"),
                Request(0, 0x15, 0, 0, 0x1, "revert"),
            ],
        )
        self.assertEqual(self.transport.calls, sent, "revert_plan() must not execute anything")
        self.assertEqual(guard.revert_plan(), plan)  # pure

    def test_empty_until_a_write_succeeds(self) -> None:
        guard = self.guard_with_baseline()
        self.assertEqual(guard.revert_plan(), [])
        self.assert_refused(guard, self.write_req(STOCK.with_(offset_raw=5).to_u32()), Refusal.CHECK_FAILED)
        self.assertEqual(guard.revert_plan(), [])

    def test_write_with_unknown_outcome_stays_in_the_plan(self) -> None:
        guard = self.guard_with_baseline()
        self.transport.raise_on_call[4] = FakeMailboxTimeout("no completion")  # may or may not have been applied
        with self.assertRaises(FakeMailboxTimeout):
            guard.execute(self.write_req(STOCK.with_(offset_raw=-5).to_u32()), self.confirm)
        self.assertEqual(guard.revert_plan(), [Request(0, 0x11, 0, 0, STOCK_REVERT, "revert")])

    def test_plan_requests_go_through_the_same_checks(self) -> None:
        self.policy = dataclasses.replace(self.policy, max_operations_per_session=5)
        guard = self.guard_with_baseline()
        guard.execute(self.write_req(STOCK.with_(offset_raw=-40).to_u32()), self.confirm)
        (revert,) = guard.revert_plan()
        result = guard.execute(revert, self.confirm)  # fifth operation: allowed, baseline word satisfies vf_undervolt
        self.assertIs(result.verified, True)
        self.assertEqual(self.transport.state[(0, 0x10, 0, 0)], STOCK_REVERT)
        self.assert_refused(guard, revert, Refusal.SESSION_LIMIT)


class AuditTrail(GuardCase):
    def test_full_session_records_in_order(self) -> None:
        guard = self.guard_with_baseline()
        new = STOCK.with_(offset_raw=-40).to_u32()
        guard.execute(self.write_req(new), self.confirm)
        self.wall.advance(2.5)
        self.assert_refused(guard, self.write_req(STOCK.with_(offset_raw=-300).to_u32()), Refusal.CHECK_FAILED)
        records = self.records()
        self.assertEqual(
            [(r["event"], r["seq"]) for r in records],
            [("attempt", 0), ("result", 1), ("attempt", 2), ("result", 3), ("attempt", 4), ("result", 5),  # the three baseline reads
             ("attempt", 6), ("result", 7), ("readback", 8), ("refused", 9)],
        )
        attempt_write = records[6]
        self.assertEqual(attempt_write["cmd"], "0x11")
        self.assertEqual(attempt_write["cpu"], 0)
        self.assertEqual(attempt_write["req_data"], f"0x{new:08X}")
        self.assertEqual(attempt_write["baseline"], f"0x{STOCK_WORD:08X}")
        self.assertEqual(attempt_write["purpose"], "test write")
        self.assertEqual(attempt_write["operator"], self.OPERATOR)
        self.assertEqual(attempt_write["ts"], "2023-11-14T22:13:20.000+00:00")
        self.assertEqual((records[7]["rc"], records[7]["rc_name"], records[7]["data"]), (0, "Pass", "0x00000000"))
        self.assertEqual(records[8]["cmd"], "0x10")  # the read-back is logged as the read it is
        self.assertEqual(records[8]["data"], f"0x{new:08X}")
        self.assertEqual(records[9]["ts"], "2023-11-14T22:13:22.500+00:00")
        self.assertEqual(records[9]["reason"], "CHECK_FAILED")
        self.assertIn("offset_raw -300", records[9]["detail"])

    def test_every_refusal_is_audited_with_its_request(self) -> None:
        guard = self.make_guard()
        self.assert_refused(guard, self.read_req(cpu=77), Refusal.CPU_NOT_ALLOWED)
        rec = self.records()[-1]
        self.assertEqual((rec["cpu"], rec["cmd"], rec["operator"]), (77, "0x10", self.OPERATOR))

    def test_two_guards_append_to_one_file_and_never_truncate(self) -> None:
        self.audit_path.write_text('{"event":"trip","note":"from an earlier session"}\n')
        first = self.make_guard()
        first.execute(self.read_req(), self.confirm)
        second = self.make_guard(lock_path=self.dir / "second.lock")  # new AuditLog on the same path
        self.assert_refused(second, self.read_req(cpu=50), Refusal.CPU_NOT_ALLOWED)
        second.execute(self.read_req(cpu=1), self.confirm)
        records = self.records()
        self.assertEqual(records[0]["note"], "from an earlier session")
        self.assertEqual([r["event"] for r in records[1:]], ["attempt", "result", "refused", "attempt", "result"])
        self.assertEqual([r["seq"] for r in records[1:]], [1, 2, 3, 4, 5])

    def test_unwritable_audit_log_blocks_sending(self) -> None:
        class Broken(AuditLog):
            def append(self, *a, **k):  # noqa: ANN002, ANN003
                raise OSError("disk full")

        guard = self.make_guard(audit=Broken(self.audit_path, fsync=False))
        with self.assertRaises(OSError):
            guard.execute(self.read_req(), self.confirm)  # the attempt could not be recorded, so nothing was sent
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.lock_path.exists())
        with self.assertRaises(GuardRefused) as cm:  # a refusal is still a refusal when its record cannot be written
            guard.execute(self.read_req(cpu=99), self.confirm)
        self.assertIs(cm.exception.reason, Refusal.CPU_NOT_ALLOWED)
        self.assertIn("audit write failed", cm.exception.detail)
        self.assertEqual(self.transport.calls, [])

    def test_audit_failure_while_failing_does_not_mask_the_transport_error(self) -> None:
        calls = {"n": 0}

        class Flaky(AuditLog):
            def append(self, event, **k):  # noqa: ANN001, ANN003
                calls["n"] += 1
                if event != "attempt":
                    raise OSError("disk full")
                return super().append(event, **k)

        self.transport.raise_on_call[1] = FakeMsrError("EIO")
        guard = self.make_guard(audit=Flaky(self.audit_path, fsync=False))
        with self.assertRaises(FakeMsrError):
            guard.execute(self.read_req(), self.confirm)
        self.assertTrue(guard.tripped)


class Hygiene(unittest.TestCase):
    """The guard is stdlib-only, never sleeps, and does not reach for the msr package, processes, network or eval."""

    FORBIDDEN_IMPORTS = ("arltuner.msr", "subprocess", "socket", "ctypes", "fcntl", "urllib", "http", "asyncio", "sched")

    def parsed(self) -> list[tuple[str, ast.AST]]:
        return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(SRC.glob("*.py"))]

    def test_exactly_the_owned_files(self) -> None:
        self.assertEqual({n for n, _ in self.parsed()}, {"__init__.py", "guard.py", "policy.py", "audit.py"})

    def test_no_forbidden_imports(self) -> None:
        for name, tree in self.parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
                else:
                    continue  # relative imports stay inside arltuner.guard
                for module in modules:
                    for bad in self.FORBIDDEN_IMPORTS:
                        self.assertFalse(module == bad or module.startswith(bad + "."), f"{name} imports {module}")

    def test_never_sleeps_and_never_evals(self) -> None:
        for name, tree in self.parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {"sleep", "system", "popen"}:
                    self.fail(f"{name}:{node.lineno} uses .{node.attr}")
                if isinstance(node, ast.Name) and node.id in {"sleep", "eval", "exec", "compile", "__import__"}:
                    self.fail(f"{name}:{node.lineno} uses {node.id}")
                if isinstance(node, ast.ImportFrom) and any(a.name == "sleep" for a in node.names):
                    self.fail(f"{name}:{node.lineno} imports sleep")


if __name__ == "__main__":
    unittest.main()
