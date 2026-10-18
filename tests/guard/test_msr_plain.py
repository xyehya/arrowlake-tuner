"""T-023 plain-MSR path of the guard: policy entries, ``turbo_ratio_bounded``, the chain of refusals, audit, caps, read-back, revert.

No hardware: the port is ``SimMsrPort`` (plain registers ``0x1AD/0x1AE/0x650/0x651/0xCE/0x194``, ``[MEASURED lnx 2026-10-01]`` values) and
the mailbox transport is a recording fake that must never see a plain request. Every refusal test checks that the port saw nothing new.
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from arltuner.guard import (
    ENV_FLAG,
    MSR_CHECKS,
    AllowEntry,
    AuditLog,
    Confirmation,
    GuardRefused,
    MsrAllowEntry,
    MsrRequest,
    MsrResult,
    Policy,
    PolicyError,
    Refusal,
    Request,
    WriteGuard,
    default_m1_policy,
    load_policy,
    read_all,
    read_state,
)
from arltuner.guard.policy import MsrCheckContext, build_msr_context, turbo_bytes
from arltuner.msr import MsrError, SimConfig, SimMsrPort

from .fakes import FakeClock, RecordingTransport

E_TABLE = 0x3030303030303030  # 0x650 [MEASURED lnx 2026-10-01]: 48 x8
P_TABLE = 0x3737373737373939  # 0x1AD: 57,57,55 x6
PLATFORM = 0x08040838F1812500  # 0xCE: [15:8] = 37
TOKEN = "tok-msr"


def table(*buckets: int) -> int:
    return sum(b << (8 * k) for k, b in enumerate(buckets))


def e_table(*buckets: int) -> int:
    return table(*buckets)


def policy_plain(*, write_entries: bool = True, **changes: object) -> Policy:
    """The default policy (plain reads only) plus, optionally, write entries for both tables; high caps, no dwell."""
    # hw_ratio_max: the CPU's own limit (cmd 0x01, D-039) as the reference machine reports it (0x778 = 120x); these tests are about the table checks
    fields: dict[str, object] = dict(max_operations_per_session=20, max_operations_per_boot=50, min_dwell_s=0.0, max_ratio_step=8, hw_ratio_max={"P": 120, "E": 120})
    fields.update(changes)
    base = dataclasses.replace(default_m1_policy(), **fields)
    if not write_entries:
        return base
    extra = (MsrAllowEntry("e_write", 0x650, "write", None, "turbo_ratio_bounded"), MsrAllowEntry("p_write", 0x1AD, "write", None, "turbo_ratio_bounded"))
    return dataclasses.replace(base, msr_allow=base.msr_allow + extra)


class PlainCase(unittest.TestCase):
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
        self.confirm = Confirmation(TOKEN, "yk")
        self.sim = SimMsrPort(SimConfig())

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.wall.advance(seconds)

    def guard(self, policy: Policy | None = None, **overrides: object) -> WriteGuard:
        kwargs: dict[str, object] = dict(
            session_token=TOKEN,
            env=self.env,
            lock_path=self.lock_path,
            clock=self.clock,
            wall_clock=self.wall,
            boot_id_fn=lambda: "test-boot",
            msr_port=self.sim,
        )
        kwargs.update(overrides)
        return WriteGuard(self.transport, policy or policy_plain(), AuditLog(self.audit_path, fsync=False), **kwargs)  # type: ignore[arg-type]

    def records(self) -> list[dict]:
        return read_all(self.audit_path)

    def refused(self, guard: WriteGuard, req: MsrRequest, reason: Refusal, confirm: object = None) -> GuardRefused:
        reads, writes = list(self.sim.reads), list(self.sim.writes)
        with self.assertRaises(GuardRefused) as cm:
            guard.execute(req, confirm or self.confirm)  # type: ignore[arg-type]
        self.assertIs(cm.exception.reason, reason, cm.exception.detail)
        self.assertEqual((self.sim.reads, self.sim.writes), (reads, writes), "a refusal must not touch the port")
        self.assertEqual(self.transport.calls, [], "a plain request must never reach the mailbox transport")
        self.assertEqual(self.records()[-1]["event"], "refused")
        self.assertEqual(self.records()[-1]["reason"], reason.value)
        return cm.exception

    # request builders
    @staticmethod
    def rd(msr: int = 0x650, cpu: int = 0) -> MsrRequest:
        return MsrRequest(cpu, msr, 0, "test read", "read")

    @staticmethod
    def wr(value: int, msr: int = 0x650, cpu: int = 0, verify: int | None = 8) -> MsrRequest:
        return MsrRequest(cpu, msr, value, "test write", "write", verify)

    def baseline(self, guard: WriteGuard, msr: int = 0x650, cpu: int = 0) -> int:
        res = guard.execute(self.rd(msr, cpu), self.confirm)
        assert isinstance(res, MsrResult)
        return res.value


class DefaultPolicyIsInert(unittest.TestCase):
    def test_default_allows_only_plain_reads_of_the_ten_msrs(self) -> None:
        p = default_m1_policy()
        self.assertEqual({e.msr for e in p.msr_allow}, {0x1AD, 0x1AE, 0x650, 0x651, 0xCE, 0x194, 0x198, 0x606, 0x610, 0x614})  # G-214: the RAPL units / limit / info registers, read-only
        self.assertTrue(all(e.kind == "read" and e.check is None for e in p.msr_allow))
        for msr in (0x1AD, 0x650, 0x1AE, 0x651, 0xCE, 0x194):
            self.assertIsNone(p.match_msr(msr, "write"))
            self.assertIsNotNone(p.match_msr(msr, "read"))
        self.assertIsNone(p.match_msr(0x150, "read"))  # an unknown MSR (the mailbox itself) is denied
        self.assertIsNone(p.match_msr(0x1A0, "read"))

    def test_a_policy_without_msr_allow_denies_every_plain_access(self) -> None:
        p = Policy(cpus=frozenset({0}))
        self.assertEqual(p.msr_allow, ())
        self.assertIsNone(p.match_msr(0x650, "read"))

    def test_the_example_policy_stays_inert_for_plain_writes(self) -> None:
        p = load_policy(Path(__file__).resolve().parents[2] / "examples" / "reference-285k" / "policies" / "example-oc-ramp.toml")
        self.assertFalse([e for e in p.msr_allow if e.kind == "write"])
        self.assertEqual(p.ratio_max["E"], 49)  # D-017

    def test_the_registry_names_the_check_and_the_mailbox_checks_do_not_accept_it(self) -> None:
        self.assertIn("turbo_ratio_bounded", MSR_CHECKS)
        with self.assertRaises(PolicyError):
            AllowEntry("x", 0x11, frozenset({0}), frozenset({0}), "write", "turbo_ratio_bounded")


class PolicyLoading(unittest.TestCase):
    BASE = 'cpus = ["0-23"]\n'

    def load(self, body: str) -> Policy:
        return load_policy(self.BASE + body)

    def test_a_read_and_a_write_entry_load(self) -> None:
        p = self.load(
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "read"\n'
            '[[msr_allow]]\nname = "w"\nmsr = 0x650\nkind = "write"\ncpus = [0]\ncheck = "turbo_ratio_bounded"\n'
        )
        self.assertEqual([(e.msr, e.kind) for e in p.msr_allow], [(0x650, "read"), (0x650, "write")])
        self.assertEqual(p.msr_allow[1].cpus, frozenset({0}))
        self.assertIs(p.match_msr(0x650, "write"), p.msr_allow[1])

    def test_bucket_count_msrs_can_never_be_written(self) -> None:
        for msr in (0x651, 0x1AE):
            with self.assertRaisesRegex(PolicyError, "bucket core counts"):
                self.load(f'[[msr_allow]]\nname = "w"\nmsr = {msr:#x}\nkind = "write"\ncheck = "turbo_ratio_bounded"\n')

    def test_other_msrs_cannot_be_written_or_read_through_the_guard(self) -> None:
        for msr in (0xCE, 0x194, 0x150, 0x1A0):
            with self.assertRaises(PolicyError):
                self.load(f'[[msr_allow]]\nname = "w"\nmsr = {msr:#x}\nkind = "write"\ncheck = "turbo_ratio_bounded"\n')
        for msr in (0x150, 0x1A0, 0x10):
            with self.assertRaises(PolicyError):
                self.load(f'[[msr_allow]]\nname = "r"\nmsr = {msr:#x}\nkind = "read"\n')

    def test_a_write_needs_the_check_and_a_read_must_not_have_one(self) -> None:
        with self.assertRaises(PolicyError):
            self.load('[[msr_allow]]\nname = "w"\nmsr = 0x650\nkind = "write"\n')
        with self.assertRaises(PolicyError):
            self.load('[[msr_allow]]\nname = "w"\nmsr = 0x650\nkind = "write"\ncheck = "vf_bounded"\n')
        with self.assertRaises(PolicyError):
            self.load('[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "read"\ncheck = "turbo_ratio_bounded"\n')

    def test_malformed_entries_are_load_errors(self) -> None:
        for body in (
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\n',  # missing kind
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "read"\nbogus = 1\n',
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "rw"\n',
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "read"\ncpus = [99]\n',  # outside the policy cpus
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "read"\n[[msr_allow]]\nname = "r2"\nmsr = 0x650\nkind = "read"\n',  # overlap
            '[[msr_allow]]\nname = "r"\nmsr = 0x650\nkind = "read"\n[[msr_allow]]\nname = "r"\nmsr = 0xCE\nkind = "read"\n',  # duplicate name
            '[[msr_allow]]\nname = "w"\nmsr = 0x650\nkind = "write"\ncheck = "turbo_ratio_bounded"\nparams = { x = 1 }\n',
        ):
            with self.assertRaises(PolicyError, msg=body):
                self.load(body)


class TurboCheck(unittest.TestCase):
    """``turbo_ratio_bounded`` on its own (policy.py): every clause, each with its own refusal."""

    CHECK = staticmethod(MSR_CHECKS["turbo_ratio_bounded"])

    def run_check(
        self, new: int, old: int = E_TABLE, msr: int = 0x650, ratio_max: int | None = None, step: int = 1, base: int | None = 37
    ) -> tuple[bool, str]:
        ctx = MsrCheckContext(kind={0x650: "E", 0x1AD: "P"}.get(msr), ratio_max=ratio_max, max_ratio_step=step, base_ratio=base)
        return self.CHECK(MsrRequest(0, msr, new, "t", "write", 8), old, {}, ctx)

    def test_no_change_and_a_lowering_pass_by_default(self) -> None:
        self.assertEqual(self.run_check(E_TABLE), (True, "ok"))
        self.assertTrue(self.run_check(table(*([47] * 8)))[0])

    def test_no_ceiling_means_no_raise_per_byte(self) -> None:
        ok, why = self.run_check(table(49, *([48] * 7)))
        self.assertFalse(ok)
        self.assertIn("bucket 0", why)
        self.assertIn("no-raise", why)
        # per byte, not against the maximum: a table whose bucket 7 was 46 may not climb back to 47 even though 47 < its own maximum
        old = table(48, 48, 48, 48, 48, 48, 48, 46)
        self.assertFalse(self.run_check(table(48, 48, 48, 48, 48, 48, 48, 47), old)[0])

    def test_a_ceiling_allows_up_to_itself_and_no_more(self) -> None:
        self.assertTrue(self.run_check(table(*([49] * 8)), ratio_max=49)[0])
        ok, why = self.run_check(table(50, *([49] * 7)), table(*([49] * 8)), ratio_max=49)
        self.assertFalse(ok)
        self.assertIn("ratio_max", why)

    def test_the_p_table_uses_the_p_kind_whatever_the_cpu(self) -> None:
        hw = {"P": 120, "E": 120}
        ctx = build_msr_context(
            dataclasses.replace(default_m1_policy(), ratio_max={"P": 58, "E": 49}, hw_ratio_max=hw), 0x1AD, platform_info=PLATFORM
        )
        self.assertEqual((ctx.kind, ctx.ratio_max, ctx.base_ratio), ("P", 58, 37))
        ctx = build_msr_context(dataclasses.replace(default_m1_policy(), ratio_max={"P": 58, "E": 49}, hw_ratio_max=hw), 0x650, platform_info=None)
        self.assertEqual((ctx.kind, ctx.ratio_max, ctx.base_ratio), ("E", 49, None))

    def test_every_byte_must_be_non_zero(self) -> None:
        ok, why = self.run_check(table(48, 48, 48, 48, 48, 48, 48, 0), step=255, ratio_max=60)
        self.assertFalse(ok)
        self.assertIn("is 0", why)

    def test_bytes_must_not_increase_with_more_cores(self) -> None:
        ok, why = self.run_check(table(48, 48, 48, 48, 48, 48, 47, 48), ratio_max=60, step=8)
        self.assertFalse(ok)
        self.assertIn("must not increase", why)

    def test_the_step_limit_applies_per_byte_in_both_directions(self) -> None:
        ok, why = self.run_check(table(*([50] * 8)), ratio_max=60, step=1)
        self.assertFalse(ok)
        self.assertIn("exceeds the per-operation limit", why)
        self.assertFalse(self.run_check(table(*([46] * 8)), ratio_max=60, step=1)[0])
        self.assertTrue(self.run_check(table(*([46] * 8)), ratio_max=60, step=2)[0])

    def test_the_floor_is_the_base_ratio_if_0xce_was_read(self) -> None:
        old = table(*([38] * 8))
        self.assertTrue(self.run_check(table(*([37] * 8)), old, base=37)[0])
        ok, why = self.run_check(table(*([36] * 8)), old, base=37, step=8)
        self.assertFalse(ok)
        self.assertIn("0xCE", why)

    def test_without_0xce_the_floor_is_the_baseline_minimum_minus_the_step(self) -> None:
        self.assertTrue(self.run_check(table(*([47] * 8)), E_TABLE, base=None, step=1)[0])
        old = table(48, 48, 48, 48, 48, 48, 48, 40)  # minimum 40
        self.assertTrue(self.run_check(table(48, 48, 48, 48, 48, 48, 48, 39), old, base=None, step=1)[0])
        ok, why = self.run_check(table(48, 48, 48, 48, 48, 48, 48, 32), old, base=None, step=8)
        self.assertTrue(ok, why)  # 32 >= 40 - 8, the lowest value the floor allows
        self.assertFalse(self.run_check(table(48, 48, 48, 48, 48, 48, 48, 31), old, base=None, step=8)[0])

    def test_an_unknown_msr_and_a_missing_context_are_refused(self) -> None:
        self.assertFalse(self.run_check(E_TABLE, msr=0x651)[0])
        self.assertFalse(self.CHECK(MsrRequest(0, 0x650, E_TABLE, "t", "write", 8), E_TABLE, {}, None)[0])
        self.assertFalse(self.CHECK(MsrRequest(0, 0x650, 1 << 70, "t", "write", 8), E_TABLE, {}, MsrCheckContext(kind="E"))[0])

    def test_turbo_bytes_is_lsb_first(self) -> None:
        self.assertEqual(turbo_bytes(P_TABLE), (57, 57, 55, 55, 55, 55, 55, 55))


class ChainOfRefusals(PlainCase):
    def test_a_guard_without_a_port_refuses_everything(self) -> None:
        g = self.guard(msr_port=None)
        self.refused(g, self.rd(), Refusal.NOT_ALLOWLISTED)

    def test_tripped_env_flag_confirmation_come_first(self) -> None:
        g = self.guard()
        self.env[ENV_FLAG] = "0"
        self.refused(g, self.rd(), Refusal.NO_ENV_FLAG)
        self.env[ENV_FLAG] = "1"
        self.refused(g, self.rd(), Refusal.BAD_CONFIRMATION, Confirmation("wrong", "yk"))
        self.refused(g, self.rd(), Refusal.BAD_CONFIRMATION, Confirmation(TOKEN, "  "))

    def test_cpu_not_in_the_policy_set(self) -> None:
        g = self.guard(policy_plain(cpus=frozenset({0, 8})))
        self.refused(g, self.rd(cpu=1), Refusal.CPU_NOT_ALLOWED)
        self.refused(g, self.rd(cpu=True), Refusal.CPU_NOT_ALLOWED)  # type: ignore[arg-type]

    def test_cpu_not_covered_by_the_entry(self) -> None:
        p = dataclasses.replace(
            policy_plain(write_entries=False), msr_allow=(MsrAllowEntry("r", 0x650, "read", frozenset({0})),)
        )
        g = self.guard(p)
        self.baseline(g, 0x650, 0)
        self.refused(g, self.rd(0x650, 1), Refusal.CPU_NOT_ALLOWED)

    def test_unknown_msr_kind_and_garbage_are_not_allowlisted(self) -> None:
        g = self.guard()
        self.refused(g, self.rd(0x150), Refusal.NOT_ALLOWLISTED)  # the mailbox MSR is not a plain MSR
        self.refused(g, self.rd(0x1A0), Refusal.NOT_ALLOWLISTED)
        self.refused(g, MsrRequest(0, 0x650, 0, "t", "erase"), Refusal.NOT_ALLOWLISTED)
        self.refused(g, MsrRequest(0, "0x650", 0, "t", "read"), Refusal.NOT_ALLOWLISTED)  # type: ignore[arg-type]
        self.refused(g, MsrRequest(0, 0x651, E_TABLE, "t", "write", 8), Refusal.NOT_ALLOWLISTED)  # the default policy has no write entry

    def test_default_policy_refuses_every_plain_write(self) -> None:
        g = self.guard(policy_plain(write_entries=False))
        self.baseline(g)
        self.refused(g, self.wr(E_TABLE), Refusal.NOT_ALLOWLISTED)
        self.refused(g, self.wr(P_TABLE, 0x1AD), Refusal.NOT_ALLOWLISTED)

    def test_bucket_count_msrs_are_never_writable_even_with_other_entries(self) -> None:
        g = self.guard()
        for msr in (0x651, 0x1AE, 0xCE, 0x194):
            self.baseline(g, msr)
            self.refused(g, self.wr(0x0102030405060708, msr), Refusal.NOT_ALLOWLISTED)

    def test_a_write_needs_a_baseline_read_on_the_same_cpu_and_msr(self) -> None:
        g = self.guard()
        self.refused(g, self.wr(E_TABLE), Refusal.BASELINE_MISSING)
        self.baseline(g, 0x650, 1)  # another CPU does not count
        self.refused(g, self.wr(E_TABLE), Refusal.BASELINE_MISSING)
        self.baseline(g, 0x1AD, 0)  # another MSR does not count
        self.refused(g, self.wr(E_TABLE), Refusal.BASELINE_MISSING)

    def test_a_read_must_carry_no_payload_and_a_purpose(self) -> None:
        g = self.guard()
        self.refused(g, MsrRequest(0, 0x650, 1, "t", "read"), Refusal.CHECK_FAILED)
        self.refused(g, MsrRequest(0, 0x650, 0, "  ", "read"), Refusal.CHECK_FAILED)
        self.refused(g, MsrRequest(0, 0x650, 0, "t", "read", 8), Refusal.CHECK_FAILED)

    def test_a_write_needs_a_proper_verify_cpu(self) -> None:
        g = self.guard()
        self.baseline(g)
        self.refused(g, self.wr(E_TABLE, verify=None), Refusal.CHECK_FAILED)
        self.refused(g, self.wr(E_TABLE, verify=0), Refusal.CHECK_FAILED)  # the written cpu itself
        self.refused(g, self.wr(E_TABLE, verify=2), Refusal.CHECK_FAILED)  # the same core kind (P) as cpu 0: proves nothing about scope
        self.refused(g, self.wr(E_TABLE, verify=99), Refusal.CPU_NOT_ALLOWED)
        self.refused(g, self.wr(E_TABLE, verify=True), Refusal.CHECK_FAILED)  # type: ignore[arg-type]

    def test_the_read_back_needs_a_read_entry_for_both_cpus(self) -> None:
        base = policy_plain(write_entries=False)
        entries = tuple(e for e in base.msr_allow if e.msr != 0x650) + (
            MsrAllowEntry("r", 0x650, "read", frozenset({0})),
            MsrAllowEntry("w", 0x650, "write", frozenset({0}), "turbo_ratio_bounded"),
        )
        g = self.guard(dataclasses.replace(base, msr_allow=entries))
        self.baseline(g)
        self.refused(g, self.wr(E_TABLE), Refusal.CHECK_FAILED)  # verify cpu 8 is not covered by the read entry

    def test_the_check_is_applied_and_names_the_clause(self) -> None:
        g = self.guard()
        self.baseline(g)
        self.baseline(g, 0xCE)
        exc = self.refused(g, self.wr(e_table(*([49] * 8))), Refusal.CHECK_FAILED)  # no ratio_max: no raise
        self.assertIn("turbo_ratio_bounded", exc.detail)
        self.assertEqual(self.sim.plain_msr(0, 0x650), E_TABLE)  # nothing was written

    def test_session_limit_counts_plain_writes_but_not_reads(self) -> None:
        p = policy_plain(max_operations_per_session=1, ratio_max={"P": None, "E": 49}, max_ratio_step=1)
        g = self.guard(p)
        for _ in range(5):
            self.baseline(g)  # five plain reads: not counted
        self.baseline(g, 0xCE)
        g.execute(self.wr(e_table(*([49] * 8))), self.confirm)  # the first write uses the only operation
        self.refused(g, self.wr(e_table(*([48] * 8))), Refusal.SESSION_LIMIT)

    def test_boot_limit_counts_plain_writes_only(self) -> None:
        p = policy_plain(max_operations_per_boot=1, ratio_max={"P": None, "E": 49})
        g = self.guard(p)
        for _ in range(3):
            self.baseline(g)
        self.baseline(g, 0xCE)
        self.assertEqual(read_state(self.audit_path).attempts_by_boot, {})  # reads are not 'attempt' records
        g.execute(self.wr(e_table(*([49] * 8))), self.confirm)
        self.assertEqual(read_state(self.audit_path).attempts_by_boot, {"test-boot": 1})
        g2 = self.guard(p)  # a new process
        self.refused(g2, self.rd(), Refusal.BOOT_LIMIT)

    def test_dwell_separates_plain_writes(self) -> None:
        p = policy_plain(min_dwell_s=60.0, ratio_max={"P": None, "E": 49})
        g = self.guard(p)
        self.baseline(g)
        self.baseline(g)  # a plain read neither waits nor starts a dwell
        g.execute(self.wr(e_table(*([49] * 8))), self.confirm)
        exc = self.refused(g, self.wr(e_table(*([48] * 8))), Refusal.DWELL)
        self.assertIsNotNone(exc.retry_after_s)
        self.advance(61)
        g.execute(self.wr(e_table(*([48] * 8))), self.confirm)

    def test_a_held_lock_refuses(self) -> None:
        g = self.guard()
        self.lock_path.write_text("999\n")
        self.refused(g, self.rd(), Refusal.LOCKED)

    def test_health_gate_before_refuses_and_after_trips(self) -> None:
        g = self.guard(health_gate=lambda phase: (phase != "before", "no"))
        self.refused(g, self.rd(), Refusal.HEALTH_GATE)
        g = self.guard(health_gate=lambda phase: (phase != "after", "tainted"), lock_path=self.dir / "l2")
        g.execute(self.rd(), self.confirm)
        self.assertTrue(g.tripped)
        self.refused(g, self.rd(), Refusal.TRIPPED)


class Executed(PlainCase):
    def p49(self) -> Policy:
        return policy_plain(ratio_max={"P": None, "E": 49})

    def test_a_read_returns_the_word_is_audited_as_msr_read_and_is_not_an_attempt(self) -> None:
        g = self.guard()
        res = g.execute(self.rd(0xCE), self.confirm)
        self.assertEqual((res.value, res.verify_value, res.verified), (PLATFORM, None, None))
        events = [(r["event"], r.get("msr")) for r in self.records()]
        self.assertEqual(events, [("msr_read", "0x000000CE"), ("result", "0x000000CE")])
        self.assertEqual(self.records()[1]["value"], "0x08040838F1812500")
        self.assertEqual(self.sim.writes, [])
        self.assertEqual(self.transport.calls, [])

    def test_a_raise_within_the_ceiling_is_written_and_read_back_on_both_cpus(self) -> None:
        g = self.guard(self.p49())
        self.baseline(g)
        self.baseline(g, 0xCE)
        new = e_table(*([49] * 8))
        res = g.execute(self.wr(new), self.confirm)
        self.assertEqual((res.value, res.verify_value, res.verified), (new, new, True))
        self.assertEqual(self.sim.writes, [(0, 0x650, new)])
        self.assertEqual([r for r in self.sim.reads if r[1] == 0x650], [(0, 0x650), (0, 0x650), (8, 0x650)])  # baseline, read-back x2
        events = [r["event"] for r in self.records()]
        self.assertEqual(events.count("attempt"), 1)
        self.assertEqual(events.count("readback"), 2)
        attempt = next(r for r in self.records() if r["event"] == "attempt")
        self.assertEqual((attempt["msr"], attempt["msr_kind"], attempt["req_data"], attempt["baseline"]), ("0x00000650", "write", "0x3131313131313131", "0x3030303030303030"))
        self.assertEqual(attempt["boot_id"], "test-boot")
        self.assertFalse(g.tripped)

    def test_the_p_table_is_bounded_by_the_p_ceiling(self) -> None:
        g = self.guard(policy_plain(ratio_max={"P": 58, "E": None}))
        self.baseline(g, 0x1AD)
        new = table(58, 58, 56, 56, 56, 56, 56, 56)
        res = g.execute(self.wr(new, 0x1AD), self.confirm)
        self.assertTrue(res.verified)
        self.assertEqual(self.sim.plain_msr(0, 0x1AD), new)
        self.refused(g, self.wr(table(59, 59, 56, 56, 56, 56, 56, 56), 0x1AD), Refusal.CHECK_FAILED)

    def test_a_mismatch_on_the_written_cpu_trips(self) -> None:
        self.sim.ignore_plain_writes(0x650)
        g = self.guard(self.p49())
        self.baseline(g)
        res = g.execute(self.wr(e_table(*([49] * 8))), self.confirm)
        self.assertFalse(res.verified)
        self.assertTrue(g.tripped)
        self.assertIn("cpu 0", g.trip_reason)
        self.assertEqual(len([r for r in self.sim.reads if r == (8, 0x650)]), 0)  # the first mismatch stops the read-backs
        self.assertEqual([r["event"] for r in self.records()][-2:], ["readback", "trip"])
        self.refused(g, self.rd(), Refusal.TRIPPED)

    def test_a_per_cpu_msr_trips_on_the_verify_cpu(self) -> None:
        self.sim = SimMsrPort(SimConfig(plain_msrs_per_cpu=True))
        g = self.guard(self.p49())
        self.baseline(g)
        res = g.execute(self.wr(e_table(*([49] * 8))), self.confirm)
        self.assertEqual((res.value, res.verified), (e_table(*([49] * 8)), False))
        self.assertEqual(res.verify_value, E_TABLE)  # cpu 8 still has the old table: not package-scoped
        self.assertTrue(g.tripped)
        self.assertIn("not package-scoped", g.trip_reason)
        self.refused(g, self.rd(), Refusal.TRIPPED)

    def test_a_port_exception_on_the_write_trips_and_keeps_it_revertible(self) -> None:
        self.sim.eio(0, 0x650)
        g = self.guard(self.p49())
        # the baseline read itself fails on the faulty register: use a clean one first
        sim = self.sim
        sim.clear_faults()
        self.baseline(g)
        sim.eio(0, 0x650)
        with self.assertRaises(MsrError):
            g.execute(self.wr(e_table(*([49] * 8))), self.confirm)
        self.assertTrue(g.tripped)
        self.assertEqual(len(g.revert_plan()), 1)

    def test_a_port_that_answers_garbage_trips(self) -> None:
        class Garbage(SimMsrPort):
            def rdmsr(self, cpu: int, msr: int) -> int:
                return -1

        self.sim = Garbage(SimConfig())
        g = self.guard()
        with self.assertRaises(ValueError):
            g.execute(self.rd(), self.confirm)
        self.assertTrue(g.tripped)

    def test_the_revert_plan_restores_the_baseline_and_orders_by_direction(self) -> None:
        g = self.guard(self.p49())
        self.baseline(g)
        self.baseline(g, 0xCE)
        g.execute(self.wr(e_table(*([49] * 8))), self.confirm)  # a raise: its undo lowers a bucket
        undo = g.revert_plan()
        self.assertEqual(undo, [MsrRequest(0, 0x650, E_TABLE, "revert", "write", 8)])
        # and the undo is itself a legal request (after the dwell): the baseline is within every bound
        res = g.execute(undo[0], self.confirm)
        self.assertTrue(res.verified)
        self.assertEqual(self.sim.plain_msr(8, 0x650), E_TABLE)

    def test_revert_of_a_lowering_comes_after_the_mailbox_undos(self) -> None:
        g = self.guard(self.p49())
        self.baseline(g)
        low = e_table(*([47] * 8))
        g.execute(self.wr(low), self.confirm)
        self.assertEqual(g.revert_plan()[-1].value, E_TABLE)  # raises a bucket back: voltage first, then this

    def test_plain_requests_never_reach_the_transport_and_nothing_else_writes(self) -> None:
        g = self.guard(self.p49())
        self.baseline(g)
        g.execute(self.wr(e_table(*([49] * 8))), self.confirm)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual([w[1] for w in self.sim.writes], [0x650])

    def test_mailbox_requests_are_unchanged_by_the_new_port(self) -> None:
        g = self.guard()
        res = g.execute(Request(0, 0x10, 0, 0, 0, "read"), self.confirm)
        self.assertEqual(res.response.rc, 0)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.sim.reads, [])


if __name__ == "__main__":
    unittest.main()
